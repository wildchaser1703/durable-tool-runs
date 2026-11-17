"""Bound request bodies before JSON parsing, including chunked requests."""

import asyncio

from starlette.responses import JSONResponse


class RequestLimits:
    def __init__(self, app, max_bytes=65536, timeout_seconds=10):
        self.app, self.max_bytes, self.timeout_seconds = app, max_bytes, timeout_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.max_bytes:
                        return await JSONResponse(
                            {"detail": f"Request exceeds {self.max_bytes} bytes"}, 413
                        )(scope, receive, send)
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await JSONResponse({"detail": "Request body timeout"}, 408)(
                scope, receive, send
            )
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
