from django.db import models


class Script(models.Model):
    """Instrument master, ported from Yantra's Scripts model shape (token/symbol/
    exchSeg/lotSize/tickSize/expiry), synced from Finvasia's own daily symbol-master
    download rather than shared with Yantra's live database.
    """

    token = models.CharField(max_length=40, db_index=True)
    # The full tradable contract code (e.g. SILVERMIC31AUG26), not the base name
    # (SILVERMIC) — this is what the broker's WS subscribe and every other lookup
    # actually needs. PriceStream is Finvasia-only, so there is no separate
    # per-broker symbol field to reconcile.
    symbol = models.CharField(max_length=40, db_index=True)
    name = models.CharField(max_length=80, default='')
    exch_seg = models.CharField(max_length=10, db_index=True)

    expiry = models.CharField(max_length=40, null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)
    strike = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    option_type = models.CharField(max_length=2, blank=True, default='')

    lot_size = models.IntegerField(default=1)
    tick_size = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    instrument_type = models.CharField(max_length=24, blank=True, default='')

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['exch_seg', 'symbol']),
            models.Index(fields=['exch_seg', 'token']),
        ]
        unique_together = [('exch_seg', 'token')]

    def __str__(self):
        return f'{self.symbol}-{self.exch_seg}-{self.token}'
