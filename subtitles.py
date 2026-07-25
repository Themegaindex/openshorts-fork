import os
import re
import subprocess
import sys
import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from video_formats import EVEN_PAD_FILTER


_STDIO_CONFIGURED = False

# Shared faster-whisper config so both transcription paths (this module and
# main.transcribe_video) behave identically. "small" is meaningfully better at
# German than "base" without being much slower on CPU.
DEFAULT_WHISPER_MODEL = "small"


def get_whisper_config():
    """Return the faster-whisper model config, overridable via env vars."""
    return {
        "model_size": os.environ.get("WHISPER_MODEL", DEFAULT_WHISPER_MODEL),
        "device": os.environ.get("WHISPER_DEVICE", "cpu"),
        "compute_type": os.environ.get("WHISPER_COMPUTE", "int8"),
    }


# Decode params shared by both transcription paths. condition_on_previous_text
# is off to avoid repetition/hallucination loops; vad_filter drops silence.
WHISPER_TRANSCRIBE_PARAMS = {
    "beam_size": 5,
    "vad_filter": True,
    "condition_on_previous_text": False,
    "word_timestamps": True,
}


def merge_continuation_words(words):
    """Merge faster-whisper continuation fragments into their base word.

    faster-whisper marks a word boundary with a LEADING SPACE on each token.
    Compound-word fragments (e.g. "-Kanal.", ".200") arrive WITHOUT a leading
    space and belong to the preceding word. Without merging, "YouTube" and
    "-Kanal." get space-joined into "YouTube -Kanal." or split across subtitle
    blocks. We concatenate such fragments onto the previous word and extend its
    end time. Normal words keep their leading space, so real word boundaries
    (e.g. "ich habe") are never glued together.

    Returns a new list; the input dicts are not mutated.
    """
    merged = []
    for word in words:
        text = word.get("word", "")
        if merged and isinstance(text, str) and text and not text.startswith(" "):
            prev = merged[-1]
            prev["word"] = f"{prev.get('word', '')}{text}"
            if word.get("end") is not None:
                prev["end"] = word["end"]
        else:
            merged.append(dict(word))
    return merged


def _configure_stdio():
    global _STDIO_CONFIGURED
    if _STDIO_CONFIGURED:
        return
    _STDIO_CONFIGURED = True
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if not stream or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _log(message):
    _configure_stdio()
    stream = sys.stdout
    text = str(message)
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        stream.write(safe_text + "\n")
    stream.flush()


def _escape_ffmpeg_filter_value(value):
    """Escape a path/value for use inside a quoted FFmpeg filter argument."""
    return value.replace('\\', '/').replace(':', '\\:').replace("'", "\\'")


def _normalize_subtitle_word(value):
    return " ".join(str(value or "").split())


def transcribe_audio(video_path):
    """
    Transcribe audio from a video file using faster-whisper.
    Returns transcript in the same format as main.py for compatibility.
    """
    from faster_whisper import WhisperModel

    _log(f"🎙️  Transcribing audio from: {video_path}")

    cfg = get_whisper_config()
    model = WhisperModel(cfg["model_size"], device=cfg["device"], compute_type=cfg["compute_type"])

    segments, info = model.transcribe(video_path, **WHISPER_TRANSCRIBE_PARAMS)

    transcript = {
        "segments": [],
        "language": info.language
    }

    for segment in segments:
        seg_data = {
            "start": segment.start,
            "end": segment.end,
            "text": segment.text,
            "words": []
        }
        if segment.words:
            # Keep the leading-space boundary signal, then merge continuation
            # fragments so compound words stay intact (see merge_continuation_words).
            raw_words = [
                {"word": word.word, "start": word.start, "end": word.end}
                for word in segment.words
            ]
            seg_data["words"] = merge_continuation_words(raw_words)
        transcript["segments"].append(seg_data)

    _log(f"✅ Transcription complete. Language: {info.language}")
    return transcript


def generate_srt_from_video(video_path, output_path, max_chars=20, max_duration=2.0,
                            style="classic", **style_opts):
    """
    Transcribe a video and generate a subtitle file directly (SRT, or karaoke
    ASS when style="karaoke"). Used for dubbed videos without a transcript.
    """
    transcript = transcribe_audio(video_path)

    # Get video duration to use as clip_end
    import cv2
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = frame_count / fps if fps else 0
    cap.release()

    if style == "karaoke":
        return generate_ass(transcript, 0, duration, output_path, max_chars, max_duration, **style_opts)
    return generate_srt(transcript, 0, duration, output_path, max_chars, max_duration)


def _collect_word_blocks(transcript, clip_start, clip_end, max_chars=20, max_duration=2.0):
    """
    Flatten transcript words for a clip range and group them into short blocks
    suitable for vertical video. Returns a list of blocks; each block is a list
    of {'word', 'start', 'end'} dicts with times relative to the clip.

    Continuation fragments are merged defensively here too, because transcripts
    from old jobs on disk store unmerged tokens (the leading space is still
    present, so the boundary signal survives).
    """
    try:
        clip_start = float(clip_start)
        clip_end = float(clip_end)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(clip_start) or not math.isfinite(clip_end) or clip_end <= clip_start:
        return []

    flat_words = []
    for segment in transcript.get('segments', []):
        segment_words = segment.get('words', []) if isinstance(segment, dict) else []
        if isinstance(segment_words, list):
            flat_words.extend(segment_words)

    # Old or merged transcript files can contain out-of-order/repeated tokens.
    # Normalize the timeline once before block building so every subtitle path
    # receives sorted, clipped and de-duplicated word data.
    timeline = []
    for order, word_info in enumerate(flat_words):
        if not isinstance(word_info, dict):
            continue
        try:
            start = float(word_info.get('start'))
            end = float(word_info.get('end'))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        if end <= clip_start or start >= clip_end:
            continue
        raw_word = str(word_info.get('word', ''))
        if not _normalize_subtitle_word(raw_word):
            continue
        start = min(clip_end, max(clip_start, start))
        end = min(clip_end, max(start, end))
        timeline.append({"word": raw_word, "start": start, "end": end, "_order": order})

    timeline.sort(key=lambda item: (item['start'], item['end'], item['_order']))
    deduped = []
    for item in timeline:
        key = _normalize_subtitle_word(item['word']).casefold()
        duplicate = any(
            key == previous['_key']
            and abs(item['start'] - previous['start']) <= 0.020
            and abs(item['end'] - previous['end']) <= 0.020
            for previous in deduped[-4:]
        )
        if duplicate:
            continue
        item['_key'] = key
        deduped.append(item)

    flat_words = merge_continuation_words(deduped)

    words = []
    for index, word_info in enumerate(flat_words):
        cleaned_word = _normalize_subtitle_word(word_info.get('word', ''))
        if not cleaned_word:
            continue
        start = max(0.0, word_info['start'] - clip_start)
        end = max(start, word_info['end'] - clip_start)
        # A zero-length final token otherwise disappears entirely. Give it one
        # ASS tick when room exists; adjacent words still share their boundary.
        if end <= start:
            end = min(clip_end - clip_start, start + 0.01)
        words.append({'word': cleaned_word, 'start': start, 'end': end})

    # Whisper occasionally overlaps adjacent word timestamps. Inside one block
    # the next start already wins, but at a line/block boundary the previous
    # word's raw end used to overlap the next Dialogue event. Clamp each end to
    # the following start so the whole clip is globally non-overlapping.
    for index in range(len(words) - 1):
        next_start = words[index + 1]['start']
        words[index]['end'] = max(
            words[index]['start'],
            min(words[index]['end'], next_start),
        )

    blocks = []
    current_block = []
    block_start = None

    for word in words:
        if not current_block:
            current_block = [word]
            block_start = word['start']
            continue

        current_text_len = sum(len(w['word']) + 1 for w in current_block)
        duration = word['end'] - block_start

        if current_text_len + len(word['word']) > max_chars or duration > max_duration:
            blocks.append(current_block)
            current_block = [word]
            block_start = word['start']
        else:
            current_block.append(word)

    if current_block:
        blocks.append(current_block)
    return blocks


def generate_srt(transcript, clip_start, clip_end, output_path, max_chars=20, max_duration=2.0):
    """
    Generates an SRT file from the transcript for a specific time range.
    Groups words into short lines suitable for vertical video.
    """
    blocks = _collect_word_blocks(transcript, clip_start, clip_end, max_chars, max_duration)
    if not blocks:
        return False

    srt_content = ""
    for index, block in enumerate(blocks, 1):
        text = " ".join(w['word'] for w in block).strip()
        srt_content += format_srt_block(index, block[0]['start'], block[-1]['end'], text)

    # Write UTF-8 with BOM so Windows/FFmpeg subtitle readers reliably detect Unicode text.
    with open(output_path, 'w', encoding='utf-8-sig') as f:
        f.write(srt_content)

    return True


def _ass_centiseconds(seconds):
    """Round seconds to an integer ASS tick with correct second/minute carry."""
    try:
        value = Decimal(str(seconds))
    except (InvalidOperation, TypeError, ValueError):
        value = Decimal(0)
    value = max(Decimal(0), value)
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _format_ass_centiseconds(total_centiseconds):
    total_centiseconds = max(0, int(total_centiseconds))
    total_seconds, centis = divmod(total_centiseconds, 100)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _ass_time(seconds):
    """Format seconds as ASS timestamp H:MM:SS.cc (centiseconds)."""
    return _format_ass_centiseconds(_ass_centiseconds(seconds))


def _hex_to_ass_inline_color(hex_color, fallback="FFFFFF"):
    """Convert #RRGGBB to the &HBBGGRR& form used by inline \\c override tags."""
    hex_digits = str(hex_color or "").lstrip('#')
    if not _HEX_COLOR_RE.match(hex_digits):
        hex_digits = fallback
    r = hex_digits[0:2]
    g = hex_digits[2:4]
    b = hex_digits[4:6]
    return f"&H{b}{g}{r}&".upper()


def _escape_ass_text(text):
    """Neutralize characters that would start ASS override blocks."""
    return str(text).replace('\\', '/').replace('{', '(').replace('}', ')')


def _dim_hex_color(hex_color, opacity, fallback="FFFFFF"):
    """Fully-opaque 'dimmed' variant of a color (scaled toward black).

    Dimming via alpha looks muddy in ASS: libass draws the outline as a
    filled shape UNDER the fill, so a semi-transparent white fill blends
    with its own black outline into dark grey. Scaling the RGB instead
    keeps the text crisp on every player."""
    hex_digits = str(hex_color or "").lstrip('#')
    if not _HEX_COLOR_RE.match(hex_digits):
        hex_digits = fallback
    # Gentle curve: even strong dimming stays a readable light silver, matching
    # the airy look of browser-alpha dimming over bright video.
    factor = 0.5 + 0.5 * _clamp_number(opacity, 0.05, 1.0, 1.0)
    r = min(255, round(int(hex_digits[0:2], 16) * factor))
    g = min(255, round(int(hex_digits[2:4], 16) * factor))
    b = min(255, round(int(hex_digits[4:6], 16) * factor))
    return f"{r:02X}{g:02X}{b:02X}"


def generate_ass(transcript, clip_start, clip_end, output_path,
                 max_chars=20, max_duration=2.0, alignment='bottom',
                 fontsize=16, font_name="Verdana", font_color="#FFFFFF",
                 border_color="#000000", border_width=2,
                 highlight_color="#FFD700", bg_color="#000000", bg_opacity=0.0,
                 effect="none", base_opacity=1.0, uppercase=False):
    """
    Generates a karaoke-style ASS file: each block is shown like the SRT path,
    but the currently spoken word is rendered in highlight_color (modern
    TikTok/CapCut caption look). One dialogue event per word, back to back, so
    the highlight moves with the audio without flicker.

    effect: "none" | "glow" (neon shine around the active word) |
            "pop" (active word scales up) | "box" (thick colored outline) |
            "bounce" (subtle spring: overshoot past target, settle back).
    base_opacity: opacity of the non-active words — dimmed base text is the
    modern captioneer look (e.g. 0.4).
    """
    blocks = _collect_word_blocks(transcript, clip_start, clip_end, max_chars, max_duration)
    if not blocks:
        return False

    # Match the SRT burn path: PlayResY 288 keeps font sizes consistent.
    final_fontsize = int(_clamp_number(fontsize, 10, 200, 16) * 0.85)
    if final_fontsize < 10:
        final_fontsize = 10

    align_map = {'top': 8, 'middle': 5, 'bottom': 2}
    ass_alignment = align_map.get(str(alignment).lower(), 2)

    safe_font = _sanitize_font_name(font_name)
    base_opacity = _clamp_number(base_opacity, 0.05, 1.0, 1.0)
    # Dim inactive words via a fully-opaque scaled color (NOT alpha — see
    # _dim_hex_color); the active word overrides the color inline.
    primary_colour = hex_to_ass_color(_dim_hex_color(font_color, base_opacity), 1.0)
    bg_opacity = _clamp_number(bg_opacity, 0.0, 1.0, 0.0)
    border_width = _clamp_number(border_width, 0, 10, 2)

    if bg_opacity > 0:
        border_style = 3
        outline_colour = hex_to_ass_color(bg_color, bg_opacity, fallback="000000")
        outline_width = 1
    else:
        border_style = 1
        outline_colour = hex_to_ass_color(border_color, 1.0, fallback="000000")
        outline_width = max(1, int(border_width))

    back_colour = hex_to_ass_color("#000000", 0.0)
    highlight_inline = _hex_to_ass_inline_color(highlight_color, fallback="FFD700")

    # Inline override tags for the active word; {\r} after it resets to the
    # (dimmed) style so the rest of the block stays untouched. Scale effects
    # are duration-aware: restarting a 180 ms spring on a 20 ms word caused
    # the visible micro-strobe reported for Bounce subtitles.
    def active_prefix_for(duration_ms):
        if effect == "glow":
            glow_bord = max(3, int(outline_width) + 2)
            return (f"{{\\c&HFFFFFF&\\3c{highlight_inline}"
                    f"\\bord{glow_bord}\\blur4}}")
        if effect == "box":
            box_bord = max(4, int(outline_width) + 3)
            return (f"{{\\c&HFFFFFF&\\3c{highlight_inline}"
                    f"\\bord{box_bord}\\blur0}}")
        if effect == "pop" and duration_ms >= 120:
            return (f"{{\\c{highlight_inline}"
                    f"\\fscx75\\fscy75\\t(0,120,\\fscx112\\fscy112)}}")
        if effect == "bounce" and duration_ms >= 180:
            return (f"{{\\c{highlight_inline}"
                    f"\\fscx85\\fscy85"
                    f"\\t(0,90,\\fscx108\\fscy108)"
                    f"\\t(90,180,\\fscx100\\fscy100)}}")
        return f"{{\\c{highlight_inline}}}"

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResY: 288\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{safe_font},{final_fontsize},{primary_colour},{primary_colour},"
        f"{outline_colour},{back_colour},1,0,0,0,100,100,0,0,{border_style},"
        f"{outline_width},0,{ass_alignment},10,10,25,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events = []
    for block in blocks:
        # Quantize each boundary once. Adjacent events therefore share the
        # exact same integer tick and can never overlap after formatting.
        boundaries = [_ass_centiseconds(block[0]['start'])]
        boundaries.extend(_ass_centiseconds(word['start']) for word in block[1:])
        boundaries.append(_ass_centiseconds(block[-1]['end']))
        for boundary_index in range(1, len(boundaries)):
            boundaries[boundary_index] = max(boundaries[boundary_index], boundaries[boundary_index - 1])

        for i, word in enumerate(block):
            start_cs = boundaries[i]
            end_cs = boundaries[i + 1]
            if end_cs <= start_cs:
                continue
            active_prefix = active_prefix_for((end_cs - start_cs) * 10)

            parts = []
            for j, other in enumerate(block):
                text = _escape_ass_text(other['word'])
                if uppercase:
                    text = text.upper()
                if j == i:
                    parts.append(f"{active_prefix}{text}{{\\r}}")
                else:
                    parts.append(text)

            events.append(
                f"Dialogue: 0,{_format_ass_centiseconds(start_cs)},"
                f"{_format_ass_centiseconds(end_cs)},Default,,0,0,0,,{' '.join(parts)}"
            )

    if not events:
        return False

    with open(output_path, 'w', encoding='utf-8-sig') as f:
        f.write(header + "\n".join(events) + "\n")

    return True

def format_srt_block(index, start, end, text):
    def format_time(seconds):
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds - int(seconds)) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
        
    return f"{index}\n{format_time(start)} --> {format_time(end)}\n{text}\n\n"

_HEX_COLOR_RE = re.compile(r'^[0-9A-Fa-f]{6}$')
_FONT_NAME_RE = re.compile(r'[^A-Za-z0-9 _-]')


def hex_to_ass_color(hex_color, opacity=1.0, fallback="FFFFFF"):
    """Convert #RRGGBB to ASS &HAABBGGRR format. opacity: 0.0=transparent, 1.0=opaque.

    Invalid hex (e.g. "#GGGGGG", None, wrong length) falls back to `fallback`
    instead of raising, so a bad color from the client can't 500 the request.
    """
    hex_digits = str(hex_color or "").lstrip('#')
    if not _HEX_COLOR_RE.match(hex_digits):
        hex_digits = fallback
    opacity = _clamp_number(opacity, 0.0, 1.0, 1.0)
    r = int(hex_digits[0:2], 16)
    g = int(hex_digits[2:4], 16)
    b = int(hex_digits[4:6], 16)
    alpha = round((1.0 - opacity) * 255)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _clamp_number(value, lo, hi, default):
    """Coerce value to float and clamp to [lo, hi]; use default if not numeric."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = float(default)
    return max(lo, min(hi, num))


def _sanitize_font_name(name):
    """Strip anything but [A-Za-z0-9 _-] so the font name can't inject extra
    ASS override fields (commas/braces/backslashes) into force_style."""
    cleaned = _FONT_NAME_RE.sub('', str(name or '')).strip()
    return cleaned or "Verdana"


def build_subtitle_filter(srt_path, alignment=2, fontsize=16,
                          font_name="Verdana", font_color="#FFFFFF",
                          border_color="#000000", border_width=2,
                          bg_color="#000000", bg_opacity=0.0):
    """Build the FFmpeg subtitle filter expression for an SRT or ASS file.
    Supports two modes:
    - Outline mode (bg_opacity=0): Text with colored outline/border
    - Box mode (bg_opacity>0): Text with semi-transparent background box
    """
    # Position mapping (ASS v4.00+ numpad alignment: 8 = top center,
    # 6 would be middle right — keep in sync with generate_ass)
    ass_alignment = 2
    align_lower = str(alignment).lower()
    if align_lower == 'top':
        ass_alignment = 8
    elif align_lower == 'middle':
        ass_alignment = 5
    elif align_lower == 'bottom':
        ass_alignment = 2

    # Font size scaling for ASS virtual resolution (PlayResY=288 default)
    # For vertical 1080x1920 video, we need larger text for readability
    final_fontsize = int(_clamp_number(fontsize, 10, 200, 16) * 0.85)
    if final_fontsize < 10:
        final_fontsize = 10

    safe_font_name = _sanitize_font_name(font_name)
    bg_opacity = _clamp_number(bg_opacity, 0.0, 1.0, 0.0)
    border_width = _clamp_number(border_width, 0, 10, 2)

    # Path handling for FFmpeg filter syntax
    safe_srt_path = _escape_ffmpeg_filter_value(srt_path)

    # Convert colors to ASS format and build style
    primary_colour = hex_to_ass_color(font_color, 1.0)

    if bg_opacity > 0:
        # Box mode: opaque background box
        border_style = 3
        outline_colour = hex_to_ass_color(bg_color, bg_opacity, fallback="000000")
        outline_width = 1
    else:
        # Outline mode: text border/outline
        border_style = 1
        outline_colour = hex_to_ass_color(border_color, 1.0, fallback="000000")
        outline_width = max(1, int(border_width))

    back_colour = hex_to_ass_color("#000000", 0.0)

    style_string = (
        f"Alignment={ass_alignment},"
        f"Fontname={safe_font_name},"
        f"Fontsize={final_fontsize},"
        f"PrimaryColour={primary_colour},"
        f"OutlineColour={outline_colour},"
        f"BackColour={back_colour},"
        f"BorderStyle={border_style},"
        f"Outline={outline_width},"
        f"Shadow=0,"
        f"MarginV=25,"
        f"Bold=1"
    )

    if str(srt_path).lower().endswith('.ass'):
        # ASS files (karaoke style) carry their own styles; force_style would
        # override the per-word color tags.
        return f"ass='{safe_srt_path}'"
    return f"subtitles='{safe_srt_path}':charenc=UTF-8:force_style='{style_string}'"


# Hook entrance animation: gentle slide-up with ease-out plus a short
# alpha fade-in. Deliberately subtle — fast/large moves read as cheap.
HOOK_ENTRANCE_SECONDS = 0.5
HOOK_ENTRANCE_FADE_SECONDS = 0.35
HOOK_ENTRANCE_SLIDE_PX = 60


def build_layer_command(video_path, output_path, subtitle_filter=None,
                        hook_png=None, hook_x=0, hook_y=0, hook_entrance=False):
    """Build ONE FFmpeg command that burns subtitles and/or a hook overlay in
    a single encode pass — chaining separate encodes would double the wait
    and stack generation loss. hook_entrance animates the hook in (slide-up
    with ease-out + fade) instead of having it pop into existence."""
    if not subtitle_filter and not hook_png:
        raise ValueError("At least one layer (subtitles or hook) is required")

    cmd = ['ffmpeg', '-y', '-i', video_path]
    if hook_png:
        cmd.extend(['-i', hook_png])

    hook_src = "[1:v]"
    hook_pre = ""
    y_value = str(int(hook_y))
    if hook_png and hook_entrance:
        # Fade the PNG's alpha in, and ease the y position up into place:
        # y(t) = target + slide * (1 - t/D)^2  -> starts slide px lower,
        # decelerates into the final position (ease-out), then stays put.
        hook_pre = (f"[1:v]format=rgba,fade=t=in:st=0"
                    f":d={HOOK_ENTRANCE_FADE_SECONDS}:alpha=1[hk];")
        hook_src = "[hk]"
        y_value = (f"'{int(hook_y)}+{HOOK_ENTRANCE_SLIDE_PX}"
                   f"*pow(1-min(t/{HOOK_ENTRANCE_SECONDS},1),2)'")

    if subtitle_filter and hook_png:
        cmd.extend([
            '-filter_complex',
            f"{hook_pre}[0:v]{subtitle_filter}[v0];"
            f"[v0]{hook_src}overlay={int(hook_x)}:{y_value}[v1];"
            f"[v1]{EVEN_PAD_FILTER}[vout]",
            '-map', '[vout]', '-map', '0:a?',
        ])
    elif hook_png:
        cmd.extend([
            '-filter_complex',
            f"{hook_pre}[0:v]{hook_src}overlay={int(hook_x)}:{y_value}[v1];"
            f"[v1]{EVEN_PAD_FILTER}[vout]",
            '-map', '[vout]', '-map', '0:a?',
        ])
    else:
        cmd.extend(['-vf', f"{subtitle_filter},{EVEN_PAD_FILTER}"])

    cmd.extend([
        '-c:a', 'copy',
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart',
        output_path
    ])
    return cmd


def burn_layers(video_path, output_path, subtitle_path=None, burn_opts=None,
                hook_png=None, hook_x=0, hook_y=0, hook_entrance=False):
    """Render subtitles and/or a hook overlay onto video_path in one pass."""
    subtitle_filter = None
    if subtitle_path:
        subtitle_filter = build_subtitle_filter(subtitle_path, **(burn_opts or {}))

    cmd = build_layer_command(video_path, output_path,
                              subtitle_filter=subtitle_filter,
                              hook_png=hook_png, hook_x=hook_x, hook_y=hook_y,
                              hook_entrance=hook_entrance)

    _log(f"🎬 Burning layers (single pass): {' '.join(cmd)}")
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    if result.returncode != 0:
        stderr_text = result.stderr.decode(errors='replace')
        _log(f"❌ FFmpeg Layer Error: {stderr_text}")
        raise Exception(f"FFmpeg failed: {stderr_text}")

    return True


def burn_subtitles(video_path, srt_path, output_path, alignment=2, fontsize=16,
                   font_name="Verdana", font_color="#FFFFFF",
                   border_color="#000000", border_width=2,
                   bg_color="#000000", bg_opacity=0.0):
    """Burns subtitles into the video using FFmpeg (single subtitle layer)."""
    return burn_layers(
        video_path, output_path,
        subtitle_path=srt_path,
        burn_opts=dict(alignment=alignment, fontsize=fontsize, font_name=font_name,
                       font_color=font_color, border_color=border_color,
                       border_width=border_width, bg_color=bg_color,
                       bg_opacity=bg_opacity),
    )

