"""Kubernetes analysis and payload shaping helpers."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any


def _container_restart_count(container: dict[str, Any]) -> int:
    try:
        return int(container.get("restart_count") or container.get("restartCount") or 0)
    except (TypeError, ValueError):
        return 0


def _parse_k8s_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_k8s_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _container_last_restart_at(container: dict[str, Any]) -> datetime | None:
    direct = _parse_k8s_timestamp(
        container.get("last_restart_at") or container.get("lastRestartAt")
    )
    if direct is not None:
        return direct
    last_state = container.get("last_state") or container.get("lastState") or {}
    if not isinstance(last_state, dict):
        return None
    terminated = last_state.get("terminated") or {}
    if not isinstance(terminated, dict):
        return None
    return _parse_k8s_timestamp(terminated.get("finished_at") or terminated.get("finishedAt"))


def _container_last_termination(container: dict[str, Any]) -> dict[str, Any] | None:
    last_state = container.get("last_state") or container.get("lastState") or {}
    if not isinstance(last_state, dict):
        return None
    terminated = last_state.get("terminated") or {}
    if not isinstance(terminated, dict) or not terminated:
        return None
    return terminated


def _restart_window_summary(
    containers: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    checked_at = now or datetime.now(tz=UTC)
    last_restart_at: datetime | None = None
    restart_count_total = 0
    has_restart_in_last_1h = False
    has_restart_in_last_24h = False
    for container in containers:
        restart_count = _container_restart_count(container)
        if restart_count <= 0:
            continue
        restart_count_total += restart_count
        restarted_at = _container_last_restart_at(container)
        if restarted_at is None:
            continue
        if last_restart_at is None or restarted_at > last_restart_at:
            last_restart_at = restarted_at
        age = checked_at - restarted_at
        if age.total_seconds() < 0:
            continue
        if age <= timedelta(hours=1):
            has_restart_in_last_1h = True
        if age <= timedelta(hours=24):
            has_restart_in_last_24h = True
    return {
        "restart_count_total": restart_count_total,
        "last_restart_at": _format_k8s_timestamp(last_restart_at),
        "has_restart_in_last_1h": has_restart_in_last_1h,
        "has_restart_in_last_24h": has_restart_in_last_24h,
        # Kubernetes Pod status exposes cumulative restart_count and the latest restart
        # timestamp, not an event histogram. Keep the old keys for response compatibility,
        # but avoid reporting the cumulative count as a time-window count.
        "restarts_last_1h": int(has_restart_in_last_1h),
        "restarts_last_24h": int(has_restart_in_last_24h),
    }


def _pod_restart_count(pod: dict[str, Any]) -> int:
    containers = pod.get("containers")
    if not isinstance(containers, list):
        return 0
    return sum(
        _container_restart_count(container)
        for container in containers
        if isinstance(container, dict)
    )


def _pod_brief(pod: dict[str, Any]) -> dict[str, Any]:
    containers = [c for c in (pod.get("containers") or []) if isinstance(c, dict)]
    return {
        "namespace": pod.get("namespace"),
        "pod": pod.get("name"),
        "phase": pod.get("phase"),
        "node": pod.get("node_name") or pod.get("nodeName"),
        "restarts": _pod_restart_count(pod),
        **_restart_window_summary(containers),
    }


def _top_restarts(pods: list[Any], limit: int = 10) -> list[dict[str, Any]]:
    rows = [
        _pod_brief(pod) for pod in pods if isinstance(pod, dict) and _pod_restart_count(pod) > 0
    ]
    rows.sort(key=lambda item: int(item.get("restarts") or 0), reverse=True)
    return rows[:limit]


def _warning_events(events: list[Any], limit: int = 10) -> list[dict[str, Any]]:
    warnings = [
        event
        for event in events
        if isinstance(event, dict) and str(event.get("type") or "").lower() == "warning"
    ]
    warnings.sort(
        key=lambda item: str(item.get("last_seen") or item.get("lastSeen") or ""),
        reverse=True,
    )
    return warnings[:limit]


def _is_ready_pod(pod: dict[str, Any]) -> bool:
    if _is_completed_pod(pod):
        return False
    if str(pod.get("phase") or "").lower() != "running":
        return False
    containers = pod.get("containers")
    if not isinstance(containers, list) or not containers:
        return False
    return all(
        bool(container.get("ready")) for container in containers if isinstance(container, dict)
    )


def _event_pod_name(event: dict[str, Any]) -> str | None:
    involved = event.get("involved_object") or event.get("involvedObject") or event.get("object")
    if isinstance(involved, dict):
        kind = str(involved.get("kind") or "").lower()
        name = str(involved.get("name") or "").strip()
        if kind == "pod" and name:
            return name
    value = event.get("object") or event.get("involved_object_name") or event.get("name")
    if isinstance(value, str):
        match = re.search(r"\bpod/([^\s]+)", value, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _classify_warning_event(
    event: dict[str, Any],
    *,
    pods_by_name: dict[str, dict[str, Any]],
    now: datetime,
    stale_after_minutes: int = 15,
) -> dict[str, Any]:
    last_seen = (
        event.get("last_seen")
        or event.get("lastSeen")
        or event.get("lastTimestamp")
        or event.get("eventTime")
    )
    parsed_last_seen = _parse_k8s_timestamp(last_seen)
    age_minutes = (
        round((now - parsed_last_seen).total_seconds() / 60, 1)
        if parsed_last_seen is not None
        else None
    )
    pod_name = _event_pod_name(event)
    pod = pods_by_name.get(pod_name or "")
    pod_ready = _is_ready_pod(pod) if pod is not None else False
    stale = age_minutes is not None and age_minutes >= stale_after_minutes
    pod_was_replaced = pod_name is not None and pod is None
    classification = (
        "stale_rollout_warning" if stale and (pod_ready or pod_was_replaced) else "active_warning"
    )

    return {
        **event,
        "pod": pod_name,
        "pod_exists": pod is not None if pod_name else None,
        "pod_ready": pod_ready,
        "age_minutes": age_minutes,
        "classification": classification,
    }


def _warning_event_summary(events: list[Any], pods: list[Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    pods_by_name = {
        str(pod.get("name")): pod for pod in pods if isinstance(pod, dict) and pod.get("name")
    }
    classified = [
        _classify_warning_event(event, pods_by_name=pods_by_name, now=now)
        for event in _warning_events(events, limit=50)
        if isinstance(event, dict)
    ]
    active = [event for event in classified if event["classification"] == "active_warning"]
    stale = [event for event in classified if event["classification"] == "stale_rollout_warning"]
    return {
        "active_warning_events": len(active),
        "stale_rollout_warning_events": len(stale),
        "active_examples": active[:5],
        "stale_examples": stale[:5],
    }


def _is_unhealthy_pod(pod: dict[str, Any]) -> bool:
    phase = str(pod.get("phase") or "").lower()
    if phase == "succeeded":
        return False
    if phase != "running":
        return True
    containers = pod.get("containers")
    if not isinstance(containers, list):
        return False
    for container in containers:
        if not isinstance(container, dict):
            continue
        if container.get("ready") is False:
            return True
        try:
            if int(container.get("restart_count") or container.get("restartCount") or 0) > 5:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _is_completed_pod(pod: dict[str, Any]) -> bool:
    return str(pod.get("phase") or "").lower() == "succeeded"


def _labels_match_selector(labels: Any, selector: Any) -> bool:
    if not isinstance(labels, dict) or not isinstance(selector, dict) or not selector:
        return False
    return all(str(labels.get(key)) == str(value) for key, value in selector.items())


def _deployment_selector(deployment: dict[str, Any]) -> dict[str, Any]:
    for key in ("selector", "match_labels", "matchLabels"):
        value = deployment.get(key)
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
    spec = deployment.get("spec")
    if isinstance(spec, dict):
        selector = spec.get("selector")
        if isinstance(selector, dict):
            match_labels = selector.get("matchLabels") or selector.get("match_labels")
            if isinstance(match_labels, dict):
                return {str(k): str(v) for k, v in match_labels.items()}
    return {}


def _pod_labels(pod: dict[str, Any]) -> dict[str, Any]:
    for key in ("labels", "metadata_labels"):
        value = pod.get(key)
        if isinstance(value, dict):
            return value
    metadata = pod.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("labels"), dict):
        return metadata["labels"]
    return {}


def _strip_image_digest(image: str) -> str:
    """Remove @sha256:... digest from an image reference, keep repo:tag."""
    at = image.find("@")
    return image[:at] if at != -1 else image


def _sanitize_pod(
    pod: dict[str, Any],
    *,
    include_labels: bool = False,
    include_images: bool = True,
    include_node: bool = True,
) -> dict[str, Any]:
    """Allowlist-based pod summary safe for SaaS output.

    Never exposes labels, node internals, image digests, annotations,
    env vars, volumes, serviceAccount, ownerReferences, or containerIDs.
    """
    containers_raw = pod.get("containers") or []
    containers: list[dict[str, Any]] = []
    for c in containers_raw:
        if not isinstance(c, dict):
            continue
        entry: dict[str, Any] = {
            "name": str(c.get("name") or ""),
            "ready": bool(c.get("ready")),
            "restart_count": int(c.get("restart_count") or 0),
        }
        if include_images:
            entry["image"] = _strip_image_digest(str(c.get("image") or ""))
        containers.append(entry)

    all_ready = bool(containers) and all(c["ready"] for c in containers)
    total_restarts = sum(c["restart_count"] for c in containers)

    summary: dict[str, Any] = {
        "name": str(pod.get("name") or ""),
        "namespace": str(pod.get("namespace") or ""),
        "phase": str(pod.get("phase") or ""),
        "ready": all_ready,
        "restarts": total_restarts,
        "age": str(pod.get("age") or ""),
        "containers": containers,
    }
    if include_node:
        summary["node"] = str(pod.get("node_name") or "")
    if include_labels:
        raw_labels = pod.get("labels")
        if isinstance(raw_labels, dict):
            summary["labels"] = raw_labels
    return summary


def _filter_workload_pods(
    pods: list[Any],
    deployments: list[Any],
    workload: str,
) -> list[dict[str, Any]]:
    workload = workload.strip()
    candidates = [pod for pod in pods if isinstance(pod, dict)]
    if not workload:
        return []

    exact_pod = [pod for pod in candidates if str(pod.get("name") or "") == workload]
    if exact_pod:
        return exact_pod

    deployment = next(
        (
            item
            for item in deployments
            if isinstance(item, dict) and str(item.get("name") or "") == workload
        ),
        None,
    )
    if deployment is not None:
        selector = _deployment_selector(deployment)
        matched = [pod for pod in candidates if _labels_match_selector(_pod_labels(pod), selector)]
        if matched:
            return matched

    return [pod for pod in candidates if str(pod.get("name") or "").startswith(f"{workload}-")]


def _workload_from_pod_name(pod_name: str) -> str:
    """Derive deployment/workload name by stripping random k8s suffixes.

    incidentflow-mcp-76f5987dc5-j5r6d  ->  incidentflow-mcp
    my-service-6d7f9b-xk2z9            ->  my-service
    standalone-pod                     ->  standalone-pod (unchanged)
    """
    import re as _re

    # ReplicaSet pods: {deployment}-{rs-hash~10}-{pod-hash~5}
    m = _re.match(r"^(.+?)-[a-z0-9]{9,10}-[a-z0-9]{5}$", pod_name)
    if m:
        return m.group(1)
    # DaemonSet / StatefulSet: {name}-{hash5}
    m = _re.match(r"^(.+?)-[a-z0-9]{5}$", pod_name)
    if m:
        return m.group(1)
    return pod_name


def _deduplicate_events(events: list[Any]) -> list[dict[str, Any]]:
    """Collapse repeated events into single entries with occurrence counts."""
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        reason = str(event.get("reason") or "")
        message = str(event.get("message") or "")[:120]
        involved = event.get("involved_object") or event.get("object") or {}
        obj_name = str(involved.get("name") if isinstance(involved, dict) else "")
        namespace = str(event.get("namespace") or "")
        key = (namespace, obj_name, reason, message)
        if key not in groups:
            entry = dict(event)
            entry["count"] = int(event.get("count") or 1)
            groups[key] = entry
        else:
            existing = groups[key]
            existing["count"] = existing.get("count", 1) + int(event.get("count") or 1)
            new_ls = str(event.get("last_seen") or event.get("lastSeen") or "")
            old_ls = str(existing.get("last_seen") or existing.get("lastSeen") or "")
            if new_ls > old_ls:
                existing["last_seen"] = new_ls
    return list(groups.values())


def _sort_events_for_display(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort events: warnings first, then newest first within each group."""

    def _key(e: dict[str, Any]) -> tuple[int, float]:
        type_order = 0 if str(e.get("type") or "").lower() == "warning" else 1
        last_seen = e.get("last_seen") or e.get("lastSeen") or e.get("lastTimestamp") or ""
        ts = _parse_k8s_timestamp(str(last_seen))
        return (type_order, -ts.timestamp() if ts is not None else 0.0)

    return sorted(events, key=_key)


def _events_for_pod(events: list[Any], pod_name: str) -> list[dict[str, Any]]:
    """Filter an event list to events that involve a specific pod."""
    result: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        involved = event.get("involved_object") or event.get("object") or {}
        if isinstance(involved, dict):
            kind = str(involved.get("kind") or "").lower()
            name = str(involved.get("name") or "")
            if kind == "pod" and name == pod_name:
                result.append(event)
                continue
        obj_str = str(event.get("object") or "")
        if f"pod/{pod_name}" in obj_str.lower() or f"Pod/{pod_name}" in obj_str:
            result.append(event)
    return result


def _diagnose_pod(
    pod_raw: dict[str, Any],
    pod_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Detect common pod failure patterns from pod data and filtered events."""
    phase = str(pod_raw.get("phase") or "").lower()
    containers = [c for c in (pod_raw.get("containers") or []) if isinstance(c, dict)]
    total_restarts = sum(_container_restart_count(c) for c in containers)
    not_ready = [c for c in containers if not c.get("ready")]

    event_reasons: set[str] = set()
    event_messages: list[str] = []
    for e in pod_events:
        if isinstance(e, dict):
            r = str(e.get("reason") or "").lower()
            if r:
                event_reasons.add(r)
            event_messages.append(str(e.get("message") or "").lower())

    issues: list[dict[str, Any]] = []
    recommendations: list[str] = []

    if "backoff" in event_reasons or "crashloopbackoff" in event_reasons:
        issues.append({"type": "CrashLoopBackOff", "severity": "critical"})
        recommendations.append("Run k8s_get_pod_logs to find the crash reason")

    if "imagepullbackoff" in event_reasons or "errimagepull" in event_reasons:
        issues.append({"type": "ImagePullBackOff", "severity": "critical"})
        recommendations.append("Check image name, tag, and registry credentials")
    elif any("pull" in msg for msg in event_messages) and "failed" in event_reasons:
        issues.append({"type": "ImagePullFailure", "severity": "critical"})
        recommendations.append("Check image name, tag, and registry credentials")

    if "oomkilling" in event_reasons or any("oom" in msg for msg in event_messages):
        issues.append({"type": "OOMKilled", "severity": "critical"})
        recommendations.append(
            "Container exceeded memory limit — increase resources.limits.memory or fix memory leak"
        )

    if "failedscheduling" in event_reasons:
        issues.append({"type": "FailedScheduling", "severity": "warning"})
        recommendations.append("Check node resources and pod resource requests")

    readiness_msgs = [msg for msg in event_messages if "readiness" in msg]
    liveness_msgs = [msg for msg in event_messages if "liveness" in msg]
    startup_msgs = [msg for msg in event_messages if "startup" in msg]
    if "unhealthy" in event_reasons:
        current_probe_failure = bool(not_ready) or phase != "running"
        restart_probe_failure = total_restarts > 0
        if readiness_msgs and current_probe_failure:
            issues.append({"type": "ReadinessProbeFailure", "severity": "warning"})
            recommendations.append("Check readiness probe endpoint and application startup time")
        if liveness_msgs and (current_probe_failure or restart_probe_failure):
            issues.append({"type": "LivenessProbeFailure", "severity": "warning"})
            recommendations.append("Check liveness probe — container may be restarting")
        if startup_msgs and current_probe_failure:
            issues.append({"type": "StartupProbeFailure", "severity": "warning"})
            recommendations.append("Startup probe failed — consider increasing initialDelaySeconds")

    if total_restarts > 5 and not any(i["type"] == "CrashLoopBackOff" for i in issues):
        issues.append({"type": "HighRestartCount", "count": total_restarts, "severity": "warning"})
        recommendations.append(
            f"Pod has restarted {total_restarts} times — check logs for past crash reasons"
        )

    if phase == "pending":
        if not any(i["type"] == "FailedScheduling" for i in issues):
            issues.append({"type": "Pending", "severity": "warning"})
            recommendations.append(
                "Pod is waiting — check events for scheduling or image pull issues"
            )
    elif phase == "failed":
        issues.append({"type": "PodFailed", "severity": "critical"})
        recommendations.append("Pod is in Failed state — check logs for exit reason")
    elif phase == "unknown":
        issues.append({"type": "UnknownPhase", "severity": "warning"})
        recommendations.append("Node may be unreachable — check node status")

    if not_ready and not issues and phase == "running":
        issues.append(
            {
                "type": "ContainersNotReady",
                "containers": [c.get("name") for c in not_ready],
                "severity": "warning",
            }
        )
        recommendations.append(
            "Containers are not ready — check readiness probe and application startup"
        )

    historical_warnings: list[str] = []
    if "unhealthy" in event_reasons and not issues and phase == "running" and not not_ready:
        if readiness_msgs:
            historical_warnings.append("ReadinessProbeFailure")
        if liveness_msgs:
            historical_warnings.append("LivenessProbeFailure")
        if startup_msgs:
            historical_warnings.append("StartupProbeFailure")

    healthy = not issues and phase == "running" and not not_ready
    return {
        "healthy": healthy,
        "issues": issues,
        "historical_warnings": historical_warnings,
        "recommendations": list(dict.fromkeys(recommendations)),
    }


def _pod_observations(
    *,
    healthy: bool,
    total_restarts: int,
    last_restart_at: str | None = None,
    historical_warnings: list[Any] | None = None,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    if healthy and total_restarts > 0:
        observation: dict[str, Any] = {
            "severity": "info",
            "code": "HISTORICAL_RESTART",
            "message": (
                f"Container restarted {total_restarts} time"
                f"{'s' if total_restarts != 1 else ''} during the pod lifetime"
                + (f"; last restart at {last_restart_at}" if last_restart_at else "")
            ),
            "count": total_restarts,
        }
        if last_restart_at:
            observation["last_restart_at"] = last_restart_at
        observations.append(observation)

    for warning in historical_warnings or []:
        warning_dict = warning if isinstance(warning, dict) else {}
        code = warning_dict.get("type") if warning_dict else str(warning)
        if not code:
            continue
        if warning_dict and code == "PreviousContainerTermination":
            observation = {
                "severity": warning_dict.get("severity") or "info",
                "code": str(code),
                "message": warning_dict.get("message")
                or "Container was previously terminated and the pod recovered",
                "container": warning_dict.get("container"),
                "exit_code": warning_dict.get("exit_code"),
                "reason": warning_dict.get("reason"),
                "finished_at": warning_dict.get("finished_at"),
            }
            observations.append({k: v for k, v in observation.items() if v is not None})
        else:
            observations.append(
                {
                    "severity": "info",
                    "code": str(code),
                    "message": "Historical pod warning observed during startup or rollout",
                }
            )

    return observations


def _pod_recommendations(
    diagnosis_recommendations: list[str],
    *,
    healthy: bool,
    total_restarts: int,
) -> list[str]:
    if healthy and total_restarts > 0 and not diagnosis_recommendations:
        return [
            (
                "No immediate action required. Check the previous container termination reason "
                "if the restart was recent or recurring."
            )
        ]
    return diagnosis_recommendations


def _pod_next_actions(
    *,
    namespace: str,
    pod: str,
    healthy: bool,
    total_restarts: int,
    source_tool: str,
) -> list[dict[str, Any]]:
    if healthy and total_restarts > 0:
        action = "k8s_describe_pod" if source_tool != "k8s_describe_pod" else "k8s_get_pod_logs"
        reason = (
            "Determine the historical restart cause"
            if action == "k8s_describe_pod"
            else "Inspect recent logs only if the restart was recent or recurring"
        )
        next_action: dict[str, Any] = {
            "action": action,
            "priority": "low",
            "reason": reason,
            "tool_arguments": {
                "namespace": namespace,
                "pod": pod,
            },
        }
        if action == "k8s_get_pod_logs":
            next_action["tool_arguments"]["tail_lines"] = 100
        return [next_action]
    return []


def _containers_without_explicit_resources(
    *,
    containers: list[dict[str, Any]],
    resources: dict[str, Any],
) -> list[str]:
    resource_items = resources.get("containers") if isinstance(resources, dict) else None
    if not isinstance(resource_items, list):
        return []
    known_container_names = {str(c.get("name") or "") for c in containers if isinstance(c, dict)}
    missing: list[str] = []
    for item in resource_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name not in known_container_names:
            continue
        requests = item.get("requests") if isinstance(item.get("requests"), dict) else {}
        limits = item.get("limits") if isinstance(item.get("limits"), dict) else {}
        if not requests and not limits:
            missing.append(name)
    return missing


def _describe_pod_structured(
    pod_raw: dict[str, Any],
    pod_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a structured describe response from raw pod data and filtered events."""
    pod_name = str(pod_raw.get("name") or "")
    namespace = str(pod_raw.get("namespace") or "")
    phase = str(pod_raw.get("phase") or "")

    containers_raw = [c for c in (pod_raw.get("containers") or []) if isinstance(c, dict)]
    containers_out: list[dict[str, Any]] = []
    for c in containers_raw:
        entry: dict[str, Any] = {
            "name": str(c.get("name") or ""),
            "image": _strip_image_digest(str(c.get("image") or "")),
            "ready": bool(c.get("ready")),
            "restart_count": _container_restart_count(c),
        }
        last_restart_at = _format_k8s_timestamp(_container_last_restart_at(c))
        if last_restart_at:
            entry["last_restart_at"] = last_restart_at
        for extra in ("state", "last_state", "started_at"):
            if extra in c:
                entry[extra] = c[extra]
        containers_out.append(entry)

    total_restarts = sum(c["restart_count"] for c in containers_out)
    restart_summary = _restart_window_summary(containers_out)
    all_ready = bool(containers_out) and all(c["ready"] for c in containers_out)

    diagnosis = _diagnose_pod(pod_raw, pod_events)
    observations = _pod_observations(
        healthy=bool(diagnosis["healthy"]),
        total_restarts=total_restarts,
        last_restart_at=restart_summary["last_restart_at"],
        historical_warnings=diagnosis.get("historical_warnings"),
    )
    recommendations = _pod_recommendations(
        diagnosis["recommendations"],
        healthy=bool(diagnosis["healthy"]),
        total_restarts=total_restarts,
    )
    next_actions = _pod_next_actions(
        namespace=namespace,
        pod=pod_name,
        healthy=bool(diagnosis["healthy"]),
        total_restarts=total_restarts,
        source_tool="k8s_get_pod",
    )
    sorted_events = _sort_events_for_display(_deduplicate_events(pod_events))[:20]

    workload = _workload_from_pod_name(pod_name)
    if diagnosis["healthy"]:
        summary = f"Pod {pod_name} is {phase}, all containers ready, {total_restarts} restarts"
        finding_lines: list[str] = ["✓ Pod is healthy"]
    else:
        issue_types = [i["type"] for i in diagnosis["issues"]]
        summary = f"Pod {pod_name} is {phase} — issues: {', '.join(issue_types)}"
        finding_lines = [
            f"⚠ {i['type']}" + (f" (x{i['count']})" if "count" in i else "")
            for i in diagnosis["issues"]
        ]

    return {
        "status": "success",
        "summary": summary,
        "findings": finding_lines,
        "observations": observations,
        "recommendations": recommendations,
        "next_actions": next_actions,
        "data": {
            "pod": {
                "name": pod_name,
                "namespace": namespace,
                "workload": workload,
                "node": str(pod_raw.get("node_name") or ""),
                "age": str(pod_raw.get("age") or ""),
            },
            "status": {
                "phase": phase,
                "ready": all_ready,
                "restart_count": total_restarts,
                **restart_summary,
            },
            "containers": containers_out,
            "events": [
                {
                    "type": e.get("type"),
                    "reason": e.get("reason"),
                    "message": str(e.get("message") or "")[:200],
                    "count": e.get("count", 1),
                    "last_seen": e.get("last_seen") or e.get("lastSeen"),
                }
                for e in sorted_events
            ],
            "diagnosis": diagnosis,
            "observations": observations,
            "next_actions": next_actions,
        },
    }


def _diagnose_pod_from_description(
    status: dict[str, Any],
    containers: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Diagnose from rich k8s.describe_pod data.

    Separates current_issues (pod needs attention now) from historical_warnings
    (problems that occurred during startup/rollout but the pod is now healthy).
    A pod that is Running+Ready with 0 restarts is treated as healthy even if
    probe-failure events exist in its history.
    """
    phase = str(status.get("phase") or "").lower()
    ready = bool(status.get("ready"))
    current_issues: list[dict[str, Any]] = []
    historical_warnings: list[dict[str, Any]] = []
    recommendations: list[str] = []

    total_restarts = sum(
        int(c.get("restart_count") or 0) for c in containers if isinstance(c, dict)
    )

    for c in containers:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        restart_count = int(c.get("restart_count") or 0)

        # Current waiting state — always a live issue
        waiting = (c.get("state") or {}).get("waiting") or {}
        w_reason = str(waiting.get("reason") or "").lower()
        w_message = str(waiting.get("message") or "").lower()
        if "crashloopbackoff" in w_reason:
            current_issues.append(
                {
                    "type": "CrashLoopBackOff",
                    "container": name,
                    "severity": "critical",
                }
            )
            recommendations.append(
                f"Container {name} is crash-looping — run k8s_get_pod_logs to find the crash reason"
            )
        elif "imagepullbackoff" in w_reason or "errimagepull" in w_reason:
            current_issues.append(
                {
                    "type": "ImagePullBackOff",
                    "container": name,
                    "severity": "critical",
                }
            )
            recommendations.append(
                f"Container {name} cannot pull image"
                " — check image name, tag, and registry credentials"
            )
        elif w_reason and "containercreat" not in w_reason:
            current_issues.append(
                {
                    "type": f"ContainerWaiting:{w_reason}",
                    "container": name,
                    "severity": "warning",
                }
            )
            if w_message:
                recommendations.append(f"Container {name} waiting: {w_message[:120]}")

        # Last termination reason (OOMKilled)
        # Historical if pod is now Ready with 0 restarts; current if restarts are ongoing
        last_term = _container_last_termination(c) or {}
        termination_reason = str(last_term.get("reason") or "")
        termination_reason_lower = termination_reason.lower()
        if termination_reason_lower == "oomkilled":
            if restart_count > 0 or not ready or phase != "running":
                current_issues.append(
                    {
                        "type": "OOMKilled",
                        "container": name,
                        "severity": "critical",
                    }
                )
                recommendations.append(
                    f"Container {name} was OOMKilled"
                    " — increase resources.limits.memory or fix memory leak"
                )
            else:
                historical_warnings.append(
                    {
                        "type": "OOMKilled",
                        "container": name,
                        "note": "Pod recovered and is now Ready",
                    }
                )
        elif last_term and restart_count > 0 and phase == "running" and ready:
            exit_code = last_term.get("exit_code") or last_term.get("exitCode")
            finished_at = last_term.get("finished_at") or last_term.get("finishedAt")
            message = (
                f"Container was previously terminated with exit code {exit_code}. "
                "The pod has remained healthy since restart."
            )
            historical_warnings.append(
                {
                    "severity": "info",
                    "type": "PreviousContainerTermination",
                    "container": name,
                    "exit_code": exit_code,
                    "reason": termination_reason,
                    "finished_at": finished_at,
                    "message": message,
                }
            )

    # Pod is currently stable if Running+Ready with no container waiting states
    pod_currently_ok = (
        phase == "running"
        and ready
        and total_restarts == 0
        and not any(i["type"] in {"CrashLoopBackOff", "ImagePullBackOff"} for i in current_issues)
    )

    # Event-based issues — collect per-reason metadata from events
    event_reasons: set[str] = set()
    event_messages: list[str] = []
    probe_events: dict[str, dict[str, Any]] = {}  # probe_type → last event info
    for e in events:
        if not isinstance(e, dict):
            continue
        r = str(e.get("reason") or "").lower()
        if r:
            event_reasons.add(r)
        msg = str(e.get("message") or "").lower()
        event_messages.append(msg)
        if r == "unhealthy":
            for probe in ("readiness", "liveness", "startup"):
                if probe in msg:
                    # Keep the most recent event info for this probe type
                    existing = probe_events.get(probe)
                    if not existing or str(e.get("last_seen") or "") > str(
                        existing.get("last_seen") or ""
                    ):
                        probe_events[probe] = {
                            "last_seen": e.get("last_seen"),
                            "count": int(e.get("count") or 1),
                            "message": str(e.get("message") or "")[:120],
                        }

    if "failedscheduling" in event_reasons:
        if phase == "pending":
            current_issues.append({"type": "FailedScheduling", "severity": "warning"})
            recommendations.append("Check node resources and pod resource requests/taints")
        else:
            historical_warnings.append(
                {
                    "type": "FailedScheduling",
                    "note": "Pod eventually scheduled and is now Running",
                }
            )

    if probe_events:
        probe_type_map = {
            "readiness": "ReadinessProbeFailure",
            "liveness": "LivenessProbeFailure",
            "startup": "StartupProbeFailure",
        }
        for probe, info in probe_events.items():
            issue_type = probe_type_map.get(probe, f"{probe.title()}ProbeFailure")
            if pod_currently_ok:
                # Pod is now healthy — probe failures are rollout/startup noise
                historical_warnings.append(
                    {
                        "type": issue_type,
                        "reason": f"{probe.title()} probe failed during startup/rollout",
                        "last_seen": info.get("last_seen"),
                        "count": info.get("count"),
                    }
                )
            else:
                current_issues.append({"type": issue_type, "severity": "warning"})
                if probe == "readiness":
                    recommendations.append(
                        "Check readiness probe endpoint and application startup time"
                    )
                elif probe == "liveness":
                    recommendations.append("Check liveness probe — container may be restarting")
                elif probe == "startup":
                    recommendations.append(
                        "Startup probe failed — consider increasing initialDelaySeconds"
                    )

    # Phase-level issues
    if phase == "pending" and not any(i["type"] == "FailedScheduling" for i in current_issues):
        if not any("imagepull" in str(i["type"]).lower() for i in current_issues):
            current_issues.append({"type": "Pending", "severity": "warning"})
            recommendations.append(
                "Pod is waiting — check events for scheduling or image pull issues"
            )
    elif phase == "failed":
        current_issues.append({"type": "PodFailed", "severity": "critical"})
        recommendations.append("Pod is in Failed state — check logs for exit reason")
    elif phase == "unknown":
        current_issues.append({"type": "UnknownPhase", "severity": "warning"})
        recommendations.append("Node may be unreachable — check node status")

    # Containers not ready with no specific cause yet detected
    not_ready = [c.get("name") for c in containers if isinstance(c, dict) and not c.get("ready")]
    if not_ready and not current_issues and phase == "running":
        current_issues.append(
            {
                "type": "ContainersNotReady",
                "containers": not_ready,
                "severity": "warning",
            }
        )
        recommendations.append(
            "Containers are not ready — check readiness probe and application startup"
        )

    healthy = not current_issues and phase == "running" and not not_ready
    return {
        "healthy": healthy,
        "current_issues": current_issues,
        "historical_warnings": historical_warnings,
        # keep "issues" as alias so existing callers don't break
        "issues": current_issues,
        "recommendations": list(dict.fromkeys(recommendations)),
    }


def _build_describe_response(
    desc: dict[str, Any],
    *,
    include_details: bool = False,
) -> dict[str, Any]:
    """Build the MCP k8s_describe_pod response from a k8s.describe_pod agent payload."""
    meta = desc.get("metadata") or {}
    status = desc.get("status") or {}
    containers = [c for c in (desc.get("containers") or []) if isinstance(c, dict)]
    resources = desc.get("resources") or {}
    probes = desc.get("probes") or []
    events = [e for e in (desc.get("events") or []) if isinstance(e, dict)]

    pod_name = str(meta.get("name") or "")
    phase = str(status.get("phase") or "")
    total_restarts = sum(int(c.get("restart_count") or 0) for c in containers)
    restart_summary = _restart_window_summary(containers)

    diagnosis = _diagnose_pod_from_description(status, containers, events)
    workload = _workload_from_pod_name(pod_name)

    historical = diagnosis.get("historical_warnings") or []
    observations = _pod_observations(
        healthy=bool(diagnosis["healthy"]),
        total_restarts=total_restarts,
        last_restart_at=restart_summary["last_restart_at"],
        historical_warnings=historical,
    )
    recommendations = _pod_recommendations(
        diagnosis["recommendations"],
        healthy=bool(diagnosis["healthy"]),
        total_restarts=total_restarts,
    )
    next_actions = _pod_next_actions(
        namespace=str(meta.get("namespace") or ""),
        pod=pod_name,
        healthy=bool(diagnosis["healthy"]),
        total_restarts=total_restarts,
        source_tool="k8s_describe_pod",
    )

    if diagnosis["healthy"]:
        if historical:
            hw_types = list(dict.fromkeys(w["type"] for w in historical))
            summary = (
                f"Pod {pod_name} is currently healthy"
                f"; historical warnings found during startup/rollout: {', '.join(hw_types)}"
            )
            findings: list[str] = [
                "✓ Pod is Running and Ready",
                f"✓ {total_restarts} restarts",
            ] + [
                "~ Historical: "
                + w["type"]
                + (f" ({w.get('reason', '')})" if w.get("reason") else "")
                for w in historical
            ]
        else:
            summary = f"Pod {pod_name} is {phase}, all containers ready, {total_restarts} restarts"
            findings = ["✓ Pod is healthy"]
    else:
        issue_types = [i["type"] for i in diagnosis["current_issues"]]
        summary = f"Pod {pod_name} is {phase} — issues: {', '.join(issue_types)}"
        findings = [
            f"⚠ {i['type']}" + (f" (container: {i['container']})" if "container" in i else "")
            for i in diagnosis["current_issues"]
        ]

    containers_missing_resources = _containers_without_explicit_resources(
        containers=containers,
        resources=resources if isinstance(resources, dict) else {},
    )
    for container_name in containers_missing_resources:
        findings.append(f"⚠ {container_name} has no explicit CPU or memory requests/limits")

    container_summaries = []
    for c in containers:
        container_summary = {
            "name": str(c.get("name") or ""),
            "ready": bool(c.get("ready")),
            "restart_count": int(c.get("restart_count") or 0),
            "image": _strip_image_digest(str(c.get("image") or "")),
        }
        last_restart_at = _format_k8s_timestamp(_container_last_restart_at(c))
        if last_restart_at:
            container_summary["last_restart_at"] = last_restart_at
        container_summaries.append(container_summary)
    pod_summary = {
        "name": pod_name,
        "namespace": str(meta.get("namespace") or ""),
        "workload": workload,
        "owner": str(meta.get("owner") or ""),
        "age": str(meta.get("age") or ""),
    }
    if include_details:
        pod_summary["node"] = str(meta.get("node") or "")
        pod_summary["pod_ip"] = str(meta.get("pod_ip") or "")

    data: dict[str, Any] = {
        "pod": pod_summary,
        "status": {
            "phase": phase,
            "ready": bool(status.get("ready")),
            "conditions": status.get("conditions") or [],
            "restart_count": total_restarts,
            **restart_summary,
            "reason": str(status.get("reason") or ""),
            "message": str(status.get("message") or ""),
        },
        "containers": container_summaries,
        "events": [
            {
                "type": e.get("type"),
                "reason": e.get("reason"),
                "message": str(e.get("message") or "")[:200],
                "count": e.get("count", 1),
                "last_seen": e.get("last_seen"),
            }
            for e in events[:20]
        ],
        "diagnosis": diagnosis,
        "observations": observations,
        "next_actions": next_actions,
    }
    if include_details:
        data["resources"] = resources
        data["probes"] = probes

    return {
        "status": "success",
        "summary": summary,
        "findings": findings,
        "observations": observations,
        "recommendations": recommendations,
        "next_actions": next_actions,
        "data": data,
    }


def _unhealthy_pod_entry(pod: dict[str, Any]) -> dict[str, Any]:
    """Build a rich unhealthy pod summary with likely cause and next action."""
    phase = str(pod.get("phase") or "")
    containers = [c for c in (pod.get("containers") or []) if isinstance(c, dict)]
    not_ready = [c for c in containers if not c.get("ready")]
    total_restarts = sum(_container_restart_count(c) for c in containers)

    if phase.lower() == "pending":
        reason = "Pending"
        likely_cause = "Pod is waiting to be scheduled or pulling an image"
        recommendation = (
            "Run k8s_describe_pod then check events for FailedScheduling or ImagePullBackOff"
        )
    elif phase.lower() == "failed":
        reason = "Failed"
        likely_cause = "Container exited with a non-zero exit code"
        recommendation = "Run k8s_debug_pod to find the crash reason in logs"
    elif phase.lower() == "unknown":
        reason = "Unknown"
        likely_cause = "Node is unreachable or the agent cannot contact the Kubernetes API"
        recommendation = "Check node status and cluster connectivity"
    elif total_restarts > 5:
        reason = f"CrashLoopBackOff (restarts: {total_restarts})"
        likely_cause = "Container is crashing repeatedly"
        recommendation = "Run k8s_debug_pod to investigate logs and crash cause"
    elif not_ready:
        not_ready_names = [str(c.get("name") or "") for c in not_ready]
        reason = f"Containers not ready: {', '.join(not_ready_names)}"
        likely_cause = "Readiness probe is failing or application is still starting up"
        recommendation = "Run k8s_describe_pod to see events and k8s_get_pod_logs for errors"
    else:
        reason = f"Phase: {phase}"
        likely_cause = "Unexpected pod state"
        recommendation = (
            "No immediate action required if the pod is Running and Ready. "
            "Check the previous container termination reason if restarts are recent or recurring."
        )

    return {
        "name": str(pod.get("name") or ""),
        "namespace": str(pod.get("namespace") or ""),
        "phase": phase,
        "reason": reason,
        "restart_count": total_restarts,
        "age": str(pod.get("age") or ""),
        "likely_cause": likely_cause,
        "recommendation": recommendation,
    }


def _cluster_health_assessment(overview: dict[str, Any]) -> dict[str, Any]:
    """Derive cluster health, findings, and recommendations from an overview payload."""
    findings: list[str] = []
    recommendations: list[str] = []

    unhealthy_count = int(overview.get("pods_unhealthy") or 0)
    total_pods = int(overview.get("pods_total") or 0)
    ws = overview.get("warning_event_summary") or {}
    active_warnings = int(ws.get("active_warning_events") or 0)
    top_restarts = overview.get("top_restarts") or []

    if unhealthy_count == 0:
        findings.append("✓ No unhealthy pods")
    else:
        findings.append(f"⚠ {unhealthy_count} unhealthy pod{'s' if unhealthy_count != 1 else ''}")
        for pod in (overview.get("unhealthy_pods") or [])[:3]:
            findings.append(f"  - {pod.get('pod')}: {pod.get('phase')}")
        recommendations.append(
            f"Investigate {unhealthy_count} unhealthy pod(s) with k8s_show_unhealthy_pods"
        )

    if active_warnings == 0:
        findings.append("✓ No active warning events")
    else:
        findings.append(
            f"⚠ {active_warnings} active warning event{'s' if active_warnings != 1 else ''}"
        )
        recommendations.append("Review active warning events with k8s_list_events")

    high_restart = [p for p in top_restarts if int(p.get("restarts") or 0) > 5]
    if high_restart:
        for p in high_restart[:3]:
            findings.append(f"⚠ {p.get('pod')} has {p.get('restarts')} restarts")
        recommendations.append("Investigate high-restart pods with k8s_debug_pod")
    elif not high_restart and unhealthy_count == 0:
        findings.append("✓ No high-restart pods")

    # Three-tier classification:
    # Degraded  — unhealthy pods, high-restart pods, or crashed workloads
    # Warning   — all pods healthy but warning events are present
    # Healthy   — all pods healthy, no warnings
    if unhealthy_count > 0 or high_restart:
        cluster_health = "Degraded"
        summary = (
            f"{unhealthy_count}/{total_pods} pods unhealthy, "
            f"{active_warnings} active warning event{'s' if active_warnings != 1 else ''}"
        )
    elif active_warnings > 0:
        cluster_health = "Warning"
        summary = (
            f"All {total_pods} pod{'s' if total_pods != 1 else ''} healthy"
            f", but {active_warnings} active warning event{'s' if active_warnings != 1 else ''}"
            " present. Review events to confirm they are not current failures."
        )
        recommendations.append("Review active warning events with k8s_list_events")
    else:
        cluster_health = "Healthy"
        summary = f"All {total_pods} pods running normally"

    return {
        "cluster_health": cluster_health,
        "summary": summary,
        "findings": findings,
        "recommendations": list(dict.fromkeys(recommendations)),
    }


def _select_workload_pod(pods: list[Any], workload: str) -> str | None:
    matched = _filter_workload_pods(pods, [], workload)
    return str(matched[0]["name"]) if matched and matched[0].get("name") else None


def _select_workload_pod_from_deployments(
    pods: list[Any],
    deployments: list[Any],
    workload: str,
) -> str | None:
    matched = _filter_workload_pods(pods, deployments, workload)
    return str(matched[0]["name"]) if matched and matched[0].get("name") else None


def _log_lines_from_payload(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    for key in ("logs", "log", "text", "output"):
        value = data.get(key)
        if isinstance(value, str):
            return value.splitlines()
        if isinstance(value, list):
            return [str(item) for item in value]
    return []


def _redact_sensitive_text(value: str) -> str:
    redacted = re.sub(r"(redis://)([^:@\s]+:)?([^@\s]+)@", r"\1***@", value)
    redacted = re.sub(
        r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)=([^\s,;]+)",
        r"\1=***",
        redacted,
    )
    return redacted


def _compact_log_payload(
    payload: dict[str, Any],
    *,
    level: str | None,
    contains: str | None,
    exclude: str | None,
    compact: bool,
) -> dict[str, Any]:
    if not compact:
        return payload

    lines = _log_lines_from_payload(payload)
    if not lines:
        return payload

    include_pattern = contains.lower().strip() if contains else ""
    exclude_pattern = exclude.lower().strip() if exclude else ""
    level_pattern = level.lower().strip() if level else ""
    noisy_patterns = (
        "debug",
        "httpcore.",
        "httpx",
        "mcp.server.lowlevel.server",
        "mcp.server.streamable_http",
        "mcp.server.streamable_http_manager",
        "sse_starlette.sse",
        "raw response",
    )
    important_patterns = (
        "error",
        "warning",
        "traceback",
        "exception",
        "timeout",
        "failed",
        " 4",
        " 5",
    )

    selected: list[str] = []
    skipped_debug = 0
    for line in lines:
        redacted_line = _redact_sensitive_text(line)
        lowered = redacted_line.lower()
        if include_pattern and include_pattern not in lowered:
            continue
        if exclude_pattern and exclude_pattern in lowered:
            continue
        if level_pattern and level_pattern not in lowered:
            continue
        if any(pattern in lowered for pattern in noisy_patterns) and not any(
            pattern in lowered for pattern in important_patterns
        ):
            skipped_debug += 1
            continue
        selected.append(redacted_line)

    highlighted = [
        line for line in selected if any(pattern in line.lower() for pattern in important_patterns)
    ]
    compact_data = dict(payload.get("data") if isinstance(payload.get("data"), dict) else {})
    compact_data.pop("logs", None)
    compact_data.pop("log", None)
    compact_data.pop("text", None)
    compact_data.pop("output", None)
    compact_data.update(
        {
            "lines": selected[-120:],
            "highlighted": highlighted[-40:],
            "line_count": len(lines),
            "returned_line_count": min(len(selected), 120),
            "skipped_debug_lines": skipped_debug,
            "compact": True,
        }
    )
    truncated = len(selected) > 120
    if truncated:
        compact_data["truncated"] = True
    return {
        **payload,
        "truncated": bool(payload.get("truncated")) or truncated,
        "data": compact_data,
    }


_INTERNAL_LOGGER_PATTERNS = (
    "httpcore.",
    "httpx",
    "platform_api.domain.services.agent_registry_service",
    "mcp.server.lowlevel.server",
    "mcp.server.streamable_http",
    "sse_starlette.sse",
)


def _redact_platform_internal_log_line(line: str) -> str:
    redacted = _redact_sensitive_text(line)
    redacted = re.sub(r"\b[\w.-]+\.svc\.cluster\.local\b", "<internal-service>", redacted)
    redacted = re.sub(
        r"\b(workspace_id|agent_id|cluster_id|request_id|command_id)=['\"]?[\w:.-]+",
        r"\1=<redacted>",
        redacted,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(
        r'"(workspace_id|agent_id|cluster_id|request_id|command_id)"\s*:\s*"[^"]+"',
        r'"\1":"<redacted>"',
        redacted,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(r"/internal/[A-Za-z0-9_./-]+", "/internal/<redacted>", redacted)
    return redacted


def _log_category(line: str, *, exclude_loggers: list[str] | None = None) -> str:
    lowered = line.lower()
    extra_patterns = tuple(pattern.lower().rstrip("*") for pattern in (exclude_loggers or []))
    if any(pattern in lowered for pattern in (*_INTERNAL_LOGGER_PATTERNS, *extra_patterns)):
        return "internal_debug"
    if any(token in lowered for token in ("status_code", "method=", "path=", "http request")):
        return "http_access"
    dependency_tokens = ("redis", "postgres", "database", "upstream", "dependency")
    if any(token in lowered for token in dependency_tokens):
        return "dependency"
    return "application"


def _log_pattern(line: str) -> str:
    text = _redact_platform_internal_log_line(line)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        event = parsed.get("event") or parsed.get("message") or parsed.get("msg")
        if event:
            return str(event)[:120]
    match = re.search(r"\bevent=['\"]([^'\"]+)['\"]", text)
    if match:
        return match.group(1)[:120]
    simplified = re.sub(r"\b\d+(?:\.\d+)?\b", "<num>", text)
    simplified = re.sub(r"\s+", " ", simplified).strip()
    return simplified[:120] or "unclassified log line"


def _extract_latency_ms(line: str) -> float | None:
    patterns = (
        r"\b(?:duration|duration_ms|latency|latency_ms|elapsed_ms)=['\"]?([0-9]+(?:\.[0-9]+)?)",
        r'"(?:duration_ms|latency_ms|elapsed_ms)"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        r"\bin\s+([0-9]+(?:\.[0-9]+)?)\s*ms\b",
    )
    for pattern in patterns:
        match = re.search(pattern, line, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _analyze_workload_logs(
    logs_data: dict[str, Any] | None,
    *,
    exclude_loggers: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(logs_data, dict):
        return {
            "lines_scanned": 0,
            "errors": 0,
            "warnings": 0,
            "top_patterns": [],
            "latency": {"p50_ms": None, "max_ms": None},
            "notable_lines": [],
            "log_categories": {
                "application": 0,
                "http_access": 0,
                "dependency": 0,
                "internal_debug": 0,
            },
        }

    raw_lines = [str(line) for line in (logs_data.get("lines") or []) if isinstance(line, str)]
    category_counts = {
        "application": 0,
        "http_access": 0,
        "dependency": 0,
        "internal_debug": int(logs_data.get("skipped_debug_lines") or 0),
    }
    pattern_counts: dict[str, int] = {}
    notable_lines: list[str] = []
    latency_values: list[float] = []
    error_count = 0
    warning_count = 0

    for line in raw_lines:
        redacted_line = _redact_platform_internal_log_line(line)
        lowered = redacted_line.lower()
        category = _log_category(redacted_line, exclude_loggers=exclude_loggers)
        category_counts[category] = category_counts.get(category, 0) + 1

        if any(token in lowered for token in ("error", "exception", "traceback", "fatal", "panic")):
            error_count += 1
            notable_lines.append(redacted_line)
        elif any(token in lowered for token in ("warn", "warning", "failed", "timeout")):
            warning_count += 1
            notable_lines.append(redacted_line)

        latency_ms = _extract_latency_ms(redacted_line)
        if latency_ms is not None:
            latency_values.append(latency_ms)

        if category != "internal_debug":
            pattern = _log_pattern(redacted_line)
            pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1

    latency_values.sort()
    p50_ms: float | None = None
    if latency_values:
        p50_ms = latency_values[len(latency_values) // 2]

    top_patterns = [
        {"event": event, "count": count}
        for event, count in sorted(pattern_counts.items(), key=lambda item: item[1], reverse=True)[
            :5
        ]
    ]
    return {
        "lines_scanned": int(logs_data.get("line_count") or len(raw_lines)),
        "errors": error_count,
        "warnings": warning_count,
        "top_patterns": top_patterns,
        "latency": {
            "p50_ms": p50_ms,
            "max_ms": max(latency_values) if latency_values else None,
        },
        "notable_lines": notable_lines[-10:],
        "log_categories": category_counts,
    }
