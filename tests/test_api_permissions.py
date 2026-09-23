from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView


class ProtectedExampleView(APIView):
    """Test-only endpoint verifying the API's default permission boundary."""

    def get(self, request):
        return Response({"private": True})


def test_api_default_rejects_anonymous_access():
    response = ProtectedExampleView.as_view()(APIRequestFactory().get("/api/v1/example/"))
    assert response.status_code == 403
    assert response.data["error"]["status_code"] == 403
    assert "private" not in response.data


def test_anonymous_admin_access_redirects_to_login(client):
    response = client.get("/admin/")
    assert response.status_code == 302
    assert response.url.startswith("/admin/login/")
