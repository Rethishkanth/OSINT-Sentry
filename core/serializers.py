from django.utils import timezone
from rest_framework import serializers

from .models import Source


class SourceSerializer(serializers.ModelSerializer):
    latest_credibility_score = serializers.SerializerMethodField()
    domain_age_days = serializers.SerializerMethodField()

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
            'domain_registered_at',
            'domain_age_days',
            'registrar',
            'registry_status',
            'registry_checked_at',
            'latest_credibility_score',
            'created_at',
            'updated_at',
        )
        # Registry fields are written by the RDAP lookup task, never by a
        # client — letting an API caller set the domain age would hand them
        # control of their own credibility score.
        read_only_fields = (
            'created_at',
            'updated_at',
            'domain_registered_at',
            'registrar',
            'registry_status',
            'registry_checked_at',
        )

    def get_latest_credibility_score(self, obj):
        latest = obj.credibility_scores.first()
        return latest.credibility_score if latest else None

    def get_domain_age_days(self, obj):
        if not obj.domain_registered_at:
            return None
        return (timezone.now() - obj.domain_registered_at).days
