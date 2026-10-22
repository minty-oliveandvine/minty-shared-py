"""The smallest Django project the minty_shared.django tests need."""

SECRET_KEY = "test-only"
DEBUG = True
ALLOWED_HOSTS = ["*"]
INSTALLED_APPS: list[str] = []
DATABASES: dict[str, dict[str, str]] = {}
ROOT_URLCONF = "tests.djangoapp.urls"
MIDDLEWARE = ["minty_shared.django.middleware.RequestLoggingMiddleware"]
MINTY_HTTP_LOGGER = "shared-test.http"
MINTY_LOGGABLE_PATH = "tests.djangoapp.urls.loggable_path"
USE_TZ = True
