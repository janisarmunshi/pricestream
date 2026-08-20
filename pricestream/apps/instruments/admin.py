from django.contrib import admin

from apps.instruments.models import Script


@admin.register(Script)
class ScriptAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'exch_seg', 'token', 'name', 'expiry', 'lot_size', 'tick_size')
    list_filter = ('exch_seg', 'instrument_type')
    search_fields = ('symbol', 'symbol_finvasia', 'token', 'name')
