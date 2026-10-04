"""A whole world as a globe you can hold: the Moon, Mars, Mercury, Venus,
Ceres, Vesta, Pluto, Charon or the Earth, its real relief stretched until
you can feel it, printed as two halves that peg together on dowels and sit
on a stand.

    parts, info = globe.build("moon", diameter=100)

The heights are src/globes/<world>.png, baked from the USGS Astrogeology
mosaics (and the AWS Terrain Tiles for Earth) by tools/bake_globes.py: a
quarter of a degree a cell, which on a 100 mm globe is a fifth of a mm --
as fine as a nozzle draws.  Each world's .json says what a grey level is in
metres and the ellipsoid the heights are measured from, so Vesta comes out
the squashed potato it is rather than a ball with bumps.

The halves are cut at the equator.  Each is a closed solid -- the relief
from the equator to its pole, and a flat face across the cut -- printed cut
face down, so the dome needs no support.  Three dowel holes, unevenly
spaced, go into both cut faces at the same places, so the halves only go
together one way, with the continents lined up across the seam.
"""
import json
import math
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

import solids

DATA = Path(__file__).resolve().parent / "globes"
WORLDS = ("moon", "mars", "mercury", "venus", "ceres", "vesta", "pluto", "charon", "earth")

DIAMETER = 100.0
SPACING = 0.6             # mm between grid lines at the equator; "fine" is 0.35
RELIEF = 0.035            # the auto stretch: relief a 30th of the diameter...
STRETCH_MIN = 1.0         # ...but never flatter than the real thing
DOWEL_ANGLES = (20.0, 140.0, 255.0)   # unevenly spaced: one way round only
CLEARANCE = 0.15          # each side of a dowel in its hole

_GRIDS = {}


def world(key):
    """(heights in metres as a (rows, cols) array, its description)."""
    if key not in WORLDS:
        raise ValueError(f"no world called {key!r}")
    if key not in _GRIDS:
        meta = json.loads((DATA / f"{key}.json").read_text())
        levels = np.asarray(Image.open(DATA / f"{key}.png"), dtype=np.float64)
        # Where the spacecraft never looked -- the far side of Pluto and
        # Charon, which New Horizons flew past -- the baker filled each row
        # with its own mean: a run of one value.  Say how much.
        filled = 0
        for row in levels:
            _, counts = np.unique(row, return_counts=True)
            if counts.max() > 20:
                filled += counts.max()
        meta["unmapped"] = round(filled / levels.size, 2)
        _GRIDS[key] = (meta["low"] + levels * meta["step"], meta)
    return _GRIDS[key]


def sample(grid, lat, lon):
    """Bilinear heights at (lat, lon) in degrees, arrays of the same shape;
    the grid's cell centres run 90N to 90S and from 180W eastward."""
    rows, cols = grid.shape
    y = np.clip((90.0 - lat) / 180.0 * rows - 0.5, 0, rows - 1.0001)
    x = ((lon + 180.0) / 360.0 * cols - 0.5) % cols
    y0, x0 = np.floor(y).astype(int), np.floor(x).astype(int)
    fy, fx = y - y0, x - x0
    x1 = (x0 + 1) % cols
    y1 = np.minimum(y0 + 1, rows - 1)
    return ((grid[y0, x0] * (1 - fx) + grid[y0, x1] * fx) * (1 - fy)
            + (grid[y1, x0] * (1 - fx) + grid[y1, x1] * fx) * fy)


def hemisphere(radius_at, n_lon, n_lat, north=True):
    """A closed half-globe: the surface from the equator to the pole, whose
    radius at (lat, lon) is radius_at(lat, lon) in mm, and a flat face over
    the cut at z = 0, fanned from the centre."""
    sign = 1.0 if north else -1.0
    lats = sign * np.linspace(0.0, 90.0, n_lat + 1)[:-1]          # the pole is one vertex
    lons = np.linspace(-180.0, 180.0, n_lon, endpoint=False)
    LAT, LON = np.meshgrid(lats, lons, indexing="ij")
    r = radius_at(LAT, LON)
    phi, lam = np.radians(LAT), np.radians(LON)
    ring = np.stack([r * np.cos(phi) * np.cos(lam), r * np.cos(phi) * np.sin(lam),
                     r * np.sin(phi)], axis=-1).reshape(-1, 3)
    ring[:n_lon, 2] = 0.0                                          # the cut, exactly flat
    pole_r = float(radius_at(np.array([sign * 90.0]), np.array([0.0]))[0])
    pole = len(ring)
    centre = pole + 1
    verts = np.vstack([ring, [[0.0, 0.0, sign * pole_r]], [[0.0, 0.0, 0.0]]])
    idx = np.arange(n_lat * n_lon).reshape(n_lat, n_lon)
    nxt = np.roll(idx, -1, axis=1)
    a, b = idx[:-1].ravel(), nxt[:-1].ravel()
    c, d = idx[1:].ravel(), nxt[1:].ravel()
    faces = [np.column_stack([a, b, d]), np.column_stack([a, d, c])]
    top, top_n = idx[-1], nxt[-1]
    faces.append(np.column_stack([top, top_n, np.full(n_lon, pole)]))
    faces.append(np.column_stack([idx[0], np.full(n_lon, centre), nxt[0]]))
    mesh = trimesh.Trimesh(verts, np.vstack(faces), process=False)
    if north is False:
        mesh.invert()                       # the same winding, mirrored, faces in
    if mesh.volume < 0:
        mesh.invert()
    return mesh


def stand_mesh(seat_r, globe_r, height):
    """A turned stand: a low cone whose top is cut into a cup the globe sits
    in.  Returns the stand and the height of the globe's centre on it."""
    base_r, top_r = seat_r * 1.35, seat_r * 1.08
    profile = np.array([[0.0, 0.0], [base_r, 0.0], [base_r, 1.2], [top_r, height],
                        [0.0, height]])
    body = trimesh.creation.revolve(profile, sections=128)
    if body.volume < 0:
        body.invert()
    centre_z = height + math.sqrt(max(globe_r ** 2 - seat_r ** 2, 1.0))
    cup = trimesh.creation.icosphere(subdivisions=5, radius=globe_r + 0.3)
    cup.apply_translation((0.0, 0.0, centre_z))
    return solids.boolean("difference", [body, cup]), centre_z


def build(key="moon", diameter=DIAMETER, exaggerate=None, fine=False, stand=True,
          dowels=True, seas=True, colours=solids.COLOURS, label=""):
    """A globe of world `key`, `diameter` mm across at its reference radius.

    `exaggerate` stretches the heights; None picks a stretch that makes the
    relief about RELIEF of the diameter, but never less than true scale.
    `seas` (Earth only) floods the oceans flat at sea level; off, the seabed
    shows -- the Earth with its oceans drained.
    """
    grid, meta = world(key)
    diameter = min(max(float(diameter), 40.0), 250.0)
    a = float(meta["a"])
    b = float(meta.get("b") or a)
    s = diameter / 2.0 / a                                    # mm per metre
    heights = np.maximum(grid, 0.0) if key == "earth" and seas else grid
    lo, hi = float(heights.min()), float(heights.max())
    true_mm = (hi - lo) * s
    auto = max(STRETCH_MIN, RELIEF * diameter / max(true_mm, 1e-6))
    x = auto if not exaggerate else min(max(float(exaggerate), 0.1), 200.0)
    x = round(x, 2 if x < 10 else 1)

    def radius_at(lat, lon):
        phi = np.radians(lat)
        ref = a * b / np.sqrt((b * np.cos(phi)) ** 2 + (a * np.sin(phi)) ** 2)
        h = sample(heights, lat, lon)
        return np.maximum((ref + h * x) * s, diameter * 0.05)

    n_lon = int(min(1440, max(360, round(math.pi * diameter / (0.35 if fine else SPACING)))))
    n_lon -= n_lon % 4
    n_lat = n_lon // 4                                        # rows per hemisphere
    north = hemisphere(radius_at, n_lon, n_lat, north=True)
    south = hemisphere(radius_at, n_lon, n_lat, north=False)

    # the equator: the smallest radius round the cut bounds where holes go
    eq = radius_at(np.zeros(720), np.linspace(-180, 180, 720, endpoint=False))
    r_min = float(eq.min())
    dowel_d = min(max(0.04 * diameter, 3.0), 6.0)
    depth = min(max(0.08 * diameter, 5.0), 12.0)
    holes_at = [(0.42 * r_min * math.cos(math.radians(t)), 0.42 * r_min * math.sin(math.radians(t)))
                for t in DOWEL_ANGLES]
    pegs = []
    if dowels:
        for half, z0 in ((north, -1.0), (south, -depth)):
            cuts = []
            for hx, hy in holes_at:
                c = trimesh.creation.cylinder(radius=dowel_d / 2 + CLEARANCE, height=depth + 1.0,
                                              sections=48)
                c.apply_translation((hx, hy, z0 + (depth + 1.0) / 2))
                cuts.append(c)
            cut = solids.boolean("difference", [half] + cuts)
            if half is north:
                north = cut
            else:
                south = cut
        peg_len = 2 * depth - 1.0
        for hx, hy in holes_at:
            p = trimesh.creation.cylinder(radius=dowel_d / 2, height=peg_len, sections=48)
            p.apply_translation((0.0, 0.0, peg_len / 2))
            pegs.append(p)

    # The south half prints with its cut face down too: turned over about x,
    # which keeps it a rotation, not a mirror image.
    flip = trimesh.transformations.rotation_matrix(math.pi, (1, 0, 0))
    south.apply_transform(flip)

    globe_r = float(max(north.bounds[1][2], -south.bounds[0][2], r_min))
    stand_solid, centre_z = (stand_mesh(0.22 * diameter, globe_r, max(0.12 * diameter, 8.0))
                             if stand else (None, globe_r))
    lift = trimesh.transformations.translation_matrix((0.0, 0.0, centre_z))

    def part(name, mesh, slot, assembled):
        g = [dict(slot=slot, element=name, face="body", mesh=mesh)]
        p = dict(name=name, label=label or f"{meta['name']} globe", card=0, groups=g,
                 mesh=mesh, assembled=assembled)
        p["slots"] = solids.slot_meshes(p)
        return p

    parts = [part("north", north, "body", lift),
             part("south", south, "body", lift @ np.linalg.inv(flip))]
    if stand_solid is not None:
        parts.append(part("stand", stand_solid, "secondary", np.eye(4)))
    for i, p in enumerate(pegs):
        hx, hy = holes_at[i]
        # glued up, a dowel sits half in each hole, out of sight
        parts.append(part(f"dowel {i + 1}", p, "secondary",
                          lift @ trimesh.transformations.translation_matrix(
                              (hx, hy, -depth + 0.5))))

    # where the highest and the lowest places are, for the readout
    hi_i = np.unravel_index(np.argmax(heights), heights.shape)
    lo_i = np.unravel_index(np.argmin(heights), heights.shape)
    rows, cols = heights.shape
    at = lambda ij: (round(90 - (ij[0] + 0.5) * 180 / rows, 1),        # noqa: E731
                     round(-180 + (ij[1] + 0.5) * 360 / cols, 1))
    meshes = [p["mesh"] for p in parts]
    info = dict(
        kind="globe", world=key, name=meta["name"], credit=meta["credit"],
        w=round(diameter, 1), h=round(diameter, 1), diameter=round(diameter, 1),
        radius_km=round(a / 1000.0, 1), polar_km=round(b / 1000.0, 1),
        scale=round(a / (diameter / 2.0) * 1000.0),               # 1 : this
        exaggerate=x, auto_exaggerate=round(auto, 2 if auto < 10 else 1),
        true_relief=round(true_mm, 2), relief=round(true_mm * x, 2),
        high=dict(m=round(hi), at=at(hi_i)), low=dict(m=round(lo), at=at(lo_i)),
        seas=bool(seas) if key == "earth" else None, unmapped=meta["unmapped"],
        grid=[n_lon, n_lat * 2], fine=bool(fine), stand=bool(stand), dowels=len(pegs),
        dowel_d=round(dowel_d, 1), dowel_len=round(2 * depth - 1.0, 1),
        parts=[p["name"] for p in parts], slots=sorted({g["slot"] for p in parts for g in p["groups"]},
                                                        key=solids.SLOTS.index),
        pins=[], volume=round(sum(m.volume for m in meshes) / 1000.0, 1),
        watertight=all(m.is_watertight and m.is_winding_consistent for m in meshes),
        too_big=bool(diameter > 256), bed=256,
        attribution=f"Elevation: {meta['credit']}.",
    )
    return parts, info


if __name__ == "__main__":
    import argparse
    import time

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("world", choices=WORLDS)
    ap.add_argument("--diameter", type=float, default=DIAMETER)
    ap.add_argument("--exaggerate", type=float, default=None)
    ap.add_argument("--fine", action="store_true")
    ap.add_argument("--no-stand", action="store_true")
    ap.add_argument("--drain", action="store_true", help="Earth: show the seabed")
    ap.add_argument("--out", default=None, help=".3mf or .stl (default stl/<world>_globe.3mf)")
    a = ap.parse_args()
    t = time.time()
    parts, info = build(a.world, a.diameter, a.exaggerate, a.fine, not a.no_stand, seas=not a.drain)
    out = Path(a.out or f"stl/{a.world}_globe.3mf")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".stl":
        solids.plate(parts).export(out)
    else:
        out.write_bytes(solids.export_3mf(parts))
    print(f"{out}: {info['name']} {info['diameter']} mm, relief x{info['exaggerate']} = "
          f"{info['relief']} mm, grid {info['grid']}, parts {info['parts']}, "
          f"watertight={info['watertight']}  ({time.time() - t:.1f}s)")
