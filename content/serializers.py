from rest_framework import serializers

from .models import ContentItem, ContentTopic, Entity


class EntitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Entity
        fields = ('id', 'text', 'label', 'confidence', 'mention_count')


class ContentTopicSerializer(serializers.ModelSerializer):
    label = serializers.CharField(source='topic.label', read_only=True)
    keywords = serializers.JSONField(source='topic.keywords', read_only=True)

    class Meta:
        model = ContentTopic
        fields = ('id', 'label', 'keywords', 'weight')


class ContentItemSerializer(serializers.ModelSerializer):
    """Content with everything the analysis pipeline attached to it."""

    source_domain = serializers.CharField(source='source.domain', read_only=True)
    entities = EntitySerializer(many=True, read_only=True)
    topics = ContentTopicSerializer(source='content_topics', many=True, read_only=True)

    class Meta:
        model = ContentItem
        fields = (
            'id',
            'source',
            'source_domain',
            'url',
            'title',
            'author',
            'language',
            'published_at',
            'fetched_at',
            'status',
            'summary',
            'sentiment',
            'stance',
            'analyzer_version',
            'analyzed_at',
            'entities',
            'topics',
            'created_at',
        )
        read_only_fields = (
            'status', 'summary', 'sentiment', 'analyzer_version', 'analyzed_at',
        )


class IngestRequestSerializer(serializers.Serializer):
    """Body for POST /api/content/ingest/."""

    source = serializers.IntegerField()
    url = serializers.URLField(max_length=1000)
    title = serializers.CharField(required=False, allow_blank=True, default='')
    body = serializers.CharField(required=False, allow_blank=True, default='')
    author = serializers.CharField(required=False, allow_blank=True, default='')
    language = serializers.CharField(required=False, allow_blank=True, default='')
