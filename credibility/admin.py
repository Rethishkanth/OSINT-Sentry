from django.contrib import admin
from django.utils.html import format_html

from .models import CredibilityScore, TrustTier

TIER_COLORS = {
    TrustTier.VERIFIED: '#0b6b3a',
    TrustTier.HIGH: '#2e7d32',
    TrustTier.MEDIUM: '#b26a00',
    TrustTier.LOW: '#c62828',
    TrustTier.UNKNOWN: '#616161',
}


@admin.register(CredibilityScore)
class CredibilityScoreAdmin(admin.ModelAdmin):
    list_display = ('source', 'credibility_score', 'tier_badge', 'model_version', 'created_at')
    list_filter = ('trust_tier', 'model_version')
    search_fields = ('source__domain', 'provenance_hash')
    autocomplete_fields = ('source',)
    date_hierarchy = 'created_at'
    readonly_fields = ('provenance_hash', 'rationale')

    @admin.display(description='Trust tier', ordering='trust_tier')
    def tier_badge(self, obj):
        """Colour-code the tier so a long list can be scanned at a glance."""
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:10px;font-weight:600">{}</span>',
            TIER_COLORS.get(obj.trust_tier, '#616161'),
            obj.get_trust_tier_display(),
        )
