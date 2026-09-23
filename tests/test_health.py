from rest_framework.test import APIClient


def test_versioned_health_endpoint_is_public() -> None:
    response = APIClient().get("/api/v1/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "api_version": "v1"}
