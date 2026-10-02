# Backend repository guidance

Read the [shared operating guide](../AGENTS.md) and the canonical [business rules](../docs/BUSINESS_RULES.md), [architecture](../docs/ARCHITECTURE.md), [data model](../docs/DATA_MODEL.md), [state machines](../docs/STATE_MACHINES.md) and [decision log](../docs/DECISIONS.md) before backend work.

Shared documentation exists only in ../docs/. Do not recreate a local copy. If this repository is checked out alone and those files are unavailable, obtain the canonical context before work that depends on it; do not reconstruct policy from assumptions.

Tasks 1–3 remain complete for their delivered authorized scope, with recorded CI/review/live gates intact. The owner's 2026-09-26 continuation authorizes Task 4 after the lightweight search-architecture review (D-20). Unresolved-decision gates still apply; reference activation alone never grants booking permission.

Respect the owner's [frontend design gate](../AGENTS.md#frontend-design-gate) ([D-21](../docs/DECISIONS.md#d-21--frontend-visual-design-awaits-owner-input)). Authorized backend, API contracts and other design-independent work may continue; do not infer customer/staff screen design from backend requirements or require final presentation to verify independent backend behavior. Frontend presentation remains pending owner-provided approved designs.

Apply the shared [standing search requirement](../AGENTS.md#standing-search-discoverability) and [task ownership map](../docs/TASKS.md#standing-search-discoverability-requirement). Public catalogue identity/metadata must remain extensible and factual; draft media/prices, customer/staff data and payment documents remain protected. Do not reopen completed foundations for minor SEO work or invent availability for structured data.

Follow the shared [business reference convention](../AGENTS.md#business-reference-comments). Reference canonical rule/decision IDs at important model validations, domain guards and history/audit boundaries; maintain the [central implementation map](../docs/TASKS.md#implementation-reference-map). Applied migrations are mapped there without rewriting their history.

Once authorized, follow the Django/DRF/PostgreSQL modular-monolith direction, backend-authoritative rules, transaction-safe stock changes, controlled migrations and idempotent finance/provider boundaries. Django Admin is the back office; enforce separate staff permissions for doorstep operations.

Use [TASKS.md](../docs/TASKS.md) for scope, dependencies and tests, [COMPLIANCE_AND_FINANCE.md](../docs/COMPLIANCE_AND_FINANCE.md) for financial evidence and [INTEGRATIONS.md](../docs/INTEGRATIONS.md) for planned adapters. Do not silently resolve open decisions or implement Task 8/9 integrations in Task 1.
