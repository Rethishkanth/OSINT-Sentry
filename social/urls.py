from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    CampaignRunViewSet,
    CampaignViewSet,
    InteractionEdgeViewSet,
    SocialAccountViewSet,
    SocialPostViewSet,
    refresh_scores,
)

router = DefaultRouter()
router.register('accounts', SocialAccountViewSet, basename='socialaccount')
router.register('posts', SocialPostViewSet, basename='socialpost')
router.register('edges', InteractionEdgeViewSet, basename='interactionedge')
router.register('campaigns', CampaignViewSet, basename='campaign')
router.register('campaign-runs', CampaignRunViewSet, basename='campaignrun')

urlpatterns = [
    path('refresh-scores/', refresh_scores, name='social-refresh-scores'),
    path('', include(router.urls)),
]
