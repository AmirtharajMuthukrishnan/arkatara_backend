from django.urls import path

from config.health import HealthView

app_name = "api_v1"

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
]
