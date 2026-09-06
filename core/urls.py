from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import SourceViewSet, health, overview

router = DefaultRouter()
router.register('sources', SourceViewSet, basename='source')

urlpatterns = [
    path('health/', health, name='health'),
    path('overview/', overview, name='overview'),
    path('', include(router.urls)),
]
