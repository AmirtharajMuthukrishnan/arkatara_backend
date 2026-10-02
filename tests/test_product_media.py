"""Fictional local storage configuration; no production upload limits are selected."""

import struct
import uuid
import zlib

import pytest
from django.contrib.auth.models import AnonymousUser, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client
from django.urls import reverse

from accounts.models import StaffUser
from catalog.media import ProductMedia
from catalog.media_forms import ProductMediaUploadForm
from catalog.media_services import (
    media_download_response,
    product_media_metadata,
    update_product_media_metadata,
    upload_product_media,
    validate_media_upload,
)
from catalog.models import Category, Product
from catalog.storage import LocalObjectStorage, MediaStorageUnavailable, get_media_storage
from compliance.models import AuditEvent


def png_bytes():
    def chunk(kind, value):
        return (
            struct.pack(">I", len(value))
            + kind
            + value
            + struct.pack(">I", zlib.crc32(kind + value))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def upload(content=None, name="fixture.png", content_type="image/png"):
    return SimpleUploadedFile(name, png_bytes() if content is None else content, content_type)


@pytest.fixture
def configured_media(settings, tmp_path):
    settings.PRODUCT_MEDIA_STORAGE = "LOCAL"
    settings.PRODUCT_MEDIA_ROOT = tmp_path / "private-catalogue-media"
    settings.PRODUCT_MEDIA_MAX_BYTES = 4096
    settings.PRODUCT_MEDIA_ALLOWED_TYPES = ["image/png", "image/jpeg", "video/mp4"]
    return settings.PRODUCT_MEDIA_ROOT


@pytest.fixture
def media_graph():
    staff = StaffUser.objects.create_superuser(username="media-fixture", password="fixture-only")
    category = Category.objects.create(code="MEDIA-FIXTURE", name="Media fixture")
    product = Product.objects.create(code="MEDIA-PRODUCT", name="Media product", category=category)
    other = Product.objects.create(
        code="OTHER-MEDIA-PRODUCT", name="Other design", category=category
    )
    return staff, product, other


def onboard(staff, product, **overrides):
    values = {
        "actor": staff,
        "product": product,
        "uploaded_file": upload(),
        "role": "MAIN",
        "alt_text": "Fictional product on plain background",
        "reason": "Fictional onboarding",
    }
    return upload_product_media(**(values | overrides))


def test_storage_and_limits_are_explicitly_unconfigured(settings):
    settings.PRODUCT_MEDIA_STORAGE = "UNCONFIGURED"
    settings.PRODUCT_MEDIA_MAX_BYTES = None
    settings.PRODUCT_MEDIA_ALLOWED_TYPES = None
    with pytest.raises(MediaStorageUnavailable):
        get_media_storage()
    with pytest.raises(ValidationError, match="byte limit"):
        validate_media_upload(upload())


@pytest.mark.parametrize("environment", ["staging", "production", "base"])
def test_direct_local_storage_construction_rejects_nonlocal_environment(
    settings, tmp_path, environment
):
    settings.ENVIRONMENT = environment
    with pytest.raises(MediaStorageUnavailable, match="local/test"):
        LocalObjectStorage(tmp_path)


@pytest.mark.parametrize(
    "key",
    [
        "../private.txt",
        "C:/private.png",
        "/private.png",
        "products\\asset.png",
        "https://host/asset.png",
    ],
)
def test_storage_rejects_unowned_paths(configured_media, key):
    storage = get_media_storage()
    for operation in (
        lambda: storage.open(key),
        lambda: storage.put(key, b"x"),
        lambda: storage.delete(key),
    ):
        with pytest.raises(ValidationError):
            operation()


def test_storage_never_overwrites_existing_objects(configured_media):
    storage = get_media_storage()
    key = f"products/{uuid.uuid4()}/{uuid.uuid4()}.png"
    storage.put(key, b"original")
    with pytest.raises(FileExistsError):
        storage.put(key, b"replacement")
    with storage.open(key) as existing:
        assert existing.read() == b"original"


def test_validator_detects_bytes_and_resets_upload_pointer(configured_media):
    candidate = upload()
    validated = validate_media_upload(candidate)
    assert validated.content == png_bytes()
    assert validated.content_type == "image/png"
    assert candidate.tell() == 0


@pytest.mark.parametrize(
    ("content", "name", "content_type"),
    [
        (b"<svg onload='alert(1)'/>", "fixture.svg", "image/svg+xml"),
        (b"<html>arbitrary</html>", "fixture.png", "image/png"),
        (png_bytes(), "fixture.jpg", "image/jpeg"),
        (png_bytes(), "fixture.png", "text/html"),
        (png_bytes() + b"<script>bad</script>", "fixture.png", "image/png"),
        (png_bytes()[:-4], "fixture.png", "image/png"),
        (b"", "fixture.png", "image/png"),
    ],
)
def test_invalid_or_disguised_media_is_rejected(configured_media, content, name, content_type):
    with pytest.raises(ValidationError):
        validate_media_upload(upload(content, name, content_type))


def test_actual_bytes_cannot_bypass_configured_limit(configured_media, settings):
    settings.PRODUCT_MEDIA_MAX_BYTES = 20
    candidate = upload()
    candidate.size = 1
    with pytest.raises(ValidationError, match="byte limit"):
        validate_media_upload(candidate)


def test_mime_allowlist_must_be_explicit_and_supported(configured_media, settings):
    for value in (None, [], ["text/html"], [["image/png"]]):
        settings.PRODUCT_MEDIA_ALLOWED_TYPES = value
        with pytest.raises(ValidationError, match="types"):
            validate_media_upload(upload())


@pytest.mark.django_db(transaction=True)
def test_upload_has_private_metadata_and_atomic_attribution(configured_media, media_graph):
    staff, product, _ = media_graph
    media = onboard(staff, product)
    assert media.status == "DRAFT"
    assert media.object_key.startswith(f"products/{product.public_id}/")
    assert (configured_media / media.object_key).read_bytes() == png_bytes()
    assert (
        AuditEvent.objects.filter(resource_identifier=str(media.public_id), actor=staff).count()
        == 1
    )
    metadata = product_media_metadata(media)
    assert metadata["delivery"] == {"state": "PRIVATE"}
    assert "object_key" not in metadata and "url" not in metadata
    with pytest.raises(TypeError):
        media.save()
    response = media_download_response(actor=staff, media=media)
    assert response["Content-Disposition"].startswith("attachment;")
    assert response["Cache-Control"] == "private, no-store"
    assert b"".join(response.streaming_content) == png_bytes()
    response.close()


@pytest.mark.django_db(transaction=True)
def test_permission_checks_live_inside_services(configured_media, media_graph):
    staff, product, _ = media_graph
    media = onboard(staff, product)
    unauthorized = StaffUser.objects.create_user(username="unprivileged-media-staff", is_staff=True)
    inactive = StaffUser.objects.create_superuser(username="inactive-media-staff", is_active=False)
    for actor in (AnonymousUser(), unauthorized, inactive):
        with pytest.raises(PermissionDenied):
            onboard(actor, product, role="ADDITIONAL")
        with pytest.raises(PermissionDenied):
            media_download_response(actor=actor, media=media)
        with pytest.raises(PermissionDenied):
            update_product_media_metadata(
                actor=actor,
                media=media,
                alt_text="changed",
                sort_order=0,
                status="RETIRED",
                reason="fixture",
            )


@pytest.mark.django_db(transaction=True)
def test_failed_audit_removes_only_new_unreferenced_object(
    configured_media, media_graph, monkeypatch
):
    staff, product, _ = media_graph
    existing = onboard(staff, product)

    def fail_audit(**kwargs):
        raise RuntimeError("Fictional audit failure")

    monkeypatch.setattr("catalog.media_services.record_model_change", fail_audit)
    with pytest.raises(RuntimeError, match="audit failure"):
        onboard(staff, product, role="ADDITIONAL")
    assert ProductMedia.objects.count() == 1
    assert list(configured_media.rglob("*.png")) == [configured_media / existing.object_key]
    assert (configured_media / existing.object_key).read_bytes() == png_bytes()


@pytest.mark.django_db(transaction=True)
def test_upload_refuses_outer_transaction_that_could_orphan_file(configured_media, media_graph):
    staff, product, _ = media_graph
    with transaction.atomic(), pytest.raises(ValidationError, match="own its database transaction"):
        onboard(staff, product)
    assert not ProductMedia.objects.exists()
    assert not configured_media.exists()


@pytest.mark.django_db(transaction=True)
def test_source_scope_role_and_main_uniqueness(configured_media, media_graph):
    staff, product, other = media_graph
    main = onboard(staff, product)
    with pytest.raises(ValidationError):
        onboard(staff, product)
    with pytest.raises(ValidationError, match="this design"):
        onboard(staff, other, role="THUMBNAIL", source_media=main)
    thumbnail = onboard(staff, product, role="THUMBNAIL", source_media=main)
    assert thumbnail.source_media_id == main.pk
    with pytest.raises(ValidationError, match="source media"):
        onboard(staff, product, role="THUMBNAIL")
    with pytest.raises(ValidationError, match="file type"):
        onboard(staff, product, role="VIDEO")


@pytest.mark.django_db(transaction=True)
def test_audited_retirement_preserves_file_and_allows_new_main(configured_media, media_graph):
    staff, product, _ = media_graph
    first = onboard(staff, product)
    retired = update_product_media_metadata(
        actor=staff,
        media=first,
        alt_text="Original image",
        sort_order=2,
        status="RETIRED",
        reason="Fictional replacement",
    )
    second = onboard(staff, product)
    assert second.public_id != first.public_id
    assert (configured_media / first.object_key).is_file()
    with pytest.raises(ValidationError, match="cannot be restored"):
        update_product_media_metadata(
            actor=staff,
            media=retired,
            alt_text="Original image",
            sort_order=2,
            status="DRAFT",
            reason="Fictional reactivation",
        )
    with pytest.raises(TypeError):
        ProductMedia.objects.filter(pk=retired.pk).delete()


@pytest.mark.django_db(transaction=True)
def test_admin_upload_form_interface(configured_media, media_graph):
    staff, product, _ = media_graph
    form = ProductMediaUploadForm(
        data={
            "product": product.pk,
            "role": "MAIN",
            "alt_text": "Fixture image",
            "sort_order": 0,
            "reason": "Fictional staff onboarding",
        },
        files={"file": upload()},
    )
    assert form.is_valid(), form.errors
    media = form.save_for_actor(staff)
    assert media.product_id == product.pk


@pytest.mark.django_db(transaction=True)
def test_media_admin_upload_download_retirement_and_csrf(configured_media, media_graph):
    staff, product, _ = media_graph
    client = Client(enforce_csrf_checks=True)
    client.force_login(staff)
    path = reverse("admin:catalog_productmedia_add")

    def values():
        return {
            "product": product.pk,
            "role": "MAIN",
            "file": upload(),
            "alt_text": "Fixture",
            "sort_order": 0,
            "reason": "Fictional upload",
        }

    assert client.post(path, values()).status_code == 403
    assert not ProductMedia.objects.exists()
    assert client.get(path).status_code == 200
    token = client.cookies["csrftoken"].value
    response = client.post(path, values(), HTTP_X_CSRFTOKEN=token, follow=True)
    assert response.status_code == 200
    assert len(response.redirect_chain) == 1
    media = ProductMedia.objects.get()
    download = reverse("admin:catalog_productmedia_download", args=[media.pk])
    response = client.get(download)
    assert response.status_code == 200
    assert {"private", "no-store"} <= set(response["Cache-Control"].split(", "))
    assert b"".join(response.streaming_content) == png_bytes()
    response.close()
    metadata = reverse("admin:catalog_productmedia_metadata", args=[media.pk])
    values = {
        "status": "RETIRED",
        "alt_text": "Retired fixture",
        "sort_order": 1,
        "reason": "Replace",
    }
    assert client.post(metadata, values).status_code == 403
    assert client.post(metadata, values, HTTP_X_CSRFTOKEN=token).status_code == 302
    media.refresh_from_db()
    assert media.status == "RETIRED"
    assert (configured_media / media.object_key).is_file()


@pytest.mark.django_db(transaction=True)
def test_media_add_only_staff_gets_authorized_success_destination(configured_media, media_graph):
    _, product, _ = media_graph
    staff = StaffUser.objects.create_user(username="media-add-only", is_staff=True)
    staff.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="catalog", codename__in=["add_productmedia", "view_product"]
        )
    )
    client = Client()
    client.force_login(staff)
    response = client.post(
        reverse("admin:catalog_productmedia_add"),
        {
            "product": product.pk,
            "role": "MAIN",
            "file": upload(),
            "alt_text": "Fixture",
            "sort_order": 0,
            "reason": "Fictional upload",
        },
        follow=True,
    )
    assert response.status_code == 200
    assert ProductMedia.objects.count() == 1
    media = ProductMedia.objects.get()
    assert (
        client.get(reverse("admin:catalog_productmedia_download", args=[media.pk])).status_code
        == 403
    )


@pytest.mark.django_db(transaction=True)
def test_media_admin_reports_unconfigured_storage_without_writing(settings, media_graph):
    staff, product, _ = media_graph
    settings.PRODUCT_MEDIA_MAX_BYTES = None
    client = Client()
    client.force_login(staff)
    response = client.post(
        reverse("admin:catalog_productmedia_add"),
        {
            "product": product.pk,
            "role": "MAIN",
            "file": upload(),
            "alt_text": "Fixture",
            "sort_order": 0,
            "reason": "Fictional upload",
        },
    )
    assert response.status_code == 200
    assert "byte limit" in str(response.context["form"].errors)
    assert not ProductMedia.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_video_and_thumbnail_use_distinct_private_assets(configured_media, media_graph):
    staff, product, _ = media_graph

    def box(kind, payload):
        return struct.pack(">I", 8 + len(payload)) + kind + payload

    # Structural container fixture only: this does not assert decodable video.
    video_bytes = (
        box(b"ftyp", b"isom\x00\x00\x00\x00") + box(b"moov", b"fixture") + box(b"mdat", b"fixture")
    )
    video = onboard(
        staff, product, role="VIDEO", uploaded_file=upload(video_bytes, "fixture.mp4", "video/mp4")
    )
    thumbnail = onboard(staff, product, role="THUMBNAIL", source_media=video)
    assert thumbnail.source_media_id == video.pk
    assert video.content_type == "video/mp4"
    assert video.object_key != thumbnail.object_key
    assert product_media_metadata(video)["delivery"] == {"state": "PRIVATE"}


def test_jpeg_structure_and_extension_are_checked(configured_media):
    # Small structural fixture; private delivery makes no full-decoder guarantee.
    jpeg = b"\xff\xd8\xff\xc0\x00\x08\x08\x00\x01\x00\x01\x00\xff\xda\x00\x02\x01\xff\xd9"
    assert (
        validate_media_upload(upload(jpeg, "fixture.jpeg", "image/jpeg")).content_type
        == "image/jpeg"
    )
    with pytest.raises(ValidationError):
        validate_media_upload(upload(jpeg[:-2], "fixture.jpeg", "image/jpeg"))
