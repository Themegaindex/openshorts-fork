"""Real libass renders: portrait captions land inside the safe zone.

Every style is burned with the production filter into a single synthetic
1080x1920 frame at a chosen clip second; the caption box is the bounding box
of the pixels that differ from the clean frame.
"""
import shutil
import subprocess

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageChops  # noqa: E402

import caption_placement as cp  # noqa: E402
import subtitles  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg and ffprobe",
)

W, H = 1080, 1920
GREY = 0x60


def _w(text, start, end):
    return {"word": " " + text, "start": start, "end": end}


TRANSCRIPT = {"segments": [{"start": 0, "end": 10, "text": "", "words": [
    _w("Das", 0.2, 0.5), _w("ist", 0.5, 0.8), _w("wirklich", 0.8, 1.2),
    _w("eine", 1.2, 1.5), _w("gute", 1.5, 1.8), _w("Frage", 1.8, 2.3),
    _w("Gerichte", 4.6, 5.2), _w("entscheiden", 5.2, 6.0),
    # Starts more than every style's block duration after the last block
    # began, so it opens its own block across the cut at 8.0 s.
    _w("jetzt", 7.8, 8.3), _w("hier", 8.3, 9.0),
]}]}
TIMELINE = [
    cp.LayoutSegment(0.0, 3.0, cp.LAYOUT_SPLIT_STACKED),
    cp.LayoutSegment(3.0, 8.0, cp.LAYOUT_TRACK),
    cp.LayoutSegment(8.0, 11.0, cp.LAYOUT_SPLIT_STACKED),
]
STYLES = {
    "classic": dict(fontsize=24, font_name="Verdana", border_width=2),
    "karaoke": dict(fontsize=24, font_name="Verdana", border_width=2, highlight_color="#FE2C55"),
    "neon_sweep": dict(preset="neon_sweep", fontsize=20, font_name="Arial Black"),
    "neon_tube": dict(preset="neon_tube", fontsize=20, font_name="Arial Black", frame_size=(W, H)),
    "rainbow_word": dict(preset="rainbow_word", fontsize=29, font_name="Arial Black"),
}
ZONE = cp.SafeZone()
TOP, BOTTOM = ZONE.top * H, H - ZONE.bottom * H
LEFT, RIGHT = ZONE.side * W, W - ZONE.side * W


def _frame(subtitle_filter, seconds):
    """One clean frame stamped with clip second ``seconds``, then burned.

    The stamp goes in front of the subtitle filter: FFmpeg resets an input's
    first timestamp to zero, so stamping the source itself would not work.
    """
    source = f"color=c=0x{GREY:02x}{GREY:02x}{GREY:02x}:s={W}x{H}:r=10:d=0.1"
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", source,
         "-vf", f"setpts=PTS+{seconds}/TB,{subtitle_filter}",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True, capture_output=True, timeout=60,
    ).stdout
    return Image.frombytes("RGB", (W, H), raw)


def _caption_box(frame, threshold=24):
    red, green, blue = ImageChops.difference(frame, Image.new("RGB", (W, H), (GREY,) * 3)).split()
    strongest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    return strongest.point(lambda value: 255 if value > threshold else 0).getbbox()


def _ass(tmp_path, style, position):
    placement = cp.placement_for((W, H), position, timeline=TIMELINE, safe_zone=ZONE)
    path = tmp_path / f"{style}_{position}.ass"
    if style == "classic":
        assert subtitles.generate_classic_ass(TRANSCRIPT, 0, 10, str(path), placement=placement,
                                              **STYLES[style])
    else:
        assert subtitles.generate_ass(TRANSCRIPT, 0, 10, str(path), placement=placement, **STYLES[style])
    return subtitles.build_subtitle_filter(str(path))


def _assert_inside(box):
    assert box is not None, "no caption rendered"
    x0, y0, x1, y1 = box
    assert x0 >= LEFT and x1 <= RIGHT and y0 >= TOP and y1 <= BOTTOM, box
    assert abs((x0 + x1) / 2 - W / 2) <= 8, box


@pytest.mark.parametrize("style", list(STYLES))
def test_auto_captions_sit_on_the_seam_over_split_and_at_the_bottom_elsewhere(tmp_path, style):
    subtitle_filter = _ass(tmp_path, style, "auto")
    # libass centres the line box; the visible ink of lower-case text sits a
    # little lower, so the middle is checked to within 16 px.
    for seconds in (1.0, 7.9):   # over SPLIT, and the block that crosses the cut
        box = _caption_box(_frame(subtitle_filter, seconds))
        _assert_inside(box)
        assert abs((box[1] + box[3]) / 2 - H / 2) <= 16, (seconds, box)
    box = _caption_box(_frame(subtitle_filter, 5.0))   # over TRACK
    _assert_inside(box)
    # Close to the line: only the room kept for outline and glow (up to the
    # 22 px bloom of Neon Sweep and Rainbow Word) and the descender lies between.
    assert BOTTOM - box[3] <= 64, box


@pytest.mark.parametrize("style", list(STYLES))
def test_top_captions_start_below_the_upper_safe_line(tmp_path, style):
    box = _caption_box(_frame(_ass(tmp_path, style, "top"), 1.0))
    _assert_inside(box)
    # Room for outline and glow plus the gap between ascender and capitals.
    assert box[1] - TOP <= 80, box


@pytest.mark.parametrize("style", ["classic", "karaoke"])
@pytest.mark.parametrize("word,lines", [
    ("eigentlich", 1),                  # fits as it is
    ("Großbuchstaben", 1),              # the block shrinks a little
    ("Bundesverfassungsgericht", 2),    # too wide even at 70 %: split
])
def test_long_words_stay_inside_the_side_margins(tmp_path, style, word, lines):
    transcript = {"segments": [{"start": 0, "end": 2, "text": "", "words": [_w(word, 0.2, 1.5)]}]}
    placement = cp.placement_for((W, H), "bottom", safe_zone=ZONE)
    path = tmp_path / f"{style}.ass"
    if style == "classic":
        assert subtitles.generate_classic_ass(transcript, 0, 2, str(path), placement=placement, fontsize=24)
    else:
        assert subtitles.generate_ass(transcript, 0, 2, str(path), placement=placement, fontsize=24)
    box = _caption_box(_frame(subtitles.build_subtitle_filter(str(path)), 1.0))
    _assert_inside(box)
    # Verdana at size 24 is 20 script units = 133 px high per line.
    line_height = 20 * H / 288
    assert (lines - 1) * line_height < box[3] - box[1] < lines * line_height, box


@pytest.mark.parametrize("bg_opacity", [0.0, 0.6])
def test_classic_ass_looks_exactly_like_the_srt_burn(tmp_path, bg_opacity):
    """Same renderer, same pixels: the portrait Classic file keeps the look
    (font, size, outline or box, colours) of the legacy SRT burn."""
    opts = dict(fontsize=24, font_name="Verdana", font_color="#FFFF00", border_color="#112233",
                border_width=3, bg_color="#FF0000", bg_opacity=bg_opacity)
    srt = tmp_path / "legacy.srt"
    ass = tmp_path / "legacy.ass"
    assert subtitles.generate_srt(TRANSCRIPT, 0, 10, str(srt))
    assert subtitles.generate_classic_ass(TRANSCRIPT, 0, 10, str(ass), alignment="bottom", **opts)
    srt_frame = _frame(subtitles.build_subtitle_filter(str(srt), alignment="bottom", **opts), 1.0)
    ass_filter = f"subtitles='{subtitles._escape_ffmpeg_filter_value(str(ass))}'"
    assert _caption_box(srt_frame) is not None
    assert ImageChops.difference(srt_frame, _frame(ass_filter, 1.0)).getbbox() is None


@pytest.mark.parametrize("style", list(STYLES))
def test_captions_go_up_over_a_low_face_and_do_not_flip_on_a_short_scene(tmp_path, style):
    """Low face 0-3 s, a short scene 3-3.8 s, then a high face: top, top, bottom."""
    faces = [
        cp.LayoutSegment(0.0, 3.0, cp.LAYOUT_TRACK, face_top=0.62, face_bottom=0.74),
        # High faces below the top band of every style (Rainbow Word at 29
        # reaches 31 % of the height).
        cp.LayoutSegment(3.0, 3.8, cp.LAYOUT_TRACK, face_top=0.35, face_bottom=0.50),
        cp.LayoutSegment(3.8, 11.0, cp.LAYOUT_TRACK, face_top=0.35, face_bottom=0.50),
    ]
    transcript = {"segments": [{"start": 0, "end": 10, "text": "", "words": [
        _w("Das", 0.2, 0.5), _w("ist", 0.5, 0.8), _w("gut", 0.8, 1.4),
        _w("kurz", 3.1, 3.7),
        _w("Gerichte", 6.6, 7.2), _w("entscheiden", 7.2, 8.0),
    ]}]}
    placement = cp.placement_for((W, H), "auto", timeline=faces, safe_zone=ZONE)
    path = tmp_path / f"{style}.ass"
    if style == "classic":
        assert subtitles.generate_classic_ass(transcript, 0, 10, str(path), placement=placement, **STYLES[style])
    else:
        assert subtitles.generate_ass(transcript, 0, 10, str(path), placement=placement, **STYLES[style])
    subtitle_filter = subtitles.build_subtitle_filter(str(path))
    for seconds in (1.0, 3.4):
        box = _caption_box(_frame(subtitle_filter, seconds))
        _assert_inside(box)
        assert box[1] - TOP <= 80, (seconds, box)        # at the top
    box = _caption_box(_frame(subtitle_filter, 7.0))
    _assert_inside(box)
    assert BOTTOM - box[3] <= 64, box                    # back at the bottom


@pytest.mark.parametrize("style", ["classic", "karaoke"])
@pytest.mark.parametrize("word", ["программа", "сегодня", "πρόγραμμα"])
def test_cyrillic_and_greek_words_stay_whole_at_full_size(tmp_path, style, word):
    """They used to count one font size per letter: shrunk to 70 % and split."""
    transcript = {"segments": [{"start": 0, "end": 2, "text": "", "words": [_w(word, 0.2, 1.5)]}]}
    placement = cp.placement_for((W, H), "bottom", safe_zone=ZONE)
    path = tmp_path / f"{style}.ass"
    if style == "classic":
        assert subtitles.generate_classic_ass(transcript, 0, 2, str(path), placement=placement, fontsize=24)
    else:
        assert subtitles.generate_ass(transcript, 0, 2, str(path), placement=placement, fontsize=24)
    content = path.read_text(encoding="utf-8-sig")
    assert "\\fscy" not in content and word in content   # no shrink, no split
    box = _caption_box(_frame(subtitles.build_subtitle_filter(str(path)), 1.0))
    _assert_inside(box)
    # One line: Greek descenders and accents reach a little past the line height.
    assert box[3] - box[1] < 1.5 * 20 * H / 288, box
