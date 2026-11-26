"""Command line: tidewise serve | ask | eval | run-evals | site | record."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from . import config


def _model(choice: str | None):  # noqa: ANN202
    from .agent import AnthropicModel
    from .fake import RuleModel, SloppyModel

    if choice is None:
        choice = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "fake"
        if choice == "fake":
            print("No ANTHROPIC_API_KEY set, using the offline rule-based model.", file=sys.stderr)
    if choice == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY is not set. Use --model fake to run offline.")
        return AnthropicModel()
    if choice == "sloppy":
        return SloppyModel(config.now())
    return RuleModel(config.now())


async def _ask(args: argparse.Namespace) -> int:
    from .agent import McpTools, run_agent
    from .server import create_server

    model = _model(args.model)
    async with McpTools(create_server()) as tools:
        result = await run_agent(args.question, model, tools, config.now())
    if args.verbose:
        for call in result.tool_calls:
            flag = " (error)" if call.is_error else ""
            print(f"[tool] {call.name} {call.input}{flag}", file=sys.stderr)
    print(result.answer)
    return 0


async def _eval(args: argparse.Namespace) -> int:
    from .agent import McpTools
    from .evaluation import DEFAULT_CASES, load_cases, run_eval
    from .server import create_server

    now, cases = load_cases(Path(args.cases) if args.cases else DEFAULT_CASES)
    os.environ["TIDEWISE_OFFLINE"] = "1"  # the cases are written against the sample
    os.environ["TIDEWISE_NOW"] = now.isoformat()
    model = _model(args.model or "fake")
    async with McpTools(create_server()) as tools:
        report = await run_eval(model, tools, cases, now)
    for r in report.results:
        if args.verbose or not r.passed:
            mark = "ok  " if r.passed else "FAIL"
            print(
                f"{mark} {r.id}: tools={r.tools_called} tool_ok={r.tool_ok} "
                f"answer_ok={r.answer_ok} grounded={r.grounded}"
            )
            if not r.passed:
                print(f"     answer: {r.answer[:200]}")
                if r.missing:
                    print(f"     missing: {r.missing}")
                for c in r.unsupported:
                    print(f"     unsupported {c['kind']}: {c['text']} ({c['support']})")
    print(report.summary())
    # Held-out cases are reported but do not decide the exit code.
    dev = [r for r in report.results if r.split != "heldout"]
    return 0 if all(r.passed for r in dev) else 1


def _record(args: argparse.Namespace) -> int:
    from . import record as rec
    from .evaluation import DEFAULT_CASES, load_cases

    now, cases = load_cases(Path(args.cases) if args.cases else DEFAULT_CASES)
    folder = rec.recordings_dir(args.model, Path(args.root))
    try:
        if hasattr(args, "case_id"):
            args.case_id = rec.resolve_id(cases, args.case_id)
        if args.action == "list":
            print(rec.cmd_list(folder, cases))
        elif args.action == "status":
            print(rec.cmd_status(folder, cases))
        elif args.action == "prompt":
            print(rec.cmd_prompt(folder, cases, now, args.case_id, live=args.live, redo=args.redo))
        elif args.action == "tool":
            output, is_error = rec.cmd_tool(
                folder, args.case_id, args.tool, args.arguments, live=args.live
            )
            print(output)
            if is_error:
                print("(the tool returned an error; this call is logged)", file=sys.stderr)
        else:
            rec.cmd_answer(folder, args.case_id, args.text, model=args.model)
            print(f"Saved the final answer for {rec.oid(args.case_id)}. Done with this question.")
    except rec.RecordError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


async def _run_evals(args: argparse.Namespace) -> int:
    from .evaluation import DEFAULT_CASES, load_cases
    from .report import write_report
    from .runner import run_cases

    now, cases = load_cases(Path(args.cases) if args.cases else DEFAULT_CASES)
    if args.model == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Use --model fake to run offline.")
    if args.only:
        cases = [c for c in cases if any(part in c["id"] for part in args.only)]
        if not cases:
            sys.exit("No case id matches --only.")

    def progress(run) -> None:  # noqa: ANN001
        mark = "ok  " if run.passed else ("FAIL" if run.status == "ok" else run.status.upper())
        extra = f" ({len(run.attempts)} tries)" if len(run.attempts) > 1 else ""
        print(f"{mark} {run.case['id']} {run.duration_s:.2f}s{extra}", file=sys.stderr)

    summary = await run_cases(
        cases,
        now,
        model=args.model,
        jobs=args.jobs,
        timeout_s=args.timeout,
        retries=args.retries,
        latency_ms=args.latency_ms,
        on_done=progress if args.verbose else None,
    )
    index = write_report(summary, Path(args.out))
    n = summary.n
    tool, _ = summary.rate("tool_ok")
    ans, _ = summary.rate("answer_ok")
    gro, _ = summary.rate("grounded")
    print(f"cases: {n}   model: {args.model}   jobs: {args.jobs}   timeout: {args.timeout:g}s")
    print(f"tool selection: {tool}/{n}   answer correct: {ans}/{n}   grounded: {gro}/{n}")
    print(
        f"all three: {summary.passed}/{n}   timeouts: {summary.count('timeout')}   "
        f"network errors: {summary.count('transient_error')}   errors: {summary.count('error')}   "
        f"cases retried: {summary.retried}"
    )
    print(f"wall time: {summary.wall_s:g} s   (sum of case times: {summary.busy_s:g} s)")
    print(f"report: {index}")
    dev = [r for r in summary.runs if r.case.get("split") != "heldout"]
    return 0 if all(r.passed for r in dev) else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="tidewise", description="Canadian tide data for AI assistants."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the MCP server over stdio")

    ask = sub.add_parser("ask", help="ask a question with the tool-calling agent")
    ask.add_argument("question")
    ask.add_argument(
        "--model", choices=["anthropic", "fake"], help="default: anthropic if a key is set"
    )
    ask.add_argument("--offline", action="store_true", help="use the bundled sample data")
    ask.add_argument("-v", "--verbose", action="store_true", help="print the tool calls")

    ev = sub.add_parser("eval", help="score the agent on the question set (always offline data)")
    ev.add_argument(
        "--model",
        choices=["anthropic", "fake", "sloppy"],
        help="default: fake. 'sloppy' is a careless stand-in that the checks should catch",
    )
    ev.add_argument("--cases", help="path to a cases JSON file")
    ev.add_argument("-v", "--verbose", action="store_true", help="show every case")
    ev.add_argument(
        "--from-recordings",
        metavar="DIR",
        help="score saved recordings (see `tidewise record`) instead of running a model",
    )

    rc = sub.add_parser(
        "record", help="record a model's answers to the eval questions, step by step"
    )
    rc.add_argument("--model", default="claude-sonnet-5-5", help="model id, names the folder")
    rc.add_argument("--root", default="recordings", help="where recordings are kept")
    rc.add_argument("--cases", help="path to a cases JSON file")
    ra = rc.add_subparsers(dest="action", required=True)
    ra.add_parser("list", help="every case id and whether it is recorded")
    ra.add_parser("status", help="how many cases are recorded")
    rp = ra.add_parser("prompt", help="print what the model sees and start a session")
    rp.add_argument("case_id")
    rp.add_argument("--live", action="store_true", help="use live data (not scoreable)")
    rp.add_argument("--redo", action="store_true", help="record a case that is already done")
    rt = ra.add_parser("tool", help="run a real tool and log the call")
    rt.add_argument("case_id")
    rt.add_argument("tool")
    rt.add_argument("arguments", nargs="?", default="{}", help="a JSON object")
    rt.add_argument("--live", action="store_true", help="use live data (not scoreable)")
    rn = ra.add_parser("answer", help="save the final answer")
    rn.add_argument("case_id")
    rn.add_argument("text")

    rv = sub.add_parser(
        "run-evals",
        help="run each eval case in its own process, in parallel, and write an HTML report",
    )
    rv.add_argument("--model", choices=["anthropic", "fake", "sloppy", "flaky"], default="fake")
    rv.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 2), help="cases at a time")
    rv.add_argument("--timeout", type=float, default=60.0, help="seconds per attempt")
    rv.add_argument("--retries", type=int, default=2, help="extra tries after a network error")
    rv.add_argument("--latency-ms", type=int, default=0, help="add delay to every tool call")
    rv.add_argument("--out", default="eval-report", help="folder for the report")
    rv.add_argument("--cases", help="path to a cases JSON file")
    rv.add_argument("--only", nargs="+", help="run only cases whose id contains one of these")
    rv.add_argument("-v", "--verbose", action="store_true", help="print each case as it finishes")

    site = sub.add_parser("site", help="build the static demo site from real eval runs")
    site.add_argument("--out", default="site", help="folder to write")
    site.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 2))
    site.add_argument(
        "--live",
        action="store_true",
        help="add a Today page from live tide and weather data (falls back to the sample)",
    )
    site.add_argument(
        "--recordings",
        default="recordings/claude-sonnet-5-5",
        help="folder of recorded runs; the page is added only if it has recordings",
    )
    site.add_argument(
        "--bench", action="store_true", help="also time the cases one at a time, for comparison"
    )

    args = p.parse_args(argv)
    logging.getLogger().setLevel(
        logging.ERROR
    )  # the MCP client logs tool errors; the CLI shows them itself
    if getattr(args, "offline", False):
        os.environ["TIDEWISE_OFFLINE"] = "1"
    if args.cmd == "serve":
        from .server import main as serve

        serve()
        return 0
    if args.cmd == "record":
        return _record(args)
    if args.cmd == "eval" and args.from_recordings:
        from .evaluation import DEFAULT_CASES
        from .record import RecordError, score_command

        try:
            return score_command(
                Path(args.from_recordings), Path(args.cases) if args.cases else DEFAULT_CASES
            )
        except RecordError as exc:
            sys.exit(f"error: {exc}")
    if args.cmd == "ask":
        return asyncio.run(_ask(args))
    if args.cmd == "site":
        from .site import build_site

        asyncio.run(
            build_site(
                Path(args.out),
                jobs=args.jobs,
                bench=args.bench,
                live=args.live,
                recordings=Path(args.recordings),
            )
        )
        print(f"site written to {args.out}/index.html")
        return 0
    if args.cmd == "run-evals":
        return asyncio.run(_run_evals(args))
    return asyncio.run(_eval(args))


if __name__ == "__main__":
    sys.exit(main())
