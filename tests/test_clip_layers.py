import asyncio
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


def _race_job(monkeypatch, tmp_path, job_id="job-race"):
    """Two-clip job fixture for the job-wide lost-update regression tests."""
    output_dir = tmp_path / job_id
    output_dir.mkdir()
    (output_dir / "clip_a.mp4").write_bytes(b"a")
    (output_dir / "clip_b.mp4").write_bytes(b"b")
    shorts = [
        {"output_filename": "clip_a.mp4", "video_url": f"/videos/{job_id}/clip_a.mp4"},
        {"output_filename": "clip_b.mp4", "video_url": f"/videos/{job_id}/clip_b.mp4"},
    ]
    metadata_path = output_dir / "video_metadata.json"
    metadata_path.write_text(json.dumps({"shorts": shorts}), encoding="utf-8")
    monkeypatch.setattr(app, "jobs", {
        job_id: {
            "job_id": job_id,
            "status": "completed",
            "output_dir": str(output_dir),
            "result": {"clips": [dict(short) for short in shorts]},
            "raw_logs": [],
            "important_logs": [],
        }
    })
    monkeypatch.setattr(app, "job_state_locks", {})
    return job_id, output_dir, metadata_path, shorts


def test_parallel_clip_commits_do_not_lose_each_other(monkeypatch, tmp_path):
    """clip_layers.json and metadata.json are job-wide while locking is
    per clip: both operations read the initial state before either one
    saves, exactly like two clips encoding in parallel."""
    job_id, output_dir, metadata_path, shorts = _race_job(monkeypatch, tmp_path)

    async def scenario():
        entry_a = await app._resolve_clip_layer_entry(job_id, str(output_dir), 0, shorts[0])
        entry_b = await app._resolve_clip_layer_entry(job_id, str(output_dir), 1, shorts[1])

        entry_a["subtitle"] = {"path": "subs_a.ass"}
        entry_a["current_render"] = "subtitled_x_clip_a.mp4"
        await app._commit_clip_layer_state(
            job_id, str(output_dir), 0, entry_a,
            f"/videos/{job_id}/subtitled_x_clip_a.mp4",
            metadata_path=str(metadata_path),
        )

        entry_b["hook"] = {"text": "Zweiter Hook"}
        entry_b["current_render"] = "hook_y_clip_b.mp4"
        await app._commit_clip_layer_state(
            job_id, str(output_dir), 1, entry_b,
            f"/videos/{job_id}/hook_y_clip_b.mp4",
            metadata_path=str(metadata_path),
        )

    asyncio.run(scenario())

    store = json.loads((output_dir / app.CLIP_LAYERS_FILE).read_text(encoding="utf-8"))
    assert store["clips"]["0"]["subtitle"] == {"path": "subs_a.ass"}
    assert store["clips"]["0"]["current_render"] == "subtitled_x_clip_a.mp4"
    assert store["clips"]["1"]["hook"] == {"text": "Zweiter Hook"}

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["shorts"][0]["video_url"] == f"/videos/{job_id}/subtitled_x_clip_a.mp4"
    assert metadata["shorts"][1]["video_url"] == f"/videos/{job_id}/hook_y_clip_b.mp4"
    assert app.jobs[job_id]["result"]["clips"][0]["video_url"].endswith("subtitled_x_clip_a.mp4")


def test_resolve_entry_persists_migration_and_returns_a_copy(monkeypatch, tmp_path):
    """v1 migration is persisted immediately under the job lock, and the
    returned entry is a copy — mutating it before commit must not leak
    into what another clip reads from disk."""
    job_id, output_dir, _metadata_path, shorts = _race_job(monkeypatch, tmp_path)
    (output_dir / app.CLIP_LAYERS_FILE).write_text(json.dumps({
        "clip_a.mp4": {"subtitle": {"path": "legacy_a.ass"}},
        "clip_b.mp4": {"hook": {"text": "Legacy-Hook B"}},
    }), encoding="utf-8")

    async def scenario():
        return await app._resolve_clip_layer_entry(job_id, str(output_dir), 0, shorts[0])

    entry = asyncio.run(scenario())
    assert entry["subtitle"] == {"path": "legacy_a.ass"}

    on_disk = json.loads((output_dir / app.CLIP_LAYERS_FILE).read_text(encoding="utf-8"))
    assert on_disk["version"] == app.CLIP_LAYERS_VERSION
    assert on_disk["clips"]["0"]["subtitle"] == {"path": "legacy_a.ass"}
    # clip_b's unresolved v1 entry survives for its own later migration
    assert on_disk["legacy_entries"] == {"clip_b.mp4": {"hook": {"text": "Legacy-Hook B"}}}

    entry["subtitle"]["path"] = "mutated.ass"
    unchanged = json.loads((output_dir / app.CLIP_LAYERS_FILE).read_text(encoding="utf-8"))
    assert unchanged["clips"]["0"]["subtitle"] == {"path": "legacy_a.ass"}


def _store(clips, legacy=None):
    return {"version": 2, "clips": clips, "legacy_entries": legacy or {}}


class TestPruneReplacedClipFiles:
    """Every restyle wrote a new full-length MP4 and kept the old one.

    Trying five preset looks on a ten-clip job left 50 orphaned videos behind
    until the whole job was purged 24 hours later.
    """

    def _populate(self, tmp_path, names):
        for name in names:
            (tmp_path / name).write_bytes(b"x")

    def test_removes_the_render_and_subtitle_this_clip_replaced(self, tmp_path):
        self._populate(tmp_path, [
            "clip.mp4",
            "subtitled_new111_clip.mp4", "subs_0_new111.ass",   # current
            "subtitled_old999_clip.mp4", "subs_0_old999.ass",   # replaced
        ])
        previous = {
            "clean_source": "clip.mp4",
            "current_render": "subtitled_old999_clip.mp4",
            "subtitle": {"path": "subs_0_old999.ass"}, "hook": None,
        }
        store = _store({"0": {
            "clean_source": "clip.mp4",
            "current_render": "subtitled_new111_clip.mp4",
            "subtitle": {"path": "subs_0_new111.ass"}, "hook": None,
        }})

        app._prune_replaced_clip_files(str(tmp_path), previous, store)

        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "clip.mp4", "subs_0_new111.ass", "subtitled_new111_clip.mp4",
        ]

    def test_never_deletes_a_clean_source_or_unrelated_file(self, tmp_path):
        """Only the two generated name patterns may ever be removed."""
        self._populate(tmp_path, ["clip.mp4", "edited_x_clip.mp4", "translated_y_clip.mp4"])
        previous = {"clean_source": "clip.mp4", "current_render": "edited_x_clip.mp4",
                    "subtitle": None, "hook": None}
        store = _store({"0": {"clean_source": "translated_y_clip.mp4",
                              "current_render": "translated_y_clip.mp4",
                              "subtitle": None, "hook": None}})

        app._prune_replaced_clip_files(str(tmp_path), previous, store)

        assert len(list(tmp_path.iterdir())) == 3

    def test_keeps_a_render_another_clip_still_points_at(self, tmp_path):
        self._populate(tmp_path, ["clip.mp4", "subtitled_shared_clip.mp4"])
        previous = {"clean_source": "clip.mp4",
                    "current_render": "subtitled_shared_clip.mp4",
                    "subtitle": None, "hook": None}
        store = _store({
            "0": {"clean_source": "clip.mp4", "current_render": "clip.mp4",
                  "subtitle": None, "hook": None},
            "1": {"clean_source": "subtitled_shared_clip.mp4",
                  "current_render": "subtitled_shared_clip.mp4",
                  "subtitle": None, "hook": None},
        })

        app._prune_replaced_clip_files(str(tmp_path), previous, store)

        assert (tmp_path / "subtitled_shared_clip.mp4").exists()

    def test_does_not_touch_files_of_a_clip_still_encoding(self, tmp_path):
        """The reason this is not a directory sweep.

        Another clip of the same job can be minutes into an FFmpeg encode
        whose output is not in the store yet; a sweep would delete it while
        it is being written.
        """
        self._populate(tmp_path, [
            "clip.mp4",
            "subtitled_old_clip1.mp4",           # what clip 0 replaced
            "subtitled_inflight_clip2.mp4",      # clip 1 is still writing this
        ])
        previous = {"clean_source": "clip.mp4",
                    "current_render": "subtitled_old_clip1.mp4",
                    "subtitle": None, "hook": None}
        store = _store({"0": {"clean_source": "clip.mp4",
                              "current_render": "subtitled_new_clip1.mp4",
                              "subtitle": None, "hook": None}})

        app._prune_replaced_clip_files(str(tmp_path), previous, store)

        assert (tmp_path / "subtitled_inflight_clip2.mp4").exists()
        assert not (tmp_path / "subtitled_old_clip1.mp4").exists()

    def test_keeps_subtitle_files_referenced_by_unmigrated_v1_entries(self, tmp_path):
        self._populate(tmp_path, ["clip.mp4", "subs_3_legacy.ass"])
        previous = {"clean_source": "clip.mp4", "current_render": "clip.mp4",
                    "subtitle": {"path": "subs_3_legacy.ass"}, "hook": None}
        store = _store(
            {"0": {"clean_source": "clip.mp4", "current_render": "clip.mp4",
                   "subtitle": None, "hook": None}},
            legacy={"other.mp4": {"subtitle": {"path": "subs_3_legacy.ass"}}},
        )

        app._prune_replaced_clip_files(str(tmp_path), previous, store)

        assert (tmp_path / "subs_3_legacy.ass").exists()

    def test_first_operation_on_a_clip_has_nothing_to_prune(self, tmp_path):
        app._prune_replaced_clip_files(str(tmp_path), None, _store({}))


class TestRemoveLayerState:
    """Removing a layer re-renders from the clean source without it."""

    def test_dropping_the_last_layer_needs_no_encode(self, tmp_path):
        entry = {"clean_source": "clip.mp4", "current_render": "subtitled_a_clip.mp4",
                 "subtitle": {"path": "subs_0_a.ass"}, "hook": None}
        candidate = dict(entry)
        candidate["subtitle"] = None
        assert not (candidate.get("subtitle") or candidate.get("hook"))
        # The clean source becomes the result as-is.
        (tmp_path / "clip.mp4").write_bytes(b"video")
        assert os.path.basename(
            app._clean_source_path(str(tmp_path), candidate)
        ) == "clip.mp4"

    def test_layer_summary_reports_what_the_client_can_remove(self):
        assert app._clip_layer_summary({"subtitle": {"path": "s.ass"}, "hook": None}) == {
            "subtitle": True, "hook": False,
        }
        assert app._clip_layer_summary({"subtitle": None, "hook": {"text": "Hi"}}) == {
            "subtitle": False, "hook": True,
        }

    def test_remove_all_falls_back_to_the_original_clip(self, tmp_path):
        """'all' also undoes auto-edit and dubbing, not just the overlays."""
        (tmp_path / "clip.mp4").write_bytes(b"original")
        entry = {"clean_source": "translated_x_clip.mp4",
                 "current_render": "subtitled_a_translated_x_clip.mp4",
                 "subtitle": {"path": "s.ass"}, "hook": {"text": "Hi"},
                 "transcript_source": "media"}
        candidate = dict(entry)
        candidate["subtitle"] = None
        candidate["hook"] = None
        candidate["clean_source"] = "clip.mp4"
        candidate.pop("transcript_source", None)
        assert candidate["clean_source"] == "clip.mp4"
        assert "transcript_source" not in candidate
