from django.shortcuts import render
from rest_framework import status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .domains import InvalidDomain, normalize_domain, scheme_of
from .models import Source
from .serializers import SourceSerializer


class SourceViewSet(viewsets.ModelViewSet):
    """CRUD for tracked sources. Requires authentication (project-wide default)."""

    queryset = Source.objects.prefetch_related('credibility_scores')
    serializer_class = SourceSerializer
    search_fields = ('domain', 'name', 'publisher')
    ordering_fields = ('domain', 'created_at', 'last_seen')

    def create(self, request, *args, **kwargs):
        """Submit a website for scoring, given a URL or a bare domain.

        Submitting a site that is already tracked re-scores it instead of
        returning a uniqueness error. Someone pasting a link is asking "check
        this", and "you already added that" answers a question they did not
        ask - while leaving the score they wanted just as stale as it was.
        """
        # Imported here rather than at module scope: credibility imports core,
        # so a top-level import would close the cycle.
        from celery import chain

        from credibility.tasks import refresh_domain_registration, score_source

        try:
            domain = normalize_domain(request.data.get('domain', ''))
        except InvalidDomain as exc:
            raise ValidationError({'domain': [str(exc)]}) from exc

        existing = self.get_queryset().filter(domain=domain).first()
        if existing is not None:
            chain(
                refresh_domain_registration.si(existing.pk),
                score_source.si(existing.pk),
            ).delay()
            return Response(
                {
                    **self.get_serializer(existing).data,
                    'created': False,
                    'detail': f'{domain} is already tracked - a rescore is queued.',
                },
                status=status.HTTP_200_OK,
            )

        data = request.data.copy()
        if not data.get('url'):
            # A pasted https:// link is evidence the site serves TLS. Dropping
            # it would leave the scorer recording transport as unknown for a
            # site we were just handed proof about.
            scheme = scheme_of(request.data.get('domain', ''))
            if scheme:
                data['url'] = f'{scheme}://{domain}/'

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        # New sources are scored by the post_save signal, not from here.
        self.perform_create(serializer)
        return Response(
            {
                **serializer.data,
                'created': True,
                'detail': f'Tracking {domain}. Looking up its registration…',
            },
            status=status.HTTP_201_CREATED,
            headers=self.get_success_headers(serializer.data),
        )


@api_view(['GET'])
@permission_classes([AllowAny])
def api_root(request):
    """Index for the bare hostname.

    Django serves the API only, so hitting / in a browser used to 404 with no
    hint that the console lives on a different port. This says where to go.
    """
    from django.conf import settings

    def url(path):
        return request.build_absolute_uri(path)

    endpoints = {
        'health': url('/api/health/'),
        'obtain_token': url('/api/auth/token/'),
        'refresh_token': url('/api/auth/token/refresh/'),
        'overview': url('/api/overview/'),
        'sources': url('/api/sources/'),
        'credibility': url('/api/credibility/'),
        'content': url('/api/content/'),
        'social': url('/api/social/'),
        'media': url('/api/media/'),
        'reportgis': url('/api/reportgis/'),
    }

    return Response({
        'service': 'osint-sentry',
        'detail': 'This is the API backend. The analyst console runs separately.',
        'console': settings.FRONTEND_URL,
        'admin': url('/admin/'),
        'endpoints': endpoints,
    })


@api_view(['GET'])
@permission_classes([AllowAny])
def health(request):
    """Unauthenticated liveness check, also used by the frontend smoke test."""
    return Response({'status': 'ok', 'service': 'osint-sentry'})


@api_view(['GET'])
def overview(request):
    """Counts and module status for the dashboard.

    Every module reports online now. Module 5 used to report otherwise: its
    GeoEvent stored a PostGIS geometry, so a machine without GDAL kept the app
    out of INSTALLED_APPS entirely and the console drew an OFFLINE panel. The
    geometry is two float columns now and the app loads everywhere.
    """
    from django.conf import settings

    from content.models import ContentItem, Corroboration, Entity
    from credibility.models import CredibilityScore
    from media.models import MediaItem, TamperingResult
    from reportgis.models import ExtractionRun, GeoEvent, Report
    from security.models import AuditLogEntry
    from social.models import (
        Campaign,
        InteractionEdge,
        SocialAccount,
        SocialPost,
    )

    modules = {
        'credibility': {
            'online': True,
            'scores': CredibilityScore.objects.count(),
            'sources': Source.objects.count(),
            'model_version': settings.CREDIBILITY_SCORER.rsplit('.', 1)[-1],
        },
        # Ordered for the same reason the social dict below is: the panel
        # renders three sub-metrics and drops the rest. `unchecked` - articles
        # nobody has cross-referenced - sits beside `analyzed` because an
        # unchecked article is not a cleared one, and `entities` is a volume
        # figure that changes nothing an analyst does.
        'content': {
            'online': True,
            'items': ContentItem.objects.count(),
            'analyzed': ContentItem.objects.filter(
                status=ContentItem.Status.ANALYZED
            ).count(),
            'unchecked': (
                ContentItem.objects.count()
                - Corroboration.objects.values('content_item').distinct().count()
            ),
            'entities': Entity.objects.count(),
            'checks': Corroboration.objects.count(),
            'fact_checked': Corroboration.objects.filter(
                fact_check_status=Corroboration.FactCheckStatus.FOUND
            ).count(),
            'model_version': settings.CONTENT_ANALYZER.rsplit('.', 1)[-1],
        },
        # Order matters here: the dashboard panel renders the first three
        # sub-metrics and drops the rest, so the figures that change what an
        # analyst does come first. `unscored` sits directly beside `flagged`
        # because an account the scorer declined to judge is not a cleared
        # account, and a panel showing only the flag count files it as one.
        # `edges` and `posts` fall off the panel and are on the module page.
        'social': {
            'online': True,
            'accounts': SocialAccount.objects.count(),
            'flagged': SocialAccount.objects.filter(bot_likelihood__gte=0.7).count(),
            'unscored': SocialAccount.objects.filter(
                bot_likelihood__isnull=True
            ).count(),
            'campaigns': Campaign.objects.count(),
            'edges': InteractionEdge.objects.count(),
            'posts': SocialPost.objects.count(),
            'model_version': settings.SOCIAL_GRAPH_SCORER.rsplit('.', 1)[-1],
        },
        'media': {
            'online': True,
            'items': MediaItem.objects.count(),
            'results': TamperingResult.objects.count(),
            'manipulated': TamperingResult.objects.filter(
                verdict=TamperingResult.Verdict.MANIPULATED
            ).count(),
            'suspicious': TamperingResult.objects.filter(
                verdict=TamperingResult.Verdict.SUSPICIOUS
            ).count(),
            'model_version': settings.MEDIA_FORENSICS_ANALYZER.rsplit('.', 1)[-1],
        },
        'reportgis': {
            'online': True,
            'events': GeoEvent.objects.count(),
            'located_precisely': GeoEvent.objects.filter(
                confidence=GeoEvent.Confidence.HIGH
            ).count(),
            'articles_read': ExtractionRun.objects.values('content_item').distinct().count(),
            'reports': Report.objects.count(),
            'model_version': settings.REPORTGIS_GEOCODER.rsplit('.', 1)[-1],
        },
    }

    return Response({
        'modules': modules,
        'audit_entries': AuditLogEntry.objects.count(),
    })


# -- public pages ---------------------------------------------------------
#
# Served by Django rather than added to the React app, which has no router
# and is the login-gated console. These two pages are the only part of the
# system a person can read without an account, so they stay plain HTML with
# no build step, no JavaScript and nothing loaded from another domain.

# The date the policy text last changed. Deliberately a constant rather than
# the current date: a policy that claims to have been updated today every
# time it is rendered is telling the reader something false.
PRIVACY_LAST_UPDATED = '25 October 2026'


def landing(request):
    return render(request, 'public/landing.html')


def privacy(request):
    return render(request, 'public/privacy.html',
                  {'last_updated': PRIVACY_LAST_UPDATED})
