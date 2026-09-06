from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ContentItemViewSet, fetch, ingest

router = DefaultRouter()
router.register('', ContentItemViewSet, basename='contentitem')

urlpatterns = [
    path('fetch/', fetch, name='content-fetch'),
    path('ingest/', ingest, name='content-ingest'),
    path('', include(router.urls)),
]
