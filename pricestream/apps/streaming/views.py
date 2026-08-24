import csv
from datetime import datetime, timezone as dt_timezone

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import BrokerAccount
from apps.streaming.models import StreamingConfig, StreamingSetting, Subscription
from apps.streaming.redis_utils import get_redis, last_tick_key
from apps.streaming.tasks import ingest_account_ticks
from apps.ticks.models import StreamMetrics, SystemEvent
from apps.ticks.queries import query_ticks


@login_required
def dashboard(request):
    """Live connection status, tick rate, lag, storage usage, and the enabled token
    list with last-updated time per account. The per-token timestamps come from the
    Redis hash ingest_account_ticks already maintains for the WS health check
    (ps:last_tick:{account_id}) — an O(1) hash read per account, not a query against
    the tick hypertable, so this adds no load proportional to how much history has
    been captured.
    """
    accounts = BrokerAccount.objects.filter(owner=request.user)
    r = get_redis()
    rows = []
    for account in accounts:
        setting = getattr(account, 'streaming_setting', None)
        metrics = StreamMetrics.objects.filter(account_id=account.id).order_by('-created_at').first()

        subs = (
            Subscription.objects.filter(account_id=account.id, is_enabled=True)
            .select_related('script')
            .order_by('script__exch_seg', 'script__symbol')
        )
        last_ticks = r.hgetall(last_tick_key(account.id))
        tokens = []
        for sub in subs:
            ts = last_ticks.get(sub.script.token)
            last_updated = datetime.fromtimestamp(float(ts), tz=dt_timezone.utc) if ts else None
            tokens.append({'script': sub.script, 'last_updated': last_updated})

        rows.append({'account': account, 'setting': setting, 'metrics': metrics, 'tokens': tokens})
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


def _data_explorer_queryset(request):
    """Shared by the AJAX query endpoint and the CSV export so there is one
    filter-parsing implementation, not two that could quietly drift apart.
    Only returns ticks for accounts the requesting user actually owns.
    """
    owned_account_ids = set(
        BrokerAccount.objects.filter(owner=request.user).values_list('id', flat=True)
    )
    token = request.GET.get('token')
    start = request.GET.get('start')
    end = request.GET.get('end')

    try:
        account_id = int(request.GET.get('account_id', ''))
    except ValueError:
        account_id = None

    if account_id is None or account_id not in owned_account_ids:
        return query_ticks(account_ids=[])  # empty result, not another user's data

    return query_ticks(
        account_ids=[account_id],
        tokens=[token] if token else None,
        start=parse_datetime(start) if start else None,
        end=parse_datetime(end) if end else None,
    )


@login_required
def data_explorer(request):
    """Query historical ticks, export CSV, basic charting. The results table is
    populated via AJAX (data_explorer_query) so pressing Query never reloads the
    page or clears the date inputs — a plain <form method="get"> would force a full
    navigation. The date inputs default to today's date on first load.
    """
    accounts = BrokerAccount.objects.filter(owner=request.user)
    # localtime(), not utcnow() — TIME_ZONE is Asia/Kolkata, so this is "today" in
    # IST, matching what the data itself will be shown in (see data_explorer_query/
    # data_explorer_export below) rather than flipping a date early per UTC.
    today = timezone.localtime(timezone.now()).date().isoformat()
    return render(request, 'pricestream/data_explorer.html', {'accounts': accounts, 'today': today})


@login_required
@require_GET
def data_explorer_query(request):
    """AJAX JSON endpoint backing the Data Explorer results table — same filters as
    the CSV export and the external API's GET /api/v1/ticks/, one query
    implementation shared by all three.

    query_ticks() orders ascending (time), which is correct for a full CSV
    export/report but wrong for this preview: taking the first 1000 rows of a busy
    trading day returns the EARLIEST ticks, silently truncating the view partway
    through the afternoon and making it look like logging stopped hours ago when it
    hadn't — confirmed live (a token still updating at 20:03 IST showed no data past
    ~19:15 in this view, purely because ~1000 ticks across the day's subscribed
    instruments had already accumulated by then). Reverse in Python rather than
    change query_ticks' own ordering, since the CSV export and the external API both
    depend on it staying chronological.
    """
    ticks = list(_data_explorer_queryset(request).order_by('-time')[:1000])
    ticks.reverse()  # chronological in the table, only the SELECTION was "latest 1000"
    return JsonResponse({'results': [
        {
            # t.time is stored/queried in UTC (Django's USE_TZ convention) — convert
            # to IST (settings.TIME_ZONE) before rendering, same as DRF's
            # DateTimeField already does automatically for the external API. Left as
            # plain .isoformat() before, this showed raw UTC with a +00:00 offset.
            'time': timezone.localtime(t.time).isoformat(),
            'account_id': t.account_id,
            'exch_seg': t.exch_seg,
            'token': t.token,
            'symbol': t.symbol,
            'ltp': str(t.ltp),
            'volume': t.volume,
        }
        for t in ticks
    ]})


@login_required
@require_GET
def data_explorer_export(request):
    """CSV export — streams straight to a file download, never rendered on screen."""
    ticks = _data_explorer_queryset(request)[:100_000]

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="ticks_export.csv"'
    writer = csv.writer(response)
    writer.writerow(['time', 'account_id', 'exch_seg', 'token', 'symbol', 'ltp', 'volume'])
    for t in ticks:
        writer.writerow([timezone.localtime(t.time).isoformat(), t.account_id, t.exch_seg, t.token, t.symbol, t.ltp, t.volume])
    return response


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
