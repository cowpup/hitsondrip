"""Measure ground-truth bboxes of static landmarks in assets/background.png.

The renderer relies on knowing precisely where the gold rectangular frame
and the gold pill outline sit in the background image. Earlier iterations
guessed these by eyeballing rendered outputs and nudging constants — slow,
error-prone, and unstable across background revisions.

This tool detects three landmarks programmatically:

  FRAME_BBOX  — the gold rectangular outline around the slab. Found as the
                largest gold-colored 8-connected component in the central
                region of the image (excluding the top/bottom headers).
  PILL_BBOX   — the smaller gold pill outline in the bottom-right of the
                frame. Found as the largest gold-colored 8-connected
                component within the bottom-right quadrant.
  PRICE_LABEL_BBOX
              — the static "PACK PRICE" white text baked into the pill.
                Found as the largest near-white 8-connected component
                within PILL_BBOX. Used to compute clearance below the
                price number.

Outputs:
  1. Human-readable pixel + design-unit coords for each landmark.
  2. A Python snippet ready to paste into src/renderer.py:
       FRAME_BBOX_DU = (l, t, r, b)
       PILL_BBOX_DU  = (l, t, r, b)
       PRICE_LABEL_BBOX_DU = (l, t, r, b)
  3. assets/landmarks_debug.png — a copy of background.png with colored
     rectangles drawn around each detected landmark so we can verify
     the detection visually before trusting it.

CLI:
  uv run python -m tools.measure_landmarks            # normal detection
  uv run python -m tools.measure_landmarks --diagnose # HSV histograms only

If a landmark looks wrong in the debug PNG, run with --diagnose to dump
HSV histograms of the frame and pill search regions. The histograms show
where gold-hue pixels actually concentrate in S and V space, so the
threshold constants can be set from real data instead of guessing.
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from pathlib import Path
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFont

# --------------------------------------------------------------------------- #
# Paths + constants
# --------------------------------------------------------------------------- #

ASSETS_DIR = Path("assets")
BACKGROUND_PATH = ASSETS_DIR / "background.png"
FONT_PATH = ASSETS_DIR / "fonts" / "DMSans-Bold.ttf"
DEBUG_PATH = ASSETS_DIR / "landmarks_debug.png"
GOLD_MASK_DEBUG_PATH = ASSETS_DIR / "landmarks_gold_mask.png"
PILL_MASK_DEBUG_PATH = ASSETS_DIR / "pill_mask_debug.png"

# Matches the renderer's design-unit reference — DESIGN_HEIGHT is what
# converts between full-res pixels and the renderer's 384×480 design grid.
DESIGN_HEIGHT = 480

# Downscale factor for connected-component analysis. Full-res background
# is 3750×4688 ≈ 17.6M pixels — pure-Python BFS on that is too slow.
# 5× → 750×938 ≈ 700K pixels stays tractable while preserving ~0.5 DU
# precision in the recovered bboxes (each downscaled cell = 5 px and
# scale ≈ 9.77 px/DU, so each cell ≈ 0.5 DU).
DOWNSCALE = 5

# HSV gold filter — values are in Pillow's 0-255 scale, not degrees.
# Conversion: degrees / 360 * 255. Gold/yellow lives roughly 30°–55°
# in standard HSV, which is 21–39 here.
#
# Iteration history:
#   v1 (S≥80, V≥100):
#       too loose — diffuse glow + sparkles connected everything into
#       one giant 8-connected component.
#   v2 (S≥130, V≥150, 3× erosion):
#       too aggressive — erosion ate the thin frame and pill outlines,
#       only sparkle clusters survived.
#   v3-v4 (S≥110, V≥140, getbbox of region):
#       caught the frame outline cleanly but missed the pill — the
#       --diagnose HSV histogram showed the pill outline has the same
#       S distribution as the frame (~239 vs 224) but a much darker V
#       distribution (median 59 vs 123), so a single V threshold can't
#       catch both without flooding in V<20 dust.
#   v5 (current): separate V thresholds for frame vs pill. Saturation
#       shared at 180 (both have S medians >220, dust mostly below 180).
GOLD_HUE_MIN = 20
GOLD_HUE_MAX = 45
GOLD_SAT_MIN = 180     # ~71% saturation — well below both medians, well above dust
FRAME_VAL_MIN = 80     # frame outline V band; below median 123, above pill's dim band
PILL_VAL_MIN = 45      # pill outline V band; above the V<20 dust spike

# Clamp on the frame bbox's top edge. The frame's actual top sits at
# ≈DU 102 (corner brackets at the top-left and top-right of the card
# area). Scattered sparkle dots above the frame can pull the detected
# y_top up by a dozen DU; clamp so detection can't extend above this.
FRAME_TOP_CLAMP_DU = 102

# White-text detection for the static "PACK PRICE" label. HSV-based
# (high V + low S) is more reliable than plain luminance because it
# excludes high-saturation bright golds that L-mode conversion can
# round up to >220.
WHITE_VAL_MIN = 240
WHITE_SAT_MAX = 40

# Region constraints — fractions of full image dimensions. These do the
# heavy lifting of separating frame from pill from sparkles, so they
# need to be tight enough to exclude obvious noise. Tuned from the
# landmarks_gold_mask.png produced by v3 — see ASCII map in module docstring.
#
# Frame outline lives at approximately (0.13, 0.21) to (0.77, 0.84).
# Heavy sparkle clusters at x<0.13 (left wing), x>0.78 (right wing,
# including a long diagonal streak at y 0.32-0.62), and y>0.85 (bottom
# swoosh). Region pushed just inside the frame edges on left + right
# so sparkle wings are excluded; top inset below "JUST PULLED" text
# at y≈0.13; bottom capped above the pill at y=0.85.
#
# Pill outline lives at approximately (0.85, 0.83) to (0.98, 0.96).
# Region is tight enough to exclude the frame's bottom-right corner
# (frame ends at x≈0.77, so x≥0.83 is safe) and the footer text band
# at y>0.97.
FRAME_REGION_FRAC = (0.13, 0.18, 0.77, 0.85)  # (x0, y0, x1, y1)
PILL_REGION_FRAC = (0.83, 0.81, 1.00, 0.97)

# Diagnostic regions for --diagnose mode. v1 of this used a wide quadrant
# (0.60-1.00, 0.60-1.00) for the pill, which produced histograms dominated
# by surrounding dust + bottom swoosh — V median 59 was misleading. v2
# matches DIAG_PILL_REGION_FRAC to the tight pill detection region so the
# histogram samples ~500K pixels almost all inside the pill, not 2.8M
# pixels mostly outside it. Frame diagnostic stays wide for comparison.
DIAG_FRAME_REGION_FRAC = (0.15, 0.18, 0.85, 0.85)
DIAG_PILL_REGION_FRAC = (0.83, 0.81, 1.00, 0.97)

# Diagnostic gold-hue range — the band of H values we consider
# "definitely gold-ish" when looking at S/V distributions. Wider than
# the GOLD_HUE_MIN/MAX detection range so we don't pre-filter out
# pixels whose true gold identity we're trying to establish. In Pillow's
# 0-255 H scale, [20, 45] corresponds to roughly 28°–63° (yellow-orange
# through pure yellow).
DIAG_GOLD_HUE_LO = 20
DIAG_GOLD_HUE_HI = 45

# Histogram printout: bucket S/V into 10-unit bands and draw a bar chart.
DIAG_BUCKET_SIZE = 10
DIAG_BAR_MAX_CHARS = 50


# --------------------------------------------------------------------------- #
# Mask building (C-fast via Pillow ops)
# --------------------------------------------------------------------------- #


def _gold_mask(rgb: Image.Image, val_min: int) -> Image.Image:
    """Binary mask: 255 where pixel is gold-colored at V ≥ val_min, 0 otherwise.

    H and S thresholds are shared (GOLD_HUE_MIN..GOLD_HUE_MAX, GOLD_SAT_MIN);
    the V threshold is parameterized so we can use the bright FRAME_VAL_MIN
    for the frame outline and the dimmer PILL_VAL_MIN for the pill outline.
    """
    h, s, v = rgb.convert("HSV").split()
    h_mask = h.point(lambda p: 255 if GOLD_HUE_MIN <= p <= GOLD_HUE_MAX else 0)
    s_mask = s.point(lambda p: 255 if p >= GOLD_SAT_MIN else 0)
    v_mask = v.point(lambda p: 255 if p >= val_min else 0)
    # Intersection of three thresholds via per-pixel multiply. multiply
    # normalizes to 255, so (255 × 255 / 255) = 255 and any 0 zeros out.
    return ImageChops.multiply(ImageChops.multiply(h_mask, s_mask), v_mask)


def _white_mask(rgb: Image.Image) -> Image.Image:
    """Binary mask: 255 where pixel is bright + desaturated (white text)."""
    _, s, v = rgb.convert("HSV").split()
    s_mask = s.point(lambda p: 255 if p <= WHITE_SAT_MAX else 0)
    v_mask = v.point(lambda p: 255 if p >= WHITE_VAL_MIN else 0)
    return ImageChops.multiply(s_mask, v_mask)


def _region_clip(mask: Image.Image, region_px: tuple[int, int, int, int]) -> Image.Image:
    """Zero out all mask pixels outside `region_px` (x0, y0, x1, y1)."""
    cleared = Image.new("L", mask.size, 0)
    cleared.paste(mask.crop(region_px), (region_px[0], region_px[1]))
    return cleared


# --------------------------------------------------------------------------- #
# Connected-component BFS on downscaled mask
# --------------------------------------------------------------------------- #


def _largest_component_bbox(
    mask: Image.Image,
) -> Optional[tuple[int, int, int, int]]:
    """Largest 8-connected component's bbox, in the input mask's pixel coords.

    Downscales the mask by DOWNSCALE, runs pure-Python BFS to label
    components, finds the component with the most cells, and scales its
    bbox back to full-res pixel coords. Returns None if no foreground
    pixels exist.
    """
    w, h = mask.size
    small = mask.resize((w // DOWNSCALE, h // DOWNSCALE), Image.NEAREST)
    sw, sh = small.size
    px = small.tobytes()
    visited = bytearray(sw * sh)

    best_bbox: Optional[tuple[int, int, int, int]] = None
    best_size = 0

    # Pre-compute neighbor offsets in flat-index space so the inner loop
    # avoids tuple unpacking + x/y bounds math on every neighbor.
    # Bounds checks still happen via x ∈ [0, sw) and y ∈ [0, sh).
    neighbors_8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1),
                   (0, 1), (1, -1), (1, 0), (1, 1))

    for start_y in range(sh):
        row_off = start_y * sw
        for start_x in range(sw):
            i = row_off + start_x
            if visited[i] or px[i] == 0:
                continue
            q = deque()
            q.append((start_x, start_y))
            visited[i] = 1
            count = 0
            minx = maxx = start_x
            miny = maxy = start_y
            while q:
                x, y = q.popleft()
                count += 1
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
                for dx, dy in neighbors_8:
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < sw and 0 <= ny < sh:
                        ni = ny * sw + nx
                        if not visited[ni] and px[ni] != 0:
                            visited[ni] = 1
                            q.append((nx, ny))
            if count > best_size:
                best_size = count
                best_bbox = (minx, miny, maxx, maxy)

    if best_bbox is None:
        return None
    # (maxx, maxy) is inclusive in the downscaled mask. Convert to a
    # half-open full-res bbox by adding 1 to the max coords before scaling.
    return (
        best_bbox[0] * DOWNSCALE,
        best_bbox[1] * DOWNSCALE,
        (best_bbox[2] + 1) * DOWNSCALE,
        (best_bbox[3] + 1) * DOWNSCALE,
    )


# --------------------------------------------------------------------------- #
# Coord conversion + public API
# --------------------------------------------------------------------------- #


def _px_to_du(
    bbox_px: tuple[int, int, int, int], scale: float
) -> tuple[int, int, int, int]:
    return tuple(int(round(v / scale)) for v in bbox_px)


def _region_px(
    image_size: tuple[int, int], frac: tuple[float, float, float, float]
) -> tuple[int, int, int, int]:
    w, h = image_size
    return (int(w * frac[0]), int(h * frac[1]), int(w * frac[2]), int(h * frac[3]))


def measure(background_path: Path = BACKGROUND_PATH) -> dict:
    """Detect FRAME, PILL, PRICE_LABEL bboxes and return both px + DU coords.

    Two-threshold pipeline: build a FRAME gold mask (V ≥ FRAME_VAL_MIN, the
    brighter outline) and a separate PILL gold mask (V ≥ PILL_VAL_MIN, dimmer
    outline). Each is clipped to its own tight region and reduced to a bbox
    via Image.getbbox(). The shared region clamps + per-landmark V thresholds
    isolate frame from pill from sparkles without needing connected-component
    labeling. Frame bbox then gets a y_top clamp (FRAME_TOP_CLAMP_DU) so
    scattered sparkle dots above the frame can't pull its top edge up.
    """
    if not background_path.exists():
        raise FileNotFoundError(f"Background not found: {background_path}")

    bg = Image.open(background_path).convert("RGB")
    w, h = bg.size
    scale = h / DESIGN_HEIGHT

    frame_mask = _gold_mask(bg, FRAME_VAL_MIN)
    pill_mask = _gold_mask(bg, PILL_VAL_MIN)
    white = _white_mask(bg)

    frame_region = _region_px(bg.size, FRAME_REGION_FRAC)
    pill_region = _region_px(bg.size, PILL_REGION_FRAC)

    # bbox-of-mask-in-region. _region_clip zeros out pixels outside the
    # region, so getbbox() returns the bbox of in-region gold pixels.
    frame_bbox_px = _region_clip(frame_mask, frame_region).getbbox()
    pill_bbox_px = _region_clip(pill_mask, pill_region).getbbox()

    # Frame top-clamp: if detected y_top sits above the known frame top
    # (DU 102), it's sparkle noise — push y_top down to the clamp.
    if frame_bbox_px is not None:
        clamp_px = int(round(FRAME_TOP_CLAMP_DU * scale))
        if frame_bbox_px[1] < clamp_px:
            frame_bbox_px = (
                frame_bbox_px[0], clamp_px, frame_bbox_px[2], frame_bbox_px[3]
            )

    if pill_bbox_px is None:
        # Without the pill bbox we have nowhere to look for the price label.
        price_label_bbox_px = None
    else:
        # Static text: letters aren't 8-connected to each other, so any
        # CC-based detection would only catch one letter. Use the union
        # bbox of all white pixels within the detected pill bbox — works
        # because the pill body is gold and the only white element inside
        # it is the static "PACK PRICE" label.
        price_label_bbox_px = _region_clip(white, pill_bbox_px).getbbox()

    return {
        "image_size_px": (w, h),
        "scale_px_per_du": scale,
        "frame_region_px": frame_region,
        "pill_region_px": pill_region,
        # Carry both masks through so the CLI can save them for inspection.
        # FRAME mask (V≥80) goes to landmarks_gold_mask.png — the global
        # gold structure view. PILL mask (V≥45) goes to pill_mask_debug.png
        # clipped to the pill region — the critical verification image.
        "frame_mask": frame_mask,
        "pill_mask": pill_mask,
        "frame_bbox_px": frame_bbox_px,
        "frame_bbox_du": _px_to_du(frame_bbox_px, scale) if frame_bbox_px else None,
        "pill_bbox_px": pill_bbox_px,
        "pill_bbox_du": _px_to_du(pill_bbox_px, scale) if pill_bbox_px else None,
        "price_label_bbox_px": price_label_bbox_px,
        "price_label_bbox_du": (
            _px_to_du(price_label_bbox_px, scale) if price_label_bbox_px else None
        ),
    }


# --------------------------------------------------------------------------- #
# Debug overlay
# --------------------------------------------------------------------------- #


def save_debug_overlay(
    background_path: Path, result: dict, output_path: Path = DEBUG_PATH
) -> Path:
    """Draw colored rectangles around each detected landmark and save.

    Also draws dashed-style outlines for the SEARCH REGIONS so we can
    see at a glance whether the region itself is positioned correctly
    (a wrong region produces a wrong bbox).
    """
    bg = Image.open(background_path).convert("RGBA")
    draw = ImageDraw.Draw(bg)
    try:
        font = ImageFont.truetype(str(FONT_PATH), 60)
    except OSError:
        font = ImageFont.load_default()

    def _rect(
        bbox: Optional[tuple[int, int, int, int]],
        color: str,
        label: str,
        width: int = 8,
    ) -> None:
        if not bbox:
            return
        draw.rectangle(bbox, outline=color, width=width)
        draw.text(
            (bbox[0], max(0, bbox[1] - 70)),
            label,
            fill=color,
            font=font,
        )

    # Search regions first (thinner, dimmer) so the detected bboxes
    # drawn after them sit on top.
    _rect(result["frame_region_px"], "#226633", "frame search", width=3)
    _rect(result["pill_region_px"], "#662244", "pill search", width=3)

    _rect(result["frame_bbox_px"], "#00FF66", "FRAME")
    _rect(result["pill_bbox_px"], "#FF33AA", "PILL")
    _rect(result["price_label_bbox_px"], "#33CCFF", "PRICE LABEL")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    bg.save(output_path, format="PNG")
    return output_path


def save_gold_mask_debug(
    result: dict, output_path: Path = GOLD_MASK_DEBUG_PATH
) -> Path:
    """Save the FRAME-threshold gold mask (V ≥ FRAME_VAL_MIN) full-image.

    This is the "global view" of what the bright gold catches — frame
    outline + the "JUST PULLED" header + "drip" logo + the brightest
    sparkles. The pill outline mostly does NOT appear here because it's
    too dim at FRAME_VAL_MIN=80; see pill_mask_debug.png for that.
    """
    mask: Image.Image = result["frame_mask"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mask.save(output_path, format="PNG")
    return output_path


def save_pill_mask_debug(
    result: dict, output_path: Path = PILL_MASK_DEBUG_PATH
) -> Path:
    """Save the PILL-threshold mask clipped to the pill search region.

    This is the critical verification image: the pill outline (dim gold,
    only caught by the looser PILL_VAL_MIN=45 threshold) should appear
    as a clean rounded-rectangle outline; dust noise should be sparse.
    If the rounded-rect is visible and dominant in this image, the
    detected pill bbox is trustworthy. If instead we see scattered
    speckles with no rect, the V threshold is still too loose / the
    pill outline doesn't even meet PILL_VAL_MIN=45 and we'd need to
    escalate to row/column-sum analysis or manual measurement.
    """
    pill_mask: Image.Image = result["pill_mask"]
    pill_region_px: tuple[int, int, int, int] = result["pill_region_px"]
    clipped = _region_clip(pill_mask, pill_region_px)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    clipped.save(output_path, format="PNG")
    return output_path


# --------------------------------------------------------------------------- #
# HSV histogram diagnostic (--diagnose)
# --------------------------------------------------------------------------- #


def _channel_stats(
    counts: list[int], total: int
) -> tuple[int, int, int]:
    """min, max, median from a 256-bucket count array. O(256), independent
    of `total`, so this scales fine to multi-million-pixel regions."""
    if total == 0:
        return 0, 0, 0
    mn = next((i for i, c in enumerate(counts) if c > 0), 0)
    mx = next((i for i in range(255, -1, -1) if counts[i] > 0), 0)
    half = total // 2
    cum = 0
    med = 0
    for i, c in enumerate(counts):
        cum += c
        if cum >= half:
            med = i
            break
    return mn, mx, med


def _print_histogram(counts: list[int], total: int) -> None:
    """Print a 10-unit-bucketed bar chart of `counts` (length 256)."""
    if total == 0:
        print("    (no data)")
        return
    bucket_size = DIAG_BUCKET_SIZE
    buckets: list[tuple[int, int, int]] = []
    for start in range(0, 256, bucket_size):
        end = min(255, start + bucket_size - 1)
        cnt = sum(counts[start:end + 1])
        buckets.append((start, end, cnt))
    max_cnt = max(c for _, _, c in buckets) or 1
    for start, end, cnt in buckets:
        bar_len = int((cnt / max_cnt) * DIAG_BAR_MAX_CHARS)
        # ASCII '#' instead of '█' — Windows console cp1252 can't encode
        # U+2588, and we want this to work uniformly on every shell.
        bar = "#" * bar_len
        pct = (cnt / total) * 100
        print(f"    {start:3d}-{end:3d} {bar:<{DIAG_BAR_MAX_CHARS}} {cnt:>9,}  ({pct:5.2f}%)")


def _diagnose_region(
    rgb: Image.Image,
    region_frac: tuple[float, float, float, float],
    label: str,
) -> None:
    """Dump HSV stats + S/V histograms for `region_frac` of `rgb`.

    Prints two stats blocks for each region:
      1. min/max/median H/S/V across ALL pixels in the region (baseline,
         dominated by the dark background — useful to see how dark the
         non-gold majority actually is).
      2. min/max/median S/V across the SUBSET of pixels whose H falls in
         the diagnostic gold-hue band [DIAG_GOLD_HUE_LO, DIAG_GOLD_HUE_HI]
         — this is where the pill outline / frame outline pixels live, and
         where we need to tune detection thresholds.

    Then prints S and V histograms for the gold-hue subset only, so we
    can see where the actual gold pixels cluster vs background dust.
    """
    w, h = rgb.size
    x0 = int(w * region_frac[0])
    y0 = int(h * region_frac[1])
    x1 = int(w * region_frac[2])
    y1 = int(h * region_frac[3])

    region_rgb = rgb.crop((x0, y0, x1, y1))
    hsv = region_rgb.convert("HSV")
    h_bytes = hsv.getchannel("H").tobytes()
    s_bytes = hsv.getchannel("S").tobytes()
    v_bytes = hsv.getchannel("V").tobytes()
    total = len(h_bytes)

    # 256-bucket count arrays for each channel, plus separate S/V counts
    # for the gold-hue subset. Single linear pass over pixel bytes.
    h_all = [0] * 256
    s_all = [0] * 256
    v_all = [0] * 256
    s_gold = [0] * 256
    v_gold = [0] * 256
    gold_count = 0
    hue_lo = DIAG_GOLD_HUE_LO
    hue_hi = DIAG_GOLD_HUE_HI

    for i in range(total):
        hv = h_bytes[i]
        sv = s_bytes[i]
        vv = v_bytes[i]
        h_all[hv] += 1
        s_all[sv] += 1
        v_all[vv] += 1
        if hue_lo <= hv <= hue_hi:
            gold_count += 1
            s_gold[sv] += 1
            v_gold[vv] += 1

    print(f"=== {label} region (frac {region_frac}, px ({x0},{y0})..({x1},{y1})) ===")
    print(f"Region pixels: {total:,}")
    print()
    print("All pixels:")
    for ch, counts in (("H", h_all), ("S", s_all), ("V", v_all)):
        mn, mx, med = _channel_stats(counts, total)
        print(f"  {ch}: min={mn:3d}  max={mx:3d}  median={med:3d}")
    print()

    pct = (gold_count / total * 100) if total else 0.0
    print(
        f"Gold-hue subset (H in [{hue_lo}, {hue_hi}], ~"
        f"{int(hue_lo / 255 * 360)}°..{int(hue_hi / 255 * 360)}°): "
        f"{gold_count:,} pixels ({pct:.2f}%)"
    )
    if gold_count == 0:
        print("  (no gold-hue pixels in region — bg has no gold here at all)")
        print()
        return
    for ch, counts in (("S", s_gold), ("V", v_gold)):
        mn, mx, med = _channel_stats(counts, gold_count)
        print(f"  {ch}: min={mn:3d}  max={mx:3d}  median={med:3d}")
    print()
    print(f"  S histogram (gold-hue subset, {DIAG_BUCKET_SIZE}-unit buckets):")
    _print_histogram(s_gold, gold_count)
    print()
    print(f"  V histogram (gold-hue subset, {DIAG_BUCKET_SIZE}-unit buckets):")
    _print_histogram(v_gold, gold_count)
    print()


def run_diagnose(background_path: Path = BACKGROUND_PATH) -> int:
    """--diagnose entry point: print histograms and exit without detection."""
    if not background_path.exists():
        print(f"Background not found: {background_path}", file=sys.stderr)
        return 1
    bg = Image.open(background_path).convert("RGB")
    print(f"Background: {bg.size[0]}×{bg.size[1]} px")
    print()
    _diagnose_region(bg, DIAG_FRAME_REGION_FRAC, "FRAME")
    _diagnose_region(bg, DIAG_PILL_REGION_FRAC, "PILL")
    print(
        "Threshold guidance: pick GOLD_SAT_MIN / FRAME_VAL_MIN / PILL_VAL_MIN\n"
        "below the median of the gold-hue subset for the landmark you're\n"
        "trying to catch, but above the long-tail of background dust. If\n"
        "the PILL subset's V median falls well below the FRAME's (which it\n"
        "does on this background), a single V threshold can't catch both —\n"
        "use the two-threshold pipeline already implemented in measure()."
    )
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Measure landmark bboxes in assets/background.png, or with "
            "--diagnose dump HSV histograms of the frame and pill regions "
            "so detection thresholds can be tuned from real data."
        ),
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help=(
            "Run HSV histogram diagnostic on the FRAME and PILL search "
            "regions and exit without running detection. Use when "
            "auto-detection misses a landmark and you need to see where "
            "its actual S/V values cluster vs background dust."
        ),
    )
    args = parser.parse_args()

    if args.diagnose:
        return run_diagnose()

    result = measure()
    w, h = result["image_size_px"]
    scale = result["scale_px_per_du"]
    print(f"Background: {w}×{h} px, scale = {scale:.4f} px/DU")
    print()
    print(f"  Frame search region (px): {result['frame_region_px']}")
    print(f"  Pill  search region (px): {result['pill_region_px']}")
    print()
    for key in ("frame", "pill", "price_label"):
        px = result[f"{key}_bbox_px"]
        du = result[f"{key}_bbox_du"]
        print(f"  {key.upper():12} px={px}  du={du}")

    print()
    print("# -------- paste into src/renderer.py --------")
    if result["frame_bbox_du"]:
        print(f"FRAME_BBOX_DU = {tuple(result['frame_bbox_du'])}")
    if result["pill_bbox_du"]:
        print(f"PILL_BBOX_DU = {tuple(result['pill_bbox_du'])}")
    if result["price_label_bbox_du"]:
        print(f"PRICE_LABEL_BBOX_DU = {tuple(result['price_label_bbox_du'])}")
    print("# --------------------------------------------")

    overlay = save_debug_overlay(BACKGROUND_PATH, result)
    gold_mask_path = save_gold_mask_debug(result)
    pill_mask_path = save_pill_mask_debug(result)
    print(f"\nDebug overlay:    {overlay.resolve()}")
    print(f"Gold mask (frame): {gold_mask_path.resolve()}")
    print(f"Pill mask (clipped to pill region, V>={PILL_VAL_MIN}):")
    print(f"  {pill_mask_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
