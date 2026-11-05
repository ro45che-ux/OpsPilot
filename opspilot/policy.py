"""Deterministic policy gate between what the agent proposes and what may run.

The LLM never decides whether an action is safe. This module does, from rules that can be read,
unit-tested and audited:

  * structure: each action type has required parameters; unknown services/keys are denied
  * provenance: rollbacks go only to versions the service has actually run; config reverts only to
    values the config has actually had, and only for allow-listed keys
  * approval: every mutation needs an explicit human approval, no exceptions; only `escalate` is free
  * budget: at most MAX_MUTATIONS mutating actions per incident
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from opspilot.schemas import Action

SERVICES = {"api", "worker"}
CONFIG_ALLOWLIST = {"db_pool_size", "db_pool_timeout_s", "max_attempts"}
MAX_MUTATIONS = 2
MUTATING = {"rollback_deploy", "restart_service", "revert_config"}


class Risk(BaseModel):
    level: Literal["low", "medium", "high"]
    blast_radius: str
    reversible: bool
    notes: str


class Decision(BaseModel):
    verdict: Literal["allow", "needs_approval", "deny"]
    reasons: list[str]
    risk: Risk | None = None
    plan: str = ""


def _ver(v: str) -> tuple:
    return tuple(int(p) for p in v.split(".") if p.isdigit())


def risk_of(a: Action) -> Risk:
    user_facing = a.service == "api"
    if a.type == "restart_service":
        return Risk(level="high" if user_facing else "low",
                    blast_radius="user-facing API; in-flight requests are dropped" if user_facing
                    else "background worker; queued jobs wait during restart",
                    reversible=True, notes="in-memory state is lost; a restart masks leaks rather than fixing them")
    if a.type == "rollback_deploy":
        return Risk(level="medium" if user_facing else "low",
                    blast_radius="all traffic to " + a.service, reversible=True,
                    notes="reverts every change in the releases being rolled back, including unrelated ones")
    if a.type == "revert_config":
        flag = (a.key or "").startswith("flags.")
        return Risk(level="low" if flag else "medium", blast_radius=f"{a.service} behaviour for key {a.key}",
                    reversible=True, notes="restores a value the service has previously run with")
    return Risk(level="low", blast_radius="none", reversible=True, notes="hands the incident to a human")


def evaluate(action: Action, config_of, mutations_so_far: int = 0) -> Decision:
    """`config_of(service)` returns the env's current config view (version, known_versions, history)."""
    a = action
    deny = lambda *why: Decision(verdict="deny", reasons=list(why))  # noqa: E731
    if a.type == "escalate":
        if not a.reason:
            return deny("escalate needs a reason")
        return Decision(verdict="allow", reasons=["escalation changes nothing"], risk=risk_of(a),
                        plan=f"Page a human: {a.reason}")
    if a.type not in MUTATING:
        return deny(f"unknown action type {a.type}")
    if a.service not in SERVICES:
        return deny(f"unknown service {a.service!r}")
    if mutations_so_far >= MAX_MUTATIONS:
        return deny(f"mutation budget exhausted ({MAX_MUTATIONS} per incident)")
    cfg = config_of(a.service)

    if a.type == "rollback_deploy":
        if not a.to_version:
            return deny("rollback_deploy needs to_version")
        if a.to_version == cfg["version"]:
            return deny(f"{a.service} is already on {a.to_version}")
        if a.to_version not in cfg["known_versions"]:
            return deny(f"{a.to_version} was never deployed to {a.service}; known: {cfg['known_versions']}")
        if _ver(a.to_version) > _ver(cfg["version"]):
            return deny("to_version is newer than the running version; that is a roll-forward, not a rollback")
        plan = f"Roll {a.service} back {cfg['version']} -> {a.to_version}"
    elif a.type == "revert_config":
        if a.key is None or a.value is None:
            return deny("revert_config needs key and value")
        if a.key not in CONFIG_ALLOWLIST and not a.key.startswith("flags."):
            return deny(f"config key {a.key!r} is not revertable by automation")
        hist = cfg["config_history"].get(a.key, [])
        known = {repr(h["old"]) for h in hist} | {repr(h["new"]) for h in hist}
        if repr(a.value) not in known:
            return deny(f"{a.value!r} is not a value {a.key} has had; history: {[(h['old'], h['new']) for h in hist]}")
        if cfg["config"].get(a.key) == a.value:
            return deny(f"{a.key} is already {a.value!r}")
        plan = f"Set {a.service} {a.key}: {cfg['config'].get(a.key)!r} -> {a.value!r}"
    else:  # restart_service
        plan = f"Restart {a.service} (running {cfg['version']})"

    return Decision(verdict="needs_approval", reasons=["every mutation requires human approval"],
                    risk=risk_of(a), plan=plan)
