"""What an agent must hand back: ranked hypotheses with evidence, a typed action proposal, and
the recovery criteria it will be held to. `validate_findings` is what makes 'evidence-backed' real:
a claim that cites nothing, or cites an evidence ID that does not exist or returned an error, is
rejected before a human ever sees it."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from opspilot.schemas import Action, Criterion

Category = Literal["bad_deploy", "config_change", "flag_change", "downstream_dependency", "poison_message", "unknown"]
CHANGE_CATEGORIES = {"bad_deploy", "config_change", "flag_change"}


class Hypothesis(BaseModel):
    category: Category
    service: str = Field(description="service where the fault originates: api, worker, or payments")
    summary: str
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str]
    change_id: str | None = Field(default=None, description="the offending change; required for deploy/config/flag causes")


class Proposal(BaseModel):
    action: Action
    rationale: str


class Findings(BaseModel):
    summary: str
    hypotheses: list[Hypothesis]
    proposals: list[Proposal]
    success_criteria: list[Criterion]


def validate_findings(f: Findings, env) -> list[str]:
    errs: list[str] = []
    if not f.hypotheses:
        errs.append("at least one hypothesis is required")
    change_ids = {c.id for c in env.tel.changes}
    for i, h in enumerate(f.hypotheses):
        tag = f"hypotheses[{i}]"
        if not h.evidence_ids:
            errs.append(f"{tag}: cite at least one evidence_id")
        for eid in h.evidence_ids:
            ev = env.evidence.get(eid)
            if ev is None:
                errs.append(f"{tag}: evidence_id {eid} does not exist (only IDs returned by tools are valid)")
            elif isinstance(ev.result, dict) and "error" in ev.result:
                errs.append(f"{tag}: evidence {eid} is a failed tool call")
        if h.category in CHANGE_CATEGORIES and not h.change_id:
            errs.append(f"{tag}: category {h.category} must name the offending change_id")
        if h.change_id and h.change_id not in change_ids:
            errs.append(f"{tag}: change_id {h.change_id} does not exist")
    if len(f.proposals) > 2:
        errs.append("propose at most 2 actions")
    if not f.proposals:
        errs.append("propose an action; use type 'escalate' if no safe automated fix exists")
    for p in f.proposals:
        a = p.action
        need = {"rollback_deploy": ("service", "to_version"), "restart_service": ("service",),
                "revert_config": ("service", "key", "value"), "escalate": ("reason",)}[a.type]
        for n in need:
            if getattr(a, n) is None:
                errs.append(f"action {a.type} is missing '{n}'")
    return errs


def submit_schema() -> dict:
    """Hand-written (not pydantic's) so it stays small and unambiguous for the model."""
    action = {
        "type": "object", "additionalProperties": False, "required": ["type"],
        "properties": {
            "type": {"type": "string", "enum": ["rollback_deploy", "restart_service", "revert_config", "escalate"]},
            "service": {"type": "string", "enum": ["api", "worker"]},
            "to_version": {"type": "string", "description": "rollback_deploy: a version from get_config.known_versions"},
            "key": {"type": "string", "description": "revert_config: config key"},
            "value": {"description": "revert_config: a previous value from get_config.config_history"},
            "reason": {"type": "string", "description": "escalate: why a human must act"},
        },
    }
    return {
        "type": "object", "additionalProperties": False, "required": ["summary", "hypotheses", "proposals", "success_criteria"],
        "properties": {
            "summary": {"type": "string"},
            "hypotheses": {"type": "array", "description": "ranked, most likely first; 1-3 items", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["category", "service", "summary", "confidence", "evidence_ids"],
                "properties": {
                    "category": {"type": "string", "enum": [c for c in Category.__args__]},
                    "service": {"type": "string", "enum": ["api", "worker", "payments"]},
                    "summary": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "change_id": {"type": "string"}}}},
            "proposals": {"type": "array", "description": "1-2 actions, best first", "items": {
                "type": "object", "additionalProperties": False, "required": ["action", "rationale"],
                "properties": {"action": action, "rationale": {"type": "string"}}}},
            "success_criteria": {"type": "array", "description": "how recovery will be judged after the action; "
                                 "thresholds must separate the healthy baseline from the degraded state", "items": {
                "type": "object", "additionalProperties": False, "required": ["metric", "op", "value"],
                "properties": {"metric": {"type": "string", "enum": ["api.error_rate", "api.p99_latency_s", "api.memory_mb",
                                                                     "worker.queue_depth"]},
                               "op": {"type": "string", "enum": ["<", ">"]}, "value": {"type": "number"}}}},
        },
    }
