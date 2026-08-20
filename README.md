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

Topology: gunicorn (frontend), celeryd (one long-lived task per active account +
committer), celerybeat (health-check every ~5s, market-hours supervisor every
~1min, daily script sync), Postgres+TimescaleDB and Redis (AOF-enabled) on the same
VPS to start. `deploy/` has the systemd unit files and the celeryd force-restart
script, all pointed at `/opt/pricestream` under a `pricestream` service user —
adjust `User=`/`WorkingDirectory=`/paths in the `.service` files if deploying
under a different account (e.g. this VPS's `worker` user).

### First-time VPS setup

```bash
ssh worker@200.97.160.153

sudo apt update
sudo apt install -y python3-venv python3-pip postgresql redis-server git

# TimescaleDB (Ubuntu/Debian — see https://docs.timescale.com/install/latest/self-hosted/installation-linux/
# for the exact repo setup for your distro/release before this step):
sudo apt install -y timescaledb-2-postgresql-16
sudo timescaledb-tune --yes
sudo systemctl restart postgresql

sudo -u postgres psql -c "CREATE USER pricestream WITH PASSWORD 'change-me';"
sudo -u postgres psql -c "CREATE DATABASE pricestream OWNER pricestream;"

# Redis: enable AOF persistence so a restart never loses queued-but-uncommitted ticks
sudo sed -i 's/^appendonly no/appendonly yes/' /etc/redis/redis.conf
sudo systemctl restart redis-server

sudo mkdir -p /opt/pricestream
sudo chown worker:worker /opt/pricestream
git clone <your-remote-url> /opt/pricestream
cd /opt/pricestream

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp pricestream/.env.example pricestream/.env
nano pricestream/.env   # set SECRET_KEY, CRYPTOGRAPHY_KEY, DB_*, REDIS_*, ALLOWED_HOSTS

cd pricestream
python manage.py migrate
python manage.py createsuperuser
python manage.py collectstatic --noinput
```

Then install the systemd units (edit `User=`/paths in the `.service` files first
if not using a dedicated `pricestream` user):

```bash
sudo cp /opt/pricestream/deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pricestream-gunicorn pricestream-celeryd pricestream-celerybeat
sudo systemctl status pricestream-gunicorn pricestream-celeryd pricestream-celerybeat --no-pager
```

Front gunicorn with nginx/Caddy for TLS + the public hostname — not included here
since it depends on what's already on this VPS.

### Redeploying after a code change

```bash
cd /opt/pricestream && git pull
source venv/bin/activate && pip install -r requirements.txt
cd pricestream && python manage.py migrate && python manage.py collectstatic --noinput
sudo systemctl restart pricestream-gunicorn
../deploy/restart-celeryd.sh   # force-restart sequence, not a plain systemctl restart
sudo systemctl restart pricestream-celerybeat
```

## Verification checklist

See the "Verification approach" section of [PLAN.md](PLAN.md) for the full list
(kill -9 mid-batch, Postgres outage absorption, Redis restart durability, dead
socket detection, live subscription add/remove, market-hours auto start/stop, DLQ
malformed-tick handling).
