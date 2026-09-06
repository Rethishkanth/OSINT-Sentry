from celery import chain
from rest_framework import status, viewsets
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import CredibilityScore
from .serializers import CredibilityScoreSerializer, ScoreRequestSerializer
from .tasks import refresh_domain_registration, score_source


class CredibilityScoreViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/credibility/ and /api/credibility/{id}/.

    Scores are produced by the pipeline and never edited by hand, so this is
    read-only; use the score endpoint below to request a new one.
    """

    queryset = CredibilityScore.objects.select_related('source')
    serializer_class = CredibilityScoreSerializer
    search_fields = ('source__domain', 'provenance_hash')
    ordering_fields = ('credibility_score', 'created_at')


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
