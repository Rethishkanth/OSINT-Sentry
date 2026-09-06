from django.contrib import admin
from django.utils.html import format_html

from .models import InteractionEdge, SocialAccount, SocialPost


def _risk_badge(value, label):
    """Colour a 0-1 risk score: green low, amber middling, red high."""
    if value is None:
        return '—'
    color = '#c62828' if value >= 0.7 else '#b26a00' if value >= 0.4 else '#2e7d32'
    # format_html escapes each argument into a SafeString, so the number has to
    # be formatted before it gets there — a '{:.2f}' spec would fail on a str.
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 8px;'
        'border-radius:10px;font-weight:600">{} {}</span>',
        color, label, f'{value:.2f}',
    )


@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):
    list_display = (
        'handle', 'platform', 'display_name', 'follower_count',
        'bot_badge', 'coordination_badge', 'scored_at',
    )
    list_filter = ('platform', 'graph_model_version')
    search_fields = ('handle', 'display_name', 'external_id')
    readonly_fields = (
        'bot_likelihood', 'coordination_score', 'graph_model_version', 'scored_at',
    )

    @admin.display(description='Bot likelihood', ordering='bot_likelihood')
    def bot_badge(self, obj):
        return _risk_badge(obj.bot_likelihood, 'bot')

    @admin.display(description='Coordination', ordering='coordination_score')
    def coordination_badge(self, obj):
        return _risk_badge(obj.coordination_score, 'coord')


@admin.register(SocialPost)
class SocialPostAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'account', 'posted_at', 'sentiment', 'toxicity_badge')
    list_filter = ('account__platform', 'text_model_version')
    search_fields = ('text', 'account__handle')
    autocomplete_fields = ('account',)
    date_hierarchy = 'posted_at'
    readonly_fields = ('sentiment', 'toxicity', 'text_model_version', 'scored_at')

    @admin.display(description='Toxicity', ordering='toxicity')
    def toxicity_badge(self, obj):
        return _risk_badge(obj.toxicity, 'tox')


@admin.register(InteractionEdge)
class InteractionEdgeAdmin(admin.ModelAdmin):
    list_display = ('from_account', 'edge_type', 'to_account', 'weight', 'observed_at')
    list_filter = ('edge_type',)
    search_fields = ('from_account__handle', 'to_account__handle')
    autocomplete_fields = ('from_account', 'to_account')
