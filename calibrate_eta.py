"""Seed the learned ETA history with speeds measured on this machine.

Until enough jobs have finished, the live ETA falls back to guessed defaults
(main.PHASE_COST_PRIORS), so the first jobs on a new machine show remaining
times that can be far off. This script measures the two hardware-bound phases
with the pipeline's own code — faster-whisper transcription and the vertical
reframing renderer — and stores the results as regular samples in
output/.job_stats.json, exactly where finished jobs record theirs. Download
and Gemini analysis are network/API-bound, not hardware-bound, so their
defaults are left untouched.

Each phase is measured on a short and a long segment and only the marginal
cost per media second is recorded: one-off costs such as model loading cancel
out in the difference instead of inflating the per-second rate (they are
covered by main.PHASE_COST_FLOOR).

A real video with speech is required — the pipeline transcribes with a
voice-activity filter, so synthetic tones would be skipped and produce a
uselessly optimistic transcription rate. By default the largest source video
a previous job left in output/ is used.

Usage:
    python calibrate_eta.py                  # use a previous job's source video
    python calibrate_eta.py --source my.mp4  # measure with a specific video
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

import main


def marginal_rate(short_elapsed, short_media_seconds, long_elapsed, long_media_seconds):
    """Cost per media second with fixed one-off overhead cancelled out."""
    span = float(long_media_seconds) - float(short_media_seconds)
    if span <= 0:
        return None
    return max(0.0, (float(long_elapsed) - float(short_elapsed)) / span)


def find_default_source():
    """The largest complete source video a previous job left in output/."""
    output_root = os.path.dirname(main.JOB_STATS_PATH)
    try:
        entries = os.listdir(output_root)
    except FileNotFoundError:
        return None
    candidates = []
    for entry in entries:
        job_dir = os.path.join(output_root, entry)
        if entry.startswith(".") or entry == "thumbnails" or not os.path.isdir(job_dir):
            continue
        source = main._find_source_video(job_dir, require_audio=True)
        if source:
            candidates.append(source)
    return max(candidates, key=os.path.getsize) if candidates else None


def _cut_segment(source, seconds, dest):
    """Re-encode the first ``seconds`` of ``source`` into a clean benchmark clip."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-ss", "0", "-t", f"{float(seconds):.3f}",
            "-i", source,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac",
            dest,
        ],
        check=True, timeout=600,
    )


def benchmark_transcribe(source, workdir, long_seconds):
    """Measured faster-whisper seconds per source second on this machine."""
    short_seconds = max(10.0, float(long_seconds) / 4.0)
    timings = {}
    for name, seconds in (("short", short_seconds), ("long", float(long_seconds))):
        segment = os.path.join(workdir, f"transcribe_{name}.mp4")
        _cut_segment(source, seconds, segment)
        started = time.perf_counter()
        main.transcribe_video(segment, seconds)
        timings[name] = time.perf_counter() - started
    return marginal_rate(timings["short"], short_seconds, timings["long"], long_seconds)


def benchmark_render(source, workdir, long_seconds):
    """Measured vertical-reframe render seconds per output second."""
    short_seconds = max(4.0, float(long_seconds) / 3.0)
    timings = {}
    for name, seconds in (("short", short_seconds), ("long", float(long_seconds))):
        segment = os.path.join(workdir, f"render_src_{name}.mp4")
        _cut_segment(source, seconds, segment)
        output = os.path.join(workdir, f"render_out_{name}.mp4")
        started = time.perf_counter()
        if not main._render_clip(segment, output, output_format="vertical", layout_style="smart"):
            raise RuntimeError(f"Benchmark render failed for the {name} segment")
        timings[name] = time.perf_counter() - started
    return marginal_rate(timings["short"], short_seconds, timings["long"], long_seconds)


def run_calibration(argv=None):
    parser = argparse.ArgumentParser(
        description="Measure this machine's transcription and render speed "
                    "and seed the ETA history with the results.",
    )
    parser.add_argument(
        "--source",
        help="Video with real speech to measure with (default: the largest "
             "source video a previous job left in output/).",
    )
    parser.add_argument("--transcribe-seconds", type=float, default=60.0,
                        help="Length of the long transcription segment (default: 60).")
    parser.add_argument("--render-seconds", type=float, default=12.0,
                        help="Length of the long render segment (default: 12).")
    parser.add_argument("--skip-transcribe", action="store_true")
    parser.add_argument("--skip-render", action="store_true")
    args = parser.parse_args(argv)

    source = args.source or find_default_source()
    if not source or not os.path.exists(source):
        print(
            "❌ No source video found. Run one job first or pass --source. "
            "A real video with speech is required: the pipeline's "
            "voice-activity filter would skip synthetic audio and make the "
            "transcription measurement uselessly fast."
        )
        return 1
    duration = main._get_video_duration(source)
    needed = max(float(args.transcribe_seconds), float(args.render_seconds))
    if duration < needed:
        print(f"❌ Source is too short: {duration:.0f}s, but {needed:.0f}s are needed.")
        return 1

    print(f"🎬 Calibrating with: {source}")
    samples = {}
    workdir = tempfile.mkdtemp(prefix="eta_calibration_")
    try:
        if not args.skip_transcribe:
            print("⏱️  Measuring transcription speed (short + long segment)...")
            rate = benchmark_transcribe(source, workdir, args.transcribe_seconds)
            # A near-zero marginal rate means the VAD skipped the audio or the
            # clock resolution was hit — such a sample would poison the median.
            if rate and rate > 0.001:
                samples["transcribe"] = {"per_source_second": [rate]}
                print(f"   transcribe: {rate:.3f}s per source second "
                      f"(guessed default: {main.PHASE_COST_PRIORS['transcribe']})")
            else:
                print("   ⚠️ Transcription finished implausibly fast — sample "
                      "skipped. Does the source contain real speech?")
        if not args.skip_render:
            print("⏱️  Measuring render speed (short + long segment)...")
            rate = benchmark_render(source, workdir, args.render_seconds)
            if rate and rate > 0.001:
                samples["render"] = {"per_output_second": [rate]}
                print(f"   render: {rate:.3f}s per output second "
                      f"(guessed default: {main.PHASE_COST_PRIORS['render']})")
            else:
                print("   ⚠️ Render finished implausibly fast — sample skipped.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if not samples:
        print("Nothing was measured; the ETA keeps its current numbers.")
        return 1
    main._append_job_stat_samples(samples)
    print(f"✅ Measured speeds stored in {main.JOB_STATS_PATH}.")
    print("   Download and Gemini analysis stay on their defaults — they "
          "depend on network/API latency, not on this machine.")
    return 0


if __name__ == "__main__":
    sys.exit(run_calibration())
