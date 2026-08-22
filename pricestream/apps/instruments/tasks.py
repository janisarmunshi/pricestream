from celery import shared_task

from apps.instruments.services import delete_expired_scripts, sync_all_exchanges


@shared_task(bind=True)
def sync_script_master(self):
    return sync_all_exchanges()


@shared_task(bind=True)
def sync_expired_scripts(self):
    """Standalone expiry cleanup, separate from the full symbol-master download —
    matches Yantra's own split (symbols/sync/ vs symbols/sync/expiry/) so stale
    expired contracts can be swept more often than the full daily sync needs to run.
    """
    return delete_expired_scripts()
