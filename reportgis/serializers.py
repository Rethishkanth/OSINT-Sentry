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
    """The whole report, document included.

    Everything the builder wrote is read-only. An analyst who disagrees with a
    report changes the scope and regenerates it, or writes their own
    alongside; editing the summary in place would leave prose that the
    findings underneath it no longer support, which is worse than either.
    """

    event_count = serializers.IntegerField(source='geo_events.count', read_only=True)
    gap_count = serializers.SerializerMethodField()

    class Meta:
        model = Report
        fields = (
            'id', 'title', 'summary', 'document', 'findings', 'gaps',
            'gap_count', 'scope', 'builder_version', 'status', 'author', 'pdf',
            'generated_at', 'event_count', 'geo_events', 'created_at',
        )
        read_only_fields = (
            'summary', 'document', 'findings', 'gaps', 'scope',
            'builder_version', 'generated_at',
        )

    def get_gap_count(self, obj) -> int:
        return len(obj.gaps or [])


class ReportListSerializer(ReportSerializer):
    """The index. Drops the document, which is the only large field here.

    A list of twenty reports carrying twenty full Markdown documents is a
    slow endpoint that nobody reads the payload of; the console shows titles
    and counts and fetches the document when one is opened.
    """

    class Meta(ReportSerializer.Meta):
        fields = tuple(
            name for name in ReportSerializer.Meta.fields
            if name not in ('document', 'geo_events')
        )
