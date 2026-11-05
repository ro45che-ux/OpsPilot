"""Load scenario definitions from scenarios/*.yaml and check they are internally consistent."""
from __future__ import annotations

from pathlib import Path

import yaml

from opspilot.schemas import Scenario
from testbed.releases.catalog import RELEASES

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "scenarios"


def load_all(directory: Path = SCENARIO_DIR) -> dict[str, Scenario]:
    out: dict[str, Scenario] = {}
    for path in sorted(directory.glob("*.yaml")):
        sc = Scenario.model_validate(yaml.safe_load(path.read_text()))
        if sc.id in out:
            raise ValueError(f"duplicate scenario id {sc.id}")
        if sc.id != path.stem:
            raise ValueError(f"{path.name}: id '{sc.id}' must match filename")
        out[sc.id] = sc
    return out


def load(scenario_id: str) -> Scenario:
    return load_all()[scenario_id]


def check_consistency(sc: Scenario) -> list[str]:
    """Static checks that don't need a recording. Returns a list of problems."""
    problems: list[str] = []
    gt = sc.ground_truth
    change_ids = {i.id for i in sc.inject if i.id}
    for i in sc.inject:
        if i.op == "deploy" and (i.service, i.version) not in RELEASES:
            problems.append(f"inject: unknown release {i.service} {i.version}")
        if i.op == "deploy" and not i.id:
            problems.append(f"inject: deploy of {i.version} needs an explicit id")
        if i.op == "set_config" and not i.id:
            problems.append(f"inject: set_config {i.key} needs an explicit id")
        if i.at < -sc.timeline.baseline_s:
            problems.append(f"inject: at={i.at} falls before the recording starts")
    if gt.root_cause.change_id and gt.root_cause.change_id not in change_ids:
        problems.append(f"root_cause.change_id {gt.root_cause.change_id} is not injected")
    for cid in gt.red_herring_changes:
        if cid not in change_ids:
            problems.append(f"red herring {cid} is not injected")
    for m in gt.required_evidence:
        if m.source == "change" and m.ref not in change_ids:
            problems.append(f"required_evidence references unknown change {m.ref}")
    for a in gt.correct_actions + gt.acceptable_actions + gt.wrong_actions:
        if a.type == "rollback_deploy" and (a.service, a.to_version) not in RELEASES:
            problems.append(f"action: unknown release {a.service} {a.to_version}")
    if sc.timeline.fix_at_s <= sc.timeline.alert_delay_s:
        problems.append("fix_at_s must come after the alert fires")
    return problems
