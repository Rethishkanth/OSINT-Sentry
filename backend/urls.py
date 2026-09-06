"""URL configuration for the OSINT-Sentry backend.

Django serves the API only; the UI is the Vite/React app in frontend/, which
proxies /api to this server in development.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from core.views import api_root


def api_hello(request):
    return JsonResponse({"message": "Hello from Django"})


urlpatterns = [
    path('', api_root, name='api-root'),
    path('admin/', admin.site.urls),
    path('api/hello/', api_hello),
    path('api/auth/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/', include('core.urls')),
    path('api/credibility/', include('credibility.urls')),
    path('api/content/', include('content.urls')),
    path('api/social/', include('social.urls')),
    path('api/media/', include('media.urls')),
]

# reportgis is only in INSTALLED_APPS when USE_GIS is on, so its routes cannot
# be imported unconditionally.
if settings.USE_GIS:
    urlpatterns.append(path('api/reportgis/', include('reportgis.urls')))

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
