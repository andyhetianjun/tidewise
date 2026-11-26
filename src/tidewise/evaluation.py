"""Score an agent on the question set in eval_cases.json.

Two checks per case:
- tool selection: did it call the expected tool, with the expected arguments?
- answer: does the final text contain the expected facts?

Cases that expect no tool pass the first check only if no tool was called.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent import AgentResult, McpTools, ModelClient, run_agent

DEFAULT_CASES = Path(__file__).parent / "eval_cases.json"


@dataclass
class CaseResult:
    id: str
    tool_ok: bool
    answer_ok: bool
    tools_called: list[str]
    answer: str
    missing: list[list[str]]


@dataclass
class Report:
    results: list[CaseResult]

    @property
    def n(self) -> int:
        return len(self.results)

    def rate(self, attr: str) -> float:
        return sum(getattr(r, attr) for r in self.results) / self.n

    @property
    def both(self) -> float:
        return sum(r.tool_ok and r.answer_ok for r in self.results) / self.n

    def summary(self) -> str:
        def pct(x: float) -> str:
            return f"{round(x * self.n)}/{self.n} ({x:.0%})"

        return (
            f"cases: {self.n}\n"
            f"tool selection: {pct(self.rate('tool_ok'))}\n"
            f"answer correct: {pct(self.rate('answer_ok'))}\n"
            f"both:           {pct(self.both)}"
        )


def _arg_matches(actual: Any, spec: Any) -> bool:
    if isinstance(spec, dict):
        try:
            return abs(float(actual) - spec["approx"]) <= spec["tol"]
        except (TypeError, ValueError):
            return False
    options = spec if isinstance(spec, list) else [spec]
    return str(actual).strip().lower() in {str(o).strip().lower() for o in options}


def check_tools(case: dict, result: AgentResult) -> bool:
    expected = case["expected_tool"]
    if expected is None:
        return not result.tool_calls
    for call in result.tool_calls:
        if call.name != expected:
            continue
        if all(
            key in call.input and _arg_matches(call.input[key], spec)
            for key, spec in case["expected_args"].items()
        ):
            return True
    return False


def check_answer(case: dict, answer: str) -> tuple[bool, list[list[str]]]:
    text = answer.lower()
    missing = [
        group
        for group in case["answer_must_include"]
        if not any(option.lower() in text for option in group)
    ]
    return not missing, missing


def load_cases(path: Path = DEFAULT_CASES) -> tuple[datetime, list[dict]]:
    raw = json.loads(path.read_text())
    return datetime.fromisoformat(raw["now"]).astimezone(UTC), raw["cases"]


async def run_eval(model: ModelClient, tools: McpTools, cases: list[dict], now: datetime) -> Report:
    results = []
    for case in cases:
        result = await run_agent(case["question"], model, tools, now)
        answer_ok, missing = check_answer(case, result.answer)
        results.append(
            CaseResult(
                id=case["id"],
                tool_ok=check_tools(case, result),
                answer_ok=answer_ok,
                tools_called=[c.name for c in result.tool_calls],
                answer=result.answer,
                missing=missing,
            )
        )
    return Report(results)
