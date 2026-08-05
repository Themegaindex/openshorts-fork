import json

import pytest

pytest.importorskip("cv2", reason="calibration imports the video pipeline")
pytest.importorskip("ultralytics")
pytest.importorskip("mediapipe")

import calibrate_eta
import main


def test_marginal_rate_cancels_fixed_overhead():
    # 20s of model loading in both runs: only the 30s of extra work across the
    # 45 extra media seconds may count, not the overhead.
    assert calibrate_eta.marginal_rate(35.0, 15.0, 65.0, 60.0) == pytest.approx(30.0 / 45.0)
    # Degenerate spans cannot produce a rate.
    assert calibrate_eta.marginal_rate(35.0, 60.0, 65.0, 60.0) is None
    # Noise can make the long run "faster"; clamp instead of storing negative cost.
    assert calibrate_eta.marginal_rate(50.0, 15.0, 40.0, 60.0) == 0.0


def test_calibration_seeds_stats_that_feed_the_first_estimate(monkeypatch, tmp_path):
    stats_path = tmp_path / ".job_stats.json"
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr(main, "JOB_STATS_PATH", str(stats_path))
    monkeypatch.setattr(calibrate_eta, "find_default_source", lambda: str(source))
    monkeypatch.setattr(main, "_get_video_duration", lambda path: 600.0)
    monkeypatch.setattr(calibrate_eta, "benchmark_transcribe", lambda *args: 0.42)
    monkeypatch.setattr(calibrate_eta, "benchmark_render", lambda *args: 1.05)

    assert calibrate_eta.run_calibration([]) == 0

    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    assert stats["transcribe"]["per_source_second"] == [0.42]
    assert stats["render"]["per_output_second"] == [1.05]

    # A brand-new job must pick the measured numbers up instead of the guess.
    reporter = main.JobReporter(job_id="first-after-calibration")
    reporter.video_duration = 100.0
    assert reporter._phase_cost_prior("transcribe") == pytest.approx(42.0)


def test_calibration_rejects_implausibly_fast_measurements(monkeypatch, tmp_path):
    stats_path = tmp_path / ".job_stats.json"
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr(main, "JOB_STATS_PATH", str(stats_path))
    monkeypatch.setattr(main, "_get_video_duration", lambda path: 600.0)
    # A VAD that skipped all audio yields a ~zero marginal rate. Storing it
    # would poison the median with a "transcription is free" sample.
    monkeypatch.setattr(calibrate_eta, "benchmark_transcribe", lambda *args: 0.0)
    monkeypatch.setattr(calibrate_eta, "benchmark_render", lambda *args: 0.0)

    assert calibrate_eta.run_calibration(["--source", str(source)]) == 1
    assert not stats_path.exists()


def test_calibration_without_source_explains_the_speech_requirement(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(main, "JOB_STATS_PATH", str(tmp_path / ".job_stats.json"))
    monkeypatch.setattr(calibrate_eta, "find_default_source", lambda: None)

    assert calibrate_eta.run_calibration([]) == 1
    assert "voice-activity filter" in capsys.readouterr().out
