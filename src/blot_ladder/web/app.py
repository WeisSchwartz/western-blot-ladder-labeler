"""Local Flask app: upload → auto-detect → correct → download (SPEC.md §5, §8)."""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import socket
import threading
import urllib.request
import uuid
import webbrowser
from collections import OrderedDict
from pathlib import Path

import numpy as np
from flask import Flask, Response, abort, jsonify, render_template, request

from ..detect import detect_ladders, find_sample_lanes, reassign
from ..geometry import Geometry, apply as apply_geometry, auto_straighten
from ..imaging import LoadedImage, load_image, to_png_bytes
from ..ladders import all_ladders, default_ladder_id, get_ladder
from ..merge import MergeOptions, merge
from ..render import LaneLabels, RenderOptions, auto_font_size, encode, render_figure

MAX_SESSIONS = 20
MAX_ANGLE = 45.0


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024
    sessions: OrderedDict[str, dict] = OrderedDict()
    lock = threading.Lock()

    def get_session(sid: str) -> dict:
        with lock:
            s = sessions.get(sid)
            if s is None:
                abort(404, description="Session expired. Please upload the image again.")
            sessions.move_to_end(sid)
            return s

    def source_image(s: dict) -> LoadedImage:
        return s["marker"] or s["blot"]

    def body() -> dict:
        return request.get_json(force=True, silent=True) or {}

    def composed_blot(s: dict, b: dict) -> np.ndarray:
        """The (transformed) blot, with the marker merged in if requested."""
        opts = MergeOptions.from_dict(b.get("merge"))
        if opts.enabled and s["marker"] is not None:
            return merge(s["blot"].rgb, s["marker"].rgb, opts, b.get("ladder_lanes"))
        return s["blot"].rgb

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def _err(e):
        return jsonify(error=getattr(e, "description", str(e))), e.code

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/ladders")
    def ladders():
        return jsonify(default=default_ladder_id(), ladders=[lad.to_dict() for lad in all_ladders()])

    @app.post("/api/session")
    def new_session():
        f = request.files.get("blot")
        if not f or not f.filename:
            abort(400, description="Please choose a blot image.")
        try:
            blot = load_image(f.read(), f.filename)
            marker = None
            m = request.files.get("marker")
            if m and m.filename:
                marker = load_image(m.read(), m.filename)
        except ValueError as exc:
            abort(400, description=str(exc))
        if marker is not None and marker.shape != blot.shape:
            abort(400, description=(
                f"The marker image is {marker.shape[1]}×{marker.shape[0]} px but the blot is "
                f"{blot.shape[1]}×{blot.shape[0]} px. Export both from the imager at the same size."))
        sid = uuid.uuid4().hex
        with lock:
            sessions[sid] = {"orig_blot": blot, "orig_marker": marker, "blot": blot, "marker": marker,
                             "geometry": Geometry(), "name": Path(f.filename).stem, "png": {}}
            while len(sessions) > MAX_SESSIONS:
                sessions.popitem(last=False)
        notes = []
        for label, im in (("blot", blot), ("marker", marker)):
            if im is not None and im.was_stretched:
                notes.append(f"The {label} image is {im.source_bit_depth}-bit. It was linearly scaled "
                             "to 8-bit for display and output.")
        h, w = blot.shape
        return jsonify(id=sid, name=Path(f.filename).stem, width=w, height=h,
                       has_marker=marker is not None, notes=notes,
                       auto_font_size=auto_font_size(h),
                       blot_dark=bool(np.median(blot.rgb) < 128))

    @app.get("/api/session/<sid>/image/<which>")
    def image(sid: str, which: str):
        s = get_session(sid)
        im = s.get(which) if which in ("blot", "marker") else None
        if im is None:
            abort(404, description="No such image.")
        if which not in s["png"]:
            s["png"][which] = to_png_bytes(im.rgb)
        return Response(s["png"][which], mimetype="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/session/<sid>/geometry")
    def set_geometry(sid: str):
        s = get_session(sid)
        b = body()
        geom = Geometry(angle=float(np.clip(float(b.get("angle", 0)), -MAX_ANGLE, MAX_ANGLE)))
        for key in ("blot", "marker"):
            orig = s["orig_" + key]
            if orig is not None:
                s[key] = dataclasses.replace(orig, rgb=apply_geometry(orig.rgb, geom))
        s["geometry"] = geom
        s["png"] = {}
        h, w = s["blot"].shape
        return jsonify(width=w, height=h, angle=geom.angle,
                       auto_font_size=auto_font_size(h))

    @app.post("/api/session/<sid>/straighten")
    def straighten(sid: str):
        s = get_session(sid)
        imgs = [s["orig_blot"].rgb] + ([s["orig_marker"].rgb] if s["orig_marker"] is not None else [])
        return jsonify(angle=auto_straighten(imgs))

    @app.post("/api/session/<sid>/detect")
    def run_detect(sid: str):
        s = get_session(sid)
        b = body()
        try:
            ladder = get_ladder(b.get("ladder"))
        except KeyError as exc:
            abort(400, description=str(exc))
        n = b.get("n_ladders", "auto")
        n_ladders = None if n in (None, "auto") else max(1, int(n))
        lanes = [{"x": float(ln["x"]), "width": ln.get("width")} for ln in b.get("lanes") or []]
        res = detect_ladders(source_image(s).rgb, ladder, polarity=b.get("polarity", "auto"),
                             y_range=_y_range(b), lanes=lanes or None, n_ladders=n_ladders)
        return jsonify(res.to_dict())

    @app.post("/api/session/<sid>/assign")
    def run_assign(sid: str):
        s = get_session(sid)
        b = body()
        ys = [float(y) for y in b.get("bands", [])]
        if b.get("lane_x") is None or len(ys) < 2:
            abort(400, description="Need a lane and at least two bands to assign.")
        res = reassign(source_image(s).rgb, get_ladder(b.get("ladder")), float(b["lane_x"]),
                       ys, polarity=b.get("polarity", "auto"), lane_width=b.get("lane_width"),
                       y_range=_y_range(b))
        return jsonify(res.to_dict())

    @app.post("/api/session/<sid>/lanes")
    def run_lanes(sid: str):
        s = get_session(sid)
        b = body()
        lanes = find_sample_lanes(s["blot"].rgb, polarity=b.get("polarity", "auto"),
                                  y_range=_y_range(b), ladder_xs=b.get("ladder_xs") or [])
        return jsonify(lanes=lanes)

    @app.post("/api/session/<sid>/merged")
    def merged_view(sid: str):
        s = get_session(sid)
        b = body()
        if s["marker"] is None:
            abort(400, description="There is no marker image to merge.")
        rgb = merge(s["blot"].rgb, s["marker"].rgb, MergeOptions.from_dict({**(b.get("merge") or {}),
                    "enabled": True}), b.get("ladder_lanes"))
        return Response(to_png_bytes(rgb), mimetype="image/png")

    @app.post("/api/session/<sid>/render")
    def run_render(sid: str):
        s = get_session(sid)
        b = body()
        fmt = "tiff" if b.get("format") in ("tif", "tiff") else "png"
        fs = b.get("font_size")
        opts = RenderOptions(font_size=int(fs) if fs else None,
                             header=bool(b.get("header", True)),
                             ticks=bool(b.get("ticks", True)))
        image_ = render_figure(composed_blot(s, b), b.get("left"), b.get("right"), opts,
                               crop=b.get("crop"), lane_labels=LaneLabels.from_dict(b.get("lane_labels")))
        if b.get("preview"):
            return Response(encode(image_, "png"), mimetype="image/png")
        ext = "tif" if fmt == "tiff" else "png"
        return Response(encode(image_, fmt, s["blot"].dpi),
                        mimetype="image/tiff" if fmt == "tiff" else "image/png",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{s["name"]}_labeled.{ext}"'})

    return app


def _y_range(b: dict) -> tuple[float, float] | None:
    yr = b.get("y_range")
    if not yr or len(yr) != 2 or yr[0] is None or yr[1] is None:
        return None
    return float(yr[0]), float(yr[1])


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _is_this_app(url: str) -> bool:
    """True if `url` is already serving this app (e.g. the launcher was double-clicked twice)."""
    try:
        with urllib.request.urlopen(url + "api/ladders", timeout=1.5) as resp:
            return "ladders" in json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001 — anything else on that port is not us
        return False


def serve() -> None:
    host = os.environ.get("BLOT_LADDER_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", os.environ.get("BLOT_LADDER_PORT", "5057")))
    open_browser = not os.environ.get("BLOT_LADDER_NO_BROWSER")

    if not _port_free(host, port):
        url = f"http://{host}:{port}/"
        if _is_this_app(url):
            print(f"\n  Western Blot Ladder Labeler is already running at {url}")
            print("  Opening it in your browser. This window can be closed.\n")
            if open_browser:
                webbrowser.open(url)
            return
        # Another program is using the port: take the next free one.
        for candidate in range(port + 1, port + 50):
            if _port_free(host, candidate):
                print(f"  (Port {port} is used by another program, so port {candidate} is used instead.)")
                port = candidate
                break

    url = f"http://{host}:{port}/"
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"\n  Western Blot Ladder Labeler is running at {url}")
    print("  Your browser should open automatically. If it doesn't, open the address above.")
    print("  Keep this window open while you use the app. Close it (or press Ctrl+C) to quit.\n")
    # Keep the window readable for non-technical users: hide the development-server
    # banner and per-request log lines. Errors are still printed.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    from werkzeug.serving import run_simple
    run_simple(host, port, create_app(), threaded=True)
