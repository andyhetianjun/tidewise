from tests.test_agent import text_turn, tool_turn
from tidewise.agent import AgentResult, McpTools, ToolCall, ToolRecord
from tidewise.cli import main
from tidewise.evaluation import (
    DEFAULT_CASES,
    check_answer,
    check_tools,
    load_cases,
    run_eval,
)
from tidewise.fake import RuleModel, ScriptedModel, SloppyModel
from tidewise.fixtures import FixtureSource
from tidewise.server import create_server


def test_case_file_is_well_formed():
    now, cases = load_cases(DEFAULT_CASES)
    assert len(cases) >= 20
    assert len({c["id"] for c in cases}) == len(cases)
    tools = {
        "find_stations",
        "get_water_levels",
        "get_tide_predictions",
        "next_high_low_tide",
        "compare_observed_vs_predicted",
        "get_marine_forecast",
        "get_hourly_weather",
        None,
    }
    for c in cases:
        assert c["expected_tool"] in tools
        for extra in c.get("also_expected", []):
            assert extra["tool"] in tools
        # a trap case may have nothing to include: saying "I don't know" is the answer
        assert c["answer_must_include"] or c.get("expect_abstain"), c["id"]
    assert sum(1 for c in cases if c.get("expect_abstain")) >= 10
    assert {c["category"] for c in cases} == {"tide", "weather", "combined", "trap"}


def test_checkers():
    case = {
        "expected_tool": "next_high_low_tide",
        "expected_args": {"station": ["00490", "Halifax"], "count": {"approx": 6, "tol": 1}},
        "answer_must_include": [["08:36", "8:36"], ["1.98"]],
    }
    good = AgentResult(
        "x", [ToolRecord("next_high_low_tide", {"station": "halifax", "count": 6}, "", False)]
    )
    wrong_station = AgentResult(
        "x", [ToolRecord("next_high_low_tide", {"station": "Saint John", "count": 6}, "", False)]
    )
    wrong_tool = AgentResult(
        "x", [ToolRecord("get_tide_predictions", {"station": "Halifax"}, "", False)]
    )
    assert check_tools(case, good)
    assert not check_tools(case, wrong_station)
    assert not check_tools(case, wrong_tool)
    assert check_answer(case, "High tide at 8:36 AM, 1.98 m")[0]
    ok, missing = check_answer(case, "High tide at 8:36 AM")
    assert not ok and missing == [["1.98"]]


async def test_rule_model_passes_every_dev_case_and_most_held_out_cases():
    now, cases = load_cases()
    async with McpTools(create_server(FixtureSource())) as tools:
        report = await run_eval(RuleModel(now), tools, cases, now)
    failed = [r.id for r in report.results if not r.passed]
    # The one held-out question it gets wrong: it looks up a station for a question
    # no tool can answer. Kept in the set on purpose.
    assert failed == ["heldout-tidal-streams"]


async def test_sloppy_model_is_caught_by_the_grounding_check_not_the_old_checks():
    now, cases = load_cases()
    async with McpTools(create_server(FixtureSource())) as tools:
        report = await run_eval(SloppyModel(now), tools, cases, now)
    # It calls the right tools, so tool selection stays high.
    assert report.rate("tool_ok") > 0.95
    # It never admits it does not know, so no trap case is handled.
    assert not any(r.answer_ok and r.grounded for r in report.traps())
    # Most of its answers still contain the expected facts, but invent extra ones.
    padded = [r for r in report.results if r.answer_ok and not r.grounded]
    assert len(padded) >= 10
    assert report.rate("grounded") < 0.15


async def test_eval_scores_a_wrong_model_lower():
    """A model that always asks for predictions and answers vaguely should lose points."""
    now, cases = load_cases()
    turns = []
    for _ in cases:
        turns += [
            tool_turn(
                ToolCall(
                    "t",
                    "get_tide_predictions",
                    {"station": "Halifax", "start": "2026-11-26", "end": "2026-11-26"},
                )
            ),
            text_turn("Probably around noon."),
        ]
    async with McpTools(create_server(FixtureSource())) as tools:
        report = await run_eval(ScriptedModel(turns), tools, cases, now)
    assert report.rate("tool_ok") < 0.25
    assert report.rate("answer_ok") < 0.4


def test_cli_eval_offline_exit_code(capsys, monkeypatch):
    code = main(["eval"])
    out = capsys.readouterr().out
    assert code == 0
    assert "cases: 43" in out
    assert "grounded:       43/43" in out


def test_cli_ask_fake_model(capsys):
    code = main(["ask", "When is high tide in Halifax tomorrow?", "--offline", "--model", "fake"])
    assert code == 0
    assert "high tide at 08:36 AST" in capsys.readouterr().out


def test_cli_ask_without_key_refuses_anthropic(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        main(["ask", "hi", "--offline", "--model", "anthropic"])
    except SystemExit as exc:
        assert "ANTHROPIC_API_KEY" in str(exc)
    else:
        raise AssertionError("expected SystemExit")
