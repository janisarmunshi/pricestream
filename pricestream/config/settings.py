"""
Django settings for the PriceStream project.
"""

import os
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
environ.Env.read_env(os.path.join(BASE_DIR, '.env'))

SECRET_KEY = env('SECRET_KEY')
# Separate key for encrypted model fields (BrokerAccount credentials) so rotating
# SECRET_KEY (which invalidates sessions/signed cookies) doesn't also break decryption
# of stored credentials, and vice versa.
CRYPTOGRAPHY_KEY = env('CRYPTOGRAPHY_KEY')

DEBUG = env('DEBUG')

ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['127.0.0.1', 'localhost'])

# Served under a path prefix (marketmantra.tech/pricestream), not at domain root —
# nginx strips the prefix before proxying (proxy_pass .../ with a trailing slash),
# so Django must add it back onto every URL/static path it generates.
FORCE_SCRIPT_NAME = env('FORCE_SCRIPT_NAME', default=None)
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'rest_framework',
    'django_celery_beat',
    'django_cryptography',

    'apps.accounts',
    'apps.instruments',
    'apps.streaming',
    'apps.ticks',
    'apps.api',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database — PostgreSQL + TimescaleDB extension (enabled via migration).
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': env('DB_NAME'),
        'USER': env('DB_USER'),
        'PASSWORD': env('DB_PASSWORD'),
        'HOST': env('DB_HOST', default='localhost'),
        'PORT': env('DB_PORT', default='5432'),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'

# --- Redis --------------------------------------------------------------
REDIS_HOST = env('REDIS_HOST', default='127.0.0.1')
REDIS_PORT = env.int('REDIS_PORT', default=6379)
REDIS_PASSWORD = env('REDIS_PASSWORD', default=None)
_redis_auth = f':{REDIS_PASSWORD}@' if REDIS_PASSWORD else ''

REDIS_URL_CELERY = f'redis://{_redis_auth}{REDIS_HOST}:{REDIS_PORT}/0'
REDIS_URL_CACHE = f'redis://{_redis_auth}{REDIS_HOST}:{REDIS_PORT}/1'
REDIS_URL_STREAMS = f'redis://{_redis_auth}{REDIS_HOST}:{REDIS_PORT}/2'

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': REDIS_URL_CACHE,
    }
}

# --- Celery ---------------------------------------------------------------
CELERY_BROKER_URL = REDIS_URL_CELERY
CELERY_RESULT_BACKEND = REDIS_URL_CELERY
CELERY_ACCEPT_CONTENT = ['application/json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Asia/Kolkata'
CELERY_ENABLE_UTC = False
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers.DatabaseScheduler'

# --- Tick ingestion pipeline defaults (overridable via streaming.StreamingConfig) ---
TICK_STREAM_KEY_PREFIX = 'ps:ticks'          # one stream per account: ps:ticks:{accountId}
TICK_DLQ_STREAM_KEY = 'ps:ticks:dlq'
TICK_CONSUMER_GROUP = 'ps-committer'
TICK_BATCH_SIZE = env.int('TICK_BATCH_SIZE', default=500)
TICK_BATCH_FLUSH_SECONDS = env.float('TICK_BATCH_FLUSH_SECONDS', default=2.0)
TICK_MAX_INSERT_RETRIES = 3

# WS health-check cadence (seconds) — matches Yantra's proven ~5s interval.
WS_HEALTH_CHECK_INTERVAL = env.int('WS_HEALTH_CHECK_INTERVAL', default=5)
# Auto-start/stop supervisor cadence (minutes).
MARKET_HOURS_SUPERVISOR_INTERVAL_MINUTES = env.int('MARKET_HOURS_SUPERVISOR_INTERVAL_MINUTES', default=1)
# Per-instrument silence threshold before treating a subscription as gone quiet (seconds).
TICK_SILENCE_THRESHOLD_SECONDS = env.int('TICK_SILENCE_THRESHOLD_SECONDS', default=120)

# --- SOCKS5 proxy credentials (per-account sourceIp is stored on BrokerAccount) ---
SOCKS_PROXY_USER = env('SOCKS_PROXY_USER', default='')
SOCKS_PROXY_PASS = env('SOCKS_PROXY_PASS', default='')

# --- DRF --------------------------------------------------------------
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'apps.api.authentication.ApiKeyAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_PAGINATION_CLASS': 'apps.api.pagination.TickCursorPagination',
    'PAGE_SIZE': 500,
    'DEFAULT_THROTTLE_CLASSES': (
        'apps.api.throttling.ApiKeyRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': {
        'api_key': '120/min',
    },
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] ({process:d}) {name} - {levelname} - {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'level': 'INFO',
            'class': 'logging.handlers.TimedRotatingFileHandler',
            'filename': os.path.join(BASE_DIR, 'logs', 'pricestream.log'),
            'when': 'midnight',
            'interval': 1,
            'backupCount': 30,
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'apps': {
            'handlers': ['console', 'file'],
            'level': env('LOG_LEVEL', default='INFO'),
            'propagate': False,
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'WARNING',
    },
}
