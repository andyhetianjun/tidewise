# Eval runner

**In short:** `tidewise run-evals` runs every eval question in its own process, several at once, with a time limit and retries, and writes a web page you can click through. Each case page shows every step the agent took: what it asked for, the exact arguments, what the tool returned, and how long each took.

## Why I added this

`tidewise eval` runs the questions one after another in one process. That is fine offline, but with a real model each question takes seconds and the network is flaky. One hung request stalls everything, one connection error counts as a wrong answer, and a case that changes the clock or the cache can affect the next one. I wanted to see how to run evals so those problems stay local to one case.

## What it does

- **One process per case.** The runner starts `python -m tidewise.worker`, sends the case as JSON, and reads the result as JSON. Cases cannot share state.
- **Parallel.** `--jobs N` cases at once (default: your core count, at most 8).
- **Timeout.** `--timeout` seconds per attempt (default 60). On timeout the runner kills only the process it started.
- **Retries.** A timeout, a network error, a rate limit, or a tool that reports it could not reach the data service is retried up to `--retries` times (default 2) with a short back-off. A crash or a wrong answer is not retried: it is a result.
- **Traces.** For each case: every model reply and tool call, with arguments, result and timing, plus every attempt and why it ended.
- **Report.** `index.html` with filters, one page per case, raw JSON per case and `results.json`. Links are relative, so the folder works from disk or any URL path.

## Try it

```bash
uv run tidewise run-evals --out eval-report -v
open eval-report/index.html

uv run tidewise run-evals --model sloppy --out eval-sloppy     # see failures marked
uv run tidewise run-evals --model flaky --only hilo-halifax    # first try of each case fails, retry works
uv run tidewise run-evals --jobs 1 --latency-ms 300            # serial, with slower tools
```

`flaky` is a test stand-in that fails the first attempt of every case with a simulated network error. `--latency-ms` adds a delay to every tool call, to see what parallelism does when calls are slow. It is a simulation and is labelled as one below.

## Measured wall time

43 cases, offline rule-based model, on my laptop (15 cores), one run each, so expect some noise:

| Command | Wall time |
| --- | --- |
| `uv run tidewise eval` (in-process, one process for all cases, for reference) | 0.7 s |
| `uv run tidewise run-evals --jobs 1` | 18.6 s |
| `uv run tidewise run-evals --jobs 4` | 4.6 s |
| `uv run tidewise run-evals --jobs 8` | 3.3 s |
| `--jobs 1 --latency-ms 300` (simulated slow tools) | 41.1 s |
| `--jobs 8 --latency-ms 300` (simulated slow tools) | 6.5 s |

So isolation is not free: starting a Python process per case costs about 0.4 s each, which is why serial run-evals is slower than the in-process eval. Parallelism more than makes it back (5.7x faster at 8 jobs). Against a real model the per-case time is dominated by the network, which is where parallel runs help most; I have only simulated that.

The GitHub Pages build repeats the serial and parallel timing on the Actions runner and shows it on the demo home page.

## Layout

`runner.py` (scheduling, timeouts, retries), `worker.py` (one case in one process), `report.py` (HTML). Tests are in `tests/test_runner.py` and use small stand-in worker scripts to check retries, timeouts and ordering without waiting on real work.
