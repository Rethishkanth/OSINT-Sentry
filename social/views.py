from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from .models import InteractionEdge, SocialAccount, SocialPost
from .serializers import (
    InteractionEdgeSerializer,
    SocialAccountSerializer,
    SocialPostSerializer,
)
from .tasks import refresh_graph_scores, score_post


class SocialAccountViewSet(viewsets.ModelViewSet):
    """Accounts with bot-likelihood and coordination scores."""

    queryset = SocialAccount.objects.all()
    serializer_class = SocialAccountSerializer
    search_fields = ('handle', 'display_name', 'external_id')
    ordering_fields = ('bot_likelihood', 'coordination_score', 'follower_count', 'handle')

    @action(detail=False, methods=['get'])
    def flagged(self, request):
        """GET /api/social/accounts/flagged/ — the accounts worth a look."""
        threshold = float(request.query_params.get('threshold', 0.7))
        queryset = self.filter_queryset(
            self.get_queryset().filter(bot_likelihood__gte=threshold)
        ).order_by('-bot_likelihood')

        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page or queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


class SocialPostViewSet(viewsets.ModelViewSet):
    """Posts with sentiment and toxicity."""

    queryset = SocialPost.objects.select_related('account')
    serializer_class = SocialPostSerializer
    search_fields = ('text', 'account__handle')
    ordering_fields = ('posted_at', 'sentiment', 'toxicity')

    @action(detail=True, methods=['post'])
    def score(self, request, pk=None):
        """POST /api/social/posts/{id}/score/ — rescore one post inline."""
        post = self.get_object()
        score_post.run(post.pk)
        post.refresh_from_db()
        return Response(self.get_serializer(post).data)


class InteractionEdgeViewSet(viewsets.ModelViewSet):
    queryset = InteractionEdge.objects.select_related('from_account', 'to_account')
    serializer_class = InteractionEdgeSerializer
    ordering_fields = ('weight', 'observed_at')


@api_view(['POST'])
def refresh_scores(request):
    """POST /api/social/refresh-scores/ — queue a full graph rescore."""
    async_result = refresh_graph_scores.delay()
    return Response(
        {'task_id': async_result.id, 'status': 'queued'},
        status=status.HTTP_202_ACCEPTED,
    )
