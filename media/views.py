from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .forensics import PILLOW_AVAILABLE, get_analyzer
from .models import MediaItem, TamperingResult
from .serializers import MediaItemSerializer, TamperingResultSerializer
from .tasks import analyze_media_item, analyze_unanalyzed


class MediaItemViewSet(viewsets.ModelViewSet):
    """Media under forensic analysis, with its tampering results."""

    queryset = MediaItem.objects.prefetch_related('tampering_results')
    serializer_class = MediaItemSerializer
    search_fields = ('sha256', 'source_url')
    ordering_fields = ('created_at', 'captured_at')

    def perform_create(self, serializer):
        """Analyse an upload as soon as it lands.

        Run inline rather than queued: someone who has just uploaded an image
        is waiting for the answer, and a hash plus an error-level pass over a
        downscaled copy is fast enough to hand back in the same response.
        """
        item = serializer.save()
        analyze_media_item.run(item.pk)
        item.refresh_from_db()

    @action(detail=True, methods=['post'])
    def analyze(self, request, pk=None):
        """POST /api/media/items/{id}/analyze/ — re-run the engine now."""
        item = self.get_object()
        analyze_media_item.run(item.pk)
        item.refresh_from_db()
        return Response(self.get_serializer(item).data)

    @action(detail=False, methods=['post'], url_path='analyze-pending')
    def analyze_pending(self, request):
        """POST /api/media/items/analyze-pending/ — sweep anything unseen."""
        async_result = analyze_unanalyzed.delay()
        return Response(
            {'task_id': async_result.id, 'status': 'queued'},
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=False, methods=['get'])
    def engine(self, request):
        """GET /api/media/items/engine/ — what the console is actually running.

        The module used to announce "detector not installed" as a flat fact.
        It now runs a real engine for part of the job and none at all for the
        rest, so it reports which is which instead.
        """
        analyzer = get_analyzer()
        return Response({
            'engine': analyzer.engine_version,
            'class': f'{type(analyzer).__module__}.{type(analyzer).__name__}',
            'image_analysis': PILLOW_AVAILABLE,
            'deepfake_model': False,
            'capabilities': [
                'Error level analysis (JPEG)',
                'JPEG quantization table profiling',
                'EXIF thumbnail vs image comparison',
                'Editor and generator metadata tags',
                'SHA-256 fingerprinting',
            ],
            'not_available': [
                'Deepfake / synthetic media classification (no model installed)',
                'Video and audio forensics',
            ],
        })


class TamperingResultViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TamperingResult.objects.select_related('media_item')
    serializer_class = TamperingResultSerializer
    ordering_fields = ('created_at', 'deepfake_score', 'ela_score')
