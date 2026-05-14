"""One-shot verification of the New Chase visual effects.

Two effects are needed by the New Chase renderer; both are pure Pillow,
no rembg needed:

  1. Outer-glow text — applied to the hit value ("$25,000") so it pops
     off the dark cosmic background, matching the Canva mockup.

  2. Outer-glow on the slab image — a dark halo around the slab so its
     sharp black corners blend into the cosmic background instead of
     looking pasted-in. (Original plan was background removal; pivoted
     to glow on 2026-05-14 because the slabs have inherent black borders
     we want to keep but soften.)

The raw pack image is a transparent PNG in DripShopLive's DB by default
(unlike box_breaks.image, which is the marketing graphic). No effect
needed there — just open + composite.

Outputs to data/new_chase_effects_test/:
  - 01_card_input.png        — slab as-downloaded
  - 02_card_with_glow.png    — slab with black outer glow on dark bg
  - 03_pack_input.png        — raw pack as-downloaded (already transparent)
  - 04_pack_on_dark.png      — pack composited onto dark bg to show transparency
  - 05_value_with_glow.png   — "$25,000" with white outer glow (transparent)
  - 06_value_on_dark.png     — same, composited onto dark bg so glow is visible

Run:
  uv run python -u -m tools.verify_new_chase_effects
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests
from PIL import Image, ImageFont

# Windows console fix — same pattern as other tools.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.image_effects import apply_image_glow, apply_text_glow  # noqa: E402


OUT_DIR = Path("data") / "new_chase_effects_test"

CARD_URL = "https://cdn.dripshop.live/product/2v3sW2EyiwnjVqd3nziG4.webp"
PACK_URL = "https://cdn.dripshop.live/product/6X56YFJb1vviZ6r72-bG6.png"

GLOW_TEXT = "$25,000"
FONT_PATH = Path("assets") / "fonts" / "DMSans-Bold.ttf"
GLOW_FONT_SIZE_PX = 400

# Rough match to the New Chase template's dark cosmic background — used
# to give the transparent glow outputs something visible to render on
# top of in the preview files we save.
DARK_BG = (10, 12, 28, 255)


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    print(f"  saved {dest.name}  ({len(resp.content):,} bytes)")


def _drop_on_dark(image: Image.Image) -> Image.Image:
    """Composite a transparent PIL image onto the dark template bg color."""
    rgba = image.convert("RGBA") if image.mode != "RGBA" else image
    canvas = Image.new("RGBA", rgba.size, DARK_BG)
    return Image.alpha_composite(canvas, rgba)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("\n== 1) Download inputs ==")
    card_in = OUT_DIR / "01_card_input.png"
    pack_in = OUT_DIR / "03_pack_input.png"
    _download(CARD_URL, card_in)
    _download(PACK_URL, pack_in)

    print("\n== 2) Slab — black outer glow ==")
    card_img = Image.open(card_in)
    print(f"  input mode={card_img.mode}  size={card_img.size}")
    card_glowed = apply_image_glow(
        card_img,
        glow_color=(0, 0, 0, 235),
        glow_radius_px=40,
        glow_passes=3,
        padding_px=180,
    )
    # Preview against the template's dark cosmic bg so we can SEE the
    # halo (transparent on transparent is invisible).
    card_preview = _drop_on_dark(card_glowed)
    card_out = OUT_DIR / "02_card_with_glow.png"
    card_preview.save(card_out, format="PNG")
    print(f"  saved {card_out.name}  ({card_preview.size[0]}×{card_preview.size[1]})")

    print("\n== 3) Pack — already transparent, just verify ==")
    pack_img = Image.open(pack_in)
    print(f"  input mode={pack_img.mode}  size={pack_img.size}  "
          f"has alpha={'A' in pack_img.getbands()}")
    pack_preview = _drop_on_dark(pack_img)
    pack_out = OUT_DIR / "04_pack_on_dark.png"
    pack_preview.save(pack_out, format="PNG")
    print(f"  saved {pack_out.name}  (preview on dark bg)")

    print("\n== 4) Hit value — white outer glow ==")
    if not FONT_PATH.exists():
        raise FileNotFoundError(f"Font missing: {FONT_PATH}")
    font = ImageFont.truetype(str(FONT_PATH), GLOW_FONT_SIZE_PX)
    glow_img = apply_text_glow(
        GLOW_TEXT,
        font=font,
        text_color=(255, 255, 255, 255),
        glow_color=(255, 255, 255, 220),
        glow_radius_px=38,
        glow_passes=3,
        padding_px=160,
    )
    glow_path = OUT_DIR / "05_value_with_glow.png"
    glow_img.save(glow_path, format="PNG")
    print(f"  saved {glow_path.name}  ({glow_img.size[0]}×{glow_img.size[1]})")
    glow_on_dark = _drop_on_dark(glow_img)
    on_dark_path = OUT_DIR / "06_value_on_dark.png"
    glow_on_dark.save(on_dark_path, format="PNG")
    print(f"  saved {on_dark_path.name}  (glow visible on dark bg)")

    print(f"\nAll outputs in: {OUT_DIR.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
