from django.utils import timezone
from rest_framework import serializers

from .domains import InvalidDomain, normalize_domain
from .models import Source


class SourceSerializer(serializers.ModelSerializer):
    latest_credibility_score = serializers.SerializerMethodField()
    domain_age_days = serializers.SerializerMethodField()
    # Accepts a pasted article URL, not just a hostname; validate_domain
    # reduces it. Declared explicitly because the model allows a blank name
    # and the default derived field would then be silently optional in a way
    # that is easy to miss when reading the serializer.
    name = serializers.CharField(required=False, allow_blank=True, max_length=255)

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

    def validate_domain(self, value):
        """Reduce a pasted link to its domain before uniqueness is checked.

        Order matters: normalising here means https://www.bbc.co.uk/news is
        recognised as the bbc.co.uk we already track, instead of being stored
        as a second, unlookupable row.
        """
        try:
            domain = normalize_domain(value)
        except InvalidDomain as exc:
            raise serializers.ValidationError(str(exc)) from exc

        # DRF's own uniqueness check ran against the raw input, so two
        # different-looking URLs for the same site would both pass it and then
        # collide in the database as an unhandled IntegrityError. Re-check on
        # the normalised value.
        clash = Source.objects.filter(domain=domain)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(f'{domain} is already tracked.')
        return domain

    def get_latest_credibility_score(self, obj):
        latest = obj.credibility_scores.first()
        return latest.credibility_score if latest else None

    def get_domain_age_days(self, obj):
        if not obj.domain_registered_at:
            return None
        return (timezone.now() - obj.domain_registered_at).days
