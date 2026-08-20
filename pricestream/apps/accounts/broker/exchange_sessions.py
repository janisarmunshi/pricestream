"""
Exchange market-session logic, ported as-is from Yantra's Broker.EXCHANGE_SESSIONS /
isMarketDayActive / isAnyMarketDayActive (trading/Entities/Brokers/broker.py) — the
concrete source of truth already proven in production.

Known limitation inherited from Yantra: weekday + session-time only, no exchange
holiday calendar. Not blocking for v1 (Yantra runs the same way today).
"""
from datetime import datetime, time

import pytz

IST = pytz.timezone('Asia/Kolkata')

EXCHANGE_SESSIONS = {
    'BSE': [(time(9, 15, 0), time(15, 30, 0))],
    'NSE': [(time(9, 15, 0), time(15, 30, 0))],
    'CDS': [(time(9, 0, 0), time(17, 0, 0))],
    'MCX': [(time(9, 0, 0), time(23, 55, 0))],
}


def get_market_sessions(exch_seg):
    return EXCHANGE_SESSIONS.get(exch_seg)


def is_market_open(exch_seg):
    """Single non-blocking check of whether the market is currently open."""
    sessions = get_market_sessions(exch_seg)
    if not sessions:
        return True  # No session info — assume open
    curr = datetime.now(IST)
    if curr.weekday() >= 5:
        return False
    curr_time = curr.time()
    return any(start <= curr_time <= end for start, end in sessions)


def is_market_day_active(exch_seg):
    """True while a long-running worker should stay alive *today*: a trading weekday
    whose final session has not yet closed. Unlike is_market_open() this stays True
    BEFORE the open (e.g. ~09:00, before the 09:15 NSE open) so workers launched
    pre-market wait for the open instead of exiting immediately, and flips False once
    the last session closes (or on weekends), letting the loop exit cleanly.
    """
    sessions = get_market_sessions(exch_seg)
    if not sessions:
        return True
    curr = datetime.now(IST)
    if curr.weekday() >= 5:
        return False
    last_close = max(session[1] for session in sessions)
    return curr.time() <= last_close


def is_any_market_day_active(exch_segs):
    """True if is_market_day_active() holds for any exchange in the list. Lets a
    per-account WS ingestion task stay alive until the latest close across every
    exchange that account's enabled subscriptions span (e.g. keep an MCX feed up
    until 23:55 even after NSE closed at 15:30).
    """
    return any(is_market_day_active(seg) for seg in exch_segs)
