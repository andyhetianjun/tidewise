import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from tidewise import tracing
from tidewise.agent import McpTools, ToolCall, Turn, run_agent
from tidewise.fake import ScriptedModel
from tidewise.server import create_server
from tidewise.traceview import recording_to_otlp

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def exporter():
    exp = InMemorySpanExporter()
    tracing.setup_tracing(exp, simple=True)
    yield exp
    tracing.shutdown_tracing()


def tool_turn(call: ToolCall, usage=None) -> Turn:
    content = [{"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}]
    return Turn("", [call], "tool_use", content, usage=usage)


def end_turn(text: str, usage=None) -> Turn:
    return Turn(text, [], "end_turn", [{"type": "text", "text": text}], usage=usage)


async def run(turns, question="When is high tide in Halifax?"):
    async with McpTools(create_server()) as tools:
        return await run_agent(question, ScriptedModel(turns), tools, NOW)


def by_op(spans, op):
    return [s for s in spans if s.attributes["gen_ai.operation.name"] == op]


async def test_span_tree_and_attributes(exporter):
    call = ToolCall("toolu_1", "find_stations", {"query": "Halifax"})
    await run(
        [
            tool_turn(call, {"input_tokens": 100, "output_tokens": 20}),
            end_turn("Halifax station found.", {"input_tokens": 300, "output_tokens": 30}),
        ]
    )
    spans = exporter.get_finished_spans()
    (root,) = by_op(spans, "invoke_agent")
    chats = by_op(spans, "chat")
    (tool,) = by_op(spans, "execute_tool")
    assert root.name == "invoke_agent tidewise" and root.parent is None
    assert len(chats) == 2 and all(c.parent.span_id == root.context.span_id for c in chats)
    assert tool.parent.span_id == root.context.span_id
    assert len({s.context.trace_id for s in spans}) == 1
    assert all(c.kind == SpanKind.CLIENT for c in chats)
    # the names follow "<operation> <target>"
    assert tool.name == "execute_tool find_stations"
    assert tool.attributes["gen_ai.tool.name"] == "find_stations"
    assert tool.attributes["gen_ai.tool.call.id"] == "toolu_1"
    assert tool.attributes["gen_ai.tool.type"] == "function"
    # tokens where the model reported them, summed on the agent span
    assert chats[0].attributes["gen_ai.usage.input_tokens"] == 100
    assert chats[1].attributes["gen_ai.usage.output_tokens"] == 30
    assert chats[0].attributes["gen_ai.response.finish_reasons"] == ("tool_use",)
    assert root.attributes["gen_ai.usage.input_tokens"] == 400
    assert root.attributes["gen_ai.usage.output_tokens"] == 50
    assert root.attributes["tidewise.tool_calls"] == 1 and root.attributes["tidewise.steps"] == 2
    assert root.attributes["gen_ai.provider.name"] == "tidewise.offline"


async def test_no_token_attributes_when_the_model_reports_none(exporter):
    await run([end_turn("I do not know.")])
    spans = exporter.get_finished_spans()
    assert not any("gen_ai.usage.input_tokens" in s.attributes for s in spans)


async def test_content_is_not_recorded_by_default(exporter):
    call = ToolCall("t1", "find_stations", {"query": "Halifax"})
    await run([tool_turn(call), end_turn("Done.")], question="secret question text")
    for span in exporter.get_finished_spans():
        assert "secret question text" not in json.dumps(dict(span.attributes))
        assert "gen_ai.tool.call.arguments" not in span.attributes


async def test_content_is_recorded_when_asked(exporter, monkeypatch):
    monkeypatch.setenv("TIDEWISE_TRACE_CONTENT", "1")
    call = ToolCall("t1", "find_stations", {"query": "Halifax"})
    await run([tool_turn(call), end_turn("Done.")])
    (tool,) = by_op(exporter.get_finished_spans(), "execute_tool")
    assert json.loads(tool.attributes["gen_ai.tool.call.arguments"]) == {"query": "Halifax"}
    assert "Halifax" in tool.attributes["gen_ai.tool.call.result"]


async def test_tool_errors_are_recorded(exporter):
    call = ToolCall("t1", "get_tide_predictions", {"station": "nowhere", "start": "x", "end": "y"})
    await run([tool_turn(call), end_turn("It failed.")])
    (tool,) = by_op(exporter.get_finished_spans(), "execute_tool")
    assert tool.status.status_code == StatusCode.ERROR
    assert tool.attributes["error.type"]


async def test_unknown_tool_is_recorded_as_an_error(exporter):
    call = ToolCall("t1", "no_such_tool", {})
    await run([tool_turn(call), end_turn("Sorry.")])
    (tool,) = by_op(exporter.get_finished_spans(), "execute_tool")
    assert tool.status.status_code == StatusCode.ERROR and "error.type" in tool.attributes


async def test_model_failure_is_recorded_and_reraised(exporter):
    class Broken:
        def create(self, system, messages, tools):
            raise ConnectionError("no network")

    async with McpTools(create_server()) as tools:
        with pytest.raises(ConnectionError):
            await run_agent("q", Broken(), tools, NOW)
    spans = exporter.get_finished_spans()
    (chat,) = by_op(spans, "chat")
    (root,) = by_op(spans, "invoke_agent")
    assert chat.attributes["error.type"] == "ConnectionError"
    assert chat.status.status_code == StatusCode.ERROR
    assert root.attributes["error.type"] == "ConnectionError"
    assert root.status.status_code == StatusCode.ERROR


async def test_step_limit_marks_the_agent_span_as_an_error(exporter):
    call = ToolCall("t1", "find_stations", {"query": "Halifax"})
    await run([tool_turn(call) for _ in range(8)])
    (root,) = by_op(exporter.get_finished_spans(), "invoke_agent")
    assert root.attributes["error.type"] == "max_steps"
    assert root.status.status_code == StatusCode.ERROR


async def test_off_by_default_emits_nothing():
    tracing.shutdown_tracing()
    assert isinstance(tracing.get_tracer(), type(tracing.trace.NoOpTracer()))
    result = await run([end_turn("hi")])
    assert result.answer == "hi"


async def test_file_exporter_writes_otlp_json(tmp_path, monkeypatch):
    out = tmp_path / "t" / "spans.jsonl"
    monkeypatch.setenv("TIDEWISE_TRACE_FILE", str(out))
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    tracing.setup_tracing(simple=True)
    call = ToolCall("t1", "find_stations", {"query": "Halifax"})
    await run([tool_turn(call), end_turn("ok")])
    tracing.shutdown_tracing()
    docs = [json.loads(line) for line in out.read_text().splitlines()]
    spans = [
        s for d in docs for rs in d["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]
    ]
    assert len(spans) == 4
    assert all(len(s["traceId"]) == 32 and len(s["spanId"]) == 16 for s in spans)
    assert all(int(s["endTimeUnixNano"]) >= int(s["startTimeUnixNano"]) for s in spans)
    res = docs[0]["resourceSpans"][0]["resource"]["attributes"]
    assert {"key": "service.name", "value": {"stringValue": "tidewise"}} in res
    tool = next(s for s in spans if s["name"] == "execute_tool find_stations")
    attrs = {a["key"]: a["value"] for a in tool["attributes"]}
    assert attrs["gen_ai.tool.name"] == {"stringValue": "find_stations"}


def test_endpoint_env_var_selects_the_otlp_http_exporter(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.delenv("TIDEWISE_TRACE_FILE", raising=False)
    provider = tracing.setup_tracing()
    try:
        names = [
            type(p.span_exporter).__name__ for p in provider._active_span_processor._span_processors
        ]
        assert names == ["OTLPSpanExporter"]
        exporter = provider._active_span_processor._span_processors[0].span_exporter
        assert exporter._endpoint == "http://localhost:4318/v1/traces"
    finally:
        tracing.shutdown_tracing()


def test_trace_env_switch(monkeypatch):
    monkeypatch.delenv("TIDEWISE_TRACE", raising=False)
    assert tracing.setup_from_env() is False
    monkeypatch.setenv("TIDEWISE_TRACE", "1")
    monkeypatch.setenv("TIDEWISE_TRACE_FILE", "/dev/null")
    try:
        assert tracing.setup_from_env() is True
    finally:
        tracing.shutdown_tracing()


def test_compose_file_is_valid_and_exposes_the_ports():
    doc = yaml.safe_load((ROOT / "docker-compose.tracing.yml").read_text())
    jaeger = doc["services"]["jaeger"]
    assert jaeger["image"].startswith("jaegertracing/")
    assert "4318:4318" in jaeger["ports"] and "16686:16686" in jaeger["ports"]


def test_recorded_run_becomes_otlp_spans():
    rec = {
        "case_id": "x",
        "model": "claude-sonnet-5-5",
        "started_at": "2026-11-26T02:46:45+00:00",
        "elapsed_s": 7.9,
        "tool_calls": [
            {
                "name": "find_stations",
                "called_at": "2026-11-26T02:46:47+00:00",
                "duration_ms": 14.7,
                "is_error": False,
            },
            {
                "name": "get_marine_forecast",
                "called_at": "2026-11-26T02:46:48+00:00",
                "duration_ms": 14.3,
                "is_error": True,
            },
        ],
    }
    spans = recording_to_otlp(rec)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root, first, second = spans
    assert root["name"] == "invoke_agent tidewise" and "parentSpanId" not in root
    assert first["parentSpanId"] == root["spanId"] == second["parentSpanId"]
    assert int(first["startTimeUnixNano"]) - int(root["startTimeUnixNano"]) == 2_000_000_000
    assert int(first["endTimeUnixNano"]) - int(first["startTimeUnixNano"]) == 14_700_000
    assert second["status"] == {"code": 2}
    assert int(root["endTimeUnixNano"]) - int(root["startTimeUnixNano"]) == 7_900_000_000
