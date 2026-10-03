"""Model/database boundaries only; fictional contacts and limits are not launch policy."""

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, close_old_connections, connection, transaction
from django.db.models import QuerySet
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from accounts.models import CustomerAccount, StaffUser
from catalog.models import Category
from compliance.models import AuditEvent, PolicyDocumentRevision
from delivery.models import ArrivalOTP, DeliveryAssignment
from markets.models import Market
from trials.models import (
    BookingPhoneChallenge,
    BookingPolicyAcceptance,
    GuestBookingAccessGrant,
    TrialBooking,
    TrialBox,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def market():
    return Market.objects.create(code="IDENTITY-TEST", name="Fictional market", country_code="IN")


@pytest.fixture
def booking_data(market):
    return {
        "checkout_mode": "GUEST",
        "market": market,
        "contact_name": "Fictional recipient",
        "contact_phone": "+919000000001",
        "address": "Fictional test address",
        "country_code": "IN",
        "postal_code": "560001",
        "request_fingerprint": "a" * 64,
    }


@pytest.fixture
def booking(booking_data):
    return TrialBooking.objects.create(**booking_data)


@pytest.fixture
def staff():
    # A delivery identity need not have access to Django Admin.
    return StaffUser.objects.create_user(username="identity-agent", is_staff=False)


@pytest.fixture
def assignment(booking, staff):
    return DeliveryAssignment.objects.create(
        booking=booking,
        agent=staff,
        assigned_by=staff,
        assigned_at=timezone.now() - timedelta(minutes=1),
    )


def challenge_values(phone="+919000000001"):
    now = timezone.now()
    return {
        "phone": phone,
        "secret_hash": make_password("fictional-code"),
        "issued_at": now - timedelta(seconds=5),
        "expires_at": now + timedelta(minutes=3),
        "max_attempts": 3,
    }


def sql_rejected(sql, params):
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(sql, params)


def test_customer_is_not_staff_auth_and_profile_edits_do_not_rewrite_orders(booking_data):
    account = CustomerAccount.objects.create(display_name="Original profile")
    assert settings.AUTH_USER_MODEL == "accounts.StaffUser"
    assert not hasattr(account, "is_authenticated")
    assert not hasattr(account, "is_staff")
    assert not hasattr(account, "password")
    order = TrialBooking.objects.create(
        **{**booking_data, "checkout_mode": "ACCOUNT", "customer_account": account}
    )
    account.display_name = "Changed profile"
    account.save()
    order.refresh_from_db()
    assert order.contact_name == "Fictional recipient"
    assert order.customer_account_id == account.pk
    with pytest.raises(ProtectedError):
        QuerySet(model=CustomerAccount).filter(pk=account.pk).delete()


def test_customer_closure_preserves_identity_and_history(booking_data):
    account = CustomerAccount.objects.create()
    order = TrialBooking.objects.create(
        **{**booking_data, "checkout_mode": "ACCOUNT", "customer_account": account}
    )
    account.status = "CLOSED"
    account.closed_at = timezone.now()
    account.save()
    order.refresh_from_db()
    assert order.checkout_mode == "ACCOUNT" and order.customer_account_id == account.pk
    account.status, account.closed_at = "ACTIVE", None
    with pytest.raises(ValidationError):
        account.save()
    sql_rejected(
        "UPDATE accounts_customeraccount SET status='ACTIVE', closed_at=NULL WHERE id=%s",
        [account.pk],
    )
    sql_rejected("DELETE FROM accounts_customeraccount WHERE id=%s", [account.pk])


@pytest.mark.parametrize(
    "mode,has_account", [("GUEST", True), ("ACCOUNT", False), ("OTHER", False)]
)
def test_invalid_checkout_ownership_rejected(booking_data, mode, has_account):
    data = {**booking_data, "checkout_mode": mode}
    if has_account:
        data["customer_account"] = CustomerAccount.objects.create()
    with pytest.raises(ValidationError):
        TrialBooking.objects.create(**data)
    # Bypass model validation to prove the database check is authoritative too.
    with pytest.raises(IntegrityError), transaction.atomic():
        QuerySet(model=TrialBooking).bulk_create([TrialBooking(**data)])


def test_guest_cannot_be_linked_even_when_mode_and_fk_change_together(booking):
    account = CustomerAccount.objects.create()
    booking.checkout_mode, booking.customer_account = "ACCOUNT", account
    with pytest.raises(ValidationError):
        booking.save()
    with pytest.raises(TypeError):
        TrialBooking.objects.filter(pk=booking.pk).update(
            checkout_mode="ACCOUNT", customer_account=account
        )
    sql_rejected(
        "UPDATE trials_trialbooking SET checkout_mode='ACCOUNT', "
        "customer_account_id=%s WHERE id=%s",
        [account.pk, booking.pk],
    )
    booking.refresh_from_db()
    assert booking.checkout_mode == "GUEST" and booking.customer_account_id is None


def test_same_phone_is_not_an_account_link_or_a_unique_booking(booking_data, booking):
    customer = CustomerAccount.objects.create()
    second = TrialBooking.objects.create(**booking_data)
    third = TrialBooking.objects.create(
        **{**booking_data, "checkout_mode": "ACCOUNT", "customer_account": customer}
    )
    assert booking.contact_phone == second.contact_phone == third.contact_phone
    assert list(customer.bookings.values_list("pk", flat=True)) == [third.pk]
    assert TrialBooking.objects.filter(customer_account__isnull=True).count() == 2


def test_account_owned_booking_cannot_be_reassigned_or_converted_to_guest(booking_data):
    first = CustomerAccount.objects.create()
    second = CustomerAccount.objects.create()
    booking = TrialBooking.objects.create(
        **{**booking_data, "checkout_mode": "ACCOUNT", "customer_account": first}
    )
    booking.customer_account = second
    with pytest.raises(ValidationError):
        booking.save()
    sql_rejected(
        "UPDATE trials_trialbooking SET customer_account_id=%s WHERE id=%s",
        [second.pk, booking.pk],
    )
    sql_rejected(
        "UPDATE trials_trialbooking SET customer_account_id=NULL,checkout_mode='GUEST' WHERE id=%s",
        [booking.pk],
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("contact_name", " "),
        ("address", ""),
        ("contact_phone", "9000000001"),
        ("postal_code", "000000"),
        ("country_code", "US"),
        ("request_fingerprint", "not-a-digest"),
        ("status", "CONFIRMED"),
    ],
)
def test_required_snapshots_and_unimplemented_confirmation_fail_closed(booking_data, field, value):
    with pytest.raises(ValidationError):
        TrialBooking.objects.create(**{**booking_data, field: value})
    with pytest.raises(IntegrityError), transaction.atomic():
        QuerySet(model=TrialBooking).bulk_create([TrialBooking(**{**booking_data, field: value})])


def test_snapshot_identity_and_request_are_immutable(booking):
    for field, value in (("contact_phone", "+919000000002"), ("address", "Another address")):
        setattr(booking, field, value)
        with pytest.raises(ValidationError):
            booking.save()
        booking.refresh_from_db()
        sql_rejected(f"UPDATE trials_trialbooking SET {field}=%s WHERE id=%s", [value, booking.pk])
    sql_rejected("DELETE FROM trials_trialbooking WHERE id=%s", [booking.pk])
    clone = TrialBooking.objects.get(pk=booking.pk)
    clone._state.adding = True
    with pytest.raises(ValidationError):
        clone.save()


def test_booking_request_identity_cannot_be_replayed_as_a_new_record(booking, booking_data):
    with pytest.raises(ValidationError):
        TrialBooking.objects.create(**booking_data, request_id=booking.request_id)


def test_category_boxes_share_one_booking_without_unapproved_category_uniqueness(booking):
    category = Category.objects.create(code="IDENTITY-RINGS", name="Fictional rings")
    first = TrialBox.objects.create(booking=booking, category=category)
    TrialBox.objects.create(booking=booking, category=category)
    assert booking.boxes.count() == 2
    with pytest.raises(TypeError):
        TrialBox.objects.filter(pk=first.pk).delete()
    sql_rejected("DELETE FROM trials_trialbox WHERE id=%s", [first.pk])


def test_policy_revision_and_guest_acceptance_preserve_content_without_customer_link(booking):
    policy = PolicyDocumentRevision.objects.create(
        document_code="fictional-terms",
        language_code="en",
        revision=1,
        content="Fictional test-only policy, not approved terms.",
    )
    assert policy.content_sha256 == hashlib.sha256(policy.content.encode()).hexdigest()
    acceptance = BookingPolicyAcceptance.objects.create(
        booking=booking,
        policy_revision=policy,
        accepted_at=timezone.now(),
        request_id=uuid.uuid4(),
        request_fingerprint="b" * 64,
    )
    assert "customer_account" not in {f.name for f in acceptance._meta.fields}
    with pytest.raises(ValidationError):
        policy.save()
    with pytest.raises(ValidationError):
        acceptance.save()
    sql_rejected(
        "UPDATE compliance_policydocumentrevision SET content='changed' WHERE id=%s", [policy.pk]
    )
    sql_rejected("DELETE FROM trials_bookingpolicyacceptance WHERE id=%s", [acceptance.pk])
    assert AuditEvent.objects.count() == 0  # A model write does not claim a workflow audit.


def test_guest_grant_is_scoped_and_revocation_is_irreversible(booking, booking_data):
    now = timezone.now()
    grant = GuestBookingAccessGrant.objects.create(
        booking=booking,
        token_digest="c" * 64,
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    customer = CustomerAccount.objects.create()
    owned = TrialBooking.objects.create(
        **{**booking_data, "checkout_mode": "ACCOUNT", "customer_account": customer}
    )
    with pytest.raises(ValidationError):
        GuestBookingAccessGrant.objects.create(
            booking=owned,
            token_digest="d" * 64,
            issued_at=now,
            expires_at=now + timedelta(minutes=10),
        )
    sql_rejected(
        "UPDATE trials_guestbookingaccessgrant SET booking_id=%s WHERE id=%s", [owned.pk, grant.pk]
    )
    grant.revoked_at = timezone.now()
    grant.save()
    grant.revoked_at = None
    with pytest.raises(ValidationError):
        grant.save()
    sql_rejected(
        "UPDATE trials_guestbookingaccessgrant SET revoked_at=NULL WHERE id=%s", [grant.pk]
    )


@pytest.mark.parametrize("kind", ["booking", "arrival"])
def test_challenge_binding_attempts_and_consumption_are_monotonic(booking, assignment, kind):
    model = BookingPhoneChallenge if kind == "booking" else ArrivalOTP
    subject = {"booking": booking} if kind == "booking" else {"assignment": assignment}
    challenge = model.objects.create(**subject, **challenge_values())
    assert challenge.secret_hash != "fictional-code"
    challenge.attempts_used = 1
    challenge.verified_at = timezone.now()
    challenge.save()
    stale = model.objects.get(pk=challenge.pk)
    challenge.consumed_at = timezone.now()
    challenge.save()
    stale.consumed_at = timezone.now()
    with pytest.raises(ValidationError):
        stale.save()
    sql_rejected(f"UPDATE {model._meta.db_table} SET consumed_at=NULL WHERE id=%s", [challenge.pk])
    sql_rejected(
        f"UPDATE {model._meta.db_table} SET phone=%s WHERE id=%s", ["+919000000002", challenge.pk]
    )
    assert BookingPhoneChallenge._meta.db_table != ArrivalOTP._meta.db_table


@pytest.mark.parametrize(
    "overrides",
    [
        {"phone": "+919000000002"},
        {"secret_hash": "123456"},
        {"secret_hash": "md5$broken"},
        {"max_attempts": 0},
        {"attempts_used": 1},
        {"verified_at": timezone.now()},
        {"expires_at": timezone.now() - timedelta(days=1)},
    ],
)
def test_invalid_challenge_cannot_be_created(booking, overrides):
    with pytest.raises(ValidationError):
        BookingPhoneChallenge.objects.create(booking=booking, **{**challenge_values(), **overrides})


def test_exhausted_or_invalidated_challenge_cannot_be_verified(booking):
    challenge = BookingPhoneChallenge.objects.create(booking=booking, **challenge_values())
    challenge.attempts_used = challenge.max_attempts
    challenge.save()
    challenge.verified_at = timezone.now()
    with pytest.raises(ValidationError):
        challenge.save()
    sql_rejected(
        "UPDATE trials_bookingphonechallenge SET verified_at=now() WHERE id=%s", [challenge.pk]
    )
    challenge.refresh_from_db()
    challenge.invalidated_at = timezone.now()
    challenge.save()
    sql_rejected(
        "UPDATE trials_bookingphonechallenge SET invalidated_at=NULL WHERE id=%s", [challenge.pk]
    )


def test_assignment_history_is_not_overwritten_and_agent_need_not_be_admin(assignment, staff):
    assert staff.is_staff is False
    replacement = StaffUser.objects.create_user(username="replacement-agent")
    with pytest.raises(ValidationError):
        DeliveryAssignment.objects.create(
            booking=assignment.booking,
            agent=replacement,
            assigned_by=staff,
            assigned_at=timezone.now(),
        )
    assignment.agent = replacement
    with pytest.raises(ValidationError):
        assignment.save()
    assignment.refresh_from_db()
    assignment.ended_at, assignment.end_reason = timezone.now(), "Fictional reassignment"
    assignment.save()
    new = DeliveryAssignment.objects.create(
        booking=assignment.booking,
        agent=replacement,
        assigned_by=staff,
        assigned_at=timezone.now(),
    )
    assert new.pk != assignment.pk
    assert assignment.booking.assignments.count() == 2
    sql_rejected(
        "UPDATE delivery_deliveryassignment SET ended_at=NULL,end_reason='' WHERE id=%s",
        [assignment.pk],
    )
    sql_rejected(
        "UPDATE delivery_deliveryassignment SET agent_id=%s WHERE id=%s", [staff.pk, new.pk]
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_assignment_inserts_cannot_bypass_one_open_assignment(booking, staff):
    barrier = Barrier(2)

    def insert():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            with transaction.atomic():
                QuerySet(model=DeliveryAssignment).bulk_create(
                    [
                        DeliveryAssignment(
                            booking_id=booking.pk,
                            agent_id=staff.pk,
                            assigned_by_id=staff.pk,
                            assigned_at=timezone.now(),
                        )
                    ]
                )
            return "created"
        except IntegrityError:
            return "conflict"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: insert(), range(2)))
    assert sorted(results) == ["conflict", "created"]


def test_raw_sql_wrong_phone_cannot_substitute_for_binding(booking):
    data = {**challenge_values(), "booking": booking, "phone": "+919000000002"}
    with pytest.raises(IntegrityError), transaction.atomic():
        QuerySet(model=BookingPhoneChallenge).bulk_create([BookingPhoneChallenge(**data)])


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["booking", "arrival"])
def test_concurrent_challenge_consumption_keeps_only_one_recorded_outcome(
    booking, assignment, kind
):
    model = BookingPhoneChallenge if kind == "booking" else ArrivalOTP
    subject = {"booking": booking} if kind == "booking" else {"assignment": assignment}
    challenge = model.objects.create(**subject, **challenge_values())
    challenge.attempts_used = 1
    challenge.verified_at = timezone.now()
    challenge.save()
    barrier = Barrier(2)

    def consume(offset):
        close_old_connections()
        try:
            stale = model.objects.get(pk=challenge.pk)
            # Distinct fictional timestamps make competing evidence unambiguous.
            stale.consumed_at = challenge.verified_at + timedelta(microseconds=offset)
            barrier.wait(timeout=10)
            stale.save()
            return "recorded"
        except ValidationError:
            return "rejected"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, [1, 2]))
    assert sorted(results) == ["recorded", "rejected"]
    challenge.refresh_from_db()
    assert challenge.attempts_used == 1
    assert challenge.consumed_at is not None


def test_sql_cannot_consume_challenge_at_or_after_expiry(booking):
    challenge = BookingPhoneChallenge.objects.create(booking=booking, **challenge_values())
    challenge.attempts_used = 1
    challenge.verified_at = timezone.now()
    challenge.save()
    sql_rejected(
        "UPDATE trials_bookingphonechallenge SET consumed_at=expires_at WHERE id=%s",
        [challenge.pk],
    )
