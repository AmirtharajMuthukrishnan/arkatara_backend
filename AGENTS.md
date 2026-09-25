# Backend repository guidance

Read the [shared operating guide](../AGENTS.md) and the canonical [business rules](../docs/BUSINESS_RULES.md), [architecture](../docs/ARCHITECTURE.md), [data model](../docs/DATA_MODEL.md), [state machines](../docs/STATE_MACHINES.md) and [decision log](../docs/DECISIONS.md) before backend work.

Shared documentation exists only in ../docs/. Do not recreate a local copy. If this repository is checked out alone and those files are unavailable, obtain the canonical context before work that depends on it; do not reconstruct policy from assumptions.

Task 1 is verified complete. Task 2 domain foundation implementation is authorized as of 2026-09-24. Later tasks remain governed by the backlog and unresolved-decision gates; reference activation alone never grants booking permission.

Once authorized, follow the Django/DRF/PostgreSQL modular-monolith direction, backend-authoritative rules, transaction-safe stock changes, controlled migrations and idempotent finance/provider boundaries. Django Admin is the back office; enforce separate staff permissions for doorstep operations.

Use [TASKS.md](../docs/TASKS.md) for scope, dependencies and tests, [COMPLIANCE_AND_FINANCE.md](../docs/COMPLIANCE_AND_FINANCE.md) for financial evidence and [INTEGRATIONS.md](../docs/INTEGRATIONS.md) for planned adapters. Do not silently resolve open decisions or implement Task 8/9 integrations in Task 1.
