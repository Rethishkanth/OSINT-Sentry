from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ContentItemViewSet, CorroborationViewSet, fetch, ingest

router = DefaultRouter()
# Registered before the catch-all: ContentItemViewSet's detail pattern is
# `{pk}/`, which would otherwise swallow `corroborations/` as an id.
router.register('corroborations', CorroborationViewSet, basename='corroboration')
router.register('', ContentItemViewSet, basename='contentitem')

urlpatterns = [
    path('fetch/', fetch, name='content-fetch'),
    path('ingest/', ingest, name='content-ingest'),
    path('', include(router.urls)),
]
