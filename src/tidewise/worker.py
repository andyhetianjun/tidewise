"""Run one eval case in its own process.

The runner starts `python -m tidewise.worker`, writes one JSON request to its
standard input and reads one JSON reply from its standard output. A fresh process
per case means one case cannot leak state into another (the clock override, the
cache, a hung request), and the runner can kill it if it takes too long.

Request:  {"case": {...}, "model": "fake", "now": "...", "attempt": 1, "latency_ms": 0}
Reply:    {"status": "ok" | "transient_error" | "error", ...}
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime

from .errors import TransientError

# A tool that could not reach the data service says so in its error text. That is a
# network problem, not a wrong answer, so the case should be tried again.
_NETWORK_TEXT = re.compile(r"Could not reach the .* service|is busy \(HTTP", re.IGNORECASE)


def _transient_types() -> tuple[type[BaseException], ...]:
    import httpx

    types: list[type[BaseException]] = [TransientError, httpx.TransportError]
    try:
        import anthropic

        types += [
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        ]
    except ImportError:  # pragma: no cover
        pass
    return tuple(types)


def _leaves(exc: BaseException) -> list[BaseException]:
    """The MCP client runs in a task group, which wraps errors in an ExceptionGroup."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


def build_model(name: str, now: datetime, attempt: int):  # noqa: ANN201
    from .agent import AnthropicModel
    from .fake import FlakyModel, RuleModel, SloppyModel

    if name == "anthropic":
        return AnthropicModel()
    if name == "sloppy":
        return SloppyModel(now)
    if name == "flaky":
        return FlakyModel(RuleModel(now), attempt)
    return RuleModel(now)


async def run_one(request: dict) -> dict:
    from .agent import McpTools, run_agent
    from .evaluation import evaluate_case
    from .server import create_server

    now = datetime.fromisoformat(request["now"])
    os.environ["TIDEWISE_OFFLINE"] = "1"  # the cases are written against the sample
    os.environ["TIDEWISE_NOW"] = request["now"]
    case = request["case"]
    model = build_model(request["model"], now, request.get("attempt", 1))
    started = time.perf_counter()
    try:
        async with McpTools(create_server(), request.get("latency_ms", 0) / 1000) as tools:
            result = await run_agent(case["question"], model, tools, now)
    except Exception as exc:
        leaves = _leaves(exc)
        if all(isinstance(leaf, _transient_types()) for leaf in leaves):
            return {
                "status": "transient_error",
                "error": f"{type(leaves[0]).__name__}: {leaves[0]}",
            }
        raise
    for call in result.tool_calls:
        if call.is_error and _NETWORK_TEXT.search(call.output):
            return {"status": "transient_error", "error": call.output[:300]}
    scored = evaluate_case(case, result)
    return {
        "status": "ok",
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "steps": result.steps,
        "stop_reason": result.stop_reason,
        "result": asdict(scored) | {"passed": scored.passed},
        "trace": result.trace,
    }


def main() -> int:
    import logging

    logging.getLogger().setLevel(logging.ERROR)
    request = json.loads(sys.stdin.read())
    try:
        reply = asyncio.run(run_one(request))
    except Exception as exc:
        reply = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-1500:],
        }
    sys.stdout.write(json.dumps(reply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
