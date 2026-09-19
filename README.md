# ARKA TARA backend

Django/DRF modular monolith for the ARKA TARA Try-at-Home jewellery platform.

## Foundation versions

- Python 3.13
- Django 5.2 LTS
- Django REST Framework 3.18
- PostgreSQL 18
- uv for dependency locking and commands

## Local setup

1. Install PostgreSQL 18 and create an empty database/user.
2. Copy .env.example to .env and replace placeholder secrets.
3. Install dependencies with uv sync --frozen.
4. Apply migrations with uv run python manage.py migrate.
5. Start the API with uv run python manage.py runserver.

The API begins at /api/v1/; the public health endpoint is /api/v1/health/.

Django settings are split into local, test, staging, and production. Set DJANGO_SETTINGS_MODULE explicitly outside local development. Staging and production require externally supplied secrets.

## Quality checks

- uv run ruff check .
- uv run ruff format --check .
- uv run python manage.py makemigrations --check --dry-run
- uv run pytest

Tests and application settings use PostgreSQL. CI supplies an isolated PostgreSQL service; SQLite is deliberately not used as a substitute.

## Module boundaries

The initial modules are accounts, catalog, markets, inventory, trials, delivery, payments, billing, notifications, compliance, and analytics. Cross-module writes belong in explicit application workflows as described in the canonical root documentation.

Task 1 does not implement payments, messaging, catalogue, inventory, trial, delivery, or billing behavior. Empty module packages establish ownership boundaries for later authorized tasks.

