"""Blend the marker (ladder) image into the blot image (SPEC.md §4b).

The ladder is extracted from the marker image as a 0–1 signal and composited
onto the blot:

  α = opacity · signal · lane mask
  normal:   out = B·(1−α) + C·α
  multiply: out = B·(1 − α + α·C)       good on light backgrounds
  screen:   out = 1 − (1−B)·(1 − α·C)   good on dark backgrounds (fluorescence)

"auto" picks multiply for light-background blots and screen for dark ones.
The colour C is either the marker's own colour or a fixed colour. A grayscale
marker becomes black on a light blot and white on a dark one.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from .imaging import chroma_map, has_color, luminance

MODES = ("auto", "normal", "multiply", "screen")


@dataclass
class MergeOptions:
    enabled: bool = False
    mode: str = "auto"
    opacity: float = 1.0
    color: str = "marker"  # "marker" or "#rrggbb"
    lanes_only: bool = True

    @classmethod
    def from_dict(cls, d: dict | None) -> "MergeOptions":
        d = d or {}
        mode = d.get("mode", "auto")
        return cls(enabled=bool(d.get("enabled", False)),
                   mode=mode if mode in MODES else "auto",
                   opacity=float(np.clip(float(d.get("opacity", 1.0)), 0, 1)),
                   color=str(d.get("color", "marker")),
                   lanes_only=bool(d.get("lanes_only", True)))


def is_dark_background(rgb: np.ndarray) -> bool:
    return float(np.median(luminance(rgb))) < 0.5


def marker_signal(marker: np.ndarray) -> np.ndarray:
    """0–1 map of the ladder (and other marker features) against the marker background."""
    lum = luminance(marker)
    if is_dark_background(marker):
        s = marker.max(axis=2).astype(np.float32) / 255.0  # bright bands on black
    else:
        s = np.maximum(1.0 - lum, chroma_map(marker))  # dark or coloured bands on white
    h, w = s.shape
    k = (max(3, h // 12), max(3, w // 12))
    bg = ndimage.uniform_filter(ndimage.grey_opening(s, size=k), size=k)
    s = np.clip(s - bg, 0, None)
    top = float(np.percentile(s, 99.7))
    if top <= 1e-6:
        return np.zeros_like(s, dtype=np.float32)
    s = np.clip(s / top, 0, 1)
    # Suppress the low-level texture of the membrane itself.
    return np.clip((s - 0.08) / 0.92, 0, 1).astype(np.float32)


def _parse_hex(color: str) -> np.ndarray | None:
    c = color.strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        return None
    try:
        return np.array([int(c[i:i + 2], 16) for i in (0, 2, 4)], np.float32) / 255.0
    except ValueError:
        return None


def ladder_color(marker: np.ndarray, blot_dark: bool, color: str) -> np.ndarray:
    """HxWx3 (or 3,) colour in 0–1 that the ladder is painted with."""
    fixed = _parse_hex(color) if color != "marker" else None
    if fixed is not None:
        return fixed
    if not has_color(marker):
        return np.full(3, 1.0 if blot_dark else 0.0, np.float32)
    c = marker.astype(np.float32) / 255.0
    if blot_dark or is_dark_background(marker):
        # Brighten to full intensity while keeping the hue.
        c = c / np.maximum(c.max(axis=2, keepdims=True), 1e-3)
    return c


def lane_mask(shape: tuple[int, int], lanes: list[dict] | None) -> np.ndarray:
    """Soft mask that is 1 over the ladder lanes (±80% of lane width), 0 elsewhere."""
    h, w = shape
    if not lanes:
        return np.ones((1, w), np.float32)
    xs = np.arange(w, dtype=np.float32)
    m = np.zeros(w, np.float32)
    for ln in lanes:
        half = 0.8 * float(ln.get("width") or w / 25)
        feather = max(1.0, 0.25 * half)
        d = np.abs(xs - float(ln["x"])) - half
        m = np.maximum(m, np.clip(1 - d / feather, 0, 1))
    return m[None, :]


def merge(blot: np.ndarray, marker: np.ndarray, opts: MergeOptions,
          ladder_lanes: list[dict] | None = None) -> np.ndarray:
    """Composite the marker's ladder onto the blot. Both images must be the same size."""
    if marker.shape != blot.shape:
        raise ValueError("The blot and marker images must be the same size to merge.")
    blot_dark = is_dark_background(blot)
    mode = opts.mode
    if mode == "auto":
        mode = "screen" if blot_dark else "multiply"
    sig = marker_signal(marker)
    mask = lane_mask(sig.shape, ladder_lanes if opts.lanes_only else None)
    a = (opts.opacity * sig * mask)[..., None]
    C = ladder_color(marker, blot_dark, opts.color)
    B = blot.astype(np.float32) / 255.0
    if mode == "multiply":
        out = B * (1 - a + a * C)
    elif mode == "screen":
        out = 1 - (1 - B) * (1 - a * C)
    else:
        out = B * (1 - a) + C * a
    return np.clip(np.round(out * 255), 0, 255).astype(np.uint8)
