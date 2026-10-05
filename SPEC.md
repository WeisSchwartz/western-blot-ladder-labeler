# Western Blot Ladder Labeler — Spec

**Status:** v1 implemented (2026-09-28); validated on synthetic blots only
**Audience:** lab members preparing blot figures; developer maintaining the tool
**Constraint:** fully local. Images never leave the machine.

---

## 1. Purpose & scope

Take a western blot image and output the same image with the protein standard
ladder annotated with its molecular-weight values (kDa), ready to drop into a
figure.

**In scope (v1)**
- Bio-Rad **Precision Plus Protein Dual Color** (default) and **Precision Plus
  Protein All Blue**. Ladders are defined in a JSON file, so adding another is a
  data edit, not a code change.
- A single image in which the ladder is visible (e.g., a merged colorimetric +
  chemi export), **or** a blot image plus a separate, pixel-aligned marker image
  (white-light/colorimetric). The ladder is detected on the marker image and the
  labels are drawn on the blot image.
- Automatic ladder-lane and band detection, followed by review and **manual
  correction** in a local web UI.
- **Multiple ladder lanes** on one membrane. Found automatically, or set to a
  fixed count.
- **Detection limits:** adjustable top and bottom limits that exclude membrane
  edges. They are set automatically when edges are found. Each lane's left and
  right edges are adjustable too.
- **Crop before export**, with a preview of the exact output. Each side of the
  output (left/right) takes its labels from a chosen ladder, which may be
  outside the crop.
- **Rotation** of the whole image (manual or auto-straighten), applied to the
  blot and marker together. Lanes, bands, and lane labels follow the
  transform.
- **Merging the marker into the blot**, so the ladder is visible in the
  exported image. Works for grayscale and multichannel RGB (fluorescent)
  blots, on light or dark backgrounds.
- **Lane labels:** lane numbers and/or sample names above or below the blot,
  horizontal, at 45°, or vertical.
- A CLI for fully automatic / batch use.

**Out of scope (v1)**
- Estimating the MW of sample bands from the ladder fit (possible v2: the fit is
  already computed and exported in the JSON sidecar).

**Success criteria**
- On a clean ladder, all visible bands are found and correctly assigned with no
  manual edits.
- When auto-detection is wrong, fixing it takes at most a few clicks: drag the
  lane, drag/add/delete a band, or shift all labels by one.
- The blot pixels are never altered for 8-bit inputs. Labels go in an added
  margin, not on top of the data.

---

## 2. Ladders

| Ladder | Cat. # | Bands (kDa) | Reference bands |
|---|---|---|---|
| Precision Plus Protein Dual Color *(default)* | 1610374 | 250, 150, 100, 75, 50, 37, 25, 20, 15, 10 | 75 and 25 are **pink**; the rest are blue |
| Precision Plus Protein All Blue | 1610373 | 250, 150, 100, 75, 50, 37, 25, 20, 15, 10 | 75, 50, and 25 are **higher intensity** |

Definitions live in `src/blot_ladder/ladders.json`:
`id`, `name`, `bands_kda` (high to low), `reference_kda`, and `reference_cue`
(`"color_pink"` | `"intensity"` | `null`). Check the values against the product
datasheet for your lot.

---

## 3. Inputs & outputs

**Inputs**
- Blot image: PNG, JPEG, TIFF (8- or 16-bit, grayscale or RGB/RGBA), or BMP.
- Optional marker image: same formats. It must have the same pixel dimensions as
  the blot image, or the upload is rejected with a clear message.
- Options: ladder (default Dual Color) and band polarity (auto / dark bands on
  light / light bands on dark).

**Outputs**
- Labelled image as PNG (default) or TIFF, at the original resolution. Labels sit
  in a white margin added on the ladder side, with a tick mark per band and an
  optional "kDa" header.
- JSON sidecar (optional): lane position, band y-coordinates, kDa assignments,
  and the log(MW)-vs-migration fit.

**Pixel handling**
- 8-bit inputs: the original pixels are copied unchanged. Only the canvas is
  extended.
- 16-bit inputs: converted to 8-bit with one **linear** stretch across the whole
  image (0.1–99.9 percentile). Display and output use this same conversion, so
  what you see is what you get. For figure-grade control, export 8-bit from the
  imager software first.

---

## 4. Detection pipeline

1. **Detection signal.** The signal comes from the marker image if one was
   given, otherwise from the blot.
   - Polarity is auto-detected: a bright median background means dark bands.
   - If the image has meaningful color, the signal is weighted by saturation, so
     colored ladder bands stand out from a grayscale chemi signal.
2. **Candidate lanes.** Take the background-subtracted column profile and find
   its local maxima. Each maximum, with its estimated lane width, is a
   candidate.
3. **Band peaks in each candidate lane.**
   - Build a row profile from a strip at the lane centre.
   - Subtract the background (rolling minimum plus smoothing) and apply light
     Gaussian smoothing.
   - Run `find_peaks` with a noise-scaled prominence threshold.
   - Record per-peak features: prominence and pinkness (R − B in the band core).
4. **Assigning peaks to ladder values.**
   - Model: log10(kDa) decreases monotonically with y.
   - Enumerate hypotheses from anchor pairs (peak i → band a, peak j → band b),
     predict every band's y, and match each prediction to the nearest peak within
     a spacing-scaled tolerance, one-to-one.
   - Refine with a quadratic fit and rematch. This accounts for curvature and
     compression of the top bands.
   - Score = matched count − residual penalty − penalty for strong unmatched
     peaks inside the fitted range + reference-band bonus.
   - Reference-band bonus: pink 75/25 for Dual Color when color is available;
     brighter 75/50/25 for All Blue.
   - A cropped image is allowed. Any contiguous or non-contiguous subset of the
     ladder may be visible.
5. **Membrane edges.** Some peaks carry signal across ≥70% of the image width,
   measured on column-block averages. These are full-width lines such as
   membrane edges, not ladder bands, so they are rejected.
   - When such lines lie in the outer 40% of the region, the detection limits
     are set just inside them and detection re-runs.
   - This happens only when the user has not set limits.
   - The test is skipped when the image is less than 4 lane-widths wide (a
     cropped ladder strip).
6. **Lane choice.**
   - Auto mode: the best lane is always kept. Another lane is added only if all
     of the following hold:
     - at least 4 matched bands
     - a score of at least 60% of the best lane's
     - its bands sit at the same heights as the best lane's, allowing a
       constant vertical offset of up to 12% (≤2.5% median mismatch)
   - Fixed-count mode: the top N distinct lanes.
   - Each chosen lane is re-centred on the horizontal extent of its matched
     bands.
7. **Confidence and warnings.** The result is flagged when the best score is low,
   when fewer than 4 bands are matched, or when two lanes score almost the same.

---

## 4a. Rotation (`geometry.py`)

- **Angle:** positive = clockwise, about the image centre. The canvas expands
  and the corners are filled with the median border colour.
- **Whole-image only:** rotation is the only geometric correction. Local warps,
  such as straightening "smiling" bands, are deliberately not offered. They
  move bands relative to each other, which is inappropriate image manipulation
  under journal image-integrity policies.
- **Both images:** the transform is applied to the original blot and marker.
  The UI maps every stored point (bands, lanes, lane labels, and user-set
  limits) through inverse(old) then forward(new). Automatic limits are reset.
- **Auto-straighten:** finds the dominant edge orientation, using the
  magnitude²-weighted mean of exp(4iθ) over the strongest 10% of gradients
  (bands are horizontal, lanes vertical). Each image's estimate is normalised,
  and the result is refined by rotating and re-measuring (3 iterations).
  - Accuracy on synthetic blots: ≤0.1° for tilts up to 4°, ≤0.5° up to 15°.
- **Pixels change:** interpolation alters pixel values. The JSON sidecar
  records the angle.

## 4b. Merging the marker (`merge.py`)

- **Marker signal:** the marker's local contrast against its own background,
  normalised to its 99.7th percentile, with membrane texture below 8%
  suppressed.
  - On a light background: max(1 − luminance, chroma).
  - On a dark background: the maximum channel.
- **Colour:**
  - The marker's own colour, brightened to full intensity for dark blots.
  - A grayscale marker becomes black on a light blot and white on a dark one.
  - Or a custom hex colour.
- **Blend:** α = opacity · signal · lane mask.
  - multiply: B·(1 − α + αC)
  - screen: 1 − (1 − B)(1 − αC)
  - normal: lerp(B, C, α)
  - auto: screen on dark-background blots, multiply otherwise.
- **Lane mask:** by default, only the ladder lanes (±80% of lane width, soft
  edges) are blended. Everything else stays pixel-identical.

## 4c. Sample lanes & lane labels

- **Finding lanes:**
  - Lanes are the peaks of the per-column band-contrast profile, centred on
    their half-maximum extent.
  - Ladder lanes are snapped to their detected x.
  - Candidates closer than 0.55× the lane pitch are merged.
  - A gap that is a whole multiple of the pitch is filled with empty lanes.
- **Defaults:** ladder lanes are named "M" and left out of the numbering.
- **Rendering:**
  - Numbers go in a row next to the blot, with names beyond them.
  - Horizontal names are centred on the lane.
  - Angled or vertical names start at the lane and run away from the blot.
  - The canvas grows to fit.
  - Labels outside the crop are dropped.

## 5. Web UI (local)

`./run.sh` opens `http://127.0.0.1:5057/`.

**Left panel**
- Upload the blot and an optional marker image.
- Ladder dropdown, polarity, and a **Detect** button.
- Band table: y, kDa dropdown, show/hide checkbox, delete.
- **Shift labels ↑/↓**: move every assignment by one ladder position.
- **Auto-assign**: rerun matching on the current band positions.
- Output options: side (auto/left/right), font size (auto or px), "kDa" header,
  tick marks, format (PNG/TIFF), JSON sidecar.

**Canvas**
- View toggle: blot / marker.
- Lane shown as a vertical band. **Drag it** to move the lane, which re-detects
  bands in the new lane.
- Band markers: **drag** vertically to move, **Shift-click** in the lane to add,
  **double-click** a marker to delete.
- Zoom: fit, 100%, +, −.

**Preview & download**
- **Preview** renders the final output on the server and shows it.
- **Download** saves the image, plus the JSON if selected.

---

## 6. Rendering rules

- Margin width = widest label + tick length + padding.
- Label side:
  - auto = the side nearest the ladder lane.
  - If labels are on the side away from the lane, the ticks still sit at the
    image edge.
- Font size:
  - auto ≈ 3% of image height, minimum 12 px.
  - Font: Arial or Helvetica when available, otherwise the Pillow default.
- Label collisions: when labels would overlap (common at 250/150), they are
  nudged apart and joined to their band by a short angled leader.
- Only bands with a kDa value and **show** checked get a label.
- DPI metadata is preserved from the input when present.

---

## 7. CLI

```
blot-ladder label BLOT [--marker MARKER] [--ladder dual-color|all-blue]
                       [--side auto|left|right] [--font-size N] [--no-header]
                       [--polarity auto|dark|light] [--lane-x X]
                       [-o OUT] [--json]
```
This is the fully automatic path. It prints the lane, the bands, and any
warnings, and it accepts several files for batch runs.

---

## 8. Architecture

```
src/blot_ladder/
  ladders.json / ladders.py   ladder definitions
  imaging.py                  load (Pillow/tifffile), 16→8 bit, signal maps
  detect.py                   lanes, peaks, ladder assignment
  geometry.py                 whole-image rotation, auto-straighten
  merge.py                    marker → blot blending
  render.py                   crop, kDa margins, lane labels, de-overlap
  cli.py                      command-line entry
  web/app.py                  Flask API + in-memory sessions
  web/templates/index.html, web/static/{app.js,style.css}
tests/                        synthetic blots → detection/render/API tests
```

**API**
- `GET /api/ladders`
- `POST /api/session` (multipart: blot, marker?)
- `GET /api/session/<id>/image/<blot|marker>`
- `POST /api/session/<id>/detect` `{ladder, polarity, y_range?, n_ladders?, lanes?:[{x,width}]}`
  → `{lanes:[…], y_range, auto_limits, notes, warnings, candidates}`
- `POST /api/session/<id>/assign` `{ladder, lane_x, lane_width, bands:[y], y_range?}`
- `POST /api/session/<id>/geometry` `{angle}` → `{width, height}` (rotated size)
- `POST /api/session/<id>/straighten` → `{angle}`
- `POST /api/session/<id>/lanes` `{y_range?, ladder_xs}` → `{lanes:[{x, width, is_ladder}]}`
- `POST /api/session/<id>/merged` `{merge, ladder_lanes}` → PNG
- `POST /api/session/<id>/render` `{left?, right?, crop?, merge?, ladder_lanes?, lane_labels?:{items:[{x,number,name}], show_numbers, show_names, angle, position}, font_size, header, ticks, format}`

---

## 9. Testing

- Synthetic blots with known ground truth covering:
  - full ladder
  - cropped ladder (150–15 only)
  - light-on-dark polarity
  - ladder on the right
  - Dual Color colored ladder on a grayscale blot
  - separate marker image
  - All Blue intensity cues
  - noisy images
- Pass criteria: the correct lane is found, and every visible band is assigned
  its true kDa within ±1.5% of image height.
- **Still needed: validation on real lab blots.** Put examples in `samples/`.
