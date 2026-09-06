from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import MediaItemViewSet, TamperingResultViewSet

router = DefaultRouter()
router.register('items', MediaItemViewSet, basename='mediaitem')
router.register('results', TamperingResultViewSet, basename='tamperingresult')

urlpatterns = [path('', include(router.urls))]
