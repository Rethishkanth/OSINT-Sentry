from rest_framework import serializers

from core.models import Source

from .models import CredibilityScore, SiteInspection


class SiteInspectionSerializer(serializers.ModelSerializer):
    """A safety inspection, with every deduction that produced the number."""

    source_domain = serializers.CharField(source='source.domain', read_only=True)
    rating_label = serializers.CharField(source='get_rating_display', read_only=True)

    class Meta:
        model = SiteInspection
        fields = (
            'id', 'source', 'source_domain', 'score', 'rating', 'rating_label',
            'summary', 'reasons', 'findings', 'checked_url', 'ad_networks',
            'trackers', 'uses_https', 'engine_version', 'created_at',
        )
        read_only_fields = fields


class CredibilityScoreSerializer(serializers.ModelSerializer):
    source_domain = serializers.CharField(source='source.domain', read_only=True)

    class Meta:
        model = CredibilityScore
        fields = (
            'id',
            'source',
            'source_domain',
            'credibility_score',
            'trust_tier',
            'provenance_hash',
            'model_version',
            'rationale',
            'created_at',
        )
        read_only_fields = fields


class ScoreRequestSerializer(serializers.Serializer):
    """Body for POST /api/credibility/score/."""

    source = serializers.PrimaryKeyRelatedField(queryset=Source.objects.all())
    wait = serializers.BooleanField(
        default=False,
        help_text='Score inline and return the result instead of queueing it.',
    )
