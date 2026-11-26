"""A small tool-calling loop over the MCP server's tools."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from . import tracing

MODEL = "claude-sonnet-5-5"
MAX_STEPS = 8
MAX_TOOL_OUTPUT_CHARS = 12000


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Turn:
    """One model reply."""

    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # "end_turn", "tool_use", "refusal", "max_tokens", ...
    content: list[Any]  # what to put back into the history as the assistant message
    usage: dict[str, int] | None = None  # input_tokens / output_tokens, when the model says
    response_model: str | None = None


class ModelClient(Protocol):
    def create(self, system: str, messages: list[dict], tools: list[dict]) -> Turn: ...


@dataclass
class ToolRecord:
    name: str
    input: dict[str, Any]
    output: str
    is_error: bool
    duration_ms: float = 0.0


@dataclass
class AgentResult:
    answer: str
    tool_calls: list[ToolRecord] = field(default_factory=list)
    steps: int = 0
    stop_reason: str = "end_turn"
    # Every model reply and tool call in order, with timings, for the trace pages.
    trace: list[dict[str, Any]] = field(default_factory=list)


def system_prompt(now: datetime) -> str:
    return (
        "You answer questions about tides and water levels in Canada using the tools. "
        "Never guess tide times or heights; call a tool. "
        f"The current time is {now:%Y-%m-%d %H:%M} UTC. "
        "Find the station first with find_stations unless you already have its code. "
        "Tool results include local times for the station; give times to the user in "
        "that local time and say which station you used. If a tool returns an error, "
        "say plainly what went wrong instead of making up an answer. Keep answers short."
    )


class AnthropicModel:
    """Calls Claude through the official SDK. Reads ANTHROPIC_API_KEY from the environment."""

    provider = "anthropic"

    def __init__(self, model: str = MODEL) -> None:
        import anthropic

        self._client = anthropic.Anthropic()
        self._model = model
        self.model_name = model

    def create(self, system: str, messages: list[dict], tools: list[dict]) -> Turn:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=system,
            tools=tools,
            messages=messages,
        )
        calls = [
            ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"
        ]
        text = "".join(b.text for b in resp.content if b.type == "text")
        usage = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        }
        return Turn(
            text,
            calls,
            resp.stop_reason or "end_turn",
            list(resp.content),
            usage=usage,
            response_model=getattr(resp, "model", None),
        )


class McpTools:
    """Talks to the tidewise MCP server in-process, through the real MCP protocol."""

    def __init__(self, server: MCPServer, latency_s: float = 0.0) -> None:
        self._client = Client(server)
        self._latency_s = latency_s  # to measure the runner against a slow network
        self.specs: list[dict] = []

    async def __aenter__(self) -> McpTools:
        await self._client.__aenter__()
        listing = await self._client.list_tools()
        self.specs = [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in listing.tools
        ]
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.__aexit__(*exc)

    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        if self._latency_s:
            await asyncio.sleep(self._latency_s)
        result = await self._client.call_tool(name, args)
        text = "".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        return text[:MAX_TOOL_OUTPUT_CHARS], bool(result.is_error)


def _provider_name(model: object) -> str:
    return str(getattr(model, "provider", "tidewise.offline"))


def _model_name(model: object) -> str:
    return str(getattr(model, "model_name", type(model).__name__))


async def run_agent(
    question: str, model: ModelClient, tools: McpTools, now: datetime
) -> AgentResult:
    """Run the loop inside one `invoke_agent` span (see tracing.py)."""
    tracer = tracing.get_tracer()
    provider, model_name = _provider_name(model), _model_name(model)
    with tracer.start_as_current_span(
        "invoke_agent tidewise",
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.provider.name": provider,
            "gen_ai.agent.name": "tidewise",
            "gen_ai.request.model": model_name,
        },
    ) as root:
        try:
            result = await _loop(question, model, tools, now, tracer, provider, model_name)
        except Exception as exc:
            root.set_attribute("error.type", type(exc).__name__)
            raise
        root.set_attribute("tidewise.steps", result.steps)
        root.set_attribute("tidewise.tool_calls", len(result.tool_calls))
        root.set_attribute("gen_ai.response.finish_reasons", [result.stop_reason])
        usage = [t["usage"] for t in result.trace if t.get("usage")]
        if usage:
            root.set_attribute("gen_ai.usage.input_tokens", sum(u["input_tokens"] for u in usage))
            root.set_attribute("gen_ai.usage.output_tokens", sum(u["output_tokens"] for u in usage))
        if result.stop_reason == "max_steps":
            root.set_attribute("error.type", "max_steps")
            root.set_status(Status(StatusCode.ERROR, "no final answer within the step limit"))
        if tracing.capture_content():
            root.set_attribute("tidewise.question", question)
            root.set_attribute("tidewise.answer", result.answer)
        return result


async def _loop(
    question: str,
    model: ModelClient,
    tools: McpTools,
    now: datetime,
    tracer: trace.Tracer,
    provider: str,
    model_name: str,
) -> AgentResult:
    system = system_prompt(now)
    messages: list[dict] = [{"role": "user", "content": question}]
    result = AgentResult(answer="")
    t0 = time.perf_counter()

    def ms() -> float:
        return round((time.perf_counter() - t0) * 1000, 1)

    for step in range(1, MAX_STEPS + 1):
        started = ms()
        with tracer.start_as_current_span(
            f"chat {model_name}",
            kind=trace.SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": provider,
                "gen_ai.request.model": model_name,
                "tidewise.step": step,
            },
        ) as span:
            try:
                turn = model.create(system, messages, tools.specs)
            except Exception as exc:
                span.set_attribute("error.type", type(exc).__name__)
                raise
            span.set_attribute("gen_ai.response.finish_reasons", [turn.stop_reason])
            if turn.response_model:
                span.set_attribute("gen_ai.response.model", turn.response_model)
            if turn.usage:
                span.set_attribute("gen_ai.usage.input_tokens", turn.usage["input_tokens"])
                span.set_attribute("gen_ai.usage.output_tokens", turn.usage["output_tokens"])
        entry = {
            "type": "model",
            "step": step,
            "at_ms": started,
            "duration_ms": round(ms() - started, 1),
            "text": turn.text,
            "stop_reason": turn.stop_reason,
            "tool_calls": [{"id": c.id, "name": c.name, "input": c.input} for c in turn.tool_calls],
        }
        if turn.usage:
            entry["usage"] = turn.usage
        result.trace.append(entry)
        result.steps = step
        result.stop_reason = turn.stop_reason
        messages.append({"role": "assistant", "content": turn.content})
        if turn.stop_reason == "refusal":
            result.answer = turn.text or "The model declined to answer this question."
            return result
        if not turn.tool_calls:
            result.answer = turn.text
            if turn.stop_reason == "max_tokens":
                result.answer += " [cut off: the reply hit the token limit]"
            return result
        blocks = []
        for call in turn.tool_calls:
            started = ms()
            with tracer.start_as_current_span(
                f"execute_tool {call.name}",
                attributes={
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": call.name,
                    "gen_ai.tool.call.id": call.id,
                    "gen_ai.tool.type": "function",
                    "tidewise.step": step,
                },
            ) as span:
                if tracing.capture_content():
                    span.set_attribute("gen_ai.tool.call.arguments", json.dumps(call.input))
                try:
                    output, is_error = await tools.call(call.name, call.input)
                except Exception as exc:  # bad tool name, protocol problem
                    output, is_error = f"Tool call failed: {exc}", True
                    span.record_exception(exc)
                    span.set_attribute("error.type", type(exc).__name__)
                else:
                    if is_error:
                        span.set_attribute("error.type", "tool_error")
                if is_error:
                    span.set_status(Status(StatusCode.ERROR, output[:200]))
                if tracing.capture_content():
                    span.set_attribute("gen_ai.tool.call.result", output)
            took = round(ms() - started, 1)
            result.tool_calls.append(ToolRecord(call.name, call.input, output, is_error, took))
            result.trace.append(
                {
                    "type": "tool",
                    "step": step,
                    "at_ms": started,
                    "duration_ms": took,
                    "id": call.id,
                    "name": call.name,
                    "input": call.input,
                    "output": output,
                    "is_error": is_error,
                }
            )
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": output,
                    **({"is_error": True} if is_error else {}),
                }
            )
        # All results for one assistant turn go back in a single user message.
        messages.append({"role": "user", "content": blocks})
    result.answer = f"Stopped after {MAX_STEPS} steps without a final answer."
    result.stop_reason = "max_steps"
    return result


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None
