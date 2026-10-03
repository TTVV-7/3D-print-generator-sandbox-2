"""A topographic map: a patch of the real world as a relief, with its ocean,
lakes and rivers in a second colour and a pin standing on each place you name.

    parts, info = topo.build(pins=["Squamish", "49.6847, -123.1558"])
    parts, info = topo.build(centre="Lake Louise", span=12, size=150)

Three kinds of data go into it, all of them fetched on demand and none of
them needing a key:

- **The ground** is the AWS Terrain Tiles' Terrarium PNGs: the whole world's
  elevation, SRTM and friends merged, as 256-pixel tiles whose red, green and
  blue are the height in metres.  Down to about 5 m a pixel.
- **The water** is OpenStreetMap, as OpenFreeMap's vector tiles in the
  OpenMapTiles schema: the `water` layer's polygons (ocean, lakes, wide
  rivers) and the `waterway` layer's lines (rivers, canals and streams).
  Decoded here by a forty-line protobuf reader rather than a dependency.
- **The pins** are places by name, looked up on OpenStreetMap's Nominatim
  (or Photon, when Nominatim is busy), or latitude, longitude pairs given
  straight.

The relief is a heightfield on a regular grid, closed into a solid with walls
and a floor.  Water is the awkward part: a lake's surface should be flat and
an ocean's at sea level, while the elevation data under them is whatever the
survey made of a reflective surface.  So the grid carries three surfaces --
the ground, the water's surface, and the floor the water sits on -- and the
water is the vector outline, cut between the floor and the surface.  The land
is the ground with that taken out.  Every water body is at least `depth` mm
of its own colour, so it prints as a layer of blue rather than a speck, and
a river narrower than the grid still comes out the width you asked for,
because its outline is the vector line, not a run of grid cells.

The part is the same shape as the print sandbox's other generators build --
land in the body slot, water in the pattern slot, pins in the primary slot --
so solids.py's plate, 3MF and STL writers and the viewer take it as is.

Elevation: Mapzen / AWS Terrain Tiles (SRTM, GMTED, ETOPO1, NED and others).
Water and places: (c) OpenStreetMap contributors, via OpenFreeMap and
Nominatim.
"""
import gzip
import hashlib
import io
import json
import math
import os
import re
import struct
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import shapely
import trimesh
from PIL import Image
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

import layers
import solids

# The model.  150 mm is a coaster-to-tile sized piece that still fits the
# smallest beds with room round it.
SIZE = 150.0
SHAPES = ("rect", "square", "round")
ASPECT = 0.75             # a "rect" is landscape, 4 : 3

# How much ground it covers when nothing else says, in km across.
SPAN = 20.0
SPAN_MIN, SPAN_MAX = 0.5, 2000.0
# Room left round the pins when the map is fitted to them, as a share of
# their spread -- so the outermost pin is not standing on the edge.
PAD = 0.18

# Under the lowest point.  It is also what the water sits in, so it has to
# be thicker than the water is deep.
BASE = 3.0
# Real relief is flat at any size a printer makes: a 2000 m mountain on a
# 20 km map, at 150 mm, is 15 mm tall -- and a 200 m hill is 1.5.  So it is
# stretched, and the readout says by how much.
EXAGGERATE = 1.5
EXAGGERATE_MAX = 20.0
# The tallest the relief is allowed to get, whatever the stretch says: a
# thin tall peak on a big map is a thing that snaps off.
RELIEF_MAX = 60.0

# Every water body is at least this deep in its own colour: three layers at
# 0.2, which is enough to read as blue from the side and not just the top.
DEPTH = 0.6
# A river on a 20 km map is narrower than a hair.  These are the widths it
# is drawn at instead -- a nozzle and a half for a river, one for a stream.
RIVER = 1.0
STREAM = 0.6
# A lake smaller than this, in mm^2 on the model, is a speck the nozzle
# would only smear.
MIN_LAKE = 1.5

# The grid.  0.5 mm is a nozzle and a bit: finer than that and the slicer
# throws it away.  The cell grows with the model so the triangle count does
# not -- the preview has to get to the browser.
CELL = 0.5
MAX_CELLS = 300

# The pins: a map marker turned on a lathe -- a ball on a cone, the point
# down -- standing on the ground at the place.  Its cone is steep enough to
# print without support, and it goes into the ground on a stem so it does
# not snap off at its point.
PIN = 8.0
PIN_HEAD = 0.3            # the ball's radius, as a share of the height
PIN_NECK = 0.8            # the radius where it meets the ground
PIN_STEM = 2.0            # how far the stem goes down into the ground

# The mines: a small marker on each, its shape saying how far the site got
# -- a headframe for a working mine, a spoil heap for one that was, a stud
# for a prospect, a dot for a showing.  MINE is the headframe's height; the
# others are in proportion.  Every one is a convex solid with sides no
# shallower than 45 degrees, so it prints without support, and it goes into
# the ground on the same stem as a pin.
MINE = 4.0
MINES = ("off",) + tuple(layers.SHOW)

# The forest: the top SKIN mm of the land, wherever it is wooded, in a shade
# of green -- thick enough to read from the side, as the water is.  Patches
# smaller than MIN_FOREST mm^2 on the model are left in the land colour.
SKIN = 0.6
MIN_FOREST = 2.0
FOREST_PX = 1600          # the most pixels across the species picture is asked for

BED = 256.0
MAX_PINS = 24             # each is a lookup, and a lookup is a second

ELEVATION = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
VECTOR_INDEX = "https://tiles.openfreemap.org/planet"
VECTOR_FALLBACK = "https://tiles.openfreemap.org/planet/{z}/{x}/{y}.pbf"
GEOCODER = "https://nominatim.openstreetmap.org/search"
# Komoot's Photon: OpenStreetMap too, asked when Nominatim says no -- it
# turns a busy shared address away with a 429 long before its one-a-second.
GEOCODER_FALLBACK = "https://photon.komoot.io/api/"
AGENT = "3d-print-generator-sandbox-2 topo generator (https://github.com/TTVV-7/3D-print-generator-sandbox-2)"
ATTRIBUTION = ("Elevation: AWS Terrain Tiles (SRTM, GMTED, ETOPO1 and others). "
               "Water and places: © OpenStreetMap contributors, via OpenFreeMap "
               "and Nominatim.")

ELEVATION_ZOOM_MAX = 14   # the tiles go to 15; 14 is already ~10 m a pixel
VECTOR_ZOOM_MAX = 14      # OpenMapTiles stops at 14 and overzooms after
MAX_TILES = 64            # per source, per build

EARTH = 40075016.686      # metres round the equator

# OpenMapTiles' water classes, sorted the way they are drawn.  A lake is
# flattened to one level; a wide river keeps the slope of its valley.
OCEAN = {"ocean"}
LAKE = {"lake", "pond", "reservoir", "basin", "dock", "lagoon"}
RIVER_AREA = {"river"}


# ---------------------------------------------------------------- fetching

_MEMO = {}
_MEMO_LOCK = threading.Lock()
CACHE = Path(os.environ.get("TOPO_CACHE") or Path(tempfile.gettempdir()) / "topo-tiles")


def fetch(url, timeout=25, form=None):
    """The bytes at `url`, from memory, from the disk cache, or from the
    network -- in that order.  Tiles do not change between one build and
    the next, and a slider dragged across the page is a dozen builds of the
    same tiles.  With `form`, a POST of it, cached the same way: a
    request too long to go in a URL."""
    key = url if form is None else url + "\n" + form
    with _MEMO_LOCK:
        if key in _MEMO:
            return _MEMO[key]
    path = CACHE / hashlib.sha1(key.encode()).hexdigest()
    data = None
    try:
        data = path.read_bytes()
    except OSError:
        pass
    if data is None:
        headers = {"User-Agent": AGENT, "Accept-Encoding": "gzip"}
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=None if form is None else form.encode(),
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:           # no tile there: open sea, mostly
                data = b""
            else:
                raise
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        if b"ServiceException" in data[:2000]:
            return data                   # a map server's error: not one to keep
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:                   # a read-only disk is only slower
            pass
    with _MEMO_LOCK:
        if len(_MEMO) > 512:
            _MEMO.clear()
        _MEMO[key] = data
    return data


def fetch_all(urls):
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(fetch, urls))


_VECTOR_URL = []


def vector_url():
    """OpenFreeMap's tile URL carries the date of the planet build in it; the
    TileJSON says which is current."""
    if not _VECTOR_URL:
        try:
            _VECTOR_URL.append(json.loads(fetch(VECTOR_INDEX))["tiles"][0])
        except Exception:
            return VECTOR_FALLBACK
    return _VECTOR_URL[0]


# ---------------------------------------------------------------- places

LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*$")
_PLACES = {}
_GEOCODE_LOCK = threading.Lock()
_LAST_LOOKUP = [0.0]


def geocode(query):
    """(lat, lon, name) for a place name, or for "lat, lon" given straight."""
    query = (query or "").strip()
    m = LATLON.match(query)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-85 <= lat <= 85 and -180 <= lon <= 180):
            raise ValueError(f"{query} is not a latitude, longitude")
        return lat, lon, f"{lat:.4f}, {lon:.4f}"
    key = query.lower()
    if key in _PLACES:
        return _PLACES[key]
    url = GEOCODER + "?" + urllib.parse.urlencode(dict(q=query, format="json", limit=1))
    # Nominatim asks for no more than one request a second: one at a time
    # through here, spaced out, and every answer cached.
    with _GEOCODE_LOCK:
        wait = _LAST_LOOKUP[0] + 1.0 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST_LOOKUP[0] = time.monotonic()
        try:
            found = [(float(h["lat"]), float(h["lon"]),
                      h.get("display_name", query).split(",")[0])
                     for h in json.loads(fetch(url) or b"[]")]
        except Exception as exc:
            try:
                url = GEOCODER_FALLBACK + "?" + urllib.parse.urlencode(dict(q=query, limit=1))
                found = [(f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0],
                          f["properties"].get("name") or query)
                         for f in json.loads(fetch(url) or b"{}").get("features", [])]
            except Exception:
                raise ValueError(f"could not look up {query!r}: {exc}") from None
    if not found:
        raise ValueError(f"no place called {query!r} -- try adding the region, "
                         "or give it as latitude, longitude")
    _PLACES[key] = found[0]
    return _PLACES[key]


# ---------------------------------------------------------------- projection
#
# Everything is Web Mercator, as the tiles are, in "world" units: u runs 0..1
# west to east and v 0..1 north to south.  Mercator is conformal, so over a
# map a few hundred km across a square on the ground is a square on the model.

def to_world(lat, lon):
    s = math.sin(math.radians(max(-85.05, min(85.05, lat))))
    return (lon + 180.0) / 360.0, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


def to_latlon(u, v):
    lon = u * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * v))))
    return lat, lon


def metres_per_world(v):
    """Ground metres in one world unit, at the latitude of `v`."""
    return EARTH * math.cos(math.radians(to_latlon(0.5, v)[0]))


# ---------------------------------------------------------------- elevation

def tiles_for(u0, v0, u1, v1, zoom, limit=MAX_TILES):
    """The zoom (no deeper than asked, shallower if the area would need more
    than `limit` tiles) and the tile x, y ranges covering the box."""
    while zoom > 0:
        n = 2 ** zoom
        x0, x1 = int(math.floor(u0 * n)), int(math.floor(u1 * n))
        y0, y1 = int(math.floor(v0 * n)), int(math.floor(v1 * n))
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= limit:
            break
        zoom -= 1
    n = 2 ** zoom
    return (zoom, range(int(math.floor(u0 * n)), int(math.floor(u1 * n)) + 1),
            range(max(0, int(math.floor(v0 * n))), min(n - 1, int(math.floor(v1 * n))) + 1))


def elevation(us, vs, zoom):
    """Metres above sea level at every (us[j], vs[i]), as an (len(vs),
    len(us)) array, sampled bilinearly from the Terrarium tiles."""
    pad = 1.0 / (2 ** zoom * 256)
    zoom, xs, ys = tiles_for(us.min() - pad, vs.min() - pad, us.max() + pad, vs.max() + pad, zoom)
    n = 2 ** zoom
    urls = [ELEVATION.format(z=zoom, x=x % n, y=y) for y in ys for x in xs]
    mosaic = np.zeros((len(ys) * 256, len(xs) * 256), dtype=np.float32)
    for k, data in enumerate(fetch_all(urls)):
        i, j = divmod(k, len(xs))
        if not data:
            continue
        rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.float32)
        mosaic[i * 256:(i + 1) * 256, j * 256:(j + 1) * 256] = \
            rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0
    # pixel centres: pixel p covers [p, p+1) in tile-pixel units
    px = us * n * 256 - xs.start * 256 - 0.5
    py = vs * n * 256 - ys.start * 256 - 0.5
    px = np.clip(px, 0, mosaic.shape[1] - 1.001)
    py = np.clip(py, 0, mosaic.shape[0] - 1.001)
    x0, y0 = np.floor(px).astype(int), np.floor(py).astype(int)
    fx, fy = px - x0, py - y0
    X0, Y0 = np.meshgrid(x0, y0)
    FX, FY = np.meshgrid(fx, fy)
    m = mosaic
    return ((m[Y0, X0] * (1 - FX) + m[Y0, X0 + 1] * FX) * (1 - FY)
            + (m[Y0 + 1, X0] * (1 - FX) + m[Y0 + 1, X0 + 1] * FX) * FY), zoom


# ---------------------------------------------------------------- vector tiles

def _varint(buf, i):
    out = shift = 0
    while True:
        b = buf[i]
        i += 1
        out |= (b & 0x7F) << shift
        if b < 0x80:
            return out, i
        shift += 7


def _fields(buf):
    """(field number, wire type, value) for each field of one protobuf
    message; a length-delimited value comes back as its bytes."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _varint(buf, i)
        elif wire == 1:
            val, i = buf[i:i + 8], i + 8
        elif wire == 2:
            ln, i = _varint(buf, i)
            val, i = buf[i:i + ln], i + ln
        elif wire == 5:
            val, i = buf[i:i + 4], i + 4
        else:
            raise ValueError("unsupported protobuf wire type")
        yield field, wire, val


def _packed(buf):
    out, i = [], 0
    while i < len(buf):
        v, i = _varint(buf, i)
        out.append(v)
    return out


def _value(buf):
    for field, _, val in _fields(buf):
        if field == 1:
            return bytes(val).decode("utf-8", "replace")
        if field == 2:
            return struct.unpack("<f", val)[0]
        if field == 3:
            return struct.unpack("<d", val)[0]
        if field in (4, 5):
            return val
        if field == 6:
            return (val >> 1) ^ -(val & 1)
        if field == 7:
            return bool(val)
    return None


def _rings(cmds):
    """The geometry commands of one feature, as lists of (x, y) runs."""
    runs, run, x, y, i = [], [], 0, 0, 0
    while i < len(cmds):
        cmd, count = cmds[i] & 7, cmds[i] >> 3
        i += 1
        if cmd == 7:                       # ClosePath
            if run:
                run.append(run[0])
            continue
        for _ in range(count):
            dx, dy = cmds[i], cmds[i + 1]
            i += 2
            x += (dx >> 1) ^ -(dx & 1)
            y += (dy >> 1) ^ -(dy & 1)
            if cmd == 1:                   # MoveTo starts a new run
                if run:
                    runs.append(run)
                run = [(x, y)]
            else:
                run.append((x, y))
    if run:
        runs.append(run)
    return runs


def _polygon(runs):
    """MVT polygon rings into shapely: a ring with positive area in tile
    coordinates (y down) opens a polygon, a negative one is its hole."""
    polys, current, holes = [], None, []
    for ring in runs:
        if len(ring) < 4:
            continue
        a = np.asarray(ring, dtype=float)
        area = 0.5 * np.sum(a[:-1, 0] * a[1:, 1] - a[1:, 0] * a[:-1, 1])
        if area > 0:
            if current is not None:
                polys.append(Polygon(current, holes))
            current, holes = ring, []
        elif current is not None:
            holes.append(ring)
    if current is not None:
        polys.append(Polygon(current, holes))
    return [p if p.is_valid else p.buffer(0) for p in polys]


def decode(data, want):
    """{layer: [(class, geometry in tile units 0..extent, extent)]} for the
    layers named in `want`."""
    out = {name: [] for name in want}
    for field, _, layer in _fields(data):
        if field != 3:
            continue
        name, keys, values, feats, extent = None, [], [], [], 4096
        for f, _, v in _fields(layer):
            if f == 1:
                name = bytes(v).decode()
            elif f == 2:
                feats.append(v)
            elif f == 3:
                keys.append(bytes(v).decode())
            elif f == 4:
                values.append(_value(v))
            elif f == 5:
                extent = v
        if name not in want:
            continue
        for feat in feats:
            tags, kind, geom = [], 0, []
            for f, _, v in _fields(feat):
                if f == 2:
                    tags = _packed(v)
                elif f == 3:
                    kind = v
                elif f == 4:
                    geom = _packed(v)
            props = {keys[tags[k]]: values[tags[k + 1]] for k in range(0, len(tags) - 1, 2)}
            runs = _rings(geom)
            if kind == 3:
                geoms = _polygon(runs)
            elif kind == 2:
                geoms = [LineString(r) for r in runs if len(r) > 1]
            else:
                continue
            for g in geoms:
                out[name].append((props.get("class"), props, g, extent))
    return out


def water(u0, v0, u1, v1, zoom):
    """The water in the box, in world units: {"ocean": [...], "lake": [...],
    "river": [...] polygons, "waterway": [(class, line), ...]} -- and, from
    the same tiles, OpenStreetMap's woodland as "wood" polygons."""
    zoom, xs, ys = tiles_for(u0, v0, u1, v1, min(zoom, VECTOR_ZOOM_MAX), limit=36)
    n = 2 ** zoom
    template = vector_url()
    urls = [template.format(z=zoom, x=x % n, y=y) for y in ys for x in xs]
    found = {"ocean": [], "lake": [], "river": [], "waterway": [], "wood": []}
    for k, data in enumerate(fetch_all(urls)):
        if not data:
            continue
        i, j = divmod(k, len(xs))
        tx, ty = xs[j], ys[i]
        layers = decode(data, ("water", "waterway", "landcover"))
        for layer, items in layers.items():
            for cls, props, g, extent in items:
                # tile units to world units
                g = affinity.affine_transform(g, [1.0 / (extent * n), 0, 0, 1.0 / (extent * n),
                                                  tx / n, ty / n])
                if layer == "water":
                    if cls in OCEAN:
                        found["ocean"].append(g)
                    elif cls in LAKE:
                        found["lake"].append(g)
                    elif cls in RIVER_AREA:
                        found["river"].append(g)
                elif layer == "landcover":
                    # the forest outside BC, where there is no species data
                    if cls == "wood" and g.geom_type == "Polygon":
                        found["wood"].append(g)
                elif props.get("intermittent") not in (1, True):
                    found["waterway"].append((cls, g))
    return found, zoom


# ---------------------------------------------------------------- solids

def heightfield(xs, ys, z, floor=0.0):
    """A closed solid: the surface z[i, j] over (xs[j], ys[i]), walls down
    to `floor` all round, and a flat floor.  The floor is a fan from its
    centre to the ring of wall vertices, so it costs a few hundred triangles
    rather than a second copy of the grid."""
    ny, nx = z.shape
    X, Y = np.meshgrid(xs, ys)
    top = np.column_stack([X.ravel(), Y.ravel(), z.ravel()])
    idx = np.arange(nx * ny).reshape(ny, nx)
    a, b = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel()
    c, d = idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    faces = [np.column_stack([a, b, d]), np.column_stack([a, d, c])]
    # the boundary, once round, anticlockwise from the bottom-left corner
    ring = np.concatenate([idx[0, :], idx[1:, -1], idx[-1, -2::-1], idx[-2:0:-1, 0]])
    base = len(top)
    low = top[ring].copy()
    low[:, 2] = floor
    k = np.arange(len(ring))
    k1 = (k + 1) % len(ring)
    faces.append(np.column_stack([ring[k], base + k, base + k1]))
    faces.append(np.column_stack([ring[k], base + k1, ring[k1]]))
    centre = base + len(ring)
    faces.append(np.column_stack([np.full(len(ring), centre), base + k1, base + k]))
    verts = np.vstack([top, low, [[X.mean(), Y.mean(), floor]]])
    mesh = trimesh.Trimesh(verts, np.vstack(faces), process=False)
    if mesh.volume < 0:
        mesh.invert()
    return mesh


def pin_mesh(height, x, y, ground):
    """A map marker `height` mm tall standing on `ground` at (x, y)."""
    r = height * PIN_HEAD
    neck = min(PIN_NECK, r * 0.6)
    teardrop = shapely.convex_hull(unary_union([Point(0, height - r).buffer(r, 64),
                                                Point(0, 0).buffer(neck, 16)]))
    stem = box(-neck, -PIN_STEM, neck, 0.0)
    shape = unary_union([teardrop, stem])
    half = shape.intersection(box(0, -PIN_STEM - 1, r + 1, height + 1))
    # the right half's outline, from the bottom of the axis round to the top
    pts = np.asarray(half.exterior.coords)[:-1]
    on_axis = np.isclose(pts[:, 0], 0.0)
    start = int(np.argmin(np.where(on_axis, pts[:, 1], np.inf)))
    pts = np.roll(pts, -start, axis=0)
    if pts[1, 0] < 1e-9:                    # walking up the axis: turn round
        pts = np.vstack([pts[:1], pts[1:][::-1]])
    end = int(np.argmax(np.where(np.isclose(pts[:, 0], 0.0), pts[:, 1], -np.inf)))
    profile = pts[:end + 1]
    mesh = trimesh.creation.revolve(profile, sections=48)
    if mesh.volume < 0:
        mesh.invert()
    mesh.apply_translation((x, y, ground))
    return mesh


def mine_mesh(status, size, x, y, ground):
    """The marker for a mine of `status`, `size` mm tall at its tallest,
    standing on `ground` at (x, y)."""
    def ring(r, z, n, turn=0.0):
        a = np.linspace(0, 2 * np.pi, n, endpoint=False) + turn
        return np.column_stack([r * np.cos(a), r * np.sin(a), np.full(n, z)])
    s, down = size, -PIN_STEM
    if status == "producer":            # a headframe: a tower with a pointed top
        r = 0.25 * s
        pts = [ring(r, down, 4, np.pi / 4), ring(r, 0.7 * s, 4, np.pi / 4), [[0, 0, s]]]
    elif status == "past":              # a spoil heap
        r = 0.5 * s
        pts = [ring(r, down, 4, np.pi / 4), ring(r, 0.0, 4, np.pi / 4), [[0, 0, 0.45 * s]]]
    elif status == "developed":
        pts = [ring(0.28 * s, down, 6), ring(0.28 * s, 0.5 * s, 6)]
    elif status == "prospect":
        pts = [ring(0.22 * s, down, 6), ring(0.22 * s, 0.35 * s, 6)]
    else:                               # a showing
        pts = [ring(0.18 * s, down, 12), ring(0.18 * s, 0.2 * s, 12)]
    import manifold3d                   # its hull, rather than scipy for one
    m = manifold3d.Manifold.hull_points(np.vstack(pts).astype(np.float64)).to_mesh()
    mesh = trimesh.Trimesh(np.asarray(m.vert_properties)[:, :3], np.asarray(m.tri_verts),
                           process=False)
    mesh.apply_translation((x, y, ground))
    return mesh


def grid_polygons(grid, value, w, h):
    """The cells of `grid` equal to `value` as polygons in model mm: the
    grid spans the model, its top row the north edge.  Built a row of runs
    at a time, so a forest is a few thousand boxes, not a million pixels."""
    rows, cols = grid.shape
    cw, ch = w / cols, h / rows
    boxes = []
    for i in range(rows):
        row = np.concatenate([[False], grid[i] == value, [False]])
        edges = np.flatnonzero(row[1:] != row[:-1])
        y1 = h / 2 - i * ch
        for a, b in zip(edges[::2], edges[1::2]):
            boxes.append(box(-w / 2 + a * cw, y1 - ch, -w / 2 + b * cw, y1))
    return unary_union(boxes) if boxes else None


def solid_of(polys, z0, z1):
    """The polygons as one solid between z0 and z1 (disjoint prisms)."""
    polys = [p for p in polys if p.area > 1e-6]
    if not polys:
        return None
    meshes = solids.prisms(polys, z0, z1 - z0)
    return meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)


# How far simplifying may move the surface, in mm: a tenth of a layer.
SIMPLIFY = 0.03


def simplify(mesh, eps=SIMPLIFY):
    import manifold3d
    m = manifold3d.Manifold(manifold3d.Mesh(
        vert_properties=np.asarray(mesh.vertices, dtype=np.float32),
        tri_verts=np.asarray(mesh.faces, dtype=np.uint32))).simplify(eps).to_mesh()
    return trimesh.Trimesh(np.asarray(m.vert_properties)[:, :3], np.asarray(m.tri_verts),
                           process=False)


def pieces(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon" and not g.is_empty]


def inside(geom, X, Y):
    """Which grid nodes fall inside the polygons."""
    if geom is None or geom.is_empty:
        return np.zeros(X.shape, dtype=bool)
    shapely.prepare(geom)
    return shapely.contains_xy(geom, X, Y)


# ---------------------------------------------------------------- the map

def frame_box(size, shape):
    """(w, h) of the model in mm."""
    if shape == "rect":
        return size, round(size * ASPECT, 2)
    return size, size


def area(centre, span, pins, w, h):
    """(uc, vc, du, dv): the middle of the map and its width and height, in
    world units -- from a named centre and a span, or fitted round the pins."""
    if centre:
        uc, vc = to_world(*centre[:2])
        span_m = float(span or SPAN) * 1000.0
    elif pins:
        us = [to_world(p[0], p[1])[0] for p in pins]
        vs = [to_world(p[0], p[1])[1] for p in pins]
        uc, vc = (min(us) + max(us)) / 2.0, (min(vs) + max(vs)) / 2.0
        spread_u = (max(us) - min(us)) * (1 + 2 * PAD)
        spread_v = (max(vs) - min(vs)) * (1 + 2 * PAD)
        # fit both ways, keeping the model's own proportions
        du = max(spread_u, spread_v * w / h)
        span_m = du * metres_per_world(vc)
        if span:                            # a span given is a floor, not a cap
            span_m = max(span_m, float(span) * 1000.0)
        span_m = max(span_m, 2000.0)        # a single pin: a couple of km round it
        if len(pins) == 1 and not span:
            span_m = SPAN * 1000.0
    else:
        raise ValueError("say where: a centre, or at least one pin")
    span_m = min(max(span_m, SPAN_MIN * 1000.0), SPAN_MAX * 1000.0)
    du = span_m / metres_per_world(vc)
    return uc, vc, du, du * h / w


def build(pins=(), centre="", span=None, size=SIZE, shape="rect", exaggerate=EXAGGERATE,
          base=BASE, depth=DEPTH, river=RIVER, streams=False, pin_h=PIN,
          ocean=True, lakes=True, rivers=True, mines="mines", mine_h=MINE, forest="two",
          skin=SKIN, colours=solids.COLOURS, label=""):
    """One topographic map, as printable parts plus the numbers worth knowing.

    `pins` are place names or "lat, lon" strings; `centre` is one too, or
    empty to fit the map round the pins.  `span` is the ground the map covers
    across, in km.  `size` is the model's width in mm; `shape` is "rect"
    (4 : 3), "square" or "round".  `exaggerate` stretches the relief;
    `depth` is how thick the water's colour is; `river` is the width a river
    is drawn at in mm, whatever its real width.  `mines` is which mine sites
    get a marker ("off", or a key of layers.SHOW) and `mine_h` the tallest
    marker's height; `forest` is how the forest is coloured ("off", "one"
    shade, conifer and broadleaf as "two", or by "species" group) and `skin`
    how thick that colour is.
    """
    if shape not in SHAPES:
        raise ValueError(f"no such map shape: {shape}")
    if mines not in MINES:
        raise ValueError(f"no such choice of mines: {mines}")
    if forest not in layers.MODES:
        raise ValueError(f"no such forest colouring: {forest}")
    mine_h = min(max(float(mine_h), 1.5), 12.0)
    skin = min(max(float(skin), 0.2), 3.0)
    size = min(max(float(size), 30.0), 400.0)
    exaggerate = min(max(float(exaggerate), 0.1), EXAGGERATE_MAX)
    depth = min(max(float(depth), 0.2), 5.0)
    base = max(float(base), depth + 0.6)
    river = min(max(float(river), 0.0), 6.0)
    pin_h = min(max(float(pin_h), 0.0), 40.0)
    w, h = frame_box(size, shape)

    places = []
    for q in pins or ():
        q = (q or "").strip()
        if q:
            lat, lon, name = geocode(q)
            places.append(dict(query=q, lat=lat, lon=lon, name=name))
    middle = geocode(centre) if (centre or "").strip() else None
    uc, vc, du, dv = area(middle, span, [(p["lat"], p["lon"]) for p in places], w, h)
    u0, u1, v0, v1 = uc - du / 2, uc + du / 2, vc - dv / 2, vc + dv / 2
    ground_m = du * metres_per_world(vc)           # metres across the model
    mm_per_m = w / ground_m

    # world <-> model: x east, y north, the model centred on the origin
    def to_mm(u, v):
        return (u - uc) / du * w, -(v - vc) / dv * h

    world_to_mm = [w / du, 0, 0, -h / dv, -uc / du * w, vc / dv * h]

    # --- the grid
    cell = max(CELL, max(w, h) / MAX_CELLS)
    nx, ny = int(math.ceil(w / cell)) + 1, int(math.ceil(h / cell)) + 1
    xs, ys = np.linspace(-w / 2, w / 2, nx), np.linspace(-h / 2, h / 2, ny)
    us = uc + xs / w * du
    vs = vc - ys / h * dv
    want_zoom = int(math.ceil(math.log2(EARTH * math.cos(math.radians(to_latlon(uc, vc)[0]))
                                        / 256.0 / (cell / mm_per_m))))
    want_zoom = min(max(want_zoom, 1), ELEVATION_ZOOM_MAX)
    elev, zoom = elevation(us, vs, want_zoom)
    X, Y = np.meshgrid(xs, ys)

    # --- the water, as outlines on the model
    frame = box(-w / 2, -h / 2, w / 2, h / 2) if shape != "round" else Point(0, 0).buffer(w / 2, 256)
    clip = box(-w / 2 - 2, -h / 2 - 2, w / 2 + 2, h / 2 + 2)
    water_note = None
    try:
        found, vzoom = water(u0, v0, u1, v1, zoom)
    except Exception as exc:                 # the map still comes out; say so
        found, vzoom = {"ocean": [], "lake": [], "river": [], "waterway": [], "wood": []}, None
        water_note = f"no water data ({exc.__class__.__name__}); elevation only"

    def on_model(geoms):
        g = unary_union([affinity.affine_transform(x, world_to_mm) for x in geoms])
        return g.intersection(clip).simplify(0.05) if not g.is_empty else None

    sea = on_model(found["ocean"]) if ocean and found["ocean"] else None
    lake = on_model(found["lake"]) if lakes and found["lake"] else None
    if lake is not None:
        lake = unary_union([p for p in pieces(lake) if p.area >= MIN_LAKE]) or None
    lines = []
    if rivers and river > 0:
        for cls, g in found["waterway"]:
            if cls in ("river", "canal"):
                lines.append(affinity.affine_transform(g, world_to_mm).buffer(river / 2, 6))
            elif streams and cls in ("stream", "drain", "ditch"):
                lines.append(affinity.affine_transform(g, world_to_mm)
                             .buffer(min(STREAM, river) / 2, 6))
        lines += [affinity.affine_transform(g, world_to_mm) for g in found["river"]]
    flowing = unary_union(lines).intersection(clip).simplify(0.05) if lines else None
    # Where the vector data has no ocean but the ground is under the sea --
    # open sea with no tile, or the vector fetch failed -- the elevation is
    # the clue, and the sea is drawn on the grid instead.
    sea_from_ground = None
    if ocean and sea is None and (elev < -1.0).mean() > 0.02:
        sea_from_ground = elev < -1.0

    # --- the surfaces, in metres first
    in_sea = inside(sea, X, Y) if sea is not None else np.zeros(X.shape, bool)
    if sea_from_ground is not None:
        in_sea = sea_from_ground
    ground = elev.astype(float).copy()
    surface = ground.copy()                  # where the water's top is
    surface[in_sea] = 0.0
    lake_count = 0
    for p in pieces(lake):
        m = inside(p, X, Y)
        if m.any():
            # the elevation over a lake is the survey's guess at its surface;
            # the middle of those guesses is steadier than the edge's
            surface[m] = float(np.median(ground[m]))
        lake_count += 1
    wet = in_sea.copy()
    for p in pieces(lake):
        wet |= inside(p, X, Y)
    # under flat water the ground is the water's surface: the survey's
    # seabed is not the model's business -- the water's colour is, `depth`
    # of it, on a floor that never dips through the base
    ground[wet] = surface[wet]
    lowest = float(min(ground[~wet].min() if (~wet).any() else 0.0,
                       surface[wet].min() if wet.any() else np.inf))
    highest = float(max(ground.max(), surface.max()))
    relief_m = max(highest - lowest, 1.0)
    k = mm_per_m * exaggerate
    if relief_m * k > RELIEF_MAX:            # say so rather than print a spike
        k = RELIEF_MAX / relief_m
    z_ground = base + (ground - lowest) * k
    z_surface = base + (surface - lowest) * k
    # the water's floor: never less than `depth` under its surface, and
    # never above the ground
    z_floor = np.minimum(z_ground, z_surface - depth)
    # under the sea and the lakes the ground is the floor: the land part has
    # to stop where the water starts
    z_ground = np.where(wet, z_floor, z_ground)

    land = heightfield(xs, ys, z_ground)
    water_2d = unary_union([g for g in (sea, lake, flowing) if g is not None])
    if sea_from_ground is not None:
        # no outline for a sea found only from the ground: draw the grid's
        sea_poly = unary_union([box(x - cell / 2, y - cell / 2, x + cell / 2, y + cell / 2)
                                for x, y in zip(X[sea_from_ground], Y[sea_from_ground])])
        water_2d = unary_union([water_2d, sea_poly]) if not water_2d.is_empty else sea_poly
    water_2d = water_2d.intersection(frame) if not water_2d.is_empty else water_2d
    top = float(max(z_ground.max(), z_surface.max())) + pin_h + 10.0
    wet_solid = None
    if not water_2d.is_empty:
        prism = solid_of(pieces(water_2d.buffer(0)), -1.0, top)
        if prism is not None:
            under = heightfield(xs, ys, z_floor, floor=-0.5)
            over = heightfield(xs, ys, np.maximum(z_surface, z_floor + depth), floor=-0.25)
            wet_solid = solids.boolean("intersection", [prism, solids.boolean(
                "difference", [over, under])])
            land = solids.boolean("difference", [land, solids.boolean(
                "difference", [prism, under])])

    # --- the pins
    pin_solid = None
    marks = []
    for p in places:
        u, v = to_world(p["lat"], p["lon"])
        x, y = to_mm(u, v)
        fits = frame.buffer(-PIN_HEAD * pin_h).contains(Point(x, y))
        p.update(x=round(x, 2), y=round(y, 2), on_map=bool(fits))
        if not fits or pin_h <= 0:
            continue
        gz = float(_sample(xs, ys, np.maximum(z_ground, z_surface), x, y))
        marks.append(pin_mesh(pin_h, x, y, gz))
    if marks:
        pin_solid = solids.union(marks)

    # --- the mines
    sites, mine_sources, mine_note, mine_solid = [], [], None, None
    if mines != "off":
        nlat_, wlon_ = to_latlon(u0, v0)
        slat_, elon_ = to_latlon(u1, v1)
        try:
            sites, mine_sources = layers.mines(fetch, slat_, wlon_, nlat_, elon_)
        except Exception as exc:
            mine_note = f"no mine data ({exc.__class__.__name__}); the map has none"
        shown = layers.SHOW[mines]
        heads, kept = [], []
        inner = frame.buffer(-0.5 * mine_h)
        # A mining district is hundreds of sites in a few km; markers that
        # touch print as one lump.  So the biggest operations go first, and a
        # site whose marker would touch one already placed is left off.
        gap = 0.75 * mine_h
        sites.sort(key=lambda m: layers.STATUSES.index(m["status"]))
        for m in sites:
            u, v = to_world(m["lat"], m["lon"])
            x, y = to_mm(u, v)
            m.update(x=round(x, 2), y=round(y, 2), on_map=False, crowded=False)
            if m["status"] not in shown or not inner.contains(Point(x, y)):
                continue
            if any((x - a) ** 2 + (y - b) ** 2 < gap ** 2 for a, b in kept):
                m["crowded"] = True
                continue
            m["on_map"] = True
            kept.append((x, y))
            gz = float(_sample(xs, ys, np.maximum(z_ground, z_surface), x, y))
            heads.append(mine_mesh(m["status"], mine_h, x, y, gz))
        if heads:
            mine_solid = solids.union(heads)
            if pin_solid is not None:     # a pin on a mine wins
                mine_solid = solids.boolean("difference", [mine_solid, pin_solid])

    # Out of the land and the water now, so the forest -- taken from the
    # land next -- has their holes in it already.
    stood = [m for m in (pin_solid, mine_solid) if m is not None and len(m.faces)]
    if stood:
        land = solids.boolean("difference", [land] + stood)
        if wet_solid is not None:
            wet_solid = solids.boolean("difference", [wet_solid] + stood)


    # --- the forest: the top `skin` of the land wherever it is wooded,
    # one solid per shade.  In BC the province draws its inventory by
    # species group; anywhere else it is OpenStreetMap's woodland.
    woods, forest_note, forest_source, forest_share = {}, None, None, {}
    if forest != "off":
        nlat_, wlon_ = to_latlon(u0, v0)
        slat_, elon_ = to_latlon(u1, v1)
        classes = {}
        if layers.in_bc(slat_, wlon_, nlat_, elon_):
            px = min(FOREST_PX, nx)
            try:
                grid = layers.species_grid(fetch, (u0 - 0.5) * EARTH, (0.5 - v1) * EARTH,
                                           (u1 - 0.5) * EARTH, (0.5 - v0) * EARTH,
                                           px, max(1, round(px * h / w)))
            except Exception as exc:
                grid, forest_note = None, f"no species data ({exc}); woodland only"
            if grid is not None:
                forest_source = "BC Vegetation Resources Inventory"
                for n, (key, _, _) in enumerate(layers.SPECIES, start=1):
                    g = grid_polygons(grid, n, w, h)
                    if g is not None:
                        classes[key] = g
        if not classes and found["wood"]:
            g = on_model(found["wood"])
            if g is not None and not g.is_empty:
                forest_source = "OpenStreetMap woodland"
                classes["forest"] = g
        by_slot = {}
        for key, g in classes.items():
            slot = layers.slot_for(key, forest) if key != "forest" else "forest"
            by_slot.setdefault(slot, []).append(g)
        # inside the frame -- a hair inside, on a round map, so the disc cut
        # from the land below does not leave a sliver of forest proud of it
        edge = frame.buffer(-0.05) if shape == "round" else frame
        dry = edge if water_2d.is_empty else edge.difference(water_2d.buffer(0.2))
        lowered = None
        for slot, gs in by_slot.items():
            # close the pixel steps up a little and drop the specks
            g = unary_union(gs).buffer(0.3, 4).buffer(-0.3, 4).simplify(0.15)
            g = g.intersection(dry)
            polys = [p for p in pieces(g) if p.area >= MIN_FOREST]
            if not polys:
                continue
            prism = solid_of([p.buffer(0) for p in polys], -1.0, top)
            if prism is None:
                continue
            if lowered is None:
                # a cell wider all round than the land, so the two share no
                # wall for the boolean to make slivers of
                lowered = heightfield(np.concatenate([[xs[0] - 1], xs, [xs[-1] + 1]]),
                                      np.concatenate([[ys[0] - 1], ys, [ys[-1] + 1]]),
                                      np.pad(np.maximum(z_ground - skin, 0.3), 1, mode="edge"),
                                      floor=-0.5)
                shell = solids.boolean("difference", [land, lowered])
            piece = solids.boolean("intersection", [prism, shell])
            if len(piece.faces):
                woods[slot] = piece
                forest_share[slot] = round(sum(p.area for p in polys) / frame.area * 100, 1)
        if woods:
            land = solids.boolean("difference", [land] + list(woods.values()))
        elif forest_note is None and forest_source is None:
            forest_note = "no forest mapped here"

    # --- the shape
    if shape == "round":
        disc = trimesh.creation.cylinder(radius=w / 2, height=top + 4, sections=192)
        disc.apply_translation((0, 0, top / 2))
        land = solids.boolean("intersection", [land, disc])
        if wet_solid is not None:
            wet_solid = solids.boolean("intersection", [wet_solid, disc])

    # A flat lake is a few thousand grid triangles that all say the same
    # thing; simplifying within a few hundredths of a mm keeps the shape and
    # loses two thirds of the file.
    land = simplify(land)
    wet_solid = simplify(wet_solid) if wet_solid is not None else None
    pin_solid = simplify(pin_solid) if pin_solid is not None else None
    mine_solid = simplify(mine_solid) if mine_solid is not None else None
    woods = {slot: simplify(m) for slot, m in woods.items()}

    groups = [dict(slot="body", element="land", face="body", mesh=land)]
    if wet_solid is not None and len(wet_solid.faces):
        groups.append(dict(slot="pattern", element="water", face="front", mesh=wet_solid))
    if pin_solid is not None:
        groups.append(dict(slot="primary", element="pins", face="front", mesh=pin_solid))
    if mine_solid is not None and len(mine_solid.faces):
        groups.append(dict(slot="secondary", element="mines", face="front", mesh=mine_solid))
    for slot in solids.SLOTS:
        if slot in woods and len(woods[slot].faces):
            groups.append(dict(slot=slot, element="forest", face="front", mesh=woods[slot]))
    part = dict(name="", label=label or "topo map", card=0, groups=groups,
                assembled=np.eye(4))
    part["slots"] = solids.slot_meshes(part)
    part["mesh"] = trimesh.util.concatenate([g["mesh"] for g in groups]) \
        if len(groups) > 1 else land
    parts = [part]

    clat, clon = to_latlon(uc, vc)
    nlat, wlon = to_latlon(u0, v0)
    slat, elon = to_latlon(u1, v1)
    meshes = [g["mesh"] for g in groups]
    tall = float(part["mesh"].bounds[1][2])
    info = dict(
        kind="topo", label=label, text="", front_up=True,
        w=round(w, 2), h=round(h, 2), thick=round(base, 2), rise=0.0,
        face=solids.FACE, chamfer=0.0, shape=shape,
        centre=[round(clat, 5), round(clon, 5)],
        bounds=[round(slat, 5), round(wlon, 5), round(nlat, 5), round(elon, 5)],
        span_km=round(ground_m / 1000.0, 2), span_ns_km=round(ground_m * h / w / 1000.0, 2),
        scale=round(ground_m * 1000.0 / w),            # 1 : this
        exaggerate=round(k / mm_per_m, 2), asked_exaggerate=round(exaggerate, 2),
        capped=bool(k < mm_per_m * exaggerate - 1e-9),
        low_m=round(lowest), high_m=round(highest),
        relief=round((highest - lowest) * k, 2), base=round(base, 2), depth=round(depth, 2),
        cell=round(cell, 2), grid=[nx, ny], zoom=zoom, water_zoom=vzoom,
        ocean=bool(in_sea.any()), lakes=lake_count,
        rivers=len([1 for c, _ in found["waterway"] if c in ("river", "canal")]),
        streams=len([1 for c, _ in found["waterway"] if c in ("stream", "drain", "ditch")]),
        river=round(river, 2), water_area=round(float(water_2d.area) if not water_2d.is_empty
                                                 else 0.0, 1),
        water_note=water_note,
        pins=[dict(name=p["name"], query=p["query"], lat=round(p["lat"], 5),
                   lon=round(p["lon"], 5), on_map=p["on_map"]) for p in places],
        pin_h=round(pin_h, 2), attribution=ATTRIBUTION + " " + layers.ATTRIBUTION,
        mines=mines, mine_h=round(mine_h, 2), mine_sources=mine_sources, mine_note=mine_note,
        mine_counts={st: sum(1 for m in sites if m["status"] == st) for st in layers.STATUSES},
        mine_crowded=sum(1 for m in sites if m.get("crowded")),
        mine_sites=[dict(name=m["name"], status=m["status"], lat=round(m["lat"], 5),
                         lon=round(m["lon"], 5), commodities=m["commodities"])
                    for m in sites if m.get("on_map")],
        forest=forest, skin=round(skin, 2), forest_source=forest_source,
        forest_note=forest_note, forest_share=forest_share,
        slots=[g["slot"] for g in groups], parts=["map"],
        part_slots={"map": [g["slot"] for g in groups]},
        tag_mode="none", joint=None, tag=[0, 0, 0], pause_z=None, lines={},
        logo=None, qr=None, layout=None, look=None, pins_count=len(marks),
        too_big=bool(max(w, h) > BED), bed=BED,
        total_z=round(tall, 2),
        volume=round(sum(m.volume for m in meshes) / 1000.0, 2),
        watertight=all(m.is_watertight and m.is_winding_consistent for m in meshes),
        thin=[],
    )
    return parts, info


def _sample(xs, ys, z, x, y):
    """z bilinearly at (x, y) on the grid."""
    fx = np.interp(x, xs, np.arange(len(xs)))
    fy = np.interp(y, ys, np.arange(len(ys)))
    i0, j0 = int(min(fy, len(ys) - 2)), int(min(fx, len(xs) - 2))
    ty, tx = fy - i0, fx - j0
    return ((z[i0, j0] * (1 - tx) + z[i0, j0 + 1] * tx) * (1 - ty)
            + (z[i0 + 1, j0] * (1 - tx) + z[i0 + 1, j0 + 1] * tx) * ty)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pins", nargs="*", help='places to pin: names, or "lat, lon"')
    ap.add_argument("--centre", default="", help="the middle of the map (else: fit the pins)")
    ap.add_argument("--span", type=float, default=None, help="km across")
    ap.add_argument("--size", type=float, default=SIZE, help="mm across")
    ap.add_argument("--shape", choices=SHAPES, default="rect")
    ap.add_argument("--exaggerate", type=float, default=EXAGGERATE)
    ap.add_argument("--streams", action="store_true")
    ap.add_argument("--mines", choices=MINES, default="mines", help="which mine sites get a marker")
    ap.add_argument("--forest", choices=layers.MODES, default="two",
                    help="the forest in one green, conifer/broadleaf, or by species")
    ap.add_argument("--out", default="stl/topo.3mf", help=".3mf or .stl")
    a = ap.parse_args()
    t = time.time()
    parts, info = build(a.pins, centre=a.centre, span=a.span, size=a.size, shape=a.shape,
                        exaggerate=a.exaggerate, streams=a.streams, mines=a.mines,
                        forest=a.forest)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".stl":
        solids.plate(parts).export(out)
    else:
        out.write_bytes(solids.export_3mf(parts))
    print(f"{out}: {info['w']} x {info['h']} mm, {info['span_km']} km across "
          f"(1:{info['scale']:,}), relief x{info['exaggerate']} = {info['relief']} mm, "
          f"{info['lakes']} lakes, {info['rivers']} rivers, ocean={info['ocean']}, "
          f"pins={[p['name'] for p in info['pins']]}, "
          f"mines={len(info['mine_sites'])}, forest={info['forest_share']}, "
          f"watertight={info['watertight']}  ({time.time() - t:.1f}s)")
