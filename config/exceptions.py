from rest_framework.response import Response
from rest_framework.views import exception_handler


def api_exception_handler(exc: Exception, context: dict) -> Response | None:
    response = exception_handler(exc, context)
    if response is None:
        return None

    response.data = {
        "error": {
            "status_code": response.status_code,
            "details": response.data,
        }
    }
    return response
