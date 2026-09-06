from rest_framework import viewsets

from .models import MediaItem, TamperingResult
from .serializers import MediaItemSerializer, TamperingResultSerializer


class MediaItemViewSet(viewsets.ModelViewSet):
    """Media under forensic analysis, with its tampering results."""

    queryset = MediaItem.objects.prefetch_related('tampering_results')
    serializer_class = MediaItemSerializer
    search_fields = ('sha256', 'source_url')
    ordering_fields = ('created_at', 'captured_at')


class TamperingResultViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TamperingResult.objects.select_related('media_item')
    serializer_class = TamperingResultSerializer
    ordering_fields = ('created_at', 'deepfake_score', 'ela_score')
