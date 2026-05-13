"""Color-pick pack-name and pack-price text colors from a Path A reference sample.

Run after dropping assets/reference_sample.png into the repo. Prints hex
values that can be copy-pasted into src/renderer.py as the named constants
PACK_NAME_COLOR and PACK_PRICE_COLOR.

Uses two estimators (mode and per-channel median) over an 11×11 region
around each sample point, then reports both with their saturation values.
Anti-aliased edge pixels typically have lower saturation than the core
text color, so the higher-saturation estimator is usually the right pick.

Also writes assets/extract_colors_debug.png — a copy of the reference
sample with red rectangles around each sample region. If the rectangles
overlap actual glyph strokes, the coordinates are correct. If they sit
in empty/background space, override with --name-xy / --price-xy.

If src/renderer.py already exists, --write inserts/updates the two
constants in-place near the top of that file (idempotent).

Run:
  uv run python -m tools.extract_colors                          # default coords
  uv run python -m tools.extract_colors --name-xy 305,383 \\
                                        --price-xy 298,400        # custom coords
  uv run python -m tools.extract_colors --write                  # also update renderer.py
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from statistics import median

from PIL import Image, ImageDraw

REFERENCE_SAMPLE = Path("assets/reference_sample.png")
RENDERER_PATH = Path("src/renderer.py")
DEBUG_OUTPUT = Path("assets/extract_colors_debug.png")

# Path A's design-unit canvas. Both the pack-name and pack-price text
# positions below come from the original Canva template specification.
DESIGN_WIDTH = 384
DESIGN_HEIGHT = 480

# Approximate centers of the text bodies in design units. The exact pixel
# inside the rendered text may shift across exports; the 11×11 region
# absorbs small position drift.
PACK_NAME_DU = (305, 383)
PACK_PRICE_DU = (298, 400)
SAMPLE_REGION = 11  # odd number so we have a centered pixel


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{c:02X}" for c in rgb)


def saturation(rgb: tuple[int, int, int]) -> float:
    """0..1 saturation. Anti-aliased pixels blend text+bg and saturate less
    than the pure text color — useful tie-breaker when mode and median
    disagree."""
    r, g, b = rgb
    mx, mn = max(r, g, b), min(r, g, b)
    return (mx - mn) / max(mx, 1)


def estimate_colors(
    img: Image.Image, center: tuple[int, int], region: int
) -> tuple[tuple[int, int, int], tuple[int, int, int], list[tuple[int, int, int]]]:
    """Return (mode_rgb, median_rgb, all_pixels) from a centered square region."""
    rgb_img = img.convert("RGB")
    px = rgb_img.load()
    half = region // 2
    cx, cy = center
    w, h = rgb_img.size

    pixels: list[tuple[int, int, int]] = []
    for dy in range(-half, half + 1):
        for dx in range(-half, half + 1):
            x = max(0, min(w - 1, cx + dx))
            y = max(0, min(h - 1, cy + dy))
            pixels.append(px[x, y])

    mode_rgb = Counter(pixels).most_common(1)[0][0]
    r = int(median(p[0] for p in pixels))
    g = int(median(p[1] for p in pixels))
    b = int(median(p[2] for p in pixels))
    return mode_rgb, (r, g, b), pixels


def pick_recommended(
    mode_rgb: tuple[int, int, int], median_rgb: tuple[int, int, int]
) -> tuple[tuple[int, int, int], str]:
    """Pick the higher-saturation of (mode, median). Returns (rgb, reason)."""
    s_mode = saturation(mode_rgb)
    s_med = saturation(median_rgb)
    if s_mode >= s_med:
        return mode_rgb, f"mode (saturation {s_mode:.2f} ≥ median {s_med:.2f})"
    return median_rgb, f"median (saturation {s_med:.2f} > mode {s_mode:.2f})"


def write_constants_into_renderer(
    pack_name_hex: str, pack_price_hex: str, path: Path
) -> bool:
    """Insert or update PACK_NAME_COLOR / PACK_PRICE_COLOR near the top of
    renderer.py. Returns True if the file was modified."""
    if not path.exists():
        return False
    original = path.read_text(encoding="utf-8")
    updated = original

    def upsert(text: str, name: str, value: str) -> str:
        pattern = rf'^{name}\s*=\s*["\'][^"\']*["\'].*$'
        line = f'{name} = "{value}"  # auto-set by tools/extract_colors.py'
        if re.search(pattern, text, flags=re.MULTILINE):
            return re.sub(pattern, line, text, flags=re.MULTILINE)
        # No existing line — insert after the last import block.
        last_import = list(re.finditer(r"^(from\s|import\s).+$", text, flags=re.MULTILINE))
        if last_import:
            insert_at = last_import[-1].end()
            return text[:insert_at] + f"\n\n{line}" + text[insert_at:]
        # Fallback: prepend.
        return line + "\n\n" + text

    updated = upsert(updated, "PACK_NAME_COLOR", pack_name_hex)
    updated = upsert(updated, "PACK_PRICE_COLOR", pack_price_hex)

    if updated == original:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def _parse_xy(raw: str) -> tuple[int, int]:
    """Parse 'x,y' into (int, int). Used by --name-xy / --price-xy."""
    parts = raw.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"Expected 'x,y' but got {raw!r}"
        )
    try:
        return int(parts[0].strip()), int(parts[1].strip())
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"Both x and y must be integers in {raw!r}: {e}"
        )


def _write_debug_png(
    img: Image.Image,
    samples: list[tuple[str, tuple[int, int]]],
    region: int,
    output_path: Path,
) -> None:
    """Draw red rectangles around each sample region on a copy of img and save.

    samples is a list of (label, (cx, cy)) tuples. region is the square side.
    Rectangle outline is 2 px and the label is rendered above each box.
    """
    debug = img.convert("RGB").copy()
    draw = ImageDraw.Draw(debug)
    half = region // 2
    for label, (cx, cy) in samples:
        x0, y0 = cx - half, cy - half
        x1, y1 = cx + half, cy + half
        draw.rectangle([(x0, y0), (x1, y1)], outline=(255, 0, 0), width=2)
        # Label slightly above the box. Use Pillow's default font — debug only.
        draw.text((x0, max(0, y0 - 14)), label, fill=(255, 0, 0))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    debug.save(output_path, format="PNG")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--write",
        action="store_true",
        help="Also write the recommended values into src/renderer.py if it exists.",
    )
    parser.add_argument(
        "--region",
        type=int,
        default=SAMPLE_REGION,
        help=f"Sample region size in pixels (default {SAMPLE_REGION}).",
    )
    parser.add_argument(
        "--name-xy",
        type=_parse_xy,
        default=None,
        metavar="X,Y",
        help=(
            "Override the pack-name sample center, in DESIGN UNITS (384×480 canvas). "
            f"Default {PACK_NAME_DU[0]},{PACK_NAME_DU[1]}."
        ),
    )
    parser.add_argument(
        "--price-xy",
        type=_parse_xy,
        default=None,
        metavar="X,Y",
        help=(
            "Override the pack-price sample center, in DESIGN UNITS (384×480 canvas). "
            f"Default {PACK_PRICE_DU[0]},{PACK_PRICE_DU[1]}."
        ),
    )
    args = parser.parse_args()

    if not REFERENCE_SAMPLE.exists():
        print(f"ERROR: {REFERENCE_SAMPLE} not found. Drop a Path A export there.")
        return 2

    img = Image.open(REFERENCE_SAMPLE)
    w, h = img.size
    print(f"Reference sample: {REFERENCE_SAMPLE} ({w}×{h})")

    # Scale design units to actual pixels using image height. We use height
    # rather than width because Path A's canvas is 4:5 portrait — height is
    # the more reliable axis if the export is tightly cropped to the design.
    scale = h / DESIGN_HEIGHT
    print(f"Design-unit → pixel scale: ×{scale:.3f}")
    print(f"Sample region: {args.region}×{args.region} px per measurement")

    name_du = args.name_xy if args.name_xy is not None else PACK_NAME_DU
    price_du = args.price_xy if args.price_xy is not None else PACK_PRICE_DU
    if args.name_xy is not None:
        print(f"  PACK_NAME_COLOR coord override: design unit {name_du}")
    if args.price_xy is not None:
        print(f"  PACK_PRICE_COLOR coord override: design unit {price_du}")

    results: dict[str, str] = {}
    centers: list[tuple[str, tuple[int, int]]] = []
    for label, du in (("PACK_NAME_COLOR", name_du), ("PACK_PRICE_COLOR", price_du)):
        center = (int(du[0] * scale), int(du[1] * scale))
        centers.append((label, center))
        mode_rgb, median_rgb, _ = estimate_colors(img, center, args.region)
        recommended, why = pick_recommended(mode_rgb, median_rgb)

        print(f"\n{label}  (design unit {du}, pixel {center})")
        print(f"  mode    : {to_hex(mode_rgb):>8}  rgb={mode_rgb}  saturation={saturation(mode_rgb):.2f}")
        print(f"  median  : {to_hex(median_rgb):>8}  rgb={median_rgb}  saturation={saturation(median_rgb):.2f}")
        print(f"  → pick  : {to_hex(recommended)}  ({why})")
        results[label] = to_hex(recommended)

    # Visual sanity check: write a debug PNG showing where we sampled.
    _write_debug_png(img, centers, args.region, DEBUG_OUTPUT)
    print(f"\nDebug overlay: {DEBUG_OUTPUT} (red boxes show sample regions).")
    print(
        "If the boxes don't cover actual glyph strokes, override the coords "
        "with --name-xy / --price-xy and rerun."
    )

    print("\n--- copy/paste into src/renderer.py ---")
    print(f'PACK_NAME_COLOR  = "{results["PACK_NAME_COLOR"]}"')
    print(f'PACK_PRICE_COLOR = "{results["PACK_PRICE_COLOR"]}"')

    if args.write:
        modified = write_constants_into_renderer(
            results["PACK_NAME_COLOR"], results["PACK_PRICE_COLOR"], RENDERER_PATH
        )
        if modified:
            print(f"\nUpdated {RENDERER_PATH} in place.")
        elif RENDERER_PATH.exists():
            print(f"\n{RENDERER_PATH} already had matching values — no change.")
        else:
            print(f"\n{RENDERER_PATH} does not exist yet — skipped --write.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
