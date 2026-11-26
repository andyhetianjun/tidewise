import json
import sys
import textwrap
import time
from datetime import UTC, datetime

from tidewise.cli import main
from tidewise.evaluation import load_cases
from tidewise.report import write_report
from tidewise.runner import ERROR, OK, TIMEOUT, TRANSIENT, run_case, run_cases
from tidewise.worker import _NETWORK_TEXT, _leaves

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)
CASE = {
    "id": "x",
    "question": "q",
    "expected_tool": None,
    "expected_args": {},
    "answer_must_include": [],
}


def script(code: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(code)]


OK_REPLY = """
import json, sys
req = json.loads(sys.stdin.read())
print(json.dumps({"status": "ok", "result": {"passed": True}, "trace": []}))
"""

FLAKY = """
import json, sys
req = json.loads(sys.stdin.read())
if req["attempt"] < 3:
    print(json.dumps({"status": "transient_error", "error": "connection reset"}))
else:
    print(json.dumps({"status": "ok", "result": {"passed": True}, "trace": []}))
"""


async def test_retries_a_transient_error_until_it_works():
    run = await run_case(
        CASE, model="fake", now=NOW.isoformat(), timeout_s=10, retries=2,
        command=script(FLAKY), backoff_s=0,
    )  # fmt: skip
    assert [a.status for a in run.attempts] == [TRANSIENT, TRANSIENT, OK]
    assert run.passed


async def test_gives_up_after_the_retry_limit():
    run = await run_case(
        CASE, model="fake", now=NOW.isoformat(), timeout_s=10, retries=1,
        command=script(FLAKY), backoff_s=0,
    )  # fmt: skip
    assert [a.status for a in run.attempts] == [TRANSIENT, TRANSIENT]
    assert run.status == TRANSIENT and not run.passed


async def test_a_hung_case_is_killed_at_the_timeout():
    started = time.perf_counter()
    run = await run_case(
        CASE, model="fake", now=NOW.isoformat(), timeout_s=0.5, retries=1,
        command=script("import time; time.sleep(60)"), backoff_s=0,
    )  # fmt: skip
    assert [a.status for a in run.attempts] == [TIMEOUT, TIMEOUT]
    assert "no reply within 0.5 s" in run.attempts[0].error
    assert time.perf_counter() - started < 5  # not 120 s: the child was killed


async def test_a_crash_is_an_error_and_is_not_retried():
    run = await run_case(
        CASE, model="fake", now=NOW.isoformat(), timeout_s=10, retries=3,
        command=script("import sys; sys.exit(3)"), backoff_s=0,
    )  # fmt: skip
    assert [a.status for a in run.attempts] == [ERROR]
    assert "code 3" in run.attempts[0].error


async def test_cases_run_in_parallel_and_keep_their_order():
    slow = script(OK_REPLY.replace("import json, sys", "import json, sys, time\ntime.sleep(0.6)"))
    cases = [{**CASE, "id": f"c{i}"} for i in range(4)]
    serial = await run_cases(cases, NOW, jobs=1, command=slow, timeout_s=10)
    parallel = await run_cases(cases, NOW, jobs=4, command=slow, timeout_s=10)
    assert [r.case["id"] for r in parallel.runs] == ["c0", "c1", "c2", "c3"]
    assert serial.wall_s >= 2.4
    assert parallel.wall_s < serial.wall_s / 2


async def test_real_worker_returns_a_scored_case_with_a_trace():
    _, cases = load_cases()
    case = next(c for c in cases if c["id"] == "hilo-halifax-high-tomorrow")
    run = await run_case(case, model="fake", now=NOW.isoformat(), timeout_s=60, retries=0)
    assert run.status == OK and run.passed
    trace = run.reply["trace"]
    assert [e["type"] for e in trace] == ["model", "tool", "model", "tool", "model"]
    tool = trace[3]
    assert tool["name"] == "next_high_low_tide"
    assert tool["input"]["station"] == "00490"
    assert "08:36" in tool["output"] and tool["duration_ms"] >= 0


async def test_cases_do_not_share_state():
    """Each case sets its own clock; a dev case then a held-out case give the same
    answers whichever order they run in."""
    _, cases = load_cases()
    picked = [c for c in cases if c["id"] in ("hilo-halifax-high-tomorrow", "trap-waves")]
    a = await run_cases(picked, NOW, jobs=1)
    b = await run_cases(picked[::-1], NOW, jobs=2)
    assert {r.case["id"]: r.passed for r in a.runs} == {r.case["id"]: r.passed for r in b.runs}


def test_leaves_unwraps_exception_groups():
    group = ExceptionGroup("g", [ValueError("a"), ExceptionGroup("h", [KeyError("b")])])
    assert [type(e) for e in _leaves(group)] == [ValueError, KeyError]


def test_network_text_in_a_tool_error_counts_as_transient():
    assert _NETWORK_TEXT.search("Could not reach the IWLS service (ConnectError). Try again.")
    assert _NETWORK_TEXT.search("MSC GeoMet is busy (HTTP 503). Try again in a moment.")
    assert not _NETWORK_TEXT.search("No stations found.")


async def test_report_files_and_marked_claims(tmp_path):
    _, cases = load_cases()
    picked = [c for c in cases if c["id"] in ("hilo-halifax-high-tomorrow", "trap-waves")]
    summary = await run_cases(picked, NOW, model="sloppy", jobs=2)
    index = write_report(summary, tmp_path)
    html = index.read_text()
    assert 'href="cases/hilo-halifax-high-tomorrow.html"' in html
    assert "deliberately careless" in html
    page = (tmp_path / "cases" / "hilo-halifax-high-tomorrow.html").read_text()
    assert "<mark>" in page  # the invented detail is highlighted
    assert "next_high_low_tide" in page and "../index.html" in page
    data = json.loads((tmp_path / "results" / "trap-waves.json").read_text())
    assert data["passed"] is False and data["trace"][0]["type"] == "model"
    assert (tmp_path / "results.json").exists() and (tmp_path / "style.css").exists()


def test_cli_run_evals(tmp_path, capsys):
    code = main(
        ["run-evals", "--only", "hilo-halifax-high", "--jobs", "1", "--out", str(tmp_path / "r")]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "all three: 1/1" in out
    assert (tmp_path / "r" / "index.html").exists()
