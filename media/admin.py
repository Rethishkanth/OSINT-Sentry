from django.contrib import admin
from django.utils.html import format_html, format_html_join

from .models import MediaItem, TamperingResult

VERDICT_COLORS = {
    TamperingResult.Verdict.AUTHENTIC: '#2e7d32',
    TamperingResult.Verdict.SUSPICIOUS: '#b26a00',
    TamperingResult.Verdict.MANIPULATED: '#c62828',
    TamperingResult.Verdict.INCONCLUSIVE: '#616161',
}


@admin.register(MediaItem)
class MediaItemAdmin(admin.ModelAdmin):
    list_display = (
        '__str__', 'kind', 'content_item', 'dimensions', 'latest_verdict',
        'short_hash', 'captured_at',
    )
    list_filter = ('kind', 'tampering_results__verdict')
    search_fields = ('sha256', 'source_url')
    autocomplete_fields = ('content_item',)
    date_hierarchy = 'created_at'
    # Derived from the file itself by the pipeline; typing a different hash in
    # here would break the one thing tying a result to exact bytes.
    readonly_fields = ('sha256', 'width', 'height')

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('tampering_results')

    @admin.display(description='SHA-256', ordering='sha256')
    def short_hash(self, obj):
        return f'{obj.sha256[:16]}…' if obj.sha256 else '—'

    @admin.display(description='Size')
    def dimensions(self, obj):
        return f'{obj.width}×{obj.height}' if obj.width and obj.height else '—'

    @admin.display(description='Latest verdict')
    def latest_verdict(self, obj):
        # prefetched above, and TamperingResult is ordered newest-first.
        results = list(obj.tampering_results.all())
        return _verdict_badge(results[0].verdict) if results else '—'


@admin.register(TamperingResult)
class TamperingResultAdmin(admin.ModelAdmin):
    list_display = (
        'media_item', 'verdict_badge', 'ela_score', 'deepfake_score',
        'model_version', 'created_at',
    )
    list_filter = ('verdict', 'model_version')
    search_fields = ('media_item__sha256',)
    autocomplete_fields = ('media_item',)
    date_hierarchy = 'created_at'
    # The whole record is the engine's account of what it saw. Editing any of
    # it by hand would leave the stored reasons describing a verdict nobody
    # actually reached.
    readonly_fields = (
        'verdict', 'ela_score', 'deepfake_score', 'model_version',
        'summary', 'why', 'details',
    )

    @admin.display(description='Verdict', ordering='verdict')
    def verdict_badge(self, obj):
        return _verdict_badge(obj.verdict)

    @admin.display(description='Summary')
    def summary(self, obj):
        return (obj.details or {}).get('summary') or '—'

    @admin.display(description='Why this verdict')
    def why(self, obj):
        """The reasons, so the details blob does not have to be read as JSON."""
        reasons = (obj.details or {}).get('reasons') or []
        if not reasons:
            return '—'
        return format_html(
            '<ul style="margin:0;padding-left:18px">{}</ul>',
            format_html_join(
                '',
                '<li><b>{}</b> {}</li>',
                (
                    (f'{r["points"]:+.2f}' if r.get('points') else '·', r.get('label', ''))
                    for r in reasons
                ),
            ),
        )


def _verdict_badge(verdict):
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 8px;'
        'border-radius:10px;font-weight:600">{}</span>',
        VERDICT_COLORS.get(verdict, '#616161'),
        str(verdict).title(),
    )
