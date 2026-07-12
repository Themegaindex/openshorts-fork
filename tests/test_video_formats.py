import pytest

from video_formats import (
    EVEN_PAD_FILTER,
    fit_even_output_dimensions,
    normalize_output_format,
    output_aspect_ratio,
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
