"""Request throttling and body-size limits (in-memory, single process)."""
import json
import time
from collections import defaultdict, deque

from app.core.errors import AppError, envelope


class RateLimiter:
    """Sliding-window counter keyed by an arbitrary string (user name, client IP, ...)."""

    def __init__(self, clock=time.monotonic):
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._clock = clock

    def _prune(self, key: str, window: float) -> deque:
        q = self._hits[key]
        cutoff = self._clock() - window
        while q and q[0] <= cutoff:
            q.popleft()
        if not q:
            self._hits.pop(key, None)  # do not grow without bound
        return q

    def hit(self, key: str, limit: int, window: float = 60.0) -> bool:
        """Record one event; return False when the key is over `limit` within `window` seconds."""
        if limit <= 0:
            return True
        q = self._prune(key, window)
        if len(q) >= limit:
            return False
        self._hits[key].append(self._clock())
        return True

    def over(self, key: str, limit: int, window: float = 60.0) -> bool:
        return limit > 0 and len(self._prune(key, window)) >= limit

    def record(self, key: str) -> None:
        self._hits[key].append(self._clock())


def throttle(limiter: RateLimiter, key: str, limit: int) -> None:
    if not limiter.hit(key, limit):
        raise AppError("rate_limited", "Too many requests. Please wait a moment and try again.", 429)


class _TooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Reject oversized request bodies (declared or streamed) before they are parsed in memory."""

    def __init__(self, app, small_bytes: int, large_bytes: int, large_paths: tuple[str, ...]):
        self.app, self.small, self.large, self.large_paths = app, small_bytes, large_bytes, large_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS", "DELETE"):
            return await self.app(scope, receive, send)
        limit = self.large if scope["path"].endswith(self.large_paths) else self.small
        declared = dict(scope["headers"]).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            return await self._reject(send, limit)
        seen = 0
        started = rejected = False

        async def counting_receive():
            nonlocal seen, rejected
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    # Answer 413 right here: frameworks downstream may swallow our exception and reply 400.
                    if not started and not rejected:
                        rejected = True
                        await self._reject(send, limit)
                    raise _TooLarge
            return msg

        async def tracking_send(msg):
            nonlocal started
            if rejected:  # the 413 is already on the wire; drop whatever the app tries to send next
                return
            started = started or msg["type"] == "http.response.start"
            await send(msg)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _TooLarge:
            if not started and not rejected:
                await self._reject(send, limit)

    @staticmethod
    async def _reject(send, limit: int):
        body = json.dumps(envelope("payload_too_large", f"Request body is larger than {limit // 1024} KB.")).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
