"""Ladder lane detection, band peak finding, and kDa assignment.

Pipeline (see SPEC.md §4):
  signal map → detection region (top/bottom limits)
             → candidate lanes (column profile maxima)
             → band peaks per lane (row profile). Full-width lines such as
               membrane edges are rejected.
             → match peaks to ladder bands with log10(kDa) ~ migration model
             → choose the ladder lane(s) whose peaks best fit the ladder.

All internal work is done on a downscaled copy (longest side ≤ ANALYSIS_MAX_DIM).
The public results use original-image pixel coordinates.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image
from scipy import ndimage
from scipy import signal as sps

from .imaging import band_signal, chroma_map, pinkness_map
from .ladders import Ladder

ANALYSIS_MAX_DIM = 1200
MAX_CANDIDATE_LANES = 12
MAX_PEAKS_PER_LANE = 30
ANCHOR_SPAN = 3  # anchor pairs use peaks/bands at most this many indices apart
MATCH_TOLERANCE = 0.35  # fraction of the predicted gap to the nearest neighbor band
PINK_SPREAD_MIN = 0.12  # (R-B)/255 spread needed before pinkness counts as a cue
# A peak whose signal covers this fraction of the image width is a full-width
# line (membrane edge, gel-front line, scanner artefact), not a ladder band.
EDGE_WIDTH_FRACTION = 0.7
# Auto mode accepts an extra ladder lane only if its bands line up with the best one.
EXTRA_LADDER_MIN_SCORE_RATIO = 0.6
EXTRA_LADDER_SPACING_TOL = 0.025  # median spacing mismatch, fraction of region height
EXTRA_LADDER_OFFSET_TOL = 0.12  # allowed overall vertical shift (tilted membrane)


# ───────────────────────────── data classes ─────────────────────────────

@dataclass
class Peak:
    y: float  # analysis coordinates
    prominence: float
    pink: float | None = None


@dataclass
class Assignment:
    band_to_peak: dict[int, int]
    score: float
    coeffs: np.ndarray | None  # y = polyval(coeffs, log10(kDa)), analysis coords
    reference_cue_used: bool = False

    @property
    def n_matched(self) -> int:
        return len(self.band_to_peak)


EMPTY = Assignment({}, float("-inf"), None)


@dataclass
class DetectionResult:
    """One ladder lane."""
    lane_x: float
    lane_width: float
    bands: list[dict]  # {y, kda, prominence}, sorted top → bottom
    extra_peaks: list[float]  # detected peaks not matched to any ladder band
    edge_peaks: list[float]  # full-width lines that were ignored
    predicted: list[dict]  # {kda, y | None} predicted position of every ladder band
    score: float | None
    polarity: str
    color: bool
    reference_cue_used: bool
    candidates: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "lane_x": self.lane_x,
            "lane_width": self.lane_width,
            "bands": self.bands,
            "extra_peaks": self.extra_peaks,
            "edge_peaks": self.edge_peaks,
            "predicted": self.predicted,
            "score": self.score,
            "polarity": self.polarity,
            "color": self.color,
            "reference_cue_used": self.reference_cue_used,
            "candidates": self.candidates,
            "warnings": self.warnings,
        }


@dataclass
class LadderSet:
    """Every ladder lane found on one image."""
    lanes: list[DetectionResult]  # sorted left → right
    y_range: tuple[float, float]  # detection region actually used (original px)
    polarity: str
    color: bool
    candidates: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    auto_limits: bool = False

    def to_dict(self) -> dict:
        return {
            "lanes": [ln.to_dict() for ln in self.lanes],
            "y_range": list(self.y_range),
            "auto_limits": self.auto_limits,
            "notes": self.notes,
            "polarity": self.polarity,
            "color": self.color,
            "candidates": self.candidates,
            "warnings": self.warnings,
        }


# ───────────────────────────── preparation ─────────────────────────────

class Prepared:
    """Downscaled signal maps restricted to the detection region [lo, hi)."""

    def __init__(self, rgb: np.ndarray, polarity: str = "auto",
                 y_range: tuple[float, float] | None = None):
        H, W = rgb.shape[:2]
        s = min(1.0, ANALYSIS_MAX_DIM / max(H, W))
        if s < 1.0:
            w, h = max(1, round(W * s)), max(1, round(H * s))
            small = np.asarray(Image.fromarray(rgb).resize((w, h), Image.BOX))
        else:
            small = rgb
        self.H, self.W = H, W
        self.h, self.w = small.shape[:2]
        self.sx, self.sy = self.w / W, self.h / H

        self.lo, self.hi = 0, self.h
        if y_range is not None:
            top, bottom = sorted(float(v) for v in y_range)
            lo = int(np.floor(self.to_small_y(max(top, 0.0))))
            hi = int(np.ceil(self.to_small_y(min(bottom, H - 1.0)))) + 1
            lo, hi = max(0, lo), min(self.h, hi)
            if hi - lo >= 10:
                self.lo, self.hi = lo, hi

        raw, info = band_signal(small, polarity)
        self.polarity: str = info["polarity"]
        self.color: bool = info["color"]
        region = raw[self.lo:self.hi]
        # sig: 2-D background removed, for finding lanes.
        self.sig = np.zeros_like(raw)
        self.sig[self.lo:self.hi] = _subtract_background_2d(region)
        # vc: per-column vertical background removed, for band profiles and the
        # full-width (membrane edge) test.
        self.vc = np.zeros_like(raw)
        self.vc[self.lo:self.hi] = _vertical_contrast(region)
        # Column-block averages of vc (lower noise) for the full-width line test.
        n_blocks = int(np.clip(self.w // 4, 1, 48))
        cuts = np.linspace(0, self.w, n_blocks + 1).astype(int)
        region_vc = self.vc[self.lo:self.hi]
        blocks = np.stack([region_vc[:, a:b].mean(axis=1) for a, b in zip(cuts[:-1], cuts[1:])], axis=1)
        self.blocks = ndimage.gaussian_filter1d(blocks, max(1.0, self.h / 400), axis=0)
        self.pink = pinkness_map(small) if self.color else None
        self.chroma = chroma_map(small) if self.color else None

    @property
    def region_height(self) -> int:
        return self.hi - self.lo

    # coordinate helpers
    def to_orig_y(self, y: float) -> float:
        return (y + 0.5) / self.sy - 0.5

    def to_orig_x(self, x: float) -> float:
        return (x + 0.5) / self.sx - 0.5

    def to_small_y(self, y: float) -> float:
        return (y + 0.5) * self.sy - 0.5

    def to_small_x(self, x: float) -> float:
        return (x + 0.5) * self.sx - 0.5

    def orig_y_range(self) -> tuple[float, float]:
        return (round(max(0.0, self.to_orig_y(self.lo)), 1),
                round(min(self.H - 1.0, self.to_orig_y(self.hi - 1)), 1))


def _subtract_background_2d(sig: np.ndarray) -> np.ndarray:
    """Rolling-ball-like background removal: grey opening with a large window."""
    h, w = sig.shape
    size = (max(3, h // 8), max(3, w // 8))
    bg = ndimage.grey_opening(sig, size=size)
    bg = ndimage.uniform_filter(bg, size=size)
    return np.clip(sig - bg, 0, None).astype(np.float32)


def _vertical_contrast(sig: np.ndarray) -> np.ndarray:
    """Remove each column's slowly varying vertical background (smears, gradients)."""
    k = max(3, sig.shape[0] // 10)
    bg = ndimage.uniform_filter(ndimage.grey_opening(sig, size=(k, 1)), size=(k, 1))
    return np.clip(sig - bg, 0, None).astype(np.float32)


# ───────────────────────────── lanes & peaks ─────────────────────────────

def lane_candidates(prep: Prepared) -> list[tuple[float, float]]:
    """(x, width) of likely lanes in analysis coordinates, strongest first."""
    prof = prep.sig[prep.lo:prep.hi].mean(axis=0)
    prof = ndimage.gaussian_filter1d(prof, max(1.0, prep.w / 200))
    rng = float(prof.max() - prof.min())
    if rng <= 0:
        return []
    pk, props = sps.find_peaks(prof, prominence=0.05 * rng, distance=max(2, prep.w // 60))
    if len(pk) == 0:
        return []
    widths = sps.peak_widths(prof, pk, rel_height=0.5)[0]
    order = np.argsort(props["prominences"])[::-1][:MAX_CANDIDATE_LANES]
    return [(float(pk[i]), _clip_width(widths[i], prep.w)) for i in order]


def _clip_width(width: float, w: int) -> float:
    return float(np.clip(width, max(3.0, w / 150), w / 4))


def lane_width_at(prep: Prepared, x: float) -> float:
    cands = lane_candidates(prep)
    if cands:
        nearest = min(cands, key=lambda c: abs(c[0] - x))
        if abs(nearest[0] - x) <= max(nearest[1], prep.w / 30):
            return nearest[1]
        return float(np.median([c[1] for c in cands]))
    return _clip_width(prep.w / 25, prep.w)


def _lane_columns(prep: Prepared, x: float, width: float) -> tuple[int, int]:
    half = max(1, int(round(width * 0.3)))
    xc = int(round(x))
    x0, x1 = max(0, xc - half), min(prep.w, xc + half + 1)
    if x1 <= x0:
        x0, x1 = max(0, min(prep.w - 1, xc)), max(1, min(prep.w, xc + 1))
    return x0, x1


def _region_profile(prep: Prepared, x0: int, x1: int) -> np.ndarray:
    """Smoothed lane profile over the detection region (index 0 = row prep.lo)."""
    p = prep.vc[prep.lo:prep.hi, x0:x1].mean(axis=1).astype(np.float64)
    return ndimage.gaussian_filter1d(p, max(0.8, prep.h / 600))


def _is_full_width(prep: Prepared, y: int, value: float, width: float) -> bool:
    """True if the signal at row y spans most of the image width, like a membrane edge."""
    if prep.w < 4 * width or value <= 0:
        return False  # image is little more than the lane itself; the test is meaningless
    r = max(2, prep.h // 60)  # tolerate a slightly tilted edge
    y0, y1 = max(0, y - prep.lo - r), min(prep.region_height, y - prep.lo + r + 1)
    if y1 <= y0:
        return False
    rows = prep.blocks[y0:y1].max(axis=0)
    return float((rows > 0.4 * value).mean()) >= EDGE_WIDTH_FRACTION


def find_band_peaks(prep: Prepared, x: float, width: float) -> tuple[list[Peak], list[float]]:
    """Band peaks in a lane plus the y of rejected full-width lines (analysis coords)."""
    x0, x1 = _lane_columns(prep, x, width)
    raw = prep.vc[prep.lo:prep.hi, x0:x1].mean(axis=1).astype(np.float64)
    if len(raw) < 5:
        return [], []
    d = np.diff(raw)
    noise = 1.4826 * np.median(np.abs(d - np.median(d))) / np.sqrt(2) + 1e-6
    p = ndimage.gaussian_filter1d(raw, max(0.8, prep.h / 600))
    if p.max() <= 0:
        return [], []
    prom_min = max(3 * noise, 0.03 * float(p.max()))
    pk, props = sps.find_peaks(p, prominence=prom_min, distance=max(2, prep.h // 200))
    peaks, edges = [], []
    for i in np.argsort(props["prominences"])[::-1]:
        yi = int(pk[i])
        y = prep.lo + _subpixel(p, yi)
        if _is_full_width(prep, prep.lo + yi, float(p[yi]), width):
            edges.append(y)
        elif len(peaks) < MAX_PEAKS_PER_LANE:
            peaks.append(Peak(y=y, prominence=float(props["prominences"][i]),
                              pink=_pink_at(prep, prep.lo + yi, x0, x1)))
    peaks.sort(key=lambda pk_: pk_.y)
    return peaks, sorted(edges)


def peaks_at_positions(prep: Prepared, x: float, width: float, ys_small: list[float]) -> list[Peak]:
    """Build Peak features at user-specified positions (for re-assignment)."""
    x0, x1 = _lane_columns(prep, x, width)
    p = _region_profile(prep, x0, x1)
    out = []
    for y in sorted(ys_small):
        yi = int(np.clip(round(y), 0, prep.h - 1))
        idx = yi - prep.lo
        val = p[idx] if 0 <= idx < len(p) else 0.0
        out.append(Peak(y=float(y), prominence=float(max(val, 1e-6)), pink=_pink_at(prep, yi, x0, x1)))
    return out


def _subpixel(p: np.ndarray, i: int) -> float:
    if 0 < i < len(p) - 1:
        a, b, c = p[i - 1], p[i], p[i + 1]
        den = a - 2 * b + c
        if den < 0:
            return i + float(np.clip(0.5 * (a - c) / den, -0.5, 0.5))
    return float(i)


def _pink_at(prep: Prepared, y: int, x0: int, x1: int) -> float | None:
    if prep.pink is None:
        return None
    r = max(1, prep.h // 300)
    y0, y1 = max(0, y - r), min(prep.h, y + r + 1)
    wts = prep.chroma[y0:y1, x0:x1]
    if wts.sum() <= 1e-6:
        return None
    return float((prep.pink[y0:y1, x0:x1] * wts).sum() / wts.sum())


# ───────────────────────────── assignment ─────────────────────────────

def _local_gap(pred: np.ndarray) -> np.ndarray:
    d = np.abs(np.diff(pred))
    return np.minimum(np.r_[np.inf, d], np.r_[d, np.inf])


def _match(pred: np.ndarray, ys: np.ndarray, lo: float, hi: float) -> dict[int, int]:
    """One-to-one, order-preserving nearest matching of predicted band y to peaks."""
    tol = np.maximum(MATCH_TOLERANCE * _local_gap(pred), 1.0)
    inframe = (pred >= lo - tol) & (pred <= hi - 1 + tol)
    D = np.abs(pred[:, None] - ys[None, :])
    ok = (D <= tol[:, None]) & inframe[:, None]
    pairs = np.argwhere(ok)
    if len(pairs) == 0:
        return {}
    pairs = pairs[np.argsort(D[ok], kind="stable")]
    used_b, used_p, m = set(), set(), {}
    for b, p in pairs:
        if b not in used_b and p not in used_p:
            m[int(b)] = int(p)
            used_b.add(b)
            used_p.add(p)
    out, last = {}, -1
    for b in sorted(m):
        if m[b] > last:
            out[b] = m[b]
            last = m[b]
    return out


def _fit(L: np.ndarray, y: np.ndarray, L_all: np.ndarray) -> np.ndarray:
    if len(y) >= 5:
        c = np.polyfit(L, y, 2)
        lo, hi = L_all.min(), L_all.max()
        deriv = 2 * c[0] * np.array([lo, hi]) + c[1]
        if np.all(deriv < 0):  # y must decrease as MW increases across the ladder
            return c
    return np.polyfit(L, y, 1)


def _reference_bonus(match: dict[int, int], peaks: list[Peak], ladder: Ladder) -> tuple[float, bool]:
    refs = set(ladder.reference_kda)
    kda = ladder.bands_kda
    if ladder.reference_cue == "color_pink":
        vals = [(k, peaks[p].pink) for k, p in match.items() if peaks[p].pink is not None]
        if len(vals) < 3:
            return 0.0, False
        pinks = np.array([v for _, v in vals])
        if pinks.max() - pinks.min() < PINK_SPREAD_MIN:
            return 0.0, False
        thr = (pinks.max() + pinks.min()) / 2
        bonus = 0.0
        for k, v in vals:
            is_ref, is_pink = kda[k] in refs, v > thr
            if is_ref:
                bonus += 1.0 if is_pink else -1.0
            elif is_pink:
                bonus -= 1.0
        return bonus, True
    if ladder.reference_cue == "intensity":
        bonus, used = 0.0, False
        for k, p in match.items():
            if kda[k] not in refs:
                continue
            nb = [peaks[match[j]].prominence for j in (k - 1, k + 1)
                  if j in match and kda[j] not in refs]
            if nb:
                r = peaks[p].prominence / max(np.mean(nb), 1e-9)
                bonus += 0.6 * float(np.clip(np.log2(r), -1, 1))
                used = True
        return bonus, used
    return 0.0, False


def _score(match: dict[int, int], coeffs: np.ndarray, L: np.ndarray, peaks: list[Peak],
           ys: np.ndarray, lo: float, hi: float, ladder: Ladder) -> tuple[float, bool]:
    bands = np.array(sorted(match))
    pidx = np.array([match[b] for b in bands])
    pred = np.polyval(coeffs, L)
    gap = np.maximum(_local_gap(pred), 1.0)
    resid = (ys[pidx] - pred[bands]) / gap[bands]
    rms = float(np.sqrt(np.mean(resid ** 2)))

    unmatched_bands = [k for k in range(len(L)) if k not in match]
    missing = sum(1 for k in unmatched_bands if lo <= pred[k] <= hi - 1)

    proms = np.array([p.prominence for p in peaks])
    med = float(np.median(proms[pidx]))
    y_lo, y_hi = ys[pidx].min(), ys[pidx].max()
    extra = 0.0
    matched_peaks = set(pidx.tolist())
    for i, yv in enumerate(ys):
        if i not in matched_peaks and y_lo < yv < y_hi:
            extra += min(1.0, proms[i] / max(med, 1e-9))

    bonus, cue = _reference_bonus(match, peaks, ladder)
    score = len(match) - 0.35 * missing - 0.6 * extra - 3.0 * rms + bonus
    return score, cue


def assign_ladder(peaks: list[Peak], ladder: Ladder, lo: float, hi: float) -> Assignment:
    """Find the best assignment of detected peaks to ladder bands within rows [lo, hi)."""
    if len(peaks) < 2:
        return EMPTY
    L = np.log10(np.array(ladder.bands_kda, dtype=float))  # descending
    ys = np.array([p.y for p in peaks])
    n, m = len(L), len(ys)

    seen: dict[tuple, dict[int, int]] = {}
    for i in range(m):
        for j in range(i + 1, min(m, i + ANCHOR_SPAN + 1)):
            for a in range(n):
                for b in range(a + 1, min(n, a + ANCHOR_SPAN + 1)):
                    slope = (ys[j] - ys[i]) / (L[b] - L[a])
                    pred = ys[i] + (L - L[a]) * slope
                    match = _match(pred, ys, lo, hi)
                    if len(match) >= 2:
                        seen.setdefault(tuple(sorted(match.items())), match)

    best = EMPTY
    refined_seen: set[tuple] = set()
    for match in seen.values():
        for _ in range(2):  # fit → rematch → fit
            bk = np.array(sorted(match))
            coeffs = _fit(L[bk], ys[[match[b] for b in bk]], L)
            new = _match(np.polyval(coeffs, L), ys, lo, hi)
            if len(new) < 2 or new == match:
                break
            match = new
        key = tuple(sorted(match.items()))
        if key in refined_seen:
            continue
        refined_seen.add(key)
        bk = np.array(sorted(match))
        coeffs = _fit(L[bk], ys[[match[b] for b in bk]], L)
        score, cue = _score(match, coeffs, L, peaks, ys, lo, hi, ladder)
        if score > best.score:
            best = Assignment(dict(match), score, coeffs, cue)
    return best


# ───────────────────────────── lane selection ─────────────────────────────

@dataclass
class _Lane:
    x: float
    width: float
    peaks: list[Peak]
    edges: list[float]
    asg: Assignment


def _evaluate_lane(prep: Prepared, x: float, width: float, ladder: Ladder) -> _Lane:
    peaks, edges = find_band_peaks(prep, x, width)
    return _Lane(x, width, peaks, edges, assign_ladder(peaks, ladder, prep.lo, prep.hi))


def _refine_lane(prep: Prepared, lane: _Lane, ladder: Ladder) -> _Lane:
    """Re-centre the lane on the matched bands' horizontal extent and re-evaluate."""
    if lane.asg.n_matched < 2:
        return lane
    rows = sorted({int(round(lane.peaks[p].y)) for p in lane.asg.band_to_peak.values()})
    xa = int(max(0, np.floor(lane.x - 1.5 * lane.width)))
    xb = int(min(prep.w, np.ceil(lane.x + 1.5 * lane.width) + 1))
    if xb - xa < 3:
        return lane
    stack = [prep.vc[max(0, r - 1):r + 2, xa:xb].mean(axis=0) for r in rows]
    prof = ndimage.gaussian_filter1d(np.mean(stack, axis=0), 1.0)
    c0 = int(np.clip(round(lane.x - lane.width / 2) - xa, 0, len(prof) - 1))
    c1 = int(np.clip(round(lane.x + lane.width / 2) - xa, c0 + 1, len(prof)))
    peak = c0 + int(np.argmax(prof[c0:c1]))
    half = 0.5 * prof[peak]
    if half <= 0:
        return lane
    left, right = peak, peak
    while left > 0 and prof[left - 1] > half:
        left -= 1
    while right < len(prof) - 1 and prof[right + 1] > half:
        right += 1
    x = xa + (left + right) / 2
    width = _clip_width((right - left + 1) * 1.1, prep.w)
    if abs(x - lane.x) < 0.5 and abs(width - lane.width) < 1:
        return lane
    new = _evaluate_lane(prep, x, width, ladder)
    return new if new.asg.score >= lane.asg.score - 0.5 else lane


def _membrane_limits(prep: Prepared, lanes: list[_Lane]) -> tuple[float, float] | None:
    """Detection limits just inside full-width lines near the top/bottom (membrane edges)."""
    tol = max(2, prep.h // 60)
    all_edges = sorted(y for ln in lanes for y in ln.edges)
    if not all_edges:
        return None
    # Keep lines seen in at least two lanes (or the only lane evaluated).
    need = 2 if len(lanes) > 1 else 1
    edges = [y for y in all_edges if sum(abs(y - o) <= tol for o in all_edges) >= need]
    band = 0.4 * prep.region_height
    tops = [y for y in edges if y < prep.lo + band]
    bottoms = [y for y in edges if y > prep.hi - band]
    if not tops and not bottoms:
        return None
    margin = max(2.0, prep.h / 100)
    lo = max(tops) + margin if tops else prep.lo
    hi = min(bottoms) - margin if bottoms else prep.hi - 1
    if hi - lo < 0.3 * prep.region_height:
        return None
    return (max(0.0, prep.to_orig_y(lo)), min(prep.H - 1.0, prep.to_orig_y(hi)))


def _consistent(a: _Lane, b: _Lane, prep: Prepared) -> bool:
    """Do two lanes put the same kDa bands at (nearly) the same heights?"""
    common = sorted(set(a.asg.band_to_peak) & set(b.asg.band_to_peak))
    if len(common) < 3:
        return False
    ya = np.array([a.peaks[a.asg.band_to_peak[k]].y for k in common])
    yb = np.array([b.peaks[b.asg.band_to_peak[k]].y for k in common])
    diff = ya - yb
    offset = float(np.median(diff))
    h = prep.region_height
    return (abs(offset) <= EXTRA_LADDER_OFFSET_TOL * h and
            float(np.median(np.abs(diff - offset))) <= EXTRA_LADDER_SPACING_TOL * h)


def _select_ladders(evaluated: list[_Lane], n_ladders: int | None, prep: Prepared) -> list[_Lane]:
    valid = [e for e in evaluated if np.isfinite(e.asg.score) and e.asg.n_matched >= 2]
    if not valid:
        return [evaluated[0]]
    best = valid[0]
    chosen = [best]
    for e in valid[1:]:
        if n_ladders is not None and len(chosen) >= n_ladders:
            break
        if any(abs(e.x - c.x) < max(e.width, c.width) for c in chosen):
            continue
        if n_ladders is None:  # auto: only accept convincing, consistent ladders
            if (e.asg.n_matched < 4
                    or e.asg.score < max(3.0, EXTRA_LADDER_MIN_SCORE_RATIO * best.asg.score)
                    or not _consistent(e, best, prep)):
                continue
        chosen.append(e)
    return chosen


# ───────────────────────────── public API ─────────────────────────────

def detect_ladders(rgb: np.ndarray, ladder: Ladder, polarity: str = "auto",
                   y_range: tuple[float, float] | None = None,
                   lanes: list[dict] | None = None,
                   n_ladders: int | None = None) -> LadderSet:
    """Find ladder lane(s) and label their bands.

    lanes:     evaluate exactly these lanes ({x, width?}, original px) instead of searching.
    n_ladders: None = auto (all convincing ladders), otherwise the number to return.
    y_range:   (top, bottom) detection region in original px; None = whole image.
    """
    prep = Prepared(rgb, polarity, y_range)
    auto_limits = False

    def evaluate_given(p: Prepared) -> list[_Lane]:
        out = []
        for ln in lanes:
            xs = p.to_small_x(float(ln["x"]))
            width = float(ln["width"]) * p.sx if ln.get("width") else lane_width_at(p, xs)
            out.append(_evaluate_lane(p, xs, _clip_width(width, p.w), ladder))
        return out

    def evaluate_search(p: Prepared) -> list[_Lane]:
        cands = lane_candidates(p) or [(p.w * 0.05, _clip_width(p.w / 25, p.w))]
        out = [_evaluate_lane(p, x, w, ladder) for x, w in cands]
        return sorted(out, key=lambda e: e.asg.score, reverse=True)

    evaluate = evaluate_given if lanes else evaluate_search
    evaluated = evaluate(prep)
    if y_range is None:
        limits = _membrane_limits(prep, evaluated)
        if limits is not None:
            prep = Prepared(rgb, polarity, limits)
            evaluated = evaluate(prep)
            auto_limits = True

    if lanes:
        chosen = evaluated
        candidates = []
    else:
        chosen = [_refine_lane(prep, e, ladder) for e in _select_ladders(evaluated, n_ladders, prep)]
        chosen.sort(key=lambda e: e.x)
        candidates = [
            {"x": round(prep.to_orig_x(e.x), 2), "width": round(e.width / prep.sx, 2),
             "score": (round(e.asg.score, 3) if np.isfinite(e.asg.score) else None),
             "n_bands": e.asg.n_matched}
            for e in evaluated
        ]

    results = [_build_result(prep, ladder, e) for e in chosen]
    for r in results:
        r.warnings = _lane_warnings(r, ladder)
    out = LadderSet(lanes=results, y_range=prep.orig_y_range(), polarity=prep.polarity,
                    color=prep.color, candidates=candidates, auto_limits=auto_limits)
    if auto_limits:
        out.notes.append("Membrane edges were detected, and the top/bottom detection limits were "
                         "set just inside them. Drag the limit lines to change them.")
    if not lanes and n_ladders == 1 and len(evaluated) > 1:
        a, b = evaluated[0], evaluated[1]
        if (np.isfinite(b.asg.score) and a.asg.score - b.asg.score < 1.0
                and abs(a.x - b.x) > a.width and b.asg.n_matched >= 4):
            out.warnings.append(
                f"Another lane (x≈{prep.to_orig_x(b.x):.0f} px) scored almost as well. Make sure "
                "the highlighted lane is the ladder, or detect more than one ladder.")
    return out


def detect(rgb: np.ndarray, ladder: Ladder, polarity: str = "auto",
           lane_x: float | None = None,
           y_range: tuple[float, float] | None = None) -> DetectionResult:
    """Single-ladder convenience wrapper around detect_ladders()."""
    lanes = [{"x": lane_x}] if lane_x is not None else None
    res = detect_ladders(rgb, ladder, polarity, y_range, lanes=lanes, n_ladders=1)
    first = res.lanes[0]
    first.candidates = res.candidates
    first.warnings = res.warnings + first.warnings
    return first


def reassign(rgb: np.ndarray, ladder: Ladder, lane_x: float, ys: list[float],
             polarity: str = "auto", lane_width: float | None = None,
             y_range: tuple[float, float] | None = None) -> DetectionResult:
    """Assign kDa values to user-supplied band positions (original px)."""
    prep = Prepared(rgb, polarity, y_range)
    xs = prep.to_small_x(float(lane_x))
    width = lane_width * prep.sx if lane_width else lane_width_at(prep, xs)
    peaks = peaks_at_positions(prep, xs, width, [prep.to_small_y(y) for y in ys])
    # User-placed bands are allowed outside the detection region.
    lo = min([prep.lo] + [int(np.floor(p.y)) for p in peaks])
    hi = max([prep.hi] + [int(np.ceil(p.y)) + 1 for p in peaks])
    asg = assign_ladder(peaks, ladder, lo, hi)
    result = _build_result(prep, ladder, _Lane(xs, width, peaks, [], asg), keep_unmatched=True)
    if asg.n_matched < len(peaks):
        result.warnings.append(
            f"{len(peaks) - asg.n_matched} band(s) did not fit the ladder pattern and were "
            "left unlabeled. Set their kDa by hand or delete them.")
    return result


def _build_result(prep: Prepared, ladder: Ladder, lane: _Lane,
                  keep_unmatched: bool = False) -> DetectionResult:
    asg = lane.asg
    peak_to_band = {p: b for b, p in asg.band_to_peak.items()}
    bands, extra = [], []
    for i, pk in enumerate(lane.peaks):
        y = round(prep.to_orig_y(pk.y), 2)
        if i in peak_to_band:
            bands.append({"y": y, "kda": ladder.bands_kda[peak_to_band[i]],
                          "prominence": round(pk.prominence, 5)})
        elif keep_unmatched:
            bands.append({"y": y, "kda": None, "prominence": round(pk.prominence, 5)})
        else:
            extra.append(y)
    # Predictions cover the whole image, not just the detection region, so a band
    # just outside the limits still shows as a clickable ghost.
    predicted = []
    for kda in ladder.bands_kda:
        yp = None
        if asg.coeffs is not None:
            v = prep.to_orig_y(float(np.polyval(asg.coeffs, np.log10(kda))))
            yp = round(v, 2) if -0.5 <= v <= prep.H - 0.5 else None
        predicted.append({"kda": kda, "y": yp})
    return DetectionResult(
        lane_x=round(prep.to_orig_x(lane.x), 2), lane_width=round(lane.width / prep.sx, 2),
        bands=bands, extra_peaks=extra,
        edge_peaks=[round(prep.to_orig_y(e), 2) for e in lane.edges], predicted=predicted,
        score=float(asg.score) if np.isfinite(asg.score) else None,
        polarity=prep.polarity, color=prep.color, reference_cue_used=asg.reference_cue_used,
    )


def _lane_warnings(result: DetectionResult, ladder: Ladder) -> list[str]:
    w = []
    n = len(result.bands)
    if n == 0:
        w.append("No ladder bands found in this lane. Drag the lane onto the ladder, adjust the "
                 "top/bottom limits, or check the polarity setting.")
        return w
    if n < 4:
        w.append(f"Only {n} ladder band(s) matched. Check the lane and band positions.")
    if ladder.reference_cue and not result.reference_cue_used:
        cue = {"color_pink": "pink reference bands", "intensity": "brighter reference bands"}
        w.append(f"The {cue.get(ladder.reference_cue, 'reference-band')} "
                 f"({', '.join(f'{k:g}' for k in ladder.reference_kda)} kDa) could not be used. "
                 "Labels are based on band spacing only, so check them. The most likely error is "
                 "every label shifted by one band.")
    return w


# ───────────────────────────── sample lanes (for lane labels) ─────────────────────────────

def find_sample_lanes(rgb: np.ndarray, polarity: str = "auto",
                      y_range: tuple[float, float] | None = None,
                      ladder_xs: list[float] | None = None) -> list[dict]:
    """Every lane on the blot, left → right: [{x, width, is_ladder}] in original px.

    Lanes come from the column profile of band contrast. Each lane is centred on
    the half-maximum extent of its profile peak. Missing lanes (empty lanes with
    no signal) are filled in when a gap is a whole multiple of the typical lane spacing.
    """
    prep = Prepared(rgb, polarity, y_range)
    prof = prep.vc[prep.lo:prep.hi].mean(axis=0).astype(np.float64)
    prof = ndimage.gaussian_filter1d(prof, max(1.0, prep.w / 300))
    rng = float(prof.max() - prof.min())
    lanes: list[tuple[float, float, float]] = []  # (x, width, prominence)
    if rng > 0:
        pk, props = sps.find_peaks(prof, prominence=0.04 * rng, distance=max(2, prep.w // 80))
        if len(pk):
            _, _, left, right = sps.peak_widths(prof, pk, rel_height=0.5)
            lanes = [((lo + hi) / 2, _clip_width(hi - lo, prep.w), float(pr))
                     for lo, hi, pr in zip(left, right, props["prominences"])]
    xs_ladder = [prep.to_small_x(x) for x in (ladder_xs or [])]
    typical_w = float(np.median([w for _, w, _ in lanes])) if lanes else prep.w / 25
    for lx in xs_ladder:  # ladder lanes are lanes too; use their known centre
        lanes = [ln for ln in lanes if abs(ln[0] - lx) >= 0.6 * typical_w]
        lanes.append((lx, typical_w, float("inf")))
    lanes.sort()

    if len(lanes) >= 3:
        pitch = float(np.median(np.diff([x for x, _, _ in lanes])))
        merged: list[tuple[float, float, float]] = []  # too close to be separate lanes → keep stronger
        for ln in lanes:
            if merged and ln[0] - merged[-1][0] < 0.55 * pitch:
                if ln[2] > merged[-1][2]:
                    merged[-1] = ln
            else:
                merged.append(ln)
        lanes = merged
    if len(lanes) >= 3:
        pitch = float(np.median(np.diff([x for x, _, _ in lanes])))
        filled = [lanes[0]]
        for ln in lanes[1:]:
            gap = ln[0] - filled[-1][0]
            k = int(round(gap / pitch))
            if k >= 2 and abs(gap - k * pitch) < 0.3 * pitch:
                x0 = filled[-1][0]
                filled.extend((x0 + gap * j / k, typical_w, 0.0) for j in range(1, k))
            filled.append(ln)
        lanes = filled

    out = []
    for x, w, _ in lanes:
        is_ladder = any(abs(x - lx) < 1e-6 for lx in xs_ladder)
        out.append({"x": round(float(ladder_xs[xs_ladder.index(x)]) if is_ladder else prep.to_orig_x(x), 2),
                    "width": round(w / prep.sx, 2), "is_ladder": is_ladder})
    return out
