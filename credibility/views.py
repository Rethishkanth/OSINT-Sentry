from celery import chain
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from .models import CredibilityScore, SiteInspection
from .serializers import (
    ClearInspectionsSerializer,
    CredibilityScoreSerializer,
    ScoreRequestSerializer,
    SiteInspectionSerializer,
)
from .tasks import inspect_site, refresh_domain_registration, score_source


class CredibilityScoreViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/credibility/ and /api/credibility/{id}/.

    Scores are produced by the pipeline and never edited by hand, so this is
    read-only; use the score endpoint below to request a new one.
    """

    queryset = CredibilityScore.objects.select_related('source')
    serializer_class = CredibilityScoreSerializer
    search_fields = ('source__domain', 'provenance_hash')
    ordering_fields = ('credibility_score', 'created_at')


class SiteInspectionViewSet(viewsets.ReadOnlyModelViewSet):
    """The log of site safety inspections, newest first.

    Cleared rows are hidden by default and returned by `?cleared=1`; nothing
    here deletes them, so a cleared log can always be read back.
    """

    serializer_class = SiteInspectionSerializer
    search_fields = ('source__domain', 'rating')
    ordering_fields = ('created_at', 'score')

    def get_queryset(self):
        rows = SiteInspection.objects.select_related('source')
        wanted = self.request.query_params.get('cleared', '').lower()
        if wanted in ('1', 'true', 'only'):
            return rows.filter(cleared_at__isnull=False)
        if wanted == 'all':
            return rows
        return rows.filter(cleared_at__isnull=True)

    @action(detail=False, methods=['post'])
    def clear(self, request):
        """POST /api/credibility/inspections/clear/ — empty the safety log.

        Marks rows cleared instead of deleting them. Pass `source` to clear
        one site. Credibility assessments live in a different table and are
        not touched: a site whose inspections are cleared still has every
        assessment it ever had, and is still a tracked source.
        """
        serializer = ClearInspectionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        source = serializer.validated_data.get('source')

        rows = SiteInspection.objects.filter(cleared_at__isnull=True)
        if source is not None:
            rows = rows.filter(source=source)
        cleared = rows.update(cleared_at=timezone.now())

        return Response({
            'cleared': cleared,
            'source': source.pk if source else None,
            'detail': (
                f'{cleared} inspection(s) cleared. They are hidden, not '
                'deleted — assessments and tracked sources are unaffected.'
            ),
        })

    @action(detail=False, methods=['post'])
    def restore(self, request):
        """POST /api/credibility/inspections/restore/ — undo a clear."""
        serializer = ClearInspectionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        source = serializer.validated_data.get('source')

        rows = SiteInspection.objects.filter(cleared_at__isnull=False)
        if source is not None:
            rows = rows.filter(source=source)
        restored = rows.update(cleared_at=None)

        return Response({'restored': restored, 'detail': f'{restored} restored.'})


@api_view(['POST'])
def trigger_inspection(request):
    """POST /api/credibility/inspect/ — look inside a site and rate it.

    Runs inline: it fetches the homepage and opens a TLS connection, so a few
    seconds is normal, and the caller is waiting for a rating rather than a
    task id.
    """
    serializer = ScoreRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    source = serializer.validated_data['source']

    inspection_id = inspect_site.run(source.pk)
    inspection = SiteInspection.objects.get(pk=inspection_id)
    return Response(
        SiteInspectionSerializer(inspection).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(['POST'])
def trigger_scoring(request):
    """POST /api/credibility/score/ — request a score for a Source."""
    serializer = ScoreRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    source = serializer.validated_data['source']

    if serializer.validated_data['wait']:
        # Runs in-process. Useful when no worker is up, and for tests.
        # The lookup short-circuits on a fresh cache, so the registry is only
        # actually contacted when this source's data is stale or missing.
        refresh_domain_registration.run(source.pk)
        score_id = score_source.run(source.pk)
        score = CredibilityScore.objects.get(pk=score_id)
        return Response(
            CredibilityScoreSerializer(score).data,
            status=status.HTTP_201_CREATED,
        )

    async_result = chain(
        refresh_domain_registration.si(source.pk),
        score_source.si(source.pk),
    ).delay()
    return Response(
        {'task_id': async_result.id, 'source': source.pk, 'status': 'queued'},
        status=status.HTTP_202_ACCEPTED,
    )
