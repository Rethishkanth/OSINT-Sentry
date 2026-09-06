from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import CredibilityScoreViewSet, trigger_scoring

router = DefaultRouter()
router.register('', CredibilityScoreViewSet, basename='credibilityscore')

urlpatterns = [
    path('score/', trigger_scoring, name='credibility-score'),
    path('', include(router.urls)),
]
