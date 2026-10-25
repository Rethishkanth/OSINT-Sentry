from django.contrib import admin
from django.utils.html import format_html, format_html_join

from .models import (
    Campaign,
    CampaignRun,
    InteractionEdge,
    SocialAccount,
    SocialPost,
)


def _risk_badge(value, label):
    """Colour a 0-1 risk score: green low, amber middling, red high.

    A missing score gets its own grey badge rather than a dash. Both scorers
    withhold a number when the evidence will not carry one, and an empty cell
    in a column of green badges reads as the quietest row on the page.
    """
    if value is None:
        return format_html(
            '<span style="background:#4a4a4a;color:#fff;padding:2px 8px;'
            'border-radius:10px;font-weight:600">not scored</span>'
        )
    color = '#c62828' if value >= 0.7 else '#b26a00' if value >= 0.4 else '#2e7d32'
    # format_html escapes each argument into a SafeString, so the number has to
    # be formatted before it gets there - a '{:.2f}' spec would fail on a str.
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 8px;'
        'border-radius:10px;font-weight:600">{} {}</span>',
        color, label, f'{value:.2f}',
    )


def _reasons_list(reasons):
    """The scorer's working, as the admin can show it without a template."""
    if not reasons:
        return 'Not scored yet.'
    return format_html_join(
        '', '<p style="margin:0 0 8px"><b>{}</b><br>{}<br>'
        '<small style="opacity:.6">{}</small></p>',
        (
            (r.get('label', ''), r.get('detail', ''), r.get('effect', ''))
            for r in reasons
        ),
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
        'bot_likelihood', 'coordination_score', 'reasons_text',
        'graph_model_version', 'scored_at',
    )

    @admin.display(description='Why these scores')
    def reasons_text(self, obj):
        return _reasons_list(obj.reasons)

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
    readonly_fields = (
        'sentiment', 'toxicity', 'reasons_text', 'text_model_version', 'scored_at',
    )

    @admin.display(description='Why these scores')
    def reasons_text(self, obj):
        return _reasons_list(obj.reasons)

    @admin.display(description='Toxicity', ordering='toxicity')
    def toxicity_badge(self, obj):
        return _risk_badge(obj.toxicity, 'tox')


@admin.register(InteractionEdge)
class InteractionEdgeAdmin(admin.ModelAdmin):
    list_display = ('from_account', 'edge_type', 'to_account', 'weight', 'observed_at')
    list_filter = ('edge_type',)
    search_fields = ('from_account__handle', 'to_account__handle')
    autocomplete_fields = ('from_account', 'to_account')


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ('name', 'size', 'shared_target_count', 'cohesion', 'last_seen_at')
    list_filter = ('detector_version',)
    search_fields = ('name',)
    filter_horizontal = ('members', 'common_targets')
    readonly_fields = (
        'fingerprint', 'size', 'shared_target_count', 'cohesion',
        'reasons_text', 'detector_version', 'last_seen_at',
    )

    @admin.display(description='Why this cluster')
    def reasons_text(self, obj):
        return _reasons_list(obj.reasons)


@admin.register(CampaignRun)
class CampaignRunAdmin(admin.ModelAdmin):
    list_display = (
        '__str__', 'campaigns_found', 'accounts_considered', 'accounts_total',
        'detector_version',
    )
    list_filter = ('detector_version',)
    readonly_fields = (
        'campaigns_found', 'accounts_considered', 'accounts_total',
        'notes_text', 'detector_version',
    )

    @admin.display(description='What the pass declined to report')
    def notes_text(self, obj):
        return _reasons_list(obj.notes)
