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


@shared_task(bind=True)
def test_login_task(self, account_id, force=False):
    account = BrokerAccount.objects.filter(id=account_id).first()
    if not account:
        return None
    return test_login(account, force=force)
