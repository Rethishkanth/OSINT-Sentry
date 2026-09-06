"""Development settings."""

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, env  # noqa: F401

DEBUG = True

ALLOWED_HOSTS = ['localhost', '127.0.0.1']

# The Vite dev server runs on :5173 and proxies /api to Django, so requests
# arrive same-origin. These stay listed for direct browser calls to :8000.
CSRF_TRUSTED_ORIGINS = ['http://localhost:5173', 'http://127.0.0.1:5173']

# GeoDjango can usually find GDAL/GEOS on PATH. On Windows with an OSGeo4W
# install it generally cannot, so point at the DLLs explicitly when the .env
# supplies a path.
GDAL_LIBRARY_PATH = env('GDAL_LIBRARY_PATH', default=None)
GEOS_LIBRARY_PATH = env('GEOS_LIBRARY_PATH', default=None)

if GDAL_LIBRARY_PATH is None:
    del GDAL_LIBRARY_PATH
if GEOS_LIBRARY_PATH is None:
    del GEOS_LIBRARY_PATH
