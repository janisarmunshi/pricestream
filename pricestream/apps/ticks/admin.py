from django.contrib import admin

from apps.ticks.models import FailedTick, StreamMetrics, SystemEvent, Tick


@admin.register(Tick)
class TickAdmin(admin.ModelAdmin):
    list_display = ('time', 'account_id', 'exch_seg', 'token', 'symbol', 'ltp', 'volume')
    list_filter = ('exch_seg',)
    search_fields = ('token', 'symbol')
    date_hierarchy = 'time'


@admin.register(FailedTick)
class FailedTickAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'account_id', 'stream_entry_id', 'error', 'resolved')
    list_filter = ('resolved',)


@admin.register(SystemEvent)
class SystemEventAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'account_id', 'event_type', 'message')
    list_filter = ('event_type',)


@admin.register(StreamMetrics)
class StreamMetricsAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'account_id', 'tick_rate_per_min', 'committer_lag_seconds', 'stream_depth', 'dlq_depth')
    list_filter = ('account_id',)
