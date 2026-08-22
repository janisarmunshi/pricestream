"""
Celery tasks for the streaming app:

- ingest_account_ticks: one long-lived task per active broker account. Opens the
  Finvasia WebSocket, and on every tick does exactly one fast durable operation —
  XADD onto that account's Redis Stream (pipelined in small batches for throughput,
  but the exposure window per tick stays a fraction of a second). Never touches
  Postgres directly.
- ws_health_check: beat task every ~5s. Second resilience layer, external to the
  ingestion task: checks per-instrument tick silence (a socket that looks connected
  but silently stopped receiving a subset of tokens) and the account's ownership
  lock (task died), and asks the ingestion task to resubscribe with the CURRENT
  enabled Subscription list when either trips. The FIRST layer — the connection-flag
  check (isWsConnected/wsIdleSeconds) — runs directly inside ingest_account_ticks,
  where the connector object actually lives.
- market_hours_supervisor: beat task every 1-2 min. Auto-starts/stops each account's
  ingest_account_ticks task based on whether any of its subscribed exchanges are
  still within their trading session for the day — genuinely autonomous, unlike
  Yantra's manually-triggered equivalent.
"""
import json
import logging
import time

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.accounts.broker.exchange_sessions import is_any_market_day_active, is_market_open
from apps.accounts.broker.finvasia import FinvasiaConnector
from apps.accounts.models import BrokerAccount
from apps.streaming.models import StreamingSetting, Subscription
from apps.streaming.redis_utils import get_redis, get_redis_streams, last_tick_key as _last_tick_key, tick_stream_key, ws_lock_key
from apps.ticks.models import SystemEvent

logger = logging.getLogger(__name__)

# In-process pipelined XADD buffer: flush every N ticks or T ms, whichever first —
# same throughput benefit as a single batched write, but the exposure window stays
# bounded to a fraction of a second instead of "however long a buffer holds it".
_XADD_FLUSH_COUNT = 50
_XADD_FLUSH_SECONDS = 0.1


def _current_subscriptions(account_id):
    """The CURRENT enabled Subscription rows for this account, read fresh from the DB.
    Called before every (re)subscribe — a stale in-memory list would silently under-
    or over-subscribe after a reconnect, defeating the whole point of the frontend.
    """
    rows = (
        Subscription.objects
        .filter(account_id=account_id, is_enabled=True, script__isnull=False)
        .select_related('script')
    )
    return [
        {'exchange': row.script.exch_seg, 'token': row.script.token, 'symbol': row.script.symbol}
        for row in rows
    ]


def _subscribed_exchanges(account_id):
    return list(
        Subscription.objects
        .filter(account_id=account_id, is_enabled=True)
        .values_list('script__exch_seg', flat=True)
        .distinct()
    )


# Minimum time between two resubscribes for the same account. A resubscribe closes
# and reopens the broker WS, which takes a real round-trip to settle — retriggering
# within that window (e.g. two health-check ticks queuing up before the first
# reconnect finishes) tears down a connection that just came up, rather than fixing
# anything. Deliberately longer than WS_HEALTH_CHECK_INTERVAL so at least one full
# health-check cycle elapses (with fresh ticks flowing) before silence is re-evaluated.
_RESUBSCRIBE_COOLDOWN_SECONDS = 30


def _resubscribe_cooldown_key(account_id):
    return f'ps:ws:resubscribe_cooldown:{account_id}'


def _resubscribe_cooldown_ok(r, account_id):
    return not r.exists(_resubscribe_cooldown_key(account_id))


def _mark_resubscribed(r, account_id):
    r.set(_resubscribe_cooldown_key(account_id), '1', ex=_RESUBSCRIBE_COOLDOWN_SECONDS)


class _TickBuffer:
    """Pipelines XADD calls in small bounded batches. Redis (AOF-backed) is the
    actual durability boundary, not this buffer — flush is time/count bounded so
    the in-flight window stays tiny even under a burst of ticks.
    """

    def __init__(self, redis_streams, stream_key):
        self.r = redis_streams
        self.stream_key = stream_key
        self.pending = []
        self.last_flush = time.time()

    def add(self, tick: dict):
        self.pending.append(tick)
        if len(self.pending) >= _XADD_FLUSH_COUNT or (time.time() - self.last_flush) >= _XADD_FLUSH_SECONDS:
            self.flush()

    def flush(self):
        if not self.pending:
            self.last_flush = time.time()
            return
        pipe = self.r.pipeline()
        for tick in self.pending:
            pipe.xadd(self.stream_key, {'data': json.dumps(tick)}, maxlen=2_000_000, approximate=True)
        pipe.execute()
        self.pending.clear()
        self.last_flush = time.time()


@shared_task(bind=True)
def ingest_account_ticks(self, account_id):
    """Long-lived WebSocket ingestion task, one per active broker account. Mirrors
    Yantra's workerAccountWebSocket lifetime pattern: runs until none of the account's
    subscribed exchanges are still in session for the day, or the StreamingSetting's
    desired_state moves off RUNNING.
    """
    account_id = int(account_id)
    account = BrokerAccount.objects.filter(id=account_id, is_active=True).first()
    if not account:
        return None

    exchanges = _subscribed_exchanges(account_id)
    if not exchanges:
        logger.info(f'[WS-ING] account={account_id} no enabled subscriptions — not starting')
        return None

    setting, _ = StreamingSetting.objects.get_or_create(account=account)

    connector = FinvasiaConnector(account)
    conn = connector.get_connection_object(session_only=True)
    if conn is None:
        logger.error(f'[WS-ING] account={account_id} broker connection failed')
        setting.actual_state = StreamingSetting.STATE_STOPPED
        setting.last_error = 'broker connection failed (no valid session token)'
        setting.save(update_fields=['actual_state', 'last_error'])
        return False

    redis_streams = get_redis_streams()
    buffer = _TickBuffer(redis_streams, tick_stream_key(account_id))
    r = get_redis()
    last_tick_key = _last_tick_key(account_id)

    # The broker's own tick payload only carries token ('tk') and exchange ('e'), not
    # symbol — this map is rebuilt on every (re)subscribe so on_tick can still attach
    # the human-readable symbol onto each tick row.
    token_to_symbol = {}

    def refresh_symbol_map(subs):
        token_to_symbol.clear()
        token_to_symbol.update({s['token']: s['symbol'] for s in subs})

    def on_tick(message):
        token = message.get('tk', '')
        r.hset(last_tick_key, token, time.time())
        r.expire(last_tick_key, 3600)
        tick = {
            'account_id': account_id,
            'exch_seg': message.get('e', ''),
            'token': token,
            'symbol': token_to_symbol.get(token, ''),
            'ltp': message.get('lp', 0),
            'volume': message.get('v', 0),
            'open': message.get('o', 0),
            'high': message.get('h', 0),
            'low': message.get('l', 0),
            'close': message.get('c', 0),
            'avg_price': message.get('ap', 0),
            'ts': time.time(),
        }
        buffer.add(tick)

    subscriptions = _current_subscriptions(account_id)
    refresh_symbol_map(subscriptions)
    started = connector.subscribe_websocket(subscriptions, tick_callback=on_tick)
    if not started:
        logger.warning(f'[WS-ING] account={account_id} WebSocket already owned by another task — exiting')
        return None

    setting.actual_state = StreamingSetting.STATE_RUNNING
    setting.celery_task_id = self.request.id or ''
    setting.last_started_at = timezone.now()
    setting.last_error = ''
    setting.save(update_fields=['actual_state', 'celery_task_id', 'last_started_at', 'last_error'])
    SystemEvent.log(account_id, SystemEvent.TYPE_CONNECT, f'WebSocket started, exchanges={exchanges}')

    lock_key = ws_lock_key(account_id)
    control_channel = f'ps:ws:control:{account_id}'
    control_pubsub = r.pubsub()
    control_pubsub.subscribe(control_channel)

    idle_cycles = 0
    # WS_HEALTH_CHECK_INTERVAL / 0.5s poll = cycles between connection-flag checks —
    # this is the FIRST layer of resilience (the authoritative "has it stopped"
    # signal, same as Yantra's isWsConnected()), running in-process where the
    # connector actually lives. The ws_health_check beat task is the SECOND layer,
    # catching per-instrument silence a connected-looking socket can still hide.
    health_check_cycles = max(1, int(settings.WS_HEALTH_CHECK_INTERVAL / 0.5))

    try:
        while is_any_market_day_active(exchanges):
            setting.refresh_from_db(fields=['desired_state'])
            if setting.desired_state != StreamingSetting.STATE_RUNNING:
                logger.info(f'[WS-ING] account={account_id} desired_state={setting.desired_state} — stopping')
                break

            r.expire(lock_key, 16 * 3600)  # keep the ownership lock alive for the day
            buffer.flush()

            idle_cycles += 1
            if (
                idle_cycles % health_check_cycles == 0
                and not connector.isWsConnected()
                and _resubscribe_cooldown_ok(r, account_id)
            ):
                idle_secs = connector.wsIdleSeconds()
                reason = f'connection flag reports disconnected (idle {idle_secs:.1f}s)'
                logger.warning(f'[WS-ING] account={account_id} {reason} — resubscribing')
                fresh_subscriptions = _current_subscriptions(account_id)
                refresh_symbol_map(fresh_subscriptions)
                connector.resubscribe_websocket(fresh_subscriptions)
                _mark_resubscribed(r, account_id)
                SystemEvent.log(account_id, SystemEvent.TYPE_RESUBSCRIBE, reason)

            # Drain every queued control message and act on only the last one — a
            # resubscribe takes long enough (close + reopen + broker round-trip)
            # that several health-check ticks can queue up requests before the first
            # one finishes; replaying each in turn tears down a connection that just
            # came up milliseconds earlier (observed as "Connection is already
            # closed" / "'NoneType' object has no attribute 'sock'" in production).
            latest_payload = None
            while True:
                msg = control_pubsub.get_message(timeout=0.5 if latest_payload is None else 0)
                if not msg:
                    break
                if msg.get('type') == 'message':
                    try:
                        latest_payload = json.loads(msg['data'])
                    except (TypeError, ValueError):
                        pass

            if latest_payload and latest_payload.get('action') == 'resubscribe':
                if _resubscribe_cooldown_ok(r, account_id):
                    logger.warning(
                        f"[WS-ING] account={account_id} resubscribe requested: {latest_payload.get('reason', '')}"
                    )
                    fresh_subscriptions = _current_subscriptions(account_id)
                    refresh_symbol_map(fresh_subscriptions)
                    connector.resubscribe_websocket(fresh_subscriptions)
                    _mark_resubscribed(r, account_id)
                    SystemEvent.log(account_id, SystemEvent.TYPE_RESUBSCRIBE, latest_payload.get('reason', ''))
                else:
                    logger.debug(f'[WS-ING] account={account_id} resubscribe request ignored — still in cooldown')
    except Exception as e:
        logger.error(f'[WS-ING] account={account_id} error: {e}', exc_info=True)
        SystemEvent.log(account_id, SystemEvent.TYPE_ERROR, str(e))
    finally:
        buffer.flush()
        connector.close_websocket()
        control_pubsub.close()
        r.delete(lock_key)
        r.delete(last_tick_key)
        setting.actual_state = StreamingSetting.STATE_STOPPED
        setting.last_stopped_at = timezone.now()
        setting.save(update_fields=['actual_state', 'last_stopped_at'])
        SystemEvent.log(account_id, SystemEvent.TYPE_DISCONNECT, 'WebSocket stopped')

    return 'ingestion completed'


@shared_task(bind=True)
def ws_health_check(self):
    """Beat task every ~5s (settings.WS_HEALTH_CHECK_INTERVAL). Second resilience
    layer on top of the connection-flag check already running inside
    ingest_account_ticks: resubscribe if the ownership lock is missing (the
    ingestion task died) OR if any subscribed instrument WHOSE EXCHANGE IS CURRENTLY
    OPEN has gone quiet beyond TICK_SILENCE_THRESHOLD_SECONDS — this catches a socket
    that looks "connected" but has silently stopped receiving a subset of this
    account's subscriptions specifically (e.g. a server-side resubscribe that dropped
    some tokens), which the connection flag alone would never surface. Instruments on
    a closed exchange (e.g. NSE/BSE after 15:30 while an MCX contract on the same
    account keeps trading until 23:55) are excluded from the silence check entirely —
    their silence is expected, not a dead feed.

    A resubscribe just requested (still within _RESUBSCRIBE_COOLDOWN_SECONDS) is
    never re-flagged — the new connection needs a real chance to start ticking again
    before its own silence is judged.

    This process doesn't hold the live FinvasiaConnector for a running ingestion task
    (that object lives inside the ingest_account_ticks worker process/thread), so it
    can't call isWsConnected()/resubscribe directly. Instead it reads the per-token
    last-tick-at hash the ingestion task maintains in Redis (ps:last_tick:{account_id})
    and asks that task to resubscribe via a pub/sub control message it listens for
    once per loop iteration (apps.streaming.tasks.ingest_account_ticks).
    """
    r = get_redis()
    running = StreamingSetting.objects.filter(actual_state=StreamingSetting.STATE_RUNNING)

    for setting in running:
        account_id = setting.account_id
        needs_resubscribe = False
        reason = ''

        if not r.exists(ws_lock_key(account_id)):
            needs_resubscribe = True
            reason = 'ownership lock missing (task likely died)'
        elif not _resubscribe_cooldown_ok(r, account_id):
            pass  # a resubscribe just happened; give the new connection time to tick
        else:
            # Only tokens whose exchange is CURRENTLY in session can be judged "gone
            # quiet" — NSE/BSE closing at 15:30 while an MCX contract on the same
            # account keeps ticking until 23:55 is completely normal, not a dead feed.
            subscribed = (
                Subscription.objects.filter(account_id=account_id, is_enabled=True)
                .values_list('script__token', 'script__exch_seg')
            )
            live_tokens = [token for token, exch_seg in subscribed if is_market_open(exch_seg)]

            last_ticks = r.hgetall(_last_tick_key(account_id))
            now = time.time()
            quiet_tokens = []
            for token in live_tokens:
                last_ts = last_ticks.get(token)
                if last_ts is None:
                    continue  # never ticked yet since (re)subscribe — not silence, just startup
                if now - float(last_ts) > settings.TICK_SILENCE_THRESHOLD_SECONDS:
                    quiet_tokens.append(token)
            if quiet_tokens:
                needs_resubscribe = True
                reason = f'instruments gone quiet beyond {settings.TICK_SILENCE_THRESHOLD_SECONDS}s: {quiet_tokens}'

        if needs_resubscribe:
            logger.warning(f'[WS-HEALTH] account={account_id} {reason} — publishing resubscribe request')
            r.publish(f'ps:ws:control:{account_id}', json.dumps({'action': 'resubscribe', 'reason': reason}))
            SystemEvent.log(account_id, SystemEvent.TYPE_RESUBSCRIBE, reason)


@shared_task(bind=True)
def market_hours_supervisor(self):
    """Beat task every 1-2 min (settings.MARKET_HOURS_SUPERVISOR_INTERVAL_MINUTES).
    Genuinely autonomous auto-start/stop, unlike Yantra's manually-triggered
    equivalent: for every account with at least one enabled Subscription, start its
    ingestion task if any subscribed exchange is still in session and it isn't
    already running (same ps:ws:{accountId} Redis-lock pattern guarantees single
    ownership so calling this every minute is harmless/idempotent). When none of an
    account's subscribed exchanges are still active, its own loop exits naturally —
    no force-kill needed here.
    """
    r = get_redis()
    account_ids = (
        Subscription.objects
        .filter(is_enabled=True, account__is_active=True)
        .values_list('account_id', flat=True)
        .distinct()
    )

    for account_id in account_ids:
        exchanges = _subscribed_exchanges(account_id)
        if not exchanges or not is_any_market_day_active(exchanges):
            continue

        setting, _ = StreamingSetting.objects.get_or_create(account_id=account_id)
        if setting.desired_state == StreamingSetting.STATE_PAUSED:
            continue  # explicit user pause overrides auto-start

        if r.exists(ws_lock_key(account_id)):
            continue  # already running somewhere

        setting.desired_state = StreamingSetting.STATE_RUNNING
        setting.save(update_fields=['desired_state'])
        ingest_account_ticks.delay(account_id)
        logger.info(f'[MARKET-HOURS] account={account_id} auto-started (exchanges={exchanges})')
