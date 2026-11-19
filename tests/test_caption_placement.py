"""Safe zone, layout timeline and slot choice for portrait captions."""
import json

import pytest

import caption_placement as cp


SPLIT_CENTERS = [(320, 300), (960, 300)]


# --- Safe zone -----------------------------------------------------------

def test_safe_zone_defaults(monkeypatch):
    for name in (cp.SAFE_BOTTOM_ENV, cp.SAFE_TOP_ENV, cp.SAFE_SIDE_ENV):
        monkeypatch.delenv(name, raising=False)
    zone = cp.safe_zone_from_env()
    assert (zone.bottom, zone.top, zone.side) == (0.25, 0.10, 0.14)


def test_safe_zone_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv(cp.SAFE_BOTTOM_ENV, "0.3")
    monkeypatch.setenv(cp.SAFE_TOP_ENV, " 0.05 ")
    monkeypatch.setenv(cp.SAFE_SIDE_ENV, "0.1")
    zone = cp.safe_zone_from_env()
    assert (zone.bottom, zone.top, zone.side) == (0.3, 0.05, 0.1)


@pytest.mark.parametrize("raw", ["", "abc", "nan", "inf", "-inf", "1e400"])
def test_unusable_safe_zone_values_fall_back_to_the_default(raw):
    zone = cp.safe_zone_from_env({cp.SAFE_BOTTOM_ENV: raw, cp.SAFE_SIDE_ENV: raw})
    assert zone.bottom == cp.DEFAULT_SAFE_BOTTOM
    assert zone.side == cp.DEFAULT_SAFE_SIDE


def test_out_of_range_safe_zone_values_are_clamped():
    zone = cp.safe_zone_from_env({
        cp.SAFE_BOTTOM_ENV: "0.9", cp.SAFE_TOP_ENV: "-1", cp.SAFE_SIDE_ENV: "25",
    })
    assert zone.bottom == cp.SAFE_BOTTOM_LIMITS[1]
    assert zone.top == 0.0
    assert zone.side == cp.SAFE_SIDE_LIMITS[1]
    # The middle slot can never fall into the protected bottom band.
    assert 1.0 - cp.SAFE_BOTTOM_LIMITS[1] > 0.5


@pytest.mark.parametrize("size,portrait", [
    ((1080, 1920), True), ((720, 1280), True), ((1080, 1080), False),
    ((1920, 1080), False), (None, False), ((0, 1920), False), (("x", 2), False),
])
def test_is_portrait(size, portrait):
    assert cp.is_portrait(size) is portrait


# --- Layout timeline -----------------------------------------------------

@pytest.mark.parametrize("strategy,centers,stacked,layout", [
    ("SPLIT", SPLIT_CENTERS, True, cp.LAYOUT_SPLIT_STACKED),
    ("SPLIT", SPLIT_CENTERS, False, cp.LAYOUT_SPLIT_SIDE),   # square: side by side
    ("SPLIT", None, True, cp.LAYOUT_GENERAL),                # drawn as GENERAL
    ("SPLIT", [], True, cp.LAYOUT_GENERAL),
    ("GENERAL", None, True, cp.LAYOUT_GENERAL),
    ("TRACK", None, True, cp.LAYOUT_TRACK),
    ("SOMETHING", None, True, cp.LAYOUT_TRACK),
])
def test_rendered_layout_mirrors_the_frame_loop(strategy, centers, stacked, layout):
    assert cp.rendered_layout(strategy, centers, stacked) == layout


def test_timeline_from_scene_decisions_records_what_was_drawn():
    timeline = cp.build_layout_timeline(
        [(0, 30), (30, 90), (90, 150)],
        ["SPLIT", "TRACK", "SPLIT"],
        [SPLIT_CENTERS, None, None],   # the last SPLIT has no centers
        stacked=True, fps=30, frames_rendered=150,
    )
    assert timeline == [
        cp.LayoutSegment(0.0, 1.0, cp.LAYOUT_SPLIT_STACKED),
        cp.LayoutSegment(1.0, 3.0, cp.LAYOUT_TRACK),
        cp.LayoutSegment(3.0, 5.0, cp.LAYOUT_GENERAL),
    ]


def test_timeline_assigns_frames_like_the_frame_loop():
    # Frames before the first scene start belong to it, frames after the last
    # scene end to the last one, and the encoder may stop early.
    timeline = cp.build_layout_timeline(
        [(5, 30), (30, 60)], ["TRACK", "GENERAL"], [None, None],
        stacked=True, fps=30, frames_rendered=75,
    )
    assert timeline == [
        cp.LayoutSegment(0.0, 1.0, cp.LAYOUT_TRACK),
        cp.LayoutSegment(1.0, 2.5, cp.LAYOUT_GENERAL),
    ]
    short = cp.build_layout_timeline(
        [(0, 30), (30, 60), (60, 90)], ["TRACK", "GENERAL", "TRACK"], [],
        stacked=True, fps=30, frames_rendered=40,
    )
    assert [(segment.start, segment.layout) for segment in short] == [
        (0.0, cp.LAYOUT_TRACK), (1.0, cp.LAYOUT_GENERAL),
    ]
    assert short[-1].end == pytest.approx(40 / 30)


def test_square_split_is_side_by_side_in_the_timeline():
    timeline = cp.build_layout_timeline(
        [(0, 60)], ["SPLIT"], [SPLIT_CENTERS], stacked=False, fps=30, frames_rendered=60,
    )
    assert [segment.layout for segment in timeline] == [cp.LAYOUT_SPLIT_SIDE]


@pytest.mark.parametrize("kwargs", [
    dict(fps=0, frames_rendered=30), dict(fps=float("nan"), frames_rendered=30),
    dict(fps=30, frames_rendered=0), dict(fps="x", frames_rendered=30),
])
def test_timeline_without_usable_frames_is_empty(kwargs):
    assert cp.build_layout_timeline([(0, 30)], ["TRACK"], [None], stacked=True, **kwargs) == []
    assert cp.build_layout_timeline([], [], [], stacked=True, fps=30, frames_rendered=30) == []


def test_sidecar_round_trip(tmp_path):
    clip = tmp_path / "show_clip_1.mp4"
    segments = [
        cp.LayoutSegment(0.0, 1.0, cp.LAYOUT_SPLIT_STACKED),
        cp.LayoutSegment(1.0, 2.5, cp.LAYOUT_TRACK),
    ]
    sidecar = cp.layout_sidecar_path(str(clip))
    assert sidecar == str(clip) + ".layout.json"
    with open(sidecar, "w", encoding="utf-8") as handle:
        json.dump(cp.layout_timeline_payload(segments, 29.97), handle)
    assert cp.load_layout_timeline(str(clip)) == segments


def test_missing_or_broken_sidecar_means_no_timeline(tmp_path):
    clip = tmp_path / "clip.mp4"
    assert cp.load_layout_timeline(str(clip)) is None
    sidecar = cp.layout_sidecar_path(str(clip))
    for content in ("{broken", "[]", '{"version": 0, "segments": []}',
                    '{"version": true, "segments": []}', '{"version": 1, "segments": {}}'):
        with open(sidecar, "w", encoding="utf-8") as handle:
            handle.write(content)
        assert cp.load_layout_timeline(str(clip)) is None, content


def test_sidecar_skips_bad_segments_and_sorts(tmp_path):
    payload = {"version": 2, "future_field": 1, "segments": [
        {"start": 3, "end": 5, "layout": "TRACK", "faces": [0.4]},
        {"start": "x", "end": 1, "layout": "TRACK"},
        {"start": 2, "end": 1, "layout": "TRACK"},
        {"start": 0, "end": "inf", "layout": "TRACK"},
        {"start": 0, "end": 3, "layout": 5},
        "nonsense",
        {"start": -1, "end": 3, "layout": cp.LAYOUT_SPLIT_STACKED},
    ]}
    assert cp.parse_layout_timeline(payload) == [
        cp.LayoutSegment(0.0, 3.0, cp.LAYOUT_SPLIT_STACKED),
        cp.LayoutSegment(3.0, 5.0, cp.LAYOUT_TRACK),
    ]


def test_oversized_sidecar_is_ignored(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    with open(cp.layout_sidecar_path(str(clip)), "w", encoding="utf-8") as handle:
        json.dump(cp.layout_timeline_payload([cp.LayoutSegment(0, 1, "TRACK")], 30), handle)
    monkeypatch.setattr(cp, "MAX_LAYOUT_SIDECAR_BYTES", 10)
    assert cp.load_layout_timeline(str(clip)) is None


# --- Slot choice ---------------------------------------------------------

TIMELINE = (
    cp.LayoutSegment(0.0, 3.0, cp.LAYOUT_SPLIT_STACKED),
    cp.LayoutSegment(3.0, 7.0, cp.LAYOUT_TRACK),
    cp.LayoutSegment(7.0, 10.0, cp.LAYOUT_SPLIT_STACKED),
    cp.LayoutSegment(10.0, 12.0, cp.LAYOUT_SPLIT_SIDE),
    cp.LayoutSegment(12.0, 14.0, cp.LAYOUT_GENERAL),
)


@pytest.mark.parametrize("start,end,slot", [
    (0.2, 2.3, cp.SLOT_MIDDLE),     # inside the stacked SPLIT
    (3.2, 5.0, cp.SLOT_BOTTOM),     # inside TRACK
    (6.8, 8.0, cp.SLOT_MIDDLE),     # straddles the cut, mostly SPLIT
    (2.5, 3.8, cp.SLOT_BOTTOM),     # straddles the cut, mostly TRACK
    (2.5, 3.5, cp.SLOT_MIDDLE),     # a tie goes to the slot that comes first
    (10.5, 11.5, cp.SLOT_BOTTOM),   # side-by-side SPLIT has no seam
    (12.5, 13.0, cp.SLOT_BOTTOM),
    (1.0, 1.0, cp.SLOT_MIDDLE),     # zero length still has a position
    (20.0, 21.0, cp.SLOT_BOTTOM),   # outside the timeline
])
def test_auto_slot_follows_the_layout_the_block_overlaps_longest(start, end, slot):
    assert cp.auto_slot(start, end, TIMELINE) == slot


def test_auto_slot_without_timeline_is_bottom():
    assert cp.auto_slot(0.2, 2.3, None) == cp.SLOT_BOTTOM
    assert cp.auto_slot(0.2, 2.3, ()) == cp.SLOT_BOTTOM
    assert cp.auto_slot("x", 2.3, TIMELINE) == cp.SLOT_BOTTOM


@pytest.mark.parametrize("position", ["top", "middle", "bottom"])
def test_fixed_positions_ignore_the_timeline(position):
    placement = cp.placement_for((1080, 1920), position, timeline=TIMELINE)
    assert placement.slot_for_block(0.2, 2.3) == position
    assert placement.slot_for_block(3.2, 5.0) == position


def test_placement_only_for_portrait_frames():
    for size in ((1920, 1080), (1080, 1080), None, (0, 0)):
        assert cp.placement_for(size, "auto", timeline=TIMELINE) is None
    placement = cp.placement_for((1080, 1920), "auto", timeline=list(TIMELINE))
    assert placement.frame_size == (1080, 1920)
    assert placement.timeline == TIMELINE
    assert placement.slot_for_block(0.2, 2.3) == cp.SLOT_MIDDLE


def test_old_clip_without_timeline_gets_the_bottom():
    placement = cp.placement_for((1080, 1920), "auto", timeline=None)
    assert placement.slot_for_block(0.2, 2.3) == cp.SLOT_BOTTOM


def test_placement_reads_the_safe_zone_from_the_environment(monkeypatch):
    monkeypatch.setenv(cp.SAFE_BOTTOM_ENV, "0.3")
    assert cp.placement_for((1080, 1920), "auto").safe_zone.bottom == 0.3


def test_unknown_position_is_bottom_like_the_legacy_burn():
    assert cp.placement_for((1080, 1920), "diagonal").position == cp.SLOT_BOTTOM
    assert cp.legacy_alignment("diagonal") == cp.SLOT_BOTTOM


def test_auto_means_bottom_outside_portrait():
    assert cp.legacy_alignment("auto") == "bottom"
    for position in ("top", "middle", "bottom"):
        assert cp.legacy_alignment(position) == position


# --- Geometry ------------------------------------------------------------

def test_slot_geometry_puts_the_text_on_the_safe_lines():
    geometry = cp.slot_geometry(cp.SafeZone(), 384, 288)
    assert geometry[cp.SLOT_BOTTOM] == cp.SlotGeometry(2, 54, 54, 72)   # 25 % of 288
    assert geometry[cp.SLOT_TOP] == cp.SlotGeometry(8, 54, 54, 29)      # 10 % of 288, rounded up
    assert geometry[cp.SLOT_MIDDLE].alignment == 5
    assert cp.usable_width(geometry, 384) == 384 - 2 * 54


def test_slot_geometry_keeps_the_outline_inside():
    geometry = cp.slot_geometry(cp.SafeZone(), 162, 288, ink=1.6)
    assert geometry[cp.SLOT_BOTTOM] == cp.SlotGeometry(2, 25, 25, 74)
    assert geometry[cp.SLOT_TOP].margin_v == 31


def test_slot_geometry_converts_blur_pixels_per_axis():
    # 4 px of blur on 1080x1920 are 4 * 384 / 1080 = 1.42 x-units and
    # 4 * 288 / 1920 = 0.6 y-units.
    geometry = cp.slot_geometry(cp.SafeZone(), 384, 288, ink=2, blur_px=4, frame_size=(1080, 1920))
    assert geometry[cp.SLOT_BOTTOM] == cp.SlotGeometry(2, 58, 58, 75)
    assert geometry[cp.SLOT_TOP].margin_v == 32
    # Without a frame size the blur cannot be converted and only the ink counts.
    assert cp.slot_geometry(cp.SafeZone(), 384, 288, ink=2, blur_px=4)[cp.SLOT_BOTTOM].margin_v == 74


def test_glyph_scale_matches_the_canvas():
    assert cp.glyph_scale_x((1080, 1920), 162, 288) == pytest.approx(1.0)
    assert cp.glyph_scale_x((1080, 1920), 384, 288) == pytest.approx(1920 / 288 / (1080 / 384))
    assert cp.glyph_scale_x(None, 384, 288) == 1.0


def test_dashboard_preview_uses_the_same_safe_zone_and_defaults_to_auto():
    """The subtitle preview copies the safe zone; keep both in step."""
    import pathlib
    import re

    source = (pathlib.Path(__file__).parents[1] / "dashboard" / "src" / "components"
              / "SubtitleModal.jsx").read_text(encoding="utf-8")
    zone = dict(re.findall(r"(bottom|top|side): ([0-9.]+)",
                           re.search(r"const SAFE_ZONE = \{([^}]*)\}", source).group(1)))
    assert {key: float(value) for key, value in zone.items()} == {
        "bottom": cp.DEFAULT_SAFE_BOTTOM, "top": cp.DEFAULT_SAFE_TOP, "side": cp.DEFAULT_SAFE_SIDE,
    }
    positions = re.findall(r"\{ id: '(\w+)', label: '\w+' \}",
                           re.search(r"const POSITIONS = \[(.*?)\];", source, re.S).group(1))
    assert positions == ["auto", "top", "middle", "bottom"]
    assert "useState(saved?.position ?? 'auto')" in source


# --- Face rule (top slot when the face sits low) -------------------------

# A two-line Classic block at size 24: 2 x 20 units plus a 2 unit outline on
# either side, on a 288 unit high canvas.
BAND = (2 * 20 + 2 * 2) / 288
BANDS = cp.text_bands(cp.SafeZone(), BAND)   # top 0.100-0.253, bottom 0.597-0.750


def _track(start, end, face=None):
    top, bottom = face if face else (None, None)
    return cp.LayoutSegment(start, end, cp.LAYOUT_TRACK, face_top=top, face_bottom=bottom)


def test_text_bands_follow_the_safe_zone_and_block_height():
    (top0, top1), (bottom0, bottom1) = BANDS
    assert (top0, bottom1) == (pytest.approx(0.10), pytest.approx(0.75))
    assert top1 - top0 == pytest.approx(BAND) and bottom1 - bottom0 == pytest.approx(BAND)


@pytest.mark.parametrize("face,wants_top", [
    ((0.62, 0.74), True),    # facecam low in the frame: the bottom captions cover it
    ((0.20, 0.45), False),   # face high: the bottom is free anyway
    ((0.50, 0.62), False),   # under a quarter of the face (19 %) in the bottom band
    ((0.50, 0.64), True),    # a third of the face in the band counts
    ((0.15, 0.70), False),   # a big face reaches into the top band too
    (None, False),           # no face data
])
def test_face_wants_top(face, wants_top):
    assert cp.face_wants_top(_track(0, 5, face), BANDS) is wants_top


def test_face_rule_needs_a_free_top_and_never_moves_the_seam():
    low = _track(0, 5, (0.62, 0.74))
    assert cp.slot_for_segment(low, BANDS) == cp.SLOT_TOP
    assert cp.slot_for_segment(low, BANDS, hook_slot=cp.SLOT_TOP) == cp.SLOT_BOTTOM
    split = cp.LayoutSegment(0, 5, cp.LAYOUT_SPLIT_STACKED, face_top=0.62, face_bottom=0.74)
    assert cp.slot_for_segment(split, BANDS) == cp.SLOT_MIDDLE
    general = cp.LayoutSegment(0, 5, cp.LAYOUT_GENERAL)
    assert cp.slot_for_segment(general, BANDS) == cp.SLOT_BOTTOM


def test_short_scenes_keep_the_slot_of_the_scene_before():
    timeline = (
        _track(0.0, 3.0, (0.62, 0.74)),     # low face: top
        _track(3.0, 3.8, (0.30, 0.45)),     # short, high face: stays at the top
        _track(3.8, 7.0, (0.30, 0.45)),     # long, high face: bottom
        _track(7.0, 7.6, (0.62, 0.74)),     # short, low face: stays at the bottom
        cp.LayoutSegment(7.6, 9.0, cp.LAYOUT_SPLIT_STACKED),   # the seam, even when short
        _track(9.0, 9.5, (0.62, 0.74)),     # short after the seam: its own rule
        _track(9.5, 12.0, (0.62, 0.74)),
        _track(12.0, 12.5, (0.15, 0.30)),   # short, but the top would touch its face
    )
    assert cp.segment_slots(timeline, BANDS) == (
        cp.SLOT_TOP, cp.SLOT_TOP, cp.SLOT_BOTTOM, cp.SLOT_BOTTOM, cp.SLOT_MIDDLE,
        cp.SLOT_TOP, cp.SLOT_TOP, cp.SLOT_BOTTOM,
    )
    # Without a free top nothing goes up; the seam stays.
    assert cp.segment_slots(timeline, BANDS, cp.SLOT_TOP) == (
        cp.SLOT_BOTTOM,) * 4 + (cp.SLOT_MIDDLE,) + (cp.SLOT_BOTTOM,) * 3


def test_placement_uses_the_face_rule_only_for_auto():
    timeline = [_track(0.0, 5.0, (0.62, 0.74)), _track(5.0, 10.0, (0.25, 0.45))]
    auto = cp.placement_for((1080, 1920), "auto", timeline=timeline, safe_zone=cp.SafeZone())
    assert auto.slot_for_block(1.0, 2.5, BAND) == cp.SLOT_TOP
    assert auto.slot_for_block(6.0, 7.5, BAND) == cp.SLOT_BOTTOM
    assert auto.slot_for_block(1.0, 2.5) == cp.SLOT_BOTTOM            # no block height: no rule
    assert auto.with_hook(cp.SLOT_TOP).slot_for_block(1.0, 2.5, BAND) == cp.SLOT_BOTTOM
    assert auto.with_hook(cp.SLOT_MIDDLE).slot_for_block(1.0, 2.5, BAND) == cp.SLOT_TOP
    assert auto.hook_slot is None and auto.with_hook(cp.SLOT_TOP).hook_slot == cp.SLOT_TOP
    fixed = cp.placement_for((1080, 1920), "middle", timeline=timeline)
    assert fixed.slot_for_block(1.0, 2.5, BAND) == cp.SLOT_MIDDLE


# --- Face samples from the frame loop ------------------------------------

def test_face_band_in_crop_uses_the_drawn_window():
    # A 608 px high window starting at y=200 is scaled to the full output.
    assert cp.face_band_in_crop((100, 352, 50, 152), (60, 200, 402, 808)) == (
        pytest.approx(0.25), pytest.approx(0.5))
    assert cp.face_band_in_crop((0, 0, 50, 60), (60, 200, 402, 808)) is None      # off screen
    assert cp.face_band_in_crop((100, 100, 0, 60), (0, 0, 200, 400)) is None       # empty box
    assert cp.face_band_in_crop(None, (0, 0, 200, 400)) is None
    assert cp.face_band_in_crop((100, 360, 40, 60), (0, 0, 200, 400)) == (0.9, 1.0)   # clipped


def test_face_band_is_the_median_with_a_minimum_of_samples():
    samples = [(0.60, 0.72), (0.62, 0.74), (0.10, 0.20), (0.61, 0.73)]
    assert cp.face_band(samples) == (pytest.approx(0.605), pytest.approx(0.725))
    assert cp.face_band(samples[:2]) is None
    assert cp.face_band([]) is None


def test_timeline_records_faces_for_track_scenes_only():
    samples = {0: [(0.6, 0.7)] * 3, 1: [(0.6, 0.7)] * 3, 2: [(0.6, 0.7)] * 2}
    timeline = cp.build_layout_timeline(
        [(0, 30), (30, 60), (60, 90)], ["TRACK", "SPLIT", "TRACK"],
        [None, SPLIT_CENTERS, None], stacked=True, fps=30, frames_rendered=90,
        face_samples=samples,
    )
    assert (timeline[0].face_top, timeline[0].face_bottom) == (0.6, 0.7)
    assert not timeline[1].has_face           # the seam scene keeps no face
    assert not timeline[2].has_face           # too few detections


def test_sidecar_round_trip_keeps_faces_and_reads_old_files(tmp_path):
    clip = tmp_path / "clip.mp4"
    segments = [_track(0.0, 2.0, (0.6123, 0.7456)), _track(2.0, 4.0)]
    payload = cp.layout_timeline_payload(segments, 30)
    assert "face_top" not in payload["segments"][1]          # unchanged format without faces
    with open(cp.layout_sidecar_path(str(clip)), "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    loaded = cp.load_layout_timeline(str(clip))
    assert (loaded[0].face_top, loaded[0].face_bottom) == (0.612, 0.746)
    assert not loaded[1].has_face
    bad = {"version": 1, "segments": [
        {"start": 0, "end": 1, "layout": "TRACK", "face_top": 0.8, "face_bottom": 0.2},
        {"start": 1, "end": 2, "layout": "TRACK", "face_top": "x", "face_bottom": 0.5},
        {"start": 2, "end": 3, "layout": "TRACK", "face_top": -0.1, "face_bottom": 0.5},
    ]}
    assert not any(segment.has_face for segment in cp.parse_layout_timeline(bad))


# The real clip's scenes with every face moved 40 % down: five TRACK scenes
# have no face data (short, or no stable tracker target).
FACE_LOW = (
    cp.LayoutSegment(0.0, 3.84, cp.LAYOUT_SPLIT_STACKED),
    _track(3.84, 15.84, (0.629, 0.907)),
    _track(15.84, 17.36),
    _track(17.36, 23.36, (0.604, 0.884)),
    _track(23.36, 24.84),
    _track(24.84, 26.36),
    _track(26.36, 29.36, (0.618, 0.866)),
    _track(29.36, 30.36),
    _track(30.36, 31.84),
    _track(31.84, 40.36, (0.62, 0.90)),
    _track(40.36, 41.84, (0.589, 0.842)),
    _track(41.84, 49.64, (0.617, 0.905)),
)


def test_scenes_without_face_data_do_not_move_the_captions():
    assert cp.segment_slots(FACE_LOW, BANDS) == (cp.SLOT_MIDDLE,) + (cp.SLOT_TOP,) * 11
    placement = cp.placement_for((1080, 1920), "auto", timeline=FACE_LOW, safe_zone=cp.SafeZone())
    blocks = [(0.06, 4.08), (4.08, 15.76), (15.94, 17.38), (17.38, 24.58), (24.80, 26.44), (26.44, 49.18)]
    assert [placement.slot_for_block(start, end, BAND) for start, end in blocks] == [
        cp.SLOT_MIDDLE] + [cp.SLOT_TOP] * 5
    # A hook at the top: nothing goes up, not even inherited.
    assert cp.segment_slots(FACE_LOW, BANDS, cp.SLOT_TOP) == (cp.SLOT_MIDDLE,) + (cp.SLOT_BOTTOM,) * 11


def test_scenes_without_evidence_take_the_nearest_face_decision():
    low = (0.62, 0.74)
    # Before the first face scene: that scene's slot. Across a SPLIT: the last one.
    timeline = (
        cp.LayoutSegment(0.0, 2.0, cp.LAYOUT_GENERAL),
        _track(2.0, 4.0),
        cp.LayoutSegment(4.0, 6.0, cp.LAYOUT_SPLIT_STACKED),
        _track(6.0, 10.0, low),
        cp.LayoutSegment(10.0, 12.0, cp.LAYOUT_SPLIT_STACKED),
        cp.LayoutSegment(12.0, 14.0, "SOMETHING_NEW"),
        _track(14.0, 18.0, (0.25, 0.45)),
        cp.LayoutSegment(18.0, 20.0, cp.LAYOUT_GENERAL),
    )
    assert cp.segment_slots(timeline, BANDS) == (
        cp.SLOT_TOP, cp.SLOT_TOP, cp.SLOT_MIDDLE, cp.SLOT_TOP, cp.SLOT_MIDDLE, cp.SLOT_TOP,
        cp.SLOT_BOTTOM, cp.SLOT_BOTTOM,
    )
    # A face on a GENERAL scene is no evidence (the renderer only records TRACK).
    general = cp.LayoutSegment(0.0, 5.0, cp.LAYOUT_GENERAL, face_top=0.62, face_bottom=0.74)
    assert cp.segment_slots((general,), BANDS) == (cp.SLOT_BOTTOM,)


def test_clips_without_face_data_keep_the_stage_one_slots():
    timeline = (
        cp.LayoutSegment(0.0, 3.0, cp.LAYOUT_SPLIT_STACKED),
        _track(3.0, 4.0),
        cp.LayoutSegment(4.0, 9.0, cp.LAYOUT_GENERAL),
        cp.LayoutSegment(9.0, 10.0, cp.LAYOUT_SPLIT_SIDE),
    )
    assert cp.segment_slots(timeline, BANDS) == (
        cp.SLOT_MIDDLE, cp.SLOT_BOTTOM, cp.SLOT_BOTTOM, cp.SLOT_BOTTOM,
    )


@pytest.mark.parametrize("content", [
    '{"version": 1, "segments": [{"start": 1' + "0" * 400 + ', "end": 2, "layout": "TRACK"}]}',
    '{"version": 1, "segments": [{"start": 0, "end": 2, "layout": "TRACK", "face_top": 1'
    + "0" * 400 + ', "face_bottom": 0.5}]}',
    "[" * 100000 + "]" * 100000,
], ids=["huge-start", "huge-face", "deep-nesting"])
def test_any_loading_error_means_no_timeline(tmp_path, content):
    clip = tmp_path / "clip.mp4"
    with open(cp.layout_sidecar_path(str(clip)), "w", encoding="utf-8") as handle:
        handle.write(content)
    timeline = cp.load_layout_timeline(str(clip))   # must not raise
    assert timeline is None or all(not segment.has_face for segment in timeline)


def test_a_sidecar_with_more_segments_than_any_clip_counts_as_none(tmp_path):
    clip = tmp_path / "clip.mp4"
    segments = [_track(float(i), float(i + 1)) for i in range(cp.MAX_LAYOUT_SEGMENTS + 1)]
    with open(cp.layout_sidecar_path(str(clip)), "w", encoding="utf-8") as handle:
        json.dump(cp.layout_timeline_payload(segments, 30), handle)
    assert cp.load_layout_timeline(str(clip)) is None
    assert cp.MAX_LAYOUT_SEGMENTS <= 2000


def test_a_center_hook_sends_the_seam_scenes_down():
    timeline = (cp.LayoutSegment(0.0, 3.0, cp.LAYOUT_SPLIT_STACKED), _track(3.0, 8.0, (0.62, 0.74)))
    assert cp.segment_slots(timeline, BANDS, cp.SLOT_MIDDLE) == (cp.SLOT_BOTTOM, cp.SLOT_TOP)
    assert cp.segment_slots(timeline, BANDS, cp.SLOT_TOP) == (cp.SLOT_MIDDLE, cp.SLOT_BOTTOM)
    assert cp.segment_slots(timeline, BANDS) == (cp.SLOT_MIDDLE, cp.SLOT_TOP)
