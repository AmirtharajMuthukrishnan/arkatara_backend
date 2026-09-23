"""Shared fail-closed settings for externally configured deployments."""

from config.env import EnvironmentConfigurationError, env, env_bool, env_list

from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)
if len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5 or SECRET_KEY.startswith("django-insecure-"):
    raise EnvironmentConfigurationError("DJANGO_SECRET_KEY must be a strong deployment secret.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
    raise EnvironmentConfigurationError("DJANGO_ALLOWED_HOSTS must list explicit deployment hosts.")

# Do not inherit localhost database credentials or browser origins in a deployment.
for field, variable in {
    "NAME": "POSTGRES_DB",
    "USER": "POSTGRES_USER",
    "PASSWORD": "POSTGRES_PASSWORD",
    "HOST": "POSTGRES_HOST",
    "PORT": "POSTGRES_PORT",
}.items():
    DATABASES["default"][field] = env(variable, required=True)  # noqa: F405

CORS_ALLOWED_ORIGINS = env_list("DJANGO_CORS_ALLOWED_ORIGINS")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
