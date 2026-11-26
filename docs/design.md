# Design notes

## Layout

```
src/tidewise/
  server.py       MCP tool definitions
  service.py      tool logic
  stations.py     name and distance search
  iwls.py         live API client
  fixtures.py     offline sample reader
  agent.py        tool-calling loop, Anthropic model wrapper
  fake.py         scripted and rule-based offline models
  evaluation.py   scoring
  eval_cases.json the questions
tests/            pytest suite
scripts/          fetch_samples.py
```

## How the pieces fit

- `service.py` has the tool logic as plain methods. `server.py` wraps them as MCP tools and turns my own errors into tool errors the client can read.
- The data source is swappable. `iwls.py` talks to the API; `fixtures.py` reads the committed sample. Tests and the eval use the sample.
- High and low tide labels: the API returns tide events without saying which are highs, so the tool fetches a little before the requested time and compares each event with its neighbour.
- The agent calls `claude-sonnet-5-5` through the Anthropic SDK. Answer checks accept several spellings of a time or number (for example `08:36` or `8:36`), but a model may still phrase things in a way I did not allow for.

## The offline sample

`src/tidewise/data/` holds a 168 KB sample of real responses: the list of operating stations (135), and for Halifax, Vancouver, Saint John, Charlottetown and Tofino, predictions and high/low events for 2026-11-24 to 2026-11-29 plus observed levels up to 2026-11-25 12:00 UTC. Offline mode pretends it is that moment, so "tomorrow" is 2026-11-26.

Refresh it with `uv run python scripts/fetch_samples.py`. The eval answers would then need checking again.
