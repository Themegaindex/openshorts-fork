"""Canonical output-format handling and aspect-safe output geometry.

This module intentionally has no OpenCV or application imports so the API,
worker and tests all use exactly the same compatibility rules.
"""

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

    Keeping both axes at or below the source avoids accidental upscaling. It
    also handles portrait-to-square and unusually narrow portrait sources,
    where the old width-only crop could stretch the image vertically.
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
