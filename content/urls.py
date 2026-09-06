from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ContentItemViewSet, ingest

router = DefaultRouter()
router.register('', ContentItemViewSet, basename='contentitem')

urlpatterns = [
    path('ingest/', ingest, name='content-ingest'),
    path('', include(router.urls)),
]
