from rest_framework import viewsets

from .models import GeoEvent, Report
from .serializers import GeoEventSerializer, ReportSerializer


class GeoEventViewSet(viewsets.ModelViewSet):
    """Geolocated events, served as GeoJSON."""

    queryset = GeoEvent.objects.select_related('content_item')
    serializer_class = GeoEventSerializer
    search_fields = ('place_name', 'event_type')
    ordering_fields = ('occurred_at', 'created_at')


class ReportViewSet(viewsets.ModelViewSet):
    queryset = Report.objects.prefetch_related('geo_events')
    serializer_class = ReportSerializer
    search_fields = ('title', 'summary')
    ordering_fields = ('created_at', 'generated_at')
