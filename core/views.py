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
        ask — while leaving the score they wanted just as stale as it was.
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
                    'detail': f'{domain} is already tracked — a rescore is queued.',
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
    from django.apps import apps as django_apps
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
    }
    if django_apps.is_installed('reportgis'):
        endpoints['reportgis'] = url('/api/reportgis/')

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

    Reports which modules are actually online rather than letting the UI
    assume — reportgis is not even installed when USE_GIS is off.
    """
    from django.apps import apps as django_apps
    from django.conf import settings

    from content.models import ContentItem, Entity
    from credibility.models import CredibilityScore
    from media.models import MediaItem, TamperingResult
    from security.models import AuditLogEntry
    from social.models import InteractionEdge, SocialAccount, SocialPost

    gis_online = django_apps.is_installed('reportgis')

    modules = {
        'credibility': {
            'online': True,
            'scores': CredibilityScore.objects.count(),
            'sources': Source.objects.count(),
            'model_version': settings.CREDIBILITY_SCORER.rsplit('.', 1)[-1],
        },
        'content': {
            'online': True,
            'items': ContentItem.objects.count(),
            'entities': Entity.objects.count(),
            'analyzed': ContentItem.objects.filter(
                status=ContentItem.Status.ANALYZED
            ).count(),
            'model_version': settings.CONTENT_ANALYZER.rsplit('.', 1)[-1],
        },
        'social': {
            'online': True,
            'accounts': SocialAccount.objects.count(),
            'edges': InteractionEdge.objects.count(),
            'posts': SocialPost.objects.count(),
            'flagged': SocialAccount.objects.filter(bot_likelihood__gte=0.7).count(),
            'model_version': settings.SOCIAL_GRAPH_SCORER.rsplit('.', 1)[-1],
        },
        'media': {
            'online': True,
            'items': MediaItem.objects.count(),
            'results': TamperingResult.objects.count(),
            'model_version': None,
        },
        'reportgis': {
            'online': gis_online,
            'reason': None if gis_online else 'GDAL/GEOS not installed (USE_GIS is off)',
            'events': 0,
            'reports': 0,
            'model_version': None,
        },
    }

    if gis_online:
        GeoEvent = django_apps.get_model('reportgis', 'GeoEvent')
        Report = django_apps.get_model('reportgis', 'Report')
        modules['reportgis']['events'] = GeoEvent.objects.count()
        modules['reportgis']['reports'] = Report.objects.count()

    return Response({
        'modules': modules,
        'audit_entries': AuditLogEntry.objects.count(),
        'gis_enabled': gis_online,
    })
