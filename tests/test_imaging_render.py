import io

import numpy as np
import tifffile
from PIL import Image
from synth import make_blot

from blot_ladder.imaging import load_image
from blot_ladder.render import (RenderOptions, default_sides, encode, render_figure, render_labels,
                                spread_positions)


def test_8bit_pixels_are_preserved_in_output():
    syn = make_blot(seed=1)
    bands = [{"y": y, "kda": k} for k, y in syn.truth.items()]
    out = np.asarray(render_labels(syn.blot, bands, RenderOptions(side="left", header=False)))
    H, W = syn.blot.shape[:2]
    margin = out.shape[1] - W
    assert margin > 0 and out.shape[0] == H
    assert np.array_equal(out[:, margin:], syn.blot)


def test_right_side_and_header_extends_when_needed():
    syn = make_blot(seed=1)
    bands = [{"y": 2.0, "kda": 250}, {"y": 300.0, "kda": 50}]
    out = np.asarray(render_labels(syn.blot, bands, RenderOptions(side="right", header=True)))
    H, W = syn.blot.shape[:2]
    extra = out.shape[0] - H
    assert extra > 0
    assert np.array_equal(out[extra:, :W], syn.blot)


def test_hidden_and_unlabeled_bands_are_skipped():
    syn = make_blot(seed=1)
    a = render_labels(syn.blot, [{"y": 100, "kda": 50}], RenderOptions(header=False))
    b = render_labels(syn.blot, [{"y": 100, "kda": 50}, {"y": 200, "kda": 37, "show": False},
                                 {"y": 300, "kda": None}], RenderOptions(header=False))
    assert np.array_equal(np.asarray(a), np.asarray(b))


def test_spread_positions():
    out = spread_positions([10, 12, 13, 100], 10, 0, 1000)
    assert all(b - a >= 10 - 1e-9 for a, b in zip(out, out[1:]))
    assert out[-1] == 100
    assert spread_positions([0, 1], 10, 5, 1000)[0] >= 5


def test_16bit_tiff_is_stretched_and_dpi_png_roundtrip():
    arr = (np.linspace(0, 4000, 200 * 100).reshape(200, 100)).astype(np.uint16)
    buf = io.BytesIO()
    tifffile.imwrite(buf, arr)
    im = load_image(buf.getvalue(), "x.tif")
    assert im.rgb.dtype == np.uint8 and im.rgb.shape == (200, 100, 3)
    assert im.was_stretched and im.source_bit_depth == 16
    assert im.rgb.max() == 255 and im.rgb.min() == 0

    png = encode(Image.fromarray(im.rgb), "png", (300.0, 300.0))
    assert abs(Image.open(io.BytesIO(png)).info["dpi"][0] - 300) < 0.01  # PNG stores px/m


def test_rgba_and_grayscale_png_load():
    buf = io.BytesIO()
    Image.fromarray(np.zeros((10, 20, 4), np.uint8)).save(buf, format="PNG")  # fully transparent
    im = load_image(buf.getvalue(), "a.png")
    assert (im.rgb == 255).all()
    buf = io.BytesIO()
    Image.fromarray(np.full((10, 20), 77, np.uint8)).save(buf, format="PNG")
    im = load_image(buf.getvalue(), "g.png")
    assert (im.rgb == 77).all() and not im.was_stretched and not im.is_color


def test_crop_and_labels_on_both_sides():
    syn = make_blot(ladder_lane=[0, 7], seed=2)
    bands = [{"y": y, "kda": k} for k, y in syn.truth.items()]
    crop = {"x": 150, "y": 100, "w": 500, "h": 450}  # ladders are cropped out
    opts = RenderOptions(header=False)
    out = np.asarray(render_figure(syn.blot, bands, bands, opts, crop=crop))
    only_left = np.asarray(render_figure(syn.blot, bands, None, opts, crop=crop))
    ml = only_left.shape[1] - 500
    assert out.shape[0] == 450 and ml > 0
    assert np.array_equal(out[:, ml:ml + 500], syn.blot[100:550, 150:650])
    assert out.shape[1] == 500 + 2 * ml


def test_bands_outside_crop_are_dropped():
    syn = make_blot(seed=2)
    opts = RenderOptions(header=False)
    a = render_figure(syn.blot, [{"y": 300, "kda": 50}], None, opts, crop={"x": 0, "y": 200, "w": 400, "h": 200})
    b = render_figure(syn.blot, [{"y": 300, "kda": 50}, {"y": 50, "kda": 250}, {"y": 650, "kda": 10}],
                      None, opts, crop={"x": 0, "y": 200, "w": 400, "h": 200})
    assert np.array_equal(np.asarray(a), np.asarray(b))


def test_crop_is_clamped_to_image():
    syn = make_blot(seed=2)
    out = render_figure(syn.blot, None, None, crop={"x": -50, "y": 600, "w": 5000, "h": 5000})
    assert out.size == (900, 100)


def test_default_sides():
    assert default_sides([{"lane_x": 50}], 900) == (0, None)
    assert default_sides([{"lane_x": 850}], 900) == (None, 0)
    assert default_sides([{"lane_x": 850}, {"lane_x": 50}, {"lane_x": 400}], 900) == (1, 0)
    assert default_sides([], 900) == (None, None)
