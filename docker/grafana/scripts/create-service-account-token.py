from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


GRAFANA_URL = _env("GRAFANA_URL", "http://grafana:3000").rstrip("/")
ADMIN_USER = _env("GRAFANA_ADMIN_USER", "admin")
ADMIN_PASSWORD = _env("GRAFANA_ADMIN_PASSWORD", "admin")
SA_NAME = _env("GRAFANA_SERVICE_ACCOUNT_NAME", "incidentflow-platform")
TOKEN_NAME = _env("GRAFANA_SERVICE_ACCOUNT_TOKEN_NAME", "incidentflow-platform-local")
SA_ROLE = _env("GRAFANA_SERVICE_ACCOUNT_ROLE", "Admin")


def _auth_header() -> str:
    raw = f"{ADMIN_USER}:{ADMIN_PASSWORD}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _request(method: str, path: str, payload: dict[str, object] | None = None) -> object:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{GRAFANA_URL}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": _auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        text = resp.read().decode("utf-8")
        return json.loads(text) if text else {}


def _wait_for_grafana() -> None:
    for _ in range(30):
        try:
            _request("GET", "/api/health")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"Grafana did not become ready at {GRAFANA_URL}")


def _find_service_account_id() -> int | None:
    payload = _request("GET", f"/api/serviceaccounts/search?query={SA_NAME}")
    accounts = payload.get("serviceAccounts", []) if isinstance(payload, dict) else []
    for account in accounts:
        if isinstance(account, dict) and account.get("name") == SA_NAME:
            return int(account["id"])
    return None


def _create_service_account() -> int:
    try:
        payload = _request("POST", "/api/serviceaccounts", {"name": SA_NAME, "role": SA_ROLE})
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        existing_id = _find_service_account_id()
        if existing_id is None:
            raise
        return existing_id
    if not isinstance(payload, dict) or "id" not in payload:
        raise RuntimeError(f"Unexpected service account response: {payload!r}")
    return int(payload["id"])


def main() -> int:
    _wait_for_grafana()
    service_account_id = _find_service_account_id() or _create_service_account()
    payload = _request(
        "POST",
        f"/api/serviceaccounts/{service_account_id}/tokens",
        {"name": f"{TOKEN_NAME}-{int(time.time())}"},
    )
    if not isinstance(payload, dict) or "key" not in payload:
        raise RuntimeError(f"Unexpected token response: {payload!r}")

    print("Grafana service account token created.")
    print(f"GRAFANA_URL=http://localhost:{os.environ.get('GRAFANA_PORT', '3000')}")
    print(f"GRAFANA_SERVICE_ACCOUNT_ID={service_account_id}")
    print(f"GRAFANA_SA_TOKEN={payload['key']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise
