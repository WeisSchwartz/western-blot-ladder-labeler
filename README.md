# Western Blot Ladder Labeler

This tool labels the protein standard ladder on western blot images with its kDa
values. It auto-detects the ladder lane and bands, lets you correct them in a
local web UI, and outputs a figure-ready image. Everything runs on your machine.

The design and algorithm are described in [SPEC.md](SPEC.md).

## Getting started (no command line needed)

1. **Get the folder.** Download it, unzip it, and put it somewhere permanent,
   such as your Documents folder.
2. **Double-click the launcher:**
   - **Mac:** `Blot Ladder Labeler.command`
   - **Windows:** `Blot Ladder Labeler.bat`
3. **The first time only,** the launcher installs what the app needs. This
   takes a minute or two and needs an internet connection. After that, the app
   starts in a few seconds.
4. **Your browser opens the app.** A small window also opens and shows the
   app's status. Keep that window open while you work, and close it when
   you're done to quit the app. Double-clicking the launcher again while the
   app is running just reopens it in your browser.

**First time on a Mac:**
- **"Cannot be opened because it is from an unidentified developer."** Right-click
  (or Control-click) the launcher, choose **Open**, then click **Open** again.
  You only need to do this once.
- **A window asks to install the "command line developer tools".** Click
  **Install**, wait for it to finish, then double-click the launcher again. This
  provides Python, which the app needs.
- **"You do not have appropriate access privileges."** This happens when the
  folder came through a file-sharing service that drops the "can be run"
  setting. Open the Terminal app, type `bash ` (with a space), drag the
  launcher into the Terminal window, and press Return.

**First time on Windows:**
- **"Python was not found."** Install Python from
  <https://www.python.org/downloads/>. In the installer, tick **Add python.exe
  to PATH**. Then double-click the launcher again.
- **"Windows protected your PC."** Click **More info**, then **Run anyway**.

**If the folder is in OneDrive, iCloud, or Dropbox:** right-click the folder
and choose **Always Keep on This Device** (or the equivalent). Otherwise the
sync app can move the app's files to the cloud, and the app then freezes at
startup while they download.

## Setup and running from the command line

The launchers do this for you. Behind the scenes, the Python environment is
created in `.venv` inside this folder (git ignores it). To put it somewhere
else, set `BLOT_LADDER_VENV` before running `./setup.sh` and `./run.sh`.

```bash
./setup.sh
```

```bash
./run.sh
```

## Using the web app

A browser opens at <http://127.0.0.1:5057/>. If another program is already
using that port, the app picks the next free one and shows the address in its
window. Then:

1. **Choose the blot image.** If your imager exports the ladder as a separate
   colorimetric or white-light image, also add it as the **Marker image**. It
   must be the same size as the blot image.
2. Pick the ladder. The default is **Precision Plus Dual Color**; **All Blue**
   is also available. Detection runs automatically.
3. Check the orange labels on each ladder lane. All ladders are found
   automatically. Set **Ladder lanes** to a fixed number if you prefer, or use
   **+ Add ladder lane** and click on the ladder.
   - Drag the red **top/bottom limit** lines to keep membrane edges out of
     detection. The limits are set automatically when edges are found.
   - **Drag a band** to move it. Arrow keys nudge the selected band.
   - **Shift-click** in the lane to add a band.
   - **Click a dashed ghost** (a predicted band position) to add a band there.
   - **Double-click** a band, or press Delete, to remove it.
   - **Drag the lane** sideways, or drag its **left/right edges**, to re-detect.
   - Use **Labels ↑ / ↓** if every label is off by one band.
4. **Align & crop** tab:
   - Use **Rotate**, or click **Auto-straighten**, if the blot is tilted.
     Bands you've already placed move with the image. Only whole-image
     rotation is offered. Warping curved ("smiling") bands straight would
     count as image manipulation.
   - Drag a crop box. The ladder can be left outside the box, and its labels
     are kept.
5. **Merge marker** (only when a marker image is loaded): blends the marker's
   ladder into the exported image. It works on light (chemi) and dark
   (fluorescent RGB) backgrounds. Use the **Merged** view to check the result.
6. **Lanes** tab: lanes are found automatically. Type or paste sample names,
   drag markers to adjust, and **Shift-click** to add a lane. You can show
   numbers, names, or both, horizontal, angled, or vertical, above or below
   the blot.
7. Under **Output**, choose which ladder labels the left and right sides.
   Check **Preview**, then click **Download**.

## Use the command line (fully automatic)

```bash
.venv/bin/blot-ladder label blot.tif
```

```bash
.venv/bin/blot-ladder label chemi.tif --marker marker.tif --ladder all-blue --json
```

```bash
.venv/bin/blot-ladder label *.png -o labeled/
```

```bash
.venv/bin/blot-ladder label blot.png --ladders 2 --y-range 40 650 --crop 150 40 550 610
```

```bash
.venv/bin/blot-ladder label chemi.tif --marker marker.tif --auto-straighten --merge --lane-numbers --lane-names "WT,KO,Rescue"
```

Run `blot-ladder label -h` to see all options.

## Notes

- **8-bit images** keep their pixels unchanged. Labels go in an added white
  margin.
- **16-bit images** are converted to 8-bit with one linear stretch across the
  whole image (0.1–99.9 percentile). For full control, export 8-bit from the
  imager software first.
- **Reference-band cues:**
  - On a color image, Dual Color's pink 75 and 25 kDa bands anchor the
    assignment.
  - For All Blue, the brighter 75, 50, and 25 kDa bands do the same.
  - On grayscale images with Dual Color, labels come from band spacing only, and
    the app shows a warning. Check the labels in this case. The typical error is
    every label shifted by one band.
- **Adding a ladder:** add an entry to `src/blot_ladder/ladders.json`.

## Tests

```bash
.venv/bin/python -m pytest
```
