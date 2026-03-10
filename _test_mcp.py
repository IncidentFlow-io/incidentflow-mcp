# uv run python _test_mcp.py

import json
import httpx

BASE = "http://localhost:8000"
TOKEN = ""

MCP_HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def sep(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def _pretty(text: str) -> None:
    """Parse SSE body and pretty-print each data: line as JSON."""
    for line in text.splitlines():
        if line.startswith("event:"):
            print(f"  \033[90mevent: {line[6:].strip()}\033[0m")
        elif line.startswith("data:"):
            raw = line[5:].strip()
            try:
                print(json.dumps(json.loads(raw), indent=2, ensure_ascii=False))
            except json.JSONDecodeError:
                print(raw)


def mcp(method: str, params: dict, req_id: int = 1) -> None:
    r = httpx.post(
        f"{BASE}/mcp",
        headers=MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
        timeout=10,
    )
    status_color = "\033[32m" if r.status_code == 200 else "\033[31m"
    print(f"{status_color}STATUS {r.status_code}\033[0m")
    _pretty(r.text)


# ── Public endpoints ────────────────────────────────────────────

sep("GET /healthz  (no auth)")
r = httpx.get(f"{BASE}/healthz", timeout=5)
status_color = "\033[32m" if r.status_code == 200 else "\033[31m"
print(f"{status_color}STATUS {r.status_code}\033[0m")
try:
    print(json.dumps(r.json(), indent=2))
except Exception:
    print(r.text)

sep("GET /openapi.json  (no auth)")
r = httpx.get(f"{BASE}/openapi.json", timeout=5)
status_color = "\033[32m" if r.status_code == 200 else "\033[31m"
print(f"{status_color}STATUS {r.status_code}\033[0m  (schema chars: {len(r.text)})")

# ── MCP protocol ────────────────────────────────────────────────

sep("POST /mcp  initialize")
mcp(
    "initialize",
    {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1"},
    },
)

sep("POST /mcp  tools/list")
mcp("tools/list", {}, req_id=2)

sep("POST /mcp  tools/call — incident_summary INC-001 (full)")
mcp(
    "tools/call",
    {
        "name": "incident_summary",
        "arguments": {
            "incident_id": "INC-001",
            "include_timeline": True,
            "include_affected_services": True,
        },
    },
    req_id=3,
)

sep("POST /mcp  tools/call — incident_summary INC-002 (no timeline)")
mcp(
    "tools/call",
    {
        "name": "incident_summary",
        "arguments": {
            "incident_id": "INC-002",
            "include_timeline": False,
            "include_affected_services": True,
        },
    },
    req_id=4,
)

sep("POST /mcp  tools/call — incident_summary unknown ID")
mcp(
    "tools/call",
    {"name": "incident_summary", "arguments": {"incident_id": "INC-999"}},
    req_id=5,
)

sep("POST /mcp  tools/call — correlate_alerts (2 alerts, same service)")
mcp(
    "tools/call",
    {
        "name": "correlate_alerts",
        "arguments": {
            "alerts": [
                {
                    "alert_id": "a1",
                    "name": "HighMemoryUsage",
                    "service": "api-gateway",
                    "severity": "critical",
                    "status": "firing",
                    "fired_at": "2024-01-15T10:00:00Z",
                    "labels": {"env": "prod"},
                },
                {
                    "alert_id": "a2",
                    "name": "SlowRequests",
                    "service": "api-gateway",
                    "severity": "warning",
                    "status": "firing",
                    "fired_at": "2024-01-15T10:05:00Z",
                    "labels": {"env": "prod"},
                },
                {
                    "alert_id": "a3",
                    "name": "DatabaseTimeout",
                    "service": "postgres",
                    "severity": "critical",
                    "status": "firing",
                    "fired_at": "2024-01-15T10:02:00Z",
                    "labels": {"env": "prod"},
                },
            ],
            "window_minutes": 15,
            "min_cluster_size": 2,
        },
    },
    req_id=6,
)

sep("POST /mcp  tools/call — correlate_alerts (label affinity, cross-service)")
mcp(
    "tools/call",
    {
        "name": "correlate_alerts",
        "arguments": {
            "alerts": [
                {
                    "alert_id": "b1",
                    "name": "CPUThrottle",
                    "service": "worker-a",
                    "severity": "warning",
                    "status": "firing",
                    "fired_at": "2024-01-15T12:00:00Z",
                    "labels": {"region": "us-east-1"},
                },
                {
                    "alert_id": "b2",
                    "name": "CPUThrottle",
                    "service": "worker-b",
                    "severity": "warning",
                    "status": "firing",
                    "fired_at": "2024-01-15T12:03:00Z",
                    "labels": {"region": "us-east-1"},
                },
            ],
            "window_minutes": 30,
            "min_cluster_size": 2,
        },
    },
    req_id=7,
)

# ── Auth edge cases ─────────────────────────────────────────────

sep("POST /mcp  no token → 401")
r = httpx.post(
    f"{BASE}/mcp",
    headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    json={"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}},
    timeout=5,
)
print(f"\033[{'32' if r.status_code == 401 else '31'}mSTATUS {r.status_code}  (expected 401)\033[0m")

sep("POST /mcp  wrong token → 401")
r = httpx.post(
    f"{BASE}/mcp",
    headers={
        "Authorization": "Bearer if_pat_local_00000000.wrongsecret",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    },
    json={"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}},
    timeout=5,
)
print(f"\033[{'32' if r.status_code == 401 else '31'}mSTATUS {r.status_code}  (expected 401)\033[0m")
