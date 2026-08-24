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


@shared_task(bind=True)
def preauthenticate_all_accounts(self):
    """Beat task at ~08:00 IST, well before NSE's 09:15 open — mirrors Yantra's
    workerPreAuthenticateBrokers exactly: pre-authenticate every account with at
    least one enabled Subscription via Selenium+TOTP NOW, while there's no time
    pressure, so a fresh access_token is already sitting in the DB by market open.
    market_hours_supervisor then only ever needs a fast token *validation* at the
    actual open, never a ~30s-3min Selenium round-trip blocking the time-critical
    start of the trading session — which is what caused ingestion to sit unauthenticated
    for hours when it was only ever attempted reactively at auto-start time.

    Staggered 5s apart per account (same interval as Yantra) so many concurrent
    headless Chrome sessions don't all launch in the same instant on one VPS.

    Always force=True: this is a scheduled daily re-auth, not a "reuse it if it's
    still valid" check — a leftover token from a prior day (or a prior manual
    login) validating successfully here would skip the whole point of doing this
    ahead of market open, since a token that validates now could still expire
    partway through the trading session. Always get a genuinely fresh one.
    """
    account_ids = (
        BrokerAccount.objects.filter(
            is_active=True, subscriptions__is_enabled=True,
        ).distinct().values_list('id', flat=True)
    )
    for i, account_id in enumerate(account_ids):
        test_login_task.apply_async((account_id,), kwargs={'force': True}, countdown=i * 5)
    return f'pre-auth dispatched for {len(account_ids)} account(s)'
