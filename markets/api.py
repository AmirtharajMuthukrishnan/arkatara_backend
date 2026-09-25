"""Public, read-only reference contracts without stock or customer information."""

from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from catalog.models import Category, Material, Purity
from common.reference import ReferenceStatus
from markets.models import Market
from markets.services import coverage_state

PUBLIC_STATUSES = (ReferenceStatus.ACTIVE, ReferenceStatus.COMING_SOON)


def reference_row(row):
    return {"id": str(row.public_id), "code": row.code, "name": row.name, "status": row.status}


class ReferenceDataView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(
            {
                "schema_version": 1,
                "markets": [
                    {**reference_row(row), "country_code": row.country_code}
                    for row in Market.objects.filter(status__in=PUBLIC_STATUSES)
                ],
                "materials": [
                    reference_row(row)
                    for row in Material.objects.filter(status__in=PUBLIC_STATUSES)
                ],
                "purities": [
                    {**reference_row(row), "material_id": str(row.material.public_id)}
                    for row in Purity.objects.select_related("material").filter(
                        status__in=PUBLIC_STATUSES, material__status__in=PUBLIC_STATUSES
                    )
                ],
                "categories": [
                    reference_row(row)
                    for row in Category.objects.filter(status__in=PUBLIC_STATUSES)
                ],
            }
        )


class ServiceabilityQuery(serializers.Serializer):
    market = serializers.SlugField(max_length=64, trim_whitespace=False)
    country_code = serializers.RegexField(r"\A[A-Z]{2}\Z", trim_whitespace=False)
    postal_code = serializers.CharField(max_length=20, trim_whitespace=False)

    def validate_postal_code(self, value):
        if not value.strip() or value != value.strip() or any(ord(char) < 32 for char in value):
            raise serializers.ValidationError("Use a postal identifier without outer whitespace.")
        return value


class ServiceabilityView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        fields = {"market", "country_code", "postal_code"}
        if set(request.query_params) != fields or any(
            len(request.query_params.getlist(key)) != 1 for key in fields
        ):
            raise serializers.ValidationError("Supply market, country_code and postal_code once.")
        query = ServiceabilityQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        values = query.validated_data
        market = get_object_or_404(
            Market.objects.exclude(status=ReferenceStatus.DRAFT), code=values["market"]
        )
        return Response(
            {
                "market_code": market.code,
                "country_code": values["country_code"],
                "postal_code": values["postal_code"],
                "state": coverage_state(
                    market, country_code=values["country_code"], postal_code=values["postal_code"]
                ),
                # Geographic coverage cannot establish price/stock/plan/visit readiness.
                "bookable": False,
            }
        )
