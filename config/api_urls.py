from django.urls import path

from catalog.storefront_api import CatalogueView, StorefrontPageView
from config.health import HealthView
from markets.api import ReferenceDataView, ServiceabilityView

app_name = "api_v1"

urlpatterns = [
    path("catalogue/", CatalogueView.as_view(), name="catalogue"),
    path("catalogue/pages/<slug:slug>/", StorefrontPageView.as_view(), name="storefront-page"),
    path("health/", HealthView.as_view(), name="health"),
    path("reference-data/", ReferenceDataView.as_view(), name="reference-data"),
    path("serviceability/", ServiceabilityView.as_view(), name="serviceability"),
]
