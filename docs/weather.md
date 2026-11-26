# Weather alongside tides

**In short:** two new tools give the assistant marine weather from Environment and Climate Change Canada, so it can answer questions that need both tide and weather, like "Is tomorrow morning a good time to launch a kayak at Halifax?"

## Why I added this

Tide predictions leave out weather (see the limits in the README). Whether the water is a good place to be depends on wind and warnings as much as on the tide, and I wanted to see how an assistant combines tools in more than one step.

## The data

Environment and Climate Change Canada's [MSC GeoMet API](https://api.weather.gc.ca) needs no key. Two collections are used:

- `marineweather-realtime`: the text forecast and warnings for each marine area (a polygon on the water). Wind is in knots.
- `citypageweather-realtime`: hourly temperature, wind and chance of rain at a named place, plus sunrise and sunset. Wind is in km/h.

I checked both with live requests before building on them. Contains information licensed under the [Open Government Licence - Canada](https://eccc-msc.github.io/open-data/licence/readme_en/).

## The tools

| Tool | What it returns |
| --- | --- |
| `get_marine_forecast(station)` | The forecast text for the marine area the station is in (or the nearest one within 25 km), and any warnings such as gale or storm |
| `get_hourly_weather(station, start, end)` | Hour by hour wind (km/h and knots), temperature, rain chance, and a summary and sunrise/sunset. Covers about the next 24 hours |

Errors say what to do: a station with no marine area, a time outside the forecast window (the message gives the window), a marine area that has stopped forecasting for the season.

## Multi-step questions

The kayak questions need three tools after finding the station: `next_high_low_tide`, `get_marine_forecast` and `get_hourly_weather`. The offline model asks for them in one go and answers with a verdict from simple rules written in `fake.py`: any warning in effect means no; wind above 25 km/h means no; above 15 km/h or a 60% rain chance means marginal; otherwise it looks good. Those thresholds are mine, not an official rule.

The eval has 5 weather questions and 5 combined questions. A case can list extra tools it must call (`also_expected`). In the sample, Vancouver and Tofino have a storm warning in effect (so the right answer is no) while Halifax, Saint John and Charlottetown are calm.

```bash
uv run tidewise ask "Is tomorrow morning a good time to launch a kayak at Halifax?" --offline --model fake -v
uv run tidewise ask "Is tomorrow morning a good time to launch a kayak at Vancouver?" --offline --model fake -v
```

## Offline sample

`scripts/fetch_weather_samples.py` saves the forecast areas and city points near the five sample stations to `src/tidewise/data/weather/` (192 KB, French text removed). It is a snapshot from 2026-11-25 evening, so the offline clock is the same day and the forecast window covers "tomorrow morning".

## Limits

- The marine forecast is text. The tool passes it on; it does not parse it into numbers.
- A station is matched to a marine area by geometry, so a harbour station can be "nearest area, 10 km away". The output says so.
- Hourly weather comes from the nearest city point (within 30 km), not from the water itself.
- Only the next day or so is covered. Anything further says so.
