"""What lies on the ground, for src/topo.py: the mines in it and the forests
over it.  Fetched on demand, like the terrain and the water, and none of it
needing a key.

- **Mines** are the British Columbia Geological Survey's MINFILE inside BC
  -- every mineral occurrence the province knows of, from a showing in a
  creek bed to a working mine -- and the USGS Mineral Resources Data System
  (MRDS) everywhere else.  Both are points with a name, a status and the
  commodities found there.
- **Forests** are BC's Vegetation Resources Inventory inside BC: polygons of
  forest, each with its leading tree species.  There are far too many of
  them to download for a map a hundred km across, so the province's map
  server draws them instead -- one flat colour per species group, no
  antialiasing -- and the picture is read back as a grid of classes.
  Outside BC there is no species data to be had for free, so the forest is
  OpenStreetMap's woodland from the same vector tiles as the water, in one
  shade.
"""
import re
import urllib.parse
import xml.etree.ElementTree as ET

import numpy as np

# Roughly British Columbia: a map that touches this asks the province first.
BC = (48.2, -139.1, 60.0, -114.0)              # south, west, north, east

MINFILE = ("https://openmaps.gov.bc.ca/geo/pub/wfs?service=WFS&version=2.0.0"
           "&request=GetFeature&outputFormat=json&srsName=EPSG:4326"
           "&typeName=WHSE_MINERAL_TENURE.MINFIL_MINERAL_FILE&count=3000"
           "&propertyName=MINFILE_NAME1,STATUS_DESCRIPTION,"
           + ",".join(f"COMMODITY_DESCRIPTION{i}" for i in range(1, 9)) + ",SHAPE")
MRDS = ("https://mrdata.usgs.gov/services/wfs/mrds?service=WFS&version=1.0.0"
        "&request=GetFeature&typeName=mrds&maxFeatures=3000")
VRI_WMS = "https://openmaps.gov.bc.ca/geo/pub/wms"
VRI_LAYER = "pub:WHSE_FOREST_VEGETATION.VEG_COMP_LYR_R1_POLY"

ATTRIBUTION = ("Mines: BC Geological Survey MINFILE / USGS MRDS. "
               "Forests: BC Vegetation Resources Inventory / OpenStreetMap.")

# --------------------------------------------------------------- mines

# Every source's word for how far a site got, to one of ours.
STATUSES = ("producer", "past", "developed", "prospect", "showing")
STATUS_WORDS = {
    "producer": "producer", "past producer": "past", "developed prospect": "developed",
    "prospect": "prospect", "showing": "showing", "occurrence": "showing",
    "anomaly": "showing", "plant": "producer",
}
STATUS_NAMES = {"producer": "working mine", "past": "past producer",
                "developed": "developed prospect", "prospect": "prospect",
                "showing": "showing"}
# Which statuses each choice on the page shows.
SHOW = {
    "mines": ("producer", "past"),
    "prospects": ("producer", "past", "developed", "prospect"),
    "all": STATUSES,
}

# MRDS gives commodities as codes.
COMMODITY = {
    "AU": "Gold", "AG": "Silver", "CU": "Copper", "PB": "Lead", "ZN": "Zinc",
    "FE": "Iron", "MO": "Molybdenum", "W": "Tungsten", "SN": "Tin", "NI": "Nickel",
    "CO": "Cobalt", "U": "Uranium", "HG": "Mercury", "SB": "Antimony", "MN": "Manganese",
    "CR": "Chromium", "PT": "Platinum", "PGE": "Platinum group", "BA": "Barium",
    "F": "Fluorite", "GYP": "Gypsum", "CLY": "Clay", "SDG": "Sand and gravel",
    "STN": "Stone", "SIL": "Silica", "COA": "Coal", "TAL": "Talc", "GRF": "Graphite",
    "AS": "Arsenic", "BI": "Bismuth", "CD": "Cadmium", "LI": "Lithium", "REE": "Rare earths",
    "TI": "Titanium", "V": "Vanadium", "BE": "Beryllium", "MG": "Magnesium",
}


def in_bc(south, west, north, east):
    s, w, n, e = BC
    return south < n and north > s and west < e and east > w


def _status(word):
    return STATUS_WORDS.get((word or "").strip().lower())


def minfile(fetch, south, west, north, east):
    """BC's mineral occurrences in the box."""
    bbox = f"{south},{west},{north},{east},urn:ogc:def:crs:EPSG::4326"
    data = fetch(MINFILE + "&bbox=" + urllib.parse.quote(bbox), timeout=40)
    import json
    out = []
    for f in json.loads(data or b"{}").get("features", []):
        p, g = f.get("properties") or {}, f.get("geometry") or {}
        status = _status(p.get("STATUS_DESCRIPTION"))
        if not status or g.get("type") != "Point":
            continue
        lon, lat = g["coordinates"][:2]
        goods = [p.get(f"COMMODITY_DESCRIPTION{i}") for i in range(1, 9)]
        out.append(dict(name=(p.get("MINFILE_NAME1") or "").strip().title(),
                        status=status, lat=lat, lon=lon,
                        commodities=[c.strip() for c in goods if c and c.strip()]))
    return out


def mrds(fetch, south, west, north, east):
    """The USGS's mineral sites in the box, anywhere in the world."""
    data = fetch(MRDS + f"&bbox={west},{south},{east},{north}", timeout=40)
    out = []
    if not data:
        return out
    root = ET.fromstring(data)
    ns = {"gml": "http://www.opengis.net/gml", "ms": "http://mapserver.gis.umn.edu/mapserver"}
    for site in root.iter("{http://mapserver.gis.umn.edu/mapserver}mrds"):
        status = _status(site.findtext("ms:dev_stat", "", ns))
        xy = site.findtext(".//gml:Point/gml:coordinates", "", ns).split()
        if not status or not xy:
            continue
        lon, lat = (float(v) for v in xy[0].split(",")[:2])
        codes = (site.findtext("ms:code_list", "", ns) or "").split()
        out.append(dict(name=(site.findtext("ms:site_name", "", ns) or "").strip(),
                        status=status, lat=lat, lon=lon,
                        commodities=[COMMODITY.get(c, c.title()) for c in codes]))
    return out


def mines(fetch, south, west, north, east):
    """(sites, source): MINFILE inside BC, MRDS outside it.  A map across the
    border gets both, each for its own side."""
    sites, sources = [], []
    if in_bc(south, west, north, east):
        sites += minfile(fetch, south, west, north, east)
        sources.append("BC MINFILE")
    s, w, n, e = BC
    if not (south >= s and north <= n and west >= w and east <= e):
        found = mrds(fetch, south, west, north, east)
        if sources:                       # MRDS has BC too; MINFILE is better there
            found = [m for m in found if not in_bc(m["lat"], m["lon"], m["lat"], m["lon"])]
        sites += found
        sources.append("USGS MRDS")
    return sites, sources


# --------------------------------------------------------------- forests

# The species groups, each with the first letters of the inventory's species
# codes that fall in it.  The order is the class number in the raster.
SPECIES = (
    ("fir", "Douglas-fir", ("F",)),
    ("hemlock", "hemlock", ("H",)),
    ("cedar", "cedar and cypress", ("C", "Y")),
    ("spruce", "spruce and true fir", ("S", "B")),
    ("pine", "pine and larch", ("P", "L", "J", "T")),
    ("broadleaf", "broadleaf", ("D", "A", "E", "M", "R", "Q", "W", "V", "G", "K", "U", "Z")),
)
CONIFER = ("fir", "hemlock", "cedar", "spruce", "pine")
MODES = ("off", "one", "two", "species")
STEP = 30                     # the red channel of class k is k * STEP


def _sld():
    rules = []
    for k, (_, _, letters) in enumerate(SPECIES, start=1):
        likes = "".join(
            '<ogc:PropertyIsLike wildCard="*" singleChar="." escape="!">'
            f'<ogc:PropertyName>SPECIES_CD_1</ogc:PropertyName><ogc:Literal>{c}*</ogc:Literal>'
            '</ogc:PropertyIsLike>' for c in letters)
        cond = f"<ogc:Or>{likes}</ogc:Or>" if len(letters) > 1 else likes
        rules.append(f'<Rule><ogc:Filter>{cond}</ogc:Filter><PolygonSymbolizer><Fill>'
                     f'<CssParameter name="fill">#{k * STEP:02x}0000</CssParameter>'
                     '</Fill></PolygonSymbolizer></Rule>')
    return ('<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld" '
            'xmlns:ogc="http://www.opengis.net/ogc"><NamedLayer>'
            f'<Name>{VRI_LAYER}</Name><UserStyle><FeatureTypeStyle>{"".join(rules)}'
            '</FeatureTypeStyle></UserStyle></NamedLayer></StyledLayerDescriptor>')


# The map server draws a few thousand polygons a second.  A map 80 km across
# is a hundred thousand of them, so it is asked for in TILE_KM pieces, all at
# once: the same picture in a fifth of the time.
TILE_KM = 25.0
MAX_TILES = 9


def species_grid(fetch, x0, y0, x1, y1, width, height):
    """An (height, width) array of species class numbers -- 0 for no forest,
    k for SPECIES[k - 1] -- over the Web Mercator box (metres), top row
    north.  None if the map server has nothing there (outside BC)."""
    import io
    import math
    from concurrent.futures import ThreadPoolExecutor

    from PIL import Image

    # Mercator metres are stretched by 1 / cos(latitude); BC is near 50.
    km = (x1 - x0) * 0.64 / 1000.0
    n = max(1, min(int(math.ceil(km / TILE_KM)), int(math.sqrt(MAX_TILES))))
    cols = np.linspace(0, width, n + 1).round().astype(int)
    rows = np.linspace(0, height, n + 1).round().astype(int)
    style = _sld()

    def one(ij):
        i, j = ij
        c0, c1, r0, r1 = cols[j], cols[j + 1], rows[i], rows[i + 1]
        if c1 <= c0 or r1 <= r0:
            return ij, None
        # rows run north to south, the bbox's y south to north
        bx0 = x0 + (x1 - x0) * c0 / width
        bx1 = x0 + (x1 - x0) * c1 / width
        by1 = y1 - (y1 - y0) * r0 / height
        by0 = y1 - (y1 - y0) * r1 / height
        query = urllib.parse.urlencode(dict(
            service="WMS", version="1.1.1", request="GetMap", layers=VRI_LAYER, styles="",
            srs="EPSG:3857", bbox=f"{bx0},{by0},{bx1},{by1}", width=c1 - c0, height=r1 - r0,
            format="image/png", transparent="true", format_options="antialias:none",
            SLD_BODY=style))
        # the style is several kB: too long for a URL, so it goes as a form
        return ij, fetch(VRI_WMS, timeout=60, form=query)

    grid = np.zeros((height, width), dtype=int)
    with ThreadPoolExecutor(max_workers=n * n) as pool:
        for (i, j), data in pool.map(one, [(i, j) for i in range(n) for j in range(n)]):
            if data is None:
                continue
            if not data.startswith(b"\x89PNG"):
                msg = re.sub(rb"<[^>]+>", b" ", data[:300]).decode(errors="replace").strip()
                raise ValueError(f"the forest map server said: {msg or 'nothing'}")
            rgba = np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"))
            part = np.rint(rgba[..., 0] / STEP).astype(int)
            part[(rgba[..., 3] < 128) | (part > len(SPECIES))] = 0
            grid[rows[i]:rows[i + 1], cols[j]:cols[j + 1]] = part
    return grid if grid.any() else None


def slot_for(key, mode):
    """The colour slot a species group is drawn in, for a forest mode."""
    if mode == "one":
        return "forest"
    if mode == "two":
        return "conifer" if key in CONIFER else "broadleaf"
    return key
