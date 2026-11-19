#!/web/urban-data/.env/bin/python
"""
Group 04 - NYC Access backend.

Serves the NYC Access front end (nyc-access.html) and a JSON API with the same
data the front end shows: subway stations on a map, colored by the weighted
availability of their elevators and escalators, filterable by borough, line,
equipment type, year and month.

Data is pulled live from NY Open Data and cached in memory:
  - MTA NYCT Subway Elevator and Escalator Availability  (rc78-7x78)
  - MTA Subway Stations                                   (39hk-dx4f)

Put nyc-access.html (and optionally about.html) in the same folder as this file.
Dependencies: starlette, uvicorn. Everything else is the standard library.
"""
import asyncio
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Route

GROUP = "04"
HERE = Path(__file__).resolve().parent
FRONTEND = HERE / "nyc-access.html"
ABOUT = HERE / "about.html"

AVAIL_URL = "https://data.ny.gov/resource/rc78-7x78.json"
STATIONS_URL = "https://data.ny.gov/resource/39hk-dx4f.json"
CACHE_TTL = 600            # aggregate queries: 10 minutes
STATIONS_TTL = 24 * 3600   # station locations rarely change
THRESHOLD = 0.85           # the front end flags stations "Below 85%"

BOROUGHS = ["Manhattan", "Brooklyn", "Queens", "Bronx"]
TYPES = ["Elevator", "Escalator"]
BOROUGH_CODES = {"M": "Manhattan", "Bk": "Brooklyn", "Q": "Queens", "Bx": "Bronx", "SI": "Staten Island"}
ADA = {"0": "none", "1": "full", "2": "partial"}
CODE_RE = re.compile(r"^[A-Z]{2}\d{1,4}[A-Z]?$")

_cache = {}


class BadRequest(Exception):
    pass


# ---------------------------------------------------------------- data access

def _get(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "group04-nyc-access"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


async def fetch(url, ttl=CACHE_TTL, **params):
    """GET a Socrata endpoint with SoQL params, cached in memory."""
    params = {"$" + k: v for k, v in params.items() if v is not None}
    key = url + json.dumps(params, sort_keys=True)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    data = await asyncio.to_thread(_get, url, params)
    _cache[key] = (time.time(), data)
    return data


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _ratio(a, t):
    return round(_f(a) / _f(t), 4) if _f(t) else None


async def station_index():
    """Station complexes keyed by complex id: name, borough, location, lines, ADA."""
    rows = await fetch(
        STATIONS_URL, ttl=STATIONS_TTL,
        select="complex_id, stop_name, borough, daytime_routes, ada, gtfs_latitude, gtfs_longitude",
        limit=5000,
    )
    out = {}
    for r in rows:
        cid = r.get("complex_id")
        if not cid:
            continue
        s = out.setdefault(cid, {"id": int(cid), "stops": [], "routes": [], "lats": [], "lons": [],
                                 "borough": BOROUGH_CODES.get(r.get("borough"), r.get("borough")),
                                 "ada": "none"})
        name = r.get("stop_name")
        if name and name not in s["stops"]:
            s["stops"].append(name)
        for route in (r.get("daytime_routes") or "").split():
            if route not in s["routes"]:
                s["routes"].append(route)
        s["lats"].append(_f(r.get("gtfs_latitude")))
        s["lons"].append(_f(r.get("gtfs_longitude")))
        ada = ADA.get(r.get("ada"), "none")
        if ada == "full" or (ada == "partial" and s["ada"] == "none"):
            s["ada"] = ada
    for s in out.values():
        s["name"] = " / ".join(s["stops"])
        s["lat"] = round(sum(s["lats"]) / len(s["lats"]), 6) if s["lats"] else None
        s["lon"] = round(sum(s["lons"]) / len(s["lons"]), 6) if s["lons"] else None
        s["routes"].sort(key=lambda x: (not x.isdigit(), x))
        del s["lats"], s["lons"]
    return out


async def all_months():
    rows = await fetch(AVAIL_URL, select="distinct month", order="month", limit=1000)
    return [r["month"][:7] for r in rows]


# ---------------------------------------------------------------- filters

def period_clause(q):
    """year alone = whole year; year + month = that month; neither = all time."""
    year, month = q.get("year"), q.get("month")
    if month and not year:
        raise BadRequest("Choose a year first.")
    if not year:
        return None, "all"
    if not re.fullmatch(r"\d{4}", year):
        raise BadRequest("year must look like 2025")
    if month:
        if not re.fullmatch(r"\d{1,2}", month) or not 1 <= int(month) <= 12:
            raise BadRequest("month must be 1-12")
        m = f"{year}-{int(month):02d}"
        return f"month='{m}-01T00:00:00'", m
    return f"month between '{year}-01-01T00:00:00' and '{year}-12-01T00:00:00'", year


def build_where(q, extra=None):
    clauses = []
    period, label = period_clause(q)
    if period:
        clauses.append(period)
    borough = q.get("borough")
    if borough:
        if borough not in BOROUGHS:
            raise BadRequest(f"borough must be one of {BOROUGHS}")
        clauses.append(f"borough='{borough}'")
    etype = q.get("type")
    if etype:
        if etype not in TYPES:
            raise BadRequest(f"type must be one of {TYPES}")
        clauses.append(f"equipment_type='{etype}'")
    if extra:
        clauses.append(extra)
    return " AND ".join(clauses) or None, label


def check_line(q):
    line = q.get("line")
    if line and not re.fullmatch(r"[0-9A-Z]{1,2}", line):
        raise BadRequest("line must be a route like A, 7 or GS")
    return line


def api(fn):
    async def wrapper(request):
        try:
            return await fn(request)
        except BadRequest as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": "NY Open Data could not be reached. Try again shortly.",
                                 "detail": str(e)}, status_code=502)
    return wrapper


# ---------------------------------------------------------------- pages

async def Home(request):
    if FRONTEND.exists():
        return FileResponse(FRONTEND, media_type="text/html")
    return HTMLResponse(
        f"<h1>NYC Access · Group {GROUP}</h1><p>Put <code>nyc-access.html</code> next to "
        f"<code>api.py</code> to serve the front end here. The API is live at "
        f"<a href='/api/meta'>/api/meta</a> and <a href='/api/stations'>/api/stations</a>.</p>"
    )


async def About(request):
    if ABOUT.exists():
        return FileResponse(ABOUT, media_type="text/html")
    return HTMLResponse("<p>about.html is not on this server yet.</p>", status_code=404)


# ---------------------------------------------------------------- JSON API

@api
async def Health(request):
    return JSONResponse({"status": "ok", "group": GROUP, "frontend": FRONTEND.exists(),
                         "cache_entries": len(_cache)})


@api
async def Meta(request):
    """Everything the filter panel needs: months, years, boroughs, lines, types."""
    months, stations = await asyncio.gather(all_months(), station_index())
    lines = sorted({r for s in stations.values() for r in s["routes"]},
                   key=lambda x: (not x.isdigit(), x))
    return JSONResponse({
        "group": GROUP,
        "months": months,
        "years": sorted({m[:4] for m in months}, reverse=True),
        "boroughs": BOROUGHS,
        "lines": lines,
        "equipment_types": TYPES,
        "threshold": THRESHOLD,
        "sources": {"availability": "https://data.ny.gov/d/rc78-7x78",
                    "stations": "https://data.ny.gov/d/39hk-dx4f"},
    })


@api
async def Stations(request):
    """
    GET /api/stations?borough=&line=&type=&year=&month=
    One row per station complex with weighted availability for the period.
    """
    q = request.query_params
    line = check_line(q)
    where, label = build_where(q)
    rows, index = await asyncio.gather(
        fetch(AVAIL_URL,
              select="station_complex_mrn, station_complex_name, borough,"
                     "sum(_24_hour_hours_available) as ha, sum(_24_hour_total_hours) as th,"
                     "sum(total_outages) as o, sum(unscheduled_outages) as uo,"
                     "sum(entrapments) as en, count(distinct equipment_code) as units",
              where=where, group="station_complex_mrn, station_complex_name, borough", limit=5000),
        station_index(),
    )
    merged = {}
    for r in rows:  # a complex can appear under more than one name; merge by id
        sid = r.get("station_complex_mrn")
        m = merged.setdefault(sid, {"ha": 0.0, "th": 0.0, "o": 0, "uo": 0, "en": 0, "units": 0,
                                    "fallback_name": r.get("station_complex_name"),
                                    "borough": r.get("borough")})
        for k in ("ha", "th"):
            m[k] += _f(r[k])
        for k in ("o", "uo", "en", "units"):
            m[k] += int(_f(r[k]))

    stations, tot_a, tot_t = [], 0.0, 0.0
    for sid, m in merged.items():
        info = index.get(sid, {})
        if line and line not in info.get("routes", []):
            continue
        avail = _ratio(m["ha"], m["th"])
        tot_a += m["ha"]
        tot_t += m["th"]
        stations.append({
            "id": int(sid) if sid and sid.isdigit() else sid,
            "name": info.get("name") or m["fallback_name"],
            "borough": m["borough"],
            "lat": info.get("lat"), "lon": info.get("lon"),
            "routes": info.get("routes", []),
            "ada": info.get("ada"),
            "units": m["units"],
            "availability": avail,
            "below_threshold": avail is not None and avail < THRESHOLD,
            "hours_available": round(m["ha"], 1),
            "total_hours": round(m["th"], 1),
            "outages": m["o"], "unscheduled_outages": m["uo"], "entrapments": m["en"],
        })
    stations.sort(key=lambda s: (s["availability"] is None, s["availability"] or 0))
    return JSONResponse({
        "period": label,
        "filters": {k: q.get(k) for k in ("borough", "line", "type", "year", "month") if q.get(k)},
        "overall_availability": _ratio(tot_a, tot_t),
        "count": len(stations),
        "below_threshold": sum(s["below_threshold"] for s in stations),
        "stations": stations,
    })


@api
async def StationDetail(request):
    """
    GET /api/stations/{id}?type=&year=&month=
    One station: its equipment for the period and its month-by-month availability.
    """
    sid = request.path_params["id"]
    if not sid.isdigit():
        raise BadRequest("station id is a number, e.g. 611")
    q = request.query_params
    where, label = build_where(q, f"station_complex_mrn='{sid}'")
    series_where, _ = build_where({"type": q.get("type")}, f"station_complex_mrn='{sid}'")
    equip, series, index = await asyncio.gather(
        fetch(AVAIL_URL,
              select="equipment_code, equipment_type, managed_by, station_name,"
                     "sum(_24_hour_hours_available) as ha, sum(_24_hour_total_hours) as th,"
                     "sum(total_outages) as o, sum(scheduled_outages) as so,"
                     "sum(unscheduled_outages) as uo, sum(entrapments) as en",
              where=where, group="equipment_code, equipment_type, managed_by, station_name",
              order="equipment_code", limit=500),
        fetch(AVAIL_URL,
              select="month, sum(_24_hour_hours_available) as ha, sum(_24_hour_total_hours) as th,"
                     "sum(total_outages) as o",
              where=series_where, group="month", order="month", limit=1000),
        station_index(),
    )
    if not series:
        return JSONResponse({"error": "Station data not found"}, status_code=404)
    info = index.get(sid, {})
    equipment = [{
        "code": r["equipment_code"],
        "type": r.get("equipment_type"),
        "managed_by": r.get("managed_by"),
        "location": r.get("station_name"),
        "availability": _ratio(r["ha"], r["th"]),
        "hours_available": round(_f(r["ha"]), 1),
        "total_hours": round(_f(r["th"]), 1),
        "outages": int(_f(r["o"])), "scheduled_outages": int(_f(r["so"])),
        "unscheduled_outages": int(_f(r["uo"])), "entrapments": int(_f(r["en"])),
    } for r in equip]
    ha = sum(e["hours_available"] for e in equipment)
    th = sum(e["total_hours"] for e in equipment)
    return JSONResponse({
        "id": int(sid),
        "name": info.get("name"),
        "borough": info.get("borough"),
        "lat": info.get("lat"), "lon": info.get("lon"),
        "routes": info.get("routes", []),
        "ada": info.get("ada"),
        "period": label,
        "availability": _ratio(ha, th),
        "equipment": equipment,
        "monthly": [{"month": r["month"][:7], "availability": _ratio(r["ha"], r["th"]),
                     "outages": int(_f(r["o"]))} for r in series],
    })


@api
async def EquipmentDetail(request):
    """GET /api/equipment/{code} -> full monthly history for one unit."""
    code = request.path_params["code"].upper()
    if not CODE_RE.match(code):
        raise BadRequest("equipment code looks like EL268X or ES102")
    rows = await fetch(
        AVAIL_URL,
        select="month, equipment_type, managed_by, station_name, station_complex_mrn, borough,"
               "_24_hour_hours_available, _24_hour_total_hours, am_peak_availability,"
               "pm_peak_availability, total_outages, scheduled_outages, unscheduled_outages,"
               "entrapments",
        where=f"equipment_code='{code}'", order="month", limit=1000,
    )
    if not rows:
        return JSONResponse({"error": f"No records for {code}"}, status_code=404)
    last = rows[-1]
    return JSONResponse({
        "code": code,
        "type": last.get("equipment_type"),
        "managed_by": last.get("managed_by"),
        "location": last.get("station_name"),
        "station_id": int(_f(last.get("station_complex_mrn"))) or None,
        "borough": last.get("borough"),
        "monthly": [{
            "month": r["month"][:7],
            "availability": _ratio(r.get("_24_hour_hours_available"), r.get("_24_hour_total_hours")),
            "am_peak": round(_f(r.get("am_peak_availability")), 4),
            "pm_peak": round(_f(r.get("pm_peak_availability")), 4),
            "hours_available": round(_f(r.get("_24_hour_hours_available")), 1),
            "total_hours": round(_f(r.get("_24_hour_total_hours")), 1),
            "outages": int(_f(r.get("total_outages"))),
            "scheduled_outages": int(_f(r.get("scheduled_outages"))),
            "unscheduled_outages": int(_f(r.get("unscheduled_outages"))),
            "entrapments": int(_f(r.get("entrapments"))),
        } for r in rows],
    })


@api
async def Search(request):
    """GET /api/search?q=times -> stations with equipment records whose name or line matches."""
    q = request.query_params.get("q", "").strip().lower()
    if not q:
        return JSONResponse({"query": "", "matches": []})
    if len(q) > 60:
        raise BadRequest("search is too long")
    rows, index = await asyncio.gather(
        fetch(AVAIL_URL, select="distinct station_complex_mrn", limit=5000),
        station_index(),
    )
    with_equipment = {r["station_complex_mrn"] for r in rows}
    matches = [
        {"id": s["id"], "name": s["name"], "borough": s["borough"], "routes": s["routes"]}
        for sid, s in index.items()
        if sid in with_equipment and (q in s["name"].lower() or q.upper() in s["routes"])
    ]
    matches.sort(key=lambda m: (not m["name"].lower().startswith(q), m["name"]))
    return JSONResponse({"query": q, "matches": matches[:20]})


routes = [
    Route("/", Home, methods=["GET"]),
    Route("/about.html", About, methods=["GET"]),
    Route("/api/health", Health, methods=["GET"]),
    Route("/api/meta", Meta, methods=["GET"]),
    Route("/api/stations", Stations, methods=["GET"]),
    Route("/api/stations/{id}", StationDetail, methods=["GET"]),
    Route("/api/equipment/{code}", EquipmentDetail, methods=["GET"]),
    Route("/api/search", Search, methods=["GET"]),
]

middleware = [Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])]

app = Starlette(debug=False, routes=routes, middleware=middleware)

if __name__ == "__main__":
    uvicorn.run("api:app", host="127.0.0.1", port=8084, reload=False, workers=1)

