from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from .models import ContentItem
from .serializers import ContentItemSerializer, IngestRequestSerializer
from .tasks import analyze_content, ingest_item


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
