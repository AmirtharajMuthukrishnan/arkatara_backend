"""Attributed private media onboarding; storage never decides product availability."""

import hashlib
import struct
import uuid
import zlib
from dataclasses import dataclass
from pathlib import PurePath

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.http import FileResponse

from catalog.media import ProductMedia
from catalog.models import Product
from catalog.storage import get_media_storage
from compliance.audit import model_facts, record_model_change

SUPPORTED_TYPES = {"image/png": "png", "image/jpeg": "jpg", "video/mp4": "mp4"}
AUDITED_FIELDS = (
    "product_id",
    "source_media_id",
    "role",
    "status",
    "sort_order",
    "alt_text",
    "storage_backend",
    "object_key",
    "content_type",
    "byte_size",
    "sha256",
)


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    content: bytes
    content_type: str
    extension: str
    sha256: str


def _png_valid(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    image_data = False
    first = True
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        end = offset + 12 + length
        if end > len(data):
            return False
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : end - 4]
        if zlib.crc32(kind + body) != int.from_bytes(data[end - 4 : end], "big"):
            return False
        if first:
            if kind != b"IHDR" or length != 13:
                return False
            width, height = struct.unpack(">II", body[:8])
            if not width or not height or body[10:12] != b"\x00\x00" or body[12] not in (0, 1):
                return False
            first = False
        elif kind == b"IHDR":
            return False
        if kind == b"IDAT":
            image_data = image_data or bool(length)
        if kind == b"IEND":
            return not length and image_data and end == len(data)
        offset = end
    return False


def _jpeg_valid(data):
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        return False
    offset = 2
    dimensions = False
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            return False
        marker = data[offset + 1]
        offset += 2
        if marker in {0x00, 0xD8, 0xD9}:
            return False
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            return False
        if marker in {0xC0, 0xC1, 0xC2}:
            if length < 8:
                return False
            dimensions = bool(int.from_bytes(data[offset + 3 : offset + 5], "big")) and bool(
                int.from_bytes(data[offset + 5 : offset + 7], "big")
            )
        offset += length
        if marker == 0xDA:
            # Validate the container boundary; files stay private attachments and are not decoded.
            return dimensions and offset < len(data) - 2
    return False


def _mp4_valid(data):
    offset = 0
    found = set()
    while offset + 8 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        if length < 8 or offset + length > len(data):
            return False
        if offset == 0 and kind != b"ftyp":
            return False
        if kind == b"ftyp":
            if length < 16 or data[offset + 8 : offset + 12] not in {
                b"isom",
                b"iso2",
                b"mp41",
                b"mp42",
                b"avc1",
            }:
                return False
        if kind in {b"moov", b"mdat"} and length == 8:
            return False
        found.add(kind)
        offset += length
    return offset == len(data) and {b"ftyp", b"moov", b"mdat"} <= found


def validate_media_upload(uploaded_file) -> ValidatedUpload:
    # Format support is technical capability; actual allowed types and limits need configuration.
    # Refs: DECISIONS.md BD-15; INTEGRATIONS.md#object-storage--task-3.
    maximum = getattr(settings, "PRODUCT_MEDIA_MAX_BYTES", None)
    allowed = getattr(settings, "PRODUCT_MEDIA_ALLOWED_TYPES", None)
    if type(maximum) is not int or maximum <= 0:
        raise ValidationError("Configure an explicit positive product media byte limit.")
    if (
        not isinstance(allowed, list | tuple)
        or not allowed
        or any(not isinstance(item, str) or item not in SUPPORTED_TYPES for item in allowed)
    ):
        raise ValidationError("Configure explicit supported product media types.")
    filename = getattr(uploaded_file, "name", "")
    if not filename or any(character in filename for character in ("/", "\\", ":")):
        raise ValidationError("Use a plain media filename without a path.")
    if getattr(uploaded_file, "size", 0) > maximum:
        raise ValidationError("Media exceeds the configured upload byte limit.")
    content = bytearray()
    try:
        for chunk in uploaded_file.chunks():
            content.extend(chunk)
            if len(content) > maximum:
                raise ValidationError("Media exceeds the configured upload byte limit.")
    finally:
        uploaded_file.seek(0)
    data = bytes(content)
    detected = next(
        (
            kind
            for kind, validate in (
                ("image/png", _png_valid),
                ("image/jpeg", _jpeg_valid),
                ("video/mp4", _mp4_valid),
            )
            if validate(data)
        ),
        None,
    )
    if detected is None or detected not in allowed:
        raise ValidationError("File content is not a configured supported media type.")
    extensions = (
        {".png"}
        if detected == "image/png"
        else {".mp4"}
        if detected == "video/mp4"
        else {".jpg", ".jpeg"}
    )
    if PurePath(filename).suffix.lower() not in extensions:
        raise ValidationError("Filename extension does not match the file content.")
    if getattr(uploaded_file, "content_type", None) != detected:
        raise ValidationError("Declared media type does not match the file content.")
    return ValidatedUpload(
        data, detected, SUPPORTED_TYPES[detected], hashlib.sha256(data).hexdigest()
    )


def _authorize(actor, action):
    # Model permissions are enforced inside workflows, not only by hiding Admin buttons.
    # Refs: BUSINESS_RULES.md R-18, R-30; DECISIONS.md D-14.
    if not (
        actor
        and actor.is_authenticated
        and actor.is_active
        and actor.is_staff
        and actor.has_perm(f"catalog.{action}_productmedia")
        and actor.has_perm("catalog.view_product")
    ):
        raise PermissionDenied("Media access requires the relevant staff permissions.")


def _reason(value):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Record a reason for the media operation.")
    return value.strip()


def upload_product_media(
    *, actor, product, uploaded_file, role, alt_text, reason, sort_order=0, source_media=None
):
    _authorize(actor, "add")
    reason = _reason(reason)
    if connection.in_atomic_block:
        raise ValidationError("Media upload must own its database transaction.")
    validated = validate_media_upload(uploaded_file)
    storage = get_media_storage()
    asset_id = uuid.uuid4()
    key = f"products/{product.public_id}/{asset_id}.{validated.extension}"
    stored = False
    try:
        with transaction.atomic():
            product = Product.objects.select_for_update().get(pk=product.pk)
            media = ProductMedia(
                public_id=asset_id,
                product=product,
                role=role,
                alt_text=alt_text,
                sort_order=sort_order,
                source_media=source_media,
                storage_backend=storage.name,
                object_key=key,
                content_type=validated.content_type,
                byte_size=len(validated.content),
                sha256=validated.sha256,
            )
            media.full_clean()
            storage.put(key, validated.content)
            stored = True
            media._service_write = True
            media.save()
            record_model_change(
                actor=actor,
                instance=media,
                before={},
                after=model_facts(media, AUDITED_FIELDS),
                reason=reason,
            )
        return media
    except BaseException:
        # Delete only the new object if no committed metadata owns it.
        # Never delete referenced media.
        # Owning the outer transaction makes DB/audit failure cleanup unambiguous.
        # Refs: BUSINESS_RULES.md R-30; DECISIONS.md D-14, BD-15.
        if stored and not ProductMedia.objects.filter(object_key=key).exists():
            storage.delete(key)
        raise


def update_product_media_metadata(*, actor, media, alt_text, sort_order, status, reason):
    _authorize(actor, "change")
    reason = _reason(reason)
    with transaction.atomic():
        media = ProductMedia.objects.select_for_update().get(pk=media.pk)
        before = model_facts(media, AUDITED_FIELDS)
        media.alt_text, media.sort_order, media.status = alt_text, sort_order, status
        media._service_write = True
        media.save()
        record_model_change(
            actor=actor,
            instance=media,
            before=before,
            after=model_facts(media, AUDITED_FIELDS),
            reason=reason,
        )
    return media


def product_media_metadata(media):
    return {
        "schema_version": 1,
        "id": str(media.public_id),
        "product_id": str(media.product.public_id),
        "source_media_id": str(media.source_media.public_id) if media.source_media_id else None,
        "role": media.role,
        "status": media.status,
        "sort_order": media.sort_order,
        "alt_text": media.alt_text,
        "content_type": media.content_type,
        "byte_size": media.byte_size,
        "sha256": media.sha256,
        "delivery": {"state": "PRIVATE"},
    }


def media_download_response(*, actor, media):
    _authorize(actor, "view")
    storage = get_media_storage()
    if storage.name != media.storage_backend:
        raise ValidationError("The recorded asset storage backend is not configured.")
    response = FileResponse(
        storage.open(media.object_key),
        as_attachment=True,
        filename=f"{media.public_id}.{SUPPORTED_TYPES[media.content_type]}",
        content_type=media.content_type,
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response
