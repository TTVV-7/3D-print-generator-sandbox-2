"""An NHL team's season as a trip: the arenas it plays in, and the flights
between them, for src/topo.py to pin and draw on a map.

    trip = nhl.season_trip(fetch, "VAN")

The schedule is the NHL's own (api-web.nhle.com), fetched fresh -- games move.
The flights are not published anywhere, so they are inferred from the
schedule: the team flies from each game straight to the next one's city, and
home only when the next game is at home.  Real teams sometimes fly home in
the middle of a long road trip, or fly in a day early via somewhere else;
this draws the trips a schedule implies, not the ones the charter flew.
"""
import datetime
import json
import math
from pathlib import Path

SCHEDULE = "https://api-web.nhle.com/v1/club-schedule-season/{team}/{season}"
DATA = json.loads((Path(__file__).resolve().parent / "nhl_venues.json").read_text())
TEAMS = DATA["teams"]
VENUES = DATA["venues"]
REGULAR, PRESEASON, PLAYOFFS = 2, 1, 3
EARTH_KM = 6371.0


def season_id(today=None):
    """"20262027" for the season that is on, or coming, on `today`: a new
    one from July, when the schedule is out."""
    today = today or datetime.date.today()
    y = today.year if today.month >= 7 else today.year - 1
    return f"{y}{y + 1}"


def km(a, b):
    """Great-circle distance between two (lat, lon), in km."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def great_circle(a, b, step_km=50.0):
    """Points along the great circle from a to b, as (lat, lon), every
    `step_km` or so: on a flat map a long flight is a curve."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    d = km(a, b) / EARTH_KM
    if d < 1e-9:
        return [a, b]
    n = max(2, int(km(a, b) / step_km) + 1)
    out = []
    for i in range(n + 1):
        f = i / n
        s1, s2 = math.sin((1 - f) * d) / math.sin(d), math.sin(f * d) / math.sin(d)
        x = s1 * math.cos(la1) * math.cos(lo1) + s2 * math.cos(la2) * math.cos(lo2)
        y = s1 * math.cos(la1) * math.sin(lo1) + s2 * math.cos(la2) * math.sin(lo2)
        z = s1 * math.sin(la1) + s2 * math.sin(la2)
        out.append((math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))))
    return out


def season_trip(fetch, team, season=None, preseason=False, geocode=None):
    """The season as a trip:

    - `stops`: every venue played at, with how many games there, home first;
    - `legs`: each flight, in order, as dict(src, dst, date, km);
    - `routes`: each city pair flown, either way, with how many times;
    - the totals worth printing beside it.

    `fetch(url, cache=False)` gets the schedule; `geocode(query)` places a
    venue that is not in nhl_venues.json.
    """
    team = (team or "").upper()
    if team not in TEAMS:
        raise ValueError(f"no NHL team {team!r}")
    season = season or season_id()
    data = fetch(SCHEDULE.format(team=team, season=season), cache=False)
    games = json.loads(data or b"{}").get("games", [])
    kinds = {REGULAR, PLAYOFFS} | ({PRESEASON} if preseason else set())
    games = sorted((g for g in games if g.get("gameType") in kinds),
                   key=lambda g: g.get("startTimeUTC") or g.get("gameDate"))
    if not games:
        raise ValueError(f"no {season[:4]}-{season[6:]} games for {TEAMS[team]['name']} yet")

    def where(name):
        v = VENUES.get(name)
        if v:
            return v["lat"], v["lon"], v["city"]
        if geocode is None:
            raise ValueError(f"do not know where {name} is")
        lat, lon, _ = geocode(name)
        VENUES[name] = dict(lat=lat, lon=lon, city=name)
        return lat, lon, name

    home = TEAMS[team]["arena"]
    stops, legs = {}, []

    def stop(name):
        if name not in stops:
            lat, lon, city = where(name)
            stops[name] = dict(venue=name, city=city, lat=lat, lon=lon, games=0,
                               home=name == home)
        return stops[name]

    stop(home)
    at, trip, longest = home, 0, (0, None)
    for g in games:
        venue = g["venue"]["default"]
        here = stop(venue)
        here["games"] += 1
        away = g["homeTeam"]["abbrev"] != team
        trip = trip + 1 if away else 0
        if trip > longest[0]:
            longest = (trip, g["gameDate"])
        if venue != at:
            a, b = stops[at], here
            legs.append(dict(src=at, dst=venue, date=g["gameDate"],
                             km=round(km((a["lat"], a["lon"]), (b["lat"], b["lon"])))))
            at = venue
    if at != home:                       # and home at the end of it
        a, b = stops[at], stops[home]
        legs.append(dict(src=at, dst=home, date=games[-1]["gameDate"],
                         km=round(km((a["lat"], a["lon"]), (b["lat"], b["lon"])))))

    routes = {}
    for leg in legs:
        key = tuple(sorted((leg["src"], leg["dst"])))
        routes.setdefault(key, dict(a=key[0], b=key[1], km=leg["km"], times=0))["times"] += 1
    far = max(legs, key=lambda x: x["km"]) if legs else None
    return dict(
        team=team, name=TEAMS[team]["name"], season=f"{season[:4]}-{season[6:]}",
        games=len(games), preseason=preseason,
        stops=sorted(stops.values(), key=lambda s: (not s["home"], -s["games"], s["venue"])),
        legs=legs, routes=sorted(routes.values(), key=lambda r: -r["times"]),
        flights=len(legs), total_km=sum(x["km"] for x in legs),
        longest_trip=longest[0], longest_trip_ends=longest[1],
        farthest=dict(src=far["src"], dst=far["dst"], km=far["km"]) if far else None,
    )
