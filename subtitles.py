import os
import re
import subprocess
import sys
import math
import colorsys
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


_NEON_SWEEP_PALETTE = (
    "18F8F4",  # CapCut turquoise
    "19FF43",  # laser green
    "FF2038",  # hot red
)


# Measured from the two original 100 x 100 CapCut preview WebPs.  The crucial
# detail is that the wide bloom is a blurred copy of the glyph, not a giant
# outline.  A giant outline produces the compact, synthetic-looking halo the
# old renderer had.  These four layers give us the wide bloom, middle glow,
# close corona and sharp tube, all perfectly centred with zero offset.
# Ordered widest-first, so the index doubles as the stacking order.
_SIGNATURE_GLOW_LAYERS = (
    # border, blur, fill alpha, outline alpha
    ("0.0", "70.0", "00", "FF"),
    ("0.0", "28.0", "00", "FF"),
    ("0.85", "3.5", "00", "00"),
    ("0.15", "0.08", "00", "18"),
)

# Keep the CapCut geometry, but trim only Neon Sweep's three blurred copies by
# 5%.  ASS alpha is inverted, so 0D is the nearest 8-bit value to 95% opacity.
# The sharp tube stays fully opaque and Rainbow Word keeps the measured source
# intensity above.
_NEON_SWEEP_GLOW_LAYERS = (
    ("0.0", "70.0", "0D", "FF"),
    ("0.0", "28.0", "0D", "FF"),
    ("0.85", "3.5", "0D", "0D"),
    ("0.15", "0.08", "00", "18"),
)

# Only the two tight stages are drawn for the words that have NOT been spoken
# yet. Giving them the full four-stage treatment wrapped every caption in a
# wide white fog that swallowed the coloured active word — and the two large
# blurs are also by far the most expensive ones to composite.
_NEON_SWEEP_WHITE_GLOW_INDICES = (2, 3)

# Rainbow Word desaturates the two middle stages slightly; the source preview
# shows a marginally cooler corona around a fully saturated core.
_RAINBOW_GLOW_SATURATIONS = (1.00, 0.98, 0.98, 1.00)

# Horizontal glyph scaling per signature preset (ASS \fscx, in percent).
_NEON_SWEEP_SCALE_X = 104
_RAINBOW_SCALE_X = 88

# Signature script canvas. 162 x 288 is exactly 9:16 and scales cleanly to
# 1080x1920. The side margins are what libass wraps inside, so they are also
# the budget our own line breaking has to respect.
SIGNATURE_PLAY_RES_X = 162
SIGNATURE_PLAY_RES_Y = 288
SIGNATURE_SIDE_MARGIN = 8
SIGNATURE_BOTTOM_MARGIN = 28

# Average advance width of one uppercase glyph, as a fraction of the font size
# (before \fscx). Calibrated against real libass renders at 1080x1920 using
# German uppercase caption lines: the 95th percentile is 0.54 for Arial Black,
# 0.62 for Verdana and 0.42 for Impact. We use the widest of them, because
# underestimating only costs a little density while overestimating would let
# libass re-wrap a line — and Neon Sweep's per-line highlight mask assumes the
# line breaks it emitted are the ones that actually get rendered.
SIGNATURE_GLYPH_WIDTH_RATIO = 0.62

# Never fall below this, even at the largest font size: a one or two character
# budget would shred every word into unreadable fragments.
SIGNATURE_MIN_LINE_CHARS = 5


def _signature_final_fontsize(fontsize):
    """Font size the signature style actually renders at (see the header)."""
    return max(10, int(_clamp_number(fontsize, 10, 200, 24) * 0.9))


def _signature_line_budget(final_fontsize, horizontal_scale=100):
    """How many characters fit on one line at this size, in script units.

    The old renderer used a hard-coded 16 regardless of font size, while the
    header disabled libass' own wrapping — so large sizes ran straight off both
    edges of the frame. Deriving the budget keeps every size inside the canvas.
    """
    usable = SIGNATURE_PLAY_RES_X - 2 * SIGNATURE_SIDE_MARGIN
    per_char = final_fontsize * SIGNATURE_GLYPH_WIDTH_RATIO * (horizontal_scale / 100.0)
    if per_char <= 0:
        return SIGNATURE_MIN_LINE_CHARS
    return max(SIGNATURE_MIN_LINE_CHARS, int(usable / per_char))


def _write_ass_file(output_path, header, events):
    """Write a complete ASS document with Windows-friendly Unicode encoding."""
    if not events:
        return False
    with open(output_path, 'w', encoding='utf-8-sig') as f:
        f.write(header + "\n".join(events) + "\n")
    return True


def _signature_ass_header(font_name, fontsize, alignment):
    """ASS canvas/style shared by the CapCut-grade signature presets.

    PlayResX is explicit because libass otherwise chooses a landscape-oriented
    fallback on some installations. A 162x288 script canvas is exactly 9:16
    and scales cleanly to 1080x1920 as well as other vertical resolutions.
    """
    align_map = {'top': 8, 'middle': 5, 'bottom': 2}
    ass_alignment = align_map.get(str(alignment).lower(), 2)
    safe_font = _sanitize_font_name(font_name)
    final_fontsize = _signature_final_fontsize(fontsize)
    # WrapStyle 0 (smart wrapping) is a safety net, not the primary mechanism:
    # the renderers still emit their own \N breaks so the per-line highlight
    # mask stays deterministic. But should a line still overflow — an unusually
    # wide font, a language with longer words — libass now wraps it instead of
    # running it off both edges of the frame, which is what WrapStyle 2 did.
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {SIGNATURE_PLAY_RES_X}\n"
        f"PlayResY: {SIGNATURE_PLAY_RES_Y}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Signature,{safe_font},{final_fontsize},&H00FFFFFF,&H00FFFFFF,"
        f"&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,1.2,0,"
        f"{ass_alignment},{SIGNATURE_SIDE_MARGIN},{SIGNATURE_SIDE_MARGIN},"
        f"{SIGNATURE_BOTTOM_MARGIN},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _quantized_block_boundaries(block):
    """Return monotonically increasing ASS centisecond boundaries for a block."""
    boundaries = [_ass_centiseconds(block[0]['start'])]
    boundaries.extend(_ass_centiseconds(word['start']) for word in block[1:])
    boundaries.append(_ass_centiseconds(block[-1]['end']))
    for index in range(1, len(boundaries)):
        boundaries[index] = max(boundaries[index], boundaries[index - 1])
    return boundaries


def _word_display_segments(word, max_segment_chars):
    """Split exceptional long words into balanced visual-only line segments."""
    text = str(word)
    max_segment_chars = max(1, int(max_segment_chars))
    if len(text) <= max_segment_chars:
        return [text]
    segment_count = max(2, math.ceil(len(text) / max_segment_chars))
    segment_size = math.ceil(len(text) / segment_count)
    return [text[index:index + segment_size] for index in range(0, len(text), segment_size)]


def _group_display_width(words, max_line_chars):
    """Maximum rendered line length after long-word visual wrapping."""
    line_width = 0
    maximum = 0
    for word_index, word in enumerate(words):
        segments = _word_display_segments(word, max_line_chars)
        line_width += (1 if word_index and line_width else 0) + len(segments[0])
        maximum = max(maximum, line_width)
        for segment in segments[1:]:
            line_width = len(segment)
            maximum = max(maximum, line_width)
    return maximum


def _wrap_line_indices(words, limit, max_line_chars):
    """Greedy line assignment; mirrors _group_display_width's accounting."""
    indices = []
    line = 0
    width = 0
    for word in words:
        segments = _word_display_segments(word, max_line_chars)
        first = len(segments[0])
        gap = 1 if width else 0
        if width and width + gap + first > limit:
            line += 1
            width = first
        else:
            width += gap + first
        indices.append(line)
        # A word long enough to be split itself continues on its last segment.
        for segment in segments[1:]:
            width = len(segment)
    return indices


def _balanced_line_indices(block, max_line_chars):
    """Assign words to stable, balanced caption lines within the budget.

    The signature sweep needs to know exactly which line is currently being
    spoken. Relying on libass auto-wrap would make that renderer-dependent, so
    we choose and emit the line breaks ourselves. ``max_line_chars`` comes from
    _signature_line_budget.

    Two lines cover almost every block, but word boundaries can conspire — a
    35-character block of "kompliziert zusammenarbeit wirklich" has no two-way
    split that fits. Forcing two lines there is what produced captions wider
    than the frame, so we use as few lines as the budget allows and then even
    them out.
    """
    if len(block) <= 1:
        return [0] * len(block)

    words = [str(word['word']) for word in block]
    indices = _wrap_line_indices(words, max_line_chars, max_line_chars)
    line_count = max(indices) + 1
    if line_count <= 1:
        return indices

    # Balance: the tightest limit that still needs the same number of lines
    # turns "full / full / leftover" into evenly filled lines.
    balanced = indices
    for limit in range(max_line_chars - 1, 0, -1):
        candidate = _wrap_line_indices(words, limit, max_line_chars)
        if max(candidate) + 1 != line_count:
            break
        balanced = candidate
    return balanced


def _join_ass_words(block, line_indices, max_line_chars,
                    word_prefixes=None, uppercase=True):
    """Serialize a block with explicit line breaks and optional per-word tags."""
    rendered = []
    previous_line = line_indices[0] if line_indices else 0
    for index, word in enumerate(block):
        if index:
            rendered.append(r"\N" if line_indices[index] != previous_line else " ")
        text = str(word['word'])
        if uppercase:
            text = text.upper()
        text = r"\N".join(
            _escape_ass_text(part)
            for part in _word_display_segments(text, max_line_chars)
        )
        if word_prefixes is not None:
            rendered.append(word_prefixes[index])
        rendered.append(text)
        previous_line = line_indices[index]
    return "".join(rendered)


def _neon_visible_prefix(color, fill_alpha="00", outline_alpha="00"):
    ass_color = _hex_to_ass_inline_color(color)
    return (
        f"{{\\alpha&H00&\\1a&H{fill_alpha}&\\3a&H{outline_alpha}&"
        f"\\c{ass_color}\\3c{ass_color}}}"
    )


def _generate_neon_sweep_ass(blocks, output_path, alignment, fontsize, font_name):
    """Render cumulative, line-aware neon karaoke with CapCut's four-stage glow."""
    header = _signature_ass_header(font_name, fontsize, alignment)
    line_budget = _signature_line_budget(
        _signature_final_fontsize(fontsize), _NEON_SWEEP_SCALE_X,
    )
    events = []

    for block_index, block in enumerate(blocks):
        if not block:
            continue
        boundaries = _quantized_block_boundaries(block)
        line_indices = _balanced_line_indices(block, line_budget)
        color = _NEON_SWEEP_PALETTE[block_index % len(_NEON_SWEEP_PALETTE)]
        for active_index, _word in enumerate(block):
            start_cs = boundaries[active_index]
            end_cs = boundaries[active_index + 1]
            if end_cs <= start_cs:
                continue

            active_line = line_indices[active_index]
            active_mask = [
                line_indices[index] == active_line and index <= active_index
                for index in range(len(block))
            ]
            start = _format_ass_centiseconds(start_cs)
            end = _format_ass_centiseconds(end_cs)

            # CapCut replaces the white words with the active colour.  Rendering
            # the colour over a permanent white copy makes cyan/green/red look
            # washed out, so every interval receives a complementary white mask
            # and a coloured mask.
            #
            # Stacking order matters: the two masks are interleaved so that a
            # wide coloured bloom can never cover the crisp white core of a
            # neighbouring word. Widest blur ends up at the bottom, the two
            # sharp cores on top.
            for glow_index, (border, blur, fill_alpha, outline_alpha) in enumerate(
                _NEON_SWEEP_GLOW_LAYERS
            ):
                white_layer = 2 * glow_index
                color_layer = white_layer + 1

                if glow_index in _NEON_SWEEP_WHITE_GLOW_INDICES:
                    white_prefixes = [
                        "{\\alpha&HFF&}"
                        if is_active else
                        _neon_visible_prefix("FFFFFF", fill_alpha, outline_alpha)
                        for is_active in active_mask
                    ]
                    white_text = _join_ass_words(
                        block, line_indices, line_budget,
                        word_prefixes=white_prefixes, uppercase=True,
                    )
                    events.append(
                        f"Dialogue: {white_layer},{start},{end},Signature,,0,0,0,,"
                        f"{{\\fscx{_NEON_SWEEP_SCALE_X}\\bord{border}\\blur{blur}}}"
                        f"{white_text}"
                    )

                color_prefixes = [
                    _neon_visible_prefix(color, fill_alpha, outline_alpha)
                    if is_active else "{\\alpha&HFF&}"
                    for is_active in active_mask
                ]
                colored_text = _join_ass_words(
                    block, line_indices, line_budget,
                    word_prefixes=color_prefixes, uppercase=True,
                )
                events.append(
                    f"Dialogue: {color_layer},{start},{end},Signature,,0,0,0,,"
                    f"{{\\fscx{_NEON_SWEEP_SCALE_X}\\bord{border}\\blur{blur}}}"
                    f"{colored_text}"
                )

    return _write_ass_file(output_path, header, events)


def _hsv_hex(hue, saturation=1.0, value=1.0):
    red, green, blue = colorsys.hsv_to_rgb((hue % 360.0) / 360.0, saturation, value)
    return f"{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"


def _rainbow_word_text(word, start_hue, end_hue, duration_ms,
                       saturation, fill_alpha, outline_alpha, max_line_chars):
    """Animate CapCut's spectrum plus its white light sweep across a word."""
    segments = _word_display_segments(str(word).upper(), max_line_chars)
    character_count = max(1, sum(len(segment) for segment in segments))

    def character_color(hue, character_fraction, time_fraction):
        # The original has a subtle hue slope across the word and a much
        # brighter, low-saturation band that travels right-to-left.  A narrow
        # Gaussian avoids the hard per-letter colour jumps of a basic rainbow.
        local_hue = hue + (character_fraction - 0.5) * 24.0
        shine_center = 1.05 - 1.10 * time_fraction
        shine_distance = abs(character_fraction - shine_center)
        shine = math.exp(-((shine_distance / 0.22) ** 2))
        local_saturation = saturation * (1.0 - 0.94 * shine)
        return _hex_to_ass_inline_color(
            _hsv_hex(local_hue, saturation=local_saturation)
        )

    # The source preview is 43 frames at 40 ms per frame.  Keep each hue
    # waypoint at most about one source frame apart so RGB interpolation never
    # reveals the six visible colour jumps of the previous implementation.
    transition_steps = max(7, min(16, math.ceil(duration_ms / 45)))
    rendered = [f"{{\\1a&H{fill_alpha}&\\3a&H{outline_alpha}&}}"]
    character_index = 0
    for segment_index, segment in enumerate(segments):
        if segment_index:
            rendered.append(r"\N")
        for character in segment:
            character_fraction = (character_index + 0.5) / character_count
            start_color = character_color(start_hue, character_fraction, 0.0)
            transforms = []
            for step in range(1, transition_steps + 1):
                fraction = step / transition_steps
                segment_start = round(duration_ms * (step - 1) / transition_steps)
                segment_end = round(duration_ms * fraction)
                step_color = character_color(
                    start_hue + (end_hue - start_hue) * fraction,
                    character_fraction,
                    fraction,
                )
                transforms.append(
                    f"\\t({segment_start},{segment_end},"
                    f"\\c{step_color}\\3c{step_color})"
                )
            rendered.append(
                f"{{\\c{start_color}\\3c{start_color}{''.join(transforms)}}}"
                f"{_escape_ass_text(character)}"
            )
            character_index += 1
    return "".join(rendered)


def _generate_rainbow_word_ass(blocks, output_path, alignment, fontsize, font_name):
    """Render one current word with the measured 1.72 s CapCut spectrum flow."""
    header = _signature_ass_header(font_name, fontsize, alignment)
    line_budget = _signature_line_budget(
        _signature_final_fontsize(fontsize), _RAINBOW_SCALE_X,
    )
    events = []

    for block_index, block in enumerate(blocks):
        if not block:
            continue
        boundaries = _quantized_block_boundaries(block)
        block_duration = max(1, boundaries[-1] - boundaries[0])
        hue_origin = (350.0 + block_index * 47.0) % 360.0

        for word_index, word in enumerate(block):
            start_cs = boundaries[word_index]
            end_cs = boundaries[word_index + 1]
            if end_cs <= start_cs:
                continue

            # The WebP uses a one-frame crossfade where the outgoing and next
            # word briefly overlap.  Two centiseconds on either side plus a
            # 40 ms fade recreates that transition without a black flash.
            display_start_cs = max(boundaries[0], start_cs - 2)
            display_end_cs = min(boundaries[-1], end_cs + 2)
            start_fraction = (display_start_cs - boundaries[0]) / block_duration
            end_fraction = (display_end_cs - boundaries[0]) / block_duration
            start_hue = hue_origin + 300.0 * start_fraction
            end_hue = hue_origin + 300.0 * end_fraction
            duration_ms = max(10, (display_end_cs - display_start_cs) * 10)
            fade_ms = min(40, max(10, duration_ms // 4))
            start = _format_ass_centiseconds(display_start_cs)
            end = _format_ass_centiseconds(display_end_cs)

            for layer, ((border, blur, fill_alpha, outline_alpha), saturation) in enumerate(
                zip(_SIGNATURE_GLOW_LAYERS, _RAINBOW_GLOW_SATURATIONS)
            ):
                colored_word = _rainbow_word_text(
                    word['word'], start_hue, end_hue, duration_ms,
                    saturation, fill_alpha, outline_alpha, line_budget,
                )
                events.append(
                    f"Dialogue: {layer},{start},{end},Signature,,0,0,0,,"
                    f"{{\\fscx{_RAINBOW_SCALE_X}\\bord{border}\\blur{blur}"
                    f"\\fad({fade_ms},{fade_ms})}}{colored_word}"
                )

    return _write_ass_file(output_path, header, events)


def generate_ass(transcript, clip_start, clip_end, output_path,
                 max_chars=20, max_duration=2.0, alignment='bottom',
                 fontsize=16, font_name="Verdana", font_color="#FFFFFF",
                 border_color="#000000", border_width=2,
                 highlight_color="#FFD700", bg_color="#000000", bg_opacity=0.0,
                 effect="none", base_opacity=1.0, uppercase=False,
                 preset="custom"):
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
    preset = str(preset or "custom").lower()
    if preset == "neon_sweep":
        # Keep a block down to roughly two budgeted lines. How much actually
        # fits depends on the font size the user picked, so deriving it is the
        # point: the previous fixed 34 characters produced 20-character lines
        # that ran off both edges of a 1080px frame at the preset's own default
        # size. Word boundaries can still force a third line, which the wrapper
        # handles rather than overflowing.
        max_chars = 2 * _signature_line_budget(
            _signature_final_fontsize(fontsize), _NEON_SWEEP_SCALE_X,
        )
        max_duration = max(max_duration, 2.6)
    elif preset == "rainbow_word":
        # Rainbow Word shows a single word, so the budget only has to stop one
        # word from overflowing; long words are split across visual segments.
        max_chars = 2 * _signature_line_budget(
            _signature_final_fontsize(fontsize), _RAINBOW_SCALE_X,
        )
        max_duration = max(max_duration, 2.4)

    blocks = _collect_word_blocks(transcript, clip_start, clip_end, max_chars, max_duration)
    if not blocks:
        return False

    if preset == "neon_sweep":
        return _generate_neon_sweep_ass(
            blocks, output_path, alignment, fontsize, font_name
        )
    if preset == "rainbow_word":
        return _generate_rainbow_word_ass(
            blocks, output_path, alignment, fontsize, font_name
        )

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

