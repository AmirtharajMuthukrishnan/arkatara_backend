"""Serialize cross-table integrity writes before their implicit row locks."""

from django.db import connections


def lock_integrity_writes(database):
    """Use the same transaction latch as database statement guards.

    A deliberately coarse monolith boundary avoids row-trigger lock inversions.
    Keep transactions short; later services retry whole idempotent transactions.
    Refs: BUSINESS_RULES.md R-14, R-24, R-40; DECISIONS.md D-27.
    """
    with connections[database].cursor() as cursor:
        cursor.execute("SHOW transaction_isolation")
        if cursor.fetchone()[0] != "read committed":
            raise RuntimeError("Integrity writes require READ COMMITTED isolation.")
        cursor.execute("SELECT pg_advisory_xact_lock(1095912257, 40)")
