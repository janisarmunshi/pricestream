"""Shared tick-query layer. The Data Explorer frontend view and the external
GET /api/v1/ticks/ endpoint both call into this so there is one query implementation,
not two.
"""
from apps.ticks.models import Tick


def query_ticks(*, account_ids=None, tokens=None, exch_seg=None, start=None, end=None):
    qs = Tick.objects.all()
    if account_ids:
        qs = qs.filter(account_id__in=account_ids)
    if tokens:
        qs = qs.filter(token__in=tokens)
    if exch_seg:
        qs = qs.filter(exch_seg=exch_seg)
    if start:
        qs = qs.filter(time__gte=start)
    if end:
        qs = qs.filter(time__lte=end)
    return qs.order_by('time')


def latest_tick_per_instrument(*, account_ids=None, tokens=None):
    """Most recent tick per (account, token) — a cheap "is this still updating"
    check without pulling a full range.
    """
    qs = Tick.objects.all()
    if account_ids:
        qs = qs.filter(account_id__in=account_ids)
    if tokens:
        qs = qs.filter(token__in=tokens)

    latest_ids = (
        qs.values('account_id', 'token')
        .order_by('account_id', 'token')
        .distinct()
    )
    results = []
    for row in latest_ids:
        tick = (
            qs.filter(account_id=row['account_id'], token=row['token'])
            .order_by('-time')
            .first()
        )
        if tick:
            results.append(tick)
    return results
