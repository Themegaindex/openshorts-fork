import pytest

from video_formats import (
    EVEN_PAD_FILTER,
    delivery_dimensions,
    delivery_scale_filter,
    delivery_width_from_env,
    fit_even_output_dimensions,
    normalize_output_format,
    output_aspect_ratio,
    resize_interpolation,
    upscale_interpolation_from_env,
    with_even_padding,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "vertical"),
        ("vertical", "vertical"),
        ("original", "original"),
        ("square", "square"),
        ("auto", "vertical"),
        ("horizontal", "original"),
        ("nonsense", "vertical"),
    ],
)
def test_output_format_is_canonical_with_legacy_aliases(raw, expected):
    assert normalize_output_format(raw) == expected


def test_original_has_no_target_aspect():
    assert output_aspect_ratio("original") is None
    assert output_aspect_ratio("horizontal") is None


@pytest.mark.parametrize(
    "source,aspect,expected",
    [
        ((1920, 1080), 9 / 16, (608, 1080)),
        ((1080, 1920), 9 / 16, (1080, 1920)),
        ((1080, 1080), 9 / 16, (608, 1080)),
        ((720, 1280), 1.0, (720, 720)),
        ((1920, 1080), 1.0, (1080, 1080)),
    ],
)
def test_output_dimensions_fit_both_source_axes(source, aspect, expected):
    dimensions = fit_even_output_dimensions(*source, aspect)
    assert dimensions == expected
    assert dimensions[0] <= source[0]
    assert dimensions[1] <= source[1]
    assert dimensions[0] % 2 == dimensions[1] % 2 == 0


def test_invalid_geometry_is_rejected():
    with pytest.raises(ValueError):
        fit_even_output_dimensions(0, 1080, 9 / 16)


def test_even_padding_is_appended_exactly_once():
    assert with_even_padding("eq=contrast=1.1") == f"eq=contrast=1.1,{EVEN_PAD_FILTER}"
    assert with_even_padding(EVEN_PAD_FILTER) == EVEN_PAD_FILTER


SOURCES = [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160), (640, 360),
           (720, 1280), (1080, 1920), (2160, 3840), (1080, 1350)]


@pytest.mark.parametrize("source", SOURCES)
def test_delivery_dimensions_are_exact_full_hd_for_every_source(source):
    assert delivery_dimensions(*source, 9 / 16) == (1080, 1920)
    assert delivery_dimensions(*source, 1.0) == (1080, 1080)


@pytest.mark.parametrize(
    "source,legacy",
    [((1280, 720), (404, 720)), ((1920, 1080), (608, 1080)),
     ((2560, 1440), (810, 1440)), ((3840, 2160), (1214, 2160))],
)
def test_delivery_width_zero_keeps_legacy_crop_size(source, legacy):
    assert delivery_dimensions(*source, 9 / 16, 0) == legacy == fit_even_output_dimensions(*source, 9 / 16)


@pytest.mark.parametrize(
    "width,aspect,expected",
    [(720, 9 / 16, (720, 1280)), (1440, 9 / 16, (1440, 2560)),
     (2160, 9 / 16, (2160, 3840)), (1440, 1.0, (1440, 1440))],
)
def test_delivery_dimensions_custom_width_even_and_on_aspect(width, aspect, expected):
    size = delivery_dimensions(1920, 1080, aspect, width)
    assert size == expected
    assert size[0] % 2 == 0 and size[1] % 2 == 0
    assert abs(size[0] / size[1] - aspect) < 0.002


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 1080), ("", 1080), ("1080", 1080), ("1440", 1440), ("1081", 1080),
     ("0", 0), ("native", 0), ("-1", 0), ("abc", 1080), ("200", 360), ("99999", 2160)],
)
def test_delivery_width_env_parsing(monkeypatch, raw, expected):
    monkeypatch.delenv("SHORTS_DELIVERY_WIDTH", raising=False)
    assert delivery_width_from_env(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("", "cubic"), ("nearest", "cubic"), ("LANCZOS", "lanczos"), ("cubic", "cubic"), ("linear", "linear")],
)
def test_upscale_interpolation_env_parsing(raw, expected):
    assert upscale_interpolation_from_env(raw) == expected


def test_resize_interpolation_shrink_down_upscaler_up():
    assert resize_interpolation(1214, 2160, 1080, 1920) == "shrink"
    assert resize_interpolation(2160, 2160, 1080, 1080) == "shrink"
    assert resize_interpolation(404, 720, 1080, 1920) == "cubic"
    assert resize_interpolation(404, 720, 1080, 1920, upscale="lanczos") == "lanczos"
    assert resize_interpolation(720, 720, 1080, 1080) == "cubic"
    assert resize_interpolation(1080, 1920, 1080, 1920) is None


def test_delivery_scale_filter_cover_crop():
    up = delivery_scale_filter(720, 1280, 1080, 1920)
    assert "scale=1080:1920:force_original_aspect_ratio=increase:flags=bicubic" in up
    assert up.endswith("crop=1080:1920,setsar=1")
    assert "flags=lanczos" in delivery_scale_filter(720, 1280, 1080, 1920, upscale="lanczos")
    assert "flags=area" in delivery_scale_filter(2160, 3840, 1080, 1920)
    assert delivery_scale_filter(1080, 1920, 1080, 1920) is None
    assert "flags=bicubic" in delivery_scale_filter(0, 0, 1080, 1920)


def test_invalid_delivery_geometry_is_rejected():
    with pytest.raises(ValueError):
        delivery_dimensions(0, 1080, 9 / 16)
    with pytest.raises(ValueError):
        delivery_dimensions(1920, 1080, 0)
