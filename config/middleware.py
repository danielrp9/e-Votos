from urllib.parse import urlsplit

from django.core.exceptions import DisallowedHost
from django.middleware.csrf import CsrfViewMiddleware


class NullOriginCsrfViewMiddleware(CsrfViewMiddleware):
    def _origin_verified(self, request):
        if request.META.get("HTTP_ORIGIN") != "null":
            return super()._origin_verified(request)

        referer = request.META.get("HTTP_REFERER")
        if not referer:
            return False

        try:
            referer_url = urlsplit(referer)
            request_host = request.get_host()
        except (DisallowedHost, ValueError):
            return False

        expected_scheme = "https" if request.is_secure() else "http"
        return (
            referer_url.scheme == expected_scheme
            and referer_url.netloc.lower() == request_host.lower()
        )
