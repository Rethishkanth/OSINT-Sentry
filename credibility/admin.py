from django.contrib import admin
from django.utils.html import format_html

from .models import CredibilityScore, SiteInspection, TrustTier

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


@admin.register(SiteInspection)
class SiteInspectionAdmin(admin.ModelAdmin):
    list_display = (
        'source', 'rating_out_of_ten', 'rating', 'ad_networks', 'trackers',
        'uses_https', 'engine_version', 'created_at', 'cleared_at',
    )
    list_filter = ('rating', 'uses_https', 'engine_version', 'cleared_at')
    search_fields = ('source__domain', 'summary')
    autocomplete_fields = ('source',)
    date_hierarchy = 'created_at'
    # Observed evidence. Editing it by hand would make the stored reasons a
    # lie about how the rating was reached.
    readonly_fields = (
        'score', 'rating', 'summary', 'reasons', 'findings', 'checked_url',
        'ad_networks', 'trackers', 'uses_https', 'engine_version',
    )

    @admin.display(description='Rating', ordering='score')
    def rating_out_of_ten(self, obj):
        return f'{obj.score:.1f}/10' if obj.score is not None else '-'
