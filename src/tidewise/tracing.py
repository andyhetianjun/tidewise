"""OpenTelemetry tracing for the agent loop, using the GenAI semantic conventions.

Tracing is off unless you ask for it, and then the spans go to a local file
(one OTLP JSON document per line) so nothing outside this machine is needed.
Set OTEL_EXPORTER_OTLP_ENDPOINT to send them to a collector or Jaeger instead.

    TIDEWISE_TRACE=1                       turn it on (or pass --trace to the CLI)
    TIDEWISE_TRACE_FILE=path.jsonl         where the file goes (default traces/tidewise.otlp.jsonl)
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   send to an OTLP/HTTP endpoint

The span names and attributes follow the GenAI conventions:
https://opentelemetry.io/docs/specs/semconv/gen-ai/
Prompt and answer text is not recorded unless TIDEWISE_TRACE_CONTENT=1.
"""

from __future__ import annotations

import atexit
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from google.protobuf.json_format import MessageToDict
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common._internal.trace_encoder import encode_spans
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

DEFAULT_FILE = "traces/tidewise.otlp.jsonl"
SCOPE = "tidewise"

_provider: TracerProvider | None = None


def spans_to_otlp_json(spans: Sequence[ReadableSpan]) -> dict[str, Any]:
    """The spans as an OTLP JSON `ExportTraceServiceRequest` (ids as hex, enums as integers)."""
    doc = MessageToDict(encode_spans(spans), use_integers_for_enums=True)
    for rs in doc.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                for key in ("traceId", "spanId", "parentSpanId"):
                    if span.get(key):
                        span[key] = _b64_to_hex(span[key])
    return doc


def _b64_to_hex(value: str) -> str:
    import base64

    return base64.b64decode(value).hex()


class OtlpJsonFileExporter(SpanExporter):
    """Appends one OTLP JSON document per export to a file (the collector's file format)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        line = json.dumps(spans_to_otlp_json(spans), separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def enabled_by_env() -> bool:
    return os.environ.get("TIDEWISE_TRACE", "").lower() in {"1", "true", "yes"}


def capture_content() -> bool:
    return os.environ.get("TIDEWISE_TRACE_CONTENT", "").lower() in {"1", "true", "yes"}


def _endpoint_set() -> bool:
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    )


def setup_tracing(exporter: SpanExporter | None = None, *, simple: bool = False) -> TracerProvider:
    """Start tracing. Pass an exporter to use your own (the tests use an in-memory one)."""
    global _provider
    shutdown_tracing()
    provider = TracerProvider(resource=Resource.create({"service.name": "tidewise"}))
    exporters: list[SpanExporter] = []
    if exporter is not None:
        exporters.append(exporter)
    else:
        file_chosen = bool(os.environ.get("TIDEWISE_TRACE_FILE"))
        if _endpoint_set():
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporters.append(OTLPSpanExporter())  # reads the standard OTEL_EXPORTER_OTLP_* vars
        if file_chosen or not exporters:
            exporters.append(
                OtlpJsonFileExporter(os.environ.get("TIDEWISE_TRACE_FILE", DEFAULT_FILE))
            )
    for exp in exporters:
        provider.add_span_processor(SimpleSpanProcessor(exp) if simple else BatchSpanProcessor(exp))
    _provider = provider
    atexit.register(shutdown_tracing)
    return provider


def setup_from_env() -> bool:
    if enabled_by_env() and _provider is None:
        setup_tracing()
    return _provider is not None


def shutdown_tracing() -> None:
    """Flush and stop. Safe to call twice."""
    global _provider
    if _provider is not None:
        provider, _provider = _provider, None
        provider.shutdown()


def get_tracer() -> trace.Tracer:
    """The tracer to use; without setup_tracing() it is the no-op tracer."""
    if _provider is None:
        return trace.NoOpTracer()
    return _provider.get_tracer(SCOPE)
