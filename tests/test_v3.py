"""Rotation, marker merging, sample lanes, and lane labels."""
import io

import numpy as np
import pytest
from PIL import Image
from synth import make_blot

from blot_ladder.detect import detect_ladders, find_sample_lanes
from blot_ladder.geometry import Geometry, apply, auto_straighten, forward, inverse
from blot_ladder.ladders import get_ladder
from blot_ladder.merge import MergeOptions, merge
from blot_ladder.render import LaneLabels, RenderOptions, render_figure
from blot_ladder.web.app import create_app


# ── geometry ──

@pytest.mark.parametrize("geom", [Geometry(7.5), Geometry(-12), Geometry(0.4), Geometry(-30)])
def test_point_mapping_matches_pixels(geom):
    img = np.full((300, 500, 3), 255, np.uint8)
    pts = [(60, 40), (250, 150), (430, 260)]
    for x, y in pts:
        img[y - 2:y + 3, x - 2:x + 3] = 0
    out = apply(img, geom)
    yy, xx = np.nonzero(out[..., 0] < 128)
    for x, y in pts:
        fx, fy = forward(x, y, (500, 300), geom)
        sel = np.hypot(xx - fx, yy - fy) < 8
        assert sel.any()
        assert np.hypot(xx[sel].mean() - fx, yy[sel].mean() - fy) < 0.6
        bx, by = inverse(fx, fy, (500, 300), geom)
        assert abs(bx - x) < 1e-6 and abs(by - y) < 1e-6


def test_identity_geometry_keeps_pixels():
    syn = make_blot(seed=1)
    assert apply(syn.blot, Geometry()) is syn.blot


@pytest.mark.parametrize("tilt", [3.0, -2.0, 0.5, -4.0])
def test_auto_straighten(tilt):
    syn = make_blot(ladder_style="color", separate_marker=True, seed=5, membrane=(0.06, 0.94))
    g = Geometry(-tilt)
    got = auto_straighten([apply(syn.blot, g), apply(syn.marker, g)])
    assert abs(got - tilt) <= 0.15


def test_auto_straighten_level_image():
    assert abs(auto_straighten([make_blot(seed=4).blot])) <= 0.1


def test_detection_after_straightening_matches_untilted():
    syn = make_blot(seed=3)
    lad = get_ladder("dual-color")
    straight = apply(apply(syn.blot, Geometry(-3.0)), Geometry(3.0))
    ref = {b["kda"] for b in detect_ladders(syn.blot, lad).lanes[0].bands}
    got = {b["kda"] for b in detect_ladders(straight, lad).lanes[0].bands}
    assert got == ref


# ── merge ──

def _fluorescent():
    g = make_blot(ladder_style="color", separate_marker=True, polarity="light", seed=6)
    r = make_blot(ladder_style="color", separate_marker=True, polarity="light", seed=21)
    blot = np.stack([r.blot[..., 0], g.blot[..., 0], np.zeros_like(g.blot[..., 0])], axis=2)
    return blot, g.marker, g.lane_x


MERGE_CASES = {
    "chemi, light background": lambda: (lambda s: (s.blot, s.marker, s.lane_x))(
        make_blot(ladder_style="color", separate_marker=True, seed=6)),
    "chemi, dark background": lambda: (lambda s: (s.blot, s.marker, s.lane_x))(
        make_blot(ladder_style="color", separate_marker=True, polarity="light", seed=6)),
    "fluorescent RGB": _fluorescent,
    "grayscale marker, dark blot": lambda: (lambda s: (s.blot, s.marker, s.lane_x))(
        make_blot(separate_marker=True, polarity="light", seed=6)),
}


@pytest.mark.parametrize("name", MERGE_CASES)
def test_merge_makes_ladder_visible_and_leaves_rest(name):
    blot, marker, lane_x = MERGE_CASES[name]()
    out = merge(blot, marker, MergeOptions(enabled=True), [{"x": lane_x, "width": 68}])
    x0, x1 = int(lane_x - 20), int(lane_x + 20)
    before = np.abs(blot[:, x0:x1].astype(int) - np.median(blot[:, x0:x1], axis=(0, 1))).sum(axis=2)
    after = np.abs(out[:, x0:x1].astype(int) - np.median(blot[:, x0:x1], axis=(0, 1))).sum(axis=2)
    assert after.max() > before.max() + 150  # ladder bands now stand out
    assert np.array_equal(out[:, 200:], blot[:, 200:])  # outside the ladder lane: untouched


def test_merge_whole_image_and_custom_color():
    s = make_blot(ladder_style="color", separate_marker=True, seed=6)
    out = merge(s.blot, s.marker, MergeOptions(enabled=True, lanes_only=False, color="#ff0000",
                                                mode="normal"), None)
    lane = out[:, int(s.lane_x) - 5:int(s.lane_x) + 5]
    assert (lane[..., 0].astype(int) - lane[..., 2].astype(int)).max() > 150  # painted red


def test_merge_size_mismatch():
    with pytest.raises(ValueError):
        merge(np.zeros((10, 10, 3), np.uint8), np.zeros((12, 10, 3), np.uint8), MergeOptions(enabled=True))


# ── sample lanes & lane labels ──

@pytest.mark.parametrize("kw", [dict(), dict(n_lanes=12, seed=3), dict(ladder_lane=[0, 7], seed=4),
                                dict(polarity="light", n_lanes=10, seed=5)])
def test_find_sample_lanes(kw):
    syn = make_blot(**kw)
    n = kw.get("n_lanes", 8)
    pitch = syn.blot.shape[1] / (n + 0.5)
    truth = [pitch * (i + 0.75) for i in range(n)]
    lanes = find_sample_lanes(syn.blot, ladder_xs=syn.lane_xs)
    assert len(lanes) == n
    assert all(min(abs(ln["x"] - t) for ln in lanes) < 5 for t in truth)
    ladders = sorted(ln["x"] for ln in lanes if ln["is_ladder"])
    assert len(ladders) == len(syn.lane_xs)
    assert all(abs(a - b) < 0.01 for a, b in zip(ladders, sorted(syn.lane_xs)))


@pytest.mark.parametrize("angle,position", [(0, "top"), (45, "top"), (90, "top"), (45, "bottom"), (90, "bottom")])
def test_lane_labels_add_space_and_keep_pixels(angle, position):
    syn = make_blot(seed=2)
    items = [{"x": 180, "number": "1", "name": "WT + IFN-β"}, {"x": 290, "number": "2", "name": "KO"}]
    plain = np.asarray(render_figure(syn.blot, None, None, RenderOptions()))
    out = np.asarray(render_figure(syn.blot, None, None, RenderOptions(),
                                   lane_labels=LaneLabels(items=items, angle=angle, position=position)))
    assert out.shape[0] > plain.shape[0]
    H, W = syn.blot.shape[:2]
    # The blot appears unchanged somewhere in the output (offset by the label strip).
    top = out.shape[0] - H if position == "top" else 0
    assert any(np.array_equal(out[top:top + H, x:x + W], syn.blot) for x in range(0, out.shape[1] - W + 1))


def test_lane_labels_outside_crop_are_dropped():
    syn = make_blot(seed=2)
    crop = {"x": 300, "y": 0, "w": 300, "h": 700}
    a = render_figure(syn.blot, None, None, RenderOptions(), crop=crop,
                      lane_labels=LaneLabels(items=[{"x": 400, "number": "1", "name": None}]))
    b = render_figure(syn.blot, None, None, RenderOptions(), crop=crop,
                      lane_labels=LaneLabels(items=[{"x": 400, "number": "1", "name": None},
                                                    {"x": 50, "number": "2", "name": "gone"}]))
    assert np.array_equal(np.asarray(a), np.asarray(b))


# ── web API ──

def _png(arr):
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    buf.seek(0)
    return buf


def test_api_geometry_merge_lanes_render():
    s = make_blot(ladder_style="color", separate_marker=True, seed=6)
    g = Geometry(-2.5)
    blot, marker = apply(s.blot, g), apply(s.marker, g)
    c = create_app().test_client()
    sess = c.post("/api/session", data={"blot": (_png(blot), "b.png"), "marker": (_png(marker), "m.png")},
                  content_type="multipart/form-data").get_json()
    url = f"/api/session/{sess['id']}"

    angle = c.post(f"{url}/straighten", json={}).get_json()["angle"]
    assert abs(angle - 2.5) <= 0.15
    geo = c.post(f"{url}/geometry", json={"angle": angle}).get_json()
    assert geo["width"] > blot.shape[1]
    img = Image.open(io.BytesIO(c.get(f"{url}/image/blot").data))
    assert img.size == (geo["width"], geo["height"])

    res = c.post(f"{url}/detect", json={}).get_json()
    ladder = res["lanes"][0]
    assert len(ladder["bands"]) == 10

    merged = c.post(f"{url}/merged", json={"merge": {"mode": "auto"},
                                           "ladder_lanes": [{"x": ladder["lane_x"], "width": ladder["lane_width"]}]})
    assert merged.status_code == 200 and merged.mimetype == "image/png"

    lanes = c.post(f"{url}/lanes", json={"ladder_xs": [ladder["lane_x"]]}).get_json()["lanes"]
    assert len(lanes) == 8 and sum(ln["is_ladder"] for ln in lanes) == 1
    items, n = [], 0
    for ln in lanes:
        n += not ln["is_ladder"]
        items.append({"x": ln["x"], "number": None if ln["is_ladder"] else str(n), "name": f"S{n}"})
    out = c.post(f"{url}/render", json={
        "left": ladder["bands"], "merge": {"enabled": True},
        "ladder_lanes": [{"x": ladder["lane_x"], "width": ladder["lane_width"]}],
        "lane_labels": {"items": items, "angle": 45}, "preview": True})
    assert out.status_code == 200
    assert Image.open(io.BytesIO(out.data)).size[1] > geo["height"]

    reset = c.post(f"{url}/geometry", json={"angle": 0}).get_json()
    assert (reset["width"], reset["height"]) == (blot.shape[1], blot.shape[0])


def test_api_merged_needs_marker():
    s = make_blot(seed=1)
    c = create_app().test_client()
    sess = c.post("/api/session", data={"blot": (_png(s.blot), "b.png")},
                  content_type="multipart/form-data").get_json()
    assert c.post(f"/api/session/{sess['id']}/merged", json={}).status_code == 400


def test_no_local_warp_is_possible():
    """Only whole-image rotation exists; a 'smile' value sent to the API has no effect."""
    import dataclasses
    assert [f.name for f in dataclasses.fields(Geometry)] == ["angle"]
    s = make_blot(seed=1)
    c = create_app().test_client()
    sess = c.post("/api/session", data={"blot": (_png(s.blot), "b.png")},
                  content_type="multipart/form-data").get_json()
    url = f"/api/session/{sess['id']}"
    r = c.post(f"{url}/geometry", json={"angle": 0, "smile": 25}).get_json()
    assert "smile" not in r
    img = np.asarray(Image.open(io.BytesIO(c.get(f"{url}/image/blot").data)).convert("RGB"))
    assert np.array_equal(img, s.blot)
