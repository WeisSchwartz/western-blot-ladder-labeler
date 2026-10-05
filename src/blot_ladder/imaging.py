"""Image loading, bit-depth conversion and the band-detection signal.

Every loaded image becomes an HxWx3 uint8 RGB array. That array is what the UI
displays and what the renderer writes out, so preview and output always match.
8-bit inputs keep their pixel values exactly. Deeper inputs get a single linear
stretch over the whole image.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

STRETCH_PERCENTILES = (0.1, 99.9)
# A pixel counts as colored when max(RGB) - min(RGB) exceeds this (0–1 scale).
CHROMA_PIXEL_THRESHOLD = 0.15
# The image counts as color when at least this fraction of pixels is colored.
# A ladder can cover a very small part of the image, so this is set low.
COLOR_FRACTION_THRESHOLD = 0.002


@dataclass
class LoadedImage:
    rgb: np.ndarray  # HxWx3 uint8
    source_bit_depth: int
    was_stretched: bool
    dpi: tuple[float, float] | None
    is_color: bool

    @property
    def shape(self) -> tuple[int, int]:
        return self.rgb.shape[0], self.rgb.shape[1]


def load_image(src: str | Path | bytes, filename: str | None = None) -> LoadedImage:
    """Load a blot image from a path or raw bytes."""
    if isinstance(src, (str, Path)):
        filename = filename or str(src)
        data = Path(src).read_bytes()
    else:
        data = src


    arr, dpi = None, None
    try:
        with Image.open(io.BytesIO(data)) as im:
            dpi = _dpi_of(im)
            arr = _pil_to_array(im)
    except Exception:
        arr = None
    if arr is None:  # Pillow could not decode it (e.g. 16-bit RGB TIFF)
        try:
            import tifffile

            arr = tifffile.imread(io.BytesIO(data))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Could not read image '{filename or 'upload'}': {exc}") from exc
    arr = _normalize_layout(np.asarray(arr))
    bit_depth = arr.dtype.itemsize * 8 if arr.dtype != bool else 1
    rgb, stretched = _to_rgb8(arr)
    return LoadedImage(rgb=rgb, source_bit_depth=bit_depth, was_stretched=stretched,
                       dpi=dpi, is_color=has_color(rgb))


def _dpi_of(im: Image.Image) -> tuple[float, float] | None:
    dpi = im.info.get("dpi")
    if dpi and all(float(d) > 0 for d in dpi):
        return float(dpi[0]), float(dpi[1])
    return None


def _pil_to_array(im: Image.Image) -> np.ndarray | None:
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
    mode = im.mode
    if mode in ("1",):
        return np.asarray(im.convert("L"))
    if mode in ("L", "RGB", "I;16", "I;16B", "I;16L", "I", "F"):
        return np.asarray(im)
    if mode in ("RGBA", "LA", "P", "PA"):
        rgba = np.asarray(im.convert("RGBA")).astype(np.float32)
        alpha = rgba[..., 3:4] / 255.0
        # Composite transparency onto white.
        rgb = rgba[..., :3] * alpha + 255.0 * (1 - alpha)
        return np.round(rgb).astype(np.uint8)
    if mode in ("CMYK", "YCbCr", "HSV", "LAB"):
        return np.asarray(im.convert("RGB"))
    return None  # let tifffile try (e.g. 16-bit RGB)


def _normalize_layout(arr: np.ndarray) -> np.ndarray:
    arr = np.squeeze(arr)
    if arr.ndim == 3 and arr.shape[-1] not in (1, 3, 4) and arr.shape[0] in (1, 3, 4):
        arr = np.moveaxis(arr, 0, -1)  # channels-first TIFF
    if arr.ndim == 3 and arr.shape[-1] not in (1, 3, 4):
        arr = arr[0]  # multi-page stack: first page
    if arr.ndim == 3 and arr.shape[-1] == 4:
        arr = arr[..., :3]
    if arr.ndim == 3 and arr.shape[-1] == 1:
        arr = arr[..., 0]
    if arr.ndim not in (2, 3):
        raise ValueError(f"Unsupported image shape {arr.shape}")
    return arr


def _to_rgb8(arr: np.ndarray) -> tuple[np.ndarray, bool]:
    stretched = False
    if arr.dtype == bool:
        arr = arr.astype(np.uint8) * 255
    elif arr.dtype != np.uint8:
        a = arr.astype(np.float64)
        lo, hi = np.percentile(a, STRETCH_PERCENTILES)
        if hi <= lo:
            lo, hi = float(a.min()), float(a.max()) or 1.0
        a = np.clip((a - lo) / max(hi - lo, 1e-12), 0, 1)
        arr = np.round(a * 255).astype(np.uint8)
        stretched = True
    if arr.ndim == 2:
        arr = np.repeat(arr[..., None], 3, axis=2)
    return np.ascontiguousarray(arr), stretched


def has_color(rgb: np.ndarray) -> bool:
    step = max(1, int(np.sqrt(rgb.shape[0] * rgb.shape[1] / 250_000)))
    small = rgb[::step, ::step].astype(np.int16)
    chroma = (small.max(axis=2) - small.min(axis=2)) / 255.0
    return float((chroma > CHROMA_PIXEL_THRESHOLD).mean()) >= COLOR_FRACTION_THRESHOLD


def chroma_map(rgb: np.ndarray) -> np.ndarray:
    c = rgb.astype(np.int16)
    return (c.max(axis=2) - c.min(axis=2)).astype(np.float32) / 255.0


def luminance(rgb: np.ndarray) -> np.ndarray:
    c = rgb.astype(np.float32) / 255.0
    return 0.299 * c[..., 0] + 0.587 * c[..., 1] + 0.114 * c[..., 2]


def guess_polarity(rgb: np.ndarray) -> str:
    """'dark' means dark bands on a light background."""
    return "dark" if float(np.median(luminance(rgb))) >= 0.5 else "light"


def band_signal(rgb: np.ndarray, polarity: str = "auto", use_color: bool | None = None
                ) -> tuple[np.ndarray, dict]:
    """Return a float32 map (0–1) that is high where there is band signal.

    On a color image the signal is the chroma, which isolates a colored
    prestained ladder from a grayscale chemiluminescence signal. Otherwise the
    signal is the luminance, inverted when the bands are dark.
    """
    if use_color is None:
        use_color = has_color(rgb)
    pol = guess_polarity(rgb) if polarity == "auto" else polarity
    if use_color:
        sig = chroma_map(rgb)
    else:
        lum = luminance(rgb)
        sig = 1.0 - lum if pol == "dark" else lum
    return sig.astype(np.float32), {"polarity": pol, "color": bool(use_color)}


def pinkness_map(rgb: np.ndarray) -> np.ndarray:
    """(R − B) / 255, which is positive for pink/red and negative for blue."""
    c = rgb.astype(np.float32)
    return (c[..., 0] - c[..., 2]) / 255.0


def to_png_bytes(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG", compress_level=1)
    return buf.getvalue()
