# PriceStream — standalone tick-logging service

## Context

The user wants to passively capture live market data (price ticks) for instruments they choose to monitor, across multiple broker accounts, for later offline analysis — completely separate from the live Yantra trading platform. It needs its own VPS, its own frontend for managing broker accounts/streaming/instrument selection, and a durable store (Postgres) that can never silently drop a tick, though some logging delay is fine since nothing consumes this data live.

This is a new, standalone project at `C:\Janisar\Projects\PriceStream` — not a module inside Yantra. "Use the same broker account setup" means reuse Yantra's proven *integration pattern* (Finvasia login flow, WebSocket tick subscription) as a starting point to port and trim, not share Yantra's live database or runtime. Confirmed with the user: Django+Celery+Redis (matches Yantra, and the existing Finvasia login module can be ported directly), Finvasia-only for v1, and PostgreSQL + TimescaleDB (tick data is a textbook time-series workload).

## What already exists in Yantra worth reusing (verified this session)

- `yantra/trading/Entities/Brokers/bnrathi.py` — the Finvasia (Shoonya/NorenApi) connector. Contains the **login flow** (headless Selenium + TOTP via `pyotp`, Redis-locked to serialize concurrent logins, ~up to 120s, proxy-suspect detection) and the **WebSocket layer**: `subscribeWebSockets(orderCallback, lstTicks)` opens one WS per account via `NorenApi.start_websocket(...)`, subscribing to `["EXCH|token", ...]` pairs; tick messages arrive via `event_handler_Tick_update(message)` with at least `lp` (last price) confirmed non-zero-checked in code — exact full payload shape (volume, bid/ask, feed-time) needs a quick live capture during implementation, not fully visible from static code. Also has the reconnect/health-check pattern: `resubscribeWebSockets()`, `isWsConnected()`, `wsIdleSeconds()`, and a Redis lock per account (`WS{accountId}`) so only one process owns a given account's feed.
- `yantra/trading/Entities/BrokerAccounts/brokerAccount.py` — the account/session wrapper `Connect()` calls into a broker's login.
- `yantra/trading/models.py` `BrokerAccounts` and `Scripts` models — credential fields and instrument-master shape (token, symbol, exchSeg, lotSize, tickSize, expiry) to port.
- Yantra's deploy runbook (manual `plink`/`pscp`, no git, force-restart sequence for celeryd, gunicorn restart after model changes) — reuse the same approach for consistency with what the user already operates day to day, unless they'd rather use git for this fresh project (worth asking once code work starts, not blocking the plan).

None of the order-placement/trading logic (`submitOrder`, strategy engines, order callbacks) is needed — PriceStream only ever reads ticks, never trades.

## Architecture

```
Broker (Finvasia)  --WS ticks-->  Celery worker (1 per active account)
                                        |
                                   Redis Stream (durable, AOF-persisted)
                                        |
                                  Committer task (batched insert)
                                        |
                              PostgreSQL + TimescaleDB hypertable
```

**Zero-data-loss design.** The WS callback never talks to Postgres directly — it only does one fast, durable local operation: `XADD` the tick onto a Redis Stream (one stream, or one per account). Redis persistence must be enabled (`appendonly yes`, `appendfsync everysec`) so a Redis restart doesn't lose queued-but-uncommitted ticks. A separate committer Celery task consumes the stream via a consumer group, batches entries (by count or a short time window — "delayed logging is fine" is exactly what makes batching safe), and does one bulk insert per batch into the Timescale hypertable. Only after a **confirmed DB commit** does it `XACK`+trim those entries. If Postgres is down, the stream just keeps growing (bounded by Redis memory/disk, monitored) and the committer catches up once the DB is back — no gap, only added latency, which matches the stated tolerance. This gives at-least-once delivery that survives a worker crash, a Redis restart (with AOF), or a DB outage.

**Why TimescaleDB specifically helps here:** automatic time-based partitioning (hypertable chunks) keeps inserts and range-queries fast as data grows across months, native compression for older chunks (ticks compress extremely well), and retention policies can auto-drop/compress data past a configurable age — all close to free instead of hand-rolled Postgres partitioning.

### Evaluated against the user's proposed diagram (ring buffer → Redis Stream → worker pool → partitioned Postgres, with ACK-after-commit, batching-with-timeout, DLQ, graceful shutdown)

Most of it matches this plan exactly and is adopted as-is:
- ✅ **Redis Stream as the durable handoff, worker pool batch insert, ACK only after commit** — this is the core mechanism already designed above.
- ✅ **Batching with timeout (N ticks OR T seconds)** — already the committer's design; "delayed logging is fine" is what makes this safe.
- ✅ **Partitioned PostgreSQL** — already satisfied by the earlier TimescaleDB decision; a hypertable *is* automatic time-based partitioning, so no separate hand-rolled partitioning scheme is needed on top.
- ➕ **Dead Letter Queue — adopted, was missing from the original plan.** A batch entry that fails to insert even after retries (bad/malformed payload, constraint violation) must not block everything queued behind it and must not silently vanish either. On repeated failure, move that entry to a DLQ (a second Redis stream, or a small `failed_ticks` table) and `XACK` it off the main stream so the pipeline keeps flowing; surface DLQ depth so it gets looked at rather than growing forever unnoticed.
- ➕ **Graceful shutdown (drain on SIGTERM) — adopted**, but scoped correctly: this reduces the un-persisted window during *planned* restarts/deploys, it is not itself the zero-loss guarantee — that guarantee has to come from durability at write time (see next point), because a SIGTERM handler never runs on a `kill -9`, an OOM-kill, or a power loss.
- ⚠️ **In-memory ring buffer as the first stage, before Redis — reconsidered, not adopted as described.** Anything that lives only in process memory is gone on a hard crash, which is exactly the failure mode "zero data loss" needs to survive. Writing straight to Redis `XADD` on every tick is already sub-millisecond, so there's little practical need for a memory-resident holding stage in front of it — and having one adds a real, if narrow, loss window that the rest of the design otherwise closes. If the actual goal was throughput (avoid one Redis round-trip per single tick), the right way to get that without reopening the loss window is **pipelining a tightly bounded batch of `XADD` calls** (flush every ~50 ticks or ~100ms, whichever first) — same throughput benefit, but the exposure window is bounded to a fraction of a second instead of "however long the buffer holds it," and it's still Redis (AOF-backed) that's the actual durability boundary, not application memory.

## WebSocket resilience — detecting and recovering from a mid-stream death

Yantra already has a proven version of this (see `project_ws_health_check_fix`): `bnrathi.py` tracks a `_wsConnected` flag and `_lastWsActivityAt` timestamp updated on every event, exposes `isWsConnected()`/`wsIdleSeconds()`, and `resubscribeWebSockets()` force-closes and reopens the socket, resubscribing the same instrument list — this specifically catches the "half-open TCP, no close frame ever received" case where the socket looks fine but is actually dead. Port this directly; the trimmed bnrathi.py copy for PriceStream keeps this pattern.

One deliberate difference from Yantra's precedent: Yantra's own note is that the trigger must be the connection-state flag, **not** silence, because in Yantra's order-callback use ticks aren't always flowing (no news is often normal news). PriceStream's whole job *is* continuous ticks, so for subscribed, normally-liquid instruments, per-instrument tick silence past its typical inter-tick interval is itself a meaningful, additional signal here — not a replacement for the connection-flag check, but a second layer on top of it, since it can catch a socket that looks "connected" but has silently stopped receiving *this account's* subscriptions specifically (e.g. a server-side resubscribe that silently dropped a subset of tokens).

Recovery loop: a Celery-beat health-check task every ~5s (matching Yantra's proven interval) iterates accounts that should currently be streaming and, for each: if `isWsConnected()` is False, or idle time exceeds threshold, or a subscribed instrument has gone quiet beyond its expected interval — call `resubscribeWebSockets()`. Unlike Yantra's reconnect (which just replays whatever instrument list was passed at the last connect), PriceStream's reconnect must **re-read the current enabled `Subscription` list from the DB** before resubscribing, since the whole point of the frontend is that a user can add/remove instruments for an account at any time — a stale in-memory list would silently under- or over-subscribe after a reconnect.

## Auto-start on market hours (NSE/BSE vs MCX)

Reuse Yantra's exchange-session model and logic exactly, not just the concept:
- `Broker.EXCHANGE_SESSIONS` (broker.py) is the concrete source of truth already in production: NSE/BSE 09:15–15:30, CDS 09:00–17:00, **MCX 09:00–23:55** — the same account can easily be logging both an NSE equity and an MCX bullion contract at once, with very different close times.
- `isMarketDayActive(exchSeg)` / `isAnyMarketDayActive([exchSegs])` — weekday check plus "true until the *last* session of the day closes," so a worker started pre-market waits rather than exiting, and one covering multiple exchanges stays alive until the latest of them closes (their own comment: keep an MCX feed up until 23:55 even after NSE closed at 15:30). Port this logic (and the `MarketSessions` DB model shape) as-is.

One genuine improvement over Yantra's current behavior rather than a straight port: **Yantra's WS publisher only starts because something calls `startStrategyForUser`/`workerStartStrategyForAll` — today that's a manual trigger** (confirmed this session — there is no beat-scheduled auto-start in production, `beat_schedule` in `celery.py` is empty and everything observed this session was manually kicked off). PriceStream should be genuinely autonomous, since the user explicitly asked for auto-start: a beat task every 1–2 minutes checks every account with at least one enabled `Subscription`; if `isAnyMarketDayActive()` is true for the set of exchanges that account's enabled subscriptions span, and that account's WS ingestion task isn't already running (same `WS{accountId}` Redis-lock pattern Yantra uses to guarantee single ownership), start it — idempotent, so running the check every minute is harmless. When none of an account's subscribed exchanges are still active for the day, let its WS task exit its own loop naturally (same pattern Yantra already uses), no force-kill needed.

Known limitation inherited from the pattern, worth flagging rather than silently accepting: neither Yantra nor this design accounts for exchange holidays, only weekday + session-time. A `MarketHoliday` table could be added later to skip specific dates; not blocking for v1 since Yantra runs the same way today.

## Django app breakdown

- **`accounts`** — `BrokerAccount` model (credentials incl. TOTP secret, encrypted at rest via a Fernet field), login/session status, a "test login" / "force relogin" admin action porting bnrathi.py's Selenium+TOTP flow (trimmed of order-placement code).
- **`instruments`** — `Script` model (token/symbol/exchSeg/lotSize/tickSize/expiry), synced from the broker's own symbol-master download on a daily Celery-beat schedule (independent of Yantra — PriceStream must be fully self-contained on its own VPS).
- **`streaming`** — `Subscription` model (account × instrument, enabled flag) = what a user picks in the frontend; `StreamingSetting` per account (on/off, reconnect policy); the long-lived Celery WS-ingestion task (one per active account, mirroring Yantra's `workerAccountWebSocket` pattern) plus the same 5-second health-check + auto-resubscribe logic already proven in Yantra (see `project_ws_health_check_fix` precedent).
- **`ticks`** — the TimescaleDB hypertable model (timestamp, account_id, exchange, token, symbol, ltp, + whatever else the live payload capture reveals) and the batch-committer Celery task described above. Also owns a small `SystemEvent` log (connection drops, resubscribes, DLQ entries, lag-threshold breaches) and periodic `StreamMetrics` snapshots (per-account tick rate, lag, Redis stream/DLQ depth, storage size) — cheap to populate piggybacking on the health-check task that's already running every ~5s, rather than standing up separate metrics infrastructure.
- **`api`** — the authenticated external-facing read API described below.

### Frontend

Django templates, matching Yantra's existing style rather than introducing a new stack. Seven screens, each mapped to what's already planned above rather than needing new architecture:

| Screen | Covers | Backed by |
|---|---|---|
| **Dashboard** | Live connection status, tick rate, lag, storage usage | `StreamMetrics` snapshots (above) |
| **Broker Accounts** | Add/edit/delete, test connection, refresh token | `accounts` app; "refresh token" = the same force-relogin action as "test connection" |
| **Instrument Manager** | Search/select per account, bulk CSV import, activate/deactivate | `instruments` app `Script` search + `streaming.Subscription`; CSV import is a bulk-create endpoint over the same `Subscription` model, not a separate path |
| **Streaming Control** | Start/stop/**pause** per account, active subscriptions | `streaming` app. Pause is a distinct state from deactivating a subscription — it stops the WS task without deleting what it was subscribed to, so resuming doesn't require re-selecting instruments |
| **Data Explorer** | Query historical ticks, export CSV/Excel, basic charting | Reuses the *same* internal ticks-query layer the external API's `GET /api/v1/ticks/` calls, so there's one query implementation, not two |
| **Settings** | Batch size, flush interval, retention policy, alert thresholds | A small `StreamingConfig` model (batch size/flush interval feed the committer task directly); retention/compression maps onto TimescaleDB's own `add_retention_policy`/`add_compression_policy` rather than a hand-rolled scheme |
| **Logs & Alerts** | Error logs, connection drops, lag warnings | `SystemEvent` log (above). v1 default: in-app only: no external notification channel (Telegram/email/etc.) unless wanted — flagging as an easy add-on later given Yantra already has a working `Telegram` integration to port if so, not a redesign |

## External data API

An external system needs read access to the logged ticks, so this is a proper authenticated API, not an afterthought:

- **Framework:** Django REST Framework — same library Yantra already uses (`trading/serializers.py`), so the pattern is familiar rather than a new one to learn.
- **Auth:** API-key based, not session/JWT login — external consumers are service accounts, not interactive users. A dedicated `ApiKey` model (owner label, hashed key — never store it plaintext, only a hash + a one-time reveal at creation like GitHub PATs, active/revoked flag, created/last-used timestamps, per-key **scope**: which broker accounts and/or instruments that key is allowed to read) checked via a custom DRF `authentication.BaseAuthentication` class reading a header (e.g. `Authorization: Api-Key <key>`). Scoping matters here specifically because this is market data that may be shared with more than one external party who shouldn't necessarily see each other's accounts/instruments.
- **Rate limiting:** DRF throttling classes keyed per API key, not per IP — protects the DB from a large historical pull hammering the hypertable, and gives each external consumer a predictable quota.
- **Endpoints (read-only):**
  - `GET /api/v1/instruments/` — instruments available to that key's scope, so an external system can discover what it can query before pulling data.
  - `GET /api/v1/ticks/` — the core query: filter by instrument (token or symbol), account, and a time range; **cursor-based pagination** (not offset-based — offset pagination degrades badly on large time-series tables) since a single query could span millions of rows.
  - `GET /api/v1/ticks/latest/` — most recent tick per instrument, for a cheap "is this still updating" check without pulling a range.
  - For genuinely large historical exports, a bulk/CSV export endpoint (or a documented recommendation to page through in large time chunks) is worth adding once real export sizes are known — flagging as a v1.1 concern rather than blocking the initial design.

## Deployment topology (new, separate VPS)

- `gunicorn` — the admin/frontend web app.
- `celeryd` — worker pool sized for N long-lived WS-ingestion tasks (one per active broker account) + the committer task(s). Reuse the same force-restart lessons already learned operating Yantra's celeryd (graceful stop hangs on long-lived tasks; force-kill + reset-failed + direct init script start is the known-working sequence).
- `celerybeat` — daily Script-master sync; the ~5s WS health-check/auto-resubscribe task; the 1–2 min market-hours auto-start/stop supervisor; stream/DLQ depth monitoring.
- PostgreSQL + TimescaleDB extension, Redis (AOF-enabled) — same VPS to start for simplicity, can be split to a separate DB host later without architecture changes.

## Explicitly out of scope for v1

Live trading, order placement, the Secondary hedge concept, any migration of existing Yantra data, multi-broker abstraction (Angel etc.), and any real-time consumption/alerting on the logged ticks — this is pure capture-for-later-use, per the user's own framing.

## Open items to confirm once implementation starts (not blocking this plan)

- Exact tick payload fields beyond `lp` — needs one live WS capture against a real subscribed instrument.
- Whether to reuse Yantra's manual pscp/no-git deploy style for this fresh project, or use git this time.
- Credential encryption approach (Django Fernet-encrypted field vs. OS-level secret store) — default to Fernet field, matching complexity level of the rest of the stack.

## Verification approach (once built)

- Subscribe a couple of live/liquid instruments, confirm ticks land in the hypertable within the expected delay.
- Kill -9 the committer mid-batch; confirm the un-acked stream entries are replayed and land exactly once (no dup, no gap) on restart.
- Stop Postgres for a few minutes while ticks keep arriving; confirm the stream absorbs them and the committer fully catches up once Postgres returns, with no gap in the per-account tick sequence.
- Restart Redis (with AOF on) mid-stream; confirm no queued-but-unacked ticks are lost.
- Kill the WS process mid-session (simulating a dead socket) and confirm the health-check task detects it (via flag or per-instrument silence) and resubscribes within one check interval, with the *current* DB subscription list, not a stale one.
- Add/remove an instrument for a live-streaming account via the frontend and confirm the next reconnect picks up the change without a manual restart.
- Confirm a market-hours-only account's WS task starts itself around session open with no manual trigger, and exits cleanly after the latest of its subscribed exchanges' close (e.g. an account streaming both an NSE and an MCX instrument keeps the feed up until MCX's 23:55, not NSE's 15:30).
- Feed one deliberately malformed tick and confirm it lands in the DLQ (not lost, not blocking the batch behind it) and is visible for inspection.
