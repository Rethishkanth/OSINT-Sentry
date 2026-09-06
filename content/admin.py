from django.contrib import admin

from .models import ContentItem


@admin.register(ContentItem)
class ContentItemAdmin(admin.ModelAdmin):
    list_display = ('title', 'source', 'status', 'language', 'published_at')
    list_filter = ('status', 'language', 'source')
    search_fields = ('title', 'url', 'author', 'body')
    autocomplete_fields = ('source',)
    date_hierarchy = 'published_at'
