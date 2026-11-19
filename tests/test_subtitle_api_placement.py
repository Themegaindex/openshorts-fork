"""/api/subtitle: position auto, the safe zone and the stored layout timeline."""
import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="FastAPI contract tests run in the server environment")

from pydantic import ValidationError  # noqa: E402

import app  # noqa: E402
import caption_placement as cp  # noqa: E402


CLIP = "show_clip_1.mp4"
TIMELINE = [
    cp.LayoutSegment(0.0, 3.0, cp.LAYOUT_SPLIT_STACKED),
    cp.LayoutSegment(3.0, 8.0, cp.LAYOUT_TRACK),
]
WORDS = [
    {"word": " Das", "start": 10.2, "end": 10.6}, {"word": " ist", "start": 10.6, "end": 11.0},
    {"word": " gut", "start": 11.0, "end": 11.5},
    {"word": " Und", "start": 14.6, "end": 15.0}, {"word": " jetzt", "start": 15.0, "end": 15.6},
]


def test_auto_is_the_new_default_position():
    assert app.SubtitleRequest(job_id="j", clip_index=0).position == "auto"


@pytest.mark.parametrize("position", ["auto", "top", "middle", "bottom"])
def test_every_documented_position_is_valid(position):
    assert app.SubtitleRequest(job_id="j", clip_index=0, position=position).position == position


@pytest.mark.parametrize("position", ["AUTO", "center", "", "seam"])
def test_unknown_positions_are_rejected(position):
    with pytest.raises(ValidationError):
        app.SubtitleRequest(job_id="j", clip_index=0, position=position)


def _install_short_job(monkeypatch, tmp_path, job_id, *, timeline=TIMELINE, clean_source=None,
                       video_type="short"):
    """One finished short (clip seconds 0-8 = source 10-18) ready for subtitles."""
    output_dir = tmp_path / job_id
    output_dir.mkdir(parents=True)
    (output_dir / CLIP).write_bytes(b"video")
    if timeline is not None:
        Path(cp.layout_sidecar_path(str(output_dir / CLIP))).write_text(
            json.dumps(cp.layout_timeline_payload(timeline, 30)), encoding="utf-8",
        )
    if clean_source:
        (output_dir / clean_source).write_bytes(b"edited video")
        (output_dir / app.CLIP_LAYERS_FILE).write_text(json.dumps({
            "version": 2, "legacy_entries": {},
            "clips": {"0": {"clean_source": clean_source, "current_render": clean_source,
                            "subtitle": None, "hook": None}},
        }), encoding="utf-8")
    entry = {"output_filename": CLIP, "start": 10, "end": 18}
    container = "long_videos" if video_type == "long" else "shorts"
    if video_type == "long":
        entry.update(video_type="long", start=0, end=8, segments=[{"start": 10, "end": 18}])
    metadata = {"transcript": {"segments": [{"words": WORDS}]}, "shorts": [], "long_videos": []}
    metadata[container] = [entry]
    (output_dir / "show_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    result_clip = {"output_filename": CLIP, "video_url": f"/videos/{job_id}/{CLIP}"}
    if video_type == "long":
        result_clip["video_type"] = "long"
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(app, "jobs", {job_id: {
        "job_id": job_id, "status": "completed", "output_dir": str(output_dir),
        "result": {"clips": [result_clip]}, "raw_logs": [], "important_logs": [],
    }})
    return output_dir


def _fake_render(_output_dir, _entry, output_path):
    Path(output_path).write_bytes(b"subtitled video")


def _subtitle(monkeypatch, job_id, frame_size, probed=None, **request):
    def probe(path):
        if probed is not None:
            probed.append(Path(path).name)
        return frame_size

    monkeypatch.setattr(app, "probe_frame_size", probe)
    monkeypatch.setattr(app, "_render_stored_layers", _fake_render)
    asyncio.run(app._add_subtitles_locked(app.SubtitleRequest(job_id=job_id, clip_index=0, **request)))
    store = json.loads((Path(app.OUTPUT_DIR) / job_id / app.CLIP_LAYERS_FILE).read_text(encoding="utf-8"))
    subtitle = store["clips"]["0"]["subtitle"]
    return subtitle, Path(app.OUTPUT_DIR) / job_id / subtitle["path"]


def _event_styles(path):
    styles = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("Dialogue:"):
            fields = line.split(",", 9)
            styles.append((fields[1], fields[3]))
    return styles


@pytest.mark.parametrize("request_style", [
    dict(style="classic"), dict(style="karaoke"), dict(preset="neon_sweep"),
    dict(preset="neon_tube"), dict(preset="rainbow_word"),
])
def test_portrait_auto_puts_split_blocks_on_the_seam(monkeypatch, tmp_path, request_style):
    job_id = "portrait-auto"
    _install_short_job(monkeypatch, tmp_path, job_id)
    subtitle, path = _subtitle(monkeypatch, job_id, (1080, 1920), **request_style)
    assert path.suffix == ".ass"
    styles = _event_styles(path)
    # Clip seconds 0.2-1.5 play over SPLIT, 4.6-5.6 over TRACK.
    assert {style for start, style in styles if start < "0:00:03"} == {"Middle"}
    assert {style for start, style in styles if start > "0:00:03"} == {"Bottom"}
    assert subtitle["burn_opts"]["alignment"] == "bottom"


def test_derived_clean_source_still_finds_the_clip_timeline(monkeypatch, tmp_path):
    job_id = "portrait-edited"
    edited = "edited_abcdef123456_show_clip_1.mp4"
    _install_short_job(monkeypatch, tmp_path, job_id, clean_source=edited)
    probed = []
    _subtitle_entry, path = _subtitle(monkeypatch, job_id, (1080, 1920), probed=probed)
    assert probed == [edited]   # the file the captions are burned into decides
    assert {style for start, style in _event_styles(path) if start < "0:00:03"} == {"Middle"}


def test_old_clip_without_timeline_gets_the_bottom(monkeypatch, tmp_path):
    job_id = "portrait-old"
    _install_short_job(monkeypatch, tmp_path, job_id, timeline=None)
    _subtitle_entry, path = _subtitle(monkeypatch, job_id, (1080, 1920))
    assert {style for _start, style in _event_styles(path)} == {"Bottom"}


def test_fixed_position_on_portrait_stays_in_the_safe_zone(monkeypatch, tmp_path):
    job_id = "portrait-top"
    _install_short_job(monkeypatch, tmp_path, job_id)
    _subtitle_entry, path = _subtitle(monkeypatch, job_id, (1080, 1920), position="top")
    assert {style for _start, style in _event_styles(path)} == {"Top"}
    top_style = next(line for line in path.read_text(encoding="utf-8-sig").splitlines()
                     if line.startswith("Style: Top,"))
    assert top_style.split(",")[18:22] == ["8", "56", "56", "31"]


@pytest.mark.parametrize("frame_size", [(1080, 1080), (1920, 1080), None])
@pytest.mark.parametrize("request_style", [dict(style="classic"), dict(style="karaoke")])
def test_auto_outside_portrait_is_exactly_the_legacy_bottom(monkeypatch, tmp_path, frame_size,
                                                            request_style):
    auto_job, bottom_job = "legacy-auto", "legacy-bottom"
    _install_short_job(monkeypatch, tmp_path / "a", auto_job)
    auto_subtitle, auto_path = _subtitle(monkeypatch, auto_job, frame_size, **request_style)
    _install_short_job(monkeypatch, tmp_path / "b", bottom_job)
    bottom_subtitle, bottom_path = _subtitle(monkeypatch, bottom_job, frame_size,
                                             position="bottom", **request_style)
    assert auto_path.suffix == (".srt" if request_style.get("style") == "classic" else ".ass")
    assert auto_path.read_bytes() == bottom_path.read_bytes()
    assert auto_subtitle["burn_opts"] == bottom_subtitle["burn_opts"]
    assert auto_subtitle["burn_opts"]["alignment"] == "bottom"


def test_long_video_keeps_legacy_captions_even_when_portrait(monkeypatch, tmp_path):
    job_id = "long-portrait"
    _install_short_job(monkeypatch, tmp_path, job_id, video_type="long")
    probed = []
    subtitle, path = _subtitle(monkeypatch, job_id, (1080, 1920), probed=probed)
    assert probed == []            # long videos are not probed (only Neon Tube needs it)
    assert path.suffix == ".srt"
    assert subtitle["burn_opts"]["alignment"] == "bottom"


def test_dubbed_portrait_classic_is_transcribed_into_a_placed_ass(monkeypatch, tmp_path):
    job_id = "portrait-dubbed"
    translated = "translated_de_abcdef123456_show_clip_1.mp4"
    _install_short_job(monkeypatch, tmp_path, job_id, clean_source=translated)
    captured = {}

    def fake_from_video(video_path, output_path, **opts):
        captured.update(opts, video=Path(video_path).name, output=Path(output_path).name)
        Path(output_path).write_text("dubbed", encoding="utf-8")
        return True

    monkeypatch.setattr(app, "generate_srt_from_video", fake_from_video)
    _subtitle(monkeypatch, job_id, (1080, 1920))
    assert captured["video"] == translated
    assert captured["output"].endswith(".ass")
    assert captured["placement"].slot_for_block(0.2, 1.5) == cp.SLOT_MIDDLE
    assert captured["fontsize"] == 16 and "style" not in captured


def test_root_artifact_rescue_moves_the_layout_timeline_with_its_clip(monkeypatch, tmp_path):
    job_id = "job-rescue"
    base_name = f"{job_id}_Video"
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path))
    (tmp_path / f"{base_name}_metadata.json").write_text(json.dumps({"shorts": []}), encoding="utf-8")
    clip = tmp_path / f"{base_name}_clip_1.mp4"
    clip.write_bytes(b"video")
    sidecar = Path(cp.layout_sidecar_path(str(clip)))
    sidecar.write_text(json.dumps(cp.layout_timeline_payload(TIMELINE, 30)), encoding="utf-8")

    assert app._relocate_root_job_artifacts(job_id, str(tmp_path / job_id)) is True

    moved = tmp_path / job_id / clip.name
    assert moved.exists() and not sidecar.exists()
    assert cp.load_layout_timeline(str(moved)) == TIMELINE


def test_a_later_hook_reburns_the_same_placed_file(monkeypatch, tmp_path):
    """The placement lives in the stored file, not in the request."""
    job_id = "portrait-hook"
    _install_short_job(monkeypatch, tmp_path, job_id)
    real_render = app._render_stored_layers
    subtitle, path = _subtitle(monkeypatch, job_id, (1080, 1920))
    assert subtitle["path"].endswith(".ass")
    placed = path.read_bytes()
    burned = {}

    def fake_burn(input_path, output_path, subtitle_path=None, burn_opts=None, **_hook):
        burned.update(input=Path(input_path).name, subtitle=Path(subtitle_path).read_bytes())
        Path(output_path).write_bytes(b"hooked")

    monkeypatch.setattr(app, "_render_stored_layers", real_render)
    monkeypatch.setattr(app, "burn_layers", fake_burn)
    monkeypatch.setattr(app, "_prepare_hook_layer", lambda _input, _hook: (None, 0, 0))
    asyncio.run(app._add_hook_locked(app.HookRequest(job_id=job_id, clip_index=0, text="Hook")))
    assert burned == {"input": CLIP, "subtitle": placed}


# --- Hook at the top vs auto captions at the top --------------------------

LOW_FACE = [cp.LayoutSegment(0.0, 8.0, cp.LAYOUT_TRACK, face_top=0.62, face_bottom=0.74)]


def _burn_spy(monkeypatch, tmp_path):
    """Real layer composition, fake FFmpeg: records which subtitle file burns."""
    burned = []

    def fake_burn(input_path, output_path, subtitle_path=None, burn_opts=None, **hook):
        burned.append(Path(subtitle_path).name if subtitle_path else None)
        Path(output_path).write_bytes(b"burned")

    def fake_hook(_input, hook):
        if not hook:
            return None, 0, 0
        png = tmp_path / "hook.png"   # removed again after the burn
        png.write_bytes(b"png")
        return str(png), 0, 384

    monkeypatch.setattr(app, "burn_layers", fake_burn)
    monkeypatch.setattr(app, "_prepare_hook_layer", fake_hook)
    monkeypatch.setattr(app, "probe_frame_size", lambda _path: (1080, 1920))
    return burned


def _slots_in(path):
    return {style for _start, style in _event_styles(path)}


def _store(job_id):
    return json.loads((Path(app.OUTPUT_DIR) / job_id / app.CLIP_LAYERS_FILE).read_text(encoding="utf-8"))


def test_subtitles_then_hook_never_overlap_at_the_top(monkeypatch, tmp_path):
    job_id = "subs-then-hook"
    output_dir = _install_short_job(monkeypatch, tmp_path, job_id, timeline=LOW_FACE)
    burned = _burn_spy(monkeypatch, tmp_path)
    asyncio.run(app._add_subtitles_locked(app.SubtitleRequest(job_id=job_id, clip_index=0)))
    subtitle = _store(job_id)["clips"]["0"]["subtitle"]
    assert _slots_in(output_dir / subtitle["path"]) == {"Top"}            # the face sits low
    assert list(subtitle["hook_variants"]) == ["top"]                     # no seam to keep free
    assert _slots_in(output_dir / subtitle["hook_variants"]["top"]) == {"Bottom"}
    assert burned[-1] == subtitle["path"]

    asyncio.run(app._add_hook_locked(app.HookRequest(job_id=job_id, clip_index=0, text="Hook")))
    assert burned[-1] == subtitle["hook_variants"]["top"]                 # no transcription needed

    asyncio.run(app._remove_clip_layer_locked(
        app.RemoveLayerRequest(job_id=job_id, clip_index=0, layer="hook")))
    assert burned[-1] == subtitle["path"]                                 # the top is free again


def test_subtitles_then_center_hook_never_cover_the_seam(monkeypatch, tmp_path):
    job_id = "subs-then-center-hook"
    output_dir = _install_short_job(monkeypatch, tmp_path, job_id)       # SPLIT 0-3 s, then TRACK
    burned = _burn_spy(monkeypatch, tmp_path)
    asyncio.run(app._add_subtitles_locked(app.SubtitleRequest(job_id=job_id, clip_index=0)))
    subtitle = _store(job_id)["clips"]["0"]["subtitle"]
    assert _slots_in(output_dir / subtitle["path"]) == {"Middle", "Bottom"}
    assert list(subtitle["hook_variants"]) == ["middle"]
    assert _slots_in(output_dir / subtitle["hook_variants"]["middle"]) == {"Bottom"}

    asyncio.run(app._add_hook_locked(app.HookRequest(
        job_id=job_id, clip_index=0, text="Hook", position="center")))
    assert burned[-1] == subtitle["hook_variants"]["middle"]


@pytest.mark.parametrize("timeline,hook_position,expected", [
    (LOW_FACE, "top", "top"),          # a top hook keeps the top free
    (LOW_FACE, "center", None),        # a center hook does not cover the top
    (LOW_FACE, "bottom", None),        # a bottom hook is not handled (legacy)
    (TIMELINE, "center", "middle"),    # a center hook keeps the seam free
    (TIMELINE, "top", None),
])
def test_hook_then_subtitles_keep_the_hooks_slot_free(monkeypatch, tmp_path, timeline, hook_position,
                                                      expected):
    job_id = f"hook-{hook_position}-then-subs"
    _install_short_job(monkeypatch, tmp_path, job_id, timeline=timeline)
    burned = _burn_spy(monkeypatch, tmp_path)
    asyncio.run(app._add_hook_locked(app.HookRequest(
        job_id=job_id, clip_index=0, text="Hook", position=hook_position)))
    asyncio.run(app._add_subtitles_locked(app.SubtitleRequest(job_id=job_id, clip_index=0)))
    subtitle = _store(job_id)["clips"]["0"]["subtitle"]
    assert burned[-1] == (subtitle["hook_variants"][expected] if expected else subtitle["path"])


def test_captions_without_a_hook_slot_keep_a_single_file(monkeypatch, tmp_path):
    job_id = "no-split-no-face"
    output_dir = _install_short_job(monkeypatch, tmp_path, job_id,
                                    timeline=[cp.LayoutSegment(0.0, 8.0, cp.LAYOUT_TRACK)])
    _burn_spy(monkeypatch, tmp_path)
    asyncio.run(app._add_subtitles_locked(app.SubtitleRequest(job_id=job_id, clip_index=0)))
    subtitle = _store(job_id)["clips"]["0"]["subtitle"]
    assert "hook_variants" not in subtitle
    assert sorted(path.name for path in output_dir.glob("subs_*")) == [subtitle["path"]]


@pytest.mark.parametrize("position", ["top", "middle", "bottom"])
def test_hand_picked_positions_write_one_file(monkeypatch, tmp_path, position):
    job_id = f"hand-{position}"
    output_dir = _install_short_job(monkeypatch, tmp_path, job_id, timeline=LOW_FACE)
    _burn_spy(monkeypatch, tmp_path)
    asyncio.run(app._add_subtitles_locked(app.SubtitleRequest(job_id=job_id, clip_index=0, position=position)))
    subtitle = _store(job_id)["clips"]["0"]["subtitle"]
    assert "hook_variants" not in subtitle
    assert _slots_in(output_dir / subtitle["path"]) == {position.capitalize()}


def test_a_new_subtitle_run_removes_all_old_files(monkeypatch, tmp_path):
    job_id = "restyle"
    timeline = [cp.LayoutSegment(0.0, 3.0, cp.LAYOUT_SPLIT_STACKED)] + LOW_FACE
    output_dir = _install_short_job(monkeypatch, tmp_path, job_id, timeline=timeline)
    _burn_spy(monkeypatch, tmp_path)
    asyncio.run(app._add_subtitles_locked(app.SubtitleRequest(job_id=job_id, clip_index=0)))
    first = _store(job_id)["clips"]["0"]["subtitle"]
    assert sorted(first["hook_variants"]) == ["middle", "top"]
    asyncio.run(app._add_subtitles_locked(app.SubtitleRequest(job_id=job_id, clip_index=0, style="karaoke")))
    second = _store(job_id)["clips"]["0"]["subtitle"]
    left = {path.name for path in output_dir.glob("subs_*")}
    assert left == {second["path"], *second["hook_variants"].values()}
    assert not left & {first["path"], *first["hook_variants"].values()}


def test_a_failed_timeline_move_does_not_stop_the_rescue(monkeypatch, tmp_path):
    job_id = "job-rescue-fail"
    base_name = f"{job_id}_Video"
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path))
    (tmp_path / f"{base_name}_metadata.json").write_text(json.dumps({"shorts": []}), encoding="utf-8")
    clips = [tmp_path / f"{base_name}_clip_{index}.mp4" for index in (1, 2)]
    for clip in clips:
        clip.write_bytes(b"video")
        Path(cp.layout_sidecar_path(str(clip))).write_text("{}", encoding="utf-8")
    real_move = app.shutil.move

    def move(source, destination):
        if str(source).endswith(cp.LAYOUT_SIDECAR_SUFFIX):
            raise PermissionError("locked")
        return real_move(source, destination)

    monkeypatch.setattr(app.shutil, "move", move)
    assert app._relocate_root_job_artifacts(job_id, str(tmp_path / job_id)) is True
    assert all((tmp_path / job_id / clip.name).exists() for clip in clips)
