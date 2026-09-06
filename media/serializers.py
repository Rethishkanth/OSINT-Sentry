from rest_framework import serializers

from .models import MediaItem, TamperingResult


class TamperingResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = TamperingResult
        fields = (
            'id', 'ela_score', 'deepfake_score', 'verdict',
            'model_version', 'details', 'created_at',
        )


class MediaItemSerializer(serializers.ModelSerializer):
    tampering_results = TamperingResultSerializer(many=True, read_only=True)
    latest_verdict = serializers.SerializerMethodField()

    class Meta:
        model = MediaItem
        fields = (
            'id', 'kind', 'file', 'source_url', 'sha256', 'width', 'height',
            'captured_at', 'content_item', 'tampering_results',
            'latest_verdict', 'created_at',
        )

    def get_latest_verdict(self, obj):
        latest = obj.tampering_results.first()
        return latest.verdict if latest else None
