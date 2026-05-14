"""Render the daily 'New Chase' Instagram post locally with Pillow.

Parallel to src/renderer.py (which handles the Just Pulled template).
Same 384×480 design-unit coordinate system, same canvas dimensions
(3750×4688 px), same DM Sans Bold font, same auto-sizing pattern.

Differences from Just Pulled:
  - Background: assets/new_chase_background.png (dark cosmic gradient,
    "NEW CHASE" header at top, "INSTANT PACK" footer at bottom)
  - Card slab: NO content-bbox trim. Composited as-is with a black
    outer glow around it (src/image_effects.apply_image_glow) so the
    inherent dark borders of slab photos fade smoothly into the
    cosmic background instead of looking pasted-in.
  - Pack image: already a transparent PNG in DripShopLive's DB
    (the "raw pack image" — products.image of a pack-product row,
    NOT box_breaks.image which is the marketing graphic). Composited
    directly with no effects.
  - Hit value text: rendered VERY large on the left side with a
    white outer glow halo (src/image_effects.apply_text_glow).
    Auto-sized to fit a wide bbox so 4-digit and 7-digit values
    both look proportional.
  - Pack name text: rendered at the bottom above the static
    "INSTANT PACK" footer. Auto-sized to fit.

Initial bbox constants below are best-guess from the Canva template
reference. Iterate via the --debug flag (draws red bbox overlays on
the rendered image) until placement matches the reference.

Standalone test:
  uv run python -u -m src.new_chase_renderer \\
    --card-url https://cdn.dripshop.live/product/2v3sW2EyiwnjVqd3nziG4.webp \\
    --pack-url https://cdn.dripshop.live/product/6X56YFJb1vviZ6r72-bG6.png \\
    --hit-value 25000 \\
    --pack-name "GENGARS GONE WILD" \\
    --out test_new_chase.png --debug
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from typing import Optional

import requests
from PIL import Image, ImageDraw, ImageFont

# Re-use helpers from the Just Pulled renderer where they're truly
# generic (coord conversion, scale math, auto-font sizing, downloads).
from src.renderer import (
    AUTO_FONT_MIN_DU,
    DESIGN_HEIGHT,
    DESIGN_WIDTH,
    FONT_PATH as JP_FONT_PATH,
    RenderError,
    TEXT_SIZE_CORRECTION,
    _auto_size_font,
    _bbox_center,
    _bbox_du_to_px,
    _bbox_size,
    _download_image,
    _fit_centered,
    _scale,
)
from src.image_effects import apply_image_glow, apply_text_glow

ASSETS_DIR = Path("assets")
BACKGROUND_PATH = ASSETS_DIR / "new_chase_background.png"
FONT_PATH = JP_FONT_PATH                                          # DM Sans Bold (default text)
# Hit value uses Black Italic — heaviest weight + italic slant for
# the marketing-headline look the template calls for. v11 swap.
HIT_VALUE_FONT_PATH = ASSETS_DIR / "fonts" / "DMSans-BlackItalic.ttf"


# --------------------------------------------------------------------------- #
# Layout constants — initial best-guess. Iterate via --debug overlay.
# --------------------------------------------------------------------------- #

# Card slab — v12 nudged 10 DU right of v10 per user request.
CARD_BBOX_DU = (40, 64, 275, 402)          # 235 × 338, left edge at x=40

# Pack image — unchanged from v2 ("looks good" per user). Overlaps
# with the slab on the right side; composited BEFORE the slab so the
# slab appears on top. Already RGBA-transparent in DripShopLive's DB.
PACK_IMAGE_BBOX_DU = (206, 105, 380, 405)  # 174 × 300, centered on (293, 255)

# Hit value — rendered VERTICALLY on the left side. v3 butts the
# VISIBLE TEXT up against the canvas left edge (x=0) via a negative-
# offset composite (the glow halo on the left side gets clipped off-
# canvas, which is what we want for an "on the edge" headline).
#
# HIT_VALUE_HORIZONTAL_BBOX_DU is the auto-sizer's text-fit constraint
# BEFORE rotation: width = vertical text height on canvas; height =
# vertical text width on canvas (slightly tighter than v2 because
# we widened the card to the right of it).
HIT_VALUE_HORIZONTAL_BBOX_DU = (0, 0, 305, 70)
# Only the Y matters now — X is overridden in code to butt against
# the canvas left edge. Kept as a tuple for forward compatibility.
HIT_VALUE_VERT_CENTER_DU = (0, 233)             # y = card vertical center

# Pack name text — v6 raised 15 DU back up (v5 went too low).
PACK_NAME_BBOX_DU = (30, 421, 354, 456)    # 324 × 35, centered y=438

# --------------------------------------------------------------------------- #
# Effects parameters
# --------------------------------------------------------------------------- #

# Slab outer glow — black halo extending outward. v3 stronger again
# per user feedback ("increase amount of glow around the card") —
# bumped radius 65→90, passes 4→5, padding grown so the bigger halo
# isn't clipped at the boundary of the glow image.
SLAB_GLOW_COLOR = (0, 0, 0, 255)
SLAB_GLOW_RADIUS_PX = 90
SLAB_GLOW_PASSES = 5
SLAB_GLOW_PADDING_PX = 420     # > 3× (radius × passes-likely-spread)

# Hit value glow — white halo. v11 brightened per user request —
# alpha 245→255 (fully opaque before blur, so the post-blur halo is
# more saturated/visible), passes 5→6 (slightly stronger inner ring).
HIT_VALUE_TEXT_COLOR = (255, 255, 255, 255)
HIT_VALUE_GLOW_COLOR = (255, 255, 255, 255)
HIT_VALUE_GLOW_RADIUS_PX = 80
HIT_VALUE_GLOW_PASSES = 6
HIT_VALUE_GLOW_PADDING_PX = 380
# v9 + stroke for extra weight. We're already on DM Sans Black (the
# heaviest weight in the family); the stroke piles on top to push
# the visual weight further. 10 px stroke ≈ 1 DU thicker glyphs.
HIT_VALUE_STROKE_WIDTH_PX = 10

# Pack name — plain white, no glow, similar font to Just Pulled's pill text.
PACK_NAME_COLOR = "#FFFFFF"

# Auto-font search bounds. v3 pack name 75% larger (22 → 39 max).
HIT_VALUE_MAX_FONT_DU = 130
PACK_NAME_MAX_FONT_DU = 39

# Additional left-shift applied to the rotated hit-value composite.
# v9 = 15 — restored from v7 because the DMSans-Black weight has
# slightly larger left-side bearings than DMSans-Bold; with shift=0
# the visible ink appears ~20 DU from the canvas edge, not butted.
# The 15 DU shift eats into the left bearing so the visible text
# stays flush with x=0. Right-side overlap with the card is fine —
# the card composites AFTER the value and covers it cleanly.
HIT_VALUE_EXTRA_LEFT_SHIFT_DU = 15

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _trim_alpha_padding(img: Image.Image) -> Image.Image:
    """Crop an RGBA image to its non-transparent bounding box.

    Some pack uploads on DripShopLive ship as a padded canvas (e.g.
    1920×1080 stream-frame template) with the actual content in the
    center and full transparency around it. fit-centering such an
    image preserves the empty-canvas aspect ratio, producing a tiny
    visible pack inside our layout bbox. Cropping to the alpha bbox
    first makes the renderer agnostic to source-canvas padding.

    No-op for:
      - non-RGBA images (RGB has no alpha channel to inspect)
      - fully-opaque RGBA (alpha covers the full canvas, no padding)
      - fully-transparent images (defensive — returns input unchanged
        rather than throwing, so a bad upload doesn't crash the run)

    Returns a NEW image when cropping is needed, otherwise returns the
    input unchanged.
    """
    if img.mode != "RGBA":
        return img
    alpha = img.split()[3]
    content_bbox = alpha.getbbox()
    if content_bbox is None:
        return img  # fully transparent, nothing to crop to
    iw, ih = img.size
    if content_bbox == (0, 0, iw, ih):
        return img  # alpha already fills the canvas
    return img.crop(content_bbox)


# --------------------------------------------------------------------------- #
# Debug overlay
# --------------------------------------------------------------------------- #

DEBUG_OVERLAY = False
DEBUG_STROKE_WIDTH_DU = 1
DEBUG_LABEL_FONT_SIZE_DU = 8


def _draw_debug_overlay(
    background: Image.Image,
    scale: float,
    font_path: Path,
) -> None:
    """Outline the four variable-element bboxes in red so we can verify
    placement against the rendered output."""
    draw = ImageDraw.Draw(background)
    stroke = max(1, _scale(DEBUG_STROKE_WIDTH_DU, scale))
    label_font = ImageFont.truetype(
        str(font_path),
        max(10, _scale(DEBUG_LABEL_FONT_SIZE_DU, scale)),
    )
    label_lift = _scale(DEBUG_LABEL_FONT_SIZE_DU, scale) + stroke + 2

    def _rect(bbox_du, label):
        l, t, r, b = _bbox_du_to_px(bbox_du, scale)
        draw.rectangle([(l, t), (r, b)], outline="red", width=stroke)
        draw.text((l, max(0, t - label_lift)), label, fill="red", font=label_font)

    _rect(CARD_BBOX_DU, "card")
    _rect(PACK_IMAGE_BBOX_DU, "pack-image")
    _rect(PACK_NAME_BBOX_DU, "pack-name")
    # hit-value is rotated 90° CCW; the placement is determined by
    # HIT_VALUE_VERT_CENTER_DU + the rendered glow image's dimensions.
    # Draw a marker dot at the center point so we can verify position.
    cx, cy = HIT_VALUE_VERT_CENTER_DU
    cx_px, cy_px = _scale(cx, scale), _scale(cy, scale)
    r = max(4, _scale(2, scale))
    draw.ellipse(
        [(cx_px - r, cy_px - r), (cx_px + r, cy_px + r)],
        fill="red",
    )
    draw.text((cx_px + r + 4, cy_px - r), "hit-value (rotated)", fill="red", font=label_font)


# --------------------------------------------------------------------------- #
# Public render entry point
# --------------------------------------------------------------------------- #


def render_new_chase(
    card_image_url: str,
    pack_image_url: str,
    hit_value: int,
    pack_name: str,
    output_path: Path,
    *,
    background_path: Path = BACKGROUND_PATH,
    font_path: Path = FONT_PATH,
    debug: bool = DEBUG_OVERLAY,
) -> Path:
    """Render one daily 'New Chase' Instagram post and write to disk.

    Composition order (each layer goes on top of the previous):
      1. Background (assets/new_chase_background.png) — static visual identity
      2. Pack image — composited first so the slab can overlap it
      3. Card slab + outer glow — central focal point
      4. Hit value text + outer glow — left-side headline
      5. Pack name text — bottom, above the static INSTANT PACK footer
      6. (debug overlay if requested)

    Args:
        card_image_url: URL of the graded slab photo.
        pack_image_url: URL of the raw pack image (transparent PNG).
        hit_value: Whole dollar amount, e.g. 25000 renders as "$25,000".
        pack_name: Caption-ready pack name (cleaned + uppercased upstream).
        output_path: PNG destination. Parents are created if missing.
        background_path: Override; default assets/new_chase_background.png.
        font_path: Override; default assets/fonts/DMSans-Bold.ttf.
        debug: When True, overlay red bbox rectangles for placement tuning.

    Returns the output path. Raises RenderError on any failure.
    """
    if not background_path.exists():
        raise RenderError(f"Background asset not found: {background_path}")
    if not font_path.exists():
        raise RenderError(f"Font asset not found: {font_path}")

    background = Image.open(background_path).convert("RGBA")
    bg_w, bg_h = background.size
    scale = bg_h / DESIGN_HEIGHT

    # 1) Pack image — composited first (renders BEHIND everything else).
    # Trim transparent padding before fit-centering: pack images
    # uploaded via Drip's admin panel sometimes ship as a 1920×1080
    # stream-frame canvas with the actual pack in the center and the
    # rest transparent (e.g. Collector's Jam Silver — content fills
    # only 24% × 66% of the canvas). Without this trim, fit-centered
    # preserves the empty-canvas aspect ratio and the visible pack
    # lands at ~1/4 the expected size inside our bbox. Discovered
    # 2026-05-14 after the JSONB-lookup test surfaced a tiny pack
    # render. _trim_alpha_padding is a no-op for full-bleed images
    # (the marketplace fallback at 1080×1080 fills 100%, unchanged).
    pack_src = _trim_alpha_padding(_download_image(pack_image_url))
    pack_bbox_px = _bbox_du_to_px(PACK_IMAGE_BBOX_DU, scale)
    pack_size_px = _bbox_size(pack_bbox_px)
    pack_layer = _fit_centered(pack_src, pack_size_px)
    background.alpha_composite(pack_layer, (pack_bbox_px[0], pack_bbox_px[1]))

    # 2) Hit value text — vertical, white outer glow, left edge.
    # Composited BEFORE the card so any overlap is cleanly hidden by
    # the slab on top (per user request 2026-05-14).
    # Steps:
    #   a) Auto-size to fit the HORIZONTAL bbox (which becomes the
    #      vertical extent after rotation).
    #   b) Render with glow on a transparent canvas, using the heavier
    #      DM Sans Black weight (HIT_VALUE_FONT_PATH) instead of Bold.
    #   c) Rotate 90° CCW so it reads bottom-to-top.
    #   d) Composite at the left edge of the canvas.
    value_text = f"${int(hit_value):,}"
    value_font = _auto_size_font(
        value_text, HIT_VALUE_HORIZONTAL_BBOX_DU, scale, HIT_VALUE_FONT_PATH,
        max_du=HIT_VALUE_MAX_FONT_DU, min_du=AUTO_FONT_MIN_DU,
    )
    value_glow_horizontal = apply_text_glow(
        value_text,
        font=value_font,
        text_color=HIT_VALUE_TEXT_COLOR,
        glow_color=HIT_VALUE_GLOW_COLOR,
        glow_radius_px=HIT_VALUE_GLOW_RADIUS_PX,
        glow_passes=HIT_VALUE_GLOW_PASSES,
        padding_px=HIT_VALUE_GLOW_PADDING_PX,
        stroke_width_px=HIT_VALUE_STROKE_WIDTH_PX,
    )
    # Rotate 90° CCW: result reads bottom-to-top when viewed upright.
    # expand=True grows the canvas to fit the rotated image (otherwise
    # corners would be clipped). resample=BICUBIC preserves glow softness.
    value_glow_vertical = value_glow_horizontal.rotate(
        90, expand=True, resample=Image.BICUBIC,
    )

    # Butt the VISIBLE text against the canvas left edge (paste at
    # x = -glow_padding clips the left-side glow halo off-canvas
    # while keeping the visible text touching x=0), then apply any
    # extra user-requested left shift on top of that.
    extra_shift_px = _scale(HIT_VALUE_EXTRA_LEFT_SHIFT_DU, scale)
    value_paste_x = -HIT_VALUE_GLOW_PADDING_PX - extra_shift_px
    center_y_px = _scale(HIT_VALUE_VERT_CENTER_DU[1], scale)
    value_paste_y = center_y_px - value_glow_vertical.height // 2
    background.alpha_composite(value_glow_vertical, (value_paste_x, value_paste_y))

    # 3) Card slab + black outer glow — composited AFTER the hit value
    # so the slab sits in front of the value where they overlap (per
    # user request: card on top).
    card_src = _download_image(card_image_url)
    card_bbox_px = _bbox_du_to_px(CARD_BBOX_DU, scale)
    card_size_px = _bbox_size(card_bbox_px)
    card_layer = _fit_centered(card_src, card_size_px)
    card_glowed = apply_image_glow(
        card_layer,
        glow_color=SLAB_GLOW_COLOR,
        glow_radius_px=SLAB_GLOW_RADIUS_PX,
        glow_passes=SLAB_GLOW_PASSES,
        padding_px=SLAB_GLOW_PADDING_PX,
    )
    # The glowed image is (card_size + 2*padding) in each dim. Offset
    # the composite by -padding so the original card position aligns
    # with CARD_BBOX_DU.
    card_paste_x = card_bbox_px[0] - SLAB_GLOW_PADDING_PX
    card_paste_y = card_bbox_px[1] - SLAB_GLOW_PADDING_PX
    background.alpha_composite(card_glowed, (card_paste_x, card_paste_y))

    # 4) Pack name text — plain white, no glow.
    pack_name_font = _auto_size_font(
        pack_name, PACK_NAME_BBOX_DU, scale, font_path,
        max_du=PACK_NAME_MAX_FONT_DU, min_du=AUTO_FONT_MIN_DU,
    )
    pack_name_center = _bbox_center(_bbox_du_to_px(PACK_NAME_BBOX_DU, scale))
    draw = ImageDraw.Draw(background)
    draw.text(
        pack_name_center, pack_name,
        fill=PACK_NAME_COLOR, font=pack_name_font, anchor="mm",
    )

    # 5) Debug overlay last (on top of everything).
    if debug:
        _draw_debug_overlay(background, scale, font_path)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    background.save(output_path, format="PNG")
    return output_path


# --------------------------------------------------------------------------- #
# CLI for standalone iteration
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a single New Chase post for visual iteration."
    )
    parser.add_argument("--card-url", required=True)
    parser.add_argument("--pack-url", required=True)
    parser.add_argument("--hit-value", type=int, required=True)
    parser.add_argument("--pack-name", required=True)
    parser.add_argument("--out", default=Path("test_new_chase.png"), type=Path)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    try:
        out = render_new_chase(
            args.card_url, args.pack_url, args.hit_value, args.pack_name,
            args.out, debug=args.debug,
        )
    except RenderError as e:
        print(f"ERROR: {e}")
        return 1
    print(f"Wrote: {out.resolve()} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
