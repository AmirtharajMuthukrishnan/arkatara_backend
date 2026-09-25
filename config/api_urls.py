from django.urls import path

from config.health import HealthView
from markets.api import ReferenceDataView, ServiceabilityView

app_name = "api_v1"

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("reference-data/", ReferenceDataView.as_view(), name="reference-data"),
    path("serviceability/", ServiceabilityView.as_view(), name="serviceability"),
]
