"""
Base Django settings for the OSINT-Sentry backend.

Shared by dev.py and prod.py. Secrets and environment-specific values are read
from a .env file at the project root via django-environ.
"""

from datetime import timedelta
from pathlib import Path

import environ
from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured

# backend/settings/base.py -> backend/settings -> backend -> project root
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    USE_GIS=(bool, False),
    ALLOWED_HOSTS=(list, []),
)
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('SECRET_KEY')

DEBUG = env('DEBUG')

ALLOWED_HOSTS = env('ALLOWED_HOSTS')

# GeoDjango needs GDAL/GEOS installed on the machine, and a spatial database
# behind it. Off by default so the project runs on nothing but Python: it drops
# the spatial backend and the reportgis app. A database migrated with USE_GIS
# off has no reportgis tables — turning it on later needs a fresh migrate.
USE_GIS = env('USE_GIS')


# Application definition

DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

if USE_GIS:
    DJANGO_APPS.append('django.contrib.gis')

THIRD_PARTY_APPS = [
    'rest_framework',
    'rest_framework_simplejwt',
    'django_celery_results',
    'django_celery_beat',
]

LOCAL_APPS = [
    'core',
    'accounts',
    'credibility',
    'content',
    'social',
    'media',
    'security',
]

if USE_GIS:
    LOCAL_APPS.append('reportgis')

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # After authentication, so request.user is populated when it binds.
    'security.middleware.AuditActorMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'backend.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

# 'sqlite' needs nothing installed and is the default for local work.
# 'postgres' is the deployment target; switch with DB_BACKEND in .env.
DB_BACKEND = env('DB_BACKEND', default='sqlite')

if DB_BACKEND == 'sqlite':
    DATABASES = {
        'default': {
            'ENGINE': (
                'django.contrib.gis.db.backends.spatialite'
                if USE_GIS
                else 'django.db.backends.sqlite3'
            ),
            'NAME': env('DB_NAME', default=str(BASE_DIR / 'db.sqlite3')),
        }
    }
elif DB_BACKEND == 'postgres':
    DATABASES = {
        'default': {
            'ENGINE': (
                'django.contrib.gis.db.backends.postgis'
                if USE_GIS
                else 'django.db.backends.postgresql'
            ),
            'NAME': env('DB_NAME', default='osint_sentry'),
            'USER': env('DB_USER', default='postgres'),
            'PASSWORD': env('DB_PASSWORD', default=''),
            'HOST': env('DB_HOST', default='localhost'),
            'PORT': env('DB_PORT', default='5432'),
        }
    }
else:
    raise ImproperlyConfigured(
        f"DB_BACKEND must be 'sqlite' or 'postgres', got {DB_BACKEND!r}"
    )


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static and media files
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'mediafiles'

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Django REST Framework

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_FILTER_BACKENDS': (
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 25,
    'EXCEPTION_HANDLER': 'core.exceptions.api_exception_handler',
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
}


# Celery

# Default to kombu's filesystem transport: a real broker with a real worker
# process, but backed by directories instead of a Redis server. Point
# CELERY_BROKER_URL at redis:// once a broker is available.
CELERY_BROKER_URL = env('CELERY_BROKER_URL', default='filesystem://')

if CELERY_BROKER_URL.startswith('filesystem://'):
    _broker_dir = BASE_DIR / '.celery-broker'
    for _sub in ('queue', 'processed'):
        (_broker_dir / _sub).mkdir(parents=True, exist_ok=True)
    CELERY_BROKER_TRANSPORT_OPTIONS = {
        'data_folder_in': str(_broker_dir / 'queue'),
        'data_folder_out': str(_broker_dir / 'queue'),
        'processed_folder': str(_broker_dir / 'processed'),
        'store_processed': True,
        # Without this the transport drops its control exchange files into the
        # working directory, i.e. straight into the repo root.
        'control_folder': str(_broker_dir / 'control'),
    }
    (_broker_dir / 'control').mkdir(parents=True, exist_ok=True)

# Results go to the database via django-celery-results, so no result store has
# to be running either. Celery's own file:// backend cannot parse a Windows
# drive letter, so it is not an option here.
CELERY_RESULT_BACKEND = env('CELERY_RESULT_BACKEND', default='django-db')
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TIMEZONE = TIME_ZONE

# Schedules live in the database and are editable in the admin, so an operator
# can retune the crawl cadence without a redeploy.
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
CELERY_BEAT_SCHEDULE = {
    'ingest-active-sources-hourly': {
        'task': 'content.tasks.run_scheduled_ingest',
        'schedule': crontab(minute=0),
    },
    'rescore-sources-nightly': {
        'task': 'credibility.tasks.rescore_all_sources',
        'schedule': crontab(hour=3, minute=0),
    },
    'refresh-social-graph-scores-nightly': {
        'task': 'social.tasks.refresh_graph_scores',
        'schedule': crontab(hour=4, minute=0),
    },
}


# Pluggable model implementations. Each defaults to a dependency-free heuristic
# so the pipeline runs today; point these at real models as they land.

CREDIBILITY_SCORER = env('CREDIBILITY_SCORER', default='credibility.scoring.HeuristicScorer')

# Domain registration lookups over RDAP. This is the one part of Module 1 that
# makes an outbound request; point it at NullRegistryClient to run offline,
# which leaves domain age unknown rather than assuming anything.
CREDIBILITY_REGISTRY_CLIENT = env(
    'CREDIBILITY_REGISTRY_CLIENT', default='credibility.registry.RdapClient'
)
CREDIBILITY_RDAP_BOOTSTRAP = env(
    'CREDIBILITY_RDAP_BOOTSTRAP', default='https://rdap.org/domain/'
)
CREDIBILITY_RDAP_TIMEOUT = env.float('CREDIBILITY_RDAP_TIMEOUT', default=10.0)

# Identifies this system to every server it contacts. Put a real contact
# address here before pointing it at anyone else's infrastructure.
CREDIBILITY_USER_AGENT = env(
    'CREDIBILITY_USER_AGENT',
    default='OSINT-Sentry/1.0 (+registry lookup; contact: admin@localhost)',
)
CONTENT_ANALYZER = env('CONTENT_ANALYZER', default='content.pipeline.HeuristicAnalyzer')
CONTENT_FETCHER = env('CONTENT_FETCHER', default='content.fetchers.NullFetcher')
SOCIAL_GRAPH_SCORER = env('SOCIAL_GRAPH_SCORER', default='social.analysis.HeuristicGraphScorer')
SOCIAL_TEXT_SCORER = env('SOCIAL_TEXT_SCORER', default='social.analysis.HeuristicTextScorer')

# Sources scoring below this are not ingested from at all.
CONTENT_MIN_CREDIBILITY = env.float('CONTENT_MIN_CREDIBILITY', default=25.0)


# Where the analyst console is served from. Django hosts the API only, so the
# API root points visitors here rather than 404ing on /.
FRONTEND_URL = env('FRONTEND_URL', default='http://localhost:5173')
