from rest_framework import serializers

from .models import MediaItem, TamperingResult


class TamperingResultSerializer(serializers.ModelSerializer):
    """One assessment, including the reasons behind the verdict.

    `summary` and `reasons` are lifted out of the details blob so the console
    can render the explanation without knowing the blob's shape.
    """

    summary = serializers.SerializerMethodField()
    reasons = serializers.SerializerMethodField()

    class Meta:
        model = TamperingResult
        fields = (
            'id', 'media_item', 'ela_score', 'deepfake_score', 'verdict',
            'model_version', 'summary', 'reasons', 'details', 'created_at',
        )

    def get_summary(self, obj):
        return (obj.details or {}).get('summary', '')

    def get_reasons(self, obj):
        return (obj.details or {}).get('reasons', [])


class MediaItemSerializer(serializers.ModelSerializer):
    tampering_results = TamperingResultSerializer(many=True, read_only=True)
    latest_result = serializers.SerializerMethodField()
    latest_verdict = serializers.SerializerMethodField()

    class Meta:
        model = MediaItem
        fields = (
            'id', 'kind', 'file', 'source_url', 'sha256', 'width', 'height',
            'captured_at', 'content_item', 'tampering_results',
            'latest_result', 'latest_verdict', 'created_at',
        )
        # Written by the pipeline from the file itself, never by the caller.
        read_only_fields = ('sha256', 'width', 'height')

    def _latest(self, obj):
        # TamperingResult is ordered newest-first, and the viewset prefetches
        # the whole set, so this must not re-query.
        results = obj.tampering_results.all()
        return results[0] if results else None

    def get_latest_result(self, obj):
        latest = self._latest(obj)
        return TamperingResultSerializer(latest).data if latest else None

    def get_latest_verdict(self, obj):
        latest = self._latest(obj)
        return latest.verdict if latest else None
