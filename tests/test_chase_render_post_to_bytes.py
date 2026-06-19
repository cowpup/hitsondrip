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
