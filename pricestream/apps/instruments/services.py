"""Daily instrument-master sync from Finvasia's public symbol-master download.

Finvasia publishes one gzipped CSV per exchange segment at a stable URL
(https://api.shoonya.com/{SEGMENT}_symbols.txt.zip). This is independent of any
broker account login — PriceStream must be fully self-contained on its own VPS.
"""
import csv
import io
import logging
import zipfile
from datetime import datetime

import requests
from django.utils import timezone

from apps.instruments.models import Script

logger = logging.getLogger(__name__)

SYMBOL_MASTER_URLS = {
    'NSE': 'https://api.shoonya.com/NSE_symbols.txt.zip',
    'BSE': 'https://api.shoonya.com/BSE_symbols.txt.zip',
    'MCX': 'https://api.shoonya.com/MCX_symbols.txt.zip',
    'CDS': 'https://api.shoonya.com/CDS_symbols.txt.zip',
    'NFO': 'https://api.shoonya.com/NFO_symbols.txt.zip',
    'BFO': 'https://api.shoonya.com/BFO_symbols.txt.zip',
}


def _parse_expiry_date(raw):
    for fmt in ('%d-%b-%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(raw, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def sync_exchange(exch_seg: str) -> int:
    """Download and upsert the symbol master for one exchange segment.
    Returns the number of Script rows created/updated.
    """
    url = SYMBOL_MASTER_URLS.get(exch_seg)
    if not url:
        logger.error(f'[SCRIPT-SYNC] no symbol-master URL configured for {exch_seg}')
        return 0

    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_name = zf.namelist()[0]
        with zf.open(csv_name) as f:
            text = io.TextIOWrapper(f, encoding='utf-8')
            reader = csv.DictReader(text)
            count = 0
            for row in reader:
                token = row.get('Token') or row.get('token')
                symbol = row.get('Symbol') or row.get('symbol')
                if not token or not symbol:
                    continue

                trading_symbol = row.get('TradingSymbol') or symbol
                Script.objects.update_or_create(
                    exch_seg=exch_seg, token=token,
                    defaults={
                        'symbol': symbol,
                        'symbol_finvasia': trading_symbol,
                        'name': row.get('Instrument') or row.get('Name') or '',
                        'expiry': row.get('Expiry') or '',
                        'expiry_date': _parse_expiry_date(row.get('Expiry')),
                        'strike': row.get('StrikePrice') or 0,
                        'option_type': row.get('OptionType') or '',
                        'lot_size': int(row.get('LotSize') or 1),
                        'tick_size': row.get('TickSize') or 0,
                        'instrument_type': row.get('Instrument') or '',
                    },
                )
                count += 1
    logger.info(f'[SCRIPT-SYNC] {exch_seg}: upserted {count} scripts')
    return count


def delete_expired_scripts() -> int:
    """Remove scripts whose expiry has already passed. Ported from Yantra's
    MasterDataManager.syncSymbols(), which runs this both before and after the sync
    so a stale expired contract is never left subscribable and a freshly-synced one
    that turns out already-expired doesn't linger either.
    """
    today = timezone.now().date()
    deleted_count, _ = Script.objects.filter(expiry_date__lt=today).delete()
    if deleted_count:
        logger.info(f'[SCRIPT-SYNC] deleted {deleted_count} expired scripts')
    return deleted_count


def sync_all_exchanges():
    delete_expired_scripts()
    total = 0
    for exch_seg in SYMBOL_MASTER_URLS:
        try:
            total += sync_exchange(exch_seg)
        except Exception as e:
            logger.error(f'[SCRIPT-SYNC] {exch_seg} sync failed: {e}', exc_info=True)
    delete_expired_scripts()
    return total
