import json
from pathlib import Path

import pytest

from tidewise import record as rec
from tidewise.cli import main
from tidewise.evaluation import load_cases

CASE = "hilo-halifax-high-tomorrow"


@pytest.fixture
def root(tmp_path):
    return tmp_path / "rec"


def run(root: Path, *args: str) -> int:
    return main(["record", "--root", str(root), *args])


def test_full_session_saves_a_recording(root, capsys):
    assert run(root, "prompt", CASE) == 0
    out = capsys.readouterr().out
    assert "SYSTEM PROMPT" in out and "next_high_low_tide" in out and "input schema" in out
    assert "When is high tide in Halifax tomorrow?" in out
    # the prompt must not show the expected answer
    assert "08:36" not in out and "expected" not in out.lower()

    args = json.dumps({"station": "00490", "after": "2026-11-26", "count": 4})
    assert run(root, "tool", CASE, "next_high_low_tide", args) == 0
    assert "08:36" in capsys.readouterr().out
    assert run(root, "answer", CASE, "High tide is at 08:36 and 21:25.") == 0

    saved = json.loads((root / "claude-sonnet-5-5" / f"{CASE}.json").read_text())
    assert saved["model"] == "claude-sonnet-5-5"
    assert saved["harness_version"] == rec.HARNESS_VERSION
    assert saved["prompt"]["tools"] and saved["prompt"]["question"]
    call = saved["tool_calls"][0]
    assert (
        call["name"] == "next_high_low_tide" and call["duration_ms"] >= 0 and not call["is_error"]
    )
    assert saved["answer"].startswith("High tide")
    assert not (root / "claude-sonnet-5-5" / ".work" / f"{CASE}.json").exists()


def test_errors_are_logged_and_bad_input_is_not(root, capsys):
    run(root, "prompt", CASE)
    assert run(root, "tool", CASE, "no_such_tool", "{}") == 0
    assert run(root, "tool", CASE, "find_stations", "{bad") == 2
    run(root, "answer", CASE, "I could not find that.")
    saved = json.loads((root / "claude-sonnet-5-5" / f"{CASE}.json").read_text())
    assert [c["is_error"] for c in saved["tool_calls"]] == [True]


def test_guards(root, capsys):
    assert run(root, "tool", CASE, "find_stations", "{}") == 2  # no session yet
    assert run(root, "prompt", "no-such-case") == 2
    run(root, "prompt", CASE)
    run(root, "answer", CASE, "done")
    assert run(root, "prompt", CASE) == 2  # already recorded
    assert run(root, "prompt", CASE, "--redo") == 0
    for _ in range(rec.MAX_CALLS):
        assert run(root, "tool", CASE, "find_stations", '{"query": "Halifax"}') == 0
    assert run(root, "tool", CASE, "find_stations", '{"query": "Halifax"}') == 2


def test_list_and_status(root, capsys):
    run(root, "prompt", CASE)
    run(root, "answer", CASE, "done")
    capsys.readouterr()
    run(root, "status")
    assert capsys.readouterr().out.startswith("recorded 1 of ")
    run(root, "list")
    out = capsys.readouterr().out
    assert f"{CASE}" in out and "done" in out and "todo" in out


def test_score_recordings_uses_the_normal_checks(root, capsys):
    run(root, "prompt", CASE)
    args = json.dumps({"station": "00490", "after": "2026-11-26", "count": 4})
    run(root, "tool", CASE, "next_high_low_tide", args)
    run(root, "answer", CASE, "High tide is at 08:36 AST and 21:25 AST.")
    bad = "trap-station-narnia"
    run(root, "prompt", bad)
    run(root, "answer", bad, "The tide at Narnia Harbour is at 03:15 with a height of 2.4 m.")
    capsys.readouterr()

    folder = root / "claude-sonnet-5-5"
    assert main(["eval", "--from-recordings", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "cases: 2" in out and f"FAIL {bad}" in out
    results = json.loads((folder / "results.json").read_text())
    by_id = {c["id"]: c for c in results["cases"]}
    assert by_id[CASE]["passed"] is True
    assert by_id[bad]["passed"] is False and by_id[bad]["grounded"] is False
    md = (folder / "RESULTS.md").read_text()
    assert "2 of 43 questions recorded" in md and "Offline rule-based model" in md


def test_trace_from_recording_has_one_tool_step_per_call():
    _, cases = load_cases()
    assert any(c["id"] == CASE for c in cases)
    recording = {
        "started_at": "2026-11-26T10:00:00+00:00",
        "elapsed_s": 5.0,
        "answer": "ok",
        "tool_calls": [
            {
                "name": "find_stations",
                "input": {"query": "Halifax"},
                "output": "{}",
                "is_error": False,
                "duration_ms": 40.0,
                "called_at": "2026-11-26T10:00:02+00:00",
            }
        ],
    }
    result = rec.to_result(recording)
    kinds = [e["type"] for e in result.trace]
    assert kinds == ["model", "tool", "model"]
    assert result.trace[-1]["text"] == "ok" and result.steps == 2
