"""Bake each world's elevation into a small whole-globe grid for src/globe.py.

    python3 tools/bake_globes.py            # every world
    python3 tools/bake_globes.py moon mars  # just these

The sources are the USGS Astrogeology mosaics on S3 -- 8 GB for the Moon
alone -- and a globe a printer makes needs a quarter of a degree at most.  So
this reads them once, a row at a time with HTTP range requests (every one of
them is an uncompressed GeoTIFF, so a row's bytes are at a place that can be
worked out), averages each row down to COLS cells, and writes
src/globes/<world>.png: a 16-bit greyscale picture of the heights, plus
<world>.json saying what a grey level is in metres and what the heights are
measured from.  Earth comes from the AWS Terrain Tiles the maps use.

It takes a few minutes and a few hundred MB of downloads; the result is
about a megabyte a world, and is what is committed.
"""
import io
import json
import math
import struct
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "globes"      # beside the code: a hosted function carries src/
COLS, ROWS = 1440, 720            # a quarter of a degree
S3 = "https://asc-pds-services.s3.us-west-2.amazonaws.com/mosaic/"
AGENT = "3d-print-generator-sandbox-2 globe baker"

WORLDS = {
    "moon": dict(name="the Moon", file="Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif",
                 credit="LRO LOLA, NASA / GSFC; mosaic by USGS Astrogeology"),
    "mars": dict(name="Mars", file="Mars_MGS_MOLA_DEM_mosaic_global_463m.tif",
                 credit="MGS MOLA, NASA / GSFC; mosaic by USGS Astrogeology"),
    "mercury": dict(name="Mercury", file="Mercury_Messenger_USGS_DEM_Global_665m_v2.tif",
                    credit="MESSENGER, NASA / JHUAPL / CIW; DEM by USGS Astrogeology"),
    "venus": dict(name="Venus", file="Venus_Magellan_Topography_Global_4641m_v02.tif",
                  credit="Magellan altimetry, NASA / JPL; mosaic by USGS Astrogeology"),
    "ceres": dict(name="Ceres", file="Ceres_Dawn_FC_HAMO_DTM_DLR_Global_60ppd_Oct2016.tif",
                  credit="Dawn FC, NASA / JPL / DLR; DTM by DLR"),
    "vesta": dict(name="Vesta", file="Vesta_Dawn_HAMO_DTM_DLR_Global_48ppd.tif",
                  credit="Dawn FC, NASA / JPL / DLR; DTM by DLR"),
    "pluto": dict(name="Pluto", file="Pluto_NewHorizons_Global_DEM_300m_Jul2017_16bit.tif",
                  credit="New Horizons, NASA / JHUAPL / SwRI; DEM by USGS / LPI"),
    "charon": dict(name="Charon", file="Charon_NewHorizons_Global_DEM_300m_Jul2017_16bit.tif",
                   credit="New Horizons, NASA / JHUAPL / SwRI; DEM by USGS / LPI"),
    "earth": dict(name="Earth", tiles="https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
                  credit="AWS Terrain Tiles (SRTM, GMTED, ETOPO1 and others)",
                  radius=6371008.8),
}

SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8, 17: 8}
FORMS = {1: "B", 3: "H", 4: "I", 8: "h", 9: "i", 11: "f", 12: "d", 16: "Q", 17: "q"}


def get(url, start=None, end=None):
    headers = {"User-Agent": AGENT}
    if start is not None:
        headers["Range"] = f"bytes={start}-{end}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                return r.read()
        except Exception:
            if attempt == 3:
                raise


class Tiff:
    """Just enough of a (Big)TIFF to find a row of an uncompressed image."""

    def __init__(self, url):
        self.url = url
        head = get(url, 0, 15)
        self.e = "<" if head[:2] == b"II" else ">"
        self.big = struct.unpack(self.e + "H", head[2:4])[0] == 43
        off = (struct.unpack(self.e + "Q", head[8:16])[0] if self.big
               else struct.unpack(self.e + "I", head[4:8])[0])
        d = get(url, off, off + 8192)
        if self.big:
            n, ent, base, form = struct.unpack(self.e + "Q", d[:8])[0], 20, 8, "HHQ"
        else:
            n, ent, base, form = struct.unpack(self.e + "H", d[:2])[0], 12, 2, "HHI"
        self.tags = {}
        for i in range(n):
            raw = d[base + i * ent: base + (i + 1) * ent]
            tag, typ, count = struct.unpack(self.e + form, raw[:struct.calcsize(self.e + form)])
            nbytes = SIZES.get(typ, 1) * count
            inline = raw[-(8 if self.big else 4):]
            self.tags[tag] = (typ, count, inline, nbytes)

    def value(self, tag, default=None):
        if tag not in self.tags:
            return default
        typ, count, inline, nbytes = self.tags[tag]
        if nbytes <= len(inline):
            data = inline[:nbytes]
        else:
            ptr = struct.unpack(self.e + ("Q" if self.big else "I"), inline)[0]
            data = get(self.url, ptr, ptr + nbytes - 1)
        if typ == 2:
            return data.rstrip(b"\0").decode(errors="replace")
        vals = struct.unpack(self.e + FORMS[typ] * count, data)
        return vals[0] if count == 1 else vals


def gdal_item(meta, name, default):
    import re
    m = re.search(rf'name="{name}"[^>]*>([^<]+)<', meta or "")
    return float(m.group(1)) if m else default


def bake_tiff(key, world):
    t = Tiff(S3 + world["file"])
    w, h = t.value(256), t.value(257)
    bits, fmt = t.value(258), t.value(339, 1)
    if t.value(259, 1) != 1:
        raise ValueError(f"{world['file']} is compressed")
    rps = t.value(278, h)
    offsets = t.value(273)
    offsets = offsets if isinstance(offsets, tuple) else (offsets,)
    dtype = {(16, 2): "i2", (16, 1): "u2", (32, 3): "f4", (32, 2): "i4", (8, 1): "u1"}[(bits, fmt)]
    dtype = np.dtype(dtype).newbyteorder(t.e)
    meta = t.value(42112, "")
    scale, offset = gdal_item(meta, "SCALE", 1.0), gdal_item(meta, "OFFSET", 0.0)
    nodata = t.value(42113)
    nodata = float(nodata) if nodata not in (None, "") else None
    # the reference the heights are measured from: GeoDoubleParams carry the
    # semi-major and semi-minor axes (keys 2057 and 2058)
    keys, doubles = t.value(34735), t.value(34736)
    axes = {}
    if keys:
        for i in range(4, len(keys), 4):
            k, loc, _, idx = keys[i:i + 4]
            if loc == 34736 and k in (2057, 2058):
                axes[k] = doubles[idx] if isinstance(doubles, tuple) else doubles
    a = axes.get(2057)
    b = axes.get(2058, a)
    tie, px = t.value(33922), t.value(33550)
    print(f"{key}: {w} x {h} {dtype} scale {scale} offset {offset} nodata {nodata} "
          f"axes {a} {b} tie {tie[3:5] if tie else None} px {px[:2] if px else None}")

    bytes_per = dtype.itemsize
    edges = np.linspace(0, w, COLS + 1).round().astype(int)

    def row(i):
        # the middle source row of output row i (rows run north to south)
        r = min(h - 1, int((i + 0.5) * h / ROWS))
        start = offsets[r // rps] + (r % rps) * w * bytes_per
        vals = np.frombuffer(get(t.url, start, start + w * bytes_per - 1), dtype=dtype).astype(float)
        bad = ~np.isfinite(vals)
        if nodata is not None:
            bad |= vals == nodata
        vals = np.where(bad, 0.0, vals * scale + offset)
        sums = np.add.reduceat(vals, edges[:-1])
        counts = np.add.reduceat((~bad).astype(float), edges[:-1])
        out = np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)
        return i, out

    grid = np.full((ROWS, COLS), np.nan)
    with ThreadPoolExecutor(max_workers=16) as pool:
        for k, (i, vals) in enumerate(pool.map(row, range(ROWS))):
            grid[i] = vals
            if k % 120 == 0:
                print(f"  row {k}/{ROWS}", flush=True)
    # a column of grid cells that started at 0 deg rather than -180 is rolled
    # round so that every world's grid starts at the antimeridian
    west = tie[3] / (px[0] * w) * 360.0 if tie and px else -180.0
    if abs(west + 180.0) > 1.0:
        grid = np.roll(grid, int(round((west + 180.0) / 360.0 * COLS)), axis=1)
    return grid, a, b


def bake_earth(world):
    z = 4
    n = 2 ** z
    mosaic = np.zeros((n * 256, n * 256), dtype=np.float32)

    def tile(xy):
        x, y = xy
        img = Image.open(io.BytesIO(get(world["tiles"].format(z=z, x=x, y=y)))).convert("RGB")
        rgb = np.asarray(img, dtype=np.float32)
        return x, y, rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0

    with ThreadPoolExecutor(max_workers=16) as pool:
        for x, y, v in pool.map(tile, [(x, y) for y in range(n) for x in range(n)]):
            mosaic[y * 256:(y + 1) * 256, x * 256:(x + 1) * 256] = v
    lats = 90.0 - (np.arange(ROWS) + 0.5) * 180.0 / ROWS
    lons = -180.0 + (np.arange(COLS) + 0.5) * 360.0 / COLS
    s = np.sin(np.radians(np.clip(lats, -85.05, 85.05)))
    v = 0.5 - np.log((1 + s) / (1 - s)) / (4 * math.pi)
    u = (lons + 180.0) / 360.0
    py = np.clip((v * n * 256).astype(int), 0, n * 256 - 1)
    px = np.clip((u * n * 256).astype(int), 0, n * 256 - 1)
    grid = mosaic[py[:, None], px[None, :]].astype(float)
    return grid, world["radius"], world["radius"]


def save(key, world, grid, a, b):
    # Some models (Ceres) store the distance from the centre rather than the
    # height above the reference: take the reference off.
    if a and np.nanmin(grid) > 0.5 * a:
        grid = grid - a
    # holes (a missing pole, a gap in the survey) take their row's mean, then
    # the whole grid's
    for i in range(ROWS):
        bad = ~np.isfinite(grid[i])
        if bad.all():
            continue
        grid[i, bad] = np.nanmean(grid[i])
    grid[~np.isfinite(grid)] = np.nanmean(grid)
    lo, hi = float(grid.min()), float(grid.max())
    step = max((hi - lo) / 65000.0, 1.0)      # a metre is far finer than any print
    levels = np.round((grid - lo) / step).astype(np.uint16)
    OUT.mkdir(parents=True, exist_ok=True)
    Image.fromarray(levels).save(OUT / f"{key}.png", optimize=True)
    meta = dict(name=world["name"], credit=world["credit"], source=world.get("file", "AWS Terrain Tiles"),
                cols=COLS, rows=ROWS, west=-180.0, north=90.0,
                low=round(lo, 2), step=step, high=round(hi, 2),
                a=a, b=b,
                note="Heights in metres above the reference ellipsoid (a, b), "
                     "grid cell centres from 90N to 90S and 180W eastward.")
    (OUT / f"{key}.json").write_text(json.dumps(meta, indent=1))
    print(f"  {key}: {lo:.0f} to {hi:.0f} m, step {step:.3f} m, "
          f"{(OUT / f'{key}.png').stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    wanted = sys.argv[1:] or list(WORLDS)
    for key in wanted:
        world = WORLDS[key]
        if "tiles" in world:
            grid, a, b = bake_earth(world)
        else:
            grid, a, b = bake_tiff(key, world)
        save(key, world, grid, a, b)
