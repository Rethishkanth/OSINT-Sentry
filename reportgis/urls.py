from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import GeoEventViewSet, ReportViewSet

router = DefaultRouter()
router.register('events', GeoEventViewSet, basename='geoevent')
router.register('reports', ReportViewSet, basename='report')

urlpatterns = [path('', include(router.urls))]
