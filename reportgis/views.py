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

from django.db import transaction
from django.db.models import Count
from django.http import HttpResponse
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from content.models import ContentItem

from . import gazetteer
from .geocode import get_geocoder, haversine_km
from .models import ExtractionRun, GeoEvent, Report
from .reporting import (
    EmptySelection,
    ScopeError,
    get_report_builder,
    plural,
    select_events,
)
from .serializers import (
    ExtractionRunSerializer,
    GeoEventSerializer,
    ReportListSerializer,
    ReportSerializer,
)
from .tasks import locate_content_item

# Above this a "nearby" query is asking for the whole map, and the bounding
# box stops being a useful prefilter.
MAX_RADIUS_KM = 20_000


def as_feature(event: GeoEvent) -> dict:
    """One GeoJSON Feature. Longitude first - that is the spec, not a choice."""
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
            # A malformed filter must not quietly become "no filter" - that
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
    def summary(self, request):
        """The module's headline counts, over everything rather than one page.

        The console used to derive these from the rows it had on screen, which
        is the first page of a paginated list - so twenty-six placed locations
        displayed as twenty-five, and the number got quietly worse the more
        there was to count. A headline figure has to be counted in the
        database, not in the viewport.
        """
        bands = {
            row['confidence']: row['n']
            for row in GeoEvent.objects.values('confidence').annotate(n=Count('id'))
        }

        # Only the newest run per article counts towards the refusals. The log
        # keeps every pass, so summing all of them would inflate the figure
        # every time an article is re-read.
        newest: dict[int, list] = {}
        for item_id, notes in (
            ExtractionRun.objects
            .order_by('content_item_id', '-created_at')
            .values_list('content_item_id', 'notes')
        ):
            newest.setdefault(item_id, notes or [])

        return Response({
            'events': GeoEvent.objects.count(),
            'by_confidence': {
                band: bands.get(band, 0) for band in GeoEvent.Confidence.values
            },
            'countries': (
                GeoEvent.objects.exclude(country='').values('country').distinct().count()
            ),
            'articles_read': len(newest),
            'not_placed': sum(len(notes) for notes in newest.values()),
            'reports': Report.objects.count(),
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
                'Any place the compiled table does not carry - it holds major '
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
    """Reports, and the two ways of making one.

    An analyst either hands over a list of events they have chosen, or hands
    over a scope - a country, a confidence floor, a date window, a radius -
    and lets the scope choose. The scope is stored either way, so the question
    the report answered is still on the record next month when the answer has
    changed.
    """

    # order_by is explicit because Meta.ordering is dropped on a queryset
    # that groups - the annotate() below makes this one of those, and without
    # it the paginator hands out pages in whatever order the DB felt like.
    queryset = Report.objects.prefetch_related('geo_events').annotate(
        _event_count=Count('geo_events')
    ).order_by('-created_at')
    serializer_class = ReportSerializer
    search_fields = ('title', 'summary')
    ordering_fields = ('created_at', 'generated_at')

    def get_serializer_class(self):
        if self.action == 'list':
            return ReportListSerializer
        return ReportSerializer

    # --------------------------------------------------------------- helpers

    def _resolve(self, data):
        """The events this request is about, described, with the stored scope.

        An explicit list of event ids wins over a scope, and is recorded as a
        scope of its own so that a regeneration reproduces the same selection
        rather than silently widening to everything.
        """
        ids = data.get('events')
        if ids:
            if not isinstance(ids, (list, tuple)):
                raise ScopeError('events must be a list of event ids.')
            events = list(
                GeoEvent.objects
                .select_related('content_item', 'content_item__source')
                .filter(pk__in=ids)
            )
            missing = set(ids) - {event.pk for event in events}
            if missing:
                raise ScopeError(
                    f'No such event(s): {", ".join(str(pk) for pk in sorted(missing))}.'
                )
            return (
                events,
                [f'{plural(len(events), "event")} chosen by hand'],
                {'events': list(ids)},
            )

        scope = data.get('scope') or {}
        if not isinstance(scope, dict):
            raise ScopeError('scope must be an object.')
        events, described = select_events(scope)
        return events, described, scope

    @staticmethod
    def _author(request):
        user = getattr(request, 'user', None)
        return user if getattr(user, 'is_authenticated', False) else None

    def _write(self, built, scope, author, report=None):
        with transaction.atomic():
            if report is None:
                report = Report(author=author)
            report.title = built.title
            report.summary = built.summary
            report.document = built.document
            report.findings = built.findings
            report.gaps = built.gaps
            report.scope = scope
            report.builder_version = built.builder_version
            report.generated_at = timezone.now()
            report.save()
            report.geo_events.set(built.event_ids)
        return report

    # --------------------------------------------------------------- actions

    @action(detail=False, methods=['post'])
    def preview(self, request):
        """How many events a scope picks out, before committing to a report.

        Cheap on purpose: the console calls it while the analyst is still
        changing the filters, and a generate call that turns out to select
        nothing is a worse way to learn that than this one.
        """
        try:
            events, described, scope = self._resolve(request.data)
        except ScopeError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        bands = {
            band: 0 for band in (
                GeoEvent.Confidence.HIGH,
                GeoEvent.Confidence.MEDIUM,
                GeoEvent.Confidence.LOW,
            )
        }
        for event in events:
            bands[event.confidence] = bands.get(event.confidence, 0) + 1

        return Response({
            'events': len(events),
            'by_confidence': bands,
            'countries': len({e.country for e in events if e.country}),
            'articles': len({e.content_item_id for e in events if e.content_item_id}),
            'scope': scope,
            'scope_text': described,
            'detail': (
                'Nothing matches this scope.' if not events
                else f'{plural(len(events), "event")} would be reported.'
            ),
        })

    @action(detail=False, methods=['post'])
    def generate(self, request):
        """Build a report from a selection, and store it."""
        try:
            events, described, scope = self._resolve(request.data)
        except ScopeError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        builder = get_report_builder()
        try:
            built = builder.build(
                events,
                title=str(request.data.get('title') or ''),
                scope_text=described,
            )
        except EmptySelection as exc:
            # 400, not an empty 200. The caller asked for a document and is
            # being told none can honestly be written - that is a refusal, and
            # it should read like one.
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        report = self._write(built, scope, self._author(request))
        return Response(ReportSerializer(report).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def regenerate(self, request, pk=None):
        """Rebuild this report from its stored scope, against today's events.

        The row is reused rather than a second one created, so the thing an
        analyst linked to stays the thing they linked to. What changed is
        visible in generated_at and in the document itself.
        """
        report = self.get_object()
        scope = report.scope or {}
        request_shape = scope if 'events' in scope else {'scope': scope}
        try:
            events, described, scope = self._resolve(request_shape)
        except ScopeError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        builder = get_report_builder()
        try:
            built = builder.build(events, title=report.title, scope_text=described)
        except EmptySelection as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        report = self._write(built, scope, report.author, report=report)
        return Response(ReportSerializer(report).data)

    @action(detail=True, methods=['get'])
    def download(self, request, pk=None):
        """The report as a Markdown file, for handing to someone else."""
        report = self.get_object()
        if not report.document:
            return Response(
                {'detail': 'This report has no document. Regenerate it.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        name = slugify(report.title)[:60] or f'report-{report.pk}'
        response = HttpResponse(
            report.document, content_type='text/markdown; charset=utf-8'
        )
        response['Content-Disposition'] = f'attachment; filename="{name}.md"'
        return response
