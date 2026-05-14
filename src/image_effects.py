"""Image effects helpers — background removal + outer glow.

These are the two visual treatments that the "New Chase" template uses
and that the existing "Just Pulled" Pillow renderer doesn't have yet.
Kept as a separate module so any renderer can reuse them.

- ``remove_background``: runs the U²-Net model via the ``rembg`` package.
  Returns a transparent-background RGBA PIL Image. CPU inference, no
  network call. ~2-5s per image on a modest machine; ~180MB ONNX model
  is downloaded + cached on first run.

- ``apply_text_glow``: pure Pillow. Draws the text twice — first in
  ``glow_color`` with heavy Gaussian blur (the halo), then in
  ``text_color`` sharp on top. Returns an RGBA PIL Image sized to fit
  the glow's blur radius. Composite onto whatever background you like.

Why a thin module instead of inline code:
- Both effects might get reused if we add more templates later.
- We want a clean swap point if we ever upgrade background removal to
  ``remove.bg`` API: just replace ``remove_background``'s body, keep
  the same signature.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Tuple, Union

from PIL import Image, ImageDraw, ImageFilter, ImageFont


# --------------------------------------------------------------------------- #
# Background removal
# --------------------------------------------------------------------------- #


def remove_background(
    source: Union[bytes, Image.Image, Path, str],
) -> Image.Image:
    """Strip the background from a product image; return an RGBA PIL Image.

    Accepts raw bytes, an open PIL Image, a Path, or a path string.
    Output has the foreground pixels intact and the background fully
    transparent (alpha=0). Use Image.composite or alpha_composite to
    drop it onto whatever new background you want.

    Implementation note: ``rembg.remove`` returns bytes by default; we
    decode them back into a PIL Image so callers don't have to know
    or care about that ergonomic quirk.
    """
    # Import lazily — rembg pulls in onnxruntime, which is heavy and
    # slow to import. Only paying the cost when actually called.
    from rembg import remove as _rembg_remove  # type: ignore

    if isinstance(source, Image.Image):
        buf = io.BytesIO()
        source.save(buf, format="PNG")
        input_bytes = buf.getvalue()
    elif isinstance(source, (Path, str)):
        input_bytes = Path(source).read_bytes()
    elif isinstance(source, (bytes, bytearray)):
        input_bytes = bytes(source)
    else:
        raise TypeError(
            f"remove_background expects bytes / PIL.Image / Path / str, "
            f"got {type(source).__name__}"
        )

    output_bytes = _rembg_remove(input_bytes)
    return Image.open(io.BytesIO(output_bytes)).convert("RGBA")


# --------------------------------------------------------------------------- #
# Outer-glow text
# --------------------------------------------------------------------------- #


def apply_text_glow(
    text: str,
    font: ImageFont.FreeTypeFont,
    *,
    text_color: Tuple[int, int, int, int] = (255, 255, 255, 255),
    glow_color: Tuple[int, int, int, int] = (255, 255, 255, 200),
    glow_radius_px: int = 18,
    glow_passes: int = 2,
    padding_px: int = 60,
) -> Image.Image:
    """Render ``text`` with an outer glow halo, return an RGBA PIL Image.

    The image is sized just large enough for the rendered text plus
    ``padding_px`` on every edge so the blur isn't clipped. Drop it
    onto a parent canvas with ``alpha_composite`` at the position of
    the text's intended center.

    Why two layers + multiple passes:
      Canva-style outer glow is a soft halo that fades out radially.
      A single GaussianBlur looks anemic; chaining two blurs at the
      same radius approximates the smoother falloff that Canva ships
      with. Tune ``glow_passes`` higher for a softer/wider glow.

    Args:
        text: The text to render.
        font: A loaded PIL ImageFont (use ImageFont.truetype).
        text_color: RGBA of the sharp foreground text. Default opaque white.
        glow_color: RGBA of the halo BEFORE blur. Lower alpha → fainter glow.
        glow_radius_px: GaussianBlur radius. ~10 = subtle, ~30 = strong.
        glow_passes: Run the blur this many times for a softer falloff.
        padding_px: Empty border around the text so the glow isn't clipped.
            Should be ≥ ~3× glow_radius_px to capture the full halo.

    Returns:
        RGBA PIL Image. Foreground text is sharp, glow extends outward.
    """
    # 1) Measure the rendered text so we know how big the canvas needs to be.
    #    Use a 1×1 throwaway image to get ImageDraw.textbbox.
    measure = ImageDraw.Draw(Image.new("L", (1, 1)))
    bbox = measure.textbbox((0, 0), text, font=font, anchor="lt")
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    canvas_w = text_w + padding_px * 2
    canvas_h = text_h + padding_px * 2

    # 2) Glow layer: text in glow_color, then blurred (twice for softer
    #    falloff). Built on its own canvas so the blur respects the
    #    padded margins.
    glow_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    glow_draw.text(
        (padding_px - bbox[0], padding_px - bbox[1]),
        text, font=font, fill=glow_color,
    )
    for _ in range(max(1, glow_passes)):
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(glow_radius_px))

    # 3) Sharp text layer drawn on the SAME canvas dimensions so we can
    #    alpha-composite the two.
    text_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    text_draw = ImageDraw.Draw(text_layer)
    text_draw.text(
        (padding_px - bbox[0], padding_px - bbox[1]),
        text, font=font, fill=text_color,
    )

    # 4) Glow underneath, sharp text on top.
    result = Image.alpha_composite(glow_layer, text_layer)
    return result


def apply_image_glow(
    image: Image.Image,
    *,
    glow_color: Tuple[int, int, int, int] = (0, 0, 0, 220),
    glow_radius_px: int = 30,
    glow_passes: int = 2,
    padding_px: int = 100,
) -> Image.Image:
    """Apply an outer glow around an arbitrary image; return an RGBA Image.

    Useful for softening the boundary between a sharp-edged image (like
    a PSA slab photo with hard black corners) and the canvas it's
    composited onto. The glow is the image's silhouette colored solid
    ``glow_color``, blurred outward, and the original image is placed
    sharply on top.

    Args:
        image: PIL Image of any mode. RGBA inputs use the existing alpha
            channel as the silhouette mask. RGB inputs (no transparency)
            use the full image rectangle as the silhouette, so the glow
            radiates from the image's bounding box.
        glow_color: RGBA of the halo BEFORE blur. Default near-opaque
            black for a "drop into dark background" look. Use white/235
            for a "glowing on dark sky" look.
        glow_radius_px: GaussianBlur radius. ~10 = subtle, ~50 = strong.
        glow_passes: Repeat the blur N times for a softer falloff.
        padding_px: Empty border around the image so the glow isn't
            clipped. Should be ≥ ~3× ``glow_radius_px``.

    Returns:
        RGBA PIL Image, sized (image.width + 2*padding, image.height + 2*padding),
        with the glow underneath and the original image on top.
    """
    src = image.convert("RGBA") if image.mode != "RGBA" else image
    canvas_w = src.width + padding_px * 2
    canvas_h = src.height + padding_px * 2

    # Silhouette mask — use alpha if present, else full image rectangle.
    if "A" in src.getbands():
        alpha = src.split()[3]
        if not alpha.getbbox():
            # alpha is all zero (transparent input); nothing to glow.
            alpha = Image.new("L", src.size, 255)
    else:
        alpha = Image.new("L", src.size, 255)

    # 1) Glow layer: solid glow_color in the shape of the silhouette.
    glow_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    glow_block = Image.new("RGBA", src.size, glow_color)
    glow_layer.paste(glow_block, (padding_px, padding_px), alpha)

    # 2) Blur (repeated passes for smoother falloff, matching text glow).
    for _ in range(max(1, glow_passes)):
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(glow_radius_px))

    # 3) Sharp original on top.
    image_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    image_layer.paste(src, (padding_px, padding_px), src)

    return Image.alpha_composite(glow_layer, image_layer)


def text_glow_bounds(
    text: str,
    font: ImageFont.FreeTypeFont,
    padding_px: int = 60,
) -> Tuple[int, int]:
    """Predict the (width, height) of an apply_text_glow result without
    actually rendering it. Useful for layout math (positioning the
    glow image inside a parent canvas)."""
    measure = ImageDraw.Draw(Image.new("L", (1, 1)))
    bbox = measure.textbbox((0, 0), text, font=font, anchor="lt")
    return (
        (bbox[2] - bbox[0]) + padding_px * 2,
        (bbox[3] - bbox[1]) + padding_px * 2,
    )
