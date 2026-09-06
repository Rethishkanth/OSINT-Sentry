from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from .models import ContentItem
from .serializers import (
    ContentItemSerializer,
    FetchRequestSerializer,
    IngestRequestSerializer,
)
from .tasks import analyze_content, fetch_source, fetch_url, ingest_item


class ContentItemViewSet(viewsets.ModelViewSet):
    """Content items with their entities, topics, and summaries."""

    queryset = ContentItem.objects.select_related('source').prefetch_related(
        'entities', 'content_topics__topic'
    )
    serializer_class = ContentItemSerializer
    search_fields = ('title', 'body', 'url', 'source__domain')
    ordering_fields = ('published_at', 'created_at', 'analyzed_at')

    @action(detail=True, methods=['post'])
    def analyze(self, request, pk=None):
        """POST /api/content/{id}/analyze/ — re-run analysis on one item."""
        item = self.get_object()
        async_result = analyze_content.delay(item.pk)
        return Response(
            {'task_id': async_result.id, 'content_item': item.pk, 'status': 'queued'},
            status=status.HTTP_202_ACCEPTED,
        )


def _error(message, code):
    return Response(
        {'error': {'message': message, 'status_code': code}}, status=code
    )


@api_view(['POST'])
def fetch(request):
    """POST /api/content/fetch/ — read a link, or crawl a source's feed.

    Runs inline rather than queueing, because the caller is a person who just
    pasted a link and needs to know whether it worked. It is slow by design:
    robots.txt is consulted and the per-host rate limit is honoured, so a
    response can take a few seconds.
    """
    serializer = FetchRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    if data.get('url'):
        result = fetch_url.run(data['url'])
        if 'error' in result:
            if result.get('blocked'):
                return _error(result['error'], status.HTTP_403_FORBIDDEN)
            if result.get('gate') == 'rejected':
                return _error(result['error'], status.HTTP_409_CONFLICT)
            if result.get('source'):
                # We reached the point of asking the site and it did not
                # cooperate; that is an upstream failure, not a bad request.
                return _error(result['error'], status.HTTP_502_BAD_GATEWAY)
            return _error(result['error'], status.HTTP_400_BAD_REQUEST)

        item = ContentItem.objects.get(pk=result['content_item'])
        return Response(
            {**ContentItemSerializer(item).data, 'detail': result['detail']},
            status=status.HTTP_201_CREATED,
        )

    # force: a person clicked this. The back-off that stops the hourly
    # schedule re-probing a feedless site is not meant to ignore them.
    result = fetch_source.run(data['source'], inline=True, force=True)
    if result.get('error'):
        return _error(result['error'], status.HTTP_404_NOT_FOUND)

    items = ContentItem.objects.filter(pk__in=result.get('item_ids', []))
    return Response(
        {**result, 'items': ContentItemSerializer(items, many=True).data},
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
def ingest(request):
    """POST /api/content/ingest/ — submit a document for gated ingestion.

    Runs inline so the caller learns immediately whether the credibility gate
    accepted the item; a 409 means the source scored too low.
    """
    serializer = IngestRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    item_id = ingest_item.run(
        data['source'],
        data['url'],
        title=data['title'],
        body=data['body'],
        author=data['author'],
        language=data['language'],
    )
    if item_id is None:
        return Response(
            {'error': {
                'message': 'Rejected by the credibility gate, or source not found.',
                'status_code': status.HTTP_409_CONFLICT,
            }},
            status=status.HTTP_409_CONFLICT,
        )

    analyze_content.run(item_id)
    item = ContentItem.objects.get(pk=item_id)
    return Response(
        ContentItemSerializer(item).data,
        status=status.HTTP_201_CREATED,
    )
