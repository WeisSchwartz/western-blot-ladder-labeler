"""Synthetic western blots with known ladder positions, for tests and demos."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

KDA = (250, 150, 100, 75, 50, 37, 25, 20, 15, 10)
BLUE = np.array([45, 70, 190], float)
PINK = np.array([225, 85, 150], float)


@dataclass
class Synthetic:
    blot: np.ndarray  # HxWx3 uint8
    marker: np.ndarray | None
    lane_x: float  # first ladder lane
    truth: dict[float, float]  # kDa → y for the bands inside the frame
    lane_xs: list[float] = field(default_factory=list)  # every ladder lane
    membrane: tuple[float, float] | None = None  # (top, bottom) edge rows, if drawn


def ladder_y(kda: float, top: float, span: float) -> float:
    t = (np.log10(250) - np.log10(kda)) / (np.log10(250) - np.log10(10))
    return top + span * (t + 0.18 * t * (1 - t))  # mildly curved, like a real gel


def make_blot(H=700, W=900, n_lanes=8, ladder_lane=0, top_frac=0.08, span_frac=0.85,
              polarity="dark", ladder_style="gray", ladder="dual-color",
              separate_marker=False, noise=0.015, seed=0,
              membrane: tuple[float, float] | None = None) -> Synthetic:
    """ladder_style: 'gray' (same ink as samples) or 'color' (prestained colors).

    ladder_lane: an int, or a list of ints for several ladders.
    membrane: (top, bottom) as fractions of H. Rows outside are off-membrane, and a
    dark edge line is drawn at each boundary, like a real membrane edge.
    """
    rng = np.random.default_rng(seed)
    ladder_lanes = [ladder_lane] if isinstance(ladder_lane, int) else list(ladder_lane)
    yy = np.arange(H)[:, None]
    xx = np.arange(W)[None, :]
    pitch = W / (n_lanes + 0.5)
    lane_w = pitch * 0.62
    centers = [pitch * (i + 0.75) for i in range(n_lanes)]
    sigma = H / 260

    def band(xc, y, amp, sig=sigma):
        xs = np.clip((lane_w / 2 - np.abs(xx - xc)) / 3.0, 0, 1)  # soft lane edges
        smile = 0.002 * H * ((xx - xc) / (lane_w / 2)) ** 2
        return amp * np.exp(-0.5 * ((yy - y - smile) / sig) ** 2) * xs

    sample = np.zeros((H, W))
    for i, xc in enumerate(centers):
        if i in ladder_lanes:
            continue
        for _ in range(rng.integers(1, 4)):
            sample += band(xc, rng.uniform(0.1, 0.9) * H, rng.uniform(0.3, 1.0),
                           sig=sigma * rng.uniform(1, 2.5))
        smear = np.clip((lane_w / 2 - np.abs(xx - xc)) / 3.0, 0, 1)
        sample += 0.08 * smear * np.exp(-((yy - 0.5 * H) / (0.3 * H)) ** 2) * rng.uniform(0, 1)

    refs = {"dual-color": (75, 25), "all-blue": (75, 50, 25)}[ladder]
    ladder_amp = np.zeros((H, W))
    pink_amp = np.zeros((H, W))
    truth = {}
    for k in KDA:
        y = ladder_y(k, top_frac * H, span_frac * H)
        if not (0 <= y < H):
            continue
        truth[float(k)] = float(y)
        amp = 0.55
        if ladder == "all-blue" and k in refs:
            amp = 0.95
        for li in ladder_lanes:
            b = band(centers[li], y, amp)
            if ladder == "dual-color" and k in refs and ladder_style == "color":
                pink_amp += b
            else:
                ladder_amp += b

    edge = np.zeros((H, W))
    outside = np.zeros((H, 1), bool)
    mem = None
    if membrane is not None:
        mem = (membrane[0] * H, membrane[1] * H)
        outside = (yy < mem[0]) | (yy > mem[1])
        for yb in mem:
            edge += 0.45 * np.exp(-0.5 * ((yy - yb) / 2.0) ** 2) * np.ones((1, W))
        # Nothing transfers off the membrane.
        sample = np.where(outside, 0, sample)
        ladder_amp = np.where(outside, 0, ladder_amp)
        pink_amp = np.where(outside, 0, pink_amp)
        truth = {k: y for k, y in truth.items() if mem[0] + 3 < y < mem[1] - 3}

    def gray_image(signal):
        signal = np.clip(signal, 0, 1)
        if polarity == "dark":
            img = 0.92 - 0.8 * signal
            img = np.where(outside, 0.99, img)  # off-membrane is brighter
        else:
            img = 0.06 + 0.85 * signal
            img = np.where(outside, 0.0, img)
        img = img + rng.normal(0, noise, img.shape) + 0.03 * (xx / W)
        return np.repeat(np.clip(img, 0, 1)[..., None], 3, axis=2) * 255

    def colorize(base, blue_a, pink_a):
        a_b = np.clip(blue_a, 0, 1)[..., None]
        a_p = np.clip(pink_a, 0, 1)[..., None]
        out = base * (1 - a_b) + BLUE * a_b
        return out * (1 - a_p) + PINK * a_p

    marker = None
    if separate_marker:
        blot = gray_image(sample + edge)
        membrane_img = np.full((H, W, 3), 238.0) + rng.normal(0, 3, (H, W, 1))
        membrane_img -= 150 * edge[..., None]
        if ladder_style == "color":
            marker = colorize(membrane_img, ladder_amp * 1.4, pink_amp * 1.4)
        else:
            marker = membrane_img - 180 * np.clip(ladder_amp + pink_amp, 0, 1)[..., None]
    elif ladder_style == "color":
        blot = colorize(gray_image(sample + edge), ladder_amp * 1.4, pink_amp * 1.4)
    else:
        blot = gray_image(sample + ladder_amp + pink_amp + edge)

    to8 = lambda a: np.clip(np.round(a), 0, 255).astype(np.uint8)  # noqa: E731
    xs = [float(centers[i]) for i in ladder_lanes]
    return Synthetic(to8(blot), None if marker is None else to8(marker), xs[0], truth, xs, mem)
