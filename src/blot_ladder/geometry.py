"""Whole-image rotation (SPEC.md §4a).

Conventions (shared with the web UI's JavaScript, which maps points the same way):
- `angle` is in degrees, positive = clockwise. The image is rotated about its
  centre and the canvas expands to hold the whole rotated image.
Rotation interpolates pixels, so it changes pixel values slightly. It is a
uniform transform of the whole image. Local warps, such as straightening
"smiling" bands, are deliberately not offered: they alter the relative
positions of bands, which is inappropriate image manipulation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import ndimage

from .imaging import band_signal


@dataclass(frozen=True)
class Geometry:
    angle: float = 0.0

    @property
    def is_identity(self) -> bool:
        return abs(self.angle) < 1e-6


def rotated_size(width: int, height: int, angle: float) -> tuple[int, int]:
    """Canvas size after rotating by `angle` with expansion (matches Pillow)."""
    if abs(angle) < 1e-6:
        return width, height
    # Pillow rounds the rotated corner extents its own way; ask it directly.
    return Image.new("L", (width, height)).rotate(-angle, expand=True).size


def fill_color(rgb: np.ndarray) -> tuple[int, int, int]:
    """Median colour of the image border, used for the empty corners."""
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    return tuple(int(v) for v in np.median(border, axis=0))


def apply(rgb: np.ndarray, geom: Geometry, fill: tuple[int, int, int] | None = None) -> np.ndarray:
    if geom.is_identity:
        return rgb
    out = Image.fromarray(rgb).rotate(-geom.angle, resample=Image.BICUBIC, expand=True,
                                      fillcolor=fill or fill_color(rgb))
    return np.ascontiguousarray(np.asarray(out))


def forward(x: float, y: float, size0: tuple[int, int], geom: Geometry) -> tuple[float, float]:
    """Map a point in the original image to the rotated image."""
    W0, H0 = size0
    W1, H1 = rotated_size(W0, H0, geom.angle)
    a = math.radians(geom.angle)
    dx, dy = x - W0 / 2, y - H0 / 2
    return (dx * math.cos(a) - dy * math.sin(a) + W1 / 2,
            dx * math.sin(a) + dy * math.cos(a) + H1 / 2)


def inverse(x: float, y: float, size0: tuple[int, int], geom: Geometry) -> tuple[float, float]:
    """Map a point in the rotated image back to the original image."""
    W0, H0 = size0
    W1, H1 = rotated_size(W0, H0, geom.angle)
    a = math.radians(geom.angle)
    dx, dy = x - W1 / 2, y - H1 / 2
    return (dx * math.cos(a) + dy * math.sin(a) + W0 / 2,
            -dx * math.sin(a) + dy * math.cos(a) + H0 / 2)


def auto_straighten(images: list[np.ndarray], max_angle: float = 20.0) -> float:
    """Clockwise angle (degrees) that makes bands horizontal and lanes vertical.

    Dominant edge orientation: band edges are horizontal and lane edges are
    vertical, so after a tilt of α every strong gradient points at α modulo 90°.
    The weighted mean of exp(4iθ) over all gradients gives α. Noise gradients
    point everywhere and cancel out. Each image contributes its own normalised
    mean, so a sharp image outweighs a flat one.

    Pixel-grid gradients are biased slightly towards the axes, so the estimate
    is refined by rotating and re-measuring the residual tilt, which is unbiased
    near 0°.
    """
    sigs = []
    for rgb in images:
        H, W = rgb.shape[:2]
        s = min(1.0, 800 / max(H, W))
        small = rgb if s >= 1 else np.asarray(
            Image.fromarray(rgb).resize((max(8, round(W * s)), max(8, round(H * s))), Image.BOX))
        sig, _ = band_signal(small)
        sigs.append(sig.astype(np.float32))

    correction = 0.0
    for _ in range(3):
        # float32 arrays become Pillow "F" images automatically (the mode= argument is deprecated).
        rotated = [sig if abs(correction) < 1e-6 else np.asarray(Image.fromarray(sig).rotate(
            -correction, resample=Image.BILINEAR, fillcolor=float(np.median(sig)))) for sig in sigs]
        tilt = _edge_tilt(rotated, margin=0.03 + abs(math.sin(math.radians(correction))))
        correction -= tilt
        if abs(tilt) < 0.01:
            break
    return round(float(np.clip(correction, -max_angle, max_angle)), 2)


def _edge_tilt(sigs: list[np.ndarray], margin: float) -> float:
    """Clockwise tilt (degrees) of the dominant horizontal/vertical structure."""
    z = 0j
    for sig in sigs:
        sig = ndimage.gaussian_filter(sig, 1.0)
        gy = ndimage.sobel(sig, axis=0)
        gx = ndimage.sobel(sig, axis=1)
        h, w = sig.shape
        m = max(2, int(margin * min(h, w)))  # skip the border and any fill corners
        gx, gy = gx[m:-m, m:-m], gy[m:-m, m:-m]
        if gx.size == 0:
            continue
        wgt = gx ** 2 + gy ** 2
        if float(wgt.sum()) <= 1e-12:
            continue
        keep = wgt >= np.percentile(wgt, 90)  # the strongest edges; noise is weak
        theta = np.arctan2(gy[keep], gx[keep])
        wk = wgt[keep]
        z += complex((wk * np.exp(4j * theta)).sum() / wk.sum())
    if abs(z) < 1e-9:
        return 0.0
    return float(np.degrees(np.angle(z) / 4))
