from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    CredibilityScoreViewSet,
    SiteInspectionViewSet,
    trigger_inspection,
    trigger_scoring,
)

router = DefaultRouter()
# Registered before the catch-all, whose detail pattern is `{pk}/` and would
# otherwise swallow `inspections/` as an id.
router.register('inspections', SiteInspectionViewSet, basename='siteinspection')
router.register('', CredibilityScoreViewSet, basename='credibilityscore')

urlpatterns = [
    path('score/', trigger_scoring, name='credibility-score'),
    path('inspect/', trigger_inspection, name='credibility-inspect'),
    path('', include(router.urls)),
]
