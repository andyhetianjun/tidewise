"""The trace viewer page for the demo site, and the data it shows.

`traces.json` holds a list of traces in OTLP JSON (the same shape the file exporter
writes). Two kinds go in:

- recorded: built from the saved Claude recordings. A recording keeps the start time of
  the run, the time each tool was called (to the second) and how long each tool took. It
  does not keep model timings or token counts, so those spans are not drawn.
- sample run: the offline model run in this process with tracing switched on, so these
  are the spans the instrumentation really emits. The offline model is not a language
  model and has no token counts.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .report import _page

RECORDED = "recorded"
SAMPLE = "sample run"


def _attr(key: str, value: Any) -> dict:
    if isinstance(value, bool):
        v: dict = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


def _hex(seed: str, n: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:n]


def _ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000_000)


def recording_to_otlp(rec: dict) -> dict:
    """OTLP JSON for one recorded run: one invoke_agent span and a span per tool call."""
    case = rec["case_id"]
    trace_id = _hex("trace:" + case, 32)
    start = datetime.fromisoformat(rec["started_at"])
    root_start = _ns(start)
    spans: list[dict] = []
    last_end = root_start + int(rec["elapsed_s"] * 1e9)
    for i, call in enumerate(rec["tool_calls"], 1):
        s = max(_ns(datetime.fromisoformat(call["called_at"])), root_start)
        e = s + int(call["duration_ms"] * 1e6)
        last_end = max(last_end, e)
        attrs = [
            _attr("gen_ai.operation.name", "execute_tool"),
            _attr("gen_ai.tool.name", call["name"]),
            _attr("gen_ai.tool.type", "function"),
            _attr("tidewise.step", i),
        ]
        span = {
            "traceId": trace_id,
            "spanId": _hex(f"{case}:{i}", 16),
            "parentSpanId": _hex("root:" + case, 16),
            "name": f"execute_tool {call['name']}",
            "kind": 1,
            "startTimeUnixNano": str(s),
            "endTimeUnixNano": str(e),
            "attributes": attrs,
            "status": {},
        }
        if call.get("is_error"):
            attrs.append(_attr("error.type", "tool_error"))
            span["status"] = {"code": 2}
        spans.append(span)
    root_attrs = [
        _attr("gen_ai.operation.name", "invoke_agent"),
        _attr("gen_ai.provider.name", "anthropic"),
        _attr("gen_ai.agent.name", "tidewise"),
        _attr("gen_ai.request.model", rec["model"]),
        _attr("tidewise.tool_calls", len(rec["tool_calls"])),
    ]
    root = {
        "traceId": trace_id,
        "spanId": _hex("root:" + case, 16),
        "name": "invoke_agent tidewise",
        "kind": 1,
        "startTimeUnixNano": str(root_start),
        "endTimeUnixNano": str(last_end),
        "attributes": root_attrs,
        "status": {},
    }
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attr("service.name", "tidewise")]},
                "scopeSpans": [{"scope": {"name": "tidewise"}, "spans": [root, *spans]}],
            }
        ]
    }


async def sample_traces(cases: list[dict], now: datetime) -> list[tuple[dict, dict]]:
    """Run cases through the offline model with tracing on and return their spans."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from . import tracing
    from .agent import McpTools, run_agent
    from .fake import RuleModel
    from .server import create_server

    saved = {k: os.environ.get(k) for k in ("TIDEWISE_OFFLINE", "TIDEWISE_NOW")}
    os.environ["TIDEWISE_OFFLINE"] = "1"
    os.environ["TIDEWISE_NOW"] = now.isoformat()
    out = []
    try:
        for case in cases:
            exporter = InMemorySpanExporter()
            tracing.setup_tracing(exporter, simple=True)
            async with McpTools(create_server()) as tools:
                await run_agent(case["question"], RuleModel(now), tools, now)
            out.append((case, tracing.spans_to_otlp_json(exporter.get_finished_spans())))
            tracing.shutdown_tracing()
    finally:
        tracing.shutdown_tracing()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return out


def build_trace_list(
    recorded: list[tuple[dict, dict]], samples: list[tuple[dict, dict]]
) -> list[dict]:
    items = []
    for case, rec in recorded:
        items.append(
            {
                "id": "rec-" + case["id"],
                "label": case["question"],
                "source": RECORDED,
                "model": rec["model_label"],
                "otlp": recording_to_otlp(rec),
            }
        )
    for case, otlp in samples:
        items.append(
            {
                "id": "sample-" + case["id"],
                "label": case["question"],
                "source": SAMPLE,
                "model": "Offline rule-based model",
                "otlp": otlp,
            }
        )
    return items


def write_traces(out: Path, items: list[dict]) -> None:
    (out / "traces.json").write_text(json.dumps(items, separators=(",", ":")))
    (out / "traces.html").write_text(render_traces_page(len(items)))


NOTE = (
    "<b>Recorded traces</b> come from the saved Claude Sonnet 5.5 runs. A recording has when each "
    "tool was called (to the nearest second) and how long it took, so tool bars start where the "
    "log says, give or take a second. The gaps between them are the model writing and the "
    "harness handing results back; the recordings do not split those, and they have no token "
    "counts, so no model spans are drawn. <b>Sample runs</b> are the offline model run with "
    "tracing on, so they show the spans the code really emits, including one per model reply. "
    "That model is not a language model and reports no tokens."
)


def render_traces_page(count: int) -> str:
    body = f"""
<p><a href="index.html">&larr; Tidewise demo</a></p>
<h1>Trace viewer</h1>
<p class="muted">Where the time goes in one answer. {count} traces, labelled by where they came from.</p>
<div class="note">{NOTE}</div>
<p><label>Trace <select id="pick"></select></label>
&nbsp; <label>or load your own <input type="file" id="file" accept=".json,.jsonl"></label></p>
<p id="src" class="muted"></p>
<div id="wf"></div>
<pre id="detail">Click a bar to see its attributes.</pre>
<p class="muted">The file the code writes is OTLP JSON, one document per line
(<code>TIDEWISE_TRACE=1 tidewise ask ...</code>). This page reads that format, so you can open your own.</p>
<style>
.row{{display:grid;grid-template-columns:minmax(9rem,16rem) 1fr 5rem;gap:.5rem;align-items:center;
padding:.15rem 0;border-bottom:1px solid var(--line);cursor:pointer;font-size:.88rem}}
.row:hover{{background:var(--card)}}.nm{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.track{{position:relative;height:1rem;background:var(--code);border-radius:3px}}
.bar{{position:absolute;top:0;bottom:0;border-radius:3px;min-width:3px;background:var(--accent)}}
.bar.chat{{background:var(--muted)}}.bar.invoke_agent{{background:var(--line)}}.bar.err{{background:var(--bad)}}
.d{{text-align:right;color:var(--muted)}}
</style>
<script>{TRACE_JS}</script>
"""
    return _page("Tidewise trace viewer", body, "style.css")


TRACE_JS = r"""
const $ = (id) => document.getElementById(id);
let traces = [];
function flat(otlp) {
  const out = [];
  for (const rs of otlp.resourceSpans || [])
    for (const ss of rs.scopeSpans || []) out.push(...(ss.spans || []));
  return out;
}
function attrs(span) {
  const o = {};
  for (const a of span.attributes || []) {
    const v = a.value; o[a.key] = v.stringValue ?? v.intValue ?? v.doubleValue ?? v.boolValue ??
      (v.arrayValue ? v.arrayValue.values.map((x) => x.stringValue ?? x.intValue).join(", ") : "");
  }
  return o;
}
function fmt(ms) { return ms >= 1000 ? (ms / 1000).toFixed(2) + " s" : ms.toFixed(1) + " ms"; }
function order(spans) {
  const kids = {}; const ids = new Set(spans.map((s) => s.spanId));
  for (const s of spans) (kids[ids.has(s.parentSpanId) ? s.parentSpanId : ""] ||= []).push(s);
  const out = [];
  const walk = (p, d) => (kids[p] || []).sort((a, b) => a.startTimeUnixNano - b.startTimeUnixNano)
    .forEach((s) => { out.push([s, d]); walk(s.spanId, d + 1); });
  walk("", 0); return out;
}
function show(otlp, label) {
  const spans = flat(otlp); const wf = $("wf"); wf.textContent = "";
  if (!spans.length) { wf.textContent = "No spans."; return; }
  const t0 = Math.min(...spans.map((s) => Number(s.startTimeUnixNano)));
  const t1 = Math.max(...spans.map((s) => Number(s.endTimeUnixNano)));
  const total = Math.max(t1 - t0, 1);
  for (const [s, depth] of order(spans)) {
    const a = attrs(s); const start = Number(s.startTimeUnixNano) - t0;
    const dur = Number(s.endTimeUnixNano) - Number(s.startTimeUnixNano);
    const row = document.createElement("div"); row.className = "row";
    const nm = document.createElement("div"); nm.className = "nm";
    nm.style.paddingLeft = depth * 1.1 + "rem"; nm.textContent = s.name; nm.title = s.name;
    const track = document.createElement("div"); track.className = "track";
    const bar = document.createElement("div");
    const op = a["gen_ai.operation.name"] || "";
    bar.className = "bar " + op + (a["error.type"] ? " err" : "");
    bar.style.left = (start / total) * 100 + "%";
    bar.style.width = (dur / total) * 100 + "%";
    track.appendChild(bar);
    const d = document.createElement("div"); d.className = "d"; d.textContent = fmt(dur / 1e6);
    row.append(nm, track, d);
    row.onclick = () => { $("detail").textContent = s.name + "\n" + fmt(dur / 1e6) + " from " +
      fmt(start / 1e6) + "\n\n" + Object.entries(a).map(([k, v]) => k + " = " + v).join("\n"); };
    wf.appendChild(row);
  }
  $("src").textContent = label;
}
function pick() {
  const t = traces[$("pick").value];
  show(t.otlp, t.source === "recorded"
    ? "Recorded from " + t.model + ". Tool spans only; model time and tokens were not kept."
    : "Sample run of the " + t.model + " with tracing on. Real spans, but not a language model.");
}
fetch("traces.json").then((r) => r.json()).then((list) => {
  traces = list;
  list.forEach((t, i) => { const o = document.createElement("option"); o.value = i;
    o.textContent = "[" + t.source + "] " + t.label; $("pick").appendChild(o); });
  $("pick").onchange = pick; if (list.length) pick();
}).catch(() => { $("wf").textContent = "Could not load traces.json (open this page through a web server)."; });
$("file").onchange = async (e) => {
  const text = await e.target.files[0].text(); const spans = [];
  const docs = text.trim().startsWith("{\"resourceSpans\"") && text.includes("\n")
    ? text.trim().split("\n").map((l) => JSON.parse(l)) : [JSON.parse(text)];
  const merged = { resourceSpans: docs.flatMap((d) => d.resourceSpans || []) };
  show(merged, "Your file: " + e.target.files[0].name);
};
"""
