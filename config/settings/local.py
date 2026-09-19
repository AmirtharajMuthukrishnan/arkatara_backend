from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from .base import *  # noqa: E402,F403

ENVIRONMENT = "local"
DEBUG = True
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
