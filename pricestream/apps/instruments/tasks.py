from celery import shared_task

from apps.instruments.services import sync_all_exchanges


@shared_task(bind=True)
def sync_script_master(self):
    return sync_all_exchanges()
