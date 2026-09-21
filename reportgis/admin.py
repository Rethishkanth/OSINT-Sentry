from django.contrib import admin
from django.utils.html import format_html, format_html_join

from .models import ExtractionRun, GeoEvent, Report

CONFIDENCE_COLORS = {
    GeoEvent.Confidence.HIGH: '#2e7d32',
    GeoEvent.Confidence.MEDIUM: '#b26a00',
    GeoEvent.Confidence.LOW: '#616161',
}


@admin.register(GeoEvent)
class GeoEventAdmin(admin.ModelAdmin):
    # Was a GISModelAdmin, which draws an OpenLayers widget over a geometry
    # column. There is no geometry column any more, and the widget needed
    # GDAL loaded to render it.
    list_display = (
        'place_name', 'coordinates_display', 'confidence_badge', 'method',
        'event_type', 'mentions', 'occurred_at', 'content_item',
    )
    list_filter = ('confidence', 'method', 'country', 'event_type')
    search_fields = ('place_name', 'country', 'region', 'content_item__title')
    autocomplete_fields = ('content_item',)
    date_hierarchy = 'occurred_at'
    # Produced by reportgis.geocode. Editing the working by hand would leave a
    # pin explaining itself with reasoning nothing actually followed.
    readonly_fields = ('reasons_display', 'matched_text', 'mentions', 'engine_version')
    exclude = ('reasons',)

    @admin.display(description='Coordinates')
    def coordinates_display(self, obj):
        return f'{obj.latitude:.4f}, {obj.longitude:.4f}'

    @admin.display(description='Confidence', ordering='confidence')
    def confidence_badge(self, obj):
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:10px;font-weight:600">{}</span>',
            CONFIDENCE_COLORS.get(obj.confidence, '#616161'),
            obj.get_confidence_display(),
        )

    @admin.display(description='Why this location')
    def reasons_display(self, obj):
        reasons = obj.reasons or []
        if not reasons:
            return '—'
        return format_html(
            '<ul style="margin:0;padding-left:18px">{}</ul>',
            format_html_join(
                '', '<li>{}</li>',
                ((r.get('label', ''),) for r in reasons),
            ),
        )


@admin.register(ExtractionRun)
class ExtractionRunAdmin(admin.ModelAdmin):
    list_display = ('content_item', 'events_found', 'refusals', 'engine_version', 'created_at')
    list_filter = ('engine_version',)
    search_fields = ('content_item__title',)
    autocomplete_fields = ('content_item',)
    date_hierarchy = 'created_at'
    readonly_fields = ('events_found', 'notes_display', 'engine_version')
    exclude = ('notes',)

    @admin.display(description='Not placed')
    def refusals(self, obj):
        return len(obj.notes or [])

    @admin.display(description='Names found and not placed')
    def notes_display(self, obj):
        notes = obj.notes or []
        if not notes:
            return 'None — every name found was resolved.'
        return format_html(
            '<ul style="margin:0;padding-left:18px">{}</ul>',
            format_html_join('', '<li>{}</li>', ((n,) for n in notes)),
        )


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'status', 'author', 'event_count', 'gap_count',
        'generated_at', 'created_at',
    )
    list_filter = ('status', 'builder_version')
    search_fields = ('title', 'summary', 'document')
    filter_horizontal = ('geo_events',)
    date_hierarchy = 'created_at'
    # Written by reportgis.reporting from counted figures. Editing the prose
    # in the admin would leave a report whose sentences no longer match the
    # findings printed beneath them — the regenerate action exists for this.
    readonly_fields = (
        'summary', 'document_display', 'gaps_display', 'findings',
        'scope', 'builder_version', 'generated_at',
    )
    exclude = ('document', 'gaps')

    @admin.display(description='Events')
    def event_count(self, obj):
        return obj.geo_events.count()

    @admin.display(description='Caveats')
    def gap_count(self, obj):
        return len(obj.gaps or [])

    @admin.display(description='What this report does not show')
    def gaps_display(self, obj):
        gaps = obj.gaps or []
        if not gaps:
            return 'Not generated yet.'
        return format_html(
            '<ul style="margin:0;padding-left:18px;max-width:60em">{}</ul>',
            format_html_join('', '<li style="margin-bottom:6px">{}</li>',
                             ((gap,) for gap in gaps)),
        )

    @admin.display(description='Document')
    def document_display(self, obj):
        if not obj.document:
            return 'Not generated yet.'
        return format_html(
            '<pre style="white-space:pre-wrap;max-width:60em;'
            'max-height:32em;overflow:auto">{}</pre>',
            obj.document,
        )
