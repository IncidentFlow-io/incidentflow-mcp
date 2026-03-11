"""
Request correlation ID middleware.

Reads X-Request-ID from incoming headers if present; otherwise generates a
UUID4.  Stores the ID in request.state.request_id and echoes it back in the
X-Request-ID response header so clients and logs can correlate requests.
"""

import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response
