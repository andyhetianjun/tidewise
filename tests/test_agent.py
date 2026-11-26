from datetime import UTC, datetime
from types import SimpleNamespace

from tidewise.agent import AnthropicModel, McpTools, ToolCall, Turn, run_agent, system_prompt
from tidewise.fake import ScriptedModel
from tidewise.fixtures import FixtureSource
from tidewise.server import create_server

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)


def tool_turn(*calls: ToolCall) -> Turn:
    content = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
    return Turn("", list(calls), "tool_use", content)


def text_turn(text: str, stop: str = "end_turn") -> Turn:
    return Turn(text, [], stop, [{"type": "text", "text": text}])


async def run(turns, question="q"):
    model = ScriptedModel(turns)
    async with McpTools(create_server(FixtureSource())) as tools:
        return model, await run_agent(question, model, tools, NOW)


async def test_loop_runs_tools_and_returns_final_text():
    model, result = await run(
        [
            tool_turn(ToolCall("t1", "find_stations", {"query": "Halifax"})),
            text_turn("Halifax is station 00490."),
        ]
    )
    assert result.answer == "Halifax is station 00490."
    assert [c.name for c in result.tool_calls] == ["find_stations"]
    assert result.steps == 2
    # the second request carries the tool result back, linked by id
    last = model.seen[1][-1]
    assert last["role"] == "user"
    assert last["content"][0]["tool_use_id"] == "t1"
    assert "00490" in last["content"][0]["content"]


async def test_parallel_calls_come_back_in_one_user_message():
    model, result = await run(
        [
            tool_turn(
                ToolCall("a", "find_stations", {"query": "Halifax"}),
                ToolCall("b", "find_stations", {"query": "Vancouver"}),
            ),
            text_turn("done"),
        ]
    )
    results = model.seen[1][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert len(result.tool_calls) == 2


async def test_tool_errors_are_flagged_and_loop_continues():
    model, result = await run(
        [
            tool_turn(ToolCall("t1", "find_stations", {"query": "Atlantis"})),
            text_turn("There is no station called Atlantis."),
        ]
    )
    block = model.seen[1][-1]["content"][0]
    assert block["is_error"] is True
    assert result.tool_calls[0].is_error
    assert "Atlantis" in result.answer


async def test_unknown_tool_does_not_crash():
    _, result = await run(
        [tool_turn(ToolCall("t1", "make_coffee", {})), text_turn("Sorry, I cannot do that.")]
    )
    assert result.tool_calls[0].is_error
    assert result.answer.startswith("Sorry")


async def test_stops_after_max_steps():
    forever = [
        tool_turn(ToolCall(f"t{i}", "find_stations", {"query": "Halifax"})) for i in range(20)
    ]
    _, result = await run(forever)
    assert result.stop_reason == "max_steps"
    assert result.steps == 8


async def test_refusal_and_max_tokens_are_reported():
    _, refused = await run([text_turn("", stop="refusal")])
    assert "declined" in refused.answer
    _, cut = await run([text_turn("Partial", stop="max_tokens")])
    assert "cut off" in cut.answer


def test_system_prompt_has_the_date():
    assert "2026-11-25 12:00 UTC" in system_prompt(NOW)


def test_anthropic_model_maps_response_and_request():
    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            blocks = [
                SimpleNamespace(type="text", text="Checking."),
                SimpleNamespace(
                    type="tool_use", id="toolu_1", name="find_stations", input={"query": "x"}
                ),
            ]
            return SimpleNamespace(content=blocks, stop_reason="tool_use")

    model = AnthropicModel.__new__(AnthropicModel)
    model._client = SimpleNamespace(messages=FakeMessages())
    model._model = "claude-sonnet-5-5"
    tools = [{"name": "find_stations", "description": "d", "input_schema": {"type": "object"}}]
    turn = model.create("sys", [{"role": "user", "content": "hi"}], tools)
    assert captured["model"] == "claude-sonnet-5-5"
    assert captured["tools"] == tools and captured["system"] == "sys"
    assert turn.tool_calls == [ToolCall("toolu_1", "find_stations", {"query": "x"})]
    assert turn.text == "Checking." and turn.stop_reason == "tool_use"
