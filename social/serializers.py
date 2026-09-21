from rest_framework import serializers

from .models import (
    Campaign,
    CampaignRun,
    InteractionEdge,
    SocialAccount,
    SocialPost,
)


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
            'reasons',
            'graph_model_version',
            'scored_at',
            'post_count',
        )
        read_only_fields = (
            'bot_likelihood', 'coordination_score', 'reasons',
            'graph_model_version', 'scored_at',
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
            'reasons',
            'text_model_version',
            'scored_at',
        )
        read_only_fields = (
            'sentiment', 'toxicity', 'reasons', 'text_model_version', 'scored_at',
        )


class InteractionEdgeSerializer(serializers.ModelSerializer):
    from_handle = serializers.CharField(source='from_account.handle', read_only=True)
    to_handle = serializers.CharField(source='to_account.handle', read_only=True)

    class Meta:
        model = InteractionEdge
        fields = (
            'id', 'from_account', 'from_handle', 'to_account', 'to_handle',
            'edge_type', 'weight', 'observed_at',
        )


class CampaignMemberSerializer(serializers.ModelSerializer):
    """Just enough of an account to show it in a cluster."""

    class Meta:
        model = SocialAccount
        fields = ('id', 'handle', 'platform', 'bot_likelihood', 'coordination_score')


class CampaignSerializer(serializers.ModelSerializer):
    members = CampaignMemberSerializer(many=True, read_only=True)
    common_targets = CampaignMemberSerializer(many=True, read_only=True)

    class Meta:
        model = Campaign
        fields = (
            'id',
            'name',
            'members',
            'common_targets',
            'size',
            'shared_target_count',
            'cohesion',
            'reasons',
            'detector_version',
            'last_seen_at',
            'created_at',
        )


class CampaignRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = CampaignRun
        fields = (
            'id',
            'campaigns_found',
            'accounts_considered',
            'accounts_total',
            'notes',
            'detector_version',
            'created_at',
        )
