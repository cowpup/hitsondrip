# measure_landmarks (archived)

Diagnostic tool that auto-detected the gold frame, gold pill, and "PACK PRICE"
label bboxes in `assets/background.png` via HSV thresholding. Obsoleted on
2026-05-13 when we switched to ground-truth bboxes measured directly from
`assets/reference_sample.png` in MS Paint — four constants in `src/renderer.py`
now define every variable-element bbox.

The auto-detector worked well for the gold FRAME outline but consistently
failed on the gold PILL outline. The `--diagnose` HSV histograms eventually
showed why: in the tight pill region (478K pixels), the gold-hue subset is
only 3.23% (15K pixels), and 89% of those have V<30 — the pill outline lives
in the sparse remainder, only ~600-1000 bright pixels. Signal-to-noise on
the pill is just bad on this background. Manual measurement is faster.

## Files

- `measure_landmarks.py` — the tool. Run via `uv run python -m archive.measure_landmarks.measure_landmarks` after fixing the imports if you ever need it again.
- `landmarks_debug.png` — last debug overlay (search regions + detected bboxes drawn on background.png)
- `landmarks_gold_mask.png` — last frame-threshold gold mask (V≥80)
- `pill_mask_debug.png` — last pill-threshold mask clipped to pill region (V≥45) — almost entirely black, confirming pill outline pixels are below the threshold combination

## When this might be useful again

- If we redesign `background.png` and the bboxes change, this tool's
  `--diagnose` mode can tell us where the new gold elements live without
  needing to manually measure them.
- The `_gold_mask` and `_white_mask` helpers are general-purpose HSV
  thresholding utilities.
