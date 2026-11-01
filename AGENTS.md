# Backend repository guidance

Read the [shared operating guide](../AGENTS.md) and the canonical [business rules](../docs/BUSINESS_RULES.md), [architecture](../docs/ARCHITECTURE.md), [data model](../docs/DATA_MODEL.md), [state machines](../docs/STATE_MACHINES.md) and [decision log](../docs/DECISIONS.md) before backend work.

Shared documentation exists only in ../docs/. Do not recreate a local copy. If this repository is checked out alone and those files are unavailable, obtain the canonical context before work that depends on it; do not reconstruct policy from assumptions.

Follow the shared [implementation and independent review workflow](../AGENTS.md#implementation-and-independent-review) and [D-28 finding/handoff requirements](../docs/DECISIONS.md#d-28--codex-implementation-and-independent-claude-review). Codex owns implementation and validated corrections; Claude reviews independently. Review feedback cannot redefine canonical requirements or expand the current model-only scope into deferred workflows.

D-27 extends Task 4 with guest browser sessions/scoped grants, booking recovery proof, sealed selections, payment-start reservations and upfront payment/outbox evidence. See the [checkout checkpoint](../docs/reviews/TASK4_CHECKOUT_MODELS_2026-10-04.md). These models do not enable session/API/provider workflows or finish the entire Task 4 matrix.

Tasks 1–3 remain complete for their delivered scope. [D-26](../docs/DECISIONS.md#d-26--task-4-cross-app-model-completion) now scopes Task 4 to all agreed backend persistence, integrity, migrations and model/database tests; workflows/APIs/providers remain in later tasks. Use the [coverage matrix](../docs/DATA_MODEL.md#task-4-model-coverage). Original storefront 4.1–4.7 move to Task 5 without renumbering or removing evidence. Policy gates still apply; model completeness is not live readiness.

Follow D-23–D-25 and R-33–R-38 for separate staff/customer identities, permanent unlinked guest checkout, historical ownership/snapshots and booking OTP before upfront payment. These are target requirements, not existing implemented flows; keep details canonical in root docs. Do not reset migration history for this scope update.

Respect the owner's [frontend design gate](../AGENTS.md#frontend-design-gate) ([D-21](../docs/DECISIONS.md#d-21--frontend-visual-design-awaits-owner-input)). Authorized backend, API contracts and other design-independent work may continue; do not infer customer/staff screen design from backend requirements or require final presentation to verify independent backend behavior. Frontend presentation remains pending owner-provided approved designs.

Apply the shared [standing search requirement](../AGENTS.md#standing-search-discoverability) and [task ownership map](../docs/TASKS.md#standing-search-discoverability-requirement). Public catalogue identity/metadata must remain extensible and factual; draft media/prices, customer/staff data and payment documents remain protected. Do not reopen completed foundations for minor SEO work or invent availability for structured data.

Follow the shared [business reference convention](../AGENTS.md#business-reference-comments). Reference canonical rule/decision IDs at important model validations, domain guards and history/audit boundaries; maintain the [central implementation map](../docs/TASKS.md#implementation-reference-map). Applied migrations are mapped there without rewriting their history.

Once authorized, follow the Django/DRF/PostgreSQL modular-monolith direction, backend-authoritative rules, transaction-safe stock changes, controlled migrations and idempotent finance/provider boundaries. Django Admin is the back office; enforce separate staff permissions for doorstep operations.

Use [TASKS.md](../docs/TASKS.md) for scope, dependencies and tests, [COMPLIANCE_AND_FINANCE.md](../docs/COMPLIANCE_AND_FINANCE.md) for financial evidence and [INTEGRATIONS.md](../docs/INTEGRATIONS.md) for planned adapters. Do not silently resolve open decisions or implement Task 8/9 integrations in Task 1.
