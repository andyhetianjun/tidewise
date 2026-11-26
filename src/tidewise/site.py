"""Build the static demo site (for GitHub Pages) from real runs.

Everything on the site comes from running the eval cases through the runner while
the site is built: the two reports, and the example conversations. Nothing is typed
in by hand except the explanations around it. No server and no API key are needed
to view it or to build it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from .evaluation import DEFAULT_CASES, load_cases
from .report import CSS, MODEL_NOTES, _page, render_trace, write_report
from .runner import CaseRun, RunSummary, run_cases

REPO = "https://github.com/andyhetianjun/tidewise"

# Questions shown as recorded conversations, in this order.
CONVERSATIONS = [
    ("hilo-halifax-high-tomorrow", "A plain tide question"),
    ("kayak-halifax-morning", "Tide and weather together: a good morning"),
    ("kayak-vancouver-morning", "Tide and weather together: a morning with a storm warning"),
    ("compare-saintjohn", "Measured against predicted water level"),
    ("stations-coords-halifax", "Finding the nearest station to a point"),
    ("trap-station-narnia", "A station that does not exist"),
    ("trap-hilo-far-future", "A date outside the data"),
    ("trap-waves", "A question no tool can answer"),
]

BACK = '<p><a href="../index.html">&larr; Tidewise demo</a></p>'


def _claims_line(run: CaseRun) -> str:
    res = run.result
    if not res:
        return ""
    n = len(res["claims"])
    if res["grounded"]:
        text = (
            f"Grounding check: all {n} checkable claim(s) in this answer appear in the tool output."
            if n
            else "Grounding check: the answer makes no time, number or place claims to check."
        )
        return f'<p><span class="badge pass">grounded</span> {escape(text)}</p>'
    return '<p><span class="badge fail">not grounded</span></p>'


def render_conversations(summary: RunSummary, stamp: str) -> str:
    by_id = {r.case["id"]: r for r in summary.runs}
    toc, blocks = [], []
    for i, (case_id, label) in enumerate(CONVERSATIONS, 1):
        run = by_id[case_id]
        res = run.result
        if res is None:
            continue
        toc.append(f'<li><a href="#c{i}">{escape(label)}</a></li>')
        blocks.append(
            f'<section id="c{i}"><h2>{i}. {escape(label)}</h2>'
            f'<div class="step"><h3>You</h3><p><b>{escape(run.case["question"])}</b></p></div>'
            f"{render_trace(run.reply['trace'])}"
            f"<h3>Check on the final answer</h3>{_claims_line(run)}"
            f'<p class="muted">Full case page with the checks: '
            f'<a href="eval/cases/{escape(case_id)}.html">{escape(case_id)}</a></p></section>'
        )
    body = (
        '<p><a href="index.html">&larr; Tidewise demo</a></p>'
        "<h1>Recorded example conversations</h1>"
        f'<div class="note"><b>Recorded from the offline rule-based model, not a language model.</b> '
        f"It is a keyword matcher that picks tools and fills in sentence templates. These "
        f"conversations were produced when this site was built ({escape(stamp)}), against the "
        f"bundled sample of real tide and weather data, with the clock set to "
        f"2026-11-25 12:00 UTC so that &ldquo;tomorrow&rdquo; has data behind it. "
        f"The tool calls and tool results below are exactly what the server returned. "
        f"A real model would word its answers differently.</div>"
        f"<p>Each conversation shows the question, then every step: what the model asked for, "
        f"the exact arguments, what the tool sent back, and how long it took.</p>"
        f"<ol>{''.join(toc)}</ol>" + "".join(blocks)
    )
    return _page("Recorded conversations - Tidewise", body, "style.css")


def render_home(
    fake: RunSummary, sloppy: RunSummary, bench: tuple[float, float, int] | None, stamp: str
) -> str:
    def row(label: str, s: RunSummary) -> str:
        tool, n = s.rate("tool_ok")
        ans, _ = s.rate("answer_ok")
        gro, _ = s.rate("grounded")
        traps = [r for r in s.runs if r.case.get("expect_abstain")]
        trap_ok = sum(
            bool(r.result and r.result["answer_ok"] and r.result["grounded"]) for r in traps
        )
        return (
            f"<tr><td>{label}</td><td>{tool}/{n}</td><td>{ans}/{n}</td><td>{gro}/{n}</td>"
            f"<td>{trap_ok}/{len(traps)}</td><td>{s.passed}/{n}</td></tr>"
        )

    bench_html = ""
    if bench:
        serial, parallel, jobs = bench
        bench_html = (
            f"<p>Running all {fake.n} cases one at a time took <b>{serial:g} s</b> in this build. "
            f"Running {jobs} at a time took <b>{parallel:g} s</b>. Each case is its own process, "
            "so on this offline set most of the time is starting Python, which is what "
            "running in parallel hides.</p>"
        )
    body = f"""
<h1>Tidewise demo</h1>
<p class="muted">Tide and marine weather for Canadian stations, as tools an AI assistant can call, and a way to check what it says.</p>
<div class="note"><b>What you are looking at.</b> A static site built from real runs of the project's
tests. It needs no server and no API key. The answers come from the offline rule-based model
(a keyword matcher, not a language model), run against a small sample of real data from
Fisheries and Oceans Canada (tides) and Environment and Climate Change Canada (weather).
Built {escape(stamp)}.</div>

<h2>What is in it</h2>
<ul>
<li><a href="conversations.html"><b>Recorded conversations</b></a>: eight questions, each shown step by step with the exact tool calls, arguments and results.</li>
<li><a href="eval/index.html"><b>Eval report, offline model</b></a>: {fake.n} questions, each run in its own process. Click any case for its full trace and the grounding check.</li>
<li><a href="eval-sloppy/index.html"><b>Eval report, sloppy model</b></a>: the same questions answered by a model that pads answers with made-up details. The grounding check marks every invented time, height and wind speed.</li>
</ul>

<h2>Measured scores</h2>
<div class="tablewrap"><table>
<tr><th>Model</th><th>Right tools</th><th>Answer as expected</th><th>Grounded</th><th>Trap cases handled</th><th>All three</th></tr>
{row("Offline rule-based model", fake)}
{row("Deliberately sloppy model", sloppy)}
</table></div>
<p class="muted">Grounded means every time, date, height, wind speed and place name in the answer is found
in a tool result. A trap case is a question with no real answer (a station that does not exist, a
date outside the data, a quantity no tool provides); the right behaviour is to say so.
The rule-based model was written against most of these questions, so its scores show that the
pieces fit together, not how good a language model would be. The held-out questions were written
after it was finished and are marked on the report.</p>
{bench_html}
<h2>The tools</h2>
<ul>
<li><code>find_stations</code>, <code>next_high_low_tide</code>, <code>get_tide_predictions</code>, <code>get_water_levels</code>, <code>compare_observed_vs_predicted</code>: tides and water levels.</li>
<li><code>get_marine_forecast</code>, <code>get_hourly_weather</code>: marine warnings and hourly wind, temperature and rain chance.</li>
</ul>
<p>Code, docs and how to run it yourself: <a href="{REPO}">{REPO}</a></p>
"""
    return _page("Tidewise demo", body, "style.css")


async def build_site(
    out: Path, *, jobs: int = 8, bench: bool = False, cases_path: Path = DEFAULT_CASES
) -> None:
    now, cases = load_cases(cases_path)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out.mkdir(parents=True, exist_ok=True)
    (out / "style.css").write_text(CSS)
    (out / ".nojekyll").write_text("")

    bench_result = None
    if bench:
        serial = await run_cases(cases, now, jobs=1)
        parallel = await run_cases(cases, now, jobs=jobs)
        bench_result = (serial.wall_s, parallel.wall_s, jobs)
        fake = parallel
    else:
        fake = await run_cases(cases, now, jobs=jobs)
    sloppy = await run_cases(cases, now, model="sloppy", jobs=jobs)

    write_report(fake, out / "eval", "Eval report: offline model", BACK)
    write_report(sloppy, out / "eval-sloppy", "Eval report: deliberately sloppy model", BACK)
    (out / "conversations.html").write_text(render_conversations(fake, stamp))
    (out / "index.html").write_text(render_home(fake, sloppy, bench_result, stamp))
    (out / "site.json").write_text(
        json.dumps(
            {
                "built": stamp,
                "models": {"fake": MODEL_NOTES["fake"], "sloppy": MODEL_NOTES["sloppy"]},
                "wall_s": {"offline": fake.wall_s, "sloppy": sloppy.wall_s},
                "bench": bench_result,
            },
            indent=1,
        )
    )
