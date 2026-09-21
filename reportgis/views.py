"""Read and write the geolocated picture, without a spatial database.

The one query a GIS module owes you that plain columns do not give for free is
"what happened near here". It is served by `?near=lat,lon&within_km=N`: a
bounding box in SQL to cut the candidates down using the (latitude, longitude)
index, then an exact great-circle distance in Python over what survives. The
box is a cheap over-approximation and the haversine is the real answer, so the
result is the same set PostGIS would return.
"""

from __future__ import annotations

from math import cos, radians

from django.db.models import Count
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from content.models import ContentItem

from . import gazetteer
from .geocode import get_geocoder, haversine_km
from .models import ExtractionRun, GeoEvent, Report
from .serializers import ExtractionRunSerializer, GeoEventSerializer, ReportSerializer
from .tasks import locate_content_item

# Above this a "nearby" query is asking for the whole map, and the bounding
# box stops being a useful prefilter.
MAX_RADIUS_KM = 20_000


def as_feature(event: GeoEvent) -> dict:
    """One GeoJSON Feature. Longitude first — that is the spec, not a choice."""
    return {
        'type': 'Feature',
        'id': event.pk,
        'geometry': {'type': 'Point', 'coordinates': event.coordinates},
        'properties': {
            'place_name': event.place_name,
            'country': event.country,
            'region': event.region,
            'confidence': event.confidence,
            'method': event.method,
            'event_type': event.event_type,
            'mentions': event.mentions,
            'matched_text': event.matched_text,
            'reasons': event.reasons,
            'occurred_at': event.occurred_at.isoformat() if event.occurred_at else None,
            'content_item': event.content_item_id,
        },
    }


class GeoEventViewSet(viewsets.ModelViewSet):
    queryset = GeoEvent.objects.select_related('content_item')
    serializer_class = GeoEventSerializer
    search_fields = ('place_name', 'country', 'region', 'event_type')
    ordering_fields = ('occurred_at', 'created_at', 'confidence', 'mentions')

    def get_queryset(self):
        queryset = super().get_queryset()

        confidence = self.request.query_params.get('confidence')
        if confidence:
            queryset = queryset.filter(confidence=confidence)

        near = self.request.query_params.get('near')
        if near:
            queryset = self._near(queryset, near)

        return queryset

    def _near(self, queryset, near: str):
        try:
            lat_text, lon_text = near.split(',')
            lat, lon = float(lat_text), float(lon_text)
        except ValueError:
            # A malformed filter must not quietly become "no filter" — that
            # would answer a spatial question with the whole table.
            return queryset.none()

        try:
            km = float(self.request.query_params.get('within_km', 50))
        except ValueError:
            km = 50.0
        km = max(0.0, min(km, MAX_RADIUS_KM))

        # Degrees of latitude are ~111.32km everywhere. Degrees of longitude
        # shrink towards the poles, and the cosine goes to zero at them, so the
        # span is clamped to a whole hemisphere rather than dividing by ~0.
        dlat = km / 111.32
        cos_lat = cos(radians(lat))
        dlon = 180.0 if abs(cos_lat) < 1e-6 else min(180.0, km / (111.32 * cos_lat))

        box = queryset.filter(
            latitude__gte=lat - dlat, latitude__lte=lat + dlat,
            longitude__gte=lon - dlon, longitude__lte=lon + dlon,
        )
        inside = [
            event.pk for event in box
            if haversine_km(lat, lon, event.latitude, event.longitude) <= km
        ]
        return queryset.filter(pk__in=inside)

    @action(detail=False, methods=['get'])
    def geojson(self, request):
        """A FeatureCollection, for a map layer to consume directly.

        Unpaginated, and capped instead, because half a map is worse than a
        small one: a paginated layer silently shows the first page's pins as
        if they were all of them.
        """
        events = list(self.filter_queryset(self.get_queryset())[:1000])
        return Response({
            'type': 'FeatureCollection',
            'features': [as_feature(event) for event in events],
        })

    @action(detail=False, methods=['get'])
    def engine(self, request):
        """What this module can and cannot do, read from the engine itself."""
        geocoder = get_geocoder()
        table = gazetteer.size()
        cities, countries = table['cities'], table['countries']
        return Response({
            'engine': geocoder.engine_version,
            'gazetteer': table,
            'capabilities': [
                'Coordinates stated in text (decimal, hemispheres, or DMS)',
                f'{cities} cities and {countries} countries by name',
                'Disambiguation from a written qualifier ("Tripoli, Lebanon")',
                'Disambiguation from a country named elsewhere in the article',
                'Radius search by great-circle distance',
            ],
            'refuses_to_guess': [
                'An ambiguous place name with nothing in the text to settle it '
                'is reported as found-but-not-placed, never resolved to the '
                'largest candidate.',
            ],
            'not_available': [
                'Any place the compiled table does not carry — it holds major '
                'cities, not every populated place, and a miss is silence '
                'rather than a near match.',
                'Street-level geocoding (no address data, no geocoding service)',
                'Spatial joins, polygons, and geometry operations (no PostGIS)',
            ],
        })

    @action(detail=False, methods=['post'])
    def extract(self, request):
        """Run the geocoder over stored content.

        With a content_item, that one article, inline, so the caller gets the
        working back in the response. Without one, a bounded sweep of analysed
        articles this module has never seen.
        """
        item_id = request.data.get('content_item')

        if item_id:
            try:
                item = ContentItem.objects.get(pk=item_id)
            except (ContentItem.DoesNotExist, ValueError, TypeError):
                return Response(
                    {'detail': f'No content item {item_id!r}.'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            run = locate_content_item(item)
            return Response(ExtractionRunSerializer(run).data)

        try:
            limit = int(request.data.get('limit', 25))
        except (TypeError, ValueError):
            limit = 25
        limit = max(1, min(limit, 200))

        pending = list(
            ContentItem.objects
            .filter(status=ContentItem.Status.ANALYZED, geo_extractions__isnull=True)
            .order_by('-published_at', '-created_at')[:limit]
        )
        runs = [locate_content_item(item) for item in pending]
        return Response({
            'processed': len(runs),
            'located': sum(run.events_found for run in runs),
            'not_placed': sum(len(run.notes) for run in runs),
            'detail': (
                'No analysed article is waiting on this module.'
                if not runs else
                f'Geocoded {len(runs)} article(s).'
            ),
        })


class ExtractionRunViewSet(viewsets.ReadOnlyModelViewSet):
    """The log of passes, including the ones that found nothing.

    Read-only: a run is a record of something that happened, and editing it
    would make it a record of something that did not.
    """

    queryset = ExtractionRun.objects.select_related('content_item')
    serializer_class = ExtractionRunSerializer
    search_fields = ('content_item__title',)
    ordering_fields = ('created_at', 'events_found')


class ReportViewSet(viewsets.ModelViewSet):
    queryset = Report.objects.prefetch_related('geo_events').annotate(
        _event_count=Count('geo_events')
    )
    serializer_class = ReportSerializer
    search_fields = ('title', 'summary')
    ordering_fields = ('created_at', 'generated_at')
