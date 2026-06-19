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
