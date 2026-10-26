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
    ALLOWED_HOSTS=(list, []),
)
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('SECRET_KEY')

DEBUG = env('DEBUG')

ALLOWED_HOSTS = env('ALLOWED_HOSTS')

# Application definition

DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

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
    'reportgis',
    'security',
]

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
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': env('DB_NAME', default=str(BASE_DIR / 'db.sqlite3')),
        }
    }
elif DB_BACKEND == 'postgres':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
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

# The trained gradient-boosted scorer. Off by default: it needs the optional
# ML dependencies (requirements-ml.txt) and a model artifact, and a platform
# that will not start because a .joblib is missing is worse than one scoring
# on hand-written weights. Point CREDIBILITY_SCORER at
# credibility.ml.BlendedScorer to turn it on.
CREDIBILITY_ML_ARTIFACT = env(
    'CREDIBILITY_ML_ARTIFACT',
    default=str(BASE_DIR / 'credibility' / 'artifacts' / 'credibility_gbt.joblib'),
)
# Below this confidence the model declines to speak and contributes nothing.
# Chosen from the coverage table train_credibility_model prints, not by taste.
CREDIBILITY_ML_MIN_CONFIDENCE = env.float('CREDIBILITY_ML_MIN_CONFIDENCE', default=0.90)
# How far a confident model call may move the heuristic's score.
CREDIBILITY_ML_ADJUSTMENT = env.float('CREDIBILITY_ML_ADJUSTMENT', default=15.0)

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

# The transformer-backed analyzer. Off by default for the same reason the
# trained scorer is: it needs the optional ML dependencies and several
# hundred megabytes of downloaded weights, and a platform that will not start
# because a model file is missing is worse than one reading with regexes.
# Point CONTENT_ANALYZER at content.ml.TransformerAnalyzer to turn it on,
# after running: manage.py fetch_models --module content
CONTENT_MODEL_CACHE = env(
    'CONTENT_MODEL_CACHE', default=str(BASE_DIR / '.model-cache')
)
CONTENT_NER_MODEL = env('CONTENT_NER_MODEL', default='dslim/bert-base-NER')
CONTENT_EMBEDDING_MODEL = env(
    'CONTENT_EMBEDDING_MODEL', default='sentence-transformers/all-MiniLM-L6-v2'
)
# Below this the model is not confident enough to name an entity, and the
# entity is dropped rather than recorded with a shrug. Chosen from the
# precision table manage.py evaluate_ner prints, not by taste.
CONTENT_NER_MIN_CONFIDENCE = env.float('CONTENT_NER_MIN_CONFIDENCE', default=0.90)
# Cosine similarity a keyphrase must reach against the document before it is
# offered as a topic. An article with nothing above the floor gets no topics.
# 0.30 rather than 0.25 on the table manage.py evaluate_topics prints: it
# costs the weakest of five topics on one document in a hundred, and halves
# again the rate at which a phrase lifted out of an unrelated article would
# be accepted as this one's subject.
CONTENT_TOPIC_MIN_SIMILARITY = env.float(
    'CONTENT_TOPIC_MIN_SIMILARITY', default=0.30
)

# Module 2 reads real web pages. Point CONTENT_FETCHER at NullFetcher to make
# the system contact nobody, the same switch NullRegistryClient gives Module 1.
CONTENT_FETCHER = env('CONTENT_FETCHER', default='content.fetchers.WebFetcher')

# Everything the fetcher is allowed to do to someone else's server. These are
# limits, not tuning knobs: raising them makes this system a worse citizen.
CONTENT_USER_AGENT = env(
    'CONTENT_USER_AGENT',
    default='OSINT-Sentry/1.0 (+content fetcher; contact: admin@localhost)',
)
CONTENT_FETCH_TIMEOUT = env.float('CONTENT_FETCH_TIMEOUT', default=15.0)
# Applied to the stream, not to Content-Length. 2MB is a very long article.
CONTENT_MAX_BYTES = env.int('CONTENT_MAX_BYTES', default=2_000_000)
# Minimum seconds between two requests to the same host, unless the host's own
# robots.txt asks for longer - in which case it wins.
CONTENT_CRAWL_DELAY = env.float('CONTENT_CRAWL_DELAY', default=2.0)
CONTENT_MAX_REDIRECTS = env.int('CONTENT_MAX_REDIRECTS', default=5)
CONTENT_ROBOTS_CACHE_TTL = env.float('CONTENT_ROBOTS_CACHE_TTL', default=3600.0)
CONTENT_MAX_ITEMS_PER_FETCH = env.int('CONTENT_MAX_ITEMS_PER_FETCH', default=10)
# After finding a source has no readable feed, leave it alone this long.
# Discovery costs a homepage request plus a probe of each conventional path,
# and repeating that hourly against a site that simply has no feed is rude.
# A person clicking "Crawl feed" overrides it.
CONTENT_FEED_RETRY_AFTER = env.float('CONTENT_FEED_RETRY_AFTER', default=86_400.0)
# False stores each feed entry's own summary instead of fetching the article
# page - one request per source instead of one per article.
CONTENT_FETCH_BODIES = env.bool('CONTENT_FETCH_BODIES', default=True)
# Hostnames resolving to private, loopback, or link-local addresses are
# refused. Only turn this on to crawl a site on your own network, and never in
# an environment where users can submit URLs.
CONTENT_ALLOW_PRIVATE_HOSTS = env.bool('CONTENT_ALLOW_PRIVATE_HOSTS', default=False)
# Module 3 scores the shape of the interaction graph, not the contents of any
# profile. A trained RGCN subclasses social.analysis.BaseGraphScorer and is
# named here; the tasks that call it do not change.
SOCIAL_GRAPH_SCORER = env('SOCIAL_GRAPH_SCORER', default='social.analysis.HeuristicGraphScorer')
SOCIAL_TEXT_SCORER = env('SOCIAL_TEXT_SCORER', default='social.analysis.HeuristicTextScorer')

# The trained text scorer, off by default for the same reason the others are:
# it needs the optional ML dependencies and downloaded weights. Point
# SOCIAL_TEXT_SCORER at social.ml.RobertaTextScorer to turn it on. The
# weights share CONTENT_MODEL_CACHE; manage.py fetch_models --module social
# pulls them.
SOCIAL_SENTIMENT_MODEL = env(
    'SOCIAL_SENTIMENT_MODEL',
    default='cardiffnlp/twitter-roberta-base-sentiment-latest',
)
SOCIAL_OFFENSIVE_MODEL = env(
    'SOCIAL_OFFENSIVE_MODEL',
    default='cardiffnlp/twitter-roberta-base-offensive',
)
# The sentiment model reads every post three ways and reports a probability
# for each. Below this, its best reading is not a reading: no sentiment is
# recorded and the post says so. 0.70 comes from the coverage table
# manage.py evaluate_text_scorer prints: on TweetEval's test split it leaves
# the model speaking about 65% of posts and right about 81% of those, where
# 0.60 would speak about 81% of them and be right about 77%. There is no
# knee in that curve to find - the trade is roughly four points of accuracy
# for sixteen of coverage at every step - so the line is drawn where the
# stored numbers stop being wrong one time in four.
SOCIAL_TEXT_MIN_CONFIDENCE = env.float('SOCIAL_TEXT_MIN_CONFIDENCE', default=0.70)

# The graph model. social.ml.RgcnGraphScorer is a relational graph network,
# and unlike the two above it ships with no weights: a bot classifier has to
# be trained on accounts somebody labelled, and the public labelled graphs
# are under access agreements with their authors. Mark accounts automated or
# not in the admin and run manage.py train_graph_model, which writes here.
SOCIAL_GRAPH_ARTIFACT = env(
    'SOCIAL_GRAPH_ARTIFACT', default=str(BASE_DIR / 'social' / 'artifacts' / 'rgcn.pt')
)
# How sure the graph model has to be, either way, before a likelihood is
# recorded. Higher than the text floor on purpose: calling an account
# automated is an accusation about a person, and no number is better than a
# wrong one. manage.py train_graph_model prints the table this comes from.
SOCIAL_GRAPH_MIN_CONFIDENCE = env.float('SOCIAL_GRAPH_MIN_CONFIDENCE', default=0.80)
# Floors, not tuning knobs. Below them the heuristic scorer records no score
# and says why: one unanswered mention is 100% unreciprocated, which is
# arithmetic rather than evidence of automation.
SOCIAL_MIN_INTERACTIONS = env.int('SOCIAL_MIN_INTERACTIONS', default=3)
SOCIAL_MIN_SHARED_TARGETS = env.int('SOCIAL_MIN_SHARED_TARGETS', default=3)
# Grouping accounts into clusters. A trained model subclasses
# social.campaigns.BaseCampaignDetector and is named here.
SOCIAL_CAMPAIGN_DETECTOR = env(
    'SOCIAL_CAMPAIGN_DETECTOR', default='social.campaigns.OverlapCampaignDetector'
)
SOCIAL_CAMPAIGN_MIN_SIZE = env.int('SOCIAL_CAMPAIGN_MIN_SIZE', default=3)
# Jaccard overlap two accounts' target lists need before they are linked.
# Lowering it does not find more campaigns, it finds looser ones.
SOCIAL_CAMPAIGN_MIN_OVERLAP = env.float('SOCIAL_CAMPAIGN_MIN_OVERLAP', default=0.5)

# Module 4's default runs arithmetic over the file, not a model.
# media.ml.ViTForensics adds the trained generated-image detector on top of
# the same inspection; naming it here is the whole switch.
MEDIA_FORENSICS_ANALYZER = env(
    'MEDIA_FORENSICS_ANALYZER', default='media.forensics.HeuristicForensics'
)
# The detector's weights, pinned to the revision that was measured. The
# repository replaced its weights once already, and a floor chosen against
# one upload says nothing about the next.
MEDIA_DETECTOR_MODEL = env(
    'MEDIA_DETECTOR_MODEL', default='buildborderless/CommunityForensics-DeepfakeDet-ViT'
)
MEDIA_DETECTOR_REVISION = env(
    'MEDIA_DETECTOR_REVISION', default='ac6ee457bea904a373065754107451793b56db00'
)
# Chosen from the JPEG-recompressed pass of manage.py evaluate_detector: at
# 0.95 one real photograph in 1,567 was confidently called generated, against
# three at 0.90. See media/artifacts/vit_detector.metrics.json.
MEDIA_DETECTOR_MIN_CONFIDENCE = env.float('MEDIA_DETECTOR_MIN_CONFIDENCE', default=0.95)
# The quality the image is re-encoded at for error level analysis. 90 is the
# conventional choice: high enough that an unedited photograph barely moves,
# low enough that a region with its own compression history stands out.
MEDIA_ELA_QUALITY = env.int('MEDIA_ELA_QUALITY', default=90)
# Large images are downscaled before the error-level pass. The signal is the
# *distribution* of error across the frame, which survives downscaling, and a
# 48-megapixel original otherwise makes an analyst wait for nothing.
MEDIA_ELA_MAX_EDGE = env.int('MEDIA_ELA_MAX_EDGE', default=1400)

# Module 5 resolves place names against a table compiled into the source
# (reportgis.gazetteer), not a geocoding service: no key, no rate limit, and no
# sending every article's place names to a third party. A subclass of
# reportgis.geocode.BaseGeocoder named here replaces it without changing a
# caller.
REPORTGIS_GEOCODER = env('REPORTGIS_GEOCODER', default='reportgis.geocode.GazetteerGeocoder')

# Reports are assembled from counted figures by reportgis.reporting, one
# template sentence per number - nothing writes prose about what the events
# mean. A BaseReportBuilder subclass named here replaces the writer.
REPORTGIS_REPORT_BUILDER = env(
    'REPORTGIS_REPORT_BUILDER',
    default='reportgis.reporting.NarrativeReportBuilder',
)

# Corroboration: rating a story by whether independent outlets carried it too.
# CompositeSearch looks through what we have already ingested *and* GDELT's
# free, key-less index of global news. NullSearch disables it, and reports
# itself unavailable rather than empty so it cannot be read as evidence that
# nothing corroborates a story.
CORROBORATION_SEARCH = env(
    'CORROBORATION_SEARCH', default='content.corroborate.CompositeSearch'
)
# How far either side of publication a matching story still counts. The 2005
# Mumbai floods must not corroborate a report of flooding this week.
CORROBORATION_WINDOW_DAYS = env.int('CORROBORATION_WINDOW_DAYS', default=7)
# Search backends AND their terms, so a long query returns nothing at all.
# Three, not four: a fourth term does not refine the search, it empties it,
# and precision is the similarity filter's job. Recall is what matters here -
# a candidate that is never returned can never be examined.
CORROBORATION_MAX_QUERY_TERMS = env.int('CORROBORATION_MAX_QUERY_TERMS', default=3)
CORROBORATION_MIN_SIMILARITY = env.float('CORROBORATION_MIN_SIMILARITY', default=0.30)
# Similarity is measured against this many of the article's strongest terms.
# Most candidates are known only by their headline, which cannot carry a
# dozen terms; dividing by all of them discards real matches.
CORROBORATION_SIMILARITY_TERMS = env.int('CORROBORATION_SIMILARITY_TERMS', default=5)
CORROBORATION_MAX_RECORDS = env.int('CORROBORATION_MAX_RECORDS', default=30)
CORROBORATION_SEARCH_TIMEOUT = env.float('CORROBORATION_SEARCH_TIMEOUT', default=20.0)
# GDELT rate-limits hard and a 429 is transient, so retry before giving up.
CORROBORATION_SEARCH_ATTEMPTS = env.int('CORROBORATION_SEARCH_ATTEMPTS', default=4)
CORROBORATION_RETRY_DELAY = env.float('CORROBORATION_RETRY_DELAY', default=3.0)
# Live feeds of other tracked outlets are re-read at most this often, so
# checking five articles in a row does not re-request the same feeds.
CORROBORATION_FEED_CACHE_TTL = env.float('CORROBORATION_FEED_CACHE_TTL', default=900.0)
CORROBORATION_MAX_FEEDS = env.int('CORROBORATION_MAX_FEEDS', default=12)
# Outlets from content.reference_outlets consulted per check, whether or not
# they are registered as Sources. Without these, a fresh install can only
# cross-reference against the one or two feeds someone has added, and reports
# every story as uncorroborated - truthfully, but having looked nowhere.
CORROBORATION_MAX_REFERENCE_FEEDS = env.int('CORROBORATION_MAX_REFERENCE_FEEDS', default=10)
# Other outlets' articles fetched in full per check, so their actual text can
# be matched and the article's figures checked against theirs. Each is a real
# request to someone else's server, spaced by the crawl delay - this is the
# main thing an analyst waits for. 0 disables deep reading entirely, which
# also disables claim checking, since a headline cannot corroborate a figure.
CORROBORATION_DEEP_READS = env.int('CORROBORATION_DEEP_READS', default=5)
CORROBORATION_PAGE_CACHE_TTL = env.float('CORROBORATION_PAGE_CACHE_TTL', default=1800.0)
# Candidates below this are not worth fetching. Lower than MIN_SIMILARITY on
# purpose: most candidates arrive as a headline, and the strict threshold is
# applied after the page has been read.
CORROBORATION_PREFILTER_SIMILARITY = env.float('CORROBORATION_PREFILTER_SIMILARITY', default=0.15)

# Sources scoring below this are not ingested from at all.
CONTENT_MIN_CREDIBILITY = env.float('CONTENT_MIN_CREDIBILITY', default=25.0)

# Published fact-checks about the same subject, looked up alongside the
# corroboration check. The default is the null client: this is the one part of
# the platform that needs somebody's API key, and a fresh clone must not fail
# a check because a key it was never given is missing. Point this at
# GoogleFactCheckClient once CONTENT_FACT_CHECK_API_KEY is set.
#
# A fact-check found here never moves the 0-10 rating, in either direction.
# The match is made on the article's search terms, and a number that shifts on
# a keyword overlap would be a guess with a decimal point on it.
CONTENT_FACT_CHECK_CLIENT = env(
    'CONTENT_FACT_CHECK_CLIENT', default='content.factcheck.NullFactCheckClient'
)
CONTENT_FACT_CHECK_API_KEY = env('CONTENT_FACT_CHECK_API_KEY', default='')
CONTENT_FACT_CHECK_ENDPOINT = env(
    'CONTENT_FACT_CHECK_ENDPOINT',
    default='https://factchecktools.googleapis.com/v1alpha1/claims:search',
)
CONTENT_FACT_CHECK_TIMEOUT = env.float('CONTENT_FACT_CHECK_TIMEOUT', default=12.0)
CONTENT_FACT_CHECK_MAX_RESULTS = env.int('CONTENT_FACT_CHECK_MAX_RESULTS', default=10)
CONTENT_FACT_CHECK_LANGUAGE = env('CONTENT_FACT_CHECK_LANGUAGE', default='en')
# Share of the article's search terms a reviewed claim must carry before the
# match is called close. Lowering it does not find more fact-checks about this
# story; it relabels fact-checks about other stories as being about this one.
CONTENT_FACT_CHECK_MIN_MATCH = env.float('CONTENT_FACT_CHECK_MIN_MATCH', default=0.6)


# Where the analyst console is served from. Django hosts the API only, so the
# API root points visitors here rather than 404ing on /.
FRONTEND_URL = env('FRONTEND_URL', default='http://localhost:5173')
