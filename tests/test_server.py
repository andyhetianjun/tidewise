import json

from mcp.client import Client

from tidewise.fixtures import FixtureSource
from tidewise.server import create_server

EXPECTED = {
    "find_stations",
    "get_water_levels",
    "get_tide_predictions",
    "next_high_low_tide",
    "compare_observed_vs_predicted",
}


async def test_lists_the_five_tools_with_docs_and_schemas():
    async with Client(create_server(FixtureSource())) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == EXPECTED
    for tool in tools:
        assert len(tool.description or "") > 80, tool.name
        assert tool.input_schema["type"] == "object"
    hilo = next(t for t in tools if t.name == "next_high_low_tide")
    assert hilo.input_schema["required"] == ["station"]
    assert hilo.input_schema["properties"]["count"]["type"] == "integer"


async def test_call_tool_returns_json_text():
    async with Client(create_server(FixtureSource())) as client:
        result = await client.call_tool(
            "next_high_low_tide", {"station": "Halifax", "after": "2026-11-26", "count": 2}
        )
    assert not result.is_error
    data = json.loads(result.content[0].text)
    assert data["station"]["code"] == "00490"
    assert len(data["events"]) == 2


async def test_domain_errors_reach_the_client_as_readable_errors():
    async with Client(create_server(FixtureSource())) as client:
        result = await client.call_tool("find_stations", {"query": "Atlantis"})
    assert result.is_error
    assert "No stations found" in result.content[0].text


async def test_type_errors_are_rejected_before_the_tool_runs():
    async with Client(create_server(FixtureSource())) as client:
        result = await client.call_tool("find_stations", {"query": "Halifax", "limit": "lots"})
    assert result.is_error
