"""Regenerate the owner's annotated model snapshot from Django's actual registry.

Existing per-model/field annotations are preserved unless superseded below.
This is documentation introspection only: no database connection is opened.
"""

import argparse
import ast
import inspect
import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django  # noqa: E402

django.setup()

from django.apps import apps  # noqa: E402
from django.db.models import NOT_PROVIDED  # noqa: E402

APP_ORDER = [
    "accounts",
    "trials",
    "delivery",
    "markets",
    "catalog",
    "inventory",
    "compliance",
    "payments",
    "notifications",
    "billing",
    "analytics",
]
OVERRIDES = {
    "trials.trialbooking": "Immutable guest/account identity and original contact/address "
    "snapshot. D-27 adds sealed selections, consumed booking phone proof and the pre-visit "
    "lifecycle through verified payment confirmation. PostgreSQL guards reject ownership "
    "changes, unbound proof and confirmation without a paid context. No checkout API is enabled.",
    "trials.trialbox": "A category-specific container in one combined booking. The plan reference "
    "is nullable only to preserve legacy drafts; complete plans and item snapshots are required "
    "before sealing. No unique booking/category constraint chooses repeated-box policy.",
    "trials.guestbookingaccessgrant": "An explicit permission from one anonymous browser session "
    "to one guest booking. It holds scope and validity; the credential digest now belongs to "
    "GuestBrowserSession. Revocation is irreversible. No account linkage or phone-history lookup.",
}
FIELDS = {
    "token_digest": "SHA-256 digest of an opaque secret; raw credentials are never stored here.",
    "browser_session": "Anonymous browser principal authorized for this specific booking.",
    "guest_session": "Anonymous request principal; mutually exclusive with customer_account.",
    "scope": "Permission/operation scope, not an authentication secret.",
    "key": "Action UUID, reused for exact retries within principal/operation scope.",
    "rotated_at": "Credential replacement time; grants keep their browser-session identity.",
    "verified_phone_challenge": "Consumed OTP proof belonging to this exact booking.",
    "verification_valid_until": "Deadline for initiating payment using this phone proof.",
    "sealed_at": "Seals authoritative selections and required historical evidence.",
    "acceptance_snapshot": "Copied acceptance facts; requires actual policy evidence.",
    "confirmed_at": "Time confirmation met the owning model's evidence guards.",
    "plan_revision": "Immutable offering revision; incomplete legacy drafts may lack it.",
    "plan_snapshot": "Historical offering facts, independent of future configuration revisions.",
    "selection_snapshot": "Historical requested variant facts, separate from sale prices.",
    "quantity": "Requested physical-piece count; no approved commercial maximum is implied.",
    "trial_box_item": "Requested variant/quantity that this physical allocation fulfils.",
    "reservation": "One exclusive physical-piece commitment; not a catalogue variant.",
    "payment_context": "Timed collection context that acquired this inventory commitment.",
    "booking_charge": "Upfront obligation; exclusive with a purchase obligation where applicable.",
    "purchase_obligation": "Final purchase balance, shared across payment presentation channels.",
    "amount_minor": "Integer minor units; field-specific bounds apply, without implicit rounding.",
    "currency": "Explicit currency; must agree across obligation, attempt and allocation.",
    "payee_legal_entity": "Protected party reference; historical details are snapshotted.",
    "payee_snapshot": "Historical collection-party details, independent of later entity edits.",
    "policy_snapshot": "Historical policy facts; this JSON alone does not establish approval.",
    "quote_expires_at": "Explicit deadline for accepting this quote; no default timeout.",
    "provider_account": "Provider's merchant/account namespace; never credentials.",
    "environment": "TEST/LIVE identity namespace; fakes cannot operate in production.",
    "provider_order_id": "Provider order identity, assigned once after creation/reconciliation.",
    "provider_payment_id": "Payment identity; authorization and capture remain distinct facts.",
    "provider_event_id": "Provider event identity for duplicate-webhook protection.",
    "payload_digest": "Digest of authenticated provider payload; not the unrestricted raw payload.",
    "verification_reference": "Backend verification evidence; no client-only assertion.",
    "source_event": "Authenticated inbox event with the same provider namespace as the receipt.",
    "transaction_kind": "AUTHORISATION or CAPTURE; only captured funds can be allocated.",
    "gateway_transaction": "Immutable capture being applied within receipt limits.",
    "resolution_reference": "Supplementary reference; linked resolution evidence is authoritative.",
    "resolution": "Attributable immutable provider reconciliation for this exact context.",
    "disposition": "Sale, receipt/QC or never-dispatched hub evidence closing a commitment.",
    "policy_scope_version": "Identity contract: legacy 1 uses codes; new 2 uses public UUIDs.",
    "current_evidence": "Latest visit observation; original assignment/outcome history remains.",
    "balance_evidence": "Immutable numerical balance basis and explicit calculation/policy facts.",
    "source_allocation": "Captured upfront allocation consumed within a locked source budget.",
    "amount_due_before_application": "Jewellery value plus adjustment before source funds.",
    "adjustment_minor": "Explicit signed adjustment; no commercial or tax treatment is defaulted.",
    "payable_minor": "Basis less authoritative applications, proven when selection is sealed.",
    "capture_started_at": "Start of protected capture; ordinary expiry cannot release it.",
    "capture_requested_at": "Backend capture request time, separate from verified success.",
    "last_verified_at": "Latest backend provider observation; cannot move backwards.",
    "committed_at": "Time exclusive stock became committed against successful collection.",
    "release_reason": "Explicit release reason retained with time and reconciliation evidence.",
    "redeemed_at": "Single-use recovery redemption time, requiring separate fresh proof.",
    "redeemed_session": "Browser session restored for this booking; never a customer identity.",
    "verified_challenge": "Consumed recovery-purpose challenge belonging to this grant.",
    "recovery_grant": "Single-booking recovery subject for this independent OTP purpose.",
    "response_status": "HTTP result status; retries must reauthorize and rebuild responses.",
    "dedupe_key": "Operation identity; remote exactly-once behavior is not guaranteed.",
    "payload_reference": "Protected short-lived payload reference; no plaintext OTP here.",
    "lease_token": "Worker fencing identity; worker enforcement remains later work.",
    "lease_until": "Explicit worker-claim deadline, distinct from booking/stock expiry.",
    "last_error_code": "Sanitized error code only; never secret-bearing provider responses.",
    "error_code": "Sanitized provider delivery error classification.",
    "exception_code": "Sanitized payment exception classification for reconciliation.",
}


def annotations(previous):
    prose, fields = {}, {}
    for match in re.finditer(
        r'<a id="model-([^"]+)"></a>(.*?)(?=<a |### Source:|\Z)', previous, re.S
    ):
        key = match[1].replace("-", ".", 1)
        section = match[2]
        source_end = section.find("\n\n", section.find("**Table:**"))
        table_start = section.find("| Field |")
        if source_end >= 0 and table_start > source_end:
            prose[key] = section[source_end:table_start].strip()
        for line in section.splitlines():
            if line.startswith("| `"):
                cells = line.strip("|").split("|")
                if len(cells) == 5:
                    fields[(key, cells[0].strip().strip("`"))] = cells[-1].strip()
    return prose, fields


def escape(value):
    return str(value).replace("|", "&#124;").replace("\n", " ").replace("\r", "")


def source_block(path):
    source = path.read_text(encoding="utf-8-sig").rstrip()
    ast.parse(source)
    relative = path.relative_to(BACKEND).as_posix()
    return (
        f"### Source: {relative}\n\n"
        f"Source of truth: [{relative}](../arkatara_backend/{relative}).\n\n"
        f"```python\n{source}\n```\n"
    )


def render(previous, date):
    prose, explanations = annotations(previous)
    models = [m for m in apps.get_models() if m._meta.app_label in APP_ORDER]
    output = [
        "# All Backend Models: Annotated Reference\n\n",
        f"Snapshot: {date}, working tree. {len(models)} project-defined concrete models.\n\n",
        "**Reference only. Do not import or run the copied source.** Models remain in their owning "
        "apps; the Django registry supplies all fields, including inherited fields. Source blocks "
        "preserve code/comments. Customer authentication, HTTP checkout, workers and providers "
        "providers are not implemented by these definitions.\n\n",
        "D-27 extends TrialBooking through paid confirmation. GuestBrowserSession owns "
        "token_digest; GuestBookingAccessGrant holds booking/browser permissions. Task 4 remains "
        "in progress; [coverage](DATA_MODEL.md#task-4-model-coverage) and "
        "[verification](reviews/TASK4_CHECKOUT_MODELS_2026-10-04.md) describe remaining scope.\n\n",
        "## Reading Map\n\n",
    ]
    for app in APP_ORDER:
        names = [
            f"[{m.__name__}](#model-{app}-{m._meta.model_name})"
            for m in models
            if m._meta.app_label == app
        ]
        output.append(
            f"- [{app}](#app-{app}): " + (", ".join(names) or "No concrete models yet.") + "\n"
        )
    output.append(
        "\n## Field Interpretation\n\nUUIDs and idempotency keys identify records/actions; "
        "they do not grant access. Null/blank/defaults are storage/validation properties, not "
        "business approval. PROTECT preserves references through ORM deletion; PostgreSQL checks "
        "and triggers supply additional guarantees. Choices alone do not create database checks. "
        "Model validation does not authenticate the caller, verify external evidence or authorize "
        "policy. Private fields must never be exposed through automatic public serialization.\n"
    )
    for app in APP_ORDER:
        output.append(f'\n<a id="app-{app}"></a>\n\n## {app}\n')
        sources = set()
        for model in [m for m in models if m._meta.app_label == app]:
            key = model._meta.label_lower
            path = Path(inspect.getfile(model)).resolve()
            sources.add(path)
            relative = path.relative_to(BACKEND).as_posix()
            output.append(
                f'\n<a id="model-{app}-{model._meta.model_name}"></a>\n\n### {model.__name__}\n\n'
                f"**Table:** `{model._meta.db_table}`. "
                f"**Source:** [{relative}](../arkatara_backend/{relative}).\n\n"
            )
            output.append(
                OVERRIDES.get(key, prose.get(key, inspect.cleandoc(model.__doc__ or ""))) + "\n\n"
            )
            output.append(
                "| Field | Type / relationship | Null / blank | "
                "Default / generated value | Explanation |\n"
                "| --- | --- | --- | --- | --- |\n"
            )
            for field in [*model._meta.fields, *model._meta.many_to_many]:
                kind = field.get_internal_type()
                if field.max_length:
                    kind += f"({field.max_length})"
                if hasattr(field, "decimal_places"):
                    kind += f"({field.max_digits},{field.decimal_places})"
                if field.is_relation:
                    kind += f" -> {field.related_model._meta.label}"
                    if getattr(field.remote_field, "on_delete", None):
                        kind += f"; {field.remote_field.on_delete.__name__}"
                if field.primary_key:
                    kind += "; PK"
                elif field.unique:
                    kind += "; unique"
                default = field.default
                if getattr(field, "auto_now_add", False):
                    default = "auto_now_add"
                elif getattr(field, "auto_now", False):
                    default = "auto_now"
                elif default is NOT_PROVIDED:
                    default = "No declared default"
                elif callable(default):
                    default = default.__module__ + "." + default.__qualname__
                else:
                    default = repr(default)
                explanation = FIELDS.get(
                    field.name,
                    explanations.get(
                        (key, field.name),
                        str(field.help_text)
                        or "Stored " + field.name.replace("_", " ") + "; see model guards below.",
                    ),
                )
                if field.name == "status":
                    explanation = "Record lifecycle only; see state guards and workflow boundaries."
                if key == "compliance.businessconfiguration" and field.name == "key":
                    explanation = "Registered setting name within namespace."
                if key == "compliance.businessconfiguration" and field.name == "scope":
                    explanation = "Configuration dimensions; registry/services use exact scope."
                if field.choices:
                    explanation = (
                        explanation.split(" Choices:")[0]
                        + " Choices: "
                        + ", ".join(f"`{v}`" for v, _ in field.flatchoices)
                        + "."
                    )
                output.append(
                    f"| `{field.name}` | {escape(kind)} | {'yes' if field.null else 'no'} / "
                    f"{'allowed' if field.blank else 'required'} | `{escape(default)}` | "
                    f"{escape(explanation)} |\n"
                )
            for title, values in (
                ("constraints", model._meta.constraints),
                ("indexes", model._meta.indexes),
            ):
                output.append(
                    f"\n**Explicit Meta {title}:** "
                    + (
                        ", ".join(f"`{v.name}`" for v in values)
                        or "None declared; field/FK and SQL guards may still apply."
                    )
                    + "\n"
                )
        if not sources:
            output.append(
                "\nPending persistence; billing's snapshot value object is included below. "
                "See the canonical coverage matrix for outstanding work.\n"
            )
        for path in sorted(sources):
            output.append("\n" + source_block(path))
    output.append('\n<a id="shared-bases-and-guards"></a>\n\n## Shared Bases and Guards\n')
    for relative in (
        "common/models.py",
        "common/reference.py",
        "common/evidence.py",
        "common/verification.py",
        "common/persistence.py",
        "common/fields.py",
    ):
        output.append("\n" + source_block(BACKEND / relative))
    output.append(
        '\n<a id="billing-value-object"></a>\n\n## Billing Value Object\n\n'
        "HistoricalPriceSnapshot is an existing immutable value contract, not a table or invoice.\n"
    )
    output.append(source_block(BACKEND / "billing/snapshots.py"))
    if '<a id="database-safeguards">' in previous:
        tail = previous.split('<a id="database-safeguards">', 1)[1].split(
            "## Snapshot Maintenance", 1
        )[0]
        output.append('\n<a id="database-safeguards">' + tail.rstrip() + "\n")
    if "Checkout extension D-27:" not in "".join(output):
        output.append(
            "\nCheckout extension D-27: [trials/0004_checkout_integrity.py]"
            "(../arkatara_backend/trials/migrations/0004_checkout_integrity.py) migrates legacy "
            "credential digests and installs identity, lifecycle, binding, reservation "
            "and monetary-allocation guards. It supersedes the earlier draft-only booking/grant "
            "triggers, retaining old history. See the checkout review for dependencies "
            "and refused evidence-losing downgrade.\n"
        )
    if "Review correction 2026-10-07:" not in "".join(output):
        output.append(
            "\nReview correction 2026-10-07: [review integrity guards]"
            "(../arkatara_backend/trials/migrations/0005_review_integrity.py) and "
            "[lifecycle/financial guards]"
            "(../arkatara_backend/trials/migrations/0006_lifecycle_financial_integrity.py) "
            "extend the retained checkpoint. [Finding validation and re-review handoff]"
            "(reviews/TASK4_FINDING_FIXES_2026-10-07.md) records model scope, checks, "
            "serialization/recovery implications and unresolved policy/live gates.\n"
        )
    if "Re-review correction 2026-10-10:" not in "".join(output):
        output.append(
            "\nRe-review correction 2026-10-10: [attributed visit guards]"
            "(../arkatara_backend/trials/migrations/0008_visit_corrections.py) and "
            "[source-bounded balance/scope guards]"
            "(../arkatara_backend/trials/migrations/0010_purchase_balance_integrity.py) "
            "with [atomic source-funded confirmation]"
            "(../arkatara_backend/trials/migrations/0011_atomic_source_funded_confirmation.py) "
            "add predispatch supersession/closure, preserved outcome corrections, explicit upfront "
            "applications and zero balance/document linkage without new collection. "
            "[Independent report and current handoff]"
            "(reviews/TASK4_REREVIEW_CORRECTIONS_2026-10-10.md) records checks and open gates. "
            "Frozen SQL is hand-maintained; the repository-owned effective-function inspector "
            "reads forward literals only.\n"
        )
    output.append(
        "\n## Snapshot Maintenance\n\nRegenerate with "
        "`python scripts/export_model_reference.py --date "
        + date
        + "`; use `--check` to detect drift. "
        "The exporter reads metadata/source without querying the database and retains earlier "
        "annotations where still applicable. Actual code and canonical decisions take precedence.\n"
    )
    return "".join(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()
    destination = BACKEND.parent / "docs/ALL_BACKEND_MODELS_REFERENCE.md"
    previous = destination.read_text(encoding="utf-8-sig")
    generated = render(previous, options.date)
    if options.check:
        if generated != previous:
            raise SystemExit("Model reference is out of sync; regenerate it.")
        print("Model reference matches registered fields and source.")
    else:
        destination.write_text(generated, encoding="utf-8", newline="\n")
        print(f"Updated {destination.name} from the Django model registry.")
