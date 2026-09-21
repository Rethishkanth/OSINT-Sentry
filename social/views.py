from django.db.models import Avg, Count, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from .models import (
    Campaign,
    CampaignRun,
    InteractionEdge,
    SocialAccount,
    SocialPost,
)
from .serializers import (
    CampaignRunSerializer,
    CampaignSerializer,
    InteractionEdgeSerializer,
    SocialAccountSerializer,
    SocialPostSerializer,
)
from .tasks import detect_campaigns, refresh_graph_scores, score_post


class SocialAccountViewSet(viewsets.ModelViewSet):
    """Accounts with bot-likelihood and coordination scores."""

    queryset = SocialAccount.objects.all()
    serializer_class = SocialAccountSerializer
    search_fields = ('handle', 'display_name', 'external_id')
    ordering_fields = ('bot_likelihood', 'coordination_score', 'follower_count', 'handle')

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """GET /api/social/accounts/summary/ — headline counts over everything.

        Counted in the database rather than derived from the rows the console
        has on screen: those lists are paginated, so counting them there would
        report the first twenty-five of everything as if it were everything.

        `unscored` is the figure that matters most here. An account the scorer
        declined to judge is not a safe account, and a console that shows only
        flagged-versus-total quietly files it under safe.
        """
        accounts = SocialAccount.objects.aggregate(
            total=Count('id'),
            scored=Count('id', filter=Q(bot_likelihood__isnull=False)),
            flagged=Count('id', filter=Q(bot_likelihood__gte=0.7)),
            lockstep=Count('id', filter=Q(coordination_score__gte=0.7)),
        )
        posts = SocialPost.objects.aggregate(
            total=Count('id'),
            scored=Count('id', filter=Q(scored_at__isnull=False)),
            no_sentiment=Count(
                'id', filter=Q(scored_at__isnull=False, sentiment__isnull=True)
            ),
            mean_toxicity=Avg('toxicity'),
        )
        return Response({
            'accounts': accounts['total'],
            'accounts_scored': accounts['scored'],
            'accounts_unscored': accounts['total'] - accounts['scored'],
            'flagged': accounts['flagged'],
            'lockstep': accounts['lockstep'],
            'edges': InteractionEdge.objects.count(),
            'posts': posts['total'],
            'posts_scored': posts['scored'],
            'posts_without_sentiment': posts['no_sentiment'],
            'mean_toxicity': (
                round(posts['mean_toxicity'], 4)
                if posts['mean_toxicity'] is not None else None
            ),
        })

    @action(detail=False, methods=['get'])
    def flagged(self, request):
        """GET /api/social/accounts/flagged/ — the accounts worth a look.

        Accounts the scorer declined to judge are absent by construction: a
        null score is not >= any threshold. `summary` reports how many those
        are, so the console can say so rather than letting the gap read as an
        all-clear.
        """
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
    """POST /api/social/refresh-scores/ — rescore the whole graph.

    Run inline rather than queued. The default broker is filesystem:// with no
    worker attached, so a queued rescore would return 202 Accepted to a console
    that then redraws the same numbers it already had — the button would look
    like it worked and would have done nothing. The graph is scored in memory
    in one pass, so an analyst-triggered rescore is a request the API can
    simply answer.
    """
    scored = refresh_graph_scores.run()
    return Response({'scored': scored, 'status': 'done'}, status=status.HTTP_200_OK)


class CampaignViewSet(viewsets.ReadOnlyModelViewSet):
    """Clusters of accounts moving together.

    Read-only: a campaign is the detector's output, and hand-editing one
    would leave a row that claims a detector version it was never produced by.
    """

    queryset = (
        Campaign.objects.prefetch_related('members', 'common_targets')
    )
    serializer_class = CampaignSerializer
    search_fields = ('name',)
    ordering_fields = ('size', 'cohesion', 'last_seen_at')

    @action(detail=False, methods=['post'])
    def detect(self, request):
        """POST /api/social/campaigns/detect/ — run a pass over the graph.

        Inline, for the same reason the rescore is: the default broker has no
        worker, and a queued pass would hand the console a 202 and change
        nothing.
        """
        run = CampaignRun.objects.get(pk=detect_campaigns.run())
        return Response(CampaignRunSerializer(run).data)

    @action(detail=False, methods=['get'])
    def latest_run(self, request):
        """GET /api/social/campaigns/latest_run/ — the last pass, or nothing.

        The console needs this to tell an empty campaign list apart from a
        detector that has never been run, and to mark clusters the newest pass
        did not find again.
        """
        run = CampaignRun.objects.first()
        if run is None:
            return Response({'run': None, 'campaigns': Campaign.objects.count()})
        return Response({
            'run': CampaignRunSerializer(run).data,
            'campaigns': Campaign.objects.count(),
            'stale': Campaign.objects.filter(
                last_seen_at__lt=run.created_at
            ).count(),
        })


class CampaignRunViewSet(viewsets.ReadOnlyModelViewSet):
    """The log of detection passes, including the ones that found nothing."""

    queryset = CampaignRun.objects.all()
    serializer_class = CampaignRunSerializer
