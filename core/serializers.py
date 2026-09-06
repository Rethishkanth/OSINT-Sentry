from rest_framework import serializers

from .models import Source


class SourceSerializer(serializers.ModelSerializer):
    latest_credibility_score = serializers.SerializerMethodField()

    class Meta:
        model = Source
        fields = (
            'id',
            'name',
            'domain',
            'url',
            'publisher',
            'country',
            'first_seen',
            'last_seen',
            'is_active',
            'latest_credibility_score',
            'created_at',
            'updated_at',
        )
        read_only_fields = ('created_at', 'updated_at')

    def get_latest_credibility_score(self, obj):
        latest = obj.credibility_scores.first()
        return latest.credibility_score if latest else None
