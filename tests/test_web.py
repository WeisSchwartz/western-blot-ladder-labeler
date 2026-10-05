import io

import numpy as np
from PIL import Image
from synth import make_blot

from blot_ladder.web.app import create_app


def png(arr):
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    buf.seek(0)
    return buf


def test_full_flow_with_marker():
    syn = make_blot(ladder_style="color", separate_marker=True, seed=6)
    c = create_app().test_client()
    assert c.get("/").status_code == 200
    lad = c.get("/api/ladders").get_json()
    assert lad["default"] == "dual-color"

    r = c.post("/api/session", data={"blot": (png(syn.blot), "b.png"),
                                     "marker": (png(syn.marker), "m.png")},
               content_type="multipart/form-data")
    s = r.get_json()
    assert r.status_code == 200 and s["has_marker"]
    assert c.get(f"/api/session/{s['id']}/image/marker").status_code == 200

    res = c.post(f"/api/session/{s['id']}/detect", json={"ladder": "dual-color"}).get_json()
    assert len(res["lanes"]) == 1
    d = res["lanes"][0]
    assert len(d["bands"]) == 10 and d["reference_cue_used"]

    a = c.post(f"/api/session/{s['id']}/assign",
               json={"ladder": "dual-color", "lane_x": d["lane_x"],
                     "bands": [b["y"] for b in d["bands"]]}).get_json()
    assert [b["kda"] for b in a["bands"]] == [b["kda"] for b in d["bands"]]

    out = c.post(f"/api/session/{s['id']}/render", json={"left": d["bands"], "format": "tiff"})
    assert out.status_code == 200 and out.mimetype == "image/tiff"
    assert "attachment" in out.headers["Content-Disposition"]
    img = Image.open(io.BytesIO(out.data))
    assert img.size[0] > syn.blot.shape[1]


def test_marker_size_mismatch_rejected():
    c = create_app().test_client()
    r = c.post("/api/session", data={"blot": (png(np.zeros((50, 60, 3), np.uint8)), "b.png"),
                                     "marker": (png(np.zeros((40, 60, 3), np.uint8)), "m.png")},
               content_type="multipart/form-data")
    assert r.status_code == 400 and "same size" in r.get_json()["error"]


def test_unknown_session_404_json():
    c = create_app().test_client()
    r = c.post("/api/session/nope/detect", json={})
    assert r.status_code == 404 and "expired" in r.get_json()["error"]


def test_bad_file_rejected():
    c = create_app().test_client()
    r = c.post("/api/session", data={"blot": (io.BytesIO(b"not an image"), "x.png")},
               content_type="multipart/form-data")
    assert r.status_code == 400


def test_multi_ladder_limits_lanes_and_crop():
    syn = make_blot(ladder_lane=[0, 7], membrane=(0.08, 0.9), seed=8)
    c = create_app().test_client()
    s = c.post("/api/session", data={"blot": (png(syn.blot), "b.png")},
               content_type="multipart/form-data").get_json()
    url = f"/api/session/{s['id']}"
    res = c.post(f"{url}/detect", json={}).get_json()
    assert len(res["lanes"]) == 2 and res["auto_limits"] and res["notes"]
    top, bottom = res["y_range"]

    # Re-detect one given lane with explicit limits (what the UI does after a drag).
    ln = res["lanes"][1]
    one = c.post(f"{url}/detect", json={"lanes": [{"x": ln["lane_x"], "width": ln["lane_width"]}],
                                        "y_range": [top, bottom]}).get_json()
    assert len(one["lanes"]) == 1 and len(one["lanes"][0]["bands"]) == len(ln["bands"])

    fixed = c.post(f"{url}/detect", json={"n_ladders": 1}).get_json()
    assert len(fixed["lanes"]) == 1

    crop = {"x": 120, "y": 60, "w": 600, "h": 500}
    img = Image.open(io.BytesIO(c.post(f"{url}/render", json={
        "left": res["lanes"][0]["bands"], "right": res["lanes"][1]["bands"],
        "crop": crop, "preview": True}).data))
    assert img.size[1] >= 500 and img.size[0] > 600
