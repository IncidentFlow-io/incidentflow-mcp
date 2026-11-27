"""Delegated, read-only transport for the private Logs API v1."""

import re
from uuid import UUID

import httpx

from incidentflow_mcp.config import Settings

_STATUS_CODES = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    422: "INVALID_ARGUMENT",
    429: "RATE_LIMITED",
}


class PlatformLogsError(Exception):
    def __init__(
        self,
        status: int,
        retry_after_seconds: int | None = None,
        reason: str | None = None,
    ):
        self.code = _STATUS_CODES.get(status, "UPSTREAM_ERROR")
        self.retry_after_seconds = retry_after_seconds
        self.reason = reason
        self.message = {
            "INVALID_ARGUMENT": "Invalid Logs request",
            "UNAUTHENTICATED": "Logs authentication required",
            "PERMISSION_DENIED": "Logs access denied",
            "NOT_FOUND": "Logs source not found",
            "RATE_LIMITED": "Logs rate limit exceeded",
            "UPSTREAM_ERROR": "Logs service unavailable",
        }[self.code]
        if reason:
            self.message = f"{self.message}: {reason}"
        super().__init__(self.message)


_REASON_STATUSES = frozenset({400, 403, 404, 422})
_SAFE_REASON = re.compile(r"[A-Za-z0-9 ,.:;()'_-]{1,200}")


def _policy_reason(response: httpx.Response) -> str | None:
    """Return platform-api's short policy message for client errors, nothing else.

    Only a plain sentence from the ``message`` field is kept (for example
    "Log time range exceeds source lookback"); bodies, URLs, and headers are not.
    """

    if response.status_code not in _REASON_STATUSES:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str) or not _SAFE_REASON.fullmatch(message.strip()):
        return None
    return message.strip()


def _retry_after(value: str | None) -> int | None:
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


class PlatformLogsClient:
    def __init__(
        self,
        settings: Settings,
        bearer_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if (
            not settings.platform_api_base_url
            or not settings.platform_api_internal_api_key
            or not bearer_token
        ):
            raise RuntimeError("Logs platform API is not configured")
        self.url = settings.platform_api_base_url.rstrip("/") + "/internal/v1/integrations/logs"
        self.headers = {
            "X-Internal-Api-Key": settings.platform_api_internal_api_key.get_secret_value(),
            "Authorization": f"Bearer {bearer_token}",
        }
        self.transport = transport
        self.timeout = settings.platform_api_timeout_seconds

    async def request(self, method: str, path: str, payload: dict | None = None) -> dict | list:
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
            response = await client.request(
                method, self.url + path, headers=self.headers, json=payload
            )
        if response.status_code != 200:
            raise PlatformLogsError(
                response.status_code,
                _retry_after(response.headers.get("Retry-After")),
                _policy_reason(response),
            )
        return response.json()

    async def list_sources(self) -> list[dict]:
        result = await self.request("GET", "/sources")
        return list(result)

    async def health(self, source_id: str) -> dict:
        try:
            safe_id = str(UUID(source_id))
        except ValueError:
            raise PlatformLogsError(422) from None
        return dict(await self.request("POST", f"/sources/{safe_id}/health"))

    async def query(self, payload: dict) -> dict:
        _require_uuid(payload.get("source_id"))
        return dict(await self.request("POST", "/query", payload))

    async def context(self, payload: dict) -> dict:
        _require_uuid(payload.get("source_id"))
        return dict(await self.request("POST", "/context", payload))


def _require_uuid(value: object) -> None:
    try:
        UUID(str(value))
    except ValueError:
        raise PlatformLogsError(
            422, reason="source_id must be a UUID from logs_list_sources"
        ) from None
