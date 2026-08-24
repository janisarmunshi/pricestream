import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('pricestream')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

app.conf.beat_schedule = {
    'ws-health-check': {
        'task': 'apps.streaming.tasks.ws_health_check',
        'schedule': 5.0,
    },
    'market-hours-supervisor': {
        'task': 'apps.streaming.tasks.market_hours_supervisor',
        'schedule': 60.0,
    },
    'commit-all-accounts': {
        'task': 'apps.ticks.tasks.commit_all_accounts',
        'schedule': 2.0,
    },
    'daily-script-master-sync': {
        'task': 'apps.instruments.tasks.sync_script_master',
        'schedule': crontab(hour=6, minute=0),
    },
    'daily-broker-preauth': {
        # Well before NSE's 09:15 open — mirrors Yantra's own
        # workerPreAuthenticateBrokers precedent (pre-auth at 8:30 AM there).
        # CELERY_TIMEZONE is Asia/Kolkata, so this is 08:00 IST.
        'task': 'apps.accounts.tasks.preauthenticate_all_accounts',
        'schedule': crontab(hour=8, minute=0),
    },
    'hourly-expired-script-cleanup': {
        'task': 'apps.instruments.tasks.sync_expired_scripts',
        'schedule': crontab(minute=0),
    },
}


@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
