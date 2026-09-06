from django.contrib import admin

from .models import ContentItem, ContentTopic, Entity, Topic


class EntityInline(admin.TabularInline):
    model = Entity
    extra = 0
    fields = ('text', 'label', 'mention_count', 'confidence')


class ContentTopicInline(admin.TabularInline):
    model = ContentTopic
    extra = 0
    autocomplete_fields = ('topic',)


@admin.register(ContentItem)
class ContentItemAdmin(admin.ModelAdmin):
    list_display = ('title', 'source', 'status', 'language', 'analyzer_version', 'published_at')
    list_filter = ('status', 'language', 'analyzer_version', 'source')
    search_fields = ('title', 'url', 'author', 'body')
    autocomplete_fields = ('source',)
    date_hierarchy = 'published_at'
    readonly_fields = ('summary', 'sentiment', 'analyzer_version', 'analyzed_at')
    inlines = (EntityInline, ContentTopicInline)


@admin.register(Entity)
class EntityAdmin(admin.ModelAdmin):
    list_display = ('text', 'label', 'mention_count', 'content_item')
    list_filter = ('label',)
    search_fields = ('text',)
    autocomplete_fields = ('content_item',)


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ('label',)
    search_fields = ('label',)
