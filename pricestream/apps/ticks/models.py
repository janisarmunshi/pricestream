from django.db import models

from apps.accounts.models import BrokerAccount


class Tick(models.Model):
    """The TimescaleDB hypertable. `time` is the hypertable partitioning column
    (converted to a hypertable via migration 0002, not a Django-native feature).
    No Django-managed primary key auto-increment sequence dominates here — Timescale
    hypertables work fine with a composite index instead of relying on PK lookups.
    """

    time = models.DateTimeField(db_index=True)
    account_id = models.IntegerField()
    exch_seg = models.CharField(max_length=10)
    token = models.CharField(max_length=40)
    symbol = models.CharField(max_length=40, blank=True, default='')

    ltp = models.DecimalField(max_digits=18, decimal_places=4)
    volume = models.BigIntegerField(default=0)
    open = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    high = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    low = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    close = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    avg_price = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta:
        indexes = [
            models.Index(fields=['account_id', 'token', 'time']),
            models.Index(fields=['exch_seg', 'token', 'time']),
        ]


class FailedTick(models.Model):
    """Dead Letter Queue table. A batch entry that fails to insert even after retries
    (bad/malformed payload, constraint violation) lands here instead of blocking
    everything queued behind it or silently vanishing.
    """

    raw_payload = models.JSONField()
    error = models.TextField()
    stream_entry_id = models.CharField(max_length=40, blank=True, default='')
    account_id = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']


class SystemEvent(models.Model):
    """Connection drops, resubscribes, DLQ entries, lag-threshold breaches."""

    TYPE_CONNECT = 'CONNECT'
    TYPE_DISCONNECT = 'DISCONNECT'
    TYPE_RESUBSCRIBE = 'RESUBSCRIBE'
    TYPE_ERROR = 'ERROR'
    TYPE_DLQ = 'DLQ'
    TYPE_LAG = 'LAG'
    TYPE_CHOICES = [
        (TYPE_CONNECT, 'Connect'),
        (TYPE_DISCONNECT, 'Disconnect'),
        (TYPE_RESUBSCRIBE, 'Resubscribe'),
        (TYPE_ERROR, 'Error'),
        (TYPE_DLQ, 'DLQ'),
        (TYPE_LAG, 'Lag'),
    ]

    account_id = models.IntegerField(null=True, blank=True)
    event_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    @classmethod
    def log(cls, account_id, event_type, message=''):
        return cls.objects.create(account_id=account_id, event_type=event_type, message=message)


class StreamMetrics(models.Model):
    """Periodic per-account snapshot — tick rate, lag, Redis stream/DLQ depth, storage
    size — cheap to populate piggybacking on the health-check task that's already
    running every ~5s, rather than standing up separate metrics infrastructure.
    """

    account_id = models.IntegerField(db_index=True)
    tick_rate_per_min = models.FloatField(default=0)
    committer_lag_seconds = models.FloatField(default=0)
    stream_depth = models.IntegerField(default=0)
    dlq_depth = models.IntegerField(default=0)
    last_tick_at = models.DateTimeField(null=True, blank=True)
    settings_silence_threshold = models.IntegerField(default=120)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
