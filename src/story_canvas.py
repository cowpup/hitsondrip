"""Wrap a portrait render onto a 9:16 Instagram story canvas.

The renderers emit 4:5 graphics. Instagram stories are 9:16. This module
composites the existing 4:5 render onto a 1080x1920 canvas: a blurred,
cover-fit copy of the render fills the canvas (a soft color glow given the
art's black edges), and the crisp render sits centered on top. Pure PIL,
no I/O — call it on an already-decoded image.
"""

from __future__ import annotations

from PIL import Image, ImageFilter, ImageOps

STORY_SIZE = (1080, 1920)  # Instagram story standard (9:16)
DEFAULT_BLUR_RADIUS = 48


def wrap_9x16(
    src: Image.Image,
    *,
    size: tuple[int, int] = STORY_SIZE,
    blur_radius: int = DEFAULT_BLUR_RADIUS,
) -> Image.Image:
    """Composite `src` onto a 9:16 story canvas.

    Background: `src` cover-fit to `size` (fill + center-crop) then
    Gaussian-blurred by `blur_radius`. Foreground: `src` contain-fit to
    `size` (aspect preserved) composited centered. Returns an RGB image at
    exactly `size`.
    """
    target_w, target_h = size

    background = ImageOps.fit(
        src.convert("RGB"), size, Image.Resampling.LANCZOS
    ).filter(ImageFilter.GaussianBlur(blur_radius))

    foreground = ImageOps.contain(src, size, Image.Resampling.LANCZOS)
    offset = (
        (target_w - foreground.width) // 2,
        (target_h - foreground.height) // 2,
    )

    has_alpha = foreground.mode in ("RGBA", "LA") or (
        foreground.mode == "P" and "transparency" in foreground.info
    )
    if has_alpha:
        fg_rgba = foreground.convert("RGBA")
        background.paste(fg_rgba, offset, fg_rgba)  # 3rd arg = alpha mask
    else:
        background.paste(foreground, offset)

    return background
