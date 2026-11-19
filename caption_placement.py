"""Where burned-in captions sit on a portrait (9:16) Short.

YouTube draws the channel name, the title and the sound line over the lower
quarter of a Short, the action buttons down the right edge and the search bar
across the top. Captions at the old fixed bottom margin sat right under that
overlay. On a stacked two-person shot (SPLIT) the lower face sits at about 71 %
of the height, so captions that only move up out of the overlay land on that
face; there the seam between the two halves is the free spot.

This module is the pure part of that decision. It has no FFmpeg and no video
I/O, so every rule here is unit-testable:

* ``SafeZone``: the frame margins captions keep clear of, read from the
  environment at call time.
* The layout timeline: which layout the renderer actually drew, per scene, in
  clip seconds. main.py records it next to the rendered clip, named after
  the clip file plus ``.layout.json`` (``show_clip_1.mp4.layout.json``); the
  subtitle step reads it back by the clip's original file name from the
  metadata, which survives restarts and derived files (edited_, translated_,
  subtitled_, hook_).
* Slot choice: ``auto`` puts a caption block on the seam (middle) when most of
  it plays over a stacked SPLIT, at the top when the tracked speaker's face
  sits low in the frame and the top is free, else at the bottom.
  ``top``/``middle``/``bottom`` pin one slot for the whole clip. A block never
  changes slot while it is on screen.
* Slot geometry: ASS alignment and margins for each slot on a script canvas.

Only portrait frames get any of this. Everything else keeps today's captions.
"""
import json
import math
import os
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Optional


# --- Safe zone -----------------------------------------------------------

# Shares of the frame captions keep clear of. Google's own vertical-ads
# template (1080x1920) reserves 672 px at the bottom and 288 px at the top;
# third-party measurements of the plain Shorts player put its overlay at
# 250-380 px from the bottom, 120-180 px from the top and 120-164 px from the
# right. 25 % (480 px) / 10 % (192 px) / 14 % (151 px) clears all of those
# with room to spare. The side margin is symmetric so captions stay centred.
DEFAULT_SAFE_BOTTOM = 0.25
DEFAULT_SAFE_TOP = 0.10
DEFAULT_SAFE_SIDE = 0.14

SAFE_BOTTOM_ENV = "SHORTS_CAPTION_SAFE_BOTTOM"
SAFE_TOP_ENV = "SHORTS_CAPTION_SAFE_TOP"
SAFE_SIDE_ENV = "SHORTS_CAPTION_SAFE_SIDE"

# Bounds against nonsense values. Together they still leave at least 30 % of
# the height and 40 % of the width for text, and the middle slot (50 %) can
# never fall inside the bottom zone.
SAFE_BOTTOM_LIMITS = (0.0, 0.40)
SAFE_TOP_LIMITS = (0.0, 0.30)
SAFE_SIDE_LIMITS = (0.0, 0.30)


@dataclass(frozen=True)
class SafeZone:
    """Margins as shares of the frame height (top/bottom) and width (side)."""

    top: float = DEFAULT_SAFE_TOP
    bottom: float = DEFAULT_SAFE_BOTTOM
    side: float = DEFAULT_SAFE_SIDE


def _share_from_env(name, default, limits, environ):
    raw = str(environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value):
        return default
    low, high = limits
    return min(high, max(low, value))


def safe_zone_from_env(environ=None):
    """The configured safe zone. Read at call time: main.py and app.py load
    .env only after importing this module. Unparsable values fall back to the
    default, out-of-range values are clamped to the limits."""
    environ = os.environ if environ is None else environ
    return SafeZone(
        top=_share_from_env(SAFE_TOP_ENV, DEFAULT_SAFE_TOP, SAFE_TOP_LIMITS, environ),
        bottom=_share_from_env(SAFE_BOTTOM_ENV, DEFAULT_SAFE_BOTTOM, SAFE_BOTTOM_LIMITS, environ),
        side=_share_from_env(SAFE_SIDE_ENV, DEFAULT_SAFE_SIDE, SAFE_SIDE_LIMITS, environ),
    )


def valid_frame_size(frame_size):
    try:
        width, height = (int(value) for value in frame_size)
    except (TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def is_portrait(frame_size):
    """True for a frame taller than wide. Square and landscape are not."""
    size = valid_frame_size(frame_size)
    return bool(size) and size[1] > size[0]


# --- Layout timeline -----------------------------------------------------

# What the frame loop in main.process_video_to_vertical actually drew.
LAYOUT_TRACK = "TRACK"
LAYOUT_GENERAL = "GENERAL"
LAYOUT_SPLIT_STACKED = "SPLIT_STACKED"  # two people one above the other (9:16)
LAYOUT_SPLIT_SIDE = "SPLIT_SIDE"        # two people side by side (1:1)

LAYOUT_TIMELINE_VERSION = 1
LAYOUT_SIDECAR_SUFFIX = ".layout.json"
# The sidecar is our own small file, but it is still read back from disk.
# 2000 scenes would take more than an hour of two-second shots; a file with
# more counts as no timeline, so it cannot stall the subtitle request.
MAX_LAYOUT_SIDECAR_BYTES = 2 * 1024 * 1024
MAX_LAYOUT_SEGMENTS = 2000


@dataclass(frozen=True)
class LayoutSegment:
    """One scene of the rendered clip, in clip seconds.

    ``face_top``/``face_bottom``: where the tracked speaker's face sits in the
    rendered frame, as shares of its height (TRACK scenes with enough face
    detections only). Optional, so older sidecars keep loading.
    """

    start: float
    end: float
    layout: str
    face_top: Optional[float] = None
    face_bottom: Optional[float] = None

    @property
    def has_face(self):
        return self.face_top is not None and self.face_bottom is not None

    def to_dict(self):
        data = {"start": round(self.start, 3), "end": round(self.end, 3), "layout": self.layout}
        if self.has_face:
            data["face_top"] = round(self.face_top, 3)
            data["face_bottom"] = round(self.face_bottom, 3)
        return data


# A scene needs this many face detections before its face position counts;
# the tracker detects every few frames, so a one-second scene has several.
MIN_FACE_SAMPLES = 3


def face_band_in_crop(face_box, crop_box):
    """Vertical position of a face in the rendered frame, as shares of its
    height, or None.

    ``face_box`` is the tracker's (x, y, w, h) in source pixels, ``crop_box``
    the (x1, y1, x2, y2) source window the frame loop scales to the output.
    A face whose centre lies outside the window is not on screen.
    """
    try:
        x, y, width, height = (float(value) for value in face_box)
        x1, y1, x2, y2 = (float(value) for value in crop_box)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (x, y, width, height, x1, y1, x2, y2)):
        return None
    if width <= 0 or height <= 0 or x2 <= x1 or y2 <= y1:
        return None
    if not (x1 <= x + width / 2 <= x2 and y1 <= y + height / 2 <= y2):
        return None
    span = y2 - y1
    top = min(1.0, max(0.0, (y - y1) / span))
    bottom = min(1.0, max(0.0, (y + height - y1) / span))
    return (top, bottom) if bottom > top else None


def face_band(samples, min_samples=MIN_FACE_SAMPLES):
    """One robust face position for a scene: the median top and bottom of its
    detections, or None with fewer than ``min_samples``."""
    samples = [sample for sample in (samples or []) if sample]
    if len(samples) < max(1, min_samples):
        return None
    tops = sorted(sample[0] for sample in samples)
    bottoms = sorted(sample[1] for sample in samples)

    def median(values):
        middle = len(values) // 2
        return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2

    top, bottom = median(tops), median(bottoms)
    return (top, bottom) if bottom > top else None


def rendered_layout(strategy, split_centers, stacked):
    """The layout the frame loop draws for one scene.

    Mirrors the branch in main.process_video_to_vertical: SPLIT needs measured
    centers, otherwise that scene is drawn as GENERAL. Anything that is
    neither SPLIT nor GENERAL is tracked.
    """
    if strategy == "SPLIT" and split_centers:
        return LAYOUT_SPLIT_STACKED if stacked else LAYOUT_SPLIT_SIDE
    if strategy in ("GENERAL", "SPLIT"):
        return LAYOUT_GENERAL
    return LAYOUT_TRACK


def build_layout_timeline(scene_frames, strategies, split_centers, *, stacked, fps,
                          frames_rendered, face_samples=None):
    """Per-scene layout segments in clip seconds.

    ``scene_frames`` are the (start_frame, end_frame) pairs the frame loop
    walks. Like the loop, frame n belongs to the first scene whose end frame
    lies after n; the last scene also keeps every later frame. Frame n of the
    rendered clip starts at n / fps: the reframe pipe writes frames at that
    rate from zero, and the clip cut seeks the source to the clip start, so
    second 0 here is second 0 of the clip's subtitle timeline too.
    ``face_samples`` maps a scene index to the face positions the loop saw
    (face_band_in_crop); TRACK scenes with enough of them record the median.
    """
    face_samples = face_samples or {}
    try:
        fps = float(fps)
        frames_rendered = int(frames_rendered)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(fps) or fps <= 0 or frames_rendered <= 0 or not scene_frames:
        return []

    strategies = list(strategies or [])
    split_centers = list(split_centers or [])
    segments = []
    start = 0
    last_index = len(scene_frames) - 1
    for index, (_scene_start, scene_end) in enumerate(scene_frames):
        if index == last_index:
            end = frames_rendered
        else:
            try:
                end = int(scene_end)
            except (TypeError, ValueError):
                continue
            end = min(max(end, start), frames_rendered)
        if end <= start:
            continue
        strategy = strategies[index] if index < len(strategies) else "TRACK"
        centers = (
            split_centers[index]
            if strategy == "SPLIT" and index < len(split_centers)
            else None
        )
        layout = rendered_layout(strategy, centers, stacked)
        face = face_band(face_samples.get(index)) if layout == LAYOUT_TRACK else None
        segments.append(LayoutSegment(
            start / fps, end / fps, layout,
            face_top=face[0] if face else None, face_bottom=face[1] if face else None,
        ))
        start = end
        if start >= frames_rendered:
            break
    return segments


def layout_timeline_payload(segments, fps):
    return {
        "version": LAYOUT_TIMELINE_VERSION,
        "fps": round(float(fps), 6),
        "segments": [segment.to_dict() for segment in segments],
    }


def layout_sidecar_path(video_path):
    """Where the layout timeline of a rendered clip lives."""
    return f"{video_path}{LAYOUT_SIDECAR_SUFFIX}"


def parse_layout_timeline(payload):
    """Segments from a sidecar payload, sorted by start; None when unusable.

    Unknown keys and unknown layouts are kept harmless: an unknown layout is
    simply not a stacked SPLIT.
    """
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return None
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or len(raw_segments) > MAX_LAYOUT_SEGMENTS:
        return None
    segments = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw.get("start"))
            end = float(raw.get("end"))
        except (TypeError, ValueError, OverflowError):
            continue
        layout = raw.get("layout")
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
            continue
        if not isinstance(layout, str):
            continue
        segments.append(LayoutSegment(max(0.0, start), end, layout, *_parse_face(raw)))
    segments.sort(key=lambda segment: (segment.start, segment.end))
    return segments


def _parse_face(raw):
    """(face_top, face_bottom) from a sidecar segment, or (None, None)."""
    try:
        top = float(raw["face_top"])
        bottom = float(raw["face_bottom"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, None
    if not (math.isfinite(top) and math.isfinite(bottom)) or not 0.0 <= top < bottom <= 1.0:
        return None, None
    return top, bottom


def load_layout_timeline(video_path):
    """The recorded timeline of a rendered clip, or None (older clip, write
    failure, unreadable file). Any error while loading means no timeline, so
    the sidecar can never fail a subtitle request."""
    path = layout_sidecar_path(video_path)
    try:
        if os.path.getsize(path) > MAX_LAYOUT_SIDECAR_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return parse_layout_timeline(payload)
    except Exception:
        return None


# --- Slots ---------------------------------------------------------------

SLOT_BOTTOM = "bottom"
SLOT_MIDDLE = "middle"
SLOT_TOP = "top"
FIXED_SLOTS = (SLOT_TOP, SLOT_MIDDLE, SLOT_BOTTOM)
POSITION_AUTO = "auto"
POSITIONS = (POSITION_AUTO,) + FIXED_SLOTS

# Numpad alignment (ASS v4.00+), the same codes the legacy burn paths use.
SLOT_ALIGNMENT = {SLOT_BOTTOM: 2, SLOT_MIDDLE: 5, SLOT_TOP: 8}
# Style names in the generated ASS file, one style per slot.
SLOT_STYLE_NAMES = {SLOT_BOTTOM: "Bottom", SLOT_MIDDLE: "Middle", SLOT_TOP: "Top"}


# The top slot replaces the bottom one only when this share of the face
# height (or more) lies in the bottom text band.
FACE_IN_BAND_SHARE = 0.25
# A face scene shorter than this keeps the top/bottom slot of the scene
# before it, so captions do not flip between top and bottom with every
# quick cut.
MIN_SLOT_SCENE_SECONDS = 1.5


def text_bands(safe_zone, band_height):
    """The (top, bottom) shares of the frame a caption block covers in the
    top and in the bottom slot. ``band_height`` is the height of a two-line
    block as a share of the frame height."""
    band_height = max(0.0, float(band_height or 0.0))
    top_line, bottom_line = safe_zone.top, 1.0 - safe_zone.bottom
    return (top_line, top_line + band_height), (bottom_line - band_height, bottom_line)


def _overlap(low, high, band):
    return max(0.0, min(high, band[1]) - max(low, band[0]))


def face_wants_top(segment, bands):
    """True when the bottom captions would cover this scene's face and the top
    captions would not touch it."""
    if not segment.has_face:
        return False
    top_band, bottom_band = bands
    height = segment.face_bottom - segment.face_top
    if height <= 0 or bottom_band[1] <= bottom_band[0]:
        return False
    clearly_low = _overlap(segment.face_top, segment.face_bottom, bottom_band) >= FACE_IN_BAND_SHARE * height
    return clearly_low and segment.face_top >= top_band[1]


def _face_touches_top(segment, bands):
    return segment.has_face and segment.face_top < bands[0][1]


def _has_face_evidence(segment):
    """Only a tracked scene with measured face positions says where the
    captions must go; any other scene says nothing about it."""
    return segment.layout == LAYOUT_TRACK and segment.has_face


def slot_for_segment(segment, bands=None, hook_slot=None):
    """The ``auto`` rule for one scene on its own: the seam on a stacked SPLIT
    (side-by-side SPLIT has no horizontal seam), the top when the tracked
    face sits low, else the bottom. ``hook_slot`` is the slot a hook covers;
    captions never go there."""
    if segment.layout == LAYOUT_SPLIT_STACKED:
        return SLOT_MIDDLE if hook_slot != SLOT_MIDDLE else SLOT_BOTTOM
    if (hook_slot != SLOT_TOP and bands is not None and _has_face_evidence(segment)
            and face_wants_top(segment, bands)):
        return SLOT_TOP
    return SLOT_BOTTOM


@lru_cache(maxsize=64)
def segment_slots(timeline, bands, hook_slot=None):
    """The ``auto`` slot of every scene, in timeline order.

    Only evidence moves the captions. A stacked SPLIT takes the seam. A
    TRACK scene with face data decides by its face; when it is shorter than
    MIN_SLOT_SCENE_SECONDS it keeps the top or bottom slot of the scene right
    before it, so a quick cut cannot flip the captions up and back down, but
    never a top that would touch its own face. Every other scene (no face
    data, GENERAL, unknown layout) keeps the top or bottom slot of the last
    scene with face data, also across a SPLIT; before the first one it takes
    that first one's slot, and a clip without any face data stays at the
    bottom. No scene gets the slot ``hook_slot`` covers, inherited or not:
    with a hook at the top nothing goes up, with one in the middle the
    stacked SPLIT scenes go to the bottom.
    """
    slots = []
    carried = None   # top/bottom of the last scene with face data
    for segment in timeline:
        if segment.layout == LAYOUT_SPLIT_STACKED:
            slots.append(slot_for_segment(segment, bands, hook_slot))
            continue
        if not _has_face_evidence(segment):
            slots.append(carried)   # None until the first face scene
            continue
        slot = slot_for_segment(segment, bands, hook_slot)
        previous = slots[-1] if slots else None
        if segment.end - segment.start < MIN_SLOT_SCENE_SECONDS and previous in (SLOT_TOP, SLOT_BOTTOM):
            slot = previous
            if slot == SLOT_TOP and _face_touches_top(segment, bands):
                slot = SLOT_BOTTOM
        slots.append(slot)
        carried = slot
    first = next((slot for slot, segment in zip(slots, timeline) if _has_face_evidence(segment)),
                 SLOT_BOTTOM)
    return tuple(first if slot is None else slot for slot in slots)


def auto_slot(start, end, timeline, slots=None):
    """One slot for a whole caption block.

    The block takes the slot of the scenes it overlaps longest, so it stays in
    one place even when a cut falls inside it. Ties go to the slot that comes
    first in time. Without a timeline, or outside it, the block goes to the
    bottom. ``slots`` are the per-scene slots (segment_slots); without them
    every scene uses slot_for_segment alone.
    """
    if not timeline:
        return SLOT_BOTTOM
    try:
        start = float(start)
        end = float(end)
    except (TypeError, ValueError):
        return SLOT_BOTTOM
    if not (math.isfinite(start) and math.isfinite(end)):
        return SLOT_BOTTOM
    end = max(end, start + 0.001)  # a zero-length block still has a position
    weights = {}
    for index, segment in enumerate(timeline):
        overlap = min(end, segment.end) - max(start, segment.start)
        if overlap > 0:
            slot = slots[index] if slots is not None else slot_for_segment(segment)
            weights[slot] = weights.get(slot, 0.0) + overlap
    if not weights:
        return SLOT_BOTTOM
    # max() keeps the first of equal weights, and insertion follows time.
    return max(weights, key=weights.get)


def normalize_position(position):
    value = str(position or "").strip().lower()
    if value in POSITIONS:
        return value
    # The legacy burn paths treat an unknown alignment as bottom.
    return SLOT_BOTTOM


# Slots a hook can cover (hooks.prepare_hook_overlay: top at 20 % of the
# height, center in the middle). A hook at the bottom is not handled here.
HOOK_SLOTS = (SLOT_TOP, SLOT_MIDDLE)


@dataclass(frozen=True)
class CaptionPlacement:
    """Placement for captions on one portrait frame.

    ``hook_slot``: the slot a hook covers (SLOT_TOP, SLOT_MIDDLE or None);
    ``auto`` never puts captions there (see with_hook).
    """

    frame_size: tuple
    position: str = POSITION_AUTO
    safe_zone: SafeZone = SafeZone()
    timeline: tuple = ()
    hook_slot: Optional[str] = None

    def slot_for_block(self, start, end, band_height=0.0):
        """The slot of a block. ``band_height``: height of a two-line block of
        this style as a share of the frame height, for the face rule."""
        if self.position in FIXED_SLOTS:
            return self.position
        timeline = tuple(self.timeline)
        bands = text_bands(self.safe_zone, band_height)
        return auto_slot(start, end, timeline, segment_slots(timeline, bands, self.hook_slot))

    def with_hook(self, slot):
        """The same placement with ``slot`` covered by a hook."""
        return replace(self, hook_slot=slot)


def placement_for(frame_size, position, timeline=None, safe_zone=None):
    """Placement for captions burned into a frame of ``frame_size``.

    None outside portrait: square, landscape and unknown sizes keep the
    legacy captions, where ``auto`` means bottom.
    """
    size = valid_frame_size(frame_size)
    if not is_portrait(size):
        return None
    return CaptionPlacement(
        frame_size=size,
        position=normalize_position(position),
        safe_zone=safe_zone if safe_zone is not None else safe_zone_from_env(),
        timeline=tuple(timeline or ()),
    )


def legacy_alignment(position):
    """The alignment the non-portrait paths get: ``auto`` there is bottom."""
    value = normalize_position(position)
    return SLOT_BOTTOM if value == POSITION_AUTO else value


# --- Geometry ------------------------------------------------------------

@dataclass(frozen=True)
class SlotGeometry:
    """ASS alignment and margins of one slot, in script units."""

    alignment: int
    margin_l: int
    margin_r: int
    margin_v: int


def _ceil_units(value):
    # 0.10 * 288 is 28.800000000000004 in floating point; only a real
    # fraction may round up.
    return max(0, math.ceil(value - 1e-9))


def slot_geometry(safe_zone, play_res_x, play_res_y, ink=0.0, blur_px=0.0, frame_size=None):
    """Alignment and margins per slot on a ``play_res_x`` x ``play_res_y``
    script canvas.

    Bottom: the text block ends on the lower safe line. Top: it starts on the
    upper safe line. Middle: libass centres the block on half the script
    height and ignores MarginV. The side margins are also the width libass
    wraps in.

    The margins also hold what is drawn past the glyphs, so the visible text
    stays inside the lines and not only its glyph box: ``ink`` is the widest
    outline or box in script units (libass scales \\bord per axis with
    ScaledBorderAndShadow), ``blur_px`` the widest \\blur in output pixels
    (libass does not scale it), converted per axis with ``frame_size``.
    Lines are measured with the real glyph widths, so a full line reaches the
    side margins and its glow needs room there too.
    """
    reach_x = reach_y = max(0.0, float(ink or 0.0))
    size = valid_frame_size(frame_size)
    if blur_px and size:
        reach_x += max(0.0, float(blur_px)) * play_res_x / size[0]
        reach_y += max(0.0, float(blur_px)) * play_res_y / size[1]
    side = _ceil_units(safe_zone.side * play_res_x + reach_x)
    bottom = _ceil_units(safe_zone.bottom * play_res_y + reach_y)
    top = _ceil_units(safe_zone.top * play_res_y + reach_y)
    return {
        SLOT_BOTTOM: SlotGeometry(SLOT_ALIGNMENT[SLOT_BOTTOM], side, side, bottom),
        SLOT_MIDDLE: SlotGeometry(SLOT_ALIGNMENT[SLOT_MIDDLE], side, side, 0),
        SLOT_TOP: SlotGeometry(SLOT_ALIGNMENT[SLOT_TOP], side, side, top),
    }


def usable_width(geometry, play_res_x):
    """Line width libass wraps in, in script units (all slots share it)."""
    slot = geometry[SLOT_BOTTOM]
    return max(1, play_res_x - slot.margin_l - slot.margin_r)


def glyph_scale_x(frame_size, play_res_x, play_res_y):
    """Script x-units one script y-unit of glyph width takes on this frame.

    libass sizes glyphs by the frame height and keeps their aspect, but
    measures margins and line width in x-units of the script canvas.
    """
    size = valid_frame_size(frame_size)
    if not size:
        return 1.0
    width, height = size
    return (height / float(play_res_y)) / (width / float(play_res_x))
