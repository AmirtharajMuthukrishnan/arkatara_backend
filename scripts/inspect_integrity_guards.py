"""Inspect effective frozen forward-SQL functions without executing migrations.

The migration SQL is hand-maintained. Ignored historical scratch generators are
not a source of truth. This tool reads only top-level SQL literals, never reverse
SQL, and can compare the resulting bodies with a migrated PostgreSQL database.
"""

import argparse
import ast
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
CREATE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(\w+)\s*\([^;]*?\)"
    r"\s+RETURNS\s+[^;]*?\bAS\s+\$\$(.*?)\$\$\s*;",
    re.I | re.S,
)
DROP = re.compile(r"DROP\s+FUNCTION\s+(\w+)\s*\([^;]*?\)\s*;", re.I | re.S)


def effective_functions():
    effective = {}
    for path in sorted((BACKEND / "trials/migrations").glob("[0-9]*.py")):
        parsed = ast.parse(path.read_text(encoding="utf-8-sig"))
        literals = [
            node.value.value
            for node in parsed.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "SQL" for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ]
        for sql in literals:
            events = [(match.start(), "create", match) for match in CREATE.finditer(sql)]
            events += [(match.start(), "drop", match) for match in DROP.finditer(sql)]
            for _, kind, match in sorted(events, key=lambda event: event[0]):
                name = match[1]
                if kind == "drop":
                    effective.pop(name, None)
                else:
                    effective[name] = (path.name, match[2].strip())
    if not effective or "checkout_guard_record" not in effective:
        raise RuntimeError("No recognizable forward integrity function graph.")
    return effective


def check_database(effective):
    import django

    sys.path.insert(0, str(BACKEND))
    django.setup()
    from django.db import connection

    mismatches = []
    with connection.cursor() as cursor:
        for name, (_, expected) in sorted(effective.items()):
            cursor.execute(
                "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public' AND p.proname=%s",
                [name],
            )
            actual = cursor.fetchall()
            if len(actual) != 1 or actual[0][0].strip() != expected:
                mismatches.append(name)
    if mismatches:
        raise SystemExit("Database forward-function drift: " + ", ".join(mismatches))
    print(f"All {len(effective)} frozen forward function bodies match the database.")


def check_pending_balances():
    """Read-only rollout check after billing schema exists, before enabling writers."""
    import django

    sys.path.insert(0, str(BACKEND))
    django.setup()
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM billing_purchase p WHERE p.status='SEALED' AND NOT EXISTS "
            "(SELECT 1 FROM billing_purchasebalanceevidence b WHERE b.purchase_id=p.id)"
        )
        pending = cursor.fetchone()[0]
    if pending:
        raise SystemExit(
            f"{pending} sealed legacy selection(s) need reviewed balance evidence/recovery "
            "before new confirmation guards and application writers are enabled."
        )
    print("No sealed legacy selection lacks authoritative balance evidence.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--function", help="Print one effective function body.")
    parser.add_argument("--check-database", action="store_true")
    parser.add_argument("--check-pending-balances", action="store_true")
    args = parser.parse_args()
    functions = effective_functions()
    if args.check_pending_balances:
        check_pending_balances()
    elif args.check_database:
        check_database(functions)
    elif args.function:
        source, body = functions[args.function]
        print(f"{args.function}: {source}\n{body}")
    else:
        for name, (source, _) in sorted(functions.items()):
            print(f"{name}: {source}")
