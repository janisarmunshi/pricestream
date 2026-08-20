from django.utils import timezone
from rest_framework import authentication, exceptions

from apps.api.models import ApiKey


class ApiKeyAuthentication(authentication.BaseAuthentication):
    """Authorization: Api-Key <key> — external consumers are service accounts, not
    interactive users, so this is key-based rather than session/JWT login.
    """

    keyword = 'Api-Key'

    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth_header.startswith(f'{self.keyword} '):
            return None

        raw_key = auth_header[len(self.keyword) + 1:].strip()
        api_key = ApiKey.authenticate(raw_key)
        if api_key is None:
            raise exceptions.AuthenticationFailed('Invalid or revoked API key.')

        api_key.last_used_at = timezone.now()
        api_key.save(update_fields=['last_used_at'])

        return (_ApiKeyUser(api_key), api_key)


class _ApiKeyUser:
    """Minimal user-like object DRF permission classes can check is_authenticated on,
    carrying the ApiKey so views can read its scope.
    """
    is_authenticated = True

    def __init__(self, api_key: ApiKey):
        self.api_key = api_key

    def __str__(self):
        return f'ApiKeyUser({self.api_key.label})'
