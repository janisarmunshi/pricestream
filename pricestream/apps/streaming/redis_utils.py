import redis
from django.conf import settings


def get_redis():
    return redis.Redis(
        host=settings.REDIS_HOST, port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD or None, decode_responses=True,
    )


def get_redis_streams():
    """Separate logical DB for tick streams, kept apart from the Celery broker DB
    and the cache DB so stream growth is easy to reason about/monitor independently.
    """
    return redis.Redis(
        host=settings.REDIS_HOST, port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD or None, decode_responses=True, db=2,
    )


def ws_lock_key(account_id):
    return f'ps:ws:{account_id}'


def tick_stream_key(account_id):
    return f'{settings.TICK_STREAM_KEY_PREFIX}:{account_id}'


def last_tick_key(account_id):
    """HSET token -> unix ts, maintained by ingest_account_ticks on every tick and
    read by both ws_health_check and the dashboard — an O(1) Redis hash lookup, not
    a query against the tick hypertable, so surfacing "last updated" per token on
    the dashboard never costs a Tick table scan.
    """
    return f'ps:last_tick:{account_id}'
