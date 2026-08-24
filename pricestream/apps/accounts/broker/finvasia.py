"""
Finvasia (Shoonya/NorenApi) connector — login flow + WebSocket tick subscription,
ported from Yantra's trading/Entities/Brokers/bnrathi.py and trimmed of everything
related to orders (placing/modifying/cancelling, order book, trade book, positions,
holdings). PriceStream only ever reads ticks.

Kept as-is because they're proven in production:
- Headless Selenium + TOTP OAuth login, Redis-locked to serialize concurrent logins
  for the same account (up to ~120s), with proxy-suspect detection.
- The WS resilience pattern: _wsConnected flag + _lastWsActivityAt timestamp,
  isWsConnected() / wsIdleSeconds() / resubscribeWebSockets() — this specifically
  catches a half-open TCP connection (dead socket, no close frame ever received)
  that close_callback alone would never see.
"""
import logging
import platform
import shutil
import tempfile
import time
from urllib.parse import parse_qs, urlparse

import pyotp
import redis

from django.conf import settings

from apps.accounts.broker.api_helper import NorenApiPy

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == 'Windows'


def _get_chrome_service():
    """Return None so Selenium Manager (built into Selenium 4.6+) resolves the
    chromedriver binary itself, on every platform.

    webdriver_manager was tried here first but is unreliable against modern Chrome
    (115+, which moved to the Chrome-for-Testing distribution channel) — it can
    resolve/cache a chromedriver version that doesn't match the installed Chrome,
    which fails at launch with "Chrome failed to start ... DevToolsActivePort file
    doesn't exist", a mismatch error rather than a real crash. Selenium Manager reads
    the actually-installed Chrome's version and fetches the matching driver, which is
    both simpler and correct.
    """
    return None


def _build_chrome_options(profile_dir: str):
    from selenium import webdriver as _wd
    opts = _wd.ChromeOptions()
    opts.add_argument('--headless=new')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--disable-software-rasterizer')
    opts.add_argument('--window-size=1920,1080')
    opts.add_argument('--remote-debugging-port=0')
    opts.add_argument(f'--user-data-dir={profile_dir}')
    opts.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
    if not _IS_WINDOWS:
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage')
        opts.add_argument('--no-zygote')
    return opts


def _redis_client():
    return redis.Redis(
        host=settings.REDIS_HOST, port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD or None, decode_responses=True,
    )


class FinvasiaConnector:
    """One instance per BrokerAccount. Owns login/session state and the WebSocket
    tick subscription for that account.
    """

    def __init__(self, account):
        self.Account = account
        self.ConnectionObject = None
        self.ws = None
        self._wsConnected = False
        self._lastWsActivityAt = 0.0
        self._lastLstTicks = []
        self._tick_callback = None
        self.Errors = None

    # ------------------------------------------------------------------ login

    def _get_auth_code(self):
        """Use Selenium headless Chrome to obtain the OAuth auth code."""
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

        login_url = (
            f'https://api.shoonya.com/OAuthlogin/authorize/oauth?client_id={self.Account.vendor_code}'
        )
        profile_dir = tempfile.mkdtemp(prefix=f'chrome-acct{self.Account.id}-')
        options = _build_chrome_options(profile_dir)
        service = _get_chrome_service()
        driver = (
            webdriver.Chrome(service=service, options=options)
            if service else
            webdriver.Chrome(options=options)
        )
        wait = WebDriverWait(driver, 30)

        try:
            driver.get(login_url)
            wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password']")))
            time.sleep(1)

            all_inputs = driver.find_elements(
                By.CSS_SELECTOR,
                "input:not([type='hidden']):not([type='checkbox']):not([type='radio'])",
            )
            visible_inputs = [inp for inp in all_inputs if inp.is_displayed()]

            def fast_fill(element, value):
                element.click()
                time.sleep(0.1)
                element.clear()
                element.send_keys(value)
                time.sleep(0.1)

            fast_fill(visible_inputs[0], self.Account.client_id)
            fast_fill(visible_inputs[1], self.Account.password)

            otp_value = pyotp.TOTP(self.Account.totp_secret).now()
            fast_fill(visible_inputs[2], otp_value)

            wait.until(
                EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='LOGIN']"))
            ).click()
            logger.debug(f'OAuth credentials submitted for account {self.Account.id}, waiting for auth code...')

            start = time.time()
            while True:
                try:
                    auth_code = parse_qs(urlparse(driver.current_url).query)['code'][0]
                    if auth_code:
                        logger.debug(f'OAuth auth code captured for account {self.Account.id}.')
                        return auth_code
                except (KeyError, IndexError):
                    pass

                elapsed = time.time() - start
                if elapsed > 120:
                    new_otp = pyotp.TOTP(self.Account.totp_secret).now()
                    if new_otp != otp_value:
                        fast_fill(visible_inputs[2], new_otp)
                        wait.until(
                            EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='LOGIN']"))
                        ).click()
                        start = time.time()
                        otp_value = new_otp
                        continue
                    logger.error(f'OAuth auth code capture timed out for account {self.Account.id}.')
                    break

                time.sleep(0.5)

        except (InvalidSessionIdException, WebDriverException) as e:
            logger.error(f'Browser error during OAuth for account {self.Account.id}: {e}')
        except Exception as e:
            logger.error(f'Error capturing OAuth auth code for account {self.Account.id}: {e}', exc_info=True)
        finally:
            try:
                driver.quit()
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)

        return None

    def _validate_token(self, api, label='') -> bool:
        try:
            quote = api.get_quotes('NSE', '2885')
            if quote is not None:
                return True
            logger.warning(
                f'[TOKEN-CHECK]{label} account={self.Account.id} get_quotes returned None '
                f'(token likely expired or broker rejected the request)'
            )
            return False
        except Exception as exc:
            logger.warning(
                f'[TOKEN-CHECK]{label} account={self.Account.id} get_quotes raised {exc!r} '
                f"— source_ip={self.Account.source_ip or 'direct'}"
            )
            return False

    def get_connection_object(self, session_only=False):
        """Return a connected NorenApiPy for this account, logging in via Selenium+TOTP
        if no valid stored access token exists. Redis-locked per account so two workers
        never run Selenium for the same account at once.

        session_only=True skips the Selenium re-auth fallback (raises instead of
        blocking a background task on a ~120s login) but still validates the stored
        token with one cheap get_quotes round-trip — skipping that check entirely
        previously let an expired token straight through into a WebSocket connection,
        where the broker's auth rejection triggered an unbounded reconnect loop (see
        _MAX_CONSECUTIVE_AUTH_FAILURES) instead of failing fast here with a clear error.
        """
        error = None
        try:
            source_ip = self.Account.source_ip or None

            if self.Account.access_token:
                api = NorenApiPy(source_ip=source_ip)
                if source_ip and not api.verify_proxy():
                    logger.error(
                        f'[CONN-FAIL] account={self.Account.id} proxy check failed for '
                        f'source_ip={source_ip} — cannot establish connection'
                    )
                    return None
                api.injectOAuthHeader(self.Account.access_token, self.Account.client_id, self.Account.client_id)
                if self._validate_token(api, label=' [stored-token]'):
                    self.ConnectionObject = api
                    return api
                logger.info(f'[CONN] account={self.Account.id} stored token invalid, re-authenticating')

            if session_only:
                raise Exception(
                    f'No valid session token for account {self.Account.id}. '
                    'Use the login task to authenticate first.'
                )

            r = _redis_client()
            lock_key = f'ps:oauth_lock:{self.Account.id}'
            lock_ttl = 200

            while not r.set(lock_key, '1', nx=True, ex=lock_ttl):
                logger.debug(f'OAuth lock held by another worker for account {self.Account.id}, waiting...')
                time.sleep(3)
                self.Account.refresh_from_db()
                if self.Account.access_token:
                    api = NorenApiPy(source_ip=source_ip)
                    api.injectOAuthHeader(self.Account.access_token, self.Account.client_id, self.Account.client_id)
                    if self._validate_token(api, label=' [lock-wait]'):
                        self.ConnectionObject = api
                        return api
                    logger.warning(
                        f'[CONN] account={self.Account.id} token from DB failed validation '
                        f'while waiting for OAuth lock — will retry'
                    )

            try:
                _probe = NorenApiPy(source_ip=source_ip)
                if source_ip and not _probe.verify_proxy():
                    raise Exception(
                        f'SOCKS5 proxy at {source_ip}:1080 is unreachable — '
                        f'cannot authenticate account {self.Account.id}'
                    )

                auth_code = self._get_auth_code()
                if auth_code is None:
                    raise Exception('Failed to obtain OAuth auth code.')

                oauth_api = NorenApiPy(source_ip=source_ip)
                result = oauth_api.getAccessToken(
                    auth_code,
                    self.Account.api_key,
                    self.Account.vendor_code,
                    self.Account.client_id,
                )
                if result is None:
                    raise Exception('getAccessToken returned None - check logs for the server error response.')

                acc_tok, usrid, _, actid = result
                self.Account.access_token = acc_tok
                self.Account.save(update_fields=['access_token'])
                logger.debug(f'New access token obtained and saved for account {self.Account.id}.')

                api = NorenApiPy(source_ip=source_ip)
                api.injectOAuthHeader(acc_tok, usrid, actid)

                self.ConnectionObject = api
                return api
            finally:
                r.delete(lock_key)

        except Exception as e:
            logger.error(f'Error getting connection for account {self.Account.id}: {e}', exc_info=True)
            error = {'status': 400, 'message': str(e)}

        self.Errors = error
        return self.ConnectionObject

    # ------------------------------------------------------------- WebSocket

    # Consecutive auth-rejected reconnects (broker sends {'t':'ak','s':'NOT_OK'}) to
    # tolerate before giving up entirely. The vendored NorenApi retries
    # run_forever() internally with only a fixed ~0.1s sleep and NO backoff, so an
    # expired/invalid token previously spun in a tight reconnect loop hammering the
    # broker's server hundreds of times per second — this caps that instead of
    # leaving it to run unbounded.
    _MAX_CONSECUTIVE_AUTH_FAILURES = 5

    def _open_websocket_session(self, subscriptions, tick_callback):
        """Open the broker WebSocket and wire up its callbacks.

        subscriptions: list of {'exchange': ..., 'token': ...} dicts — the CURRENT
        enabled Subscription rows for this account, re-read by the caller before every
        (re)subscribe so a reconnect never resubscribes a stale instrument list.
        tick_callback(message: dict): called for every tick with lp > 0.
        """
        self._lastLstTicks = subscriptions
        self._tick_callback = tick_callback
        self._consecutive_auth_failures = 0
        self._auth_failed_permanently = False

        def event_handler_tick_update(message):
            self._wsConnected = True
            self._lastWsActivityAt = time.time()
            self._consecutive_auth_failures = 0
            if float(message.get('lp', 0)) > 0:
                self._tick_callback(message)

        def open_callback():
            lst_subscribe = [f"{s['exchange']}|{s['token']}" for s in subscriptions]
            if lst_subscribe:
                self.ConnectionObject.subscribe(lst_subscribe)
            self._wsConnected = True
            self._lastWsActivityAt = time.time()
            logger.info(f'[WS-OPEN] websocket open for account={self.Account.id}')

        def close_callback():
            # The underlying NorenApiPy retries run_forever() internally on a clean
            # close, but a half-open TCP connection (dead socket, no close frame ever
            # received) can hang there forever without this ever firing — that case is
            # caught by wsIdleSeconds() in the health check instead.
            self._wsConnected = False
            logger.warning(f'[WS-DISCONNECT] websocket disconnected for account={self.Account.id}')

        def error_callback(error):
            self._wsConnected = False
            logger.warning(f'[WS-ERROR] websocket error for account={self.Account.id}: {error}')

            is_auth_rejection = isinstance(error, dict) and error.get('t') == 'ak' and error.get('s') == 'NOT_OK'
            if is_auth_rejection:
                self._consecutive_auth_failures += 1
                if self._consecutive_auth_failures >= self._MAX_CONSECUTIVE_AUTH_FAILURES:
                    self._auth_failed_permanently = True
                    logger.error(
                        f'[WS-AUTH-FAIL] account={self.Account.id} rejected auth '
                        f'{self._consecutive_auth_failures} times in a row (likely an expired/'
                        f'invalid access token) — forcing the socket closed instead of retrying '
                        f'unbounded against the broker'
                    )
                    try:
                        self.ConnectionObject.close_websocket()
                    except Exception:
                        pass

        self.ws = self.ConnectionObject.start_websocket(
            subscribe_callback=event_handler_tick_update,
            socket_open_callback=open_callback,
            socket_close_callback=close_callback,
            socket_error_callback=error_callback,
        )
        logger.debug(f'tick subscribed for account={self.Account.id}')

    def subscribe_websocket(self, subscriptions, tick_callback=None):
        """Acquire the per-account Redis lock and open the WebSocket. Returns False
        without opening anything if another process already owns this account's feed.
        tick_callback overrides self._tick_callback if provided (set it once via
        either path — the caller doesn't need to set both).
        """
        if tick_callback is not None:
            self._tick_callback = tick_callback
        r = _redis_client()
        lock = r.setnx(f'ps:ws:{self.Account.id}', 'running')
        if lock:
            r.expire(f'ps:ws:{self.Account.id}', 16 * 3600)
            self._open_websocket_session(subscriptions, self._tick_callback)
            return True
        logger.debug(f'WebSocket already running for account {self.Account.nickname}. Skipping.')
        return False

    def resubscribe_websocket(self, subscriptions):
        """Force-close and reopen the WebSocket with a FRESH subscription list. Does
        NOT touch the ps:ws:{accountId} lock — the caller already owns the feed, this
        only restarts the underlying transport when the health check finds it dead,
        and always re-reads the current DB subscription list rather than replaying a
        stale in-memory one (the whole point of the frontend is the user can add/remove
        instruments for an account at any time).
        """
        logger.warning(f'[WS-HEALTH] forcing websocket reconnect for account={self.Account.id}')
        try:
            self.close_websocket()
        except Exception:
            pass
        time.sleep(0.5)
        self._open_websocket_session(subscriptions, self._tick_callback)

    def wsIdleSeconds(self) -> float:
        last = self._lastWsActivityAt
        if not last:
            return 0.0
        return time.time() - last

    def isWsConnected(self) -> bool:
        return self._wsConnected

    def authFailedPermanently(self) -> bool:
        """True once the broker has rejected auth _MAX_CONSECUTIVE_AUTH_FAILURES
        times in a row — the caller (ingest_account_ticks) should stop looping and
        exit rather than let resubscribe_websocket keep retrying against a token
        that isn't going to start working on its own.
        """
        return getattr(self, '_auth_failed_permanently', False)

    def close_websocket(self):
        try:
            if self.ConnectionObject is not None:
                self.ConnectionObject.close_websocket()
                self._wsConnected = False
                logger.info(f'[WS-CLOSE] websocket closed for account={self.Account.id}')
        except Exception as e:
            logger.error(f'close_websocket error account={self.Account.id}: {e}')
