# Group 4 · NYC Access — submission files

Upload these three files to the matching sections on the submission site:

| Section | File |
|---|---|
| Front-End html | `nyc-access.html` |
| Back-End python | `api.py` |
| About Page html | `about.html` |

## What changed from the earlier upload
- **Backend (`api.py`)** now covers the full data lifecycle:
  - `/api/app/summary` and `/api/app/stations/{id}` rebuild the app's data live from NY Open Data (refreshed every 6 hours).
  - `/api/reports` (GET, POST, PUT, DELETE) stores community reports in SQLite (`reports.db`, created on first start). Anyone can read reports; only the browser that wrote one can edit or delete it.
- **Front end (`nyc-access.html`)** loads its data and community reports from the backend. If the backend can't be reached, it falls back to the snapshot built into the file.
- **About page (`about.html`)** adds three sections: How NYC Access works (infrastructure), API reference, and Prompt spec.

## Run locally
```bash
pip install starlette uvicorn
python api.py   # then open http://127.0.0.1:8084
```

Don't upload `reports.db`; the server creates its own.
