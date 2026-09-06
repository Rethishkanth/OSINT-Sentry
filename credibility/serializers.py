from rest_framework import serializers

from core.models import Source

from .models import CredibilityScore


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
