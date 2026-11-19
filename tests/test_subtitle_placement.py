"""ASS output of every caption style on a portrait placement."""
import re
import sys
import types
import unicodedata

import pytest

import caption_fonts
import caption_placement as cp
import subtitles
from subtitles import build_subtitle_filter, generate_ass, generate_classic_ass


@pytest.fixture(autouse=True, params=["win32", "linux"])
def font_table(request, monkeypatch):
    """Every test runs with both width tables: the Windows fonts and the
    Docker substitutes. Otherwise the result would depend on the machine
    running the test, and a word near the edge of the line could pass on one
    and fail on the other."""
    monkeypatch.setattr(caption_fonts, "sys", types.SimpleNamespace(platform=request.param))
    return request.param


def _w(text, start, end):
    return {"word": " " + text, "start": start, "end": end}


# Block A plays over a stacked SPLIT, B over TRACK, C straddles the cut at
# 8.5 s (0.2 s TRACK, 1.0 s SPLIT). The pauses are longer than every style's
# block duration, so each style starts new blocks at the same words.
WORDS = [
    _w("Das", 0.2, 0.5), _w("ist", 0.5, 0.8), _w("wirklich", 0.8, 1.2),
    _w("eine", 1.2, 1.5), _w("gute", 1.5, 1.8), _w("Frage", 1.8, 2.3),
    _w("Bundesverfassungsgericht", 4.6, 5.4), _w("entscheidet", 5.4, 6.0), _w("heute", 6.0, 6.4),
    _w("jetzt", 8.3, 8.8), _w("hier", 8.8, 9.5),
]
TRANSCRIPT = {"segments": [{"start": 0, "end": 10, "text": "", "words": WORDS}]}
TIMELINE = [
    cp.LayoutSegment(0.0, 3.0, cp.LAYOUT_SPLIT_STACKED),
    cp.LayoutSegment(3.0, 8.5, cp.LAYOUT_TRACK),
    cp.LayoutSegment(8.5, 11.0, cp.LAYOUT_SPLIT_STACKED),
]
PORTRAIT = (1080, 1920)
PRESETS = ["neon_sweep", "neon_tube", "rainbow_word"]


def _auto():
    return cp.placement_for(PORTRAIT, "auto", timeline=TIMELINE, safe_zone=cp.SafeZone())


def _seconds(stamp):
    hours, minutes, seconds = stamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _events(path):
    events = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("Dialogue:"):
            fields = line.split(",", 9)
            events.append((_seconds(fields[1]), _seconds(fields[2]), fields[3], fields[9]))
    return events


def _styles(path):
    styles = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("Style: "):
            fields = line[len("Style: "):].split(",")
            styles[fields[0]] = fields
    return styles


def _styles_between(events, start, end):
    return {style for event_start, event_end, style, _ in events
            if event_start < end and event_end > start}


def _visible(text):
    return re.sub(r"\{[^}]*\}", "", text)


def _generate(tmp_path, style, placement, **opts):
    out = tmp_path / f"{style}.ass"
    if style == "classic":
        assert generate_classic_ass(TRANSCRIPT, 0, 10, str(out), placement=placement, **opts)
    elif style == "karaoke":
        assert generate_ass(TRANSCRIPT, 0, 10, str(out), placement=placement, **opts)
    else:
        assert generate_ass(TRANSCRIPT, 0, 10, str(out), placement=placement,
                            preset=style, font_name="Arial Black", **opts)
    return out


@pytest.mark.parametrize("style", ["classic", "karaoke"] + PRESETS)
def test_auto_puts_split_blocks_on_the_seam_and_the_rest_at_the_bottom(tmp_path, style):
    out = _generate(tmp_path, style, _auto())
    events = _events(out)
    assert set(_styles(out)) == {"Bottom", "Middle", "Top"}
    assert _styles_between(events, 0.2, 2.3) == {"Middle"}
    assert _styles_between(events, 4.6, 6.4) == {"Bottom"}
    # The block across the cut stays in one place, also before the cut.
    assert _styles_between(events, 8.3, 8.5) == {"Middle"}
    assert _styles_between(events, 8.3, 9.5) == {"Middle"}


@pytest.mark.parametrize("style", ["classic", "karaoke"] + PRESETS)
@pytest.mark.parametrize("position", ["top", "middle", "bottom"])
def test_fixed_positions_pin_one_slot_for_the_clip(tmp_path, style, position):
    placement = cp.placement_for(PORTRAIT, position, timeline=TIMELINE, safe_zone=cp.SafeZone())
    events = _events(_generate(tmp_path, style, placement))
    assert {event[2] for event in events} == {cp.SLOT_STYLE_NAMES[position]}


@pytest.mark.parametrize("style", ["classic", "karaoke"] + PRESETS)
def test_slot_styles_differ_only_in_alignment_and_margins(tmp_path, style):
    styles = _styles(_generate(tmp_path, style, _auto()))
    bodies = {tuple(fields[1:18]) for fields in styles.values()}
    assert len(bodies) == 1
    alignments = {name: fields[18] for name, fields in styles.items()}
    assert alignments == {"Bottom": "2", "Middle": "5", "Top": "8"}


def test_karaoke_keeps_its_look_and_states_the_canvas(tmp_path):
    legacy = tmp_path / "legacy.ass"
    assert generate_ass(TRANSCRIPT, 0, 10, str(legacy), fontsize=24, border_width=3)
    portrait = _generate(tmp_path, "karaoke", _auto(), fontsize=24, border_width=3)
    content = portrait.read_text(encoding="utf-8-sig")
    assert "PlayResX: 384\nPlayResY: 288\n" in content
    legacy_style = _styles(legacy)["Default"]
    for fields in _styles(portrait).values():
        assert fields[1:18] == legacy_style[1:18]
    bottom = _styles(portrait)["Bottom"]
    # 14 % of 384 plus the 3 unit outline; 25 % of 288 plus the outline.
    assert bottom[19:22] == ["57", "57", "75"]


@pytest.mark.parametrize("bg_opacity", [0.0, 0.6])
def test_classic_ass_uses_the_burn_style_of_the_srt_path(tmp_path, bg_opacity):
    opts = dict(fontsize=24, font_name="Verdana", font_color="#FFFF00",
                border_color="#112233", border_width=3, bg_color="#FF0000", bg_opacity=bg_opacity)
    styles = _styles(_generate(tmp_path, "classic", _auto(), **opts))
    force_style = dict(
        item.split("=", 1)
        for item in build_subtitle_filter("x.srt", **opts).split("force_style='")[1].rstrip("'").split(",")
    )
    for fields in styles.values():
        assert fields[1] == force_style["Fontname"]
        assert fields[2] == force_style["Fontsize"]
        assert fields[3] == force_style["PrimaryColour"]
        assert fields[5] == force_style["OutlineColour"]
        assert fields[6] == force_style["BackColour"]
        assert fields[7] == force_style["Bold"]
        assert fields[15] == force_style["BorderStyle"]
        assert fields[16] == force_style["Outline"]
        assert fields[17] == force_style["Shadow"]


def test_classic_ass_without_placement_reproduces_the_legacy_srt_geometry(tmp_path):
    out = tmp_path / "legacy.ass"
    assert generate_classic_ass(TRANSCRIPT, 0, 10, str(out), alignment="top")
    content = out.read_text(encoding="utf-8-sig")
    assert "PlayResX: 384\nPlayResY: 288\nScaledBorderAndShadow: yes\nYCbCr Matrix: None\n" in content
    assert _styles(out)["Default"][18:22] == ["8", "10", "10", "25"]
    assert {event[2] for event in _events(out)} == {"Default"}


def _layout(style, fontsize, font_name="Verdana", effect="none", uppercase=False):
    if style in ("classic", "karaoke"):
        return subtitles._caption_portrait(
            _auto(), font_name, fontsize, 2, 0.0, effect, uppercase,
        )[1]
    scale_x = {"neon_sweep": subtitles._NEON_SWEEP_SCALE_X, "neon_tube": subtitles._NEON_TUBE_SCALE_X,
               "rainbow_word": subtitles._RAINBOW_SCALE_X}[style]
    if style == "neon_tube":
        ink, blur = subtitles._NEON_TUBE_INK, subtitles._NEON_TUBE_BLUR   # 1920 px reference
    else:
        ink, blur = subtitles._SIGNATURE_INK, subtitles._SIGNATURE_BLUR
    return subtitles._signature_portrait(
        _auto(), font_name, fontsize, scale_x, subtitles.SIGNATURE_PLAY_RES_X, ink, blur,
    )[1]


def _single_words(words, start=0.0):
    """One block per word: each word sits 3 s after the previous one."""
    return {"segments": [{"start": 0, "end": 99, "text": "", "words": [
        _w(word, start + 3.0 * index, start + 3.0 * index + 0.5) for index, word in enumerate(words)
    ]}]}


BROKEN_ON_THE_REAL_CLIP = [
    "gefreestyled", "Freestyle", "überbrückt", "ursprünglich", "geschrieben", "Großbuchstaben",
    "eigentlich", "irgendwann", "quatschig", "intelligentes", "Drogenopfer",
]
SPLIT_WORD = re.compile(r"\w\\N\w")
SCALE_TAG = re.compile(r"\\fscx([0-9.]+)\\fscy([0-9.]+)")


# Words that fit the line at full size, Verdana 24, per width table. The
# Docker substitute (DejaVu Sans) is wider: "irgendwann" needs 100.3 % of
# the line there, "geschrieben" 103.6 %.
FULL_SIZE_AT_24 = {
    "win32": ("eigentlich", "irgendwann", "überbrückt", "Freestyle", "quatschig", "geschrieben"),
    "linux": ("eigentlich", "überbrückt", "Freestyle", "quatschig"),
}
# The widest of them, "Großbuchstaben", shrinks to 75.6 % (Windows) or
# 71.7 % (Docker): still above 70 %, so it is never split.
SMALLEST_SCALE_AT_24 = {"win32": 75, "linux": 71}


@pytest.mark.parametrize("style", ["classic", "karaoke"])
def test_words_that_fit_the_line_stay_whole(tmp_path, style, font_table):
    """At the dashboard default (Verdana 24) these words were split by the
    old character estimate although they fit."""
    transcript = _single_words(BROKEN_ON_THE_REAL_CLIP)
    out = tmp_path / f"{style}.ass"
    if style == "classic":
        assert generate_classic_ass(transcript, 0, 99, str(out), placement=_auto(), fontsize=24)
    else:
        assert generate_ass(transcript, 0, 99, str(out), placement=_auto(), fontsize=24)
    events = _events(out)
    assert not [event[3] for event in events if SPLIT_WORD.search(_visible(event[3]))]
    scales = {_visible(event[3]).strip(): SCALE_TAG.search(event[3]) for event in events}
    for word in FULL_SIZE_AT_24[font_table]:
        assert scales[word] is None, word   # fits at full size
    # The longer ones shrink a little instead of breaking apart.
    shrunk = {word: float(match.group(1)) for word, match in scales.items() if match}
    assert min(shrunk, key=shrunk.get) == "Großbuchstaben"
    assert shrunk["Großbuchstaben"] >= SMALLEST_SCALE_AT_24[font_table]


def test_a_word_wider_than_the_line_shrinks_the_whole_block(tmp_path):
    transcript = {"segments": [{"start": 0, "end": 9, "text": "", "words": [
        _w("Die", 0.0, 0.3), _w("Großbuchstaben", 0.3, 0.9),
    ]}]}
    out = tmp_path / "karaoke.ass"
    assert generate_ass(transcript, 0, 9, str(out), placement=_auto(), fontsize=24)
    events = _events(out)
    assert len(events) == 2   # one block, one event per word
    scale = SCALE_TAG.search(events[0][3])
    assert scale and 70 <= float(scale.group(1)) < 100 and scale.group(1) == scale.group(2)
    for event in events:
        assert event[3].startswith("{" + scale.group(0) + "}")   # the whole block
        # {\r} would reset the scale too, so the reset carries it again.
        assert "{\\r}" not in event[3]
        assert "{\\r" + scale.group(0) + "}" in event[3]
        assert not SPLIT_WORD.search(_visible(event[3]))


@pytest.mark.parametrize("effect,start,peak", [("pop", 75, 112), ("bounce", 85, 108)])
def test_pop_and_bounce_scale_with_a_shrunken_block(tmp_path, effect, start, peak):
    transcript = _single_words(["Großbuchstaben"])
    out = tmp_path / f"{effect}.ass"
    assert generate_ass(transcript, 0, 99, str(out), placement=_auto(), fontsize=24, effect=effect)
    text = _events(out)[0][3]
    scale = float(SCALE_TAG.search(text).group(1)) / 100
    assert f"\\fscx{subtitles._scale_percent(scale, start)}\\fscy" in text
    assert f"\\fscx{subtitles._scale_percent(scale, peak)}\\fscy" in text


@pytest.mark.parametrize("style", ["classic", "karaoke"])
def test_only_a_word_too_wide_even_at_the_minimum_scale_is_split(tmp_path, style):
    out = _generate(tmp_path, style, _auto(), fontsize=24)
    text = next(event[3] for event in _events(out) if event[0] >= 4.6)
    assert SCALE_TAG.search(text).group(1) == "70"
    word = next(part for part in _visible(text).split(" ") if part.startswith("Bundes"))
    pieces = word.split(r"\N")
    assert len(pieces) > 1 and "".join(pieces) == "Bundesverfassungsgericht"
    layout = _layout(style, 24)
    assert all(layout.width(piece, 0.7) <= layout.usable for piece in pieces)


def test_legacy_karaoke_never_splits_or_scales(tmp_path):
    out = tmp_path / "legacy.ass"
    assert generate_ass(TRANSCRIPT, 0, 10, str(out), fontsize=24)
    content = out.read_text(encoding="utf-8-sig")
    assert r"\N" not in content and "\\fscy" not in content


LONG_TRANSCRIPT = {"segments": [{"start": 0, "end": 99, "text": "", "words": [
    _w(word, 0.25 * index, 0.25 * index + 0.24)
    for index, word in enumerate(
        ("und dann haben wir das eigentlich irgendwann ursprünglich zusammen geschrieben "
         "weil die Großbuchstaben quatschig aussehen aber intelligentes Design hilft "
         "wirklich immer so oder auch nicht ganz").split() * 3
    )
]}]}


@pytest.mark.parametrize("style,fontsize", [
    ("classic", 24), ("karaoke", 24), ("classic", 40), ("neon_sweep", 20), ("neon_tube", 20),
    ("rainbow_word", 29), ("neon_sweep", 40),
])
def test_portrait_blocks_are_at_most_two_lines(style, fontsize):
    layout = _layout(style, fontsize, font_name="Arial Black" if style in PRESETS else "Verdana")
    max_chars = 20 if style in ("classic", "karaoke") else float("inf")
    blocks = subtitles._collect_word_blocks(LONG_TRANSCRIPT, 0, 99, max_chars, 2.6, fits=layout.fits)
    assert sum(len(block) for block in blocks) == len(LONG_TRANSCRIPT["segments"][0]["words"])
    for block in blocks:
        texts = [layout.display(word["word"]) for word in block]
        assert layout.line_count(texts, layout.scale_for(texts)) <= 2, texts


@pytest.mark.parametrize("preset", PRESETS)
@pytest.mark.parametrize("fontsize", [20, 29, 40])
def test_signature_lines_fit_between_the_safe_side_margins(tmp_path, preset, fontsize):
    out = tmp_path / f"{preset}.ass"
    assert generate_ass(LONG_TRANSCRIPT, 0, 99, str(out), placement=_auto(), preset=preset,
                        font_name="Arial Black", fontsize=fontsize)
    layout = _layout(preset, fontsize, font_name="Arial Black")
    styles = _styles(out)
    for event in _events(out):
        match = SCALE_TAG.search(event[3])
        scale = float(match.group(2)) / 100 if match else 1.0
        assert scale >= subtitles.PORTRAIT_FLOOR_BLOCK_SCALE
        visible = _visible(event[3])
        lines = visible.split(r"\N")
        assert len(lines) <= 2, visible
        for line in lines:
            assert layout.width(line.strip(), scale) <= layout.usable, (preset, line)
    assert int(styles["Bottom"][19]) == round(subtitles.SIGNATURE_PLAY_RES_X - layout.usable) // 2


def test_neon_tube_keeps_contour_and_glow_inside_the_safe_zone(tmp_path):
    styles = _styles(_generate(tmp_path, "neon_tube", _auto(), frame_size=PORTRAIT))
    # 14 % of 162 and 25 % / 10 % of 288, each plus the 1.6 unit contour and
    # the 8 px glow blur (1.2 units either way on a 1080x1920 frame).
    assert styles["Bottom"][19:22] == ["26", "26", "75"]
    assert styles["Top"][21] == "32"


def test_neon_sweep_keeps_its_wide_bloom_inside_the_safe_zone(tmp_path):
    styles = _styles(_generate(tmp_path, "neon_sweep", _auto()))
    # The 22 px bloom is 3.3 units either way on 1080x1920, plus the 0.12 unit core.
    assert styles["Bottom"][19:22] == ["27", "27", "76"]
    assert styles["Top"][21] == "33"


def test_karaoke_glow_keeps_its_blur_inside_the_safe_zone(tmp_path):
    styles = _styles(_generate(tmp_path, "karaoke", _auto(), effect="glow", border_width=2))
    # Glow outline 4 units, plus \blur4: 1.42 x-units and 0.6 y-units.
    assert styles["Bottom"][19:22] == ["60", "60", "77"]


def test_signature_presets_without_placement_keep_the_signature_style(tmp_path):
    out = tmp_path / "tube.ass"
    assert generate_ass(TRANSCRIPT, 0, 10, str(out), preset="neon_tube",
                        font_name="Arial Black", frame_size=PORTRAIT)
    assert set(_styles(out)) == {"Signature"}
    assert {event[2] for event in _events(out)} == {"Signature"}


def test_dubbed_clip_routes_through_the_placement(tmp_path, monkeypatch):
    class FakeCapture:
        def __init__(self, _path):
            pass

        def get(self, prop):
            return {1: 100, 5: 10}.get(prop, 0)

        def release(self):
            pass

    fake_cv2 = types.SimpleNamespace(VideoCapture=FakeCapture, CAP_PROP_FPS=5, CAP_PROP_FRAME_COUNT=1)
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    monkeypatch.setattr(subtitles, "transcribe_audio", lambda _path: TRANSCRIPT)

    classic = tmp_path / "classic.ass"
    assert subtitles.generate_srt_from_video("dub.mp4", str(classic), placement=_auto(), fontsize=24)
    assert set(_styles(classic)) == {"Bottom", "Middle", "Top"}
    assert _styles_between(_events(classic), 0.2, 2.3) == {"Middle"}

    karaoke = tmp_path / "karaoke.ass"
    assert subtitles.generate_srt_from_video("dub.mp4", str(karaoke), style="karaoke",
                                             placement=_auto(), preset="neon_sweep")
    assert _styles_between(_events(karaoke), 4.6, 6.4) == {"Bottom"}

    srt = tmp_path / "legacy.srt"
    assert subtitles.generate_srt_from_video("dub.mp4", str(srt))
    assert srt.read_text(encoding="utf-8-sig").startswith("1\n00:00:00,200 --> ")


# --- Face rule in the generated files ------------------------------------

FACE_TIMELINE = [
    cp.LayoutSegment(0.0, 3.0, cp.LAYOUT_TRACK, face_top=0.62, face_bottom=0.74),   # low face
    cp.LayoutSegment(3.0, 3.8, cp.LAYOUT_TRACK, face_top=0.30, face_bottom=0.45),   # short scene
    cp.LayoutSegment(3.8, 11.0, cp.LAYOUT_TRACK, face_top=0.30, face_bottom=0.45),  # high face
]


@pytest.mark.parametrize("style", ["classic", "karaoke"] + PRESETS)
def test_auto_goes_up_when_the_face_sits_low_and_only_when_the_top_is_free(tmp_path, style):
    placement = cp.placement_for(PORTRAIT, "auto", timeline=FACE_TIMELINE, safe_zone=cp.SafeZone())
    events = _events(_generate(tmp_path, style, placement))
    assert _styles_between(events, 0.2, 2.3) == {"Top"}
    assert _styles_between(events, 4.6, 6.4) == {"Bottom"}
    hook_dir = tmp_path / "hook"
    hook_dir.mkdir()
    hooked = _events(_generate(hook_dir, style, placement.with_hook(cp.SLOT_TOP)))
    assert {event[2] for event in hooked} == {"Bottom"}


@pytest.mark.parametrize("style,fontsize", [("classic", 24), ("karaoke", 24), ("neon_sweep", 20)])
def test_the_band_is_a_two_line_block_of_the_chosen_size(style, fontsize):
    layout = _layout(style, fontsize, font_name="Arial Black" if style in PRESETS else "Verdana")
    if style in ("classic", "karaoke"):
        # 2 lines of int(24 * 0.85) = 20 units plus the 2 unit outline above and below.
        assert layout.band_height == pytest.approx((2 * 20 + 2 * 2) / 288)
    else:
        # 2 lines of int(20 * 0.9) = 18 units plus the reach of the 22 px bloom.
        assert layout.band_height == pytest.approx((2 * 18 + 2 * 4) / 288)


def test_karaoke_stays_up_through_scenes_without_face_data(tmp_path):
    """Scenes without face data used to drop the captions to the bottom for
    a second and a half, onto a low face."""
    track = lambda start, end, face=(None, None): cp.LayoutSegment(
        start, end, cp.LAYOUT_TRACK, face_top=face[0], face_bottom=face[1])
    face_low = [   # the real clip's scenes with every face moved 40 % down
        cp.LayoutSegment(0.0, 3.84, cp.LAYOUT_SPLIT_STACKED),
        track(3.84, 15.84, (0.629, 0.907)), track(15.84, 17.36),
        track(17.36, 23.36, (0.604, 0.884)), track(23.36, 24.84), track(24.84, 26.36),
        track(26.36, 29.36, (0.618, 0.866)), track(29.36, 30.36), track(30.36, 31.84),
        track(31.84, 40.36, (0.62, 0.90)), track(40.36, 41.84, (0.589, 0.842)),
        track(41.84, 49.64, (0.617, 0.905)),
    ]
    words = [_w("Das", 1.0, 1.5), _w("hier", 6.0, 6.5), _w("ohne", 16.0, 16.5),
             _w("Daten", 25.0, 25.5), _w("weiter", 30.0, 30.5), _w("Ende", 45.0, 45.5)]
    transcript = {"segments": [{"start": 0, "end": 50, "text": "", "words": words}]}
    placement = cp.placement_for(PORTRAIT, "auto", timeline=face_low, safe_zone=cp.SafeZone())
    out = tmp_path / "karaoke.ass"
    assert generate_ass(transcript, 0, 50, str(out), placement=placement, fontsize=24, font_name="Verdana")
    assert [event[2] for event in _events(out)] == ["Middle"] + ["Top"] * 5
    hooked = tmp_path / "hooked.ass"
    assert generate_ass(transcript, 0, 50, str(hooked), placement=placement.with_hook(cp.SLOT_TOP),
                        fontsize=24, font_name="Verdana")
    assert [event[2] for event in _events(hooked)] == ["Middle"] + ["Bottom"] * 5


# --- Very long words and other scripts -----------------------------------

FONTS = ["Verdana", "Arial", "Arial Black", "Impact", "Helvetica", "Georgia", "Courier New"]


@pytest.mark.parametrize("font", FONTS)
@pytest.mark.parametrize("style,effect,uppercase", [
    ("classic", "none", False), ("karaoke", "pop", True), ("karaoke", "bounce", True),
    ("neon_sweep", "none", True), ("neon_tube", "none", True), ("rainbow_word", "none", True),
])
def test_a_long_word_at_the_largest_size_stays_on_two_lines(font, style, effect, uppercase):
    """At the dashboard maximum (40) a 21-letter word used to take up to 5 lines."""
    layout = _layout(style, 40, font_name=font, effect=effect, uppercase=uppercase)
    text = layout.display("Donaudampfschifffahrt")
    scale = layout.scale_for([text])
    assert subtitles.PORTRAIT_FLOOR_BLOCK_SCALE <= scale <= subtitles.PORTRAIT_MIN_BLOCK_SCALE
    pieces = layout.segments(text, scale)
    assert len(pieces) == 2 and "".join(pieces) == text
    assert all(layout.width(piece, scale) * layout.peak <= layout.usable for piece in pieces)
    assert layout.line_count([text], scale) <= 2


def test_below_the_floor_a_word_takes_more_lines_but_stays_inside():
    layout = _layout("classic", 40)
    text = "Rindfleischetikettierungsüberwachungsaufgabenübertragungsgesetz"
    scale = layout.scale_for([text])
    pieces = layout.segments(text, scale)
    assert scale == subtitles.PORTRAIT_FLOOR_BLOCK_SCALE and len(pieces) > 2
    assert all(layout.width(piece, scale) <= layout.usable for piece in pieces)


def test_a_word_is_never_cut_before_a_combining_mark():
    layout = _layout("classic", 40)
    text = "Donaudampfschéfffahrtsgesellscháft"
    for piece in layout.segments(text, layout.scale_for([text]))[1:]:
        assert unicodedata.category(piece[0]) != "Mn", piece


@pytest.mark.parametrize("word", ["الاصطناعيالاصطناعيالاصطناعي", "बुद्धिमत्ताबुद्धिमत्ताबुद्धिमत्ता"])
def test_joined_scripts_shrink_instead_of_splitting(word):
    layout = _layout("classic", 24)
    scale = layout.scale_for([word])
    assert scale < subtitles.PORTRAIT_MIN_BLOCK_SCALE
    assert layout.segments(word, scale) == [word]


RUSSIAN = ("Сегодня мы поговорим о программе которая помогает людям учиться быстрее "
           "и понимать сложные вещи гораздо лучше чем раньше потому что это действительно работает").split()


@pytest.mark.parametrize("style", ["classic", "karaoke"])
def test_russian_words_stay_whole_and_full_size(tmp_path, style):
    transcript = {"segments": [{"start": 0, "end": 60, "text": "", "words": [
        _w(word, 0.4 * index, 0.4 * index + 0.35) for index, word in enumerate(RUSSIAN)
    ]}]}
    out = tmp_path / f"{style}.ass"
    if style == "classic":
        assert generate_classic_ass(transcript, 0, 60, str(out), placement=_auto(), fontsize=24)
    else:
        assert generate_ass(transcript, 0, 60, str(out), placement=_auto(), fontsize=24)
    events = _events(out)
    assert not [event[3] for event in events if SPLIT_WORD.search(_visible(event[3]))]
    shrunk = {_visible(event[3]) for event in events if SCALE_TAG.search(event[3])}
    # Only "действительно" (13 letters) is wider than the line at Verdana 24.
    assert all("действительно" in text for text in shrunk)
