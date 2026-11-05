"""Apply scenario injections and remediation actions to a running testbed.

`execute_action` is the testbed side of the future approval-gated executor: the agent will
propose the same typed Action objects and the executor will call this after policy + approval.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from opspilot.schemas import Action, Injection
from testbed import deployctl


class Restarter(Protocol):
    def restart(self, name: str) -> None: ...


def inject(op: Injection, state: Path) -> None:
    if op.op == "deploy":
        deployctl.deploy(op.service, op.version, state=state, change_id=op.id, author=op.author)
    elif op.op == "set_config":
        deployctl.set_config(op.service, op.key, op.value, state=state, change_id=op.id,
                             summary=op.summary, author=op.author or "ops.sam")
    elif op.op == "fault":
        deployctl.set_fault(op.key, op.value, state=state)
    elif op.op == "poison_job":
        deployctl.enqueue_poison(state=state)
    else:
        raise ValueError(op.op)


def execute_action(action: Action, state: Path, stack: Restarter) -> None:
    if action.type == "rollback_deploy":
        deployctl.deploy(action.service, action.to_version, state=state, rollback=True)
    elif action.type == "revert_config":
        deployctl.set_config(action.service, action.key, action.value, state=state,
                             summary=f"Revert {action.key} to {action.value!r}", author="ops.oncall")
    elif action.type == "restart_service":
        stack.restart(action.service)
    elif action.type == "escalate":
        pass  # a human takes over; nothing to execute
    else:
        raise ValueError(action.type)
