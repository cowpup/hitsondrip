"""Render the daily 'Just Pulled' Instagram post locally with Pillow.

Architecture B: assets/background.png contains the static visual identity
(gold outer frame, drip logo, "JUST PULLED" header, "RIP·REVEAL·COLLECT"
footer, gold pill outline, static "PACK PRICE" label). The renderer
composites four variable elements onto it:
  - card image     (content-bbox trim + 10% inner pad, contain-fit)
  - pack thumbnail (contain-fit, no trim — Drip thumbs are clean)
  - pack name text (auto-sized to fit its bbox)
  - pack price text (auto-sized to fit its bbox)

Layout is driven entirely by the four *_BBOX_DU constants, which are
ground-truth coords measured from the v3 Canva reference
(assets/reference_sample.png) in MS Paint and divided by the
design-unit scale (≈9.77 px/DU). The renderer does NO position
guessing — every layout decision derives from these four bboxes.

Standalone test:
  uv run python -m src.renderer \\
    --card-url https://cdn.dripshop.live/product/ZunQqrnKWlDohrUqdetrj.webp \\
    --pack-url https://cdn.dripshop.live/product/_tpbM51S6K806mAwwCV5E.png \\
    --pack-name "GOLD PSA 10 SLAB PACK" --pack-price 100 \\
    --out test_output.png
"""

from __future__ import annotations

import argparse
import io
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

ASSETS_DIR = Path("assets")
BACKGROUND_PATH = ASSETS_DIR / "background.png"
FONT_PATH = ASSETS_DIR / "fonts" / "DMSans-Bold.ttf"

# Design-unit reference. Every coordinate constant below is in these
# units; scale = bg_height / DESIGN_HEIGHT is applied uniformly at
# render time so a single number controls all geometry.
DESIGN_WIDTH = 384
DESIGN_HEIGHT = 480

# Ground-truth element bboxes measured from the v3 Canva reference
# (assets/reference_sample.png) in MS Paint, with pixel coords divided
# by scale ≈9.77 px/DU to land in design-unit space.
#
# Each bbox is (left, top, right, bottom) in DU. Width = right - left,
# height = bottom - top. The renderer composites each variable element
# inside its target bbox:
#   - card + pack image: scaled into the bbox via contain-fit
#   - pack name + pack price: auto-sized text centered in the bbox
#   - market-value line: two-color auto-sized text centered horizontally
#                        on the canvas at MARKET_VALUE_CENTER_DU
#
# FRAME_BBOX_DU describes the gold rectangular OUTLINE in the background;
# CARD_BBOX_DU intentionally OVERLAPS the frame — the slab is sized ~12%
# larger than the original v3 reference (170×283 → 190×316) and is meant
# to sit ON TOP of the frame's edges, not contained inside them.
# card_top=95 puts ~15 DU of overhang above the frame's top (y=118), and
# card_bottom=411 extends 27 DU below the frame's bottom (y=384). That
# overflow at the bottom IS the space where the market-value text sits.
FRAME_BBOX_DU = (110, 118, 272, 384)       # 162 × 266 — gold rectangular outline
CARD_BBOX_DU = (88, 79, 297, 427)          # 209 × 348 — slab +10% from base (locked)
PACK_NAME_BBOX_DU = (300, 384, 370, 388)   #  70 × 4   — thin row above price
PACK_PRICE_BBOX_DU = (299, 400, 333, 414)  #  34 × 14  — dominant text element
PACK_IMAGE_BBOX_DU = (337, 390, 369, 437)  #  32 × 47  — portrait pack thumb

# Card images come in with wildly varying source margins (some are
# tightly cropped to the slab, some have a generous white surround).
# _normalize_card_image() trims to actual content via bbox, then leaves
# 10% margin inside the target box so the visible slab fits in 80% of
# CARD_BBOX_DU. Permanently fixes the Deoxys cut-off issue on tightly-
# cropped sources.
CARD_INNER_PADDING_FRAC = 0.10

# Pillow's ImageFont.truetype(size=N) interprets `size` as em-height
# (the full glyph box, typically 1.4–2× cap-height), while Canva's
# font_size value maps to a tighter typographic metric closer to
# cap-height. A Canva font_size=11 displays at cap-height ~11 px, but
# a Pillow font at size=11 has cap-height only ~7.7 px. We multiply
# design-unit font sizes by TEXT_SIZE_CORRECTION when converting to
# Pillow pixel sizes so the DU values stay in Canva's native frame and
# the conversion math is self-documenting.
TEXT_SIZE_CORRECTION = 0.5

# Auto-sizing search bounds for pack name and pack price. The auto-sizer
# starts at AUTO_FONT_MAX_DU and shrinks one DU at a time until the
# rendered text fits within its target bbox in both width and height.
# 30 DU is a generous upper bound (the tallest bbox is PACK_PRICE at
# 14 DU height, so effective max is ~28 DU after correction); 4 DU is
# the smallest font we'd ever ship. Different pack-name lengths get
# different font sizes automatically — no manual tuning per string.
AUTO_FONT_MAX_DU = 30
AUTO_FONT_MIN_DU = 4

PACK_TEXT_COLOR = "#FFFFFF"  # both pack name and price render in white

# Market-value line — rendered BELOW the card, in the space between the
# card's bottom edge (y=411) and the canvas bottom (y=480). Two-color
# text: prefix in white, dollar amount in yellow. Pillow can't render
# multi-color text in one draw call, so the renderer draws the two parts
# sequentially with anchor "lm" (left-middle) so they share a vertical
# baseline and sit flush with no gap. The trailing space in the prefix
# does the visual word break.
#
# Position is an explicit (x_center, y_center) point — text is anchored
# at this center regardless of length. y=401 is a fixed absolute that
# reads as "inside the frame the card is in" against the rendered
# background, sitting just below the visible slab content. This y stays
# constant if CARD_BBOX_DU is rescaled — the card growing taller doesn't
# move where the MV text needs to land visually, since the placement is
# relative to the background's gold frame outline (frame_bottom=384),
# not to the card bbox.
#
# Max width is the full canvas width minus 12 DU of margin on each side,
# so even at the auto-sizer's max font a long pack name + value won't
# run off the page.
MARKET_VALUE_PREFIX = "Est. Market Value: "
MARKET_VALUE_PREFIX_COLOR = "#FFFFFF"
MARKET_VALUE_PRICE_COLOR = "#FFD500"        # complementary to gold frame
MARKET_VALUE_CENTER_DU = (192, 401)         # fixed absolute; reads as in-frame
MARKET_VALUE_MAX_WIDTH_DU = 360             # canvas 384 minus 12 DU each side
MARKET_VALUE_MAX_FONT_DU = 20               # bumped from 14 for larger text
MARKET_VALUE_MIN_FONT_DU = 4

# Threshold for detecting non-background pixels when the card image has
# no alpha channel. 24 in any RGB channel is forgiving enough for JPEG
# noise but strict enough to catch real card borders.
BG_DIFF_THRESHOLD = 24

REQUEST_TIMEOUT_SECONDS = 30

# Debug overlay — when enabled, draws RED rectangles around each of the
# four element bboxes (labeled "card", "pack-name", "pack-price",
# "pack-image") so placement can be verified visually against the
# rendered background. Off by default; flip via the --debug CLI flag.
DEBUG_OVERLAY = False
DEBUG_STROKE_WIDTH_DU = 1
DEBUG_LABEL_FONT_SIZE_DU = 8


class RenderError(RuntimeError):
    """Raised on missing assets, image download failure, or render failure."""


# --------------------------------------------------------------------------- #
# Image fetching
# --------------------------------------------------------------------------- #


def _download_image(url: str) -> Image.Image:
    """Fetch a URL and decode as a PIL image. Preserves alpha if present."""
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as e:
        raise RenderError(f"Failed to download {url}: {e}") from e
    try:
        img = Image.open(io.BytesIO(response.content))
        img.load()  # force decode now while we still hold the bytes buffer
    except Exception as e:  # noqa: BLE001 — surface raw decode errors
        raise RenderError(f"Failed to decode image from {url}: {e}") from e
    return img


# --------------------------------------------------------------------------- #
# Content-bbox detection (the "slab boundary" fix)
# --------------------------------------------------------------------------- #


def _detect_edge_modal_color(img: Image.Image, edge_strip: int = 2) -> tuple[int, int, int]:
    """Most common RGB color across the 4 edge strips — assumed background."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    strips = [
        rgb.crop((0, 0, w, edge_strip)),
        rgb.crop((0, h - edge_strip, w, h)),
        rgb.crop((0, 0, edge_strip, h)),
        rgb.crop((w - edge_strip, 0, w, h)),
    ]
    counter: Counter[tuple[int, int, int]] = Counter()
    for strip in strips:
        colors = strip.getcolors(maxcolors=2**16)
        if colors is None:
            # Photographic strip with too many unique RGB values —
            # reduce to 8-color palette before counting.
            colors = strip.quantize(colors=8).convert("RGB").getcolors(maxcolors=256)
        for count, color in colors:
            counter[color] += count
    return counter.most_common(1)[0][0]


def _content_bbox(
    img: Image.Image, threshold: int = BG_DIFF_THRESHOLD
) -> Optional[tuple[int, int, int, int]]:
    """Return the bounding box of non-background pixels.

    If the image has an alpha channel, use Pillow's getbbox() directly
    (treats fully-transparent pixels as background). Otherwise compute the
    pixel-wise difference against the modal edge color and threshold.
    """
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        return img.convert("RGBA").getbbox()

    rgb = img.convert("RGB")
    bg = _detect_edge_modal_color(rgb)
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg))
    mask = diff.convert("L").point(lambda p: 255 if p > threshold else 0)
    return mask.getbbox()


def _normalize_card_image(
    img: Image.Image,
    target_size: tuple[int, int],
    inner_padding_frac: float = CARD_INNER_PADDING_FRAC,
) -> Image.Image:
    """Trim to content bbox, then fit into target_size with consistent
    interior padding. Output is RGBA at exactly target_size.
    """
    bbox = _content_bbox(img)
    if bbox:
        img = img.crop(bbox)
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    target_w, target_h = target_size
    inner_w = max(1, int(target_w * (1 - 2 * inner_padding_frac)))
    inner_h = max(1, int(target_h * (1 - 2 * inner_padding_frac)))

    fitted = ImageOps.contain(img, (inner_w, inner_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", target_size, (0, 0, 0, 0))
    offset = ((target_w - fitted.width) // 2, (target_h - fitted.height) // 2)
    canvas.paste(fitted, offset, fitted)
    return canvas


def _fit_centered(
    img: Image.Image, target_size: tuple[int, int]
) -> Image.Image:
    """Fit-to-box preserving aspect ratio, centered on a transparent canvas.

    Used for the pack thumbnail — no content-bbox trim needed since Drip's
    product thumbnails are clean.
    """
    rgba = img.convert("RGBA")
    fitted = ImageOps.contain(rgba, target_size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", target_size, (0, 0, 0, 0))
    offset = ((target_size[0] - fitted.width) // 2,
              (target_size[1] - fitted.height) // 2)
    canvas.paste(fitted, offset, fitted)
    return canvas


# --------------------------------------------------------------------------- #
# Coordinate / size helpers
# --------------------------------------------------------------------------- #


def _scale(value: float, factor: float) -> int:
    return int(round(value * factor))


def _bbox_du_to_px(
    bbox_du: tuple[int, int, int, int], scale: float
) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) in DU → in pixels via uniform scale."""
    return tuple(int(round(v * scale)) for v in bbox_du)


def _bbox_size(bbox_px: tuple[int, int, int, int]) -> tuple[int, int]:
    return (bbox_px[2] - bbox_px[0], bbox_px[3] - bbox_px[1])


def _bbox_center(bbox_px: tuple[int, int, int, int]) -> tuple[int, int]:
    return (
        (bbox_px[0] + bbox_px[2]) // 2,
        (bbox_px[1] + bbox_px[3]) // 2,
    )


def _auto_size_font(
    text: str,
    bbox_du: tuple[int, int, int, int],
    scale: float,
    font_path: Path,
    max_du: int = AUTO_FONT_MAX_DU,
    min_du: int = AUTO_FONT_MIN_DU,
) -> ImageFont.FreeTypeFont:
    """Largest DM Sans Bold font that fits `text` inside `bbox_du`.

    Linear shrink from max_du down to min_du. At each candidate size,
    text is measured with ImageDraw.textbbox(); the first size where
    both rendered width ≤ bbox width and rendered height ≤ bbox height
    is returned. Falls back to min_du if nothing fits.

    Auto-sizing eliminates per-string font tuning — short pack names
    render larger, long ones shrink to fit the same bbox.
    """
    target_w, target_h = _bbox_size(_bbox_du_to_px(bbox_du, scale))
    measure_draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    for fs_du in range(max_du, min_du - 1, -1):
        pillow_size = max(1, int(round(fs_du * TEXT_SIZE_CORRECTION * scale)))
        font = ImageFont.truetype(str(font_path), pillow_size)
        tb = measure_draw.textbbox((0, 0), text, font=font, anchor="lt")
        if (tb[2] - tb[0]) <= target_w and (tb[3] - tb[1]) <= target_h:
            return font
    # Nothing in [min_du, max_du] fit; return font at min_du as a fallback
    # so we still render something visible (will overflow the bbox).
    pillow_size = max(1, int(round(min_du * TEXT_SIZE_CORRECTION * scale)))
    return ImageFont.truetype(str(font_path), pillow_size)


# --------------------------------------------------------------------------- #
# Market-value text (two-color, auto-sized, page-centered)
# --------------------------------------------------------------------------- #


def _render_market_value(
    background: Image.Image,
    hit_value: float,
    scale: float,
    font_path: Path,
) -> None:
    """Render "Est. Market Value: $X" below the card, two-color line.

    Prefix in MARKET_VALUE_PREFIX_COLOR (white), dollar amount in
    MARKET_VALUE_PRICE_COLOR (yellow). Auto-sized to fit
    MARKET_VALUE_MAX_WIDTH_DU; centered at MARKET_VALUE_CENTER_DU on the
    canvas. Position is fully explicit (not derived) so it stays put
    regardless of card / frame geometry changes.

    Pillow has no native multi-color text, so we draw the two parts
    sequentially using anchor "lm" (left-middle): both share a vertical
    midpoint, and the second part starts at the cursor position left
    by the first (via draw.textlength, which is the advance width that
    accounts for kerning and side bearings).
    """
    prefix = MARKET_VALUE_PREFIX
    price = f"${int(round(hit_value))}"
    combined = prefix + price

    # Find largest DM Sans Bold font where combined advance fits.
    max_width_px = MARKET_VALUE_MAX_WIDTH_DU * scale
    measure_draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    font: Optional[ImageFont.FreeTypeFont] = None
    for fs_du in range(
        MARKET_VALUE_MAX_FONT_DU, MARKET_VALUE_MIN_FONT_DU - 1, -1
    ):
        pillow_size = max(1, int(round(fs_du * TEXT_SIZE_CORRECTION * scale)))
        candidate = ImageFont.truetype(str(font_path), pillow_size)
        if measure_draw.textlength(combined, font=candidate) <= max_width_px:
            font = candidate
            break
    if font is None:
        # Nothing fit; render at min size and let it overflow visibly so
        # the failure is obvious in the output rather than silent.
        pillow_size = max(
            1, int(round(MARKET_VALUE_MIN_FONT_DU * TEXT_SIZE_CORRECTION * scale))
        )
        font = ImageFont.truetype(str(font_path), pillow_size)

    draw = ImageDraw.Draw(background)
    combined_advance = draw.textlength(combined, font=font)
    prefix_advance = draw.textlength(prefix, font=font)

    center_x_px = int(round(MARKET_VALUE_CENTER_DU[0] * scale))
    center_y_px = int(round(MARKET_VALUE_CENTER_DU[1] * scale))
    start_x = int(round(center_x_px - combined_advance / 2))

    draw.text(
        (start_x, center_y_px), prefix,
        fill=MARKET_VALUE_PREFIX_COLOR, font=font, anchor="lm",
    )
    draw.text(
        (start_x + int(round(prefix_advance)), center_y_px), price,
        fill=MARKET_VALUE_PRICE_COLOR, font=font, anchor="lm",
    )


# --------------------------------------------------------------------------- #
# Debug overlay
# --------------------------------------------------------------------------- #


def _draw_debug_overlay(
    background: Image.Image,
    scale: float,
    font_path: Path,
) -> None:
    """Draw red rectangles around each of the four element bboxes.

    Verifies that every composited element lands inside its target box.
    Modifies `background` in place; intended to run AFTER all production
    layers so markers sit on top.
    """
    draw = ImageDraw.Draw(background)
    stroke = max(1, _scale(DEBUG_STROKE_WIDTH_DU, scale))
    label_font = ImageFont.truetype(
        str(font_path),
        max(10, _scale(DEBUG_LABEL_FONT_SIZE_DU, scale)),
    )
    # Vertical gap to lift labels above the rectangle they describe so
    # the labels don't sit on top of the rectangle's top edge.
    label_lift = _scale(DEBUG_LABEL_FONT_SIZE_DU, scale) + stroke + 2

    def _rect(
        bbox_du: tuple[int, int, int, int], label: str, color: str = "red",
    ) -> None:
        l, t, r, b = _bbox_du_to_px(bbox_du, scale)
        draw.rectangle([(l, t), (r, b)], outline=color, width=stroke)
        draw.text((l, max(0, t - label_lift)), label, fill=color, font=label_font)

    _rect(CARD_BBOX_DU, "card")
    _rect(PACK_NAME_BBOX_DU, "pack-name")
    _rect(PACK_PRICE_BBOX_DU, "pack-price")
    _rect(PACK_IMAGE_BBOX_DU, "pack-image")
    # Market-value area: full canvas width × the leftover space below
    # the card (between card_bottom and the canvas bottom). The text
    # is centered at MARKET_VALUE_CENTER_DU inside this band. Drawn in
    # magenta to distinguish from the four red element bboxes.
    market_value_bbox_du = (0, CARD_BBOX_DU[3], DESIGN_WIDTH, DESIGN_HEIGHT)
    _rect(market_value_bbox_du, "market-value", color="magenta")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def render_just_pulled(
    card_image_url: str,
    pack_image_url: str,
    pack_name: str,
    pack_price: int,
    hit_value: float,
    output_path: Path,
    *,
    background_path: Path = BACKGROUND_PATH,
    font_path: Path = FONT_PATH,
    debug: bool = DEBUG_OVERLAY,
) -> Path:
    """Render one daily 'Just Pulled' Instagram post and write to disk.

    Layout is driven by the *_BBOX_DU constants (ground-truth coords
    measured from the Canva reference). Pack name + price auto-size to
    fit their bboxes; the market-value line auto-sizes to fit the
    leftover frame width. Long pack names and large prices both get
    smaller fonts automatically — no per-string tuning.

    Args:
        card_image_url: URL of the hit card image.
        pack_image_url: URL of the pack thumbnail (rendered on the pill's right).
        pack_name: Caption-ready pack name (Pokemon-stripped, uppercase).
        pack_price: Whole dollar amount, e.g. 100 renders as "$100".
        hit_value: Sale value in dollars; rendered as "Est. Market Value: $X"
            below the card. Accepts float (rounded to whole dollars).
        output_path: PNG destination. Parents are created if missing.
        background_path: Override for tests; default assets/background.png.
        font_path: Override for tests; default assets/fonts/DMSans-Bold.ttf.
        debug: When True, overlay red bbox rectangles for each variable
            element plus a magenta band for the market-value text area.
            Defaults to DEBUG_OVERLAY.

    Returns the output path. Raises RenderError on any failure.
    """
    if not background_path.exists():
        raise RenderError(f"Background asset not found: {background_path}")
    if not font_path.exists():
        raise RenderError(f"Font asset not found: {font_path}")

    background = Image.open(background_path).convert("RGBA")
    bg_w, bg_h = background.size
    scale = bg_h / DESIGN_HEIGHT

    # Card image — content-bbox trim + 10% inner pad + contain-fit.
    card_src = _download_image(card_image_url)
    card_bbox_px = _bbox_du_to_px(CARD_BBOX_DU, scale)
    card_layer = _normalize_card_image(card_src, _bbox_size(card_bbox_px))
    background.alpha_composite(card_layer, (card_bbox_px[0], card_bbox_px[1]))

    # Pack thumbnail — contain-fit, no trim (Drip thumbs are clean).
    pack_src = _download_image(pack_image_url)
    pack_bbox_px = _bbox_du_to_px(PACK_IMAGE_BBOX_DU, scale)
    pack_layer = _fit_centered(pack_src, _bbox_size(pack_bbox_px))
    background.alpha_composite(pack_layer, (pack_bbox_px[0], pack_bbox_px[1]))

    # Pack name + price — auto-sized to fit their respective bboxes,
    # rendered centered (anchor="mm") inside each bbox.
    draw = ImageDraw.Draw(background)
    price_text = f"${int(pack_price)}"
    name_font = _auto_size_font(pack_name, PACK_NAME_BBOX_DU, scale, font_path)
    price_font = _auto_size_font(price_text, PACK_PRICE_BBOX_DU, scale, font_path)

    name_center = _bbox_center(_bbox_du_to_px(PACK_NAME_BBOX_DU, scale))
    price_center = _bbox_center(_bbox_du_to_px(PACK_PRICE_BBOX_DU, scale))
    draw.text(name_center, pack_name, fill=PACK_TEXT_COLOR, font=name_font, anchor="mm")
    draw.text(
        price_center, price_text, fill=PACK_TEXT_COLOR, font=price_font, anchor="mm",
    )

    # Market-value line — two-color, page-centered, between card and
    # frame bottom. Auto-sized to fit the available width.
    _render_market_value(background, hit_value, scale, font_path)

    # Debug overlay last so markers sit ON TOP of the production layers.
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
        description="Render a single Just Pulled post for visual iteration."
    )
    parser.add_argument("--card-url", required=True, help="URL of the hit card image")
    parser.add_argument("--pack-url", required=True, help="URL of the pack thumbnail")
    parser.add_argument(
        "--pack-name",
        required=True,
        help="Cleaned pack name (Pokemon-stripped, uppercase)",
    )
    parser.add_argument("--pack-price", type=int, required=True, help="Whole dollars")
    parser.add_argument(
        "--hit-value", type=float, required=True,
        help=(
            "Sale value in dollars; rendered below the card as "
            "'Est. Market Value: $X'. Accepts decimals, rounded to whole."
        ),
    )
    parser.add_argument(
        "--out", default=Path("test_output.png"), type=Path,
        help="Output PNG path (default: test_output.png)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Overlay red rectangles around each variable-element bbox "
            "(card, pack-name, pack-price, pack-image) so placement can "
            "be verified against the background. Forces debug regardless "
            "of DEBUG_OVERLAY constant."
        ),
    )
    args = parser.parse_args()

    try:
        out = render_just_pulled(
            args.card_url, args.pack_url, args.pack_name, args.pack_price,
            args.hit_value, args.out,
            debug=args.debug or DEBUG_OVERLAY,
        )
    except RenderError as e:
        print(f"ERROR: {e}")
        return 1
    print(f"Wrote: {out.resolve()} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
