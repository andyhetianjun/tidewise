"""Score an agent on the question set in eval_cases.json.

Three checks per case:
- tool selection: did it call the expected tool (and any other tools the question
  needs), with the expected arguments?
- answer: does the final text contain the expected facts? For trap cases (a
  station that does not exist, a time outside the data, a quantity no tool
  provides) the right answer is to say it does not know.
- grounded: is every checkable claim in the answer (times, heights, wind speeds,
  place names) supported by what the tools returned? See grounding.py.

Cases that expect no tool pass the first check only if no tool was called.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent import AgentResult, McpTools, ModelClient, run_agent
from .grounding import abstains, check_grounding

DEFAULT_CASES = Path(__file__).parent / "eval_cases.json"


@dataclass
class CaseResult:
    id: str
    category: str
    question: str
    tool_ok: bool
    answer_ok: bool
    grounded: bool
    tools_called: list[str]
    answer: str
    missing: list[list[str]]
    unsupported: list[dict] = field(default_factory=list)
    claims: list[dict] = field(default_factory=list)
    expect_abstain: bool = False
    split: str = "dev"

    @property
    def passed(self) -> bool:
        return self.tool_ok and self.answer_ok and self.grounded


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
        return sum(r.passed for r in self.results) / self.n

    def traps(self) -> list[CaseResult]:
        return [r for r in self.results if r.expect_abstain]

    def summary(self) -> str:
        def pct(x: float) -> str:
            return f"{round(x * self.n)}/{self.n} ({x:.0%})"

        traps = self.traps()
        trap_ok = sum(r.answer_ok and r.grounded for r in traps)
        lines = [
            f"cases: {self.n}",
            f"tool selection: {pct(self.rate('tool_ok'))}",
            f"answer correct: {pct(self.rate('answer_ok'))}",
            f"grounded:       {pct(self.rate('grounded'))}",
            f"all three:      {pct(self.both)}",
        ]
        if traps:
            lines.append(f"trap cases handled (said so, invented nothing): {trap_ok}/{len(traps)}")
        held = [r for r in self.results if r.split == "heldout"]
        if held:
            lines.append(
                f"held-out cases, all three checks: {sum(r.passed for r in held)}/{len(held)}"
            )
        return "\n".join(lines)


def _arg_matches(actual: Any, spec: Any) -> bool:
    if isinstance(spec, dict):
        try:
            return abs(float(actual) - spec["approx"]) <= spec["tol"]
        except (TypeError, ValueError):
            return False
    options = spec if isinstance(spec, list) else [spec]
    return str(actual).strip().lower() in {str(o).strip().lower() for o in options}


def _call_matches(call: Any, tool: str, args: dict) -> bool:
    return call.name == tool and all(
        key in call.input and _arg_matches(call.input[key], spec) for key, spec in args.items()
    )


def check_tools(case: dict, result: AgentResult) -> bool:
    expected = case["expected_tool"]
    if expected is None:
        return not result.tool_calls
    wanted = [(expected, case["expected_args"])] + [
        (extra["tool"], extra.get("args", {})) for extra in case.get("also_expected", [])
    ]
    return all(
        any(_call_matches(c, tool, args) for c in result.tool_calls) for tool, args in wanted
    )


def check_answer(case: dict, answer: str) -> tuple[bool, list[list[str]]]:
    text = answer.lower()
    missing = [
        group
        for group in case["answer_must_include"]
        if not any(option.lower() in text for option in group)
    ]
    if case.get("expect_abstain") and not abstains(answer):
        missing.append(["(says it does not know)"])
    return not missing, missing


def load_cases(path: Path = DEFAULT_CASES) -> tuple[datetime, list[dict]]:
    raw = json.loads(path.read_text())
    return datetime.fromisoformat(raw["now"]).astimezone(UTC), raw["cases"]


def evaluate_case(case: dict, result: AgentResult) -> CaseResult:
    answer_ok, missing = check_answer(case, result.answer)
    grounding = check_grounding(
        result.answer, [c.output for c in result.tool_calls], case["question"]
    )
    return CaseResult(
        id=case["id"],
        category=case.get("category", "tide"),
        question=case["question"],
        tool_ok=check_tools(case, result),
        answer_ok=answer_ok,
        grounded=grounding.grounded,
        tools_called=[c.name for c in result.tool_calls],
        answer=result.answer,
        missing=missing,
        unsupported=[c.to_dict() for c in grounding.unsupported],
        claims=[c.to_dict() for c in grounding.claims],
        expect_abstain=bool(case.get("expect_abstain")),
        split=case.get("split", "dev"),
    )


async def run_eval(model: ModelClient, tools: McpTools, cases: list[dict], now: datetime) -> Report:
    results = []
    for case in cases:
        result = await run_agent(case["question"], model, tools, now)
        results.append(evaluate_case(case, result))
    return Report(results)
