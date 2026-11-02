"""Canonical output-format handling and aspect-safe output geometry.

This module intentionally has no OpenCV or application imports so the API,
worker and tests all use exactly the same compatibility rules.
"""
import os

CANONICAL_OUTPUT_FORMATS = ("vertical", "original", "square")
LEGACY_OUTPUT_FORMAT_ALIASES = {
    "auto": "vertical",
    "horizontal": "original",
}
OUTPUT_FORMAT_CHOICES = CANONICAL_OUTPUT_FORMATS + tuple(LEGACY_OUTPUT_FORMAT_ALIASES)

# libx264 with yuv420p requires both axes to be divisible by two. Appending
# this no-op-for-even-sources pad filter makes every re-encode path safe for
# odd-sized uploads while preserving the complete source image.
EVEN_PAD_FILTER = "pad=ceil(iw/2)*2:ceil(ih/2)*2"

# Vertical and square shorts ship at a fixed platform size: 1080x1920 (9:16)
# and 1080x1080 (1:1). 2160 keeps 2160x3840 inside NVENC's 4096 limit.
DEFAULT_DELIVERY_WIDTH = 1080
MIN_DELIVERY_WIDTH = 360
MAX_DELIVERY_WIDTH = 2160
# Bicubic: OpenCV runs single-threaded here (ultralytics turns its threads
# off) and Lanczos4 measured 22-36 ms per 1080x1920 frame against 3-5 ms.
UPSCALE_INTERPOLATIONS = ("cubic", "lanczos", "linear")
DEFAULT_UPSCALE_INTERPOLATION = "cubic"
_FFMPEG_SCALE_FLAGS = {"cubic": "bicubic", "lanczos": "lanczos", "linear": "bilinear"}


def with_even_padding(filter_chain):
    """Append the even-dimension pad exactly once to an FFmpeg filter chain."""
    filter_chain = str(filter_chain or "").strip()
    if not filter_chain:
        return EVEN_PAD_FILTER
    if EVEN_PAD_FILTER in filter_chain:
        return filter_chain
    return f"{filter_chain},{EVEN_PAD_FILTER}"


def normalize_output_format(value, default="vertical"):
    """Return a canonical format while accepting values from older jobs."""
    normalized = str(value or "").strip().lower()
    normalized = LEGACY_OUTPUT_FORMAT_ALIASES.get(normalized, normalized)
    if normalized in CANONICAL_OUTPUT_FORMATS:
        return normalized
    return default


def output_aspect_ratio(output_format):
    """Target width/height ratio, or ``None`` for source/original geometry."""
    normalized = normalize_output_format(output_format)
    if normalized == "original":
        return None
    return 1.0 if normalized == "square" else 9 / 16


def fit_even_output_dimensions(source_width, source_height, aspect_ratio):
    """Largest even target rectangle that fits inside the source dimensions.

    This is the crop window taken from the source (and the legacy output
    size). It also handles portrait-to-square and unusually narrow portrait
    sources, where the old width-only crop could stretch the image vertically.
    """
    source_width = int(source_width or 0)
    source_height = int(source_height or 0)
    aspect_ratio = float(aspect_ratio or 0)
    if source_width <= 0 or source_height <= 0 or aspect_ratio <= 0:
        raise ValueError("source dimensions and aspect ratio must be positive")

    if source_width / source_height >= aspect_ratio:
        height = source_height
        width = int(round(height * aspect_ratio))
    else:
        width = source_width
        height = int(round(width / aspect_ratio))

    # H.264 requires even dimensions. Round down so the result never exceeds
    # either source axis; a minimum of two pixels keeps tiny test inputs valid.
    width = max(2, min(source_width, width) // 2 * 2)
    height = max(2, min(source_height, height) // 2 * 2)
    return width, height


def delivery_width_from_env(raw=None):
    """SHORTS_DELIVERY_WIDTH as an even width, or 0 for the legacy crop size."""
    if raw is None:
        raw = os.environ.get("SHORTS_DELIVERY_WIDTH", "")
    text = str(raw).strip().lower()
    if text in ("", "default"):
        return DEFAULT_DELIVERY_WIDTH
    if text in ("native", "source", "off", "legacy"):
        return 0
    try:
        value = int(float(text))
    except ValueError:
        return DEFAULT_DELIVERY_WIDTH
    if value <= 0:
        return 0
    return max(MIN_DELIVERY_WIDTH, min(MAX_DELIVERY_WIDTH, value)) // 2 * 2


def upscale_interpolation_from_env(raw=None):
    """SHORTS_UPSCALE_INTERPOLATION; unknown values mean the default."""
    if raw is None:
        raw = os.environ.get("SHORTS_UPSCALE_INTERPOLATION", "")
    text = str(raw).strip().lower()
    return text if text in UPSCALE_INTERPOLATIONS else DEFAULT_UPSCALE_INTERPOLATION


def delivery_dimensions(source_width, source_height, aspect_ratio,
                        delivery_width=DEFAULT_DELIVERY_WIDTH):
    """Final pixel size of a vertical or square short.

    delivery_width > 0 gives exactly (W, even(W / aspect)) for every source:
    a 720p crop is enlarged, a 4K crop shrunk. 0 keeps the legacy crop size.
    """
    if not delivery_width:
        return fit_even_output_dimensions(source_width, source_height, aspect_ratio)
    if int(source_width or 0) <= 0 or int(source_height or 0) <= 0 or float(aspect_ratio or 0) <= 0:
        raise ValueError("source dimensions and aspect ratio must be positive")
    width = max(2, int(delivery_width) // 2 * 2)
    height = max(2, int(round(width / float(aspect_ratio))) // 2 * 2)
    return width, height


def resize_interpolation(source_width, source_height, target_width, target_height,
                         upscale=DEFAULT_UPSCALE_INTERPOLATION):
    """None for the same size, 'shrink' when shrinking on both axes, else the upscaler."""
    if (int(source_width), int(source_height)) == (int(target_width), int(target_height)):
        return None
    if target_width <= source_width and target_height <= source_height:
        return "shrink"
    return upscale if upscale in UPSCALE_INTERPOLATIONS else DEFAULT_UPSCALE_INTERPOLATION


def delivery_scale_filter(source_width, source_height, target_width, target_height,
                          upscale=DEFAULT_UPSCALE_INTERPOLATION):
    """FFmpeg chain that brings an on-aspect source to the delivery size.

    None when it is already there. Cover scale + centre crop: a source inside
    the passthrough tolerance loses a few percent at the edges, never stretched.
    """
    sw, sh = int(source_width or 0), int(source_height or 0)
    tw, th = int(target_width), int(target_height)
    if (sw, sh) == (tw, th):
        return None
    if 0 < tw <= sw and 0 < th <= sh:
        flags = "area"
    else:
        flags = _FFMPEG_SCALE_FLAGS.get(upscale, _FFMPEG_SCALE_FLAGS[DEFAULT_UPSCALE_INTERPOLATION])
    return (f"scale={tw}:{th}:force_original_aspect_ratio=increase:flags={flags},"
            f"crop={tw}:{th},setsar=1")
