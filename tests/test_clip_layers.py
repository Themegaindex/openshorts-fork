import json
import os

import pytest

pytest.importorskip("fastapi", reason="FastAPI backend dependencies are optional locally")

import app


def _clip(current="clip.mp4"):
    return {
        "output_filename": "clip.mp4",
        "video_url": f"/videos/job/{current}",
    }


def test_new_layer_state_is_indexed_by_clip_and_keeps_clean_source(tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"video")
    store, entry = app._resolve_clip_layer_state(
        str(tmp_path), 0, _clip("subtitled_old_clip.mp4"), "subtitled_old_clip.mp4",
    )
    assert store == {
        "version": 2,
        "legacy_entries": {},
        "clips": {
            "0": {
                "clean_source": "clip.mp4",
                "current_render": "subtitled_old_clip.mp4",
                "subtitle": None,
                "hook": None,
            }
        },
    }
    assert entry["clean_source"] == "clip.mp4"


def test_filename_keyed_v1_state_migrates_without_using_burned_derivative(tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"clean")
    (tmp_path / app.CLIP_LAYERS_FILE).write_text(json.dumps({
        "clip.mp4": {
            "subtitle": {"path": "subs.ass", "burn_opts": {"alignment": "bottom"}},
            "hook": {"text": "Hook", "position": "top", "size": "M"},
        }
    }), encoding="utf-8")

    store, entry = app._resolve_clip_layer_state(
        str(tmp_path), 2, _clip("edited_x_subtitled_y_clip.mp4"),
        "edited_x_subtitled_y_clip.mp4",
    )

    assert store["version"] == 2
    assert entry["clean_source"] == "clip.mp4"
    assert entry["subtitle"]["path"] == "subs.ass"
    assert entry["hook"]["text"] == "Hook"


def test_clean_legacy_edit_is_preserved_as_the_new_clean_source(tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"original")
    (tmp_path / "edited_abc_clip.mp4").write_bytes(b"edited")
    store, entry = app._resolve_clip_layer_state(
        str(tmp_path), 0, _clip("edited_abc_clip.mp4"), "edited_abc_clip.mp4",
    )
    assert store["version"] == 2
    assert entry["clean_source"] == "edited_abc_clip.mp4"


@pytest.mark.parametrize("clean_name", [
    "edited_abc_clip.mp4",
    "translated_de_abc_clip.mp4",
])
def test_layered_legacy_derivative_is_recovered_before_original(tmp_path, clean_name):
    original = tmp_path / "clip.mp4"
    clean = tmp_path / clean_name
    original.write_bytes(b"original")
    clean.write_bytes(b"clean derivative")
    current = f"subtitled_layer1_{clean_name}"
    (tmp_path / app.CLIP_LAYERS_FILE).write_text(json.dumps({
        clean_name: {
            "subtitle": {"path": "subs.ass"},
            "hook": {"text": "Keep me"},
        },
    }), encoding="utf-8")

    store, entry = app._resolve_clip_layer_state(
        str(tmp_path), 0, _clip(current), current,
    )

    assert entry["clean_source"] == clean_name
    assert entry["subtitle"]["path"] == "subs.ass"
    assert entry["hook"]["text"] == "Keep me"
    assert store["legacy_entries"] == {}


def test_migrating_one_v1_clip_preserves_other_legacy_entries(tmp_path):
    for filename in ("clip.mp4", "clip_2.mp4"):
        (tmp_path / filename).write_bytes(b"clean")
    (tmp_path / app.CLIP_LAYERS_FILE).write_text(json.dumps({
        "clip.mp4": {"subtitle": {"path": "one.ass"}},
        "clip_2.mp4": {"hook": {"text": "Second hook"}},
    }), encoding="utf-8")

    store, first = app._resolve_clip_layer_state(str(tmp_path), 0, _clip())
    assert first["subtitle"]["path"] == "one.ass"
    assert store["legacy_entries"] == {
        "clip_2.mp4": {"hook": {"text": "Second hook"}},
    }
    app._save_clip_layers(str(tmp_path), store)

    second_clip = {
        "output_filename": "clip_2.mp4",
        "video_url": "/videos/job/clip_2.mp4",
    }
    migrated, second = app._resolve_clip_layer_state(str(tmp_path), 1, second_clip)
    assert second["hook"]["text"] == "Second hook"
    assert migrated["clips"]["0"]["subtitle"]["path"] == "one.ass"
    assert migrated["legacy_entries"] == {}


def test_render_uses_clean_source_and_composes_layers_once(monkeypatch, tmp_path):
    clean = tmp_path / "edited_clean.mp4"
    subtitle = tmp_path / "subs.ass"
    hook_png = tmp_path / "hook.png"
    clean.write_bytes(b"clean")
    subtitle.write_text("ass", encoding="utf-8")
    hook_png.write_bytes(b"png")
    calls = []

    monkeypatch.setattr(
        app,
        "_prepare_hook_layer",
        lambda *_args: (str(hook_png), 10, 20),
    )
    monkeypatch.setattr(
        app,
        "burn_layers",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    entry = {
        "clean_source": clean.name,
        "current_render": "subtitled_previous_edited_clean.mp4",
        "subtitle": {"path": subtitle.name, "burn_opts": {"alignment": "bottom"}},
        "hook": {"text": "Hook"},
    }

    app._render_stored_layers(str(tmp_path), entry, str(tmp_path / "result.mp4"))

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == str(clean)
    assert args[1] == str(tmp_path / "result.mp4")
    assert kwargs["subtitle_path"] == str(subtitle)
    assert kwargs["hook_png"] == str(hook_png)
    assert not hook_png.exists()  # temporary overlay is cleaned after the pass


def test_edit_retains_layers_but_translation_clears_only_subtitle():
    initial = {
        "clean_source": "clip.mp4",
        "subtitle": {"path": "subs.ass"},
        "hook": {"text": "Hook"},
    }
    edited = app._entry_with_clean_source(initial, "edited_clip.mp4")
    assert edited["subtitle"] == initial["subtitle"]
    assert edited["hook"] == initial["hook"]

    translated = app._entry_with_clean_source(
        edited,
        "translated_de_edited_clip.mp4",
        clear_subtitle=True,
        transcript_source="media",
    )
    assert translated["subtitle"] is None
    assert translated["hook"] == initial["hook"]
    assert translated["transcript_source"] == "media"
    assert initial["clean_source"] == "clip.mp4"  # transitions are non-mutating


def test_layer_state_save_is_atomic_and_versioned(tmp_path):
    payload = {"version": 2, "clips": {"0": {"clean_source": "clip.mp4"}}}
    app._save_clip_layers(str(tmp_path), payload)
    assert json.loads((tmp_path / app.CLIP_LAYERS_FILE).read_text(encoding="utf-8")) == payload
    assert not [name for name in os.listdir(tmp_path) if name.endswith(".tmp")]
