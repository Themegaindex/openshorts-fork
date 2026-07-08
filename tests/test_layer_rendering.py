import pytest

from subtitles import build_layer_command, build_subtitle_filter


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


class TestBuildLayerCommand:
    def test_requires_at_least_one_layer(self):
        with pytest.raises(ValueError):
            build_layer_command("in.mp4", "out.mp4")

    def test_subtitles_only_uses_vf(self):
        cmd = build_layer_command("in.mp4", "out.mp4", subtitle_filter="ass='s.ass'")
        assert "-vf" in cmd
        assert "-filter_complex" not in cmd
        assert cmd[cmd.index("-vf") + 1] == "ass='s.ass'"

    def test_hook_only_uses_overlay(self):
        cmd = build_layer_command("in.mp4", "out.mp4", hook_png="h.png", hook_x=90, hook_y=384)
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert fc == "[0:v][1:v]overlay=90:384[vout]"
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
        assert fc == "[0:v]ass='s.ass'[v0];[v0][1:v]overlay=10:20[vout]"
        # exactly one encode: a single ffmpeg invocation with one output
        assert cmd.count("ffmpeg") == 1
        assert cmd[-1] == "out.mp4"

    def test_output_flags(self):
        cmd = build_layer_command("in.mp4", "out.mp4", subtitle_filter="ass='s.ass'")
        assert "+faststart" in cmd
        assert "copy" in cmd  # audio copied, not re-encoded
        assert "libx264" in cmd

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

    def test_entrance_with_subtitles_single_pass(self):
        cmd = build_layer_command("in.mp4", "out.mp4", subtitle_filter="ass='s.ass'",
                                  hook_png="h.png", hook_x=10, hook_y=20, hook_entrance=True)
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert fc == ("[1:v]format=rgba,fade=t=in:st=0:d=0.35:alpha=1[hk];"
                      "[0:v]ass='s.ass'[v0];"
                      "[v0][hk]overlay=10:'20+60*pow(1-min(t/0.5,1),2)'[vout]")

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
