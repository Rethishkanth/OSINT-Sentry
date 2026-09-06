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

Only Python 3.11 and Node.js. There is deliberately **no server to install** for
local work — the database is SQLite and Celery uses kombu's filesystem transport
with results stored in the database.

## Setup

```bash
cp .env.example .env      # then set SECRET_KEY
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe manage.py migrate
.venv/Scripts/python.exe manage.py createsuperuser
```

## Switching to Postgres / PostGIS / Redis

Everything above is env-driven, so moving to real infrastructure is a `.env`
change plus the installs:

| Setting | Local default | Production |
| --- | --- | --- |
| `DB_BACKEND` | `sqlite` | `postgres` (+ `DB_NAME`/`DB_USER`/`DB_PASSWORD`) |
| `USE_GIS` | `False` | `True` — needs GDAL/GEOS (OSGeo4W) and a spatial DB |
| `CELERY_BROKER_URL` | `filesystem://` | `redis://localhost:6379/0` |

`USE_GIS=False` drops `django.contrib.gis` **and the whole `reportgis` app**, so
a database migrated with it off has no `GeoEvent`/`Report` tables. Turning it on
means installing the spatial libraries and re-running `migrate`.

With PostGIS, create the database and enable the extension once:

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

Smoke-test the worker:

```bash
.venv/Scripts/python.exe -c "import django,os;os.environ.setdefault('DJANGO_SETTINGS_MODULE','backend.settings.dev');django.setup();from core.tasks import add;print(add.delay(2,2).get(timeout=30))"
```

## API

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `GET /api/health/` | none | liveness check |
| `POST /api/auth/token/` | none | obtain JWT access/refresh pair |
| `POST /api/auth/token/refresh/` | none | refresh access token |
| `GET /api/sources/` | JWT | list tracked sources |
| `/admin/` | session | inspect all models |
