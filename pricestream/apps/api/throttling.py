from rest_framework.throttling import SimpleRateThrottle


class ApiKeyRateThrottle(SimpleRateThrottle):
    """Throttle per API key, not per IP — protects the hypertable from a single
    consumer's large historical pull, and gives each external consumer a
    predictable quota independent of what network they call from.
    """
    scope = 'api_key'

    def get_cache_key(self, request, view):
        auth = getattr(request, 'auth', None)
        if auth is None:
            return None  # unauthenticated requests are rejected by permissions, not throttled here
        return self.cache_format % {'scope': self.scope, 'ident': auth.key_hash}
