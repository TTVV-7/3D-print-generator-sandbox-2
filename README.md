# Topo map generator

A piece of the real world in relief, ready to 3D print: the ground from
satellite elevation data, the sea, lakes and rivers in blue, the forest in
shades of green by its leading tree species, a marker on every mine, and a
map pin standing on each place you name -- or a whole NHL team's season,
every arena pinned and every flight raised across a continent.  Plain Python -- shapely for the
2-D work, trimesh and manifold for the solids.

Moved here from [TTVV-7/3D-print-sandbox](https://github.com/TTVV-7/3D-print-sandbox),
where it was the sixth shape in that app's generator.  This repository has
the map on its own: `src/topo.py` builds it, `src/layers.py` fetches the
mines and the forests, `src/nhl.py` turns a team's schedule into a trip, `src/solids.py` is the part of the sandbox's
`cards.py` it needs (the booleans, the plate and the two 3MF writers), and
the page is a topo-only one.

## Running it

```
pip install -r requirements.txt
python3 src/app.py            # then open http://127.0.0.1:8765
```

Type some places, watch the map turn in the viewer, download a 3MF (land,
water and pins each on its own filament) or an STL (all three welded into
one).  It needs the network: the map is fetched for wherever you ask.

On Vercel, `public/index.html` is served as a static file and
`api/model.py` answers `/api/model` with the same handler `src/app.py` runs
locally.  A `GET /api/model` builds a small map and reports whether the
geometry libraries and the tile fetches work where it is deployed.

---

# How it works

Name some places and get the ground round them: the mountains and valleys
from satellite elevation data, the sea flat at sea level, every lake flat at
its own level, rivers following their valleys down, and a map pin standing
on each place.  Three colours -- land, water, pins -- each its own solid, so
the 3MF opens in the slicer with nothing to paint.

| | |
|---|---|
| ![Squamish and Whistler, with the rivers between them](previews/topo_map.png) | ![the Salish Sea, round, with Vancouver and Victoria pinned](previews/topo_map_round.png) |

Left, Squamish and Whistler, fitted round the two pins, the heights
stretched 2.5 times.  Right, a round one of the Salish Sea with Stanley Park
and Victoria pinned: the Fraser running out through its delta on the right,
the Gulf Islands down the middle.

## Finding the place: the globe

The *Globe* tab over the viewer is a globe you can spin and zoom from the
whole planet down to a street -- MapLibre, on OpenFreeMap's tiles, the same
OpenStreetMap the water comes from.  It shows the map you have as an orange
outline with its pins, and flies to it whenever it moves somewhere new.

- **Click** to put the middle of the map there (and, if *Across* is empty, a
  span a quarter as wide as the view).
- **Shift-click** to drop a pin there.
- **Use this view** takes the middle and the span from what the globe shows.
- **Build it** goes back to the model and builds.

The outline is a rectangle on the flat map, so on the globe a big one curves
-- that is the Mercator projection the map is built in.

## A hockey season

Pick an NHL team and the map becomes its season: fitted round every arena it
plays in, a pin on each (home a size taller), and every flight raised on top
of the ground and the sea, following the great circle -- so a long flight
bows north, as the plane does -- and wider for a route flown again.  The
readout has the flights, the km, the longest run of road games and the
farthest hop.

The schedule is the NHL's own, from `api-web.nhle.com`, fetched fresh rather
than cached on disk.  The flights are not published, so they are worked out
from it: the team flies from each game straight to the next one's city, and
home only when the next game is at home.  Real teams sometimes go home in
the middle of a long trip; this draws the trips the schedule implies.
Arenas, and the other rinks on the schedule -- Helsinki and Düsseldorf for
the Global Series, the outdoor games -- are in `src/nhl_venues.json`; a new
one is looked up the first time it turns up.

A continent at 150 mm is 1 : 40,000,000, so choosing a team also turns the
height stretch up to x40 (the Rockies come out a few mm), shrinks the pins,
and turns the mines and the forest off -- at that scale they are noise, and
slow.  Choosing *None* puts back what was there.  The height-stretch slider
is logarithmic, x0.5 to x100, for the same reason.

## Where the map comes from

Nothing is stored here; everything is looked up when you ask, and none of it
needs a key.

- **The ground** is the [AWS Terrain Tiles](https://registry.opendata.aws/terrain-tiles/)
  -- SRTM, GMTED, ETOPO1 and national surveys merged into one world -- as
  Terrarium PNGs, whose red, green and blue are the height in metres.  The
  zoom is picked so a pixel is about a grid cell on the model.
- **The water** is OpenStreetMap, as [OpenFreeMap](https://openfreemap.org)'s
  vector tiles: the `water` layer's ocean, lake and river polygons and the
  `waterway` layer's lines.  `src/topo.py` reads the protobuf itself rather
  than adding a dependency for it.
- **The places** are OpenStreetMap's [Nominatim](https://nominatim.org), one
  lookup a second as it asks, and Komoot's [Photon](https://photon.komoot.io)
  when Nominatim turns a busy address away.  A `latitude, longitude` is
  taken as given.

Tiles are cached on disk (`$TOPO_CACHE`, or the temp folder) and in memory,
so dragging a slider rebuilds from what is already there.  A new place takes
a while the first time -- a big map of a coastline is a few dozen tiles --
and seconds after that.  If the water cannot be fetched the map still comes
out, elevation only, and the readout says so; open sea with no tile is
found from the ground being under sea level.

## The water

The elevation data under a lake is whatever the survey made of a flat
shiny surface, and under the sea it is the seabed.  Neither is what a map
wants.  So the model is built from three surfaces on one grid -- the ground,
the water's surface, and a floor `depth` mm under it -- and the water is the
vector outline cut between the floor and the surface:

- the **sea** is flat at sea level;
- a **lake** is flat at the middle of the heights the survey found over it;
- a **river** keeps the slope of its valley.

The land is the ground with the water taken out of it.  Every lake and every
river is at least the water depth thick in its own colour, so it reads from
the side and not just from above.  Rivers are far too narrow to print at any
real map scale, so each is drawn at the *River width* instead (a stream at
most 0.6 mm); a wide river that OpenStreetMap maps as an area keeps its real
outline.  Lakes under 1.5 mm^2 on the model are left out -- the nozzle would
only smear them.

## The relief

At 150 mm across a 20 km map is 1 : 133,000, and at that scale a 2,000 m
mountain is 15 mm tall and a hill is nothing.  *Height stretch* multiplies
the heights (x1 is true to scale); the relief is never let past 60 mm,
however much it is stretched, and the readout says when that held it back.
The grid is 0.5 mm, coarser on a big map so the preview still gets to the
browser, and the finished solids are simplified within 0.03 mm -- a tenth of
a layer -- which takes the flat water from thousands of triangles to a few.

## The pins

A map marker turned on a lathe: a ball on a cone, point down, standing on the
ground at the place and set into it on a short stem so it does not snap off
at the point.  The cone is 17 degrees off vertical, so it prints without
supports.  A pin whose place falls off the map is left out and named in the
readout.

## The mines

Every mine site on the map gets a small marker, its shape saying how far the
site got:

| | |
|---|---|
| headframe -- a square tower with a point | a working mine |
| spoil heap -- a low pyramid | a past producer |
| hex stud | a developed prospect, or a prospect (shorter) |
| dot | a showing: mineral found, never worked |

*Show* picks how far down that list to go: mines (the default), mines and
prospects, or everything.  Each marker is a convex solid whose sides are no
shallower than 45 degrees, so it prints without support, and it stands on the
ground on the same stem as a pin.  A mining district is hundreds of sites in
a few km -- Leadville, Colorado has over four hundred on a 25 km map -- and
markers that touch print as one lump, so the working mines are placed first,
then the past producers and so on, and a site whose marker would touch one
already placed is left off.  The readout says how many, and names the worked
ones with what came out of them.

Inside British Columbia the sites are the BC Geological Survey's
[MINFILE](https://minfile.gov.bc.ca); everywhere else they are the USGS
[Mineral Resources Data System](https://mrdata.usgs.gov/mrds/).  MRDS is
worldwide but strongest in the US, and no longer updated.

## The forest

The top of the land, wherever it is wooded, is a thin skin -- *Forest depth*,
0.6 mm by default -- in a shade of green: one green for all of it, a dark one
for conifers and a pale one for broadleaf, or six, one per species group.
Alpine, rock, farmland and towns stay the land colour, so the treeline draws
itself.

In BC the colour is the leading species of each stand in the province's
[Vegetation Resources Inventory](https://www2.gov.bc.ca/gov/content/industry/forestry/managing-our-forest-resources/forest-inventory):
Douglas-fir, hemlock, cedar and cypress, spruce and true fir, pine and larch,
and broadleaf (alder, aspen, birch, maple and the rest).  An 85 km map is a
hundred thousand inventory polygons -- far too many to download -- so the
province's map server draws them, one flat colour per group and no
antialiasing, in tiles fetched at once, and the picture is read back as a
grid at the model's own resolution.  Checked against the polygons
themselves, the shares come out within a percent.

Outside BC there is no free species map, so the forest is OpenStreetMap's
woodland, from the same vector tiles as the water, in the one green.

## How many colours

Land, water, pins and mines are four; the forest adds one, two or six, and
a hockey season's flights one more.  A
four-head printer holds four, so the page says when a map needs more.  Two
swatches set to the same colour share a filament in the 3MF, so the fix is
usually to give the mines the pins' colour, or colour the forest more
simply.  The 3MF itself is written with however many filaments the map
uses, for a printer with an AMS or a patient hand on the colour changes.

## Printing them

Flat on the plate, land colour first.  The water, the forest, the pins and
the mines are their own solids sitting in pockets in the land, so a
multi-material printer swaps filament only on the layers where they are.  A
single colour printer can print the land alone (the STL is every colour
welded into one) and the water and forest painted afterwards: their pockets
are the outline.  A pale
land colour shows the relief best; the shadows do the work.

## From a terminal

```
python3 src/topo.py "Squamish, BC" "Whistler, BC" --exaggerate 2.5
python3 src/topo.py --centre "Lake Louise, Alberta" --span 15 --shape square --streams
python3 src/topo.py "49.2867, -123.1179" --span 60 --shape round --out stl/van.stl
python3 src/topo.py --centre "Leadville, Colorado" --span 25 --mines all --forest one
```

writes `stl/topo.3mf` unless `--out` says otherwise.  `--size` is the width in
mm.  With no `--centre` the map is fitted round the pins; with one, `--span`
is how many km it covers across.

