from django.conf import settings
from django.db import models
from django_cryptography.fields import encrypt


class BrokerAccount(models.Model):
    """A single Finvasia (Shoonya/NorenApi) login the user wants to stream ticks for.

    Credential fields are Fernet-encrypted at rest (django-cryptography) rather than
    plaintext, matching Yantra's BrokerAccounts shape but trimmed of anything only
    needed for order placement.
    """

    BROKER_FINVASIA = 'FINVASIA'
    BROKER_CHOICES = [
        (BROKER_FINVASIA, 'Finvasia (Shoonya)'),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='broker_accounts',
    )
    broker = models.CharField(max_length=20, choices=BROKER_CHOICES, default=BROKER_FINVASIA)
    nickname = models.CharField(max_length=80)

    client_id = models.CharField(max_length=40)
    password = encrypt(models.CharField(max_length=128))
    totp_secret = encrypt(models.CharField(max_length=128, blank=True, default=''))
    api_key = encrypt(models.CharField(max_length=128, blank=True, default=''))
    vendor_code = models.CharField(max_length=40, blank=True, default='')

    access_token = encrypt(models.CharField(max_length=256, blank=True, default=''))
    source_ip = models.GenericIPAddressField(null=True, blank=True, default=None)

    is_active = models.BooleanField(default=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    last_login_status = models.CharField(max_length=20, blank=True, default='')
    last_login_error = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('owner', 'client_id', 'broker')]
        ordering = ['nickname']

    def __str__(self):
        return f'{self.nickname} ({self.client_id})'
