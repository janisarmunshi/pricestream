from django.contrib import admin

from apps.streaming.models import StreamingConfig, StreamingSetting, Subscription


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('account', 'script', 'is_enabled', 'created_at')
    list_filter = ('is_enabled',)
    search_fields = ('account__nickname', 'script__symbol')


@admin.register(StreamingSetting)
class StreamingSettingAdmin(admin.ModelAdmin):
    list_display = ('account', 'desired_state', 'actual_state', 'last_started_at', 'last_stopped_at')
    list_filter = ('desired_state', 'actual_state')


@admin.register(StreamingConfig)
class StreamingConfigAdmin(admin.ModelAdmin):
    list_display = ('account', 'batch_size', 'flush_interval_seconds', 'retention_days', 'compress_after_days')
