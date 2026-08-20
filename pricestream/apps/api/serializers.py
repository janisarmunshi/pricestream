from rest_framework import serializers

from apps.instruments.models import Script
from apps.ticks.models import Tick


class ScriptSerializer(serializers.ModelSerializer):
    class Meta:
        model = Script
        fields = ['token', 'symbol', 'name', 'exch_seg', 'expiry', 'lot_size', 'tick_size', 'instrument_type']


class TickSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tick
        fields = [
            'time', 'account_id', 'exch_seg', 'token', 'symbol',
            'ltp', 'volume', 'open', 'high', 'low', 'close', 'avg_price',
        ]
