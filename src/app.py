"""The topo map generator in a browser.

    python3 src/app.py            # then open http://127.0.0.1:8765

Name some places, watch the map turn in the viewer, download it.  The 3MF
carries land, water and pins as separate volumes, each on its own filament,
so the slicer opens it ready to print in three colours.  src/topo.py does the
building; this is the page and the one endpoint it talks to, /api/model --
the same path api/model.py answers on Vercel.
"""
import argparse
import gzip
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import trimesh

import solids
import topo

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "public" / "index.html"
HEX = re.compile(r"#[0-9a-fA-F]{6}$")
MATERIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 +._-]{0,23}$")

# What changes the solid.  Colours and the file format do not, so changing
# them reuses the last build rather than fetching and cutting it again.
GEOMETRY = ("topo_pins", "topo_centre", "topo_span", "topo_size", "topo_shape",
            "topo_exag", "topo_base", "topo_depth", "topo_river", "topo_pin_h",
            "topo_ocean", "topo_lakes", "topo_rivers", "topo_streams")
RECENT = {}
BUILD = threading.Lock()


def palette(params):
    """The colours from the form, defaults where a value is missing or not a
    hex colour."""
    given = params.get("colours") or []
    return tuple(c if isinstance(c, str) and HEX.match(c) else d
                 for c, d in zip(list(given) + [None] * 4, solids.COLOURS))


def filaments(params, colours):
    """One filament per colour slot -- its colour and material.  A material
    that is not plain short text falls back rather than going into the 3MF's
    XML as given."""
    given = params.get("materials") or []
    out = []
    for i, hexc in enumerate(colours):
        m = given[i] if i < len(given) and isinstance(given[i], str) else ""
        out.append(dict(hex=hexc, material=m if MATERIAL.match(m) else "PLA", name=""))
    return out


def preview(parts):
    """(bytes, runs): every slot of the map, one after another in a binary
    STL, and [slot index, triangles] for each, so the page can colour every
    triangle by what it is."""
    meshes, runs = [], []
    for part, shift in solids.layout(parts):
        for g in part["groups"]:
            mesh = g["mesh"].copy()
            mesh.apply_translation(shift)
            meshes.append(mesh)
            runs.append([solids.SLOTS.index(g["slot"]), int(len(mesh.faces))])
    mesh = trimesh.util.concatenate(meshes) if len(meshes) > 1 else meshes[0]
    return mesh.export(file_type="stl"), runs


def model(params):
    """(bytes, info, content type) for one set of form values."""
    def num(key, default):
        value = params.get(key)
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    flag = lambda k, d: bool(params.get(k, d))         # noqa: E731
    key = json.dumps({k: params.get(k) for k in GEOMETRY}, sort_keys=True)
    with BUILD:
        if key not in RECENT:
            # Places are looked up by name, so a typo is an error that says
            # which line, not a map of somewhere else.
            pins = params.get("topo_pins") or ""
            if isinstance(pins, str):
                pins = pins.splitlines()
            pins = [str(q).strip() for q in pins if str(q).strip()][:topo.MAX_PINS]
            centre = str(params.get("topo_centre") or "").strip()
            if not pins and not centre:
                raise ValueError("say where: a place in the middle, or a pin or two")
            shape = params.get("topo_shape") or "rect"
            if shape not in topo.SHAPES:
                raise ValueError(f"no such shape: {shape}")
            RECENT[key] = topo.build(
                pins, centre=centre, span=num("topo_span", 0.0) or None,
                size=num("topo_size", topo.SIZE), shape=shape,
                exaggerate=num("topo_exag", topo.EXAGGERATE),
                base=num("topo_base", topo.BASE), depth=num("topo_depth", topo.DEPTH),
                river=num("topo_river", topo.RIVER), pin_h=num("topo_pin_h", topo.PIN),
                ocean=flag("topo_ocean", True), lakes=flag("topo_lakes", True),
                rivers=flag("topo_rivers", True), streams=flag("topo_streams", False))
            while len(RECENT) > 8:
                del RECENT[next(iter(RECENT))]
        parts, info = RECENT[key]

    colours = palette(params)
    if params.get("format") == "3mf":
        return (solids.export_3mf_tools(parts, filaments(params, colours)),
                info, "model/3mf")
    if params.get("format") == "stl":
        return solids.plate(parts).export(file_type="stl"), info, "model/stl"
    data, runs = preview(parts)
    return data, {**info, "preview": runs}, "model/stl"


def health():
    """GET /api/model: did the geometry libraries load where this is deployed?

    Builds a small map of a place given as coordinates -- no lookup, but the
    elevation and water are still fetched -- so one request from a browser
    tells you the network and the boolean engine both work.
    """
    import sys
    import time
    t = time.time()
    with BUILD:
        parts, info = topo.build(["49.7016, -123.1558"], span=4, size=40, pin_h=6)
    return dict(ok=info["watertight"], water=info["water_note"] or "fetched",
                built=f"{info['w']} x {info['h']} mm map in {time.time() - t:.2f}s",
                python=sys.version.split()[0])


class Handler(BaseHTTPRequestHandler):
    server_version = "topo"

    def log_message(self, fmt, *a):          # one line per build, not per asset
        pass

    def _send(self, code, body, ctype, headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path in ("/api/model", "/model"):
            try:
                self._send(200, json.dumps(health()).encode(), "application/json")
            except Exception as exc:         # say what broke, rather than just 500
                self._send(500, json.dumps({"ok": False, "error": repr(exc)}).encode(),
                           "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path.split("?")[0] not in ("/api/model", "/model"):
            return self._send(404, b"not found", "text/plain")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            params = json.loads(body or b"{}")
            data, info, ctype = model(params)
        except Exception as exc:             # a place that cannot be found, mostly
            payload = json.dumps({"error": str(exc)}).encode()
            return self._send(400, payload, "application/json")
        headers = [("X-Model-Info", json.dumps(info))]
        # An STL squashes to about a third of its size; the page asks for that
        # when it can inflate it itself, and a hosted function has a body-size
        # ceiling a big map would hit.
        if ctype == "model/stl" and params.get("gzip") and params.get("format") != "stl":
            data = gzip.compress(data, compresslevel=6)
            headers.append(("X-Compressed", "gzip"))
        print(f"  topo {info['w']:5.1f} x {info['h']:5.1f} mm  {info['span_km']:6.1f} km  "
              f"{ctype[6:]:3s} {len(data) / 1024:6.0f} kB")
        self._send(200, data, ctype, headers)


def serve(host="127.0.0.1", port=8765, open_browser=True):
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"topo map generator on {url}  (ctrl-c to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    a = ap.parse_args()
    serve(a.host, a.port, not a.no_open)
