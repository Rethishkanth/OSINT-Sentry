# OSINT-Sentry

Django REST API backend + Vite/React frontend for the OSINT-Sentry analysis platform.

This repo currently covers the **foundation phase** (Days 1–3 of the build plan):
project skeleton, core data models, admin, auth/DRF scaffolding, and Celery wiring.

## Layout

```
backend/          Django project (settings split into base/dev/prod)
  settings/       base.py, dev.py, prod.py — configured via .env
  celery.py       Celery app instance
core/             Source model, DRF router, shared exception handler, tasks
accounts/         Role groups (Analyst / Reviewer / Admin) as a data migration
credibility/      CredibilityScore
content/          ContentItem
social/           SocialAccount, InteractionEdge
media/            MediaItem, TamperingResult
reportgis/        GeoEvent (PostGIS PointField), Report — needs GDAL/GEOS
security/         AuditLogEntry
frontend/         Vite + React app, proxies /api to Django on :8000
```

## Prerequisites

| Component | Why | Status |
| --- | --- | --- |
| Python 3.11 + `.venv` | backend | installed |
| Node.js LTS | frontend | installed |
| PostgreSQL 18 | database | installed |
| PostGIS extension | `GeoEvent.location` | **not installed** — add via Stack Builder |
| GDAL / GEOS / PROJ (OSGeo4W) | GeoDjango | **not installed** |
| Memurai (Redis-compatible) | Celery broker | **not installed** |

Set `USE_GIS=False` in `.env` to run everything except the `reportgis` app while
the spatial libraries are missing.

## Setup

```bash
cp .env.example .env      # then fill in SECRET_KEY and DB_PASSWORD
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe manage.py migrate
.venv/Scripts/python.exe manage.py createsuperuser
```

Create the database and enable PostGIS once:

```sql
CREATE DATABASE osint_sentry;
\c osint_sentry
CREATE EXTENSION postgis;
SELECT PostGIS_version();
```

## Running

```bash
.venv/Scripts/python.exe manage.py runserver
```

```bash
cd frontend && npm run dev
```

```bash
.venv/Scripts/celery.exe -A backend worker -l info --pool=solo
```

`--pool=solo` is needed on Windows; the default prefork pool does not work there.

## API

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `GET /api/health/` | none | liveness check |
| `POST /api/auth/token/` | none | obtain JWT access/refresh pair |
| `POST /api/auth/token/refresh/` | none | refresh access token |
| `GET /api/sources/` | JWT | list tracked sources |
| `/admin/` | session | inspect all models |
