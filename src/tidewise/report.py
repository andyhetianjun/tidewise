"""Static HTML for an eval run: an index page, one page per case with its full trace,
and the raw results as JSON. Plain files with relative links, so the folder can be
opened from disk or served from any sub-path (GitHub Pages).
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from .runner import ERROR, OK, TIMEOUT, TRANSIENT, CaseRun, RunSummary

MODEL_NOTES = {
    "fake": (
        "Answers in this report come from the offline rule-based model: a keyword matcher "
        "written for these questions, not a language model. It shows that the tools, the "
        "checks and the runner fit together. It says nothing about how well Claude does."
    ),
    "sloppy": (
        "Answers in this report come from a deliberately careless stand-in. It calls the "
        "right tools, then pads its answers with details no tool returned and never says "
        "it does not know. It is here to show the checks catching it."
    ),
    "flaky": "Answers come from the offline rule-based model, with a simulated network error on the first try of each case, to show retries.",
    "anthropic": "Answers in this report come from Claude, called through the Anthropic API.",
}

CSS = """
:root{--bg:#fbfaf7;--fg:#1d2427;--muted:#5b676c;--line:#dcd8cf;--card:#fff;--accent:#0b6e8a;
--ok:#1b6b3a;--ok-bg:#e1f1e6;--bad:#9a2a1c;--bad-bg:#f8e3df;--warn:#8a5a00;--warn-bg:#f8edcf;
--code:#f1efe8}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#13191b;--fg:#e4e8e8;
--muted:#9aa7ab;--line:#2c3739;--card:#1a2224;--accent:#6cc4dd;--ok:#7bd49a;--ok-bg:#183323;
--bad:#f09a8c;--bad-bg:#3a1f1b;--warn:#e8c46a;--warn-bg:#3a2f12;--code:#212b2d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:62rem;margin:0 auto;padding:1.5rem 16px 4rem}
a{color:var(--accent)}h1{font-size:1.7rem;margin:.2rem 0 .4rem}h2{font-size:1.2rem;margin:2rem 0 .6rem}
.muted{color:var(--muted)}.note{border-left:4px solid var(--accent);background:var(--card);
padding:.7rem 1rem;margin:1rem 0;border-radius:0 6px 6px 0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));gap:.7rem;margin:1rem 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.6rem .8rem}
.stat b{display:block;font-size:1.35rem}.stat span{font-size:.85rem;color:var(--muted)}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);font-size:.93rem}
th,td{text-align:left;padding:.45rem .6rem;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.8rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.tablewrap{overflow-x:auto}
.badge{display:inline-block;padding:0 .5rem;border-radius:99px;font-size:.8rem;font-weight:600;white-space:nowrap}
.pass{background:var(--ok-bg);color:var(--ok)}.fail{background:var(--bad-bg);color:var(--bad)}
.warn{background:var(--warn-bg);color:var(--warn)}.na{background:var(--code);color:var(--muted)}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85rem}
pre{background:var(--code);padding:.7rem .9rem;border-radius:6px;overflow-x:auto;margin:.4rem 0;white-space:pre-wrap;word-break:break-word}
.step{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.7rem 1rem;margin:.7rem 0}
.step h3{font-size:1rem;margin:0 0 .3rem}.step .t{font-weight:400;color:var(--muted);font-size:.85rem}
.step.tool{border-left:4px solid var(--accent)}.step.model{border-left:4px solid var(--muted)}
.step.err{border-left-color:var(--bad)}
details summary{cursor:pointer;color:var(--accent)}
mark{background:var(--bad-bg);color:var(--bad);padding:0 .15rem;border-radius:3px;font-weight:600}
.filters{display:flex;flex-wrap:wrap;gap:.4rem;margin:.8rem 0}
.filters button{font:inherit;font-size:.85rem;padding:.2rem .7rem;border-radius:99px;border:1px solid var(--line);
background:var(--card);color:var(--fg);cursor:pointer}.filters button[aria-pressed=true]{background:var(--accent);color:var(--bg);border-color:var(--accent)}
.answer{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.7rem 1rem}
@media (max-width:600px){main{padding-top:1rem}.stats{grid-template-columns:repeat(2,1fr)}}
"""


def _page(title: str, body: str, css: str, nav: str = "") -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><link rel='stylesheet' href='{css}'></head>"
        f"<body><main>{nav}{body}</main></body></html>"
    )


def badge(ok: bool | None, yes: str = "pass", no: str = "fail") -> str:
    if ok is None:
        return '<span class="badge na">n/a</span>'
    return f'<span class="badge {"pass" if ok else "fail"}">{yes if ok else no}</span>'


def status_badge(run: CaseRun) -> str:
    if run.status == OK:
        return badge(run.passed, "pass", "fail")
    label = {TIMEOUT: "timeout", TRANSIENT: "network error", ERROR: "error"}[run.status]
    return f'<span class="badge warn">{label}</span>'


def _json(value: object) -> str:
    return escape(json.dumps(value, indent=2, ensure_ascii=False))


def _highlight(answer: str, unsupported: list[dict]) -> str:
    text = escape(answer)
    for claim in unsupported:
        needle = escape(claim["text"])
        if needle in text and f"<mark>{needle}" not in text:
            text = text.replace(needle, f"<mark>{needle}</mark>", 1)
    return text


def _expectation(case: dict) -> str:
    rows = []
    tool = case["expected_tool"]
    rows.append(
        "<li>Call <code>"
        + escape(tool)
        + "</code> with "
        + escape(json.dumps(case["expected_args"]))
        + "</li>"
        if tool
        else "<li>Call no tool</li>"
    )
    for extra in case.get("also_expected", []):
        rows.append(
            f"<li>Also call <code>{escape(extra['tool'])}</code> with "
            f"{escape(json.dumps(extra.get('args', {})))}</li>"
        )
    for group in case["answer_must_include"]:
        rows.append(
            "<li>Answer includes one of: "
            + ", ".join(f"<code>{escape(g)}</code>" for g in group)
            + "</li>"
        )
    if case.get("expect_abstain"):
        rows.append(
            "<li>Answer says it does not know, and states nothing the tools did not return</li>"
        )
    rows.append(
        "<li>Every time, height, wind speed and place name in the answer is in a tool output</li>"
    )
    return "<ul>" + "".join(rows) + "</ul>"


def render_trace(trace: list[dict], heading: str = "h3") -> str:
    parts = []
    for ev in trace:
        if ev["type"] == "model":
            calls = "".join(
                f"<pre>{escape(c['name'])}({escape(json.dumps(c['input'], ensure_ascii=False))})</pre>"
                for c in ev["tool_calls"]
            )
            text = f"<p>{escape(ev['text'])}</p>" if ev["text"] else ""
            what = "asks for tools" if ev["tool_calls"] else "final answer"
            parts.append(
                f'<div class="step model"><{heading}>Step {ev["step"]}: model {what} '
                f'<span class="t">{ev["duration_ms"]} ms, at {ev["at_ms"]} ms</span></{heading}>'
                f"{text}{calls}</div>"
            )
        else:
            out = ev["output"]
            try:
                out = json.dumps(json.loads(out), indent=2, ensure_ascii=False)
            except ValueError:
                pass
            err = " err" if ev["is_error"] else ""
            flag = ' <span class="badge fail">error returned</span>' if ev["is_error"] else ""
            body = f"<pre>{escape(out)}</pre>"
            if len(out) > 700:
                body = (
                    f"<details><summary>Result ({len(out):,} characters)</summary>{body}</details>"
                )
            parts.append(
                f'<div class="step tool{err}"><{heading}>Tool: <code>{escape(ev["name"])}</code>{flag} '
                f'<span class="t">{ev["duration_ms"]} ms, at {ev["at_ms"]} ms</span></{heading}>'
                f"<div>Arguments</div><pre>{escape(json.dumps(ev['input'], ensure_ascii=False))}</pre>"
                f"<div>Result</div>{body}</div>"
            )
    return "".join(parts)


def render_case_page(run: CaseRun, summary: RunSummary, note: str) -> str:
    case, res = run.case, run.result
    nav = '<p><a href="../index.html">&larr; All cases</a></p>'
    head = (
        f"<h1>{escape(case['id'])} {status_badge(run)}</h1>"
        f'<p class="muted">{escape(case.get("category", "tide"))} case'
        f"{', held out' if case.get('split') == 'heldout' else ''}. "
        f"{escape(case.get('note') or '')}</p>"
        f'<div class="note">{escape(note)}</div>'
        f"<h2>Question</h2><p><b>{escape(case['question'])}</b></p>"
    )
    body = head
    if res is None:
        body += f"<h2>What happened</h2><p>The case did not finish: {escape(run.attempts[-1].error or '')}</p>"
    else:
        checks = (
            "<tr><td>Tool selection</td><td>"
            + badge(res["tool_ok"])
            + "</td><td>"
            + escape(", ".join(res["tools_called"]) or "no tools called")
            + "</td></tr>"
            "<tr><td>Answer</td><td>"
            + badge(res["answer_ok"])
            + "</td><td>"
            + (
                escape("missing: " + json.dumps(res["missing"]))
                if res["missing"]
                else "as expected"
            )
            + "</td></tr>"
            "<tr><td>Grounded</td><td>"
            + badge(res["grounded"])
            + "</td><td>"
            + (
                f"{len(res['unsupported'])} unsupported claim(s), marked in the answer"
                if res["unsupported"]
                else f"all {len(res['claims'])} checkable claim(s) found in tool output"
            )
            + "</td></tr>"
        )
        claims = "".join(
            f"<tr><td>{escape(c['kind'])}</td><td><code>{escape(c['text'])}</code></td>"
            f"<td>{badge(c['supported'], 'supported', 'unsupported')}</td><td>{escape(c['support'])}</td></tr>"
            for c in res["claims"]
        )
        body += (
            f"<h2>Answer</h2><div class='answer'>{_highlight(res['answer'], res['unsupported'])}</div>"
            f"<h2>What was expected</h2>{_expectation(case)}"
            f"<h2>Checks</h2><div class='tablewrap'><table><tr><th>Check</th><th>Result</th><th>Detail</th></tr>{checks}</table></div>"
            "<h2>Claims found in the answer</h2>"
            + (
                f"<div class='tablewrap'><table><tr><th>Kind</th><th>Claim</th><th></th><th>Where</th></tr>{claims}</table></div>"
                if claims
                else "<p class='muted'>No times, dates, numbers with units, codes or place names to check.</p>"
            )
            + f"<h2>Trace</h2>{render_trace(run.reply.get('trace', []))}"
        )
    attempts = "".join(
        f"<tr><td>{a.number}</td><td>{escape(a.status)}</td><td>{a.duration_s:.2f} s</td><td>{escape(a.error or '')}</td></tr>"
        for a in run.attempts
    )
    body += (
        "<h2>Attempts</h2><div class='tablewrap'><table><tr><th>#</th><th>Outcome</th><th>Time</th><th>Error</th></tr>"
        f"{attempts}</table></div>"
        f"<p class='muted'>Each attempt ran in its own process, with a {summary.timeout_s:g} s limit "
        f"and up to {summary.retries} retries for network errors and timeouts. "
        f'<a href="../results/{escape(case["id"])}.json">Raw JSON</a></p>'
    )
    return _page(f"{case['id']} - Tidewise eval", body, "../style.css", nav)


def render_index(summary: RunSummary, title: str, extra_nav: str = "") -> str:
    tool, n = summary.rate("tool_ok")
    ans, _ = summary.rate("answer_ok")
    gro, _ = summary.rate("grounded")
    traps = [r for r in summary.runs if r.case.get("expect_abstain")]
    trap_ok = sum(bool(r.result and r.result["answer_ok"] and r.result["grounded"]) for r in traps)
    stats = [
        (f"{summary.passed}/{n}", "pass all three checks"),
        (f"{tool}/{n}", "right tools"),
        (f"{ans}/{n}", "answer as expected"),
        (f"{gro}/{n}", "grounded"),
        (f"{trap_ok}/{len(traps)}", "trap cases handled"),
        (f"{summary.wall_s:g} s", f"wall time, {summary.jobs} at a time"),
        (f"{summary.busy_s:g} s", "time if run one by one"),
        (f"{summary.retried}", "cases that needed a retry"),
    ]
    stat_html = "".join(
        f'<div class="stat"><b>{v}</b><span>{escape(k)}</span></div>' for v, k in stats
    )
    rows = []
    for r in summary.runs:
        res = r.result
        cells = (
            [badge(res["tool_ok"]), badge(res["answer_ok"]), badge(res["grounded"])]
            if res
            else ["", "", ""]
        )
        trap = "trap" if r.case.get("expect_abstain") else r.case.get("category", "tide")
        state = "pass" if r.passed else ("fail" if res else "broken")
        rows.append(
            f'<tr data-cat="{escape(trap)}" data-state="{state}">'
            f'<td><a href="cases/{escape(r.case["id"])}.html">{escape(r.case["id"])}</a></td>'
            f"<td>{escape(r.case.get('category', 'tide'))}</td><td>{escape(r.case['question'])}</td>"
            f"<td>{status_badge(r)}</td><td>{cells[0]}</td><td>{cells[1]}</td><td>{cells[2]}</td>"
            f"<td>{len(r.attempts)}</td><td>{r.duration_s:.2f} s</td></tr>"
        )
    note = MODEL_NOTES.get(summary.model, "")
    body = (
        f"<h1>{escape(title)}</h1>"
        f'<p class="muted">Run at {escape(summary.started_at)}. Model: <code>{escape(summary.model)}</code>. '
        f"Each case ran in its own process (limit {summary.timeout_s:g} s, up to {summary.retries} retries). "
        f'Raw results: <a href="results.json">results.json</a>.</p>'
        f'<div class="note">{escape(note)}</div>'
        f'<div class="stats">{stat_html}</div>'
        "<h2>Cases</h2>"
        '<div class="filters" id="filters" hidden>'
        + "".join(
            f'<button type="button" data-f="{k}" aria-pressed="{"true" if k == "all" else "false"}">{lab}</button>'
            for k, lab in [
                ("all", "All"),
                ("fail", "Failed"),
                ("trap", "Traps"),
                ("combined", "Tide + weather"),
                ("weather", "Weather"),
                ("tide", "Tide"),
            ]
        )
        + "</div>"
        '<div class="tablewrap"><table id="cases"><tr><th>Case</th><th>Kind</th><th>Question</th><th>Result</th>'
        "<th>Tools</th><th>Answer</th><th>Grounded</th><th>Tries</th><th>Time</th></tr>"
        + "".join(rows)
        + "</table></div>"
        "<script>(function(){var f=document.getElementById('filters');f.hidden=false;"
        "var rows=document.querySelectorAll('#cases tr[data-cat]');"
        "f.addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;"
        "f.querySelectorAll('button').forEach(function(x){x.setAttribute('aria-pressed',x===b)});"
        "rows.forEach(function(r){var k=b.dataset.f;r.hidden=!(k==='all'||(k==='fail'&&r.dataset.state!=='pass')||r.dataset.cat===k)})})})();</script>"
    )
    return _page(title, body, "style.css", extra_nav)


def write_report(
    summary: RunSummary, out_dir: Path, title: str = "Tidewise eval report", extra_nav: str = ""
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cases").mkdir(exist_ok=True)
    (out_dir / "results").mkdir(exist_ok=True)
    (out_dir / "style.css").write_text(CSS)
    note = MODEL_NOTES.get(summary.model, "")
    for run in summary.runs:
        (out_dir / "cases" / f"{run.case['id']}.html").write_text(
            render_case_page(run, summary, note)
        )
        (out_dir / "results" / f"{run.case['id']}.json").write_text(
            json.dumps(run.to_dict(), indent=1, ensure_ascii=False)
        )
    (out_dir / "results.json").write_text(
        json.dumps(
            {
                "model": summary.model,
                "jobs": summary.jobs,
                "timeout_s": summary.timeout_s,
                "retries": summary.retries,
                "started_at": summary.started_at,
                "wall_s": summary.wall_s,
                "busy_s": summary.busy_s,
                "cases": [
                    {
                        "id": r.case["id"],
                        "status": r.status,
                        "passed": r.passed,
                        "attempts": len(r.attempts),
                        "duration_s": r.duration_s,
                    }
                    for r in summary.runs
                ],
            },
            indent=1,
        )
    )
    index = out_dir / "index.html"
    index.write_text(render_index(summary, title, extra_nav))
    return index
