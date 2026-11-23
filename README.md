# NYC Access: subway elevator and escalator reliability

**Group 4 · AI for Urban Data final project**

NYC Access is a public web app that shows how reliable New York City subway elevators and escalators have been, station by station and month by month. It uses MTA open data. Riders can also submit and track issues they notice. Those reports are stored on the server and shared with everyone who uses the site.

- **About the data:** <https://aiforurbandata.com/final/04/about.html>
- **App:** served by `api.py` on the course server at `aiforurbandata.com/final/04/`

> NYC Access is an independent guide to public data, not an official MTA website. It shows **monthly historical records, not real-time equipment status**. For current outages, see the [MTA elevator and escalator status](https://www.mta.info/elevator-escalator-status) page.

---

## Repository layout

| Path | What it is |
|---|---|
| `submission/nyc-access.html` | **Front end** (final). One self-contained HTML file: React, Leaflet, CSS and a snapshot of the data are all inlined. |
| `submission/api.py` | **Backend** (final). A single-file Python web app built on Starlette and uvicorn. |
| `submission/about.html` | **About page** (final). Covers the dataset, methodology, infrastructure, API reference and prompt spec. |
| `submission/README.md` | Which file goes in which upload section, plus what changed in the final version. |
| `api (2).py`, `nyc-access g04.html`, `group4 modified.html`, `*.pyc` | Earlier uploads, kept for reference. |

## What the app does

| Page | Purpose |
|---|---|
| **Explore** | Map of 152 station complexes, colored by weighted availability. Filter by borough, line, equipment type, year and month. A timeline plays the history month by month. Open a station to see each elevator and escalator. |
| **Compare** | Up to 3 stations side by side, plus availability by borough. |
| **Trends** | Monthly availability by equipment type, a borough × year table, and unscheduled outages per asset-year. |
| **Community Reports** | Riders report an issue (station, equipment, category, time, description, status). Everyone can read the reports. Only the browser that submitted a report can edit or delete it. |

## Data

| Dataset | Used for |
|---|---|
| [MTA NYCT Subway Elevator and Escalator Availability: Beginning 2015](https://data.ny.gov/d/rc78-7x78) (`rc78-7x78`) | One record per elevator or escalator per month: hours available, total hours, scheduled and unscheduled outages, and entrapments. |
| [MTA Subway Stations](https://data.ny.gov/d/39hk-dx4f) (`39hk-dx4f`) | Station coordinates, current daytime routes and ADA status. |

Coverage as of November 2026: **84,474 records, January 2015 to October 2026, 702 assets at 152 station complexes.**

### How availability is calculated
- **Availability = Σ hours available ÷ Σ total reported hours.** Percentages are never averaged, so each asset counts in proportion to its reported time.
- Records with missing or zero total hours (2,588, mostly capital outages) are left out of availability. Their assets still count as monitored, and their outage counts are kept.
- The pipeline drops duplicate (month, equipment) rows and invalid rows, and caps available hours at the total.
- Equipment joins to stations on **station MRN = Station ID**. All 185 stations and all 152 complexes match.
- Lines are **current** daytime routes, not historical service.

## Data lifecycle

The project requirements ask for the full lifecycle of real, in-use data. NYC Access covers it in three stages.

```
NY Open Data ──SoQL/HTTPS──▶ api.py (Starlette) ──JSON /api/…──▶ nyc-access.html (browser)
                              ├─ memory cache, rebuilt every 6 h
                              └─ SQLite reports.db (community reports)
```

1. **Structure and collection.** `api.py` pages through both datasets (50,000 rows per request) and fetches only the fields it needs. It validates the rows, joins them and aggregates hour and outage sums by station complex × equipment type × month. The result is cached for 6 hours and refreshed in the background. The download starts when the server boots, so the first visitor doesn't wait.
2. **Submission, storage and updating.** Community reports are sent with `POST /api/reports`, checked again on the server and stored in SQLite. Each browser holds a random report key, and the server keeps only its SHA-256 hash. Only the matching browser can `PUT` (edit or change status) or `DELETE` a report. Limits: 8 KB per request and 20 new reports per connection per 10 minutes.
3. **Retrieval and analysis.** The front end loads `/api/app/summary` once (about 1 MB, 126 KB gzipped). It then computes weighted availability in the browser for every filter, timeline step and comparison. Station detail loads on demand.

**Fallback:** if the backend can't be reached (for example, the HTML is opened from disk), the page uses the data snapshot built into the file and labels it "server offline". Reports are then saved in that browser only.

## API

All endpoints return JSON. Errors come back as `{"error": "..."}`. Validation errors also include `fields`.

| Method | Endpoint | Returns |
|---|---|---|
| GET | `/api/app/summary` | All data for the map, Compare and Trends pages, rebuilt from live records |
| GET | `/api/app/stations/{id}` | Monthly records for every elevator and escalator at one complex |
| GET | `/api/stations?borough=&line=&type=&year=&month=` | Weighted availability per station |
| GET | `/api/stations/{id}` | One station's equipment and monthly availability |
| GET | `/api/equipment/{code}` | Full monthly history for one unit, e.g. `EL229` |
| GET | `/api/search?q=` | Stations whose name or line matches |
| GET | `/api/meta` | Months, years, boroughs, lines and equipment types |
| GET | `/api/reports?station=&limit=` | Community reports, newest first |
| POST | `/api/reports` | Create a report (JSON body plus an `X-Owner-Token` header) |
| PUT | `/api/reports/{id}` | Update your own report |
| DELETE | `/api/reports/{id}` | Delete your own report |
| GET | `/api/health` | Server status, live-data freshness and database state |

Community report fields: `stationId`, `type` (Elevator / Escalator), `equipmentCode` (optional, e.g. EL123), `category`, `observedAt`, `description` (10–1,000 characters), `status` (Still happening / Resolved / Not sure). The server sets `id`, `createdAt` and `updatedAt`.

## Run locally

Requires Python 3.9 or newer.

```bash
cd submission
pip install starlette uvicorn
python api.py
```

Open <http://127.0.0.1:8084> for the app and <http://127.0.0.1:8084/about.html> for the About page. The first load takes a few seconds while the server downloads the MTA data. `reports.db` is created next to `api.py`; don't commit or upload it.

To check that the backend is serving live data, open `/api/health` and look for `"loaded": true`. The Explore page should also say **"Latest from NY Open Data"**.

## Tech stack

- **Front end:** React 18, TypeScript, Vite and Leaflet, built into one HTML file with `vite-plugin-singlefile`. Map tiles come from Esri Light Gray Canvas, which needs no API key.
- **Backend:** Python with Starlette and uvicorn. Everything else is the standard library (`sqlite3`, `urllib`, `hashlib`).
- **About page:** static HTML with inline CSS and JavaScript, and no libraries.

## Accessibility

- Map tiers use one orange hue that darkens as availability falls. Each tier also has its own marker shape, a text label and a numeric legend.
- Map markers work with the keyboard. Station search is an ARIA combobox.
- Every chart has a text summary and a table view.
- The app respects `prefers-reduced-motion`, and the report form shows an error summary.
- Text colors meet WCAG 2.1 AA contrast.

## Limitations

- These are **monthly historical records**, not live status. The newest month depends on when the MTA publishes.
- Availability for a single piece of equipment doesn't tell you whether a whole station or journey is accessible.
- Community reports are **unverified**. They are never sent to the MTA and never mixed with official outage counts.
