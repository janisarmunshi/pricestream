"""Account-level operations shared by the admin action and the frontend view —
one implementation of "test connection" / "force relogin", not two.
"""
import logging
from datetime import datetime

from django.utils import timezone

from apps.accounts.broker.finvasia import FinvasiaConnector

logger = logging.getLogger(__name__)


def test_login(account, force=False):
    """Force a fresh OAuth login for this account (clears the stored access token
    first when force=True) and record the outcome on the account row.

    Returns (ok: bool, error: str | None).
    """
    if force:
        account.access_token = ''
        account.save(update_fields=['access_token'])

    connector = FinvasiaConnector(account)
    conn = connector.get_connection_object(session_only=False)

    account.refresh_from_db()
    if conn is not None:
        account.last_login_status = 'OK'
        account.last_login_error = ''
        account.last_login_at = timezone.now()
        account.save(update_fields=['last_login_status', 'last_login_error', 'last_login_at'])
        return True, None

    error = connector.Errors.get('message') if connector.Errors else 'unknown login failure'
    account.last_login_status = 'FAILED'
    account.last_login_error = error
    account.last_login_at = timezone.now()
    account.save(update_fields=['last_login_status', 'last_login_error', 'last_login_at'])
    return False, error
