from django.db import models

from apps.accounts.models import BrokerAccount
from apps.instruments.models import Script


class Subscription(models.Model):
    """Account x instrument the user has picked in the frontend to be streamed.
    The reconnect loop always re-reads the enabled rows here before resubscribing —
    never replays a stale in-memory list — so add/remove takes effect on next reconnect
    without a manual restart.
    """

    account = models.ForeignKey(BrokerAccount, on_delete=models.CASCADE, related_name='subscriptions')
    script = models.ForeignKey(Script, on_delete=models.CASCADE, related_name='subscriptions')
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('account', 'script')]

    def __str__(self):
        return f'{self.account.nickname}:{self.script.symbol}'


class StreamingSetting(models.Model):
    """Per-account streaming control state. `desired_state` is what the auto-start
    supervisor and manual controls agree should be true; `PAUSED` is a distinct state
    from disabling every Subscription — it stops the WS task without touching what it
    was subscribed to, so resuming doesn't require re-selecting instruments.
    """

    STATE_STOPPED = 'STOPPED'
    STATE_RUNNING = 'RUNNING'
    STATE_PAUSED = 'PAUSED'
    STATE_CHOICES = [
        (STATE_STOPPED, 'Stopped'),
        (STATE_RUNNING, 'Running'),
        (STATE_PAUSED, 'Paused'),
    ]

    account = models.OneToOneField(BrokerAccount, on_delete=models.CASCADE, related_name='streaming_setting')
    desired_state = models.CharField(max_length=10, choices=STATE_CHOICES, default=STATE_STOPPED)
    actual_state = models.CharField(max_length=10, choices=STATE_CHOICES, default=STATE_STOPPED)
    celery_task_id = models.CharField(max_length=64, blank=True, default='')
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_stopped_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')

    def __str__(self):
        return f'{self.account.nickname}: {self.actual_state}'


class StreamingConfig(models.Model):
    """Batch size / flush interval feed the committer task directly; retention/
    compression map onto TimescaleDB's own add_retention_policy/add_compression_policy
    rather than a hand-rolled scheme — this row only stores the knobs a human tunes.
    """

    account = models.OneToOneField(
        BrokerAccount, on_delete=models.CASCADE, related_name='streaming_config',
        null=True, blank=True, help_text='Leave blank for the global default config.',
    )
    batch_size = models.IntegerField(default=500)
    flush_interval_seconds = models.FloatField(default=2.0)
    retention_days = models.IntegerField(default=365)
    compress_after_days = models.IntegerField(default=7)
    alert_lag_seconds = models.IntegerField(default=60, help_text='SystemEvent warning when committer lag exceeds this.')

    def __str__(self):
        return f'Config for {self.account.nickname}' if self.account_id else 'Global default config'
