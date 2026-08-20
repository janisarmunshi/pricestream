from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.models import BrokerAccount
from apps.streaming.models import StreamingConfig, StreamingSetting, Subscription
from apps.streaming.tasks import ingest_account_ticks
from apps.ticks.models import StreamMetrics, SystemEvent
from apps.ticks.queries import query_ticks


@login_required
def dashboard(request):
    """Live connection status, tick rate, lag, storage usage — backed by the latest
    StreamMetrics snapshot per account.
    """
    accounts = BrokerAccount.objects.filter(owner=request.user)
    rows = []
    for account in accounts:
        setting = getattr(account, 'streaming_setting', None)
        metrics = StreamMetrics.objects.filter(account_id=account.id).order_by('-created_at').first()
        rows.append({'account': account, 'setting': setting, 'metrics': metrics})
    return render(request, 'pricestream/dashboard.html', {'rows': rows})


@login_required
def streaming_control(request):
    """Start/stop/pause per account, active subscriptions. Pause stops the WS task
    without deleting what it was subscribed to, so resuming doesn't require
    re-selecting instruments.
    """
    accounts = BrokerAccount.objects.filter(owner=request.user).prefetch_related('subscriptions__script')
    return render(request, 'pricestream/streaming_control.html', {'accounts': accounts})


@login_required
@require_POST
def streaming_action(request, account_id):
    account = get_object_or_404(BrokerAccount, id=account_id, owner=request.user)
    action = request.POST.get('action')
    setting, _ = StreamingSetting.objects.get_or_create(account=account)

    if action == 'start':
        setting.desired_state = StreamingSetting.STATE_RUNNING
        setting.save(update_fields=['desired_state'])
        ingest_account_ticks.delay(account.id)
    elif action == 'pause':
        setting.desired_state = StreamingSetting.STATE_PAUSED
        setting.save(update_fields=['desired_state'])
    elif action == 'stop':
        setting.desired_state = StreamingSetting.STATE_STOPPED
        setting.save(update_fields=['desired_state'])

    return redirect('streaming_control')


@login_required
def data_explorer(request):
    """Query historical ticks, export CSV, basic charting. Reuses the same internal
    ticks-query layer the external API's GET /api/v1/ticks/ calls.
    """
    accounts = BrokerAccount.objects.filter(owner=request.user)
    account_id = request.GET.get('account_id')
    token = request.GET.get('token')
    start = request.GET.get('start')
    end = request.GET.get('end')

    ticks = []
    if account_id:
        ticks = query_ticks(
            account_ids=[int(account_id)],
            tokens=[token] if token else None,
            start=start or None,
            end=end or None,
        )[:1000]

    return render(request, 'pricestream/data_explorer.html', {'accounts': accounts, 'ticks': ticks})


@login_required
def settings_view(request):
    """Batch size, flush interval, retention policy, alert thresholds per account."""
    accounts = BrokerAccount.objects.filter(owner=request.user)
    if request.method == 'POST':
        account = get_object_or_404(BrokerAccount, id=request.POST.get('account_id'), owner=request.user)
        config, _ = StreamingConfig.objects.get_or_create(account=account)
        config.batch_size = int(request.POST.get('batch_size', config.batch_size))
        config.flush_interval_seconds = float(request.POST.get('flush_interval_seconds', config.flush_interval_seconds))
        config.retention_days = int(request.POST.get('retention_days', config.retention_days))
        config.compress_after_days = int(request.POST.get('compress_after_days', config.compress_after_days))
        config.alert_lag_seconds = int(request.POST.get('alert_lag_seconds', config.alert_lag_seconds))
        config.save()
        return redirect('settings')

    return render(request, 'pricestream/settings.html', {'accounts': accounts})


@login_required
def logs_and_alerts(request):
    """Error logs, connection drops, lag warnings. v1: in-app only."""
    account_ids = BrokerAccount.objects.filter(owner=request.user).values_list('id', flat=True)
    events = SystemEvent.objects.filter(account_id__in=account_ids)[:200]
    return render(request, 'pricestream/logs_and_alerts.html', {'events': events})
