from rest_framework import serializers

from .models import ExtractionRun, GeoEvent, Report


class GeoEventSerializer(serializers.ModelSerializer):
    """Plain JSON. GeoJSON is a separate representation, see views.as_feature.

    This used to be a GeoFeatureModelSerializer from djangorestframework-gis,
    which meant the list endpoint could only ever speak GeoJSON — convenient
    for a Leaflet layer and awkward for everything else, because the fields an
    analyst reads end up nested under "properties". The map now asks for
    GeoJSON explicitly at /events/geojson/ and everything else gets a flat
    object.
    """

    coordinates = serializers.ListField(read_only=True)
    content_title = serializers.CharField(source='content_item.title', read_only=True)

    class Meta:
        model = GeoEvent
        fields = (
            'id', 'content_item', 'content_title', 'latitude', 'longitude',
            'coordinates', 'place_name', 'country', 'region', 'event_type',
            'confidence', 'method', 'matched_text', 'mentions', 'reasons',
            'occurred_at', 'engine_version', 'created_at',
        )
        read_only_fields = ('reasons', 'engine_version', 'mentions', 'matched_text')


class ExtractionRunSerializer(serializers.ModelSerializer):
    content_title = serializers.CharField(source='content_item.title', read_only=True)

    class Meta:
        model = ExtractionRun
        fields = (
            'id', 'content_item', 'content_title', 'events_found', 'notes',
            'engine_version', 'created_at',
        )


class ReportSerializer(serializers.ModelSerializer):
    event_count = serializers.IntegerField(source='geo_events.count', read_only=True)

    class Meta:
        model = Report
        fields = (
            'id', 'title', 'summary', 'status', 'author', 'pdf',
            'generated_at', 'event_count', 'geo_events', 'created_at',
        )
