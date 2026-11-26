"""Command line: tidewise serve | ask | eval."""

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
    if args.cmd == "ask":
        return asyncio.run(_ask(args))
    return asyncio.run(_eval(args))


if __name__ == "__main__":
    sys.exit(main())
