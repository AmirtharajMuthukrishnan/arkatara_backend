"""Lock order and private media I/O regressions using disposable fictional records."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, local

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connection, transaction
from django.test import RequestFactory

from accounts.models import StaffUser
from catalog.admin import ProductVariantAdmin
from catalog.media import ProductMedia
from catalog.models import Category, ProductVariant
from catalog.storage import LocalObjectStorage
from catalog.storefront_admin import StorefrontPageAdmin
from catalog.storefront_models import StorefrontPage
from common.serialization import lock_integrity_writes
from tests.test_checkout_models import booking_graph
from tests.test_checkout_models import setup as setup
from tests.test_product_media import configured_media as configured_media
from tests.test_product_media import media_graph as media_graph
from tests.test_product_media import onboard
from trials.models import TrialBoxItem

pytestmark = pytest.mark.django_db(transaction=True)


def bounded_database_locks():
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '3s'")
        cursor.execute("SET LOCAL statement_timeout = '10s'")


def test_admin_post_refresh_and_checkout_use_the_same_lock_order(setup, monkeypatch):
    graph = booking_graph(setup)
    actor = StaffUser.objects.create_superuser(username="lock-boundary-admin")
    checkout_has_latch = Event()
    admin_requests_latch = Event()
    worker = local()

    def observe_latch(database):
        if getattr(worker, "admin", False):
            admin_requests_latch.set()
        lock_integrity_writes(database)

    monkeypatch.setattr("common.serialization.lock_integrity_writes", observe_latch)

    def checkout():
        close_old_connections()
        try:
            with transaction.atomic():
                bounded_database_locks()
                lock_integrity_writes("default")
                checkout_has_latch.set()
                assert admin_requests_latch.wait(10)
                # The selection guard takes a share lock on the catalogue variant.
                # An Admin row lock taken before its latch would complete a cycle.
                return TrialBoxItem.objects.create(
                    box_id=graph.box.pk,
                    variant_id=setup.variant.pk,
                    quantity=1,
                    selection_snapshot={"fixture": "concurrent checkout"},
                ).pk
        finally:
            close_old_connections()

    def admin_edit():
        close_old_connections()
        worker.admin = True
        try:
            assert checkout_has_latch.wait(10)
            request = RequestFactory().post("/admin/catalog/productvariant/change/")
            request.user = StaffUser.objects.get(pk=actor.pk)
            model_admin = ProductVariantAdmin(ProductVariant, AdminSite())
            with transaction.atomic():
                bounded_database_locks()
                obj = model_admin.get_object(request, str(setup.variant.pk))
                obj.specification = {"fixture": "audited Admin edit"}
                model_admin.save_model(request, obj, None, True)
            return obj.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        checkout_result = pool.submit(checkout)
        admin_result = pool.submit(admin_edit)
        assert checkout_result.result(timeout=20)
        assert admin_result.result(timeout=20) == setup.variant.pk
    setup.variant.refresh_from_db()
    assert setup.variant.specification == {"fixture": "audited Admin edit"}
    assert graph.box.items.count() == 2


def test_storefront_admin_and_page_save_use_the_same_advisory_lock_order(media_graph, monkeypatch):
    actor, product, _ = media_graph
    page = StorefrontPage.objects.create(
        kind="PRODUCT",
        product=product,
        slug="lock-boundary-draft",
        title="Fictional initial title",
        description="Fictional draft description",
    )
    writer_has_latch = Event()
    admin_requests_latch = Event()
    worker = local()

    def observe_latch(database):
        if getattr(worker, "admin", False):
            admin_requests_latch.set()
        lock_integrity_writes(database)

    monkeypatch.setattr("common.serialization.lock_integrity_writes", observe_latch)

    def page_writer():
        close_old_connections()
        try:
            with transaction.atomic():
                bounded_database_locks()
                lock_integrity_writes("default")
                writer_has_latch.set()
                assert admin_requests_latch.wait(10)
                current = StorefrontPage.objects.get(pk=page.pk)
                current.title = "Fictional concurrent page edit"
                # save() also needs the hierarchy latch. Admin cannot hold that
                # latch while waiting for this transaction's integrity latch.
                current.save()
            return current.pk
        finally:
            close_old_connections()

    def admin_edit():
        close_old_connections()
        worker.admin = True
        try:
            assert writer_has_latch.wait(10)
            request = RequestFactory().post("/admin/catalog/storefrontpage/change/")
            request.user = StaffUser.objects.get(pk=actor.pk)
            model_admin = StorefrontPageAdmin(StorefrontPage, AdminSite())
            with transaction.atomic():
                bounded_database_locks()
                current = model_admin.get_object(request, str(page.pk))
                assert current.title == "Fictional concurrent page edit"
                current.title = "Fictional audited Admin edit"
                model_admin.save_model(request, current, None, True)
            return current.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer_result = pool.submit(page_writer)
        admin_result = pool.submit(admin_edit)
        assert writer_result.result(timeout=20) == page.pk
        assert admin_result.result(timeout=20) == page.pk
    page.refresh_from_db()
    assert page.title == "Fictional audited Admin edit"
    assert page.status == "DRAFT"


def test_blocking_media_storage_does_not_block_integrity_writes(
    configured_media, media_graph, monkeypatch
):
    actor, product, _ = media_graph
    writing_file = Event()
    finish_file = Event()
    atomic_during_storage = []
    storage = LocalObjectStorage(configured_media)

    class BlockingStorage:
        name = storage.name

        def put(self, key, content):
            atomic_during_storage.append(connection.in_atomic_block)
            writing_file.set()
            assert finish_file.wait(15)
            storage.put(key, content)

        def delete(self, key):
            storage.delete(key)

    monkeypatch.setattr("catalog.media_services.get_media_storage", BlockingStorage)

    def upload_media():
        close_old_connections()
        try:
            return onboard(actor, product).pk
        finally:
            close_old_connections()

    def unrelated_write():
        close_old_connections()
        try:
            with transaction.atomic():
                bounded_database_locks()
                return Category.objects.create(
                    code="UPLOAD-CONCURRENT", name="Fictional independent category"
                ).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        upload_result = pool.submit(upload_media)
        try:
            assert writing_file.wait(10)
            write_result = pool.submit(unrelated_write)
            assert write_result.result(timeout=10)
        finally:
            finish_file.set()
        media_id = upload_result.result(timeout=20)
    assert atomic_during_storage == [False]
    assert ProductMedia.objects.get(pk=media_id).status == "DRAFT"


def test_upload_reauthorizes_staff_after_storage_and_cleans_unreferenced_file(
    configured_media, media_graph, monkeypatch
):
    actor, product, _ = media_graph
    storage = LocalObjectStorage(configured_media)

    class RevokingStorage:
        name = storage.name

        def put(self, key, content):
            storage.put(key, content)
            StaffUser.objects.filter(pk=actor.pk).update(is_active=False)

        def delete(self, key):
            storage.delete(key)

    monkeypatch.setattr("catalog.media_services.get_media_storage", RevokingStorage)
    with pytest.raises(PermissionDenied, match="relevant staff permissions"):
        onboard(actor, product)
    assert not ProductMedia.objects.exists()
    assert not list(configured_media.rglob("*.png"))
