from __future__ import annotations

from typing import Any, ClassVar

import pytest

from incidentflow_mcp.auth.context import clear_current_auth_context, set_current_auth_context
from incidentflow_mcp.config import Settings
from incidentflow_mcp.mcp.registration.slack import normalize_slack_thread_mode
from incidentflow_mcp.mcp.server import create_mcp_server
from incidentflow_mcp.slack.slack_client import SlackThreadFetchResult
from incidentflow_mcp.tools import slack_alerts

ROOT = {
    "ts": "1710000000.000100",
    "text": (
        "[FIRING:6] InstanceDown\nCluster: incidentflow\nNamespace: cert-manager\n"
        "Description: critical - pod unreachable"
    ),
    "reply_count": 2,
    "latest_reply": "1710000002.000100",
    "reply_users": ["U1", "U2"],
}

REPLIES = [
    {
        "ts": "1710000001.000100",
        "user": "U1",
        "text": "I think service: cert-manager lost endpoints\nkubectl get pods -n cert-manager",
    },
    {
        "ts": "1710000002.000100",
        "user": "U2",
        "text": "<https://grafana.example/d/cert|Grafana dashboard> fixed after restart",
    },
]


class FakeSlackClient:
    history_calls = 0
    replies_calls = 0
    user_calls: ClassVar[dict[str, int]] = {}

    def __init__(self, token: str) -> None:
        self.token = token

    async def resolve_channel(self, channel: str) -> tuple[str, str | None]:
        return "C12345678", channel.strip("#")

    async def conversation_history(self, *, channel_id: str, limit: int) -> list[dict[str, Any]]:
        FakeSlackClient.history_calls += 1
        return [ROOT]

    async def permalink(self, *, channel_id: str, message_ts: str) -> str | None:
        return f"https://slack.example/{channel_id}/{message_ts}"

    async def resolve_user(self, user_id: str) -> str | None:
        FakeSlackClient.user_calls[user_id] = FakeSlackClient.user_calls.get(user_id, 0) + 1
        return {"U1": "alice", "U2": "bob"}.get(user_id)

    async def thread_replies(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        max_replies: int,
        include_root: bool = False,
    ) -> SlackThreadFetchResult:
        FakeSlackClient.replies_calls += 1
        replies = REPLIES[:max_replies]
        return SlackThreadFetchResult(root=ROOT, replies=replies, messages=[ROOT, *replies])


class PaginatedFakeSlackClient(FakeSlackClient):
    async def thread_replies(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        max_replies: int,
        include_root: bool = False,
    ) -> SlackThreadFetchResult:
        FakeSlackClient.replies_calls += 1
        pages = [[REPLIES[0]], [REPLIES[1]]]
        replies = [reply for page in pages for reply in page][:max_replies]
        return SlackThreadFetchResult(root=ROOT, replies=replies, messages=[ROOT, *replies])


class SystemMessageFakeSlackClient(FakeSlackClient):
    async def conversation_history(self, *, channel_id: str, limit: int) -> list[dict[str, Any]]:
        _ = channel_id, limit
        dirty_alert = {
            "ts": "1779307031.278049",
            "text": (
                "[FIRING:1] InstanceDown\n"
                "Service: * kubernetes-pods-annotated\n"
                "Cluster: * minikube\n"
                "Namespace: * cert-manager\n"
                "Pod: * cert-manager-cainjector-5d5f946fd-8jp45\n"
                "Description: critical - pod unreachable"
            ),
        }
        system = {
            "ts": "1779307000.000000",
            "subtype": "channel_join",
            "text": "<@U0BCU8ZPAMB> has joined the channel",
        }
        return [system, dirty_alert]


class VerboseAlertNameFakeSlackClient(FakeSlackClient):
    async def conversation_history(self, *, channel_id: str, limit: int) -> list[dict[str, Any]]:
        _ = channel_id, limit
        return [
            {
                "ts": "1779307031.278049",
                "text": (
                    "[FIRING:1] InstanceDown kubernetes-pods-annotated critical | "
                    "<http://prometheus-alertmanager-0:9093/#/alerts?receiver=slack-notifications>\n"
                    "Cluster: minikube\n"
                    "Namespace: cert-manager\n"
                    "Description: critical - pod unreachable"
                ),
            }
        ]


class ErrorFakeSlackClient(FakeSlackClient):
    async def thread_replies(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        max_replies: int,
        include_root: bool = False,
    ) -> SlackThreadFetchResult:
        return SlackThreadFetchResult(root=ROOT, replies=[], messages=[ROOT], warning="ratelimited")


class DuplicateThreadFakeSlackClient(FakeSlackClient):
    async def conversation_history(self, *, channel_id: str, limit: int) -> list[dict[str, Any]]:
        first = dict(ROOT)
        second = dict(ROOT)
        second["ts"] = "1710000000.000200"
        second["thread_ts"] = ROOT["ts"]
        return [first, second]


class DuplicateAlertFakeSlackClient(FakeSlackClient):
    async def conversation_history(self, *, channel_id: str, limit: int) -> list[dict[str, Any]]:
        _ = channel_id, limit
        first = dict(ROOT)
        first["ts"] = "1710000000.000100"
        second = dict(ROOT)
        second["ts"] = "1710000060.000100"
        return [first, second]


class RepeatedUserFakeSlackClient(FakeSlackClient):
    async def thread_replies(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        max_replies: int,
        include_root: bool = False,
    ) -> SlackThreadFetchResult:
        replies = [
            {
                "ts": "1710000001.000100",
                "user": "U1",
                "text": "I think endpoints are stale",
            },
            {
                "ts": "1710000002.000100",
                "user": "U1",
                "text": "kubectl get endpoints -A",
            },
        ][:max_replies]
        return SlackThreadFetchResult(root=ROOT, replies=replies, messages=[ROOT, *replies])


class UserResolutionFailureFakeSlackClient(FakeSlackClient):
    async def resolve_user(self, user_id: str) -> str | None:
        FakeSlackClient.user_calls[user_id] = FakeSlackClient.user_calls.get(user_id, 0) + 1
        return None


class BotReplyFakeSlackClient(FakeSlackClient):
    async def thread_replies(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        max_replies: int,
        include_root: bool = False,
    ) -> SlackThreadFetchResult:
        replies = [
            {
                "ts": "1710000001.000100",
                "bot_id": "B1",
                "bot_profile": {"name": "incidentflow-bot"},
                "text": "fixed after restart",
            },
            {
                "ts": "1710000002.000100",
                "user": "U1",
                "username": "incidentflow",
                "text": "resolved after rollback",
            },
        ][:max_replies]
        return SlackThreadFetchResult(root=ROOT, replies=replies, messages=[ROOT, *replies])


@pytest.fixture(autouse=True)
def reset_fake() -> None:
    FakeSlackClient.history_calls = 0
    FakeSlackClient.replies_calls = 0
    FakeSlackClient.user_calls = {}


@pytest.mark.asyncio
async def test_existing_slack_alerts_list_default_has_no_thread_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", FakeSlackClient)

    result = await slack_alerts.fetch_slack_alerts(
        token="x",
        channel="#alerts",
        limit=10,
    )

    assert result.alerts[0].alert_name == "InstanceDown"
    assert result.alerts[0].thread is None
    assert FakeSlackClient.replies_calls == 0


@pytest.mark.asyncio
async def test_slack_alerts_list_filters_system_messages_and_normalizes_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", SystemMessageFakeSlackClient)

    result = await slack_alerts.fetch_slack_alerts(
        token="x",
        channel="#alerts",
        limit=10,
    )

    assert result.returned == 1
    alert = result.alerts[0]
    assert alert.alert_id == "slack-1779307031.278049"
    assert alert.name == "InstanceDown"
    assert alert.service == "kubernetes-pods-annotated"
    assert alert.cluster == "minikube"
    assert alert.namespace == "cert-manager"
    assert alert.severity == "critical"


@pytest.mark.asyncio
async def test_slack_alerts_list_returns_clean_name_and_alertmanager_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", VerboseAlertNameFakeSlackClient)

    result = await slack_alerts.fetch_slack_alerts(
        token="x",
        channel="#alerts",
        limit=10,
    )

    alert = result.alerts[0]
    assert alert.name == "InstanceDown"
    assert alert.display_name == "InstanceDown kubernetes-pods-annotated critical"
    assert alert.alertmanager_url == (
        "http://prometheus-alertmanager-0:9093/#/alerts?receiver=slack-notifications"
    )
    assert alert.service == "kubernetes-pods-annotated"
    assert alert.severity == "critical"


@pytest.mark.asyncio
async def test_slack_alerts_list_metadata_returns_thread_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", FakeSlackClient)

    result = await slack_alerts.fetch_slack_alerts(
        token="x",
        channel="#alerts",
        limit=10,
        include_threads=True,
        thread_mode="metadata",
    )

    assert result.alerts[0].slack is not None
    assert result.alerts[0].thread is not None
    assert result.alerts[0].thread.reply_count == 2
    assert result.alerts[0].thread.replies == []
    assert FakeSlackClient.replies_calls == 0


@pytest.mark.asyncio
async def test_slack_alerts_list_full_returns_replies_and_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", FakeSlackClient)

    result = await slack_alerts.fetch_slack_alerts(
        token="x",
        channel="#alerts",
        limit=10,
        include_threads=True,
        thread_mode="full",
    )

    thread = result.alerts[0].thread
    assert thread is not None
    assert len(thread.replies) == 2
    assert [reply.username for reply in thread.replies] == ["alice", "bob"]
    assert thread.analysis is not None
    assert thread.analysis.commands_found == ["kubectl get pods -n cert-manager"]
    assert thread.analysis.resolution_signal is True


@pytest.mark.asyncio
async def test_slack_alert_thread_get_returns_root_thread_and_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", FakeSlackClient)

    result = await slack_alerts.fetch_slack_alert_thread(
        token="x",
        channel_id="C12345678",
        message_ts="1710000000.000100",
    )

    assert result.root_alert is not None
    assert result.root_alert.alert_name == "InstanceDown"
    assert result.thread.reply_count == 2
    assert result.analysis.resolution_signal is True


@pytest.mark.asyncio
async def test_thread_fetch_handles_pagination_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", PaginatedFakeSlackClient)

    result = await slack_alerts.fetch_slack_alert_thread(
        token="x",
        channel_id="C12345678",
        message_ts="1710000000.000100",
        max_replies=2,
    )

    assert len(result.thread.replies) == 2


@pytest.mark.asyncio
async def test_thread_fetch_rate_limit_warning_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", ErrorFakeSlackClient)

    result = await slack_alerts.fetch_slack_alerts(
        token="x",
        channel="#alerts",
        limit=10,
        include_threads=True,
        thread_mode="full",
        deduplicate=False,
    )

    assert result.alerts[0].thread is not None
    assert result.alerts[0].thread.warning == "ratelimited"


@pytest.mark.asyncio
async def test_full_mode_dedupes_thread_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", DuplicateThreadFakeSlackClient)

    result = await slack_alerts.fetch_slack_alerts(
        token="x",
        channel="#alerts",
        limit=10,
        include_threads=True,
        thread_mode="full",
        deduplicate=False,
    )

    assert len(result.alerts) == 2
    assert FakeSlackClient.replies_calls == 1


@pytest.mark.asyncio
async def test_alert_list_deduplicates_repeated_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", DuplicateAlertFakeSlackClient)

    result = await slack_alerts.fetch_slack_alerts(
        token="x",
        channel="#alerts",
        limit=10,
    )

    assert result.parsed == 2
    assert result.returned == 1
    alert = result.alerts[0]
    assert alert.fingerprint is not None
    assert alert.occurrences == 2
    assert alert.deduplicated is True
    assert alert.first_seen == "2024-03-09T16:00:00.000100+00:00"
    assert alert.last_seen == "2024-03-09T16:01:00.000100+00:00"


@pytest.mark.asyncio
async def test_alert_list_can_disable_deduplication(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", DuplicateAlertFakeSlackClient)

    result = await slack_alerts.fetch_slack_alerts(
        token="x",
        channel="#alerts",
        limit=10,
        deduplicate=False,
    )

    assert result.deduplicated is False
    assert result.parsed == 2
    assert result.returned == 2
    assert [alert.occurrences for alert in result.alerts] == [1, 1]


@pytest.mark.asyncio
async def test_full_mode_caches_user_resolution_per_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", RepeatedUserFakeSlackClient)

    result = await slack_alerts.fetch_slack_alert_thread(
        token="x",
        channel_id="C12345678",
        message_ts="1710000000.000100",
    )

    assert [reply.username for reply in result.thread.replies] == ["alice", "alice"]
    assert FakeSlackClient.user_calls == {"U1": 1}


@pytest.mark.asyncio
async def test_user_resolution_failure_keeps_reply_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", UserResolutionFailureFakeSlackClient)

    result = await slack_alerts.fetch_slack_alert_thread(
        token="x",
        channel_id="C12345678",
        message_ts="1710000000.000100",
        max_replies=1,
    )

    assert result.thread.replies[0].user == "U1"
    assert result.thread.replies[0].username is None
    assert FakeSlackClient.user_calls == {"U1": 1}


@pytest.mark.asyncio
async def test_bot_and_existing_username_fallbacks_skip_user_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", BotReplyFakeSlackClient)

    result = await slack_alerts.fetch_slack_alert_thread(
        token="x",
        channel_id="C12345678",
        message_ts="1710000000.000100",
    )

    assert [reply.user for reply in result.thread.replies] == ["B1", "U1"]
    assert [reply.username for reply in result.thread.replies] == [
        "incidentflow-bot",
        "incidentflow",
    ]
    assert FakeSlackClient.user_calls == {}


@pytest.mark.asyncio
async def test_incident_thread_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", FakeSlackClient)

    result = await slack_alerts.summarize_incident_thread(
        token="x",
        channel_id="C12345678",
        thread_ts="1710000000.000100",
        alert_context={"alert_name": "InstanceDown"},
    )

    assert result["title"] == "InstanceDown"
    assert result["commands"] == ["kubectl get pods -n cert-manager"]


@pytest.mark.asyncio
async def test_incident_thread_summary_flags_stale_cross_cluster_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", FakeSlackClient)

    result = await slack_alerts.summarize_incident_thread(
        token="x",
        channel_id="C12345678",
        thread_ts="1710000000.000100",
        alert_context={
            "alert_name": "InstanceDown",
            "datetime_utc": "2024-03-09T16:00:00Z",
            "labels": {"cluster": "minikube"},
            "expected_cluster": "incidentflow-dev",
        },
    )

    assert any("minikube" in risk and "incidentflow-dev" in risk for risk in result["risks"])
    assert any("stale" in risk for risk in result["risks"])
    assert result["open_questions"]


def test_thread_mode_aliases_normalize_to_full() -> None:
    assert normalize_slack_thread_mode("summarize") == "full"
    assert normalize_slack_thread_mode("analysis") == "full"
    assert normalize_slack_thread_mode(" full ") == "full"


def test_unknown_thread_mode_still_raises() -> None:
    with pytest.raises(ValueError, match="none, metadata, full"):
        normalize_slack_thread_mode("deep")


@pytest.mark.asyncio
async def test_slack_alerts_list_rejects_zero_limit_before_slack_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "incidentflow_mcp.config._settings",
        Settings(_env_file=None, environment="development", redis_url="redis://test-only"),
    )

    async def allow_tool(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        return None

    monkeypatch.setattr(
        "incidentflow_mcp.mcp.server.resolve_tool_integration_context",
        allow_tool,
    )
    set_current_auth_context(
        {
            "authenticated": True,
            "auth_method": "oauth",
            "bearer_token": "token",
            "client_id": "oauth-client",
            "workspace_id": "ws_123",
            "workspace_name": "Demo Workspace",
            "workspace_slug": "demo",
            "workspace_role": "owner",
            "user_id": "user_123",
            "email": "demo@example.com",
            "plan": None,
        }
    )
    try:
        result = await create_mcp_server()._tool_manager.call_tool(
            "slack_alerts_list",
            {"channel": "alerts", "limit": 0},
        )
    finally:
        clear_current_auth_context()

    assert result["status"] == "failed"
    assert result["error"]["message"] == "limit must be between 1 and 200"


# ──────────────────────────────────────────────
# include_raw compact/redacted behavior
# ──────────────────────────────────────────────

RAW_ROOT = {
    "ts": "1710000000.000100",
    "text": (
        "[FIRING:1] PodUnreachable\nCluster: incidentflow\nNamespace: default\n"
        "Description: critical - pod 10.0.5.42 unreachable\n"
        "kubectl describe pod api-0 -n default"
    ),
    "reply_count": 0,
    "reply_users": [],
}


class RawTextFakeSlackClient(FakeSlackClient):
    async def thread_replies(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        max_replies: int,
        include_root: bool = False,
    ) -> SlackThreadFetchResult:
        return SlackThreadFetchResult(root=RAW_ROOT, replies=[], messages=[RAW_ROOT])


@pytest.mark.asyncio
async def test_thread_get_default_omits_raw_and_extracts_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", RawTextFakeSlackClient)

    result = await slack_alerts.fetch_slack_alert_thread(
        token="x",
        channel_id="C12345678",
        message_ts="1710000000.000100",
    )

    root = result.root_alert
    assert root is not None
    assert root.raw_text is None
    assert root.extracted_commands == ["kubectl describe pod api-0 -n default"]
    assert "10.0.5.42" not in root.summary
    assert "[redacted-ip]" in root.summary


@pytest.mark.asyncio
async def test_thread_get_include_raw_returns_raw_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slack_alerts, "SlackClient", RawTextFakeSlackClient)

    result = await slack_alerts.fetch_slack_alert_thread(
        token="x",
        channel_id="C12345678",
        message_ts="1710000000.000100",
        include_raw=True,
    )

    root = result.root_alert
    assert root is not None
    assert root.raw_text is not None
    assert "10.0.5.42" in root.raw_text
    assert root.extracted_commands == ["kubectl describe pod api-0 -n default"]
