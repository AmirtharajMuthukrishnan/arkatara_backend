from config.env import env_bool, env_cast

from .deployment import *  # noqa: F403

ENVIRONMENT = "production"
SECURE_HSTS_SECONDS = env_cast("DJANGO_SECURE_HSTS_SECONDS", int, 3600)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)
