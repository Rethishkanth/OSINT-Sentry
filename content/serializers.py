from rest_framework import serializers

from .models import (
    ContentItem,
    ContentTopic,
    Corroboration,
    CorroborationMatch,
    Entity,
)


class CorroborationMatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = CorroborationMatch
        fields = (
            'id', 'domain', 'url', 'title', 'similarity', 'matched_terms',
            'credibility', 'published_at',
        )


class CorroborationSerializer(serializers.ModelSerializer):
    """A credibility check, with the evidence behind it."""

    matches = CorroborationMatchSerializer(many=True, read_only=True)
    verdict_label = serializers.CharField(source='get_verdict_display', read_only=True)
    content_title = serializers.CharField(source='content_item.title', read_only=True)
    content_url = serializers.CharField(source='content_item.url', read_only=True)
    source_domain = serializers.CharField(source='content_item.source.domain', read_only=True)

    class Meta:
        model = Corroboration
        fields = (
            'id', 'content_item', 'content_title', 'content_url', 'source_domain',
            'score', 'verdict', 'verdict_label', 'keywords', 'rationale',
            'reasons', 'searched', 'sources_checked', 'independent_sources',
            'outlets_read', 'claims', 'claims_checked', 'claims_agreed',
            'claims_disputed', 'engine_version', 'matches', 'created_at',
        )


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


class FetchRequestSerializer(serializers.Serializer):
    """Body for POST /api/content/fetch/ — one link, or one source's feed."""

    url = serializers.CharField(required=False, allow_blank=True)
    source = serializers.IntegerField(required=False)

    def validate(self, attrs):
        if not attrs.get('url') and not attrs.get('source'):
            raise serializers.ValidationError(
                'Give either a url to read, or a source whose feed to crawl.'
            )
        return attrs


class IngestRequestSerializer(serializers.Serializer):
    """Body for POST /api/content/ingest/."""

    source = serializers.IntegerField()
    url = serializers.URLField(max_length=1000)
    title = serializers.CharField(required=False, allow_blank=True, default='')
    body = serializers.CharField(required=False, allow_blank=True, default='')
    author = serializers.CharField(required=False, allow_blank=True, default='')
    language = serializers.CharField(required=False, allow_blank=True, default='')
