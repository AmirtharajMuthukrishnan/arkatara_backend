# ARKA TARA backend

Django/DRF modular monolith for the ARKA TARA Try-at-Home jewellery platform.

## Current implementation scope

[Task 4](../docs/TASKS.md#4-cross-app-model-completion) now completes all agreed domain models, constraints/history protections, migrations and model/database tests. The [coverage matrix](../docs/DATA_MODEL.md#task-4-model-coverage) distinguishes existing foundations from pending work. Later tasks implement workflows, APIs and providers; no customer authentication/booking flow is delivered by this documentation update. D-23–D-25 specify separate StaffUser/CustomerAccount identities, permanent unlinked guest bookings and booking OTP before upfront payment. Retain applied migrations and historical evidence.

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

Production HSTS starts at one hour. Increase `DJANGO_SECURE_HSTS_SECONDS` after checking HTTPS deployment; `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` and `DJANGO_SECURE_HSTS_PRELOAD` require explicit opt-in after domain review. Trusted proxy handling, database transport, runtime process settings and deployment hosts must be configured/tested under D-22's AWS direction before live release.

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

Task 2 adds catalogue/location models, draft physical identities and price revisions, read-only reference APIs and the historical snapshot value contract. Task 3 adds the Admin, media, CSV and gated receiving/QC workflows below. Task 4 completes remaining persistence; payments, messaging, trial/delivery and billing workflows remain later tasks. See the canonical [data model](../docs/DATA_MODEL.md).

## Permissions and business configuration

The default API permission requires authentication. `/api/v1/health/` allows anonymous access and reports process liveness. Task 2 also exposes read-only `/api/v1/reference-data/` and `/api/v1/serviceability/`; contracts are in [ARCHITECTURE.md](../docs/ARCHITECTURE.md). Coverage always returns `bookable: false`. No customer booking or staff assignment workflow is live yet. Django Admin requires active staff status and per-model permissions; Task 3 registers restricted reference screens and dedicated operation forms. Create a local administrator explicitly with `uv run python manage.py createsuperuser`; no account or password is seeded.

Configuration Admin creates draft revisions only. No production limits, expiry periods, eligibility rules, entity identities or deposits are approved by this scaffold. Read the canonical decision register before any activation. Existing policy revisions and audit evidence must be preserved; privileged entity/configuration administration records attributable changes. Keep staff accounts inactive rather than deleting their historical attribution.

Configuration revisions are immutable through model/queryset/Admin writes; direct SQL is outside that application-level protection. Exact-scope lookup requires eligible approval evidence and refuses missing or conflicting approved policy. Activation, retirement and supersession of an open-ended approved revision need a later reviewed workflow before these settings drive live operations. The foundation does not auto-approve old records or reconstruct missing historical attribution.

## Review, migrations and recovery

Task 3 adds five migrations: catalog `0004`/`0005` and inventory `0002`/`0003`/`0004`. They add media/import/movement records, custody and gated stock states, history triggers and an inventory downgrade guard. Apply them using the usual reviewed migration commands. Existing physical drafts retain their identity and UNKNOWN custody with no invented movement history. Once stock movements or operational state exist, downgrade is rejected before removing movement protection; use a reviewed forward correction. Removing catalogue/media tables would also destroy records and is not a recovery strategy. Applied Task 1/2 migrations were not rewritten.

Start feature/fix branches from `development`, open a pull request into `development`, and require successful CI before merge. Promote reviewed release commits to `main`. CI runs on pull requests and pushes to `main`/`development`; a feature-branch push alone does not start it. CI installs `uv.lock`, checks style and migration drift, applies migrations to PostgreSQL 18 and runs the full suite. The canonical module responsibilities and shared workflow live in [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).

Generate new migrations with `uv run python manage.py makemigrations`. Review their SQL using `sqlmigrate <app> <migration>` and their dependency/order with `migrate --plan`. Keep already-merged/applied migrations unchanged; follow-up changes need a new migration. Separate structural changes from business seeds and data backfills. Verify both fresh installation and upgrades from the previous schema on disposable databases.

Task 2 adds six migrations across markets/catalog/inventory; apply them using the usual `uv run python manage.py migrate`. Separate seed migrations add only approved reference identities. Their reverse operations retain data, and reapplication preserves existing labels/statuses. The price-history trigger has reversible DDL. Reversing initial table migrations deletes domain data and is not a routine rollback strategy; prefer compatible code rollback or a reviewed forward correction. Task 1 migrations remain unchanged.

Before deployment, the release owner must record the commit and migration plan, take a database backup, verify restoration on an isolated database, and validate the release in staging. Apply migrations once with a dedicated migration role, run system/deployment checks and smoke tests, and then start compatible application processes. A deployed runtime role should not own schema objects or have CREATE/DROP/TRUNCATE privileges over evidence tables. D-22 selects AWS hosting direction; deployment ownership/provisioning remain pending and these steps do not authorize provisioning.

On failure, stop further writes where required and preserve logs/evidence. Roll back application code only when it remains compatible with the schema. Prefer a reviewed forward migration for data changes; reverse schema migrations only after reviewing their data-loss implications. Never reset a shared database to resolve drift. Audit update/delete triggers protect ordinary SQL writes; database owners/superusers can bypass schema-level controls, so restricted runtime roles and backups remain necessary.

Shared business/project documentation exists only in [../docs/](../docs/), referenced by [AGENTS.md](AGENTS.md). Approved BD-14 tracks it in a separate documentation-only root repository; see the [workspace README](../README.md) for the clone layout. The documentation remote is published, and this application clone alone does not include the canonical documents. Do not copy them into this repository.

## Task 3 Admin usage

At `/admin/`, reference screens create drafts and audit permitted edits. Immutable identifiers and used variant definitions cannot be reassigned. Editing a stale form requires a reload. Price/configuration approval and reference activation are not available through these screens. See [D-19](../docs/DECISIONS.md#d-19--task-3-back-office-and-onboarding-boundary) for the canonical scope and business gates.

Inventory browsing includes filters and read-only movement history. The dedicated registration screen requires `inventory.add_inventoryunit`; receiving requires `inventory.receive_inventoryunit`; QC requires `inventory.inspect_inventoryunit`. Grant `inventory.view_inventoryunit` and `inventory.view_inventorymovement` for the corresponding history views. Receiving/QC also require approved, effective hub-scoped procedures. Configuration Admin can prepare drafts only; do not bypass the pending approval workflow with database edits. No live operating values or real stock are seeded.

CSV imports appear under catalogue import batches. Start with a preview, inspect all errors, then select the file again to apply it. Apply revalidates current data and commits the whole file atomically. Keep the same import UUID, content and reason for an exact retry. Use these UTF-8 headers in exactly this order:

```csv
product_code,product_name,category_code,sku,material_code,purity_code,size_label
```

This format creates draft products/variants or reuses matching drafts. Category/material/purity references must already exist; empty purity is allowed. Conflicting existing definitions are rejected. Variant imports require `catalog.import_catalogue`, `catalog.add_product`, `catalog.add_productvariant` and view permissions for category/material/purity.

```csv
registration_id,variant_sku,hub_code,source_reference
```

This format registers physical drafts against existing variants/hubs. Each piece needs a distinct UUID `registration_id`, retained for retries, and a source reference. It does not record physical receipt. Unit imports require `catalog.import_catalogue`, `inventory.add_inventoryunit`, `catalog.view_productvariant` and `markets.view_hub`. Grant `catalog.view_csvimportbatch` to inspect retained outcomes. Imports are bounded to 500 rows/1,000,000 bytes per request; these are technical limits.

Product media defaults to disabled. For explicit local testing, set `PRODUCT_MEDIA_STORAGE=LOCAL`, an absolute dedicated `PRODUCT_MEDIA_ROOT` outside Git, a positive `PRODUCT_MEDIA_MAX_BYTES`, and a comma-separated `PRODUCT_MEDIA_ALLOWED_TYPES` chosen from supported `image/png,image/jpeg,video/mp4`. No allowed list or size value is selected by `.env.example`. `LOCAL` refuses staging/production. Production storage remains a separate integration dependency.

Upload through the Product media add screen with media-add/product-view permissions. Choose a role, description and reason; thumbnails need a same-product source. Media-view/product-view permissions control private attachment downloads. Metadata changes require media-change/product-view permissions and are audited. Retirement retains the original file. These are private drafts, with no customer URL. Structural validation is not full decoding or malware scanning; production publication needs the controls recorded in [INTEGRATIONS.md](../docs/INTEGRATIONS.md#object-storage--task-3).

## Task 4 storefront publication

Historical heading: this records original storefront work, now carried into Task 5 under D-26. Current Task 4 is model completion; the description below is not evidence that its cross-app checklist is finished.

Apply catalog migration `0006_storefrontpage` before using the public catalogue. It adds stable page identities and publication permissions without publishing existing products or changing stock. Reversing it removes page identities/content; preserve records and prefer a compatible code rollback or reviewed forward correction after use.

Storefront pages in Admin link one material, category or product to an immutable lowercase slug, reviewed title/description and optional SEO text. Draft authoring uses the normal model permissions and audited change reason. Separate `catalog.publish_storefrontpage` and `catalog.withdraw_storefrontpage` permissions control the publication actions; each requires a reason. Grant view permission to access the list. Publishing requires active linked references; it does not activate them. Withdraw published content before editing, then publish the reviewed revision. Slugs and subject identities cannot be reassigned or deleted; a future slug change requires the reviewed redirect/history workflow.

Anonymous `GET /api/v1/catalogue/` lists published content; `GET /api/v1/catalogue/pages/<slug>/` reads one published page. Optional `market`, `material` and positive `page` parameters are validated and repeated/unknown parameters rejected. Product details do not paginate. Private/draft/withdrawn subjects return 404; page size is a technical bound of 12, unrelated to Trial Plan quantities. The canonical [architecture](../docs/ARCHITECTURE.md) describes the response and selection semantics.

Availability is an aggregate of eligible physical stock at active hubs in the selected city. Responses always carry `bookable: false`, `price: {state: "UNCONFIGURED"}` and empty public media. No private asset locations, arbitrary internal variant specification, draft prices, unit identities or hub counts are serialized. Publication does not resolve serviceability, fulfilment, price/tax or media-provider decisions. No real storefront content or stock is seeded.
