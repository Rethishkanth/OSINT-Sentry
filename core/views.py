from rest_framework import viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import Source
from .serializers import SourceSerializer


class SourceViewSet(viewsets.ModelViewSet):
    """CRUD for tracked sources. Requires authentication (project-wide default)."""

    queryset = Source.objects.prefetch_related('credibility_scores')
    serializer_class = SourceSerializer
    search_fields = ('domain', 'name', 'publisher')
    ordering_fields = ('domain', 'created_at', 'last_seen')


@api_view(['GET'])
@permission_classes([AllowAny])
def health(request):
    """Unauthenticated liveness check, also used by the frontend smoke test."""
    return Response({'status': 'ok', 'service': 'osint-sentry'})
