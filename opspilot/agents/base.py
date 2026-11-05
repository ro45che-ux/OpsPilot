from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from opspilot.findings import Findings


@dataclass
class AgentResult:
    findings: Findings | None
    stats: dict = field(default_factory=dict)       # tool_calls, tokens, wall_s, turns, validation_retries
    transcript: list[dict] = field(default_factory=list)
    error: str | None = None


class Agent(Protocol):
    name: str

    def run(self, env) -> AgentResult: ...
