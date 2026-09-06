from django.contrib import admin

from .models import CredibilityScore


@admin.register(CredibilityScore)
class CredibilityScoreAdmin(admin.ModelAdmin):
    list_display = ('source', 'credibility_score', 'trust_tier', 'model_version', 'created_at')
    list_filter = ('trust_tier', 'model_version')
    search_fields = ('source__domain', 'provenance_hash')
    autocomplete_fields = ('source',)
    date_hierarchy = 'created_at'
