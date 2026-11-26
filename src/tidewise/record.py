"""Recorded runs: a model answers eval questions step by step through a shell.

A model that cannot be called through an API can still be evaluated. The
`tidewise record` commands let any agent that can run shell commands act as the
model: it reads the prompt, calls the real tools one at a time, and submits a final
answer. Everything is logged and saved as one JSON file per question. The saved
files are scored with the same three checks as every other model.

    tidewise record prompt <case-id>      what the model sees, and what to type next
    tidewise record tool <case-id> <name> '<json>'
    tidewise record answer <case-id> '<text>'
    tidewise record list | status
    tidewise eval --from-recordings recordings/<model>
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent import MAX_STEPS, AgentResult, McpTools, ToolRecord, system_prompt
from .evaluation import DEFAULT_CASES, CaseResult, Report, evaluate_case, load_cases

HARNESS_VERSION = "1"
DEFAULT_MODEL = "claude-sonnet-5-5"
MODEL_LABELS = {"claude-sonnet-5-5": "Claude Sonnet 5.5"}
MAX_CALLS = MAX_STEPS
CMD = "uv run tidewise record"


class RecordError(Exception):
    """A problem the person (or agent) driving the harness can fix."""


def model_label(model: str) -> str:
    return MODEL_LABELS.get(model, model)


def recordings_dir(model: str = DEFAULT_MODEL, root: Path | None = None) -> Path:
    return (root or Path("recordings")) / model


def _final_path(folder: Path, case_id: str) -> Path:
    return folder / f"{case_id}.json"


def _work_path(folder: Path, case_id: str) -> Path:
    return folder / ".work" / f"{case_id}.json"


_OPAQUE: dict[str, str] = {}


def opaque_ids(cases: list[dict]) -> dict[str, str]:
    """Readable id -> c01, c02, ... in the order of a hash of the question text.

    The readable ids (like trap-waves) give away what a good answer looks like, so a
    model being recorded only ever sees the opaque one. The order depends only on the
    question text, so the ids stay the same as long as the questions do.
    """
    ranked = sorted(cases, key=lambda c: hashlib.sha256(c["question"].encode()).hexdigest())
    width = max(2, len(str(len(ranked))))
    return {c["id"]: f"c{i:0{width}d}" for i, c in enumerate(ranked, 1)}


def oid(case_id: str) -> str:
    """The opaque id to show for a readable one."""
    return _OPAQUE.get(case_id, case_id)


def resolve_id(cases: list[dict], given: str) -> str:
    """Accept an opaque id (or the readable one, for the coordinator); return the readable id."""
    _OPAQUE.update(opaque_ids(cases))
    for readable, opaque in _OPAQUE.items():
        if given in (opaque, readable):
            return readable
    raise RecordError(f"Unknown case id: {given!r}. Use the id you were given.")


def _find_case(cases: list[dict], case_id: str) -> dict:
    for case in cases:
        if case["id"] == case_id:
            return case
    raise RecordError(f"Unknown case id: {case_id!r}. Use the id you were given.")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _status_of(folder: Path, case_id: str) -> str:
    if _final_path(folder, case_id).exists():
        return "done"
    if _work_path(folder, case_id).exists():
        return "started"
    return "todo"


# ---- the tools --------------------------------------------------------


async def _tool_specs() -> list[dict]:
    from .server import create_server

    async with McpTools(create_server()) as tools:
        return tools.specs


async def _call_tool(name: str, args: dict[str, Any]) -> tuple[str, bool]:
    from .server import create_server

    async with McpTools(create_server()) as tools:
        try:
            return await tools.call(name, args)
        except Exception as exc:  # unknown tool name, protocol problem
            return f"Tool call failed: {exc}", True


def _set_environment(live: bool, now: datetime) -> datetime:
    """Offline sample and a fixed clock, so recordings are reproducible."""
    if live:
        os.environ.pop("TIDEWISE_OFFLINE", None)
        os.environ.pop("TIDEWISE_NOW", None)
        return datetime.now(UTC)
    os.environ["TIDEWISE_OFFLINE"] = "1"
    os.environ["TIDEWISE_NOW"] = now.isoformat()
    return now


# ---- commands ---------------------------------------------------------


def _read_state(folder: Path, case_id: str) -> dict:
    path = _work_path(folder, case_id)
    if not path.exists():
        raise RecordError(
            f"No session for {oid(case_id)}. Run `{CMD} prompt {oid(case_id)}` first."
        )
    return json.loads(path.read_text())


def cmd_list(folder: Path, cases: list[dict]) -> str:
    _OPAQUE.update(opaque_ids(cases))
    lines = [f"{'id':<6} {'readable id (coordinator only)':<40} {'kind':<10} status"]
    for c in sorted(cases, key=lambda c: oid(c["id"])):
        kind = "trap" if c.get("expect_abstain") else c.get("category", "tide")
        lines.append(f"{oid(c['id']):<6} {c['id']:<40} {kind:<10} {_status_of(folder, c['id'])}")
    return "\n".join(lines)


def cmd_status(folder: Path, cases: list[dict]) -> str:
    _OPAQUE.update(opaque_ids(cases))
    states = {c["id"]: _status_of(folder, c["id"]) for c in cases}
    done = [i for i, s in states.items() if s == "done"]
    started = [i for i, s in states.items() if s == "started"]
    todo = [i for i, s in states.items() if s == "todo"]
    lines = [f"recorded {len(done)} of {len(cases)} (folder: {folder})"]
    if started:
        lines.append(
            f"started, no answer yet ({len(started)}): " + ", ".join(oid(i) for i in started)
        )
    if todo:
        lines.append(f"not started: {len(todo)}")
    return "\n".join(lines)


def cmd_prompt(
    folder: Path, cases: list[dict], now: datetime, case_id: str, *, live: bool, redo: bool
) -> str:
    case = _find_case(cases, case_id)
    if _final_path(folder, case_id).exists() and not redo:
        raise RecordError(f"{oid(case_id)} is already recorded. Pass --redo to record it again.")
    when = _set_environment(live, now)
    system = system_prompt(when)
    specs = asyncio.run(_tool_specs())
    state = {
        "harness_version": HARNESS_VERSION,
        "case_id": case_id,
        "started_at": _now_iso(),
        "live": live,
        "now": when.isoformat(),
        "prompt": {"system": system, "question": case["question"], "tools": specs},
        "tool_calls": [],
    }
    path = _work_path(folder, case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    parts = ["=== SYSTEM PROMPT ===", system, "", "=== TOOLS ==="]
    for spec in specs:
        parts += [
            f"--- {spec['name']}",
            spec["description"].strip(),
            "input schema: " + json.dumps(spec["input_schema"], ensure_ascii=False),
        ]
    parts += [
        "",
        "=== QUESTION ===",
        case["question"],
        "",
        "=== WHAT TO DO NEXT ===",
        f"Call a tool (at most {MAX_CALLS} calls in total):",
        f"  {CMD} tool {oid(case_id)} <tool-name> '<arguments as a JSON object>'",
        "Then give your final answer, as the assistant would write it to the user:",
        f"  {CMD} answer {oid(case_id)} '<your final answer>'",
        "Use only what the system prompt, the tool list and the tool results tell you.",
    ]
    return "\n".join(parts)


def cmd_tool(
    folder: Path, case_id: str, name: str, raw_args: str, *, live: bool
) -> tuple[str, bool]:
    state = _read_state(folder, case_id)
    if len(state["tool_calls"]) >= MAX_CALLS:
        raise RecordError(
            f"That is already {MAX_CALLS} tool calls. Give your final answer with "
            f"`{CMD} answer {oid(case_id)} '<text>'`."
        )
    try:
        args = json.loads(raw_args) if raw_args.strip() else {}
    except ValueError as exc:
        raise RecordError(
            f"The arguments are not valid JSON ({exc}). Nothing was logged; try again."
        ) from exc
    if not isinstance(args, dict):
        raise RecordError('The arguments must be a JSON object like \'{"station": "00490"}\'.')
    use_live = live or state["live"]
    _set_environment(use_live, datetime.fromisoformat(state["now"]))
    started = time.perf_counter()
    output, is_error = asyncio.run(_call_tool(name, args))
    took = round((time.perf_counter() - started) * 1000, 1)
    state["tool_calls"].append(
        {
            "name": name,
            "input": args,
            "output": output,
            "is_error": is_error,
            "duration_ms": took,
            "called_at": _now_iso(),
            "live": use_live,
        }
    )
    _work_path(folder, case_id).write_text(json.dumps(state, indent=1, ensure_ascii=False))
    return output, is_error


def cmd_answer(folder: Path, case_id: str, text: str, *, model: str) -> Path:
    state = _read_state(folder, case_id)
    answer = text.strip()
    if not answer:
        raise RecordError("The answer is empty.")
    started = datetime.fromisoformat(state["started_at"])
    recording = {
        "harness_version": HARNESS_VERSION,
        "model": model,
        "model_label": model_label(model),
        "case_id": case_id,
        "opaque_id": oid(case_id),
        "started_at": state["started_at"],
        "recorded_at": _now_iso(),
        "elapsed_s": round((datetime.now(UTC) - started).total_seconds(), 1),
        "live": state["live"] or any(c.get("live") for c in state["tool_calls"]),
        "now": state["now"],
        "prompt": state["prompt"],
        "tool_calls": state["tool_calls"],
        "answer": answer,
    }
    final = _final_path(folder, case_id)
    final.write_text(json.dumps(recording, indent=1, ensure_ascii=False))
    _work_path(folder, case_id).unlink(missing_ok=True)
    return final


# ---- reading recordings back -----------------------------------------


def load_recordings(folder: Path) -> dict[str, dict]:
    found = {}
    for path in sorted(folder.glob("*.json")):
        if path.name in {"results.json"}:
            continue
        data = json.loads(path.read_text())
        if "case_id" in data and "answer" in data:
            found[data["case_id"]] = data
    return found


def to_result(rec: dict) -> AgentResult:
    """Rebuild the agent result and a trace in the shape the report pages draw."""
    calls = [
        ToolRecord(c["name"], c["input"], c["output"], c["is_error"], c["duration_ms"])
        for c in rec["tool_calls"]
    ]
    trace: list[dict] = []
    clock = 0.0
    started = datetime.fromisoformat(rec["started_at"])
    step = 0
    for i, c in enumerate(rec["tool_calls"], 1):
        step = i
        at = max(0.0, (datetime.fromisoformat(c["called_at"]) - started).total_seconds() * 1000)
        trace.append(
            {
                "type": "model",
                "step": i,
                "at_ms": round(clock, 1),
                "duration_ms": round(max(0.0, at - c["duration_ms"] - clock), 1),
                "text": "",
                "stop_reason": "tool_use",
                "tool_calls": [{"id": f"rec{i}", "name": c["name"], "input": c["input"]}],
            }
        )
        trace.append(
            {
                "type": "tool",
                "step": i,
                "at_ms": round(at - c["duration_ms"], 1),
                "duration_ms": c["duration_ms"],
                "id": f"rec{i}",
                "name": c["name"],
                "input": c["input"],
                "output": c["output"],
                "is_error": c["is_error"],
            }
        )
        clock = at
    end = rec.get("elapsed_s", 0) * 1000
    trace.append(
        {
            "type": "model",
            "step": step + 1,
            "at_ms": round(clock, 1),
            "duration_ms": round(max(0.0, end - clock), 1),
            "text": rec["answer"],
            "stop_reason": "end_turn",
            "tool_calls": [],
        }
    )
    return AgentResult(
        answer=rec["answer"], tool_calls=calls, steps=step + 1, stop_reason="end_turn", trace=trace
    )


def score_recordings(
    folder: Path, cases_path: Path = DEFAULT_CASES
) -> tuple[Report, list[dict], list[str]]:
    """Score every recorded case. Returns the report, the recordings and unrecorded ids."""
    _, cases = load_cases(cases_path)
    recs = load_recordings(folder)
    unknown = [i for i in recs if i not in {c["id"] for c in cases}]
    if unknown:
        raise RecordError(f"Recordings for unknown case ids: {', '.join(unknown)}")
    results: list[CaseResult] = []
    used = []
    for case in cases:
        rec = recs.get(case["id"])
        if rec is None:
            continue
        results.append(evaluate_case(case, to_result(rec)))
        used.append(rec)
    missing = [c["id"] for c in cases if c["id"] not in recs]
    return Report(results), used, missing


async def _baseline(model_name: str, cases: list[dict], now: datetime) -> Report:
    from .evaluation import run_eval
    from .fake import RuleModel, SloppyModel
    from .server import create_server

    os.environ["TIDEWISE_OFFLINE"] = "1"
    os.environ["TIDEWISE_NOW"] = now.isoformat()
    model = SloppyModel(now) if model_name == "sloppy" else RuleModel(now)
    async with McpTools(create_server()) as tools:
        return await run_eval(model, tools, cases, now)


def results_markdown(
    report: Report, recs: list[dict], missing: list[str], baselines: dict[str, Report]
) -> str:
    label = recs[0]["model_label"] if recs else "the recorded model"
    model_id = recs[0]["model"] if recs else DEFAULT_MODEL
    n = report.n
    total = n + len(missing)

    def row(name: str, r: Report) -> str:
        traps = r.traps()
        trap_ok = sum(x.answer_ok and x.grounded for x in traps)
        k = r.n
        return (
            f"| {name} | {sum(x.tool_ok for x in r.results)}/{k} "
            f"| {sum(x.answer_ok for x in r.results)}/{k} "
            f"| {sum(x.grounded for x in r.results)}/{k} "
            f"| {f'{trap_ok}/{len(traps)}' if traps else 'n/a'} "
            f"| {sum(x.passed for x in r.results)}/{k} |"
        )

    lines = [
        f"# Recorded results: {label} (`{model_id}`)",
        "",
        f"{n} of {total} questions recorded. Scores below cover only the recorded questions.",
        "",
        "| Model | Right tools | Answer as expected | Grounded | Trap cases handled | All three |",
        "| --- | --- | --- | --- | --- | --- |",
        row(f"{label} (recorded)", report),
    ]
    for name, base in baselines.items():
        lines.append(row(name, base))
    lines += ["", "| Case | Tools | Answer | Grounded | Calls |", "| --- | --- | --- | --- | --- |"]
    for r in report.results:
        mark = {True: "pass", False: "fail"}
        lines.append(
            f"| {r.id} | {mark[r.tool_ok]} | {mark[r.answer_ok]} | {mark[r.grounded]} "
            f"| {len(r.tools_called)} |"
        )
    failed = [r for r in report.results if not r.passed]
    if failed:
        lines += ["", "## Misses", ""]
        for r in failed:
            why = []
            if not r.tool_ok:
                why.append(f"tools called: {', '.join(r.tools_called) or 'none'}")
            if r.missing:
                why.append("answer missing: " + "; ".join("/".join(g) for g in r.missing))
            for c in r.unsupported:
                why.append(f"unsupported {c['kind']}: {c['text']}")
            lines.append(f"- {r.id}: " + " | ".join(why))
    if missing:
        lines += ["", f"Not recorded yet ({len(missing)}): " + ", ".join(missing)]
    return "\n".join(lines) + "\n"


def score_command(folder: Path, cases_path: Path = DEFAULT_CASES) -> int:
    report, recs, missing = score_recordings(folder, cases_path)
    if not recs:
        print(f"No recordings found in {folder}.", file=sys.stderr)
        return 1
    now, cases = load_cases(cases_path)
    subset = [c for c in cases if c["id"] in {r.id for r in report.results}]
    baselines = {
        "Offline rule-based model": asyncio.run(_baseline("fake", subset, now)),
        "Deliberately sloppy model": asyncio.run(_baseline("sloppy", subset, now)),
    }
    for r in report.results:
        if not r.passed:
            print(
                f"FAIL {r.id}: tools={r.tools_called} tool_ok={r.tool_ok} "
                f"answer_ok={r.answer_ok} grounded={r.grounded}"
            )
            if r.missing:
                print(f"     missing: {r.missing}")
            for c in r.unsupported:
                print(f"     unsupported {c['kind']}: {c['text']}")
    print(report.summary())
    if missing:
        print(f"({len(missing)} of {report.n + len(missing)} questions not recorded)")
    (folder / "RESULTS.md").write_text(results_markdown(report, recs, missing, baselines))
    (folder / "results.json").write_text(
        json.dumps(
            {
                "model": recs[0]["model"],
                "recorded": report.n,
                "total": report.n + len(missing),
                "cases": [asdict(r) | {"passed": r.passed} for r in report.results],
            },
            indent=1,
            ensure_ascii=False,
        )
    )
    print(f"wrote {folder / 'RESULTS.md'}")
    return 0
