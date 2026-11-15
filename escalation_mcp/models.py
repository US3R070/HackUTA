"""Pydantic models for the escalation packet contract with the cloud agent."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["LOW", "HIGH"]
DecisionKind = Literal["approve", "reject", "stop", "instruction", "none"]
ActionStatus = Literal["started", "done", "failed"]
Outcome = Literal["resolved", "aborted", "handed_off"]


class MediaInfo(BaseModel):
    transport: str = "livekit"
    livekit_url: str = ""
    room: str = ""
    screen_participant: str = "local-screen"
    preview_url: str = ""


class ChannelsInfo(BaseModel):
    events_topic: str = "agent_events"
    decisions_topic: str = "human_decisions"


class EscalationPacket(BaseModel):
    schema_version: str = "1"
    incident_id: str
    created_at: str
    source: str = "grok-local"
    severity: Severity
    failure_type: str
    title: str
    summary: str
    proposed_fix: str | None = None
    run_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    media: MediaInfo = Field(default_factory=MediaInfo)
    channels: ChannelsInfo = Field(default_factory=ChannelsInfo)
    contacts: list[str] = Field(default_factory=list)


class ActionEvent(BaseModel):
    type: Literal["action"] = "action"
    incident_id: str
    seq: int
    ts: str
    action: str
    detail: str = ""
    status: ActionStatus = "started"


class HumanDecision(BaseModel):
    type: Literal["decision"] = "decision"
    incident_id: str
    ts: str
    decision: Literal["approve", "reject", "stop", "instruction"]
    text: str | None = None
    from_: str | None = Field(default=None, alias="from")

    model_config = {"populate_by_name": True}


class FinalReport(BaseModel):
    outcome: Outcome
    summary: str
    ended_at: str
    actions: list[dict[str, Any]] = Field(default_factory=list)
