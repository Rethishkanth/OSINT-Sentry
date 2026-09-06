# OSINT-Sentry

Django REST API backend + Vite/React frontend for the OSINT-Sentry analysis platform.

Covers Days 1–6 of the build plan: project skeleton and data models (1–3), plus
the credibility engine, content analysis pipeline, and social graph analysis
(4–6) with audit logging across every model.

## The ML seam

The plan specifies GBT+BERT credibility scoring, spaCy/BERTopic/RAG content
analysis, and an RGCN plus RoBERTa for social graph work. **None of those models
exist here.** Each one is an abstract base class with a dependency-free
heuristic implementation behind it, so the pipeline — tasks, gating, signals,
endpoints, admin — is real and testable today.

| Setting | Interface | Default (heuristic) | Plan calls for |
| --- | --- | --- | --- |
| `CREDIBILITY_SCORER` | `credibility.scoring.BaseScorer` | domain age / HTTPS / publisher rules | GBT + BERT |
| `CONTENT_ANALYZER` | `content.pipeline.BaseAnalyzer` | proper-noun NER, term-frequency topics, lead-sentence summary | spaCy, BERTopic, RAG |
| `CONTENT_FETCHER` | `content.fetchers.BaseFetcher` | `NullFetcher` — crawls nothing | Scrapy spiders |
| `SOCIAL_GRAPH_SCORER` | `social.analysis.BaseGraphScorer` | reciprocity + neighbourhood overlap | RGCN (PyG/DGL) |
| `SOCIAL_TEXT_SCORER` | `social.analysis.BaseTextScorer` | word lexicons | RoBERTa |

Every heuristic reports its own `model_version` (`heuristic-v1`, etc.) on the
rows it writes, so heuristic output is never mistaken for model output. Swapping
in a real model is a settings change; no call site moves.

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
frontend/         Vite + React analyst console, proxies /api to Django on :8000
  src/api.js      JWT client with transparent token refresh
  src/theme.css   console theme and cursor styling
  src/components/ Cursor, Panel, Login, shared table primitives
  src/modules/    one view per module, plus the overview
```

## The console

`frontend/` is a JWT-authenticated analyst console covering all five modules.
Log in with a superuser account; the token is exchanged at `/api/auth/token/`
and refreshed automatically when the 30-minute access token expires.

The interface is pointer-reactive throughout: a custom two-part cursor with
viewport crosshairs, a spotlight that unmasks the background grid as it moves,
per-panel sheen that tracks across each card, and a social graph whose nodes
label themselves and trace their edges as the pointer nears them. Cursor
position is written straight to the DOM in a `requestAnimationFrame` loop
rather than React state, since `mousemove` fires far faster than a component
should re-render.

Visual design is an original take on a high-contrast cyberpunk console —
acid yellow on near-black, angular clipped panels, cyan/magenta accents. No
third-party assets, fonts, or marks are used, and there are **no runtime npm
dependencies beyond React** — the graph and map are hand-rolled SVG.

Anyone who has asked their system for reduced motion gets a static, fully
legible console: the glitch, scanline, and cursor effects switch off.

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
| `GET /api/credibility/` `.../{id}/` | JWT | scores with tier, rationale, provenance hash |
| `POST /api/credibility/score/` | JWT | request a score (`wait: true` runs inline) |
| `GET /api/content/` `.../{id}/` | JWT | content with entities, topics, summary |
| `POST /api/content/ingest/` | JWT | submit a document; 409 if the credibility gate rejects it |
| `POST /api/content/{id}/analyze/` | JWT | re-run analysis |
| `GET /api/social/accounts/` | JWT | accounts with bot-likelihood and coordination scores |
| `GET /api/social/accounts/flagged/` | JWT | accounts over a bot-likelihood threshold |
| `GET /api/social/posts/` | JWT | posts with sentiment and toxicity |
| `POST /api/social/refresh-scores/` | JWT | queue a full graph rescore |
| `/admin/` | session | inspect all models |

## How the modules connect

1. Creating a `Source` fires a `post_save` signal that queues `score_source`
   on commit — scoring is never triggered by hand.
2. `ingest_item` puts every document through that score before saving it. A
   source below `CONTENT_MIN_CREDIBILITY` (default 25) is rejected, and one
   that has never been scored is scored inline rather than waved through.
3. A stored `ContentItem` queues `analyze_content`, which writes entities,
   topics, and a summary. Re-running replaces prior output instead of
   duplicating it.
4. `refresh_graph_scores` rebuilds the interaction graph and rescores every
   account in one bulk update.
5. Every model write outside the excluded apps lands in `AuditLogEntry` with
   the acting user, IP, and a field-level diff.

Periodic work is registered with Celery Beat (`DatabaseScheduler`, so schedules
are editable in the admin): hourly ingest, nightly rescore, nightly graph
refresh. Run it alongside the worker:

```bash
.venv/Scripts/celery.exe -A backend beat -l info
```
