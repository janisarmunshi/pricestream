"""
NorenApiPy — thin wrapper around NorenRestApiPy.NorenApi adding SOCKS5 proxy support
and OAuth header injection for the WebSocket auth path.

Ported from Yantra's trading/Entities/Brokers/api_helper.py, trimmed of everything
related to placing/modifying orders (place_order, place_basket, Order class) — this
project only ever reads ticks, never trades.
"""
import logging as _logging
import os
import threading as _threading
from urllib.parse import quote as _url_quote

import requests as _requests
from NorenRestApiPy.NorenApi import NorenApi
import NorenRestApiPy.NorenApi as _noren_module

_api_logger = _logging.getLogger(__name__)


class _ProxyDispatcher:
    """Thread-local proxy dispatcher installed as the module-level `requests` object
    inside NorenRestApiPy.NorenApi. Each NorenApiPy instance created with a source_ip
    activates its own proxy session for the current thread; Celery worker processes
    are isolated anyway, thread-local additionally covers multiple accounts sharing
    a process on different threads.
    """
    _local = _threading.local()

    @classmethod
    def set(cls, session):
        cls._local.session = session

    @classmethod
    def clear(cls):
        cls._local.session = None

    def post(self, *args, **kwargs):
        kwargs.setdefault('timeout', 30)
        sess = getattr(self._local, 'session', None)
        return (sess or _requests).post(*args, **kwargs)

    def get(self, *args, **kwargs):
        kwargs.setdefault('timeout', 30)
        sess = getattr(self._local, 'session', None)
        return (sess or _requests).get(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(_requests, name)


_proxy_dispatcher = _ProxyDispatcher()
_noren_module.requests = _proxy_dispatcher


class _SessionWrapper:
    def __init__(self, session):
        self._session = session

    def post(self, *args, **kwargs):
        kwargs.setdefault('timeout', 30)
        return self._session.post(*args, **kwargs)

    def get(self, *args, **kwargs):
        kwargs.setdefault('timeout', 30)
        return self._session.get(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(_requests, name)


class NorenApiPy(NorenApi):
    def __init__(self, source_ip=None):
        NorenApi.__init__(
            self,
            host='https://api.shoonya.com/NorenWClientAPI/',
            websocket='wss://api.shoonya.com/NorenWSAPI/',
        )
        self._source_ip = source_ip
        self._proxy_session = None

        if source_ip:
            session = _requests.Session()
            proxy_user = os.getenv('SOCKS_PROXY_USER', '')
            proxy_pass = os.getenv('SOCKS_PROXY_PASS', '')
            if proxy_user and proxy_pass:
                proxy_url = (
                    f'socks5h://{_url_quote(proxy_user, safe="")}:'
                    f'{_url_quote(proxy_pass, safe="")}@{source_ip}:1080'
                )
            else:
                proxy_url = f'socks5h://{source_ip}:1080'
            session.proxies = {'http': proxy_url, 'https': proxy_url}
            self._proxy_session = _SessionWrapper(session)
            _ProxyDispatcher.set(self._proxy_session)
            _api_logger.debug(f'NorenApiPy: SOCKS5 proxy activated for thread — source_ip={source_ip}:1080')
        else:
            _ProxyDispatcher.set(None)
            _api_logger.debug('NorenApiPy: direct connection (no proxy)')

    def activate(self):
        """Route this thread's NorenApi HTTP calls through this instance's proxy."""
        _ProxyDispatcher.set(self._proxy_session)

    def deactivate(self):
        _ProxyDispatcher.clear()

    def injectOAuthHeader(self, access_token, UID, AID):
        """Base NorenApi.injectOAuthHeader sets the REST Bearer header + uid/actid but not
        __access_token, which both the WebSocket auth ('a' message) and the WS URL need.
        Mirror what Finvasia's own getAccessToken does so the tick WebSocket authenticates.
        """
        headers = NorenApi.injectOAuthHeader(self, access_token, UID, AID)
        self._NorenApi__access_token = access_token
        return headers

    def verify_proxy(self) -> bool:
        """Test the SOCKS5 proxy by making an outbound request and checking the effective
        source IP. Returns True if the proxy is reachable at all (mismatch is only logged).
        """
        if not self._source_ip:
            return True
        try:
            resp = (self._proxy_session or _requests).get(
                'https://api.ipify.org?format=json', timeout=10
            )
            outgoing_ip = resp.json().get('ip', 'unknown')
            if outgoing_ip == self._source_ip:
                _api_logger.info(
                    f'[PROXY-OK] source_ip={self._source_ip} outgoing={outgoing_ip} — proxy routing correct'
                )
            else:
                _api_logger.warning(
                    f'[PROXY-MISMATCH] expected source_ip={self._source_ip} but outgoing={outgoing_ip}.'
                )
            return True
        except Exception as exc:
            _api_logger.error(
                f'[PROXY-FAIL] Cannot reach SOCKS5 proxy at {self._source_ip}:1080 — {exc!r}'
            )
            return False
