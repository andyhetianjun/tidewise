# Grounded answers

**In short:** a check that reads the assistant's final answer and asks, for every time, height, wind speed and place name in it, "did a tool actually return this?" Anything it cannot find is flagged. The eval also has questions with no real answer, where the right reply is "I don't know".

## Why I added this

The first version of the eval only checked that the right facts were in the answer. It never checked that nothing else was. A model can include the correct high tide time and then add "the next high is at 14:05 (2.4 m)" that no tool ever gave it, and that passed. I wanted to see how to catch that.

## What it checks

For each answer, `src/tidewise/grounding.py` pulls out:

- times (`08:36`, `8:36 AM`, `14:00 UTC`) and dates
- numbers with a unit: heights (m, cm, ft), wind (knots, km/h), temperature, percent, distance (km)
- station codes (`00490`) and place names ("at Halifax", "the Lunenburg station")

Each one is looked up in the tool outputs the agent received. It counts as supported when:

- it appears in a tool output, allowing for rounding (`1.98 m` for `1.977`);
- it is a unit conversion of a value in the output (`22 knots` for `40 km/h`, `198 cm` for `1.977 m`);
- it is a time in the right clock: `08:36 UTC` is not supported if only `08:36` local appears;
- the answer says "below" or "above" and the sign is the other way round in the data (`0.014 m below` for `-0.014`), but not the wrong direction;
- the sentence talks about a range or difference and the number is the gap between two returned values;
- the user already said it in the question.

Everything else is reported as unsupported, with the reason.

## Trap cases

15 of the 43 eval cases are traps. The right behaviour is to say it does not know and invent nothing:

- a station that does not exist (`Narnia Harbour`, `Winnipeg`)
- a time outside the data (a tide on 2026-12-30, an observed level on 2026-10-01, wind next week)
- something no tool provides (wave height, water temperature)

A trap passes only if the answer says it cannot answer **and** the grounding check finds no invented claims. Five of them are marked `heldout`: I wrote them after the offline model was finished and did not change the model for them.

## Measured results

```bash
uv run tidewise eval                 # offline rule-based model
uv run tidewise eval --model sloppy  # a model that is careless on purpose
```

| | Right tools | Answer as expected | Grounded | Traps handled | Held-out cases passing all checks |
| --- | --- | --- | --- | --- | --- |
| Offline rule-based model | 42/43 | 43/43 | 43/43 | 15/15 | 4/5 |
| Sloppy model | 42/43 | 28/43 | 3/43 | 0/15 | 0/5 |

The sloppy model calls the right tools. After that it adds a made-up detail to every answer (an extra high tide, a gust speed, a station that does not exist) and, when a tool returns an error, invents an answer instead of admitting it. 28 of its 43 answers still contain every fact the old check looked for, so the old check would have passed them. The grounding check passes 3 of 43 (the ones with no numbers to invent).

The one held-out case the rule-based model gets wrong is "How strong are the tidal streams in Halifax Harbour?": it answers correctly that it does not know, but only after looking up a station it did not need.

Read these carefully. The offline model is a keyword matcher I wrote, and I saw most of the questions while writing it. These numbers show the checks work, not how good Claude is. I have not run this with Claude (no API key here); `uv run tidewise eval --model anthropic` is the command, and it is untested.

### How good is the checker itself

`tests/test_grounding.py` has 35 answers I labelled by hand as grounded or not, against real tool output. The checker agrees on 35 of 35. I wrote the labels and the checker, so this is a regression test, not independent proof. While writing it, two of my labels were wrong (the invented numbers I picked did happen to appear in the data), which shows how loose some matches are.

## Limits

- It checks that a number is present, not that it belongs to the right quantity. A wind speed of 15 would be accepted if 15 appears anywhere in the output as a wind speed (or in forecast text).
- Claims with no number in them are not checked ("the wind is light").
- Arithmetic beyond one subtraction is not followed, so a correct average or sum may be flagged. That is the cautious direction.
- Place names are found by a pattern ("at X", "X station"), so a name written another way is missed.
