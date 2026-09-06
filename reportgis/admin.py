from django.contrib.gis import admin as gis_admin

from .models import GeoEvent, Report


@gis_admin.register(GeoEvent)
class GeoEventAdmin(gis_admin.GISModelAdmin):
    list_display = ('place_name', 'event_type', 'confidence', 'occurred_at', 'content_item')
    list_filter = ('confidence', 'event_type')
    search_fields = ('place_name', 'content_item__title')
    autocomplete_fields = ('content_item',)
    date_hierarchy = 'occurred_at'


@gis_admin.register(Report)
class ReportAdmin(gis_admin.ModelAdmin):
    list_display = ('title', 'status', 'author', 'generated_at', 'created_at')
    list_filter = ('status',)
    search_fields = ('title', 'summary')
    filter_horizontal = ('geo_events',)
