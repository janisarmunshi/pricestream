# PriceStream

Standalone tick-logging service. Passively captures live market data (price ticks)
for instruments across multiple Finvasia broker accounts, for later offline
analysis — separate from any live trading platform. See [PLAN.md](PLAN.md) for the
full design rationale.

## Stack

Django + Celery + Redis + PostgreSQL/TimescaleDB. Django REST Framework for the
external read API.

## Local setup

```bash
cd pricestream
python -m venv ../venv
../venv/Scripts/activate   # or venv/bin/activate on Linux/Mac
pip install -r ../requirements.txt

cp .env.example .env   # then edit SECRET_KEY, CRYPTOGRAPHY_KEY, DB_*, REDIS_*
```

Generate secrets:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"       # SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"       # CRYPTOGRAPHY_KEY
```

Postgres + TimescaleDB (local dev via Docker):

```bash
docker run -d --name pricestream-db \
  -e POSTGRES_DB=pricestream -e POSTGRES_USER=pricestream -e POSTGRES_PASSWORD=pricestream \
  -p 5432:5432 timescale/timescaledb:latest-pg16
```

Redis (AOF must be enabled in production — see deploy/ notes):

```bash
docker run -d --name pricestream-redis -p 6379:6379 redis:7 redis-server --appendonly yes
```

Then:

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

In separate terminals:

```bash
celery -A config worker --loglevel=info --pool=threads --concurrency=20
celery -A config beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler
```

## Apps

- `apps/accounts` — BrokerAccount model (Fernet-encrypted credentials via
  django-cryptography), the ported Finvasia login flow + WebSocket connector
  (`apps/accounts/broker/finvasia.py`, trimmed from Yantra's bnrathi.py).
- `apps/instruments` — Script model, daily symbol-master sync from Finvasia.
- `apps/streaming` — Subscription/StreamingSetting/StreamingConfig models, the WS
  ingestion task, health-check + auto-resubscribe, market-hours auto-start
  supervisor.
- `apps/ticks` — Tick hypertable, batch committer (Redis Stream consumer group →
  bulk insert → ACK-after-commit), DLQ (FailedTick), SystemEvent log,
  StreamMetrics.
- `apps/api` — DRF read-only external API, ApiKey auth + scoping, per-key
  throttling.

## Deployment

See `deploy/` for systemd unit files (gunicorn, celeryd, celerybeat) and the
force-restart script for celeryd. Topology: gunicorn (frontend), celeryd (one
long-lived task per active account + committer), celerybeat (health-check every
~5s, market-hours supervisor every ~1min, daily script sync), Postgres+TimescaleDB
and Redis (AOF-enabled) on the same VPS to start.

## Verification checklist

See the "Verification approach" section of [PLAN.md](PLAN.md) for the full list
(kill -9 mid-batch, Postgres outage absorption, Redis restart durability, dead
socket detection, live subscription add/remove, market-hours auto start/stop, DLQ
malformed-tick handling).
