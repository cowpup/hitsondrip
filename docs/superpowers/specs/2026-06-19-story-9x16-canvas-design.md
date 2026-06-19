# 9:16 Story Canvas Wrapper

**Date:** 2026-06-19
**Status:** Approved (design)

## Problem

Instagram posts now publish as **stories** (`instagramData.type: "STORY"`, shipped commit `717bbea`, Worker version `c6e1f5dd`). Both automations — Just Pulled (`src/renderer.py`) and New Chase (`src/new_chase_renderer.py`) — render **4:5 portrait** graphics (background.png is 3750×4688 ≈ 0.80). Stories are **9:16** (0.5625), so the 4:5 art does not fill the story canvas and gets letterboxed/fit by Instagram in a way we don't control.

We need the published image to be a proper 9:16 (1080×1920) canvas, applied to **both** automations.

## Decision

**Pad/letterbox in code with a blurred-zoom fill.** Keep the existing 4:5 artwork and both renderers untouched; add a separate, isolated post-processing step that wraps the rendered 4:5 image onto a 9:16 canvas.

Rejected alternatives:
- *Native 9:16 redesign* — best visual result but requires new Canva artwork and re-measuring every `*_BBOX_DU` constant in both renderers. Too much effort for the goal.
- *Plain black extension* — the art's edges are pure black (0,0,0), so solid-black padding would be seamless, but the user chose the blurred zoom for a more designed look. The blur of black-edged art reads as a soft glow of the gold frame rather than a busy background.

## Architecture

### New module: `src/story_canvas.py`

A single pure function. No network I/O. Operates on PIL images so it is trivially unit-testable.

```python
def wrap_9x16(
    src: Image.Image,
    *,
    size: tuple[int, int] = (1080, 1920),
    blur_radius: int = 48,
) -> Image.Image:
    """Wrap a portrait render onto a 9:16 story canvas.

    Background: src scaled to *cover* `size` (fill + center-crop), then
    Gaussian-blurred by `blur_radius`. With black-edged source art this
    yields a dark canvas with a soft color glow top/bottom.

    Foreground: src scaled to *fit* the canvas width (aspect preserved),
    composited horizontally centered and vertically centered on top of
    the blurred background.

    Returns an RGB image at exactly `size`.
    """
```

Implementation notes:
- Background cover-fit: `ImageOps.fit(src, size, Image.Resampling.LANCZOS)` then `.filter(ImageFilter.GaussianBlur(blur_radius))`.
- Foreground fit-to-width: scale `src` so width == `size[0]`, preserving aspect (4:5 → 1080×1350). Use `ImageOps.contain` against `(size[0], size[1])` — width is the binding dimension for a 4:5 source in a 9:16 box, so this yields 1080×1350.
- Composite: paste foreground centered. Convert to RGB for output (stories are opaque; no alpha needed). If `src` has alpha, flatten the foreground onto the blurred background via `alpha_composite` before converting.

### Integration: two call sites

Both `main.py` and `new_chase.py` expose a `render_post_to_bytes(...)` that runs the renderer (writing a temp PNG), reads it back, and returns PNG bytes. Insert the wrap step there, after the renderer produces bytes and before returning:

1. Decode the renderer's PNG bytes to a PIL image.
2. `story = wrap_9x16(image)`.
3. Re-encode `story` to PNG bytes and return those.

This keeps the wrapping concern out of the renderers themselves (which continue to emit 4:5) and out of the upload/Slack/Metricool layers (which are format-agnostic). It is the single funnel both automations already pass through.

## Data flow

```
renderer (4:5 PNG bytes)
  → render_post_to_bytes: decode → wrap_9x16 → re-encode (9:16 PNG bytes)
  → publish_to_github (latest.png / chase_*.png)
  → Slack preview + Metricool STORY post
```

No change to the GitHub host paths, Slack embed, Metricool body, or the Worker.

## Error handling

`wrap_9x16` is pure PIL on already-decoded, validated images (the renderer already produced them), so failure risk is low. Any PIL exception propagates; the existing `render_post_to_bytes` callers already wrap rendering in try/except that emits failure text to Slack and exits non-zero, so a wrap failure surfaces the same way as a render failure. No new error class needed.

## Testing

Unit tests for `wrap_9x16` (`tests/test_story_canvas.py`), using a small synthetic source (e.g., a 200×250 image with a distinct solid-color center block):

- **Dimensions:** output is exactly `(1080, 1920)` and mode `RGB`.
- **Foreground placement:** the center pixel of the output matches the source's center color (foreground is centered and on top, not the blur).
- **Blur applied:** a top-edge / bottom-edge region of the output differs from a plain hard-edged paste (i.e., the background layer is present and blurred), confirming the fill exists rather than transparent/black-only bars.
- **Aspect preserved:** foreground occupies full width (1080) and 4:5 height (1350), leaving symmetric vertical margins (285 px top and bottom) — verified by checking that rows at y≈140 (in the margin) come from the blurred background, not the crisp foreground.

No network, fast, deterministic.

## Scope

In: `src/story_canvas.py`, wrap-step insertion in `main.py` and `new_chase.py`, unit tests.

Out (YAGNI): new Canva artwork, any `*_BBOX_DU` changes, a darken/dim overlay knob on the blurred background (the black edges already give separation; add later only if output looks flat), per-automation resolution overrides.
