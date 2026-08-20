import hashlib
import secrets

from django.db import models

from apps.accounts.models import BrokerAccount
from apps.instruments.models import Script


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


class ApiKey(models.Model):
    """External read-access credential. Only a hash is stored — the raw key is shown
    once at creation, like a GitHub PAT — never persisted in plaintext. Scoped to
    specific broker accounts and/or instruments since this is market data that may be
    shared with more than one external party who shouldn't see each other's accounts.
    """

    label = models.CharField(max_length=80)
    key_hash = models.CharField(max_length=64, unique=True)
    key_prefix = models.CharField(max_length=12, help_text='First chars of the key, for identification in lists.')

    scoped_accounts = models.ManyToManyField(BrokerAccount, blank=True, related_name='api_keys')
    scoped_scripts = models.ManyToManyField(Script, blank=True, related_name='api_keys')

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'{self.label} ({self.key_prefix}...)'

    @staticmethod
    def generate():
        """Create a new ApiKey row and return (instance, raw_key). raw_key is only
        ever available here — callers must show it to the user immediately.
        """
        raw_key = f'ps_{secrets.token_urlsafe(32)}'
        instance = ApiKey(
            key_hash=_hash_key(raw_key),
            key_prefix=raw_key[:10],
        )
        return instance, raw_key

    @classmethod
    def authenticate(cls, raw_key: str):
        if not raw_key:
            return None
        key_hash = _hash_key(raw_key)
        return cls.objects.filter(key_hash=key_hash, is_active=True, revoked_at__isnull=True).first()

    def scoped_account_ids(self):
        """Account ids this key may read. Empty list means no accounts are scoped —
        callers must treat that as zero access, never as "all accounts".
        """
        return list(self.scoped_accounts.values_list('id', flat=True))

    def scoped_tokens(self):
        """Instrument tokens this key is restricted to, or [] if not token-restricted
        (still bounded by scoped_account_ids — an empty list here means "any token
        within the scoped accounts", not "any token at all").
        """
        return list(self.scoped_scripts.values_list('token', flat=True))
