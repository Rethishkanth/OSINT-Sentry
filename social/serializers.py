from rest_framework import serializers

from .models import InteractionEdge, SocialAccount, SocialPost


class SocialAccountSerializer(serializers.ModelSerializer):
    """Account with its coordinated-behaviour scores."""

    post_count = serializers.IntegerField(source='posts.count', read_only=True)

    class Meta:
        model = SocialAccount
        fields = (
            'id',
            'handle',
            'platform',
            'display_name',
            'external_id',
            'follower_count',
            'account_created_at',
            'bot_likelihood',
            'coordination_score',
            'graph_model_version',
            'scored_at',
            'post_count',
        )
        read_only_fields = (
            'bot_likelihood', 'coordination_score', 'graph_model_version', 'scored_at',
        )


class SocialPostSerializer(serializers.ModelSerializer):
    handle = serializers.CharField(source='account.handle', read_only=True)
    platform = serializers.CharField(source='account.platform', read_only=True)

    class Meta:
        model = SocialPost
        fields = (
            'id',
            'account',
            'handle',
            'platform',
            'url',
            'text',
            'posted_at',
            'reshare_count',
            'sentiment',
            'toxicity',
            'text_model_version',
            'scored_at',
        )
        read_only_fields = ('sentiment', 'toxicity', 'text_model_version', 'scored_at')


class InteractionEdgeSerializer(serializers.ModelSerializer):
    from_handle = serializers.CharField(source='from_account.handle', read_only=True)
    to_handle = serializers.CharField(source='to_account.handle', read_only=True)

    class Meta:
        model = InteractionEdge
        fields = (
            'id', 'from_account', 'from_handle', 'to_account', 'to_handle',
            'edge_type', 'weight', 'observed_at',
        )
