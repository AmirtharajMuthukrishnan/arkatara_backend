"""Historical sale and document facts; tax/commercial policies remain externally reviewed."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q, Sum
from django.utils import timezone

from common.evidence import AppendOnlyModel
from common.fields import MinorUnitField, SignedMinorUnitField
from common.persistence import LifecycleModel, currency_validator, money_constraints
from common.verification import validate_observed_at


class TaxEvidence(AppendOnlyModel):
    """Explicitly unconfigured treatment is never interpreted as zero tax.

    Snapshots retain entity/classification, component and calculation-policy facts.
    Refs: BUSINESS_RULES.md R-27, R-28, R-31; DECISIONS.md CA-01, CA-02, CA-03.
    """

    status = models.CharField(
        max_length=16, choices=[("UNCONFIGURED", "Unconfigured"), ("REVIEWED", "Reviewed")]
    )
    treatment_snapshot = models.JSONField(default=dict, blank=True)
    review_reference = models.CharField(max_length=500, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    reviewed_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    status="UNCONFIGURED",
                    treatment_snapshot={},
                    review_reference="",
                    reviewed_by__isnull=True,
                    reviewed_at__isnull=True,
                )
                | (
                    Q(status="REVIEWED", reviewed_by__isnull=False, reviewed_at__isnull=False)
                    & ~Q(treatment_snapshot={})
                    & ~Q(review_reference="")
                ),
                name="tax_review_evidence",
            )
        ]


class Purchase(LifecycleModel):
    """Versioned authoritative selection; gross jewellery value stays separate from balance.

    No agent pricing override or implicit deposit deduction is implemented here.
    Refs: BUSINESS_RULES.md R-16, R-19, R-25, R-27, R-28; DECISIONS.md BD-04, CA-02.
    """

    booking = models.ForeignKey(
        "trials.TrialBooking", on_delete=models.PROTECT, related_name="purchases"
    )
    revision = models.PositiveIntegerField()
    seller = models.ForeignKey("compliance.LegalEntity", on_delete=models.PROTECT)
    seller_snapshot = models.JSONField()
    customer_snapshot = models.JSONField()
    tax_evidence = models.ForeignKey(TaxEvidence, on_delete=models.PROTECT)
    currency = models.CharField(max_length=3, validators=[currency_validator])
    jewellery_total_minor = MinorUnitField()
    payable_minor = MinorUnitField()
    balance_snapshot = models.JSONField()
    status = models.CharField(
        max_length=16,
        default="DRAFT",
        choices=[(s, s.title()) for s in ("DRAFT", "SEALED", "CONFIRMED", "VOID")],
    )
    sealed_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    confirmed_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    mutable_fields = ("status", "sealed_at", "confirmed_at")
    write_once_fields = mutable_fields[1:]
    initial_status = "DRAFT"
    transitions = {"DRAFT": ("SEALED", "VOID"), "SEALED": ("CONFIRMED", "VOID")}

    class Meta(LifecycleModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["booking", "revision"], name="purchase_revision"),
            models.CheckConstraint(condition=Q(revision__gt=0), name="purchase_revision_positive"),
            models.CheckConstraint(
                condition=Q(jewellery_total_minor__gt=0, payable_minor__gte=0),
                name="purchase_positive_totals",
            ),
            models.CheckConstraint(
                condition=Q(currency__regex=r"^[A-Z]{3}$"), name="purchase_currency"
            ),
            models.CheckConstraint(
                condition=Q(status="CONFIRMED", confirmed_at__isnull=False)
                | (~Q(status="CONFIRMED") & Q(confirmed_at__isnull=True)),
                name="purchase_confirmed_time",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=["SEALED", "CONFIRMED"]) | Q(sealed_at__isnull=False),
                name="purchase_sealed_time",
            ),
        ]


class PurchaseBalanceEvidence(AppendOnlyModel):
    """Attributable calculation basis before applying previously collected funds.

    Adjustments need explicit reviewed evidence; no adjustment or credit is seeded.
    The source application changes the outstanding balance, not jewellery value.
    Refs: BUSINESS_RULES.md R-07, R-19, R-25, R-28, R-30; DECISIONS.md BD-01, CA-01, CA-02.
    """

    purchase = models.OneToOneField(
        Purchase, on_delete=models.PROTECT, related_name="balance_evidence"
    )
    amount_due_before_application = MinorUnitField()
    adjustment_minor = SignedMinorUnitField()
    currency = models.CharField(max_length=3, validators=[currency_validator])
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    observed_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])
    reason = models.TextField()
    evidence_reference = models.CharField(max_length=500)
    calculation_snapshot = models.JSONField()
    policy_snapshot = models.JSONField()

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(amount_due_before_application__gte=0), name="balance_basis_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(currency__regex=r"^[A-Z]{3}$"), name="balance_evidence_currency"
            ),
        ]

    def lock_related_records(self, database):
        if self.purchase_id:
            self.purchase = (
                Purchase.objects.using(database).select_for_update().get(pk=self.purchase_id)
            )

    def clean(self):
        super().clean()
        _validate_balance_observation(self)
        if self.purchase_id and (
            self.purchase.status != "DRAFT"
            or self.currency != self.purchase.currency
            or self.amount_due_before_application
            != self.purchase.jewellery_total_minor + self.adjustment_minor
        ):
            raise ValidationError("Balance evidence must explain the current draft purchase value.")


class UpfrontApplication(AppendOnlyModel):
    """Explicit use of a collected upfront allocation toward one purchase balance.

    Locked source totals prevent the same funds funding two purchases. A voided
    purchase does not restore credit implicitly; reversal/refund policy stays gated.
    Refs: BUSINESS_RULES.md R-25, R-26, R-28, R-30; DECISIONS.md BD-01, CA-01.
    """

    balance_evidence = models.ForeignKey(
        PurchaseBalanceEvidence, on_delete=models.PROTECT, related_name="upfront_applications"
    )
    source_allocation = models.ForeignKey(
        "payments.PaymentAllocation", on_delete=models.PROTECT, related_name="purchase_applications"
    )
    amount_minor = MinorUnitField()
    currency = models.CharField(max_length=3, validators=[currency_validator])
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    observed_at = models.DateTimeField(default=timezone.now, validators=[validate_observed_at])
    reason = models.TextField()
    evidence_reference = models.CharField(max_length=500)
    policy_snapshot = models.JSONField()
    idempotency_key = models.UUIDField(unique=True)

    class Meta(AppendOnlyModel.Meta):
        constraints = [*money_constraints("upfront_application")]

    def lock_related_records(self, database):
        from payments.models import PaymentAllocation

        if self.source_allocation_id:
            self.source_allocation = (
                PaymentAllocation.objects.using(database)
                .select_for_update(of=("self",))
                .select_related("booking_charge", "gateway_transaction")
                .get(pk=self.source_allocation_id)
            )
        if self.balance_evidence_id:
            self.balance_evidence = (
                PurchaseBalanceEvidence.objects.using(database)
                .select_for_update()
                .select_related("purchase")
                .get(pk=self.balance_evidence_id)
            )
            list(
                Purchase.objects.using(database)
                .select_for_update()
                .filter(pk=self.balance_evidence.purchase_id)
            )

    def clean(self):
        super().clean()
        _validate_balance_observation(self)
        if not self.source_allocation_id or not self.balance_evidence_id:
            return
        source = self.source_allocation
        basis = self.balance_evidence
        purchase = basis.purchase
        effective_attempt_id = source.gateway_transaction.attempt_id
        if effective_attempt_id is None:
            from payments.models import GatewayTransactionMatch

            effective_attempt_id = (
                GatewayTransactionMatch.objects.filter(
                    gateway_transaction=source.gateway_transaction
                )
                .values_list("attempt_id", flat=True)
                .first()
            )
        if (
            purchase.status != "DRAFT"
            or source.booking_charge_id is None
            or source.purchase_obligation_id is not None
            or source.gateway_transaction.transaction_kind != "CAPTURE"
            or source.booking_charge.status != "COLLECTED"
            or not source.booking_charge.contexts.filter(
                status="PAID", attempts__id=effective_attempt_id
            ).exists()
            or source.booking_charge.booking_id != purchase.booking_id
            or source.booking_charge.payee_legal_entity_id != purchase.seller_id
            or self.currency != source.currency
            or self.currency != basis.currency
            or self.observed_at < source.allocated_at
            or self.observed_at < basis.observed_at
        ):
            raise ValidationError(
                "Upfront application requires collected funds for this draft sale."
            )
        applied_source = (
            source.purchase_applications.aggregate(total=Sum("amount_minor"))["total"] or 0
        )
        applied_purchase = (
            basis.upfront_applications.aggregate(total=Sum("amount_minor"))["total"] or 0
        )
        if (
            applied_source + self.amount_minor > source.amount_minor
            or applied_purchase + self.amount_minor > basis.amount_due_before_application
        ):
            raise ValidationError(
                "Upfront application exceeds the available source or purchase balance."
            )


def _validate_balance_observation(record):
    if (
        not record.reason.strip()
        or not record.evidence_reference.strip()
        or not isinstance(record.policy_snapshot, dict)
        or not record.policy_snapshot
        or not record.actor_id
        or not record.actor.is_active
    ):
        raise ValidationError("Balance records require attributable reviewed calculation evidence.")
    if isinstance(record, PurchaseBalanceEvidence) and (
        not isinstance(record.calculation_snapshot, dict) or not record.calculation_snapshot
    ):
        raise ValidationError("Balance records require explicit calculation components.")


class PurchaseItem(AppendOnlyModel):
    """Exact selected piece and historical description/price/tax inputs.

    Refs: BUSINESS_RULES.md R-11, R-27.
    """

    purchase = models.ForeignKey(Purchase, on_delete=models.PROTECT, related_name="items")
    reservation = models.ForeignKey("inventory.InventoryReservation", on_delete=models.PROTECT)
    line_number = models.PositiveIntegerField()
    amount_minor = MinorUnitField()
    currency = models.CharField(max_length=3, validators=[currency_validator])
    product_snapshot = models.JSONField()
    price_snapshot = models.JSONField()

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            *money_constraints("purchase_item"),
            models.UniqueConstraint(
                fields=["purchase", "line_number"], name="purchase_line_number"
            ),
            models.UniqueConstraint(fields=["purchase", "reservation"], name="purchase_piece_once"),
            models.CheckConstraint(condition=Q(line_number__gt=0), name="purchase_line_positive"),
        ]


class PurchaseObligation(LifecycleModel):
    """Final balance obligation, independent of the upfront booking charge.

    A fully source-funded zero balance still preserves financial/document identity;
    it never authorizes a zero-value provider collection context or attempt.

    Refs: BUSINESS_RULES.md R-23, R-25, R-26.
    """

    purchase = models.OneToOneField(Purchase, on_delete=models.PROTECT, related_name="obligation")
    amount_minor = MinorUnitField()
    currency = models.CharField(max_length=3, validators=[currency_validator])
    quote_expires_at = models.DateTimeField()
    status = models.CharField(
        max_length=16,
        default="QUOTED",
        choices=[(s, s.title()) for s in ("QUOTED", "DUE", "COLLECTED", "VOID")],
    )
    accepted_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    mutable_fields = ("status", "accepted_at")
    write_once_fields = ("accepted_at",)
    initial_status = "QUOTED"
    transitions = {"QUOTED": ("DUE", "VOID"), "DUE": ("COLLECTED", "VOID")}

    class Meta(LifecycleModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(amount_minor__gte=0), name="purchase_obligation_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(currency__regex=r"^[A-Z]{3}$"), name="purchase_obligation_currency"
            ),
            models.CheckConstraint(
                condition=Q(accepted_at__isnull=True, status__in=["QUOTED", "VOID"])
                | Q(
                    accepted_at__isnull=False,
                    accepted_at__lt=F("quote_expires_at"),
                    status__in=["DUE", "COLLECTED", "VOID"],
                ),
                name="purchase_obligation_acceptance",
            ),
        ]


class DocumentSeries(AppendOnlyModel):
    """Reviewed numbering scope; no legal format/period is selected here.

    Refs: BUSINESS_RULES.md R-27; DECISIONS.md CA-02.
    """

    issuer = models.ForeignKey("compliance.LegalEntity", on_delete=models.PROTECT)
    code = models.CharField(max_length=128)
    scope_snapshot = models.JSONField()
    review_reference = models.CharField(max_length=500)

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["issuer", "code"], name="document_series_identity")
        ]


class FinancialDocument(LifecycleModel):
    """Draft then sealed header/lines for reviewed dispatch, upfront, sale or correction records.

    An issued document does not prove payment. Tax treatment is retained separately.
    Refs: BUSINESS_RULES.md R-27, R-28, R-29, R-31; DECISIONS.md CA-01, CA-02, LR-03.
    """

    booking = models.ForeignKey("trials.TrialBooking", on_delete=models.PROTECT)
    booking_charge = models.ForeignKey(
        "payments.BookingCharge", null=True, blank=True, on_delete=models.PROTECT
    )
    purchase_obligation = models.ForeignKey(
        PurchaseObligation, null=True, blank=True, on_delete=models.PROTECT
    )
    corrects = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    kind = models.CharField(
        max_length=24,
        choices=[
            (s, s.replace("_", " ").title())
            for s in ("DISPATCH", "UPFRONT_RECORD", "TAX_INVOICE", "ADJUSTMENT")
        ],
    )
    series = models.ForeignKey(DocumentSeries, on_delete=models.PROTECT)
    number = models.PositiveBigIntegerField(null=True, blank=True)
    issuer_snapshot = models.JSONField()
    recipient_snapshot = models.JSONField()
    tax_evidence = models.ForeignKey(TaxEvidence, on_delete=models.PROTECT)
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, validators=[currency_validator])
    status = models.CharField(
        max_length=16, default="DRAFT", choices=[("DRAFT", "Draft"), ("ISSUED", "Issued")]
    )
    issued_at = models.DateTimeField(null=True, blank=True, validators=[validate_observed_at])
    review_reference = models.CharField(max_length=500)
    mutable_fields = ("status", "number", "issued_at")
    write_once_fields = ("number", "issued_at")
    initial_status = "DRAFT"
    transitions = {"DRAFT": ("ISSUED",)}

    class Meta(LifecycleModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["series", "number"],
                condition=Q(number__isnull=False),
                name="issued_document_number",
            ),
            models.CheckConstraint(
                condition=Q(status="DRAFT", number__isnull=True, issued_at__isnull=True)
                | Q(status="ISSUED", number__gt=0, issued_at__isnull=False),
                name="document_issue_evidence",
            ),
            models.CheckConstraint(
                condition=Q(
                    booking_charge__isnull=True, purchase_obligation__isnull=True, kind="DISPATCH"
                )
                | Q(
                    booking_charge__isnull=False,
                    purchase_obligation__isnull=True,
                    kind__in=["UPFRONT_RECORD", "ADJUSTMENT"],
                )
                | Q(
                    booking_charge__isnull=True,
                    purchase_obligation__isnull=False,
                    kind__in=["TAX_INVOICE", "ADJUSTMENT"],
                ),
                name="document_obligation_kind",
            ),
        ]


class FinancialDocumentLine(AppendOnlyModel):
    document = models.ForeignKey(FinancialDocument, on_delete=models.PROTECT, related_name="lines")
    line_number = models.PositiveIntegerField()
    amount_minor = models.PositiveBigIntegerField()
    snapshot = models.JSONField()
    reservation = models.ForeignKey(
        "inventory.InventoryReservation", null=True, blank=True, on_delete=models.PROTECT
    )
    purchase_item = models.ForeignKey(PurchaseItem, null=True, blank=True, on_delete=models.PROTECT)

    class Meta(AppendOnlyModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["document", "reservation"],
                condition=Q(reservation__isnull=False),
                name="dispatch_document_piece",
            ),
            models.UniqueConstraint(
                fields=["document", "purchase_item"],
                condition=Q(purchase_item__isnull=False),
                name="invoice_document_piece",
            ),
            models.UniqueConstraint(
                fields=["document", "line_number"], name="document_line_number"
            ),
            models.CheckConstraint(condition=Q(line_number__gt=0), name="document_line_positive"),
        ]
