"""Development settings."""

from .base import *  # noqa: F401,F403
from .base import BASE_DIR  # noqa: F401

DEBUG = True

ALLOWED_HOSTS = ['localhost', '127.0.0.1']

# The Vite dev server runs on :5173 and proxies /api to Django, so requests
# arrive same-origin. These stay listed for direct browser calls to :8000.
CSRF_TRUSTED_ORIGINS = ['http://localhost:5173', 'http://127.0.0.1:5173']
