# agent-fleet

Run coding agents in parallel git worktrees inside sandboxes, shard tests and review diffs before merge.

An open-source Python CLI and small web dashboard for running many coding agents in parallel — each in its own git worktree and sandbox — then gating the result with a test sharder and a diff reviewer before a human approves, retries, or discards it.

## Design

```
                        +----------------------+
   prompts / tasks ---> |  Fleet orchestrator  |  (fleet.py, ThreadPool)
                        +----+-----------+-----+
                             |           |
              +--------------+           +---------------+
              v                                          v
   +---------------------+                    +---------------------+
   | WorktreeManager     |  one branch +    | Plugin agent        |  shell: / exec: /
   | .fleet/worktrees/<id>|  worktree/task  | (plugins.py)        |  module: / entry-point
   +----------+----------+                    +----------+----------+
              |      agent edits files                   |
              +------------------+-----------------------+
                                 v
                        +----------------------+
                        | Sandbox             | Docker (--cpus/--memory/
                        | docker | subprocess |  --network none) or subprocess
                        +----------+-----------+ (rlimits + timeout; fallback)
                                   | diff
                                   v
                        +----------------------+      +-------------------+
                        | Diff reviewer       |----->| optional LLM pass |
                        | deleted tests, weak |      | (pluggable client)|
                        | asserts, network,   |      +-------------------+
                        | secrets, large edits|
                        +----------+----------+
                                   | verdict
                                   v
   human gate:  approve -> merge | retry -> reset+rerun | discard -> remove worktree
                                   ^
                        +----------------------+
                        | FastAPI + htmx dash |  / , /api/tasks, logs, actions
                        +----------------------+

   Test sharder (sharder.py):
     AST discovery --> LPT bins by historical duration --> N sandboxes (pytest --junitxml each)
                   ^                                                   |
                   +---- durations.json  <---- merged per-test result -+
```

| Component | File | Choice | Why / ceiling |
|---|---|---|---|
| Worktrees | `agentfleet/worktree.py` | `git worktree add -B fleet/<id>`, git ops serialized with a lock | Real git isolation, no copy cost. Lock avoids `.git` races. |
| Sandbox | `agentfleet/sandbox.py` | Docker first (`--cpus --memory --memory-swap --pids-limit --network none --cap-drop ALL`), subprocess fallback (rlimits via `agentfleet/_limits.py`, timeout, process group; `unshare -rn` on Linux when available) | Docker is the real boundary; fallback is best-effort and reports what it could not enforce. |
| Plugins | `agentfleet/plugins.py` | `shell:<cmd>`, `exec:<prog> …`, `module:Class`, `agentfleet.agents` entry points; prompt passed via `$FLEET_PROMPT`, never shell-interpolated | Any local LLM, API agent CLI, or script drops in without fleet changes. |
| Sharder | `agentfleet/sharder.py` | AST discovery (functions + `Test*` classes), greedy longest-processing-time bin-packing by historical duration, one sandbox per shard, JUnit XML per shard merged into per-test counts/failures, measured durations saved back as history | Balanced shards without running pytest to collect. No history → equal weights. |
| Reviewer | `agentfleet/reviewer/` | Unified-diff parser + static rules (deleted tests, skipped/trivial/loosened/removed assertions, swallowed exceptions, new network calls incl. `curl` via subprocess, secrets, large/out-of-scope edits, CI/test-collection narrowing) + optional `LLMClient` protocol | Deterministic, explainable, offline by default; LLM is an add-on, never required. Any `high` finding blocks approve. |
| Orchestrator | `agentfleet/fleet.py` | In-memory task registry, `ThreadPoolExecutor`, statuses `queued→running→awaiting_review→merged/failed/discarded` | Simple and inspectable; not durable (see Limitations). |
| Dashboard | `agentfleet/web.py` | FastAPI + server-rendered htmx fragments (task table refreshes every 2s) + Server-Sent Events log stream (`/api/tasks/{id}/stream`) | No JS build step; ~10 lines of `EventSource` JS. 404/409 on unknown task or illegal action. |

## Run

```bash
pip install -e .            # or: uv venv && uv pip install -e .

# one agent task in its own worktree + sandbox (repo must be a git repo with a commit)
fleet run "add a retry helper" --repo /path/to/repo \
  --agent 'shell:aider --message "$FLEET_PROMPT" --yes'        # any agent command works
fleet run "write a file" --agent "shell:printf hi > OUT.md"    # built-in demo agent

# shard a pytest suite across 4 sandboxes (optionally with historical durations)
fleet shard path/to/tests -n 4
fleet shard path/to/tests -n 4 --save-durations durations.json # first run: records per-test timings
fleet shard path/to/tests -n 4 --durations durations.json      # later runs: balance by that history

# review a diff before merge (exit 1 if blocked by a high-severity finding)
git diff main...HEAD > change.diff
fleet review change.diff --scope 'src/**'

# dashboard: stream status/logs, approve / retry / discard
fleet serve --port 8000        # http://127.0.0.1:8000

# parallel demo: 3 shell agents in 3 worktrees at once
fleet demo --repo /path/to/repo
```

Agent plugin examples: `shell:<any command reading $FLEET_PROMPT>`, `exec:my-agent --task {prompt}`, a Python class (`my_pkg.agents:MyAgent` implementing `invocation(task_id, prompt)`), or an `agentfleet.agents` entry point. See `examples/echo_agent.sh`.

## Test

```bash
python -m pytest -q
```

Laptop sanity run (this repo, macOS arm64, Python 3.15):

```
...................                                                      [100%]
19 passed
```

The suite covers sandbox run/timeout, plugin specs, LPT balancing, discovery, JUnit merge + duration history, Docker flag construction (without a daemon), every reviewer rule family, the 300-diff eval thresholds, single and parallel worktree tasks, blocked-merge refusal, and the HTTP API including the SSE stream.

## Bench

```bash
fleet bench --tests 1200 -n 4   # generates a skewed 1,200-test suite; serial vs naive 4-way split vs duration-LPT 4-way split
fleet eval                      # reviewer on 300 seeded diffs (150 bad, 150 good); --seed N to vary instances
```

What the shard bench does: it generates 12 test files (9 "unit" files of mostly 2 ms tests, 3 "integration" files at the end where 30% of tests sleep 60 ms). It runs everything serially in one sandbox, recording JUnit timings as history. It then runs a naive split (4 equal-count contiguous chunks) and an LPT split that uses the serial run's history, each shard in its own sandbox. All wall times include pytest startup per sandbox.

Laptop sanity runs (MacBook, arm64, subprocess sandbox because Docker was not installed; actual output, numbers will vary):

```
$ fleet bench --tests 1200 -n 4
{
  "tests": 1200,
  "shards": 4,
  "backend": "subprocess",
  "serial_s": 11.459,
  "naive_split_s": 7.311,
  "duration_lpt_s": 3.322,
  "speedup_naive": 1.57,
  "speedup_lpt": 3.45,
  "lpt_shard_s": [3.196, 3.321, 3.189, 3.268],
  "merged_counts": {"passed": 1200},
  "all_ok": true
}

$ fleet eval
{
  "bad": 150,
  "good": 150,
  "tp": 150,
  "fp": 10,
  "recall": 1.0,
  "false_positive_rate": 0.067,
  "missed_by_category": {},
  "false_positives_by_category": {"test-fixture-key": 10}
}
```

How to read these numbers:

- **Sharding:** the naive split lands all the slow integration tests in one shard, so it gets 1.57×. Balancing by history gets 3.45× out of a 4× ceiling, and the four shards finish within 0.13 s of each other. The suite is synthetic (sleeps, no fixtures, no imports), so a real suite will gain less.
- **Reviewer:** 15 bad and 15 good templates, each instantiated 10 times with randomized names, values, tokens and context. The good set includes hard negatives: tests moved between files, strengthened assertions, env-var secrets, `.env.example` placeholders, network mentioned in comments and docs, mocked network in tests, and in-scope medium-size edits. The first run of this eval scored **recall 0.867 / FPR 0.133**. It missed loosened assertions (`== 4` → `>= 0`) and `subprocess.run(['curl', ...])`, and it flagged `curl` in docs. I then added a loosened-assertion rule and a subprocess-network pattern, and skipped docs files in the network rule, which gave the numbers above. The remaining false positive (a fake `sk-test...` key in a test file) is left in deliberately. **The rules and the eval were written together and the rules were tuned on this set**, so these numbers show the rules fire as intended on known patterns. They are not an estimate of performance on real agent diffs. Different seeds re-randomize instances, not templates.

## Limitations (honest)

- **Subprocess sandbox is not a security boundary.** Without Docker it enforces timeout/rlimits (and network only via `unshare` on Linux, else a proxy blackhole that non-proxy-aware code ignores). Memory rlimits are not enforced on macOS. Use Docker for untrusted agents.
- **State is in-memory.** Fleet/dashboard state dies with the process; no persistence, queue, auth, or multi-user support. Dashboard binds to `127.0.0.1` — do not expose it as-is.
- **Reviewer is static and pattern-based.** It does not run the code, resolve imports, or understand semantics; moved/renamed tests and multi-line constructs can fool it. The LLM pass is only as good as the client you plug in (none is bundled).
- **Sharder** splits at test-function granularity (parametrized cases stay together), discovers tests by AST rather than `pytest --collect-only` (so custom collection hooks, `python_files` overrides and generated tests are missed), and ignores fixtures, shared state and order dependence. A shard that crashes before writing its JUnit report counts as a failure but loses its per-test detail.
- **Merge is a plain `git merge`** into the repo's checked-out branch — conflicts surface as task failures for a human, there is no auto-rebase.
- Docker image is fixed (`python:3.12-slim` default); per-task images, GPU, and remote/distributed workers are not implemented.
- Nothing here talks to a specific model provider; bring your own agent command. The `LLMClient` hook is a protocol with no bundled implementation.
- **Docker path is untested on this machine.** Docker was not installed where this was built, so the bench and test runs above all used the subprocess sandbox. Only the Docker command-line flags are unit-tested.

## Unfinished

- No persistence for fleet state (tasks vanish on restart) and no auth on the dashboard.
- No per-task resource overrides from the dashboard; limits come from `SandboxLimits` defaults or the Python API.
- No bundled LLM reviewer client and no real agent-diff corpus for evaluation (see the caveats under Bench).
- Retry reuses the same worktree and resets it hard to the base commit; there is no history of earlier attempts.

## License

MIT — see `LICENSE`. Copyright (c) 2026 Ayush Gawai.
