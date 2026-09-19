from .base import *  # noqa: F403

ENVIRONMENT = "test"
SECRET_KEY = "test-only-secret-key"
DEBUG = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
