from rest_framework import serializers
from rest_framework_gis.serializers import GeoFeatureModelSerializer

from .models import GeoEvent, Report


class GeoEventSerializer(GeoFeatureModelSerializer):
    """GeoJSON Feature, so a Leaflet layer can consume it directly."""

    class Meta:
        model = GeoEvent
        geo_field = 'location'
        fields = (
            'id', 'place_name', 'event_type', 'confidence',
            'occurred_at', 'content_item', 'created_at',
        )


class ReportSerializer(serializers.ModelSerializer):
    event_count = serializers.IntegerField(source='geo_events.count', read_only=True)

    class Meta:
        model = Report
        fields = (
            'id', 'title', 'summary', 'status', 'author', 'pdf',
            'generated_at', 'event_count', 'geo_events', 'created_at',
        )
