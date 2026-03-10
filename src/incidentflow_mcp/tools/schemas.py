"""
Pydantic schemas for MCP tool inputs and outputs.

All models use strict typing and validation so that bad inputs are caught
before reaching tool logic.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class AlertStatus(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"
    PENDING = "pending"


# ---------------------------------------------------------------------------
# incident_summary schemas
# ---------------------------------------------------------------------------


class IncidentSummaryInput(BaseModel):
    """Input for the incident_summary tool."""

    incident_id: Annotated[str, Field(min_length=1, max_length=128, description="Unique incident identifier")]
    include_timeline: bool = Field(default=True, description="Include event timeline in summary")
    include_affected_services: bool = Field(default=True, description="Include impacted service list")


class TimelineEvent(BaseModel):
    timestamp: datetime
    description: str
    actor: str | None = None


class IncidentSummaryOutput(BaseModel):
    """Output from the incident_summary tool."""

    incident_id: str
    title: str
    severity: Severity
    status: str
    summary: str
    affected_services: list[str]
    timeline: list[TimelineEvent]
    recommendations: list[str]


# ---------------------------------------------------------------------------
# correlate_alerts schemas
# ---------------------------------------------------------------------------


class Alert(BaseModel):
    """A single alert to be correlated."""

    alert_id: Annotated[str, Field(min_length=1, max_length=128)]
    name: str
    service: str
    severity: Severity
    status: AlertStatus
    fired_at: datetime
    labels: dict[str, str] = Field(default_factory=dict)


class CorrelateAlertsInput(BaseModel):
    """Input for the correlate_alerts tool."""

    alerts: Annotated[list[Alert], Field(min_length=1, max_length=500, description="List of alerts to correlate")]
    window_minutes: int = Field(
        default=60,
        ge=1,
        le=1440,
        description="Correlation time window in minutes",
    )
    min_cluster_size: int = Field(
        default=2,
        ge=1,
        description="Minimum alerts in a cluster to report",
    )

    @model_validator(mode="after")
    def at_least_one_firing(self) -> "CorrelateAlertsInput":
        firing = [a for a in self.alerts if a.status == AlertStatus.FIRING]
        if not firing:
            raise ValueError("At least one alert must have status='firing'")
        return self


class AlertCluster(BaseModel):
    """A group of correlated alerts."""

    cluster_id: str
    alert_ids: list[str]
    services: list[str]
    dominant_severity: Severity
    likely_root_cause: str
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]


class CorrelateAlertsOutput(BaseModel):
    """Output from the correlate_alerts tool."""

    total_alerts: int
    clusters: list[AlertCluster]
    uncorrelated_alert_ids: list[str]
    analysis_window_minutes: int
    summary: str
