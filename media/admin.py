from django.contrib import admin

from .models import MediaItem, TamperingResult


@admin.register(MediaItem)
class MediaItemAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'kind', 'content_item', 'sha256', 'captured_at')
    list_filter = ('kind',)
    search_fields = ('sha256', 'source_url')
    autocomplete_fields = ('content_item',)


@admin.register(TamperingResult)
class TamperingResultAdmin(admin.ModelAdmin):
    list_display = ('media_item', 'verdict', 'ela_score', 'deepfake_score', 'created_at')
    list_filter = ('verdict', 'model_version')
    search_fields = ('media_item__sha256',)
    autocomplete_fields = ('media_item',)
