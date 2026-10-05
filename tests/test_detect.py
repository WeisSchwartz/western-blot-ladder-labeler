import numpy as np
import pytest
from synth import make_blot

from blot_ladder.detect import detect, detect_ladders, reassign
from blot_ladder.ladders import get_ladder

CASES = {
    "full ladder": dict(),
    "cropped 150-15": dict(top_frac=-0.12, span_frac=1.15, seed=1),
    "light on dark": dict(polarity="light", seed=2),
    "ladder on right": dict(ladder_lane=7, seed=3),
    "dual color on gray blot": dict(ladder_style="color", seed=4),
    "dual color cropped": dict(ladder_style="color", top_frac=-0.3, span_frac=1.25, seed=5),
    "separate marker": dict(ladder_style="color", separate_marker=True, seed=6),
    "all blue colored": dict(ladder="all-blue", ladder_style="color", seed=7),
    "all blue gray cropped": dict(ladder="all-blue", top_frac=-0.2, span_frac=1.2, seed=8),
    "noisy": dict(noise=0.05, seed=9),
    "large image": dict(H=2400, W=3000, seed=10),
}


def check(syn, result, H):
    assert abs(result.lane_x - syn.lane_x) < 0.03 * syn.blot.shape[1]
    got = {b["kda"]: b["y"] for b in result.bands}
    for kda, y in syn.truth.items():
        if 0.01 * H < y < 0.99 * H:  # bands sliced by the frame edge may be missed
            assert kda in got, f"{kda} kDa not found"
    for kda, y in got.items():
        assert kda in syn.truth, f"labelled {kda} kDa, which is out of frame"
        assert abs(y - syn.truth[kda]) < 0.015 * H, f"{kda} kDa misplaced"


@pytest.mark.parametrize("name", CASES)
def test_detect_cases(name):
    kw = CASES[name]
    syn = make_blot(**kw)
    img = syn.marker if syn.marker is not None else syn.blot
    result = detect(img, get_ladder(kw.get("ladder", "dual-color")))
    check(syn, result, img.shape[0])


def test_reference_cue_used_with_color():
    syn = make_blot(ladder_style="color", seed=4)
    r = detect(syn.blot, get_ladder("dual-color"))
    assert r.reference_cue_used and r.color
    assert not any("could not be used" in w for w in r.warnings)


def test_grayscale_dual_color_warns_about_missing_cue():
    syn = make_blot(seed=0)
    r = detect(syn.blot, get_ladder("dual-color"))
    assert any("shifted by one" in w for w in r.warnings)


def test_manual_lane_x():
    syn = make_blot(ladder_lane=3, seed=12)
    r = detect(syn.blot, get_ladder("dual-color"), lane_x=syn.lane_x + 3)
    check(syn, r, syn.blot.shape[0])
    assert r.candidates == []  # no lane search when the lane is given


def test_random_sweep_never_mislabels():
    rng = np.random.default_rng(123)
    for seed in range(15):
        kw = dict(top_frac=rng.uniform(-0.35, 0.1), span_frac=rng.uniform(0.9, 1.5),
                  noise=rng.uniform(0.01, 0.05), ladder_lane=int(rng.choice([0, 1, 6, 7])),
                  ladder_style=str(rng.choice(["gray", "color"])), seed=seed,
                  polarity=str(rng.choice(["dark", "light"])))
        syn = make_blot(**kw)
        check(syn, detect(syn.blot, get_ladder("dual-color")), syn.blot.shape[0])


def test_reassign_user_positions():
    syn = make_blot(seed=13)
    ys = [y for k, y in syn.truth.items() if k not in (250.0,)]
    r = reassign(syn.blot, get_ladder("dual-color"), syn.lane_x, ys)
    got = {b["kda"]: b["y"] for b in r.bands}
    for kda, y in syn.truth.items():
        if kda != 250.0:
            assert abs(got[kda] - y) < 1.0


def test_blank_image_is_graceful():
    r = detect(np.full((300, 400, 3), 240, np.uint8), get_ladder("dual-color"))
    assert r.bands == [] and r.warnings


MEMBRANE_CASES = {
    "edges, dark bands": dict(membrane=(0.05, 0.93), seed=1),
    "edges crop the ladder": dict(membrane=(0.12, 0.9), top_frac=0.0, span_frac=1.1, seed=2),
    "edges, light bands": dict(membrane=(0.06, 0.95), polarity="light", seed=3),
    "edges, marker image": dict(membrane=(0.04, 0.97), separate_marker=True, seed=4),
    "edges, noisy": dict(membrane=(0.15, 0.8), top_frac=-0.1, span_frac=1.2, noise=0.04, seed=6),
}


@pytest.mark.parametrize("name", MEMBRANE_CASES)
def test_membrane_edges_are_not_bands(name):
    syn = make_blot(**MEMBRANE_CASES[name])
    img = syn.marker if syn.marker is not None else syn.blot
    res = detect_ladders(img, get_ladder("dual-color"))
    check(syn, res.lanes[0], img.shape[0])
    top, bottom = res.y_range
    assert res.auto_limits
    assert syn.membrane[0] < top < syn.membrane[0] + 0.03 * img.shape[0]
    assert syn.membrane[1] - 0.03 * img.shape[0] < bottom < syn.membrane[1]


def test_manual_limits_exclude_edge_line():
    syn = make_blot(membrane=(0.05, 0.93), seed=1)
    m0, m1 = syn.membrane
    res = detect_ladders(syn.blot, get_ladder("dual-color"), y_range=(m0 + 6, m1 - 6))
    assert not res.auto_limits
    check(syn, res.lanes[0], syn.blot.shape[0])


def test_narrow_ladder_strip_is_not_rejected_as_edges():
    syn = make_blot(seed=0)
    strip = syn.blot[:, :160]  # only the ladder lane and a bit of the next lane
    r = detect(strip, get_ladder("dual-color"))
    assert len(r.bands) >= 9


MULTI_CASES = {
    "left and right": dict(ladder_lane=[0, 7]),
    "left and middle, colored": dict(ladder_lane=[0, 4], ladder_style="color", seed=3),
    "cropped pair": dict(ladder_lane=[1, 6], top_frac=-0.2, span_frac=1.2, seed=5),
    "three ladders": dict(ladder_lane=[0, 4, 7], n_lanes=9, seed=6),
    "pair with membrane edges": dict(ladder_lane=[0, 7], membrane=(0.08, 0.9), seed=8),
}


@pytest.mark.parametrize("name", MULTI_CASES)
def test_multiple_ladders(name):
    syn = make_blot(**MULTI_CASES[name])
    res = detect_ladders(syn.blot, get_ladder("dual-color"))
    assert len(res.lanes) == len(syn.lane_xs)
    for got, x in zip(res.lanes, syn.lane_xs):
        assert abs(got.lane_x - x) < 3
        check(Synthetic_at(syn, x), got, syn.blot.shape[0])


def Synthetic_at(syn, x):
    """The same synthetic blot, viewed as if `x` were its only ladder lane."""
    from dataclasses import replace
    return replace(syn, lane_x=x)


def test_fixed_ladder_count():
    syn = make_blot(ladder_lane=[0, 7])
    one = detect_ladders(syn.blot, get_ladder("dual-color"), n_ladders=1)
    assert len(one.lanes) == 1


def test_auto_mode_finds_no_false_extra_ladders():
    rng = np.random.default_rng(7)
    for seed in range(20):
        kw = dict(top_frac=rng.uniform(-0.35, 0.1), span_frac=rng.uniform(0.9, 1.5),
                  noise=rng.uniform(0.01, 0.05), ladder_lane=int(rng.choice([0, 1, 6, 7])),
                  ladder_style=str(rng.choice(["gray", "color"])), seed=seed,
                  polarity=str(rng.choice(["dark", "light"])))
        syn = make_blot(**kw)
        assert len(detect_ladders(syn.blot, get_ladder("dual-color")).lanes) == 1, kw
