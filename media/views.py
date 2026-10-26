from django.conf import settings
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
        """POST /api/media/items/{id}/analyze/ - re-run the engine now."""
        item = self.get_object()
        analyze_media_item.run(item.pk)
        item.refresh_from_db()
        return Response(self.get_serializer(item).data)

    @action(detail=False, methods=['post'], url_path='analyze-pending')
    def analyze_pending(self, request):
        """POST /api/media/items/analyze-pending/ - sweep anything unseen."""
        async_result = analyze_unanalyzed.delay()
        return Response(
            {'task_id': async_result.id, 'status': 'queued'},
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=False, methods=['get'])
    def engine(self, request):
        """GET /api/media/items/engine/ - what the console is actually running.

        Read from the configured analyzer rather than written down, so the
        panel cannot claim a detector the settings do not name, or deny one
        they do.
        """
        analyzer = get_analyzer()
        trained = bool(getattr(analyzer, 'is_trained', False))
        capabilities = [
            'JPEG quantization table profiling',
            'EXIF thumbnail vs image comparison',
            'Editor and generator metadata tags',
            'SHA-256 fingerprinting',
        ]
        not_available = ['Video and audio forensics']
        detector = None
        if trained:
            capabilities.insert(0, (
                'Generated-image detection (ViT, Community Forensics) - reads '
                'the centre of the frame, abstains below '
                f'{analyzer.min_confidence:.2f} confidence, and can raise a '
                'verdict to suspicious but never lower one'
            ))
            not_available.insert(0, (
                'Locating a pasted or generated region inside a real '
                'photograph (the detector reads the frame as a whole)'
            ))
            detector = {
                'model': settings.MEDIA_DETECTOR_MODEL,
                'revision': settings.MEDIA_DETECTOR_REVISION,
                'min_confidence': analyzer.min_confidence,
            }
        else:
            not_available.insert(0, (
                'Generated-image classification (set MEDIA_FORENSICS_ANALYZER='
                'media.ml.ViTForensics to enable the trained detector)'
            ))

        return Response({
            'engine': analyzer.engine_version,
            'class': f'{type(analyzer).__module__}.{type(analyzer).__name__}',
            'image_analysis': PILLOW_AVAILABLE,
            'deepfake_model': trained,
            'detector': detector,
            'capabilities': capabilities,
            'reported_not_scored': [
                'Error level analysis (JPEG) - measured, shown, and kept out '
                'of the verdict: on test composites it flagged untouched '
                'images about as often as edited ones.',
            ],
            'not_available': not_available,
        })


class TamperingResultViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TamperingResult.objects.select_related('media_item')
    serializer_class = TamperingResultSerializer
    ordering_fields = ('created_at', 'deepfake_score', 'ela_score')
