"""
Batch committer: consumes each account's Redis Stream via a consumer group, batches
entries (by count or a short time window), and does one bulk insert per batch into
the Timescale hypertable. Only after a CONFIRMED DB commit does it XACK+trim those
entries. If Postgres is down the stream just keeps growing (bounded by Redis memory,
monitored via StreamMetrics.stream_depth) and this task catches up once the DB is
back — no gap, only added latency, matching the stated tolerance ("delayed logging
is fine").

A batch entry that still fails after TICK_MAX_INSERT_RETRIES moves to FailedTick
(the DLQ) and gets XACK'd off the main stream so the pipeline keeps flowing — it is
not lost, not silently dropped, and doesn't block everything queued behind it.
"""
import json
import logging
import time
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import BrokerAccount
from apps.streaming.redis_utils import get_redis_streams, tick_stream_key
from apps.ticks.models import FailedTick, StreamMetrics, SystemEvent, Tick

logger = logging.getLogger(__name__)

CONSUMER_GROUP = settings.TICK_CONSUMER_GROUP
CONSUMER_NAME = 'committer-1'


def _ensure_group(r, stream_key):
    try:
        r.xgroup_create(stream_key, CONSUMER_GROUP, id='0', mkstream=True)
    except Exception as e:
        if 'BUSYGROUP' not in str(e):
            raise


def _build_tick(entry_id, tick: dict) -> Tick:
    return Tick(
        time=datetime.fromtimestamp(float(tick['ts']), tz=dt_timezone.utc),
        account_id=int(tick['account_id']),
        exch_seg=tick.get('exch_seg', ''),
        token=tick.get('token', ''),
        symbol=tick.get('symbol', ''),
        ltp=Decimal(str(tick.get('ltp', 0))),
        volume=int(float(tick.get('volume', 0) or 0)),
        open=Decimal(str(tick.get('open', 0) or 0)),
        high=Decimal(str(tick.get('high', 0) or 0)),
        low=Decimal(str(tick.get('low', 0) or 0)),
        close=Decimal(str(tick.get('close', 0) or 0)),
        avg_price=Decimal(str(tick.get('avg_price', 0) or 0)),
    )


def _commit_batch(entries):
    """Try a single bulk insert for the whole batch. On failure, fall back to
    inserting one row at a time so a single malformed entry doesn't sink the batch;
    the entry that fails alone is what goes to the DLQ.
    """
    ok_ids = []
    failed = []  # (entry_id, raw_fields, error)

    rows = []
    row_ids = []
    for entry_id, fields in entries:
        try:
            tick = json.loads(fields['data'])
            rows.append(_build_tick(entry_id, tick))
            row_ids.append(entry_id)
        except (KeyError, ValueError, TypeError, InvalidOperation) as e:
            failed.append((entry_id, fields, str(e)))

    if rows:
        try:
            with transaction.atomic():
                Tick.objects.bulk_create(rows)
            ok_ids.extend(row_ids)
        except Exception as e:
            logger.warning(f'[COMMITTER] bulk_create failed ({e}), retrying row-by-row')
            for entry_id, row in zip(row_ids, rows):
                try:
                    with transaction.atomic():
                        row.save()
                    ok_ids.append(entry_id)
                except Exception as row_exc:
                    failed.append((entry_id, entries_by_id(entries, entry_id), str(row_exc)))

    return ok_ids, failed


def entries_by_id(entries, entry_id):
    for eid, fields in entries:
        if eid == entry_id:
            return fields
    return {}


_PENDING_CLAIM_MIN_IDLE_MS = 30_000  # don't reclaim an entry another in-flight run is still working


@shared_task(bind=True)
def commit_account_ticks(self, account_id):
    """Drain one account's stream: reclaim any entries left pending by a crashed
    prior run, read new entries via the consumer group, bulk-insert, XACK only what
    committed, move persistent failures to the DLQ, and record a StreamMetrics
    snapshot. Safe to run repeatedly/concurrently thanks to the consumer group — a
    crash mid-batch leaves entries un-ACKed, and XAUTOCLAIM below is what actually
    picks those back up for retry (a plain XREADGROUP '>' only ever sees new
    entries, never pending ones).
    """
    r = get_redis_streams()
    stream_key = tick_stream_key(account_id)
    _ensure_group(r, stream_key)

    batch_size = settings.TICK_BATCH_SIZE
    flush_seconds = settings.TICK_BATCH_FLUSH_SECONDS

    collected = []

    _cursor = '0-0'
    while len(collected) < batch_size:
        _cursor, claimed, _deleted = r.xautoclaim(
            stream_key, CONSUMER_GROUP, CONSUMER_NAME,
            min_idle_time=_PENDING_CLAIM_MIN_IDLE_MS, start_id=_cursor,
            count=batch_size - len(collected),
        )
        collected.extend(claimed)
        if _cursor == '0-0':
            break

    deadline = time.time() + flush_seconds
    while time.time() < deadline and len(collected) < batch_size:
        remaining = max(1, int((deadline - time.time()) * 1000))
        resp = r.xreadgroup(CONSUMER_GROUP, CONSUMER_NAME, {stream_key: '>'}, count=batch_size - len(collected), block=remaining)
        if not resp:
            break
        for _, entries in resp:
            collected.extend(entries)

    if not collected:
        return 0

    ok_ids, failed = _commit_batch(collected)

    if ok_ids:
        r.xack(stream_key, CONSUMER_GROUP, *ok_ids)
        r.xdel(stream_key, *ok_ids)

    if failed:
        _handle_failures(r, stream_key, failed, account_id)

    _record_metrics(r, stream_key, account_id, len(ok_ids))

    return len(ok_ids)


def _handle_failures(r, stream_key, failed, account_id):
    """Track retry counts per stream entry; once an entry has failed
    TICK_MAX_INSERT_RETRIES times, move it to the DLQ (FailedTick) and ACK it off
    the main stream so it stops blocking redelivery of everything behind it.
    """
    max_retries = settings.TICK_MAX_INSERT_RETRIES
    for entry_id, fields, error in failed:
        retry_key = f'ps:tick_retry:{stream_key}:{entry_id}'
        retries = r.incr(retry_key)
        r.expire(retry_key, 3600)
        if retries >= max_retries:
            raw = fields.get('data', '{}')
            try:
                raw_payload = json.loads(raw)
            except (TypeError, ValueError):
                raw_payload = {'raw': raw}
            FailedTick.objects.create(
                raw_payload=raw_payload, error=error, stream_entry_id=entry_id, account_id=account_id,
            )
            SystemEvent.log(account_id, SystemEvent.TYPE_DLQ, f'entry {entry_id}: {error}')
            r.xack(stream_key, CONSUMER_GROUP, entry_id)
            r.xdel(stream_key, entry_id)
            r.delete(retry_key)


def _record_metrics(r, stream_key, account_id, committed_count):
    stream_depth = r.xlen(stream_key)
    dlq_depth = FailedTick.objects.filter(account_id=account_id, resolved=False).count()
    last_tick = Tick.objects.filter(account_id=account_id).order_by('-time').values_list('time', flat=True).first()

    from apps.streaming.models import StreamingConfig
    config = StreamingConfig.objects.filter(account_id=account_id).first() or StreamingConfig.objects.filter(account__isnull=True).first()
    silence_threshold = config.alert_lag_seconds if config else settings.TICK_SILENCE_THRESHOLD_SECONDS

    StreamMetrics.objects.create(
        account_id=account_id,
        tick_rate_per_min=committed_count * (60.0 / max(settings.TICK_BATCH_FLUSH_SECONDS, 0.1)) if committed_count else 0,
        committer_lag_seconds=(timezone.now() - last_tick).total_seconds() if last_tick else 0,
        stream_depth=stream_depth,
        dlq_depth=dlq_depth,
        last_tick_at=last_tick,
        settings_silence_threshold=silence_threshold,
    )


@shared_task(bind=True)
def commit_all_accounts(self):
    """Dispatch commit_account_ticks for every account with a non-empty stream key
    pattern — cheap to run on a short beat interval since each per-account task is
    itself bounded by TICK_BATCH_FLUSH_SECONDS.
    """
    account_ids = BrokerAccount.objects.filter(is_active=True).values_list('id', flat=True)
    for account_id in account_ids:
        commit_account_ticks.delay(account_id)
