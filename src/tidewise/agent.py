"""A small tool-calling loop over the MCP server's tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from mcp.client import Client
from mcp.server.mcpserver import MCPServer

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


class ModelClient(Protocol):
    def create(self, system: str, messages: list[dict], tools: list[dict]) -> Turn: ...


@dataclass
class ToolRecord:
    name: str
    input: dict[str, Any]
    output: str
    is_error: bool


@dataclass
class AgentResult:
    answer: str
    tool_calls: list[ToolRecord] = field(default_factory=list)
    steps: int = 0
    stop_reason: str = "end_turn"


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

    def __init__(self, model: str = MODEL) -> None:
        import anthropic

        self._client = anthropic.Anthropic()
        self._model = model

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
        return Turn(text, calls, resp.stop_reason or "end_turn", list(resp.content))


class McpTools:
    """Talks to the tidewise MCP server in-process, through the real MCP protocol."""

    def __init__(self, server: MCPServer) -> None:
        self._client = Client(server)
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
        result = await self._client.call_tool(name, args)
        text = "".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        return text[:MAX_TOOL_OUTPUT_CHARS], bool(result.is_error)


async def run_agent(
    question: str, model: ModelClient, tools: McpTools, now: datetime
) -> AgentResult:
    system = system_prompt(now)
    messages: list[dict] = [{"role": "user", "content": question}]
    result = AgentResult(answer="")
    for step in range(1, MAX_STEPS + 1):
        turn = model.create(system, messages, tools.specs)
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
            try:
                output, is_error = await tools.call(call.name, call.input)
            except Exception as exc:  # bad tool name, protocol problem
                output, is_error = f"Tool call failed: {exc}", True
            result.tool_calls.append(ToolRecord(call.name, call.input, output, is_error))
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
