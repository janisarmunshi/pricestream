#!/bin/bash
# Force-restart sequence for celeryd, reusing the lesson already learned operating
# Yantra: a graceful stop hangs on long-lived tasks (the WS ingestion loop blocks on
# a 0.5s poll, not something SIGTERM interrupts instantly), so force-kill + reset the
# "reserved" task state + direct start is the known-working sequence rather than
# `systemctl restart` alone.
set -euo pipefail

echo "Force-stopping celeryd..."
sudo systemctl stop pricestream-celeryd || true
sleep 2
pkill -9 -f "celery -A config worker" || true
sleep 1

echo "Starting celeryd..."
sudo systemctl start pricestream-celeryd
sleep 2
sudo systemctl status pricestream-celeryd --no-pager
