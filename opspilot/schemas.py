"""Typed contracts shared by the scenario library, recorder and (later) the agent and eval harness."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Service = Literal["api", "worker"]


# ---- telemetry as recorded in a snapshot -----------------------------------------------------
class Change(BaseModel):
    id: str
    ts: float
    service: str
    type: Literal["deploy", "config", "flag"]
    summary: str
    author: str
    diff: str = ""
    from_version: str | None = None
    to_version: str | None = None
    rollback: bool = False
    key: str | None = None
    old: object | None = None
    new: object | None = None


class Alert(BaseModel):
    name: str
    severity: Literal["page", "ticket"]
    service: Service
    summary: str
    fired_at: float = 0.0


# ---- scenario definition ---------------------------------------------------------------------
class Timeline(BaseModel):
    baseline_s: float = 20
    alert_delay_s: float = 15  # alert fires this long after the fault starts
    fault_s: float = 45        # nofix branch: how long the fault runs
    fix_at_s: float = 25       # fix branch: when the correct action is applied
    recovery_s: float = 25     # fix branch: how long to record after the fix

    def scaled(self, factor: float) -> "Timeline":
        return Timeline(**{k: round(v * factor, 2) for k, v in self.model_dump().items()})


class Injection(BaseModel):
    """One step that creates the incident. `at` is seconds relative to fault start (may be negative)."""
    op: Literal["deploy", "set_config", "fault", "poison_job"]
    at: float = 0
    id: str | None = None             # change id (deploy / set_config)
    service: Service | None = None
    version: str | None = None        # deploy
    key: str | None = None            # set_config key, or fault name
    value: object | None = None
    summary: str | None = None
    author: str | None = None


class Action(BaseModel):
    type: Literal["rollback_deploy", "restart_service", "revert_config", "escalate"]
    service: Service | None = None
    to_version: str | None = None  # rollback_deploy
    key: str | None = None         # revert_config
    value: object | None = None    # revert_config
    reason: str | None = None      # escalate


class Matcher(BaseModel):
    """A fact in the telemetry a good RCA should cite. Resolved against a snapshot by `validate`."""
    source: Literal["change", "log", "metric"]
    ref: str | None = None            # change: change id
    service: Service | None = None
    msg_contains: str | None = None   # log: substring of msg or error
    field: str | None = None          # log: numeric field that must be >= min
    min: float | None = None
    name: str | None = None           # metric: metric name


class Criterion(BaseModel):
    """Recovery criterion on a derived metric, e.g. api.error_rate < 0.02."""
    metric: Literal["api.error_rate", "api.p99_latency_s", "api.memory_mb", "worker.queue_depth"]
    op: Literal["<", ">"] = "<"
    value: float


class RootCause(BaseModel):
    category: Literal["bad_deploy", "config_change", "flag_change", "downstream_dependency", "poison_message"]
    service: Service | Literal["payments"]
    change_id: str | None = None  # the change that caused it, if any
    summary: str


class GroundTruth(BaseModel):
    root_cause: RootCause
    required_evidence: list[Matcher]
    red_herring_changes: list[str] = Field(default_factory=list)
    correct_actions: list[Action]
    acceptable_actions: list[Action] = Field(default_factory=list)  # mitigations that help but do not fix
    wrong_actions: list[Action] = Field(default_factory=list)        # actions an agent must not propose
    success_criteria: list[Criterion]


class Scenario(BaseModel):
    id: str
    title: str
    difficulty: Literal["easy", "medium", "hard"]
    description: str
    timeline: Timeline = Timeline()
    alert: Alert
    inject: list[Injection]
    ground_truth: GroundTruth

    @property
    def has_fix_branch(self) -> bool:
        return all(a.type != "escalate" for a in self.ground_truth.correct_actions)
