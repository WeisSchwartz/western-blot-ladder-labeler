"""Crop the blot and draw kDa labels in margins beside it (SPEC.md §6).

The (cropped) blot pixels are pasted unchanged into a larger white canvas.
Labels and ticks are drawn only in the added margins. Each side (left/right)
takes its labels from one ladder. That ladder does not have to be inside the
crop, so you can crop the ladder lane out and keep its labels.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .ladders import format_kda

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "DejaVuSans.ttf",
    "Arial.ttf",
]
HEADER_TEXT = "kDa"


@dataclass
class RenderOptions:
    side: str = "auto"  # render_labels() only: auto | left | right
    font_size: int | None = None  # None → auto (~3% of output image height)
    header: bool = True  # "kDa" above the labels
    ticks: bool = True
    lane_x: float | None = None  # render_labels() only: used by side="auto"
    color: tuple[int, int, int] = (0, 0, 0)
    background: tuple[int, int, int] = (255, 255, 255)


@dataclass
class Crop:
    x: int
    y: int
    w: int
    h: int

    @classmethod
    def clamp(cls, crop: dict | None, width: int, height: int) -> "Crop":
        if not crop:
            return cls(0, 0, width, height)
        x = int(np.clip(round(float(crop.get("x", 0))), 0, width - 1))
        y = int(np.clip(round(float(crop.get("y", 0))), 0, height - 1))
        w = int(np.clip(round(float(crop.get("w", width))), 1, width - x))
        h = int(np.clip(round(float(crop.get("h", height))), 1, height - y))
        return cls(x, y, w, h)


@dataclass
class LaneLabels:
    """Lane numbers and/or sample names above or below the blot."""
    items: list[dict] = field(default_factory=list)  # {x, number: str|None, name: str|None}
    show_numbers: bool = True
    show_names: bool = True
    angle: int = 0  # 0 (horizontal), 45, or 90 degrees
    position: str = "top"  # top | bottom

    @classmethod
    def from_dict(cls, d: dict | None) -> "LaneLabels | None":
        if not d or not d.get("items"):
            return None
        angle = int(d.get("angle", 0))
        return cls(items=[{"x": float(it["x"]),
                           "number": (str(it["number"]) if it.get("number") not in (None, "") else None),
                           "name": (str(it["name"]).strip() or None) if it.get("name") else None}
                          for it in d["items"]],
                   show_numbers=bool(d.get("show_numbers", True)),
                   show_names=bool(d.get("show_names", True)),
                   angle=angle if angle in (0, 45, 90) else 0,
                   position="bottom" if d.get("position") == "bottom" else "top")


def auto_font_size(height: int) -> int:
    return max(12, int(round(height * 0.03)))


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def resolve_side(side: str, lane_x: float | None, width: int) -> str:
    if side in ("left", "right"):
        return side
    if lane_x is not None and lane_x > width / 2:
        return "right"
    return "left"


def spread_positions(ys: list[float], min_gap: float, lo: float, hi: float) -> list[float]:
    """Nearest positions to `ys` (sorted) that are at least `min_gap` apart and inside [lo, hi].

    This is a least-squares isotonic fit: subtract i·gap, pool adjacent
    violators, then add i·gap back.
    """
    n = len(ys)
    if n == 0:
        return []
    q = [y - i * min_gap for i, y in enumerate(ys)]
    blocks: list[list[float]] = []  # [mean, count]
    for v in q:
        blocks.append([v, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            m2, c2 = blocks.pop()
            m1, c1 = blocks.pop()
            blocks.append([(m1 * c1 + m2 * c2) / (c1 + c2), c1 + c2])
    fitted = [m for m, c in blocks for _ in range(c)]
    out = [f + i * min_gap for i, f in enumerate(fitted)]
    # Keep inside the canvas; shift the whole stack if needed.
    if out[0] < lo:
        out = [y + (lo - out[0]) for y in out]
    if out[-1] > hi:
        out = [y - (out[-1] - hi) for y in out]
    return out


def _shown(bands: list[dict] | None, y_off: float, height: int) -> list[tuple[float, float]]:
    """(y, kDa) of labelled, visible bands inside the output rows, top → bottom."""
    out = []
    for b in bands or []:
        if b.get("kda") is None or not b.get("show", True):
            continue
        y = float(b["y"]) - y_off
        if -0.5 <= y <= height - 0.5:
            out.append((y, float(b["kda"])))
    return sorted(out)


def render_figure(rgb: np.ndarray, left: list[dict] | None = None, right: list[dict] | None = None,
                  opts: RenderOptions | None = None, crop: dict | Crop | None = None,
                  lane_labels: LaneLabels | dict | None = None) -> Image.Image:
    """Crop `rgb`, label the left and/or right side with kDa values, and label lanes.

    left/right: [{y, kda, show?}]; lane_labels items: [{x, number, name}].
    All coordinates are in the uncropped image.
    """
    opts = opts or RenderOptions()
    full_h, full_w = rgb.shape[:2]
    c = crop if isinstance(crop, Crop) else Crop.clamp(crop, full_w, full_h)
    img = rgb[c.y:c.y + c.h, c.x:c.x + c.w]
    H, W = img.shape[:2]
    fs = int(opts.font_size or auto_font_size(H))
    font = load_font(fs)
    core, (ox, oy) = _render_core(img, left, right, opts, c, fs, font)
    labels = lane_labels if isinstance(lane_labels, LaneLabels) or lane_labels is None \
        else LaneLabels.from_dict(lane_labels)
    sprites = _lane_label_sprites(labels, c, W, H, font, fs) if labels else []
    if not sprites:
        return core

    # Bounding box of everything, relative to the blot's top-left corner.
    pad = max(3, round(fs * 0.4))
    x0, y0, x1, y1 = -ox, -oy, core.width - ox, core.height - oy
    for spr, px, py in sprites:
        x0, y0 = min(x0, px - pad), min(y0, py - pad)
        x1, y1 = max(x1, px + spr.width + pad), max(y1, py + spr.height + pad)
    x0, y0 = math.floor(x0), math.floor(y0)
    canvas = Image.new("RGB", (math.ceil(x1) - x0, math.ceil(y1) - y0), opts.background)
    canvas.paste(core, (-ox - x0, -oy - y0))
    ink = Image.new("RGB", (1, 1), opts.color)
    for spr, px, py in sprites:
        canvas.paste(ink.resize(spr.size), (round(px - x0), round(py - y0)), mask=spr)
    return canvas


def _text_sprite(text: str, font, angle: float, centered: bool) -> tuple[Image.Image, float, float]:
    """Text as an 'L' mask rotated counter-clockwise by `angle`, plus its anchor point.

    The anchor is the text's vertical middle at its start, or at its centre if
    `centered` is true.
    """
    l, t, r, b = font.getbbox(text, anchor="lm")
    p = 2
    w, h = math.ceil(r - l) + 2 * p, math.ceil(b - t) + 2 * p
    spr = Image.new("L", (w, h), 0)
    ax, ay = p - l, p - t
    ImageDraw.Draw(spr).text((ax, ay), text, font=font, fill=255, anchor="lm")
    if centered:
        ax = w / 2
    if abs(angle) < 1e-6:
        return spr, ax, ay
    rot = spr.rotate(angle, resample=Image.BICUBIC, expand=True)
    a = math.radians(angle)
    dx, dy = ax - w / 2, ay - h / 2
    return (rot, rot.width / 2 + dx * math.cos(a) + dy * math.sin(a),
            rot.height / 2 - dx * math.sin(a) + dy * math.cos(a))


def _lane_label_sprites(labels: LaneLabels, c: Crop, W: int, H: int, font, fs: int
                        ) -> list[tuple[Image.Image, float, float]]:
    """[(mask, x, y)] positioned relative to the blot's top-left corner."""
    items = [it for it in labels.items if 0 <= it["x"] - c.x <= W - 1]
    if not items:
        return []
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    box = probe.textbbox((0, 0), "0123456789Ag", font=font, anchor="lm")
    text_h = box[3] - box[1]
    gap = max(2, round(fs * 0.35))
    top = labels.position == "top"
    sign = -1 if top else 1
    edge = 0 if top else H
    has_numbers = labels.show_numbers and any(it["number"] for it in items)
    names_off = gap + (text_h * 1.3 if has_numbers else 0)

    out = []

    def place(text, x, y, angle, centered):
        spr, ax, ay = _text_sprite(text, font, angle, centered)
        out.append((spr, x - ax, y - ay))

    for it in items:
        x = it["x"] - c.x
        if has_numbers and it["number"]:
            place(it["number"], x, edge + sign * (gap + text_h / 2), 0, True)
        if labels.show_names and it["name"]:
            if labels.angle == 0:
                place(it["name"], x, edge + sign * (names_off + text_h / 2), 0, True)
            else:
                # Text starts at the lane and runs away from the blot.
                place(it["name"], x, edge + sign * (names_off + 0.45 * text_h),
                      labels.angle if top else -labels.angle, False)
    return out


def _render_core(img: np.ndarray, left, right, opts: RenderOptions, c: Crop, fs: int, font
                 ) -> tuple[Image.Image, tuple[int, int]]:
    """Blot plus kDa margins. Returns the canvas and the blot's offset in it."""
    H, W = img.shape[:2]
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    digit_box = probe.textbbox((0, 0), "0123456789", font=font, anchor="ls")
    text_h = digit_box[3] - digit_box[1]
    tick_len = max(4, round(fs * 0.6)) if opts.ticks else 0
    gap = max(2, round(fs * 0.3))
    pad = max(3, round(fs * 0.4))
    min_gap = text_h * 1.35
    header_gap = min_gap * 1.15
    header_w = probe.textlength(HEADER_TEXT, font=font) if opts.header else 0

    sides = {}
    for name, bands in (("left", left), ("right", right)):
        shown = _shown(bands, c.y, H)
        if not shown:
            continue
        texts = [format_kda(k) for _, k in shown]
        widths = [probe.textlength(t, font=font) for t in texts]
        label_w = max(widths + [header_w])
        margin = int(np.ceil(pad + label_w + gap + tick_len + (1 if opts.ticks else 0)))
        sides[name] = {"shown": shown, "texts": texts, "widths": widths,
                       "label_w": label_w, "margin": margin}

    # Room for the "kDa" header above the top label, if the image top is too close.
    extra_top = 0
    if opts.header:
        for sd in sides.values():
            header_center = sd["shown"][0][0] - header_gap
            needed = text_h * 0.6 + pad * 0.5
            extra_top = max(extra_top, int(np.ceil(needed - header_center)))
    for sd in sides.values():
        ys = [y + extra_top for y, _ in sd["shown"]]
        sd["pos"] = spread_positions(ys, min_gap,
                                     text_h * 0.6 + (header_gap if opts.header else 0),
                                     H + extra_top - text_h * 0.6)

    m_left = sides.get("left", {}).get("margin", 0)
    m_right = sides.get("right", {}).get("margin", 0)
    canvas = Image.new("RGB", (m_left + W + m_right, H + extra_top), opts.background)
    canvas.paste(Image.fromarray(np.ascontiguousarray(img)), (m_left, extra_top))
    draw = ImageDraw.Draw(canvas)
    lw = max(1, round(fs / 12))

    def text_at(x_left: float, cy: float, text: str):
        # Center the digits' ink vertically on cy.
        draw.text((x_left, cy - (digit_box[1] + digit_box[3]) / 2), text, font=font,
                  fill=opts.color, anchor="ls")

    for name, sd in sides.items():
        if name == "left":
            edge = m_left  # first image column; ticks end just before it
            text_right = edge - tick_len - gap
            label_x = [text_right - w for w in sd["widths"]]
            header_x = text_right - sd["label_w"] + (sd["label_w"] - header_w) / 2
        else:
            edge = m_left + W - 1  # last image column; ticks start just after it
            text_left = edge + 1 + tick_len + gap
            label_x = [text_left] * len(sd["widths"])
            header_x = text_left
        for (y, _), text, lx, ly in zip(sd["shown"], sd["texts"], label_x, sd["pos"]):
            by = y + extra_top
            text_at(lx, ly, text)
            if not opts.ticks:
                continue
            if name == "left":
                start, end = (edge - tick_len, ly), (edge - 1, by)
                stub = (start[0] + tick_len * 0.35, ly)
            else:
                start, end = (edge + tick_len + 1, ly), (edge + 1, by)
                stub = (start[0] - tick_len * 0.35, ly)
            if abs(ly - by) < 0.5:
                draw.line([start, end], fill=opts.color, width=lw)
            else:  # leader: short stub at the label, angled to the band
                draw.line([start, stub, end], fill=opts.color, width=lw, joint="curve")
        if opts.header:
            hy = max(text_h * 0.6, min(sd["pos"][0], sd["shown"][0][0] + extra_top) - header_gap)
            text_at(header_x, hy, HEADER_TEXT)
    return canvas, (m_left, extra_top)


def render_labels(rgb: np.ndarray, bands: list[dict], opts: RenderOptions | None = None) -> Image.Image:
    """Single-ladder convenience wrapper: labels on one side chosen by opts.side."""
    opts = opts or RenderOptions()
    side = resolve_side(opts.side, opts.lane_x, rgb.shape[1])
    return render_figure(rgb, bands if side == "left" else None,
                         bands if side == "right" else None, opts)


def default_sides(lanes: list[dict], width: int) -> tuple[int | None, int | None]:
    """Indices of the lanes that label the left and right sides by default.

    One ladder labels the side it is nearest to. With several ladders, the
    leftmost labels the left side and the rightmost labels the right side.
    """
    if not lanes:
        return None, None
    order = sorted(range(len(lanes)), key=lambda i: lanes[i]["lane_x"])
    if len(order) == 1:
        return (None, order[0]) if lanes[order[0]]["lane_x"] > width / 2 else (order[0], None)
    return order[0], order[-1]


def encode(image: Image.Image, fmt: str = "png", dpi: tuple[float, float] | None = None) -> bytes:
    buf = io.BytesIO()
    kw = {"dpi": dpi} if dpi else {}
    if fmt.lower() in ("tif", "tiff"):
        image.save(buf, format="TIFF", compression="tiff_lzw", **kw)
    else:
        image.save(buf, format="PNG", **kw)
    return buf.getvalue()
