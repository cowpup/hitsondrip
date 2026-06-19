# 9:16 Story Canvas + IG Caption Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish Instagram story posts as proper 9:16 (1080×1920) images with no caption, for both the Just Pulled and New Chase automations.

**Architecture:** A new pure PIL helper `src/story_canvas.py` wraps each renderer's existing 4:5 output onto a 9:16 canvas (blurred cover-fit background + crisp centered foreground). It is invoked at the `render_post_to_bytes` funnel in `main.py` and `new_chase.py`, so the renderers and downstream upload/Slack/Metricool layers are untouched. Separately, the Cloudflare Worker's `createInstagramPost` sends an empty `text` because IG stories reject a caption at publish time.

**Tech Stack:** Python 3.13, Pillow 12.2, pytest 9 (run via `uv run python -m pytest`); TypeScript Cloudflare Worker deployed with `wrangler`.

## Global Constraints

- Tests run with `uv run python -m pytest` (system `python` lacks deps).
- Python imports use `from src.<module> import ...` (see `tests/test_schedule_time.py`).
- Renderers (`src/renderer.py`, `src/new_chase_renderer.py`) and their `*_BBOX_DU` constants must NOT change.
- Story canvas target size is exactly `(1080, 1920)`, output mode `RGB`.
- The X/Twitter post keeps its caption; only the Instagram body is blanked.
- `main.py` / `new_chase.py` keep generating the IG caption; the Worker payload parser keeps requiring `ig.caption`. Do not touch caption generation or validation.
- Commit messages end with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

---

### Task 1: `src/story_canvas.py` — `wrap_9x16` helper

**Files:**
- Create: `src/story_canvas.py`
- Test: `tests/test_story_canvas.py`

**Interfaces:**
- Consumes: nothing (pure PIL).
- Produces: `wrap_9x16(src: PIL.Image.Image, *, size: tuple[int, int] = (1080, 1920), blur_radius: int = 48) -> PIL.Image.Image` — returns an `RGB` image at exactly `size`. Background is `src` cover-fit to `size` then Gaussian-blurred; foreground is `src` contain-fit (aspect preserved) composited centered on top.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_story_canvas.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_story_canvas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.story_canvas'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/story_canvas.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_story_canvas.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/story_canvas.py tests/test_story_canvas.py
git commit -m "feat(story): add wrap_9x16 9:16 story canvas helper

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire `wrap_9x16` into Just Pulled (`main.py`)

**Files:**
- Modify: `main.py` (imports near line 47–48; `render_post_to_bytes` at lines 188–208)
- Test: `tests/test_render_post_to_bytes.py`

**Interfaces:**
- Consumes: `wrap_9x16` from Task 1.
- Produces: `main.render_post_to_bytes(...)` now returns PNG bytes that decode to a `(1080, 1920)` image.

- [ ] **Step 1: Write the failing test**

Create `tests/test_render_post_to_bytes.py`:

```python
"""Integration test: render_post_to_bytes wraps output to 9:16."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

import main


def _stub_render_just_pulled(*, output_path, **kwargs):
    """Stand-in for the renderer: write a 4:5 (200x250) PNG."""
    Image.new("RGB", (200, 250), (10, 20, 30)).save(output_path, format="PNG")
    return Path(output_path)


def test_render_post_to_bytes_returns_9x16(monkeypatch):
    monkeypatch.setattr(main, "render_just_pulled", _stub_render_just_pulled)
    data = main.render_post_to_bytes(
        card_image_url="x",
        pack_image_url="y",
        pack_name="PACK",
        pack_price=100,
        hit_value=1234.0,
    )
    img = Image.open(io.BytesIO(data))
    assert img.size == (1080, 1920)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_render_post_to_bytes.py -v`
Expected: FAIL — asserts `(200, 250) == (1080, 1920)` (renderer output not yet wrapped).

- [ ] **Step 3: Add the imports**

`main.py` already has `import io` (line 29), `import tempfile`, and `from pathlib import Path`, but does NOT import PIL. Add two imports alongside the other `from src.*` imports (near lines 47–48 which import `publish_to_github` and `render_just_pulled`):

```python
from PIL import Image
from src.story_canvas import wrap_9x16
```

- [ ] **Step 4: Wrap the rendered bytes**

In `main.py`, replace the body of `render_post_to_bytes` (lines 198–208) so it wraps before returning. `io` is already imported at module level — do not re-import it. The new body:

```python
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "daily.png"
        render_just_pulled(
            card_image_url=card_image_url,
            pack_image_url=pack_image_url,
            pack_name=pack_name,
            pack_price=pack_price,
            hit_value=hit_value,
            output_path=out_path,
        )
        rendered = Image.open(out_path)
        rendered.load()
    story = wrap_9x16(rendered)
    buf = io.BytesIO()
    story.save(buf, format="PNG")
    return buf.getvalue()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_render_post_to_bytes.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `uv run python -m pytest -q`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_render_post_to_bytes.py
git commit -m "feat(story): wrap Just Pulled render to 9:16 before upload

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Wire `wrap_9x16` into New Chase (`new_chase.py`)

**Files:**
- Modify: `new_chase.py` (imports near line 53–54; `render_post_to_bytes` at lines 340–361)
- Test: `tests/test_chase_render_post_to_bytes.py`

**Interfaces:**
- Consumes: `wrap_9x16` from Task 1.
- Produces: `new_chase.render_post_to_bytes(...)` now returns PNG bytes that decode to a `(1080, 1920)` image.

- [ ] **Step 1: Write the failing test**

Create `tests/test_chase_render_post_to_bytes.py`:

```python
"""Integration test: New Chase render_post_to_bytes wraps output to 9:16."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

import new_chase


def _stub_render_new_chase(*, output_path, **kwargs):
    """Stand-in for the New Chase renderer: write a 4:5 (200x250) PNG."""
    Image.new("RGB", (200, 250), (30, 20, 10)).save(output_path, format="PNG")
    return Path(output_path)


def test_chase_render_post_to_bytes_returns_9x16(monkeypatch):
    monkeypatch.setattr(new_chase, "render_new_chase", _stub_render_new_chase)
    data = new_chase.render_post_to_bytes(
        card_image_url="x",
        pack_image_url="y",
        pack_name="PACK",
        hit_value=5000.0,
    )
    img = Image.open(io.BytesIO(data))
    assert img.size == (1080, 1920)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_chase_render_post_to_bytes.py -v`
Expected: FAIL — asserts `(200, 250) == (1080, 1920)`.

- [ ] **Step 3: Add the imports**

`new_chase.py` has `import tempfile` and `from pathlib import Path`, but does NOT import `io` or PIL. Add `import io` near the other stdlib imports (e.g. next to `import tempfile` at line 43), and add the following alongside the other `from src.*` imports (near lines 53–54 which import `publish_to_github` and `render_new_chase`):

```python
from PIL import Image
from src.story_canvas import wrap_9x16
```

- [ ] **Step 4: Wrap the rendered bytes**

In `new_chase.py`, replace the body of `render_post_to_bytes` (lines 352–361) so it wraps before returning (relies on the module-level `import io` added in Step 3):

```python
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "chase.png"
        render_new_chase(
            card_image_url=card_image_url,
            pack_image_url=pack_image_url,
            pack_name=pack_name,
            hit_value=int(round(hit_value)),
            output_path=out_path,
        )
        rendered = Image.open(out_path)
        rendered.load()
    story = wrap_9x16(rendered)
    buf = io.BytesIO()
    story.save(buf, format="PNG")
    return buf.getvalue()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_chase_render_post_to_bytes.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `uv run python -m pytest -q`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add new_chase.py tests/test_chase_render_post_to_bytes.py
git commit -m "feat(story): wrap New Chase render to 9:16 before upload

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Blank IG story caption in the Worker + redeploy

**Files:**
- Modify: `worker/src/index.ts` (`createInstagramPost`, the `text:` line ~453)

**Interfaces:**
- Consumes: nothing new.
- Produces: the Instagram Metricool POST body sends `text: ""`. `createXPost` is unchanged. The `createInstagramPost` signature is unchanged (the `caption` parameter is retained but intentionally unused).

There is no JS test framework in `worker/`; verification is a type-check, the already-proven Metricool empty-text smoke test, and a deploy.

- [ ] **Step 1: Make the change**

In `worker/src/index.ts`, inside `createInstagramPost`, change the body's text field from `text: caption,` to:

```typescript
    // IG stories carry no caption; a non-empty text fails at publish.
    // The caption param is kept for signature stability (see index.ts:225).
    text: "",
```

- [ ] **Step 2: Type-check — confirm no NEW errors**

Run: `cd worker && npx tsc --noEmit`
Expected: the same 4 pre-existing errors about `.blocks` on lines ~605–644, and nothing new. (These predate this work; confirm the count is still 4 and none reference `createInstagramPost`.)

- [ ] **Step 3: Smoke-test empty-text STORY acceptance against Metricool**

Run (from repo root):

```bash
uv run python -c "
from dotenv import load_dotenv; load_dotenv()
from datetime import datetime, timedelta
import os
from src.metricool import schedule_instagram_post, delete_scheduled_post
blog_id = int(os.environ['METRICOOL_BLOG_ID'])
img = 'https://raw.githubusercontent.com/cowpup/hitsondrip/daily-output/latest.png'
when = (datetime.now() + timedelta(days=2)).replace(microsecond=0, second=0)
resp = schedule_instagram_post(blog_id, '', img, when, draft=True, auto_publish=False)
data = resp.get('data', resp)
pid = data.get('id')
print('ACCEPTED empty-text STORY id', pid, '| text=', repr(data.get('text')))
delete_scheduled_post(str(pid), blog_id); print('cleaned up', pid)
"
```

Expected: prints `ACCEPTED empty-text STORY id <n> | text= ''` then `cleaned up <n>`. (Confirms Metricool accepts the blank caption; the draft is deleted so nothing lingers.)

- [ ] **Step 4: Commit**

```bash
git add worker/src/index.ts
git commit -m "fix(story): send empty IG caption (stories reject feed captions)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 5: Deploy the Worker**

Run: `cd worker && npx wrangler deploy`
Expected: `Uploaded hitsondrip-approver` + `Deployed hitsondrip-approver triggers` and a new `Current Version ID`. (Requires the wrangler login already established this session.)

- [ ] **Step 6: Push everything**

```bash
git push origin main
```

---

## Verification (end-to-end)

After all tasks, regenerate a real story image locally and re-upload it so the live `latest.png` is 9:16, then eyeball it:

```bash
# Render via the standalone renderer CLI into a temp 4:5 PNG, then wrap:
uv run python -c "
from PIL import Image
from src.story_canvas import wrap_9x16
# Use the current hosted render as a stand-in 4:5 source:
import io, requests
r = requests.get('https://raw.githubusercontent.com/cowpup/hitsondrip/daily-output/latest.png', timeout=30)
src = Image.open(io.BytesIO(r.content)); src.load()
out = wrap_9x16(src)
out.save('story_preview.png')
print('wrote story_preview.png', out.size)
"
```

Open `story_preview.png` and confirm: 1080×1920, the framed card crisp and centered, soft blurred glow filling top/bottom. Then the next real Approve click posts a caption-free 9:16 story.
