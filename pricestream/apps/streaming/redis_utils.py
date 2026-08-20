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
