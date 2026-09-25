# ARKA TARA backend

Django/DRF modular monolith for the ARKA TARA Try-at-Home jewellery platform.

## Foundation versions

- Python 3.13
- Django 5.2 LTS
- Django REST Framework 3.18
- PostgreSQL 18
- uv for dependency locking and commands

## Local setup

1. Install Python 3.13, uv and PostgreSQL 18; start PostgreSQL locally.
2. Create a dedicated development role and empty database. With PostgreSQL command-line tools on PATH, run `createuser -h 127.0.0.1 -U postgres --pwprompt --createdb arkatara_local`, then `createdb -h 127.0.0.1 -U postgres --owner=arkatara_local arkatara_local`. The local role needs CREATEDB for pytest's temporary database; this is not a production role template.
3. Copy `.env.example` to `.env`. Set the local database/user/password to match the preceding step and replace the placeholder Django secret. `.env` stays ignored by Git.
4. Install dependencies with `uv sync --frozen`.
5. Review and apply migrations with `uv run python manage.py migrate --plan` and `uv run python manage.py migrate`.
6. Start the API with `uv run python manage.py runserver`.

The API begins at /api/v1/; the public health endpoint is /api/v1/health/.

Django settings are split into local, test, staging, and production. Only local settings load `.env`. Set `DJANGO_SETTINGS_MODULE` explicitly outside local development; setting `APP_ENV` alone does not select a settings module.

Staging and production require an externally supplied strong `DJANGO_SECRET_KEY`, an explicit `DJANGO_ALLOWED_HOSTS` list, and all five `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` settings. They refuse missing values and disable DEBUG. Browser CORS/CSRF origins default to empty in these environments; supply the approved HTTPS origins when needed. Cookies are secure and HTTPS redirects are enabled. Keep separate databases, users and secrets for each environment.

Production HSTS starts at one hour. Increase `DJANGO_SECURE_HSTS_SECONDS` after checking HTTPS deployment; `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` and `DJANGO_SECURE_HSTS_PRELOAD` require explicit opt-in after domain review. Trusted proxy handling, database transport, runtime process manager and deployment host remain to be selected and tested before a live release.

## Quality checks

- uv run ruff check .
- uv run ruff format --check .
- uv run python manage.py check
- uv run python manage.py makemigrations --check --dry-run
- uv run pytest

Tests and application settings use PostgreSQL. CI supplies an isolated PostgreSQL service. Local pytest runs use `config.settings.test`, do not load `.env`, and require the local database settings exported into the current terminal. Use only a dedicated local/test database and a test role permitted to create `test_<POSTGRES_DB>`; pytest creates and drops that database. Never point tests at staging or production. Test configuration has no seeded production policies.

For a local-only check using the `.env` connection instead, `uv run pytest --ds=config.settings.local` uses the same temporary test database mechanism. This keeps credentials out of command history; the local role still needs CREATEDB. Non-database checks can run with `uv run pytest -m "not django_db"`.

## Module boundaries

The initial modules are accounts, catalog, markets, inventory, trials, delivery, payments, billing, notifications, compliance, and analytics. Cross-module writes belong in explicit application workflows as described in the canonical root documentation.

Task 2 adds catalogue/location models, draft physical identities and price revisions, read-only reference APIs and the historical snapshot value contract. See the canonical [data model](../docs/DATA_MODEL.md) for field semantics, onboarding gates and remaining transaction work. Payments, messaging, trial/delivery workflows, operational stock transitions and billing remain later tasks.

## Permissions and business configuration

The default API permission requires authentication. `/api/v1/health/` allows anonymous access and reports process liveness. Task 2 also exposes read-only `/api/v1/reference-data/` and `/api/v1/serviceability/`; contracts are in [ARCHITECTURE.md](../docs/ARCHITECTURE.md). Coverage always returns `bookable: false`. No customer booking or staff assignment workflow is live yet. Django Admin requires active staff status and per-model permissions; Task 2 models are not registered for generic editing. Create a local administrator explicitly with `uv run python manage.py createsuperuser`; no account or password is seeded.

Configuration Admin creates draft revisions only. No production limits, expiry periods, eligibility rules, entity identities or deposits are approved by this scaffold. Read the canonical decision register before any activation. Existing policy revisions and audit evidence must be preserved; privileged entity/configuration administration records attributable changes. Keep staff accounts inactive rather than deleting their historical attribution.

Configuration revisions are immutable through model/queryset/Admin writes; direct SQL is outside that application-level protection. Exact-scope lookup requires eligible approval evidence and refuses missing or conflicting approved policy. Activation, retirement and supersession of an open-ended approved revision need a later reviewed workflow before these settings drive live operations. The foundation does not auto-approve old records or reconstruct missing historical attribution.

## Review, migrations and recovery

Start feature/fix branches from `development`, open a pull request into `development`, and require successful CI before merge. Promote reviewed release commits to `main`. CI runs on pull requests and pushes to `main`/`development`; a feature-branch push alone does not start it. CI installs `uv.lock`, checks style and migration drift, applies migrations to PostgreSQL 18 and runs the full suite. The canonical module responsibilities and shared workflow live in [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).

Generate new migrations with `uv run python manage.py makemigrations`. Review their SQL using `sqlmigrate <app> <migration>` and their dependency/order with `migrate --plan`. Keep already-merged/applied migrations unchanged; follow-up changes need a new migration. Separate structural changes from business seeds and data backfills. Verify both fresh installation and upgrades from the previous schema on disposable databases.

Task 2 adds six migrations across markets/catalog/inventory; apply them using the usual `uv run python manage.py migrate`. Separate seed migrations add only approved reference identities. Their reverse operations retain data, and reapplication preserves existing labels/statuses. The price-history trigger has reversible DDL. Reversing initial table migrations deletes domain data and is not a routine rollback strategy; prefer compatible code rollback or a reviewed forward correction. Task 1 migrations remain unchanged.

Before deployment, the release owner must record the commit and migration plan, take a database backup, verify restoration on an isolated database, and validate the release in staging. Apply migrations once with a dedicated migration role, run system/deployment checks and smoke tests, and then start compatible application processes. A deployed runtime role should not own schema objects or have CREATE/DROP/TRUNCATE privileges over evidence tables. Hosting and deployment ownership remain unselected; these steps do not authorize provisioning.

On failure, stop further writes where required and preserve logs/evidence. Roll back application code only when it remains compatible with the schema. Prefer a reviewed forward migration for data changes; reverse schema migrations only after reviewing their data-loss implications. Never reset a shared database to resolve drift. Audit update/delete triggers protect ordinary SQL writes; database owners/superusers can bypass schema-level controls, so restricted runtime roles and backups remain necessary.

Shared business/project documentation exists only in [../docs/](../docs/), referenced by [AGENTS.md](AGENTS.md). Approved BD-14 tracks it in a separate documentation-only root repository; see the [workspace README](../README.md) for the clone layout. The documentation remote is published, and this application clone alone does not include the canonical documents. Do not copy them into this repository.
