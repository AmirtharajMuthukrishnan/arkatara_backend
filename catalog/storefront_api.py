"""Anonymous published catalogue reads; no private asset or financial projections."""

from math import ceil

from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from catalog.models import Material
from catalog.storefront import (
    breadcrumbs,
    product_detail,
    product_pages,
    product_summary,
    public_variants,
    reference,
    summary,
    visible_pages,
)
from catalog.storefront_models import StorefrontPage
from markets.models import Market

PAGE_SIZE = 12  # Technical response bound, unrelated to Trial Plan or stock limits.
PUBLIC_STATUSES = ("ACTIVE", "COMING_SOON")


class CatalogueQuery(serializers.Serializer):
    market = serializers.SlugField(required=False, max_length=64, trim_whitespace=False)
    material = serializers.SlugField(required=False, max_length=64, trim_whitespace=False)
    page = serializers.RegexField(r"\A[1-9][0-9]{0,8}\Z", required=False, trim_whitespace=False)


def _selection(rows, code, field):
    if code is not None:
        selected = next((row for row in rows if row.code == code), None)
        if selected is None:
            raise serializers.ValidationError({field: "Select a visible catalogue option."})
        return selected
    # This default selects a presentation context; it cannot approve fulfilment.
    # Refs: BUSINESS_RULES.md R-07, R-09; DECISIONS.md D-20.
    return next((row for row in rows if row.status == "ACTIVE"), None)


def _context(request):
    if set(request.query_params) - {"market", "material", "page"} or any(
        len(request.query_params.getlist(key)) != 1 for key in request.query_params
    ):
        raise serializers.ValidationError("Use market, material and page at most once.")
    query = CatalogueQuery(data={key: request.query_params[key] for key in request.query_params})
    query.is_valid(raise_exception=True)
    values = query.validated_data
    markets = list(Market.objects.filter(status__in=PUBLIC_STATUSES).order_by("code"))
    materials = list(Material.objects.filter(status__in=PUBLIC_STATUSES).order_by("code"))
    return {
        "markets": markets,
        "materials": materials,
        "market": _selection(markets, values.get("market"), "market"),
        "material": _selection(materials, values.get("material"), "material"),
        "page_number": int(values.get("page", "1")),
    }


def _catalogue(context, collection=None):
    market, material, page_number = context["market"], context["material"], context["page_number"]
    pages = product_pages(market, material, collection)
    total = pages.count()
    total_pages = max(1, ceil(total / PAGE_SIZE))
    if page_number > total_pages:
        raise NotFound("This catalogue page does not exist.")
    offset = (page_number - 1) * PAGE_SIZE
    return {
        "schema_version": 1,
        "markets": [
            {**reference(row), "country_code": row.country_code} for row in context["markets"]
        ],
        "materials": [reference(row) for row in context["materials"]],
        "selected_market": {**reference(market), "country_code": market.country_code}
        if market
        else None,
        "selected_material": reference(material) if material else None,
        "collections": [summary(row) for row in visible_pages().exclude(kind="PRODUCT")],
        "products": [
            product_summary(row, market, material) for row in pages[offset : offset + PAGE_SIZE]
        ],
        "pagination": {
            "page": page_number,
            "page_size": PAGE_SIZE,
            "total_items": total,
            "total_pages": total_pages,
        },
    }


class CatalogueView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(_catalogue(_context(request)))


class StorefrontPageView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, slug):
        context = _context(request)
        page = get_object_or_404(visible_pages(), slug=slug)
        if page.kind == StorefrontPage.Kind.MATERIAL:
            context["material"] = page.material
        elif page.kind == StorefrontPage.Kind.PRODUCT:
            context["page_number"] = 1
            if "material" not in request.query_params:
                variant = (
                    public_variants(context["market"], None)
                    .filter(product_id=page.product_id)
                    .order_by("material__code", "pk")
                    .first()
                )
                context["material"] = variant.material if variant else None
        data = _catalogue(context, collection=page)
        data.update(
            {
                "page": summary(page),
                "breadcrumbs": breadcrumbs(page),
                "product": product_detail(page, context["market"], context["material"])
                if page.kind == StorefrontPage.Kind.PRODUCT
                else None,
            }
        )
        return Response(data)
