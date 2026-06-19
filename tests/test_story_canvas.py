"""Tests for src/story_canvas.py — 9:16 story wrapper."""

from __future__ import annotations

from PIL import Image

from src.story_canvas import wrap_9x16


def _split_source() -> Image.Image:
    """4:5 (100x125) source: left half black, right half white.

    The sharp vertical seam lets us tell the crisp centered foreground
    apart from the blurred background.
    """
    img = Image.new("RGB", (100, 125), (0, 0, 0))
    img.paste(Image.new("RGB", (50, 125), (255, 255, 255)), (50, 0))
    return img


def test_output_dimensions_and_mode():
    out = wrap_9x16(_split_source())
    assert out.size == (1080, 1920)
    assert out.mode == "RGB"


def test_foreground_is_centered_and_full_width():
    # Foreground is contain-fit (width-bound for a 4:5 source in a 9:16 box)
    # => 1080x1350, centered => occupies y in [285, 1635). At the vertical
    # center (y=960), the crisp seam sits at x=540: left black, right white.
    out = wrap_9x16(_split_source())
    left = out.getpixel((270, 960))
    right = out.getpixel((810, 960))
    assert left[0] < 30 and left[1] < 30 and left[2] < 30      # ~black
    assert right[0] > 225 and right[1] > 225 and right[2] > 225  # ~white


def test_background_is_blurred_in_top_margin():
    # The top margin (y < 285) is the blurred background layer. At the
    # blurred seam (x=540) the black/white edge bleeds into gray, so the
    # pixel is neither pure black nor pure white -> proves blur is applied
    # and the background fill exists (not transparent/hard-edged bars).
    out = wrap_9x16(_split_source())
    r, g, b = out.getpixel((540, 100))
    assert 30 < r < 225  # blurred gray, not a crisp 0 or 255 edge
