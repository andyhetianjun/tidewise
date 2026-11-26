"""Run eval cases in parallel, each in its own process, with a timeout and retries.

Why: a case that hangs on the network, or one flaky request, should not stall the
whole run or change the score of the other cases. Each case gets a fresh process,
a deadline, and a few retries when the failure looks temporary (a network error, a
rate limit, a timeout). The final record of every case holds each attempt and the
full trace of the last one: every model reply and tool call with its arguments,
result and timing.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Statuses of one attempt.
OK = "ok"  # the agent finished and the case was scored (it may still have failed a check)
TIMEOUT = "timeout"
TRANSIENT = "transient_error"
ERROR = "error"  # a bug or an unexpected exception: not retried

RETRYABLE = {TIMEOUT, TRANSIENT}


@dataclass
class Attempt:
    number: int
    status: str
    duration_s: float
    error: str | None = None


@dataclass
class CaseRun:
    case: dict
    attempts: list[Attempt] = field(default_factory=list)
    reply: dict[str, Any] = field(default_factory=dict)  # worker reply of the last attempt

    @property
    def status(self) -> str:
        return self.attempts[-1].status if self.attempts else ERROR

    @property
    def result(self) -> dict | None:
        return self.reply.get("result") if self.status == OK else None

    @property
    def passed(self) -> bool:
        return bool(self.result and self.result["passed"])

    @property
    def duration_s(self) -> float:
        return round(sum(a.duration_s for a in self.attempts), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case["id"],
            "status": self.status,
            "passed": self.passed,
            "attempts": [vars(a) for a in self.attempts],
            "duration_s": self.duration_s,
            "case": self.case,
            "result": self.result,
            "trace": self.reply.get("trace", []) if self.status == OK else [],
            "error": None if self.status == OK else (self.attempts[-1].error or ""),
        }


@dataclass
class RunSummary:
    model: str
    jobs: int
    timeout_s: float
    retries: int
    started_at: str
    wall_s: float
    runs: list[CaseRun]

    @property
    def n(self) -> int:
        return len(self.runs)

    def count(self, status: str) -> int:
        return sum(r.status == status for r in self.runs)

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.runs)

    @property
    def retried(self) -> int:
        return sum(len(r.attempts) > 1 for r in self.runs)

    @property
    def busy_s(self) -> float:
        """Total time all cases took if they had run one after another."""
        return round(sum(r.duration_s for r in self.runs), 2)

    def rate(self, key: str) -> tuple[int, int]:
        scored = [r.result for r in self.runs if r.result]
        return sum(bool(r[key]) for r in scored), self.n


def worker_command() -> list[str]:
    return [sys.executable, "-m", "tidewise.worker"]


async def _attempt(
    request: dict, command: list[str], timeout_s: float
) -> tuple[str, dict[str, Any], float, str | None]:
    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(request).encode()), timeout_s)
    except TimeoutError:
        proc.kill()  # only the process this runner started
        await proc.wait()
        return TIMEOUT, {}, time.perf_counter() - started, f"no reply within {timeout_s:g} s"
    took = time.perf_counter() - started
    try:
        reply = json.loads(out.decode())
    except ValueError:
        tail = err.decode(errors="replace").strip()[-300:]
        return ERROR, {}, took, f"worker exited with code {proc.returncode}: {tail}"
    return reply["status"], reply, took, reply.get("error")


async def run_case(
    case: dict,
    *,
    model: str,
    now: str,
    timeout_s: float,
    retries: int,
    latency_ms: int = 0,
    command: list[str] | None = None,
    backoff_s: float = 0.5,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> CaseRun:
    run = CaseRun(case)
    for number in range(1, retries + 2):
        request = {
            "case": case,
            "model": model,
            "now": now,
            "attempt": number,
            "latency_ms": latency_ms,
        }
        status, reply, took, error = await _attempt(request, command or worker_command(), timeout_s)
        run.attempts.append(Attempt(number, status, round(took, 3), error))
        run.reply = reply
        if status not in RETRYABLE or number > retries:
            break
        await sleep(backoff_s * 2 ** (number - 1))
    return run


async def run_cases(
    cases: list[dict],
    now: datetime,
    *,
    model: str = "fake",
    jobs: int = 4,
    timeout_s: float = 60.0,
    retries: int = 2,
    latency_ms: int = 0,
    command: list[str] | None = None,
    backoff_s: float = 0.5,
    on_done: Callable[[CaseRun], None] | None = None,
) -> RunSummary:
    gate = asyncio.Semaphore(jobs)
    started = time.perf_counter()
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")

    async def one(case: dict) -> CaseRun:
        async with gate:
            run = await run_case(
                case,
                model=model,
                now=now.isoformat(),
                timeout_s=timeout_s,
                retries=retries,
                latency_ms=latency_ms,
                command=command,
                backoff_s=backoff_s,
            )
        if on_done:
            on_done(run)
        return run

    runs = await asyncio.gather(*(one(c) for c in cases))  # keeps the input order
    return RunSummary(
        model=model,
        jobs=jobs,
        timeout_s=timeout_s,
        retries=retries,
        started_at=stamp,
        wall_s=round(time.perf_counter() - started, 2),
        runs=list(runs),
    )
