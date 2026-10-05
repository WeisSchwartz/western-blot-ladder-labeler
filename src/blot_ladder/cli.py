"""Command-line entry point: fully automatic labelling (SPEC.md §7)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import dataclasses

from .detect import detect_ladders, find_sample_lanes
from .geometry import Geometry, apply as apply_geometry, auto_straighten
from .imaging import load_image
from .ladders import all_ladders, default_ladder_id, format_kda, get_ladder
from .merge import MergeOptions, merge
from .render import Crop, LaneLabels, RenderOptions, default_sides, encode, render_figure


def label_file(blot_path: Path, args: argparse.Namespace, out: Path | None) -> int:
    blot = load_image(blot_path)
    source = blot
    if args.marker:
        source = load_image(args.marker)
        if source.shape != blot.shape:
            print(f"  ERROR: marker is {source.shape[1]}x{source.shape[0]} but blot is "
                  f"{blot.shape[1]}x{blot.shape[0]}; they must be the same size.", file=sys.stderr)
            return 2
    marker = source if args.marker else None

    angle = args.rotate
    if args.auto_straighten:
        angle = auto_straighten([blot.rgb] + ([marker.rgb] if marker else []))
    geom = Geometry(angle=angle or 0.0)
    if not geom.is_identity:
        blot = dataclasses.replace(blot, rgb=apply_geometry(blot.rgb, geom))
        if marker:
            marker = dataclasses.replace(marker, rgb=apply_geometry(marker.rgb, geom))
        source = marker or blot

    ladder = get_ladder(args.ladder)
    n_ladders = None if args.ladders == "auto" else int(args.ladders)
    lanes = [{"x": args.lane_x}] if args.lane_x is not None else None
    result = detect_ladders(source.rgb, ladder, polarity=args.polarity,
                            y_range=tuple(args.y_range) if args.y_range else None,
                            lanes=lanes, n_ladders=n_ladders)
    lane_dicts = [ln.to_dict() for ln in result.lanes]

    H, W = blot.shape
    crop = Crop.clamp(dict(zip("xywh", args.crop)) if args.crop else None, W, H)
    li, ri = default_sides(lane_dicts, W)
    if args.side == "left":
        li, ri = (li if li is not None else ri), None
    elif args.side == "right":
        li, ri = None, (ri if ri is not None else li)
    elif args.side == "both":
        first = li if li is not None else ri
        li, ri = first, (ri if ri is not None else first)
    left = lane_dicts[li]["bands"] if li is not None else None
    right = lane_dicts[ri]["bands"] if ri is not None else None

    ladder_lanes = [{"x": ln["lane_x"], "width": ln["lane_width"]} for ln in lane_dicts]
    rgb = blot.rgb
    if args.merge:
        if not marker:
            print("  ERROR: --merge needs --marker.", file=sys.stderr)
            return 2
        rgb = merge(blot.rgb, marker.rgb, MergeOptions(
            enabled=True, mode=args.merge_mode, opacity=args.merge_opacity,
            color=args.merge_color, lanes_only=not args.merge_whole_image), ladder_lanes)

    lane_labels = None
    if args.lane_numbers or args.lane_names:
        sample_lanes = find_sample_lanes(blot.rgb, polarity=args.polarity,
                                         y_range=result.y_range, ladder_xs=[ln["lane_x"] for ln in lane_dicts])
        names = [n.strip() for n in (args.lane_names or "").split(",")] if args.lane_names else []
        items, n = [], 0
        for ln in sample_lanes:
            if ln["is_ladder"]:
                items.append({"x": ln["x"], "number": None, "name": "M"})
                continue
            items.append({"x": ln["x"], "number": str(n + 1),
                          "name": names[n] if n < len(names) else None})
            n += 1
        lane_labels = LaneLabels(items=items, show_numbers=args.lane_numbers,
                                 show_names=bool(names), angle=args.lane_label_angle,
                                 position=args.lane_label_position)
        if names and len(names) != n:
            print(f"  ! {len(names)} lane name(s) given but {n} sample lane(s) found.")

    fmt = "tiff" if (out and out.suffix.lower() in (".tif", ".tiff")) else args.format
    out = out or blot_path.with_name(f"{blot_path.stem}_labeled.{'tif' if fmt == 'tiff' else 'png'}")
    image = render_figure(rgb, left, right, RenderOptions(
        font_size=args.font_size, header=not args.no_header, ticks=not args.no_ticks), crop=crop,
        lane_labels=lane_labels)
    out.write_bytes(encode(image, fmt, blot.dpi))

    top, bottom = result.y_range
    if not geom.is_identity:
        print(f"{blot_path.name}: rotated {geom.angle:+.2f}° (clockwise)")
    print(f"{blot_path.name}: {len(result.lanes)} ladder lane(s), detection rows {top:.0f}–{bottom:.0f}"
          + (" (set at membrane edges)" if result.auto_limits else ""))
    for i, ln in enumerate(result.lanes, 1):
        bands = ", ".join(f"{format_kda(b['kda'])}@{b['y']:.0f}" for b in ln.bands)
        print(f"  ladder {i}: x={ln.lane_x:.0f}px, {len(ln.bands)} bands [{bands}]")
        for w in ln.warnings:
            print(f"    ! {w}")
    print(f"  → {out}")
    for w in result.warnings:
        print(f"  ! {w}")
    if blot.was_stretched:
        print(f"  note: {blot.source_bit_depth}-bit input was linearly scaled to 8-bit for output.")
    if args.json:
        sidecar = out.with_suffix(".json")
        sidecar.write_text(json.dumps({"source": str(blot_path), "ladder": ladder.to_dict(),
                                       "crop": vars(crop), "left_ladder": li, "right_ladder": ri,
                                       "rotation_degrees_clockwise": geom.angle,
                                       "merged_marker": bool(args.merge),
                                       "lane_labels": lane_labels.items if lane_labels else None,
                                       **result.to_dict()}, indent=2))
        print(f"  → {sidecar}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="blot-ladder", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    lab = sub.add_parser("label", help="Detect the ladder and write labelled image(s).")
    lab.add_argument("blot", nargs="+", type=Path, help="Blot image(s).")
    lab.add_argument("--marker", type=Path, help="Separate marker image (single blot only).")
    lab.add_argument("--ladder", default=default_ladder_id(),
                     choices=[lad.id for lad in all_ladders()])
    lab.add_argument("--polarity", default="auto", choices=["auto", "dark", "light"],
                     help="dark = dark bands on light background.")
    lab.add_argument("--ladders", default="auto",
                     help="Number of ladder lanes to find, or 'auto' (default).")
    lab.add_argument("--lane-x", type=float, help="Ladder lane centre (px) to skip lane search.")
    lab.add_argument("--y-range", type=float, nargs=2, metavar=("TOP", "BOTTOM"),
                     help="Only detect bands between these rows (px). Default: whole image, "
                          "trimmed automatically at membrane edges.")
    lab.add_argument("--crop", type=float, nargs=4, metavar=("X", "Y", "W", "H"),
                     help="Crop the blot before labelling (px).")
    lab.add_argument("--rotate", type=float, help="Rotate by this many degrees (+ = clockwise).")
    lab.add_argument("--auto-straighten", action="store_true",
                     help="Find the rotation that makes bands horizontal (overrides --rotate).")
    lab.add_argument("--merge", action="store_true",
                     help="Blend the marker image's ladder into the output (needs --marker).")
    lab.add_argument("--merge-mode", default="auto", choices=["auto", "multiply", "screen", "normal"])
    lab.add_argument("--merge-opacity", type=float, default=1.0, help="0–1 (default 1).")
    lab.add_argument("--merge-color", default="marker",
                     help="'marker' (use the marker's colors) or a hex color such as '#2b50d6'.")
    lab.add_argument("--merge-whole-image", action="store_true",
                     help="Blend the whole marker image, not only the ladder lanes.")
    lab.add_argument("--lane-numbers", action="store_true", help="Number the sample lanes.")
    lab.add_argument("--lane-names", help='Comma-separated sample names, left to right, e.g. "WT,KO,Rescue".')
    lab.add_argument("--lane-label-angle", type=int, default=45, choices=[0, 45, 90])
    lab.add_argument("--lane-label-position", default="top", choices=["top", "bottom"])
    lab.add_argument("--side", default="auto", choices=["auto", "left", "right", "both"],
                     help="auto: nearest side for one ladder; leftmost/rightmost ladders for several.")
    lab.add_argument("--font-size", type=int)
    lab.add_argument("--no-header", action="store_true", help='Omit the "kDa" header.')
    lab.add_argument("--no-ticks", action="store_true")
    lab.add_argument("--format", default="png", choices=["png", "tiff"])
    lab.add_argument("-o", "--output", type=Path, help="Output file (single blot) or directory.")
    lab.add_argument("--json", action="store_true", help="Also write a JSON sidecar.")

    sub.add_parser("ladders", help="List known ladders.")
    sub.add_parser("web", help="Start the local web app.")

    args = ap.parse_args(argv)
    if args.cmd == "ladders":
        for lad in all_ladders():
            mark = " (default)" if lad.id == default_ladder_id() else ""
            print(f"{lad.id:12s} {lad.name}{mark}\n{'':12s} {', '.join(map(format_kda, lad.bands_kda))} kDa")
        return 0
    if args.cmd == "web":
        from .web.app import serve
        serve()
        return 0

    if args.marker and len(args.blot) > 1:
        ap.error("--marker can only be used with a single blot image")
    rc = 0
    for path in args.blot:
        out = args.output
        if out and (out.is_dir() or len(args.blot) > 1):
            out.mkdir(parents=True, exist_ok=True)
            out = out / f"{path.stem}_labeled.{'tif' if args.format == 'tiff' else 'png'}"
        try:
            rc = max(rc, label_file(path, args, out))
        except Exception as exc:  # noqa: BLE001 — report and continue the batch
            print(f"{path.name}: ERROR {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
