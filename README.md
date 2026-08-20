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

Topology: gunicorn (frontend, on `127.0.0.1:8002` — ports 8000/8001 on this VPS
are already used by other apps), celeryd (one long-lived task per active account +
committer), celerybeat (health-check every ~5s, market-hours supervisor every
~1min, daily script sync), Postgres+TimescaleDB and Redis (AOF-enabled) on the same
VPS. Deployed at `https://marketmantra.tech/pricestream` — a **path prefix**, not a
subdomain, alongside the existing `marketmantra.com` static site and other apps on
this VPS. TLS is a self-signed OpenSSL certificate (no public CA), since this is an
internal/operator-facing tool. `deploy/` has the systemd unit files (running as the
VPS's `worker` user), the nginx server block, and the celeryd force-restart script.

`marketmantra.tech` already resolves to this VPS's IP; nginx has no server block
for it yet (only `marketmantra.com`), so the block below is new, not a replacement.

### First-time VPS setup

Run as `worker` on the VPS:

```bash
#!/bin/bash
set -e

# --- System packages ---
sudo apt update
sudo apt install -y python3-venv python3-pip postgresql redis-server git curl gnupg nginx openssl

# --- TimescaleDB (not yet installed on this VPS — only plpgsql exists so far) ---
echo "deb https://packagecloud.io/timescale/timescaledb/ubuntu/ $(lsb_release -c -s) main" | sudo tee /etc/apt/sources.list.d/timescaledb.list
curl -Ls https://packagecloud.io/timescale/timescaledb/gpgkey | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/timescaledb.gpg
sudo apt update
sudo apt install -y timescaledb-2-postgresql-16
sudo timescaledb-tune --yes
sudo systemctl restart postgresql

# --- Postgres role/db ---
DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
sudo -u postgres psql -c "CREATE USER pricestream WITH PASSWORD '${DB_PASSWORD}';"
sudo -u postgres psql -c "CREATE DATABASE pricestream OWNER pricestream;"

# --- Redis: AOF persistence so a restart never loses queued-but-uncommitted ticks ---
sudo sed -i 's/^appendonly no/appendonly yes/' /etc/redis/redis.conf
sudo systemctl restart redis-server

# --- Clone the repo ---
sudo mkdir -p /opt/pricestream
sudo chown worker:worker /opt/pricestream
git clone https://github.com/janisarmunshi/pricestream /opt/pricestream
cd /opt/pricestream

# --- venv + deps ---
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# --- .env ---
cp pricestream/.env.example pricestream/.env
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(50))")
CRYPTOGRAPHY_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
sed -i "s|SECRET_KEY=change-me|SECRET_KEY=${SECRET_KEY}|" pricestream/.env
sed -i "s|CRYPTOGRAPHY_KEY=change-me-generate-with-Fernet.generate_key|CRYPTOGRAPHY_KEY=${CRYPTOGRAPHY_KEY}|" pricestream/.env
sed -i "s|DB_PASSWORD=change-me|DB_PASSWORD=${DB_PASSWORD}|" pricestream/.env
sed -i "s|DEBUG=1|DEBUG=0|" pricestream/.env
sed -i "s|FORCE_SCRIPT_NAME=|FORCE_SCRIPT_NAME=/pricestream|" pricestream/.env

echo "DB_PASSWORD (already written into .env): ${DB_PASSWORD}"

# --- Django setup ---
cd pricestream
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

### Self-signed TLS certificate

```bash
sudo mkdir -p /etc/ssl/pricestream
sudo openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
    -keyout /etc/ssl/pricestream/marketmantra.tech.key \
    -out /etc/ssl/pricestream/marketmantra.tech.crt \
    -subj "/CN=marketmantra.tech"
```

Browsers will show an untrusted-certificate warning on first visit (expected for a
self-signed cert) — click through/accept it, or install the `.crt` as a trusted
root on client machines that need to avoid the warning.

### Systemd units + nginx

```bash
#!/bin/bash
set -e

sudo cp /opt/pricestream/deploy/pricestream-gunicorn.service /etc/systemd/system/
sudo cp /opt/pricestream/deploy/pricestream-celeryd.service /etc/systemd/system/
sudo cp /opt/pricestream/deploy/pricestream-celerybeat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pricestream-gunicorn pricestream-celeryd pricestream-celerybeat
sudo systemctl status pricestream-gunicorn pricestream-celeryd pricestream-celerybeat --no-pager

# nginx: new server block for marketmantra.tech (doesn't exist yet on this VPS),
# alongside the existing marketmantra.com site
sudo cp /opt/pricestream/deploy/pricestream-nginx.conf /etc/nginx/sites-available/marketmantra-tech
sudo ln -sf /etc/nginx/sites-available/marketmantra-tech /etc/nginx/sites-enabled/marketmantra-tech
sudo nginx -t
sudo systemctl reload nginx
```

Verify: `curl -Ik https://marketmantra.tech/pricestream/login/` should return `200`
(`-k` skips certificate verification, needed for the self-signed cert).

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
