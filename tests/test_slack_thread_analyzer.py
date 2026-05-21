from incidentflow_mcp.slack.thread_analyzer import (
    analyze_replies,
    analyze_reply,
    extract_commands,
    extract_links,
    extract_resolutions,
    summarize_thread_for_sre,
)


def test_extract_commands() -> None:
    text = """
    I checked this:
    kubectl get pods -n cert-manager
    helm status cert-manager
    curl https://example.com/health
    """
    assert extract_commands(text) == [
        "kubectl get pods -n cert-manager",
        "helm status cert-manager",
        "curl https://example.com/health",
    ]


def test_normalizes_slack_markdown_and_raw_links() -> None:
    links = extract_links(
        "<https://grafana.example/d/abc|Grafana dashboard> "
        "[Runbook](https://confluence.example/runbook/cert-manager) "
        "https://kibana.example/app/discover"
    )
    assert [link.type for link in links] == ["grafana", "runbook", "kibana"]
    assert links[0].label == "Grafana dashboard"
    assert links[1].label == "Runbook"


def test_detects_multilingual_hypotheses_and_resolution_signals() -> None:
    replies = [
        analyze_reply(text="I think service: cert-manager is down", ts="1", user="U1"),
        analyze_reply(text="похоже namespace cert-manager flapping", ts="2", user="U2"),
        analyze_reply(text="схоже pod restarted", ts="3", user="U3"),
        analyze_reply(text="fixed after rollback", ts="4", user="U1"),
    ]
    analysis = analyze_replies(replies)

    assert len(analysis.engineer_hypotheses) == 3
    assert analysis.resolution_signal is True
    assert analysis.resolution_confidence == "medium"
    assert "cert-manager" in analysis.mentioned_services


def test_sre_summary_does_not_execute_commands() -> None:
    replies = [
        analyze_reply(text="kubectl delete pod nope", ts="1", user="U1"),
        analyze_reply(text="resolved after restart", ts="2", user="U1"),
    ]
    summary = summarize_thread_for_sre(
        replies=replies,
        alert_context={"alert_name": "InstanceDown"},
    )

    assert summary["title"] == "InstanceDown"
    assert summary["commands"] == ["kubectl delete pod nope"]
    assert summary["status"] == "mitigated"


def test_positive_resolution_signals_still_work() -> None:
    replies = [
        analyze_reply(text="resolved after rollback", ts="1", user="U1"),
        analyze_reply(text="решено после рестарта", ts="2", user="U2"),
    ]
    analysis = analyze_replies(replies)

    assert analysis.resolution_signal is True
    assert analysis.resolution_confidence == "medium"
    assert analysis.possible_resolution == "решено после рестарта"


def test_negated_resolution_phrases_do_not_create_resolution_signal() -> None:
    samples = [
        "not resolved yet",
        "not fixed yet",
        "not mitigated",
        "not done",
        "не resolved",
        "не считаю resolved",
        "не решено",
        "не починили",
        "не зафиксили",
        "ще не вирішено",
        "не вирішено",
    ]

    for text in samples:
        reply = analyze_reply(text=text, ts="1", user="U1")
        analysis = analyze_replies([reply])
        assert reply.contains_resolution is False, text
        assert reply.resolutions == [], text
        assert analysis.resolution_signal is False, text
        assert analysis.resolution_confidence == "low", text
        assert analysis.possible_resolution is None, text


def test_separate_positive_resolution_beats_negated_resolution() -> None:
    replies = [
        analyze_reply(text="не считаю resolved yet", ts="1", user="U1"),
        analyze_reply(text="fixed after restart", ts="2", user="U2"),
    ]
    analysis = analyze_replies(replies)

    assert analysis.resolution_signal is True
    assert analysis.possible_resolution == "fixed after restart"


def test_extract_resolutions_filters_negation_in_same_line_only() -> None:
    assert extract_resolutions("not fixed yet") == []
    assert extract_resolutions("not sure.\nfixed after restart") == ["fixed after restart"]
