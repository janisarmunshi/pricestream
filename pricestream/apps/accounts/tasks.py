"""
Celery dispatch for test_login — Selenium-based OAuth login can take up to ~120s
(see FinvasiaConnector._get_auth_code's own timeout), so it must never run inside
a Gunicorn worker request. Yantra's own startStrategy view makes exactly this point:
"Dispatch strategy start as a Celery task — never blocks the web worker. OAuth
(Selenium) inside startStrategyForUser can take up to 120s; running it in a
Gunicorn/nginx worker would exceed proxy timeouts." Running it synchronously here
was the actual cause of the app "hanging" for everyone else while one login ran —
gunicorn only has a handful of workers, and each blocked one is one fewer serving
any other request, admin included.
"""
from celery import shared_task

from apps.accounts.models import BrokerAccount
from apps.accounts.services import test_login
from apps.streaming.redis_utils import get_redis


def reauth_dispatch_lock_key(account_id):
    return f'ps:reauth_dispatch:{account_id}'


@shared_task(bind=True)
def test_login_task(self, account_id, force=False):
    """The task itself owns the reauth_dispatch lock for its actual runtime and
    deletes it on completion, rather than a caller pre-guessing a TTL long enough to
    cover an unpredictable Selenium run (real logins have taken anywhere from ~30s
    to 3+ minutes depending on TOTP-rotation retries and network latency) — a fixed
    TTL that expires before the task actually finishes lets a second beat tick
    dispatch a concurrent duplicate login, exactly as observed in production
    (two Selenium/Chrome sessions running at once for the same account).
    """
    lock_key = reauth_dispatch_lock_key(account_id)
    try:
        account = BrokerAccount.objects.filter(id=account_id).first()
        if not account:
            return None
        return test_login(account, force=force)
    finally:
        get_redis().delete(lock_key)
