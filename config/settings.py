import os
from pathlib import Path


def load_env(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue

        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
load_env(BASE_DIR / '.env')

DATA_DIR = BASE_DIR / 'data'
MEDIA_DIR = BASE_DIR / 'media'
STATIC_DIR = BASE_DIR / 'static'


SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'local-dev-secret-key')
DEBUG = os.getenv('DJANGO_DEBUG', 'true').lower() == 'true'
ALLOWED_HOSTS = [host.strip() for host in os.getenv('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',') if host.strip()]

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')
BINANCE_API_BASE_URL = os.getenv('BINANCE_API_BASE_URL', 'https://api.binance.com')


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'apps.common',
    'apps.core',
    'apps.institutions',
    'apps.accounts',
    'apps.products',
    'apps.imports',
    'apps.dashboard',
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
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'builtins': ['apps.core.templatetags.pf_extras'],
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.core.context_processors.project_settings',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

_db_engine = os.getenv('DATABASE_ENGINE', 'django.db.backends.sqlite3')
_db_config = {
    'ENGINE': _db_engine,
    'NAME': os.getenv('DATABASE_NAME', str(BASE_DIR / 'db.sqlite3')),
    'USER': os.getenv('DATABASE_USER', ''),
    'PASSWORD': os.getenv('DATABASE_PASSWORD', ''),
    'HOST': os.getenv('DATABASE_HOST', ''),
    'PORT': os.getenv('DATABASE_PORT', ''),
}
if _db_engine == 'django.db.backends.sqlite3':
    _db_config['OPTIONS'] = {'timeout': 30}

DATABASES = {'default': _db_config}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'ru'

TIME_ZONE = os.getenv('TIME_ZONE', 'Europe/Minsk')

USE_I18N = True

USE_L10N = False

USE_TZ = True

DATE_FORMAT = 'd.m.Y'
SHORT_DATE_FORMAT = 'd.m.Y'
DATETIME_FORMAT = 'd.m.Y H:i'
DATE_INPUT_FORMATS = ['%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y']


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = '/static/'
STATICFILES_DIRS = [STATIC_DIR]
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = MEDIA_DIR

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

IMPORT_RAW_DIR = DATA_DIR / 'raw'
IMPORT_PROCESSED_DIR = DATA_DIR / 'processed'
REPORTING_BASE_CURRENCY = os.getenv('REPORTING_BASE_CURRENCY', 'USD')

_csrf_origins = os.getenv('DJANGO_CSRF_TRUSTED_ORIGINS', '')
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in _csrf_origins.split(',')
    if origin.strip()
]

if os.getenv('DJANGO_USE_WHITENOISE', '').lower() == 'true':
    MIDDLEWARE.insert(1, 'whitenoise.middleware.WhiteNoiseMiddleware')
    STORAGES = {
        'default': {
            'BACKEND': 'django.core.files.storage.FileSystemStorage',
        },
        'staticfiles': {
            'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
        },
    }

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
