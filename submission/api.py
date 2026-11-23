#!/web/urban-data/.env/bin/python
"""
Group 04 - NYC Access backend.

Serves the NYC Access front end (nyc-access.html) and a JSON API with the same
data the front end shows: subway stations on a map, colored by the weighted
availability of their elevators and escalators, filterable by borough, line,
equipment type, year and month.

Data lifecycle
  1. Collection  - pulled live from NY Open Data and cached in memory:
       - MTA NYCT Subway Elevator and Escalator Availability  (rc78-7x78)
       - MTA Subway Stations                                   (39hk-dx4f)
     /api/app/summary rebuilds the exact data structure the front end uses
     (station complex x equipment type x month) from the live records, so the
     map, Compare and Trends pages always show the latest MTA month.
  2. Submission, storage, updating - community issue reports are POSTed by the
     front end and stored in SQLite (reports.db next to this file). Anyone can
     read them; only the device that wrote a report can edit or delete it.
  3. Retrieval and analysis - the JSON endpoints below (weighted availability
     by station, equipment detail, search, reports).

Put nyc-access.html (and optionally about.html) in the same folder as this file.
Dependencies: starlette, uvicorn. Everything else is the standard library.
"""
import asyncio
import contextlib
import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
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


class HTTPError(Exception):
    def __init__(self, status, message, fields=None):
        super().__init__(message)
        self.status = status
        self.fields = fields


# ---------------------------------------------------------------- data access

def _get(url, params, timeout=30):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "group04-nyc-access"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
        except HTTPError as e:
            body = {"error": str(e)}
            if e.fields:
                body["fields"] = e.fields
            return JSONResponse(body, status_code=e.status)
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
    data = _app_state["data"]
    return JSONResponse({"status": "ok", "group": GROUP, "frontend": FRONTEND.exists(),
                         "cache_entries": len(_cache),
                         "live_data": {"loaded": data is not None,
                                       "generated": data["generated"] if data else None,
                                       "last_month": data["lastMonth"] if data else None,
                                       "last_error": _app_state["error"]},
                         "reports_db": DB_PATH.exists()})


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


# ================================================================ live app data
# Rebuilds, from the live NY Open Data records, the same structure the front
# end used to ship as a static snapshot (summary + per-station detail).
# Methodology matches the About page: availability = sum(hours available) /
# sum(total hours); records with missing or non-positive total hours are left
# out of availability but still count as monitored assets.

APP_TTL = 6 * 3600   # the MTA publishes monthly; refresh a few times a day
PAGE = 50000
EQUIP_FIELDS = ("month,equipment_code,equipment_type,borough,managed_by,station_mrn,station_name,"
                "station_complex_mrn,station_complex_name,_24_hour_hours_available,_24_hour_total_hours,"
                "scheduled_outages,unscheduled_outages,entrapments")
STATION_FIELDS = "station_id,complex_id,stop_name,borough,daytime_routes,ada,gtfs_latitude,gtfs_longitude"
ROUTE_ORDER = list("1234567ACEBDFMGJZLNQRWS")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

_app_state = {"t": 0.0, "data": None, "task": None, "error": None}


def _num(v):
    if v is None or v == "":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _int(v):
    n = _num(v)
    return int(n) if n is not None else 0


def _r1(n):
    return math.floor(n * 10 + 0.5) / 10


def _sort_routes(rs):
    return sorted(rs, key=lambda r: (ROUTE_ORDER.index(r) if r in ROUTE_ORDER else 99, r))


def _fetch_all(url, select):
    rows, offset = [], 0
    while True:
        page = _get(url, {"$select": select, "$limit": PAGE, "$offset": offset, "$order": ":id"}, timeout=120)
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        offset += PAGE


def build_app_data(raw_equip, raw_stations):
    report = {"rawRecords": len(raw_equip), "duplicatesRemoved": 0, "invalidRemoved": 0,
              "excludedFromAvailability": 0, "clampedHours": 0, "unmatchedStations": []}

    # ---- validate and clean equipment records
    seen = {}
    for x in raw_equip:
        month = (x.get("month") or "")[:7]
        code = (x.get("equipment_code") or "").strip()
        etype = x.get("equipment_type") if x.get("equipment_type") in TYPES else None
        cx = _num(x.get("station_complex_mrn"))
        if not MONTH_RE.match(month) or not code or not etype or cx is None:
            report["invalidRemoved"] += 1
            continue
        key = (month, code)
        if key in seen:
            report["duplicatesRemoved"] += 1
            continue
        ha, th = _num(x.get("_24_hour_hours_available")), _num(x.get("_24_hour_total_hours"))
        valid = th is not None and th > 0 and ha is not None and ha >= 0
        if valid and ha > th:
            ha = th
            report["clampedHours"] += 1
        if not valid:
            report["excludedFromAvailability"] += 1
        st = _num(x.get("station_mrn"))
        seen[key] = {
            "month": month, "code": code, "type": etype, "borough": x.get("borough"),
            "managedBy": x.get("managed_by") or "Unknown",
            "station": int(st) if st is not None else None, "stationName": x.get("station_name"),
            "complex": int(cx), "complexName": x.get("station_complex_name") or "",
            "ha": ha if valid else None, "th": th if valid else None,
            "so": _int(x.get("scheduled_outages")), "uo": _int(x.get("unscheduled_outages")),
            "en": _int(x.get("entrapments")),
        }
    recs = list(seen.values())
    report["cleanRecords"] = len(recs)
    if not recs:
        raise RuntimeError("NY Open Data returned no usable equipment records")

    # ---- continuous month axis
    present = sorted({r["month"] for r in recs})
    y, m = map(int, present[0].split("-"))
    months = []
    while f"{y}-{m:02d}" <= present[-1]:
        months.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    m_idx = {mo: i for i, mo in enumerate(months)}
    present_set = set(present)
    report["monthsWithoutRecords"] = [mo for mo in months if mo not in present_set]

    # ---- station join: equipment station_mrn = Subway Stations station_id
    by_station, by_complex = {}, {}
    for s in raw_stations:
        by_station.setdefault(str(s.get("station_id")), []).append(s)
        by_complex.setdefault(str(s.get("complex_id")), []).append(s)

    cmap = {}
    for r in recs:
        c = cmap.setdefault(r["complex"], {"id": r["complex"], "borough": r["borough"], "stations": {},
                                           "lastMonth": "", "latestName": r["complexName"]})
        if r["month"] >= c["lastMonth"]:
            c["lastMonth"], c["latestName"] = r["month"], r["complexName"]
        c["stations"][r["station"]] = r["stationName"]

    complexes = []
    for c in sorted(cmap.values(), key=lambda c: c["id"]):
        matched = []
        for sid in c["stations"]:
            ms = by_station.get(str(sid))
            if not ms:
                report["unmatchedStations"].append(sid)
                continue
            for s in ms:
                if BOROUGH_CODES.get(s.get("borough")) != c["borough"]:
                    report["unmatchedStations"].append(f"{sid} (borough mismatch)")
                if str(s.get("complex_id")) != str(c["id"]):
                    report["unmatchedStations"].append(f"{sid} (complex mismatch)")
                matched.append(s)
        geo = matched or by_complex.get(str(c["id"]), [])
        geo = [s for s in geo if _num(s.get("gtfs_latitude")) is not None and _num(s.get("gtfs_longitude")) is not None]
        if not geo:
            report["unmatchedStations"].append(f"complex {c['id']} (no location; left off the map)")
            continue
        every = by_complex.get(str(c["id"])) or geo
        routes = {rt for s in every for rt in (s.get("daytime_routes") or "").split()}
        ada_vals = [_int(s.get("ada")) for s in every]
        ada = "full" if all(v == 1 for v in ada_vals) else "partial" if any(v in (1, 2) for v in ada_vals) else "none"
        stops = []
        for s in every:
            if s.get("stop_name") and s["stop_name"] not in stops:
                stops.append(s["stop_name"])
        complexes.append({
            "id": c["id"],
            "name": re.sub(r"\s*-\s*Station$", "", c["latestName"]).strip(),
            "borough": c["borough"],
            "lat": round(sum(_num(s["gtfs_latitude"]) for s in geo) / len(geo), 6),
            "lon": round(sum(_num(s["gtfs_longitude"]) for s in geo) / len(geo), 6),
            "routes": _sort_routes(routes),
            "ada": ada,
            "stops": stops,
        })
    c_idx = {c["id"]: i for i, c in enumerate(complexes)}
    recs = [r for r in recs if r["complex"] in c_idx]

    # ---- complex x type x month aggregates
    n = len(months)

    def blank():
        return {"ha": [None] * n, "th": [None] * n, "so": [None] * n, "uo": [None] * n, "n": [0] * n}

    series = [[blank() for _ in TYPES] for _ in complexes]
    for r in recs:
        s = series[c_idx[r["complex"]]][TYPES.index(r["type"])]
        i = m_idx[r["month"]]
        s["n"][i] += 1
        s["so"][i] = (s["so"][i] or 0) + r["so"]
        s["uo"][i] = (s["uo"][i] or 0) + r["uo"]
        if r["th"] is not None:
            s["ha"][i] = (s["ha"][i] or 0) + r["ha"]
            s["th"][i] = (s["th"][i] or 0) + r["th"]
    for by_type in series:
        for s in by_type:
            s["ha"] = [None if v is None else _r1(v) for v in s["ha"]]
            s["th"] = [None if v is None else _r1(v) for v in s["th"]]

    # ---- equipment registry (distinct asset counts) and per-station detail
    reg, details = {}, {}
    for r in recs:
        e = reg.setdefault(r["code"], {"code": r["code"], "c": c_idx[r["complex"]], "t": TYPES.index(r["type"]),
                                       "st": r["station"], "mg": r["managedBy"], "m": []})
        e["m"].append(m_idx[r["month"]])
        d = details.setdefault(r["complex"], {}).setdefault(r["code"], {
            "code": r["code"], "type": r["type"], "station": r["station"], "stationName": r["stationName"],
            "managedBy": r["managedBy"], "ha": [None] * n, "th": [None] * n, "so": [None] * n,
            "uo": [None] * n, "en": [None] * n})
        i = m_idx[r["month"]]
        d["ha"][i] = None if r["ha"] is None else _r1(r["ha"])
        d["th"][i] = -1 if r["th"] is None else _r1(r["th"])  # -1 = reported without valid hours
        d["so"][i], d["uo"][i], d["en"][i] = r["so"], r["uo"], r["en"]
    equipment = []
    for code in sorted(reg):
        e = reg[code]
        runs = []
        for i in sorted(e.pop("m")):
            if runs and runs[-1][1] == i - 1:
                runs[-1][1] = i
            else:
                runs.append([i, i])
        e["runs"] = runs
        equipment.append(e)

    summary = {
        "meta": {
            "generated": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "live": True,
            "source": {
                "equipment": {"id": "rc78-7x78", "name": "MTA NYCT Subway Elevator and Escalator Availability: Beginning 2015",
                              "url": "https://data.ny.gov/d/rc78-7x78"},
                "stations": {"id": "39hk-dx4f", "name": "MTA Subway Stations", "url": "https://data.ny.gov/d/39hk-dx4f"},
            },
            "firstMonth": months[0],
            "lastMonth": months[-1],
            "report": report,
        },
        "months": months,
        "types": TYPES,
        "complexes": complexes,
        "series": series,
        "equipment": equipment,
    }
    station_detail = {cid: {"id": cid, "equipment": [v for _, v in sorted(codes.items())]}
                      for cid, codes in details.items()}
    return {
        "summary_json": json.dumps(summary, separators=(",", ":")).encode(),
        "details": station_detail,
        "station_ids": {c["id"] for c in complexes},
        "generated": summary["meta"]["generated"],
        "lastMonth": months[-1],
    }


async def _refresh_app_data():
    raw_equip, raw_stations = await asyncio.gather(
        asyncio.to_thread(_fetch_all, AVAIL_URL, EQUIP_FIELDS),
        asyncio.to_thread(_fetch_all, STATIONS_URL, STATION_FIELDS),
    )
    data = await asyncio.to_thread(build_app_data, raw_equip, raw_stations)
    _app_state.update(t=time.time(), data=data, error=None)
    return data


async def app_data():
    """Fresh data from memory; stale data is served while a refresh runs."""
    st = _app_state
    if st["data"] and time.time() - st["t"] < APP_TTL:
        return st["data"]
    if st["task"] is None or st["task"].done():
        st["task"] = asyncio.create_task(_refresh_app_data())
        st["task"].add_done_callback(_note_refresh_error)
    if st["data"]:
        return st["data"]
    return await asyncio.shield(st["task"])


def _note_refresh_error(task):
    if not task.cancelled() and task.exception():
        _app_state["error"] = str(task.exception())


@api
async def AppSummary(request):
    """GET /api/app/summary -> everything the map, Compare and Trends pages need, built live."""
    data = await app_data()
    return Response(data["summary_json"], media_type="application/json",
                    headers={"Cache-Control": "no-cache"})


@api
async def AppStation(request):
    """GET /api/app/stations/{id} -> monthly records for every elevator/escalator at one complex."""
    try:
        cid = int(request.path_params["id"])
    except ValueError:
        raise BadRequest("id must be a station complex number")
    data = await app_data()
    if cid not in data["details"]:
        raise HTTPError(404, "No equipment records for that station complex.")
    return JSONResponse(data["details"][cid])


# ================================================================ community reports
# Stored in SQLite next to this file. Each browser makes a random owner token
# and sends it as X-Owner-Token; only a hash is kept. Everyone can read every
# report; only the owner can edit or delete one.

DB_PATH = HERE / "reports.db"
CATEGORIES = ["Out of service", "Intermittent / stopping", "Damaged or unsafe", "Entrapment",
              "Missing or wrong signage", "Other"]
STATUSES = ["Still happening", "Resolved", "Not sure"]
REPORT_CODE_RE = re.compile(r"^E[LS][A-Z0-9]{2,6}$")
OBSERVED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_BODY = 8 * 1024
RATE_LIMIT = (20, 600)   # at most 20 new reports per IP per 10 minutes
_posts = {}


def _db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _init_db():
    with contextlib.closing(_db()) as con, con:
        con.execute("""CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY,
            owner_hash TEXT NOT NULL,
            station_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            equipment_code TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""")
        con.execute("CREATE INDEX IF NOT EXISTS reports_station ON reports(station_id)")


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _owner(request, required):
    token = request.headers.get("x-owner-token", "")
    if not TOKEN_RE.match(token):
        if required:
            raise HTTPError(401, "This device has no report key. Reload the page and try again.")
        return None
    return _hash(token)


def _row_out(row, owner_hash):
    return {
        "id": row["id"], "stationId": row["station_id"], "type": row["type"],
        "equipmentCode": row["equipment_code"], "category": row["category"],
        "observedAt": row["observed_at"], "description": row["description"], "status": row["status"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        "mine": owner_hash is not None and secrets.compare_digest(row["owner_hash"], owner_hash),
    }


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


async def _body(request):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPError(413, "Report is too large.")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise BadRequest("Body must be JSON.")
    if not isinstance(body, dict):
        raise BadRequest("Body must be a JSON object.")
    return body


def _clean_report(body, partial):
    """Validate a report (all fields, or only the ones sent when partial)."""
    out, errors = {}, {}

    def has(k):
        return k in body or not partial

    if has("stationId"):
        try:
            sid = int(body.get("stationId"))
        except (TypeError, ValueError):
            sid = None
        known = _app_state["data"]["station_ids"] if _app_state["data"] else None
        if sid is None or sid <= 0 or (known is not None and sid not in known):
            errors["stationId"] = "Choose the station."
        out["station_id"] = sid
    if has("type"):
        if body.get("type") not in TYPES:
            errors["type"] = "Equipment type must be Elevator or Escalator."
        out["type"] = body.get("type")
    if has("equipmentCode"):
        code = str(body.get("equipmentCode") or "").strip().upper()
        if code and not REPORT_CODE_RE.match(code):
            errors["equipmentCode"] = "Equipment IDs look like EL123 or ES456. Leave blank if unsure."
        out["equipment_code"] = code
    if has("category"):
        if body.get("category") not in CATEGORIES:
            errors["category"] = "Choose what kind of issue it was."
        out["category"] = body.get("category")
    if has("observedAt"):
        obs = str(body.get("observedAt") or "")
        if not OBSERVED_RE.match(obs):
            errors["observedAt"] = "Enter a valid date and time."
        else:
            try:
                t = datetime.strptime(obs, "%Y-%m-%dT%H:%M")
                # local time without a zone: allow for any US time zone
                if t > datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1):
                    errors["observedAt"] = "The date can't be in the future."
                elif t.year < 2015:
                    errors["observedAt"] = "Enter a date from 2015 onward."
            except ValueError:
                errors["observedAt"] = "Enter a valid date and time."
        out["observed_at"] = obs
    if has("description"):
        desc = str(body.get("description") or "").strip()
        if len(desc) < 10:
            errors["description"] = "Describe the issue in at least 10 characters."
        elif len(desc) > 1000:
            errors["description"] = "Keep the description under 1,000 characters."
        out["description"] = desc
    if has("status"):
        if body.get("status") not in STATUSES:
            errors["status"] = "Status must be one of: " + ", ".join(STATUSES)
        out["status"] = body.get("status")
    if errors:
        raise HTTPError(422, "Some fields need fixing.", errors)
    return out


def _db_list(station, limit):
    with contextlib.closing(_db()) as con:
        if station is None:
            rows = con.execute("SELECT * FROM reports ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM reports WHERE station_id=? ORDER BY created_at DESC LIMIT ?",
                               (station, limit)).fetchall()
        total = con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    return rows, total


def _db_insert(rec):
    with contextlib.closing(_db()) as con, con:
        con.execute("""INSERT INTO reports (id, owner_hash, station_id, type, equipment_code, category,
                       observed_at, description, status, created_at, updated_at)
                       VALUES (:id, :owner_hash, :station_id, :type, :equipment_code, :category,
                       :observed_at, :description, :status, :created_at, :updated_at)""", rec)
        return con.execute("SELECT * FROM reports WHERE id=?", (rec["id"],)).fetchone()


def _db_owned(rid, owner_hash):
    with contextlib.closing(_db()) as con:
        row = con.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise HTTPError(404, "That report no longer exists.")
    if not secrets.compare_digest(row["owner_hash"], owner_hash):
        raise HTTPError(403, "Only the device that wrote this report can change it.")
    return row


def _db_update(rid, owner_hash, fields):
    _db_owned(rid, owner_hash)
    fields = dict(fields, updated_at=_now())
    sets = ", ".join(f"{k}=:{k}" for k in fields)  # keys come from _clean_report only
    with contextlib.closing(_db()) as con, con:
        con.execute(f"UPDATE reports SET {sets} WHERE id=:rid", dict(fields, rid=rid))
        return con.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()


def _db_delete(rid, owner_hash):
    _db_owned(rid, owner_hash)
    with contextlib.closing(_db()) as con, con:
        con.execute("DELETE FROM reports WHERE id=?", (rid,))


@api
async def ReportsList(request):
    """GET /api/reports?station=<complex id>&limit=200 -> newest community reports first."""
    q = request.query_params
    station = q.get("station")
    if station is not None:
        if not station.isdigit():
            raise BadRequest("station must be a complex id")
        station = int(station)
    limit = q.get("limit", "200")
    if not limit.isdigit() or not 1 <= int(limit) <= 1000:
        raise BadRequest("limit must be 1-1000")
    owner = _owner(request, required=False)
    rows, total = await asyncio.to_thread(_db_list, station, int(limit))
    return JSONResponse({"total": total, "count": len(rows), "reports": [_row_out(r, owner) for r in rows]})


@api
async def ReportsCreate(request):
    """POST /api/reports -> store a new community report (JSON body, X-Owner-Token header)."""
    owner = _owner(request, required=True)
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in _posts.get(ip, []) if now - t < RATE_LIMIT[1]]
    if len(recent) >= RATE_LIMIT[0]:
        raise HTTPError(429, "Too many reports from this connection. Try again in a few minutes.")
    fields = _clean_report(await _body(request), partial=False)
    stamp = _now()
    rec = dict(fields, id="r" + secrets.token_hex(6), owner_hash=owner, created_at=stamp, updated_at=stamp)
    row = await asyncio.to_thread(_db_insert, rec)
    _posts[ip] = recent + [now]
    return JSONResponse(_row_out(row, owner), status_code=201)


@api
async def ReportsUpdate(request):
    """PUT /api/reports/{id} -> change any fields of your own report (e.g. just the status)."""
    owner = _owner(request, required=True)
    fields = _clean_report(await _body(request), partial=True)
    if not fields:
        raise BadRequest("Nothing to update.")
    row = await asyncio.to_thread(_db_update, request.path_params["id"], owner, fields)
    return JSONResponse(_row_out(row, owner))


@api
async def ReportsDelete(request):
    """DELETE /api/reports/{id} -> remove your own report."""
    owner = _owner(request, required=True)
    await asyncio.to_thread(_db_delete, request.path_params["id"], owner)
    return JSONResponse({"deleted": request.path_params["id"]})


# ================================================================ app

@contextlib.asynccontextmanager
async def lifespan(app):
    _init_db()
    # warm the live data in the background so the first visitor doesn't wait
    task = asyncio.create_task(app_data())
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    yield


routes = [
    Route("/", Home, methods=["GET"]),
    Route("/about.html", About, methods=["GET"]),
    Route("/api/health", Health, methods=["GET"]),
    Route("/api/meta", Meta, methods=["GET"]),
    Route("/api/stations", Stations, methods=["GET"]),
    Route("/api/stations/{id}", StationDetail, methods=["GET"]),
    Route("/api/equipment/{code}", EquipmentDetail, methods=["GET"]),
    Route("/api/search", Search, methods=["GET"]),
    Route("/api/app/summary", AppSummary, methods=["GET"]),
    Route("/api/app/stations/{id}", AppStation, methods=["GET"]),
    Route("/api/reports", ReportsList, methods=["GET"]),
    Route("/api/reports", ReportsCreate, methods=["POST"]),
    Route("/api/reports/{id}", ReportsUpdate, methods=["PUT"]),
    Route("/api/reports/{id}", ReportsDelete, methods=["DELETE"]),
]

middleware = [
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "PUT", "DELETE"],
               allow_headers=["Content-Type", "X-Owner-Token"]),
    Middleware(GZipMiddleware, minimum_size=1000),
]

app = Starlette(debug=False, routes=routes, middleware=middleware, lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run("api:app", host="127.0.0.1", port=8084, reload=False, workers=1)

