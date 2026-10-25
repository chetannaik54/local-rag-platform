"""CORS that also trusts the websites registered on embed keys.

Allowed browser origins = the fixed list (CORS_ORIGINS / WIDGET_ORIGINS) plus the union of the origins of all
active embed keys. That union only decides whether the browser may *read* a response; the embed key itself
re-checks the Origin header against its own list before serving anything (see auth._visitor), so a site
registered for one customer's key cannot use another customer's key.
"""
from collections.abc import Callable

from starlette.datastructures import Headers, MutableHeaders

from app.api.auth import origin_allowed

ALLOW_METHODS = "GET, POST, PUT, DELETE, OPTIONS"


class DynamicCORSMiddleware:
    def __init__(self, app, static_origins: list[str], dynamic_origins: Callable[[], set[str]]):
        self.app = app
        self.static = {o.lower().rstrip("/") for o in static_origins}
        self.dynamic = dynamic_origins

    def _allowed(self, origin: str) -> bool:
        o = origin.lower().rstrip("/")
        return o in self.static or origin_allowed(origin, list(self.dynamic()))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if not origin:
            return await self.app(scope, receive, send)
        allowed = self._allowed(origin)

        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:  # preflight
            if not allowed:
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-length", b"0"), (b"vary", b"Origin")]})
                return await send({"type": "http.response.body", "body": b""})
            reply = MutableHeaders(raw=[])
            reply["Access-Control-Allow-Origin"] = origin
            reply["Access-Control-Allow-Methods"] = ALLOW_METHODS
            reply["Access-Control-Allow-Headers"] = headers.get("access-control-request-headers", "*")
            reply["Access-Control-Max-Age"] = "600"
            reply["Vary"] = "Origin"
            reply["Content-Length"] = "0"
            await send({"type": "http.response.start", "status": 204, "headers": reply.raw})
            return await send({"type": "http.response.body", "body": b""})

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.append("Vary", "Origin")
                if allowed:
                    h["Access-Control-Allow-Origin"] = origin
            await send(message)

        await self.app(scope, receive, send_with_cors)
