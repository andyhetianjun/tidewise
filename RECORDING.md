# Recording a model's answers

Tidewise scores how well a model uses its tools. When no API key is available, a model can be recorded another way: it plays the assistant, one question at a time, by running shell commands. The commands run the real tools and log everything. This file is the whole instruction for the model being recorded.

## If you are the model being evaluated

You are the assistant in a tide-data chat. Answer one question, using only the tools. Work from the repository folder (the one that contains this file). Use only the commands below: do not read other files in this repository, do not run `record list` or `record status`, and do not look at `recordings/`, the tests or `eval_cases.json`. They are not part of what the assistant knows.

You will be given one case id, a short code like `c07`. Then:

1. Run `uv run tidewise record prompt <case-id>`. It prints the assistant's system prompt, the tools (with their input schemas) and the user's question. That is everything the assistant knows.
2. Decide what the assistant would do. To call a tool, run
   `uv run tidewise record tool <case-id> <tool-name> '<arguments as a JSON object>'`
   and read the result it prints. Each call is logged, errors included. You can call several tools one after another, up to 8 in total.
3. Answer from what the system prompt and the tool results say, as the assistant would write it to the user. Do not use outside knowledge for tide times, heights or weather, and do not invent details. If the tools cannot answer the question, say so plainly.
4. Run `uv run tidewise record answer <case-id> '<your final answer>'`. Put the whole reply in the quotes. This saves the recording and ends the question.

Do not run `prompt` a second time for the same case (it starts the session over). If a command prints `error:`, read the message and fix the command.

## If you are running the recordings

- `uv run tidewise record list` shows every opaque id next to its readable id and whether it is done (coordinator only: the readable ids give away what a good answer looks like, so never show the list to the model). `record status` shows how many are done.
- Give each fresh session one opaque id (`c01` and so on, from a hash of the question text, so they stay the same while the questions do) and this file. The commands print only the opaque id; scoring and the demo use the readable id saved inside each recording. One session per question keeps the answers independent.
- Recordings are saved as `recordings/<model-id>/<case-id>.json`: the prompt the model saw, each tool call with its arguments, result and timing, the final answer, the model label, a timestamp and the harness version. Tools run against the bundled sample data with a fixed clock (2026-11-25 12:00 UTC), so results can be reproduced. `--live` on `prompt` or `tool` uses live data instead; such recordings cannot be scored against the sample.
- To record another model, pass `--model <id>` before the command word, for example `uv run tidewise record --model some-model prompt <case-id>`.
- Score what is recorded: `uv run tidewise eval --from-recordings recordings/claude-sonnet-5-5`. It prints the scores and writes `RESULTS.md` and `results.json` in that folder, with the offline and sloppy baselines run on the same questions.
- Rebuild the demo site with `uv run tidewise site --out site`. A "Recorded with Claude" page appears when `recordings/claude-sonnet-5-5` has recordings.

The recording is only as honest as the setup: the harness logs every tool call and result exactly, but the model's own reasoning between calls is not captured, and a session runs through a shell rather than the API.
