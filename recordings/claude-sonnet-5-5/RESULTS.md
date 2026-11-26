# Recorded results: Claude Sonnet 5.5 (`claude-sonnet-5-5`)

43 of 43 questions recorded. Scores below cover only the recorded questions.

| Model | Right tools | Answer as expected | Grounded | Trap cases handled | All three |
| --- | --- | --- | --- | --- | --- |
| Claude Sonnet 5.5 (recorded) | 40/43 | 37/43 | 38/43 | 15/15 | 31/43 |
| Offline rule-based model | 42/43 | 43/43 | 43/43 | 15/15 | 42/43 |
| Deliberately sloppy model | 42/43 | 28/43 | 3/43 | 0/15 | 3/43 |

| Case | Tools | Answer | Grounded | Calls |
| --- | --- | --- | --- | --- |
| hilo-halifax-high-tomorrow | pass | pass | pass | 2 |
| hilo-halifax-low-tomorrow | pass | pass | pass | 2 |
| hilo-saintjohn-tomorrow | pass | pass | pass | 2 |
| hilo-charlottetown-tomorrow | pass | pass | pass | 2 |
| hilo-halifax-next-high | pass | pass | pass | 2 |
| hilo-halifax-next-low | pass | pass | pass | 2 |
| hilo-charlottetown-date | pass | pass | pass | 2 |
| pred-vancouver-tomorrow | pass | fail | pass | 2 |
| pred-saintjohn-table | fail | fail | pass | 2 |
| pred-halifax-date | pass | pass | fail | 2 |
| obs-vancouver-yesterday | pass | fail | pass | 2 |
| obs-saintjohn-yesterday | pass | pass | pass | 2 |
| obs-halifax-gap | pass | pass | pass | 3 |
| compare-saintjohn | pass | pass | pass | 2 |
| compare-vancouver | pass | pass | pass | 2 |
| compare-tofino-surge | pass | pass | pass | 2 |
| stations-coords-halifax | pass | pass | pass | 1 |
| stations-coords-victoria | pass | pass | pass | 1 |
| stations-name-vancouver | pass | pass | pass | 2 |
| unknown-place | pass | pass | pass | 2 |
| no-tool-needed | pass | pass | pass | 0 |
| wind-halifax-morning | pass | pass | pass | 3 |
| wind-vancouver-morning | pass | pass | pass | 3 |
| temp-saintjohn-morning | pass | fail | pass | 2 |
| marine-warnings-vancouver | pass | pass | fail | 2 |
| marine-halifax | pass | pass | pass | 2 |
| kayak-halifax-morning | pass | pass | fail | 4 |
| kayak-vancouver-morning | pass | pass | pass | 4 |
| canoe-tofino-morning | pass | fail | pass | 4 |
| paddle-charlottetown-afternoon | pass | pass | fail | 4 |
| boat-saintjohn-morning | pass | fail | fail | 4 |
| trap-station-narnia | pass | pass | pass | 2 |
| trap-hilo-far-future | pass | pass | pass | 2 |
| trap-observed-old | pass | pass | pass | 2 |
| trap-wind-far | pass | pass | pass | 2 |
| trap-waves | fail | pass | pass | 2 |
| trap-water-temperature | pass | pass | pass | 0 |
| trap-marine-inland | pass | pass | pass | 2 |
| heldout-sea-ice | pass | pass | pass | 1 |
| heldout-future-year | pass | pass | pass | 2 |
| heldout-wind-yesterday | pass | pass | pass | 2 |
| heldout-tidal-streams | fail | pass | pass | 1 |
| heldout-kayak-unknown | pass | pass | pass | 2 |

## Misses

- pred-vancouver-tomorrow: answer missing: 0.01/0.015
- pred-saintjohn-table: tools called: find_stations, next_high_low_tide | answer missing: 8.43/8.426/8.4; 0.36
- pred-halifax-date: unsupported time: 09:30 | unsupported time: 16:30
- obs-vancouver-yesterday: answer missing: 0.71
- temp-saintjohn-morning: answer missing: 1.0 to 4.0/1 to 4
- marine-warnings-vancouver: unsupported time: 12:00 UTC
- kayak-halifax-morning: unsupported time: 08:30
- canoe-tofino-morning: answer missing: not a good time
- paddle-charlottetown-afternoon: unsupported time: 15:30
- boat-saintjohn-morning: answer missing: looks good; 06:20 | unsupported place: Fundy
- trap-waves: tools called: find_stations, get_marine_forecast
- heldout-tidal-streams: tools called: find_stations
