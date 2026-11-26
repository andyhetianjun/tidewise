"""Measure what tracing costs: the offline eval (43 questions) with and without it.

    uv run python scripts/bench_tracing.py [rounds]

Each round runs every question once with tracing off and once with it on (file
exporter, to a temp folder), alternating so drift in the machine hits both. The
offline model answers in milliseconds, so this is close to the worst case for the
relative overhead: with a real model call of a second or more, the same absolute cost
is a much smaller share.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

os.environ["TIDEWISE_OFFLINE"] = "1"

from tidewise import tracing  # noqa: E402
from tidewise.agent import McpTools  # noqa: E402
from tidewise.evaluation import DEFAULT_CASES, load_cases, run_eval  # noqa: E402
from tidewise.fake import RuleModel  # noqa: E402
from tidewise.server import create_server  # noqa: E402


async def once(traced: bool, folder: Path) -> tuple[float, int]:
    now, cases = load_cases(DEFAULT_CASES)
    os.environ["TIDEWISE_NOW"] = now.isoformat()
    os.environ["TIDEWISE_TRACE_FILE"] = str(folder / "t.jsonl")
    if traced:
        tracing.setup_tracing()
    t = time.perf_counter()
    async with McpTools(create_server()) as tools:
        await run_eval(RuleModel(now), tools, cases, now)
    tracing.shutdown_tracing()  # includes flushing the file, so it is part of the cost
    return time.perf_counter() - t, len(cases)


async def main(rounds: int) -> None:
    with tempfile.TemporaryDirectory() as d:
        folder = Path(d)
        await once(False, folder)
        await once(True, folder)  # warm up
        (folder / "t.jsonl").unlink()
        off, on = [], []
        for _ in range(rounds):
            off.append((await once(False, folder))[0])
            on.append((await once(True, folder))[0])
        n = (await once(False, folder))[1]
        import json

        spans = sum(
            len(ss["spans"])
            for line in (folder / "t.jsonl").open()
            for rs in json.loads(line)["resourceSpans"]
            for ss in rs["scopeSpans"]
        )
        size = (folder / "t.jsonl").stat().st_size
    mo, mn = statistics.median(off), statistics.median(on)
    print(f"questions per run: {n}, rounds: {rounds}")
    print(f"median, tracing off: {mo * 1000:.0f} ms ({mo / n * 1000:.1f} ms per question)")
    print(f"median, tracing on:  {mn * 1000:.0f} ms ({mn / n * 1000:.1f} ms per question)")
    print(
        f"overhead: {(mn - mo) * 1000:.0f} ms per run, {(mn - mo) / n * 1000:.2f} ms per "
        f"question, {(mn / mo - 1) * 100:.1f}%"
    )
    print(f"spans per run: {spans // rounds}, file written per run: {size // rounds} bytes")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 15))
