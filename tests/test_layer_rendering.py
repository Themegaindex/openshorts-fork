import shutil
import subprocess

import pytest

from subtitles import build_layer_command, build_subtitle_filter
from video_formats import EVEN_PAD_FILTER


class TestBuildSubtitleFilter:
    def test_srt_uses_force_style(self):
        vf = build_subtitle_filter("subs.srt", alignment="bottom", fontsize=20)
        assert vf.startswith("subtitles='")
        assert "force_style=" in vf
        assert "charenc=UTF-8" in vf

    def test_ass_keeps_own_styles(self):
        vf = build_subtitle_filter("subs.ass")
        assert vf.startswith("ass='")
        assert "force_style" not in vf

    def test_font_name_sanitized(self):
        vf = build_subtitle_filter("subs.srt", font_name="Arial,Fontsize=99{\\b1}")
        assert "{" not in vf.split("force_style=")[1]
        assert "Fontname=ArialFontsize99b1" in vf

    def test_middle_alignment_uses_valid_ass_center_code(self):
        vf = build_subtitle_filter("subs.srt", alignment="middle")
        assert "Alignment=5" in vf


class TestBuildLayerCommand:
    def test_requires_at_least_one_layer(self):
        with pytest.raises(ValueError):
            build_layer_command("in.mp4", "out.mp4")

    def test_subtitles_only_uses_vf(self):
        cmd = build_layer_command("in.mp4", "out.mp4", subtitle_filter="ass='s.ass'")
        assert "-vf" in cmd
        assert "-filter_complex" not in cmd
        assert cmd[cmd.index("-vf") + 1] == (
            f"format=yuv444p,ass='s.ass',{EVEN_PAD_FILTER},format=yuv420p"
        )

    def test_hook_only_uses_overlay(self):
        cmd = build_layer_command("in.mp4", "out.mp4", hook_png="h.png", hook_x=90, hook_y=384)
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert fc == f"[0:v][1:v]overlay=90:384[v1];[v1]{EVEN_PAD_FILTER}[vout]"
        assert "-map" in cmd
        assert "[vout]" in cmd
        assert "0:a?" in cmd  # audio optional so silent clips don't fail
        assert cmd[cmd.index("-i") + 1] == "in.mp4"
        assert "h.png" in cmd

    def test_both_layers_single_pass(self):
        cmd = build_layer_command(
            "in.mp4", "out.mp4",
            subtitle_filter="ass='s.ass'", hook_png="h.png", hook_x=10, hook_y=20,
        )
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert fc == (f"[0:v]format=yuv444p,ass='s.ass'[v0];"
                      f"[v0][1:v]overlay=10:20[v1];"
                      f"[v1]{EVEN_PAD_FILTER},format=yuv420p[vout]")
        # exactly one encode: a single ffmpeg invocation with one output
        assert cmd.count("ffmpeg") == 1
        assert cmd[-1] == "out.mp4"

    def test_output_flags(self):
        cmd = build_layer_command("in.mp4", "out.mp4", subtitle_filter="ass='s.ass'")
        assert "+faststart" in cmd
        assert "copy" in cmd  # audio copied, not re-encoded
        assert "libx264" in cmd
        assert cmd[cmd.index("-preset") + 1] == "slow"
        assert cmd[cmd.index("-crf") + 1] == "18"
        assert cmd[cmd.index("-sws_flags") + 1] == (
            "lanczos+accurate_rnd+full_chroma_int+full_chroma_inp"
        )
        assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"

    def test_hook_only_keeps_fast_encode_path(self):
        cmd = build_layer_command("in.mp4", "out.mp4", hook_png="h.png")
        assert cmd[cmd.index("-preset") + 1] == "fast"
        assert cmd[cmd.index("-crf") + 1] == "23"
        assert "-sws_flags" not in cmd

    def test_hook_coordinates_are_integers(self):
        cmd = build_layer_command("in.mp4", "out.mp4", hook_png="h.png", hook_x=12.7, hook_y=9.2)
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "overlay=12:9" in fc


class TestHookEntrance:
    def test_entrance_adds_fade_and_eased_slide(self):
        cmd = build_layer_command("in.mp4", "out.mp4", hook_png="h.png",
                                  hook_x=90, hook_y=384, hook_entrance=True)
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert fc.startswith("[1:v]format=rgba,fade=t=in:st=0:d=0.35:alpha=1[hk];")
        assert "[0:v][hk]overlay=90:" in fc
        # eased slide-up: starts 60px lower and decelerates into place
        assert "'384+60*pow(1-min(t/0.5,1),2)'" in fc
        # The PNG must be looped into a real stream, otherwise the fade leaves
        # a single transparent frame that overlay repeats forever.
        png_index = cmd.index("h.png")
        assert cmd[png_index - 3:png_index] == ["-loop", "1", "-i"]
        assert ":shortest=1[v1]" in fc

    def test_static_hook_is_not_looped(self):
        cmd = build_layer_command("in.mp4", "out.mp4", hook_png="h.png",
                                  hook_x=90, hook_y=384, hook_entrance=False)
        assert "-loop" not in cmd
        assert "shortest" not in cmd[cmd.index("-filter_complex") + 1]

    def test_entrance_with_subtitles_single_pass(self):
        cmd = build_layer_command("in.mp4", "out.mp4", subtitle_filter="ass='s.ass'",
                                  hook_png="h.png", hook_x=10, hook_y=20, hook_entrance=True)
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert fc == ("[1:v]format=rgba,fade=t=in:st=0:d=0.35:alpha=1[hk];"
                      "[0:v]format=yuv444p,ass='s.ass'[v0];"
                      "[v0][hk]overlay=10:'20+60*pow(1-min(t/0.5,1),2)':shortest=1[v1];"
                      f"[v1]{EVEN_PAD_FILTER},format=yuv420p[vout]")

    def test_no_entrance_keeps_static_overlay(self):
        cmd = build_layer_command("in.mp4", "out.mp4", hook_png="h.png",
                                  hook_x=90, hook_y=384, hook_entrance=False)
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "fade" not in fc
        assert "overlay=90:384" in fc

    def test_entrance_without_hook_is_ignored(self):
        cmd = build_layer_command("in.mp4", "out.mp4", subtitle_filter="ass='s.ass'",
                                  hook_entrance=True)
        assert "-vf" in cmd
        assert "-filter_complex" not in cmd

    def test_every_reencode_pads_odd_dimensions_before_yuv420p(self):
        commands = [
            build_layer_command("in.mp4", "out.mp4", subtitle_filter="ass='s.ass'"),
            build_layer_command("in.mp4", "out.mp4", hook_png="h.png"),
            build_layer_command(
                "in.mp4", "out.mp4", subtitle_filter="ass='s.ass'", hook_png="h.png",
            ),
        ]
        for command in commands:
            filters = command[command.index("-vf") + 1] if "-vf" in command else command[command.index("-filter_complex") + 1]
            assert EVEN_PAD_FILTER in filters


@pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="ffmpeg is required for the pixel-level hook render test",
)
def test_animated_hook_is_visible_in_rendered_pixels(tmp_path):
    """Regression: the entrance fade used to leave the hook fully transparent.
    Render a black clip with a solid white hook and sample real pixels."""
    from PIL import Image
    from subtitles import burn_layers

    video = tmp_path / "in.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=64x64:r=25:d=2",
        "-pix_fmt", "yuv420p", str(video),
    ], check=True)
    hook = tmp_path / "hook.png"
    Image.new("RGBA", (32, 32), (255, 255, 255, 255)).save(hook)

    out = tmp_path / "out.mp4"
    burn_layers(str(video), str(out), hook_png=str(hook), hook_x=16, hook_y=16,
                hook_entrance=True)

    # Sample the frame at t=1.5s, well after the 0.5s entrance animation.
    raw = subprocess.run([
        "ffmpeg", "-loglevel", "error", "-ss", "1.5", "-i", str(out),
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ], check=True, capture_output=True).stdout
    assert len(raw) == 64 * 64
    centre = raw[32 * 64 + 32]
    corner = raw[2 * 64 + 2]
    assert centre > 200, f"hook pixel is {centre}, hook is not visible"
    assert corner < 40, f"background pixel is {corner}"

    # The output must not be cut short by the looped image input.
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(out),
    ], check=True, capture_output=True, text=True).stdout.strip()
    assert abs(float(probe) - 2.0) < 0.2
