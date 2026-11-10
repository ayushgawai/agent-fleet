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

   Test sharder (sharder.py):  pytest nodes --(historical durations, greedy LPT)--> N sandboxes --> merged verdict
```

| Component | File | Choice | Why / ceiling |
|---|---|---|---|
| Worktrees | `agentfleet/worktree.py` | `git worktree add -B fleet/<id>`, git ops serialized with a lock | Real git isolation, no copy cost. Lock avoids `.git` races. |
| Sandbox | `agentfleet/sandbox.py` | Docker first (`--cpus --memory --memory-swap --pids-limit --network none --cap-drop ALL`), subprocess fallback (rlimits via `agentfleet/_limits.py`, timeout, process group; `unshare -rn` on Linux when available) | Docker is the real boundary; fallback is best-effort and reports what it could not enforce. |
| Plugins | `agentfleet/plugins.py` | `shell:<cmd>`, `exec:<prog> …`, `module:Class`, `agentfleet.agents` entry points; prompt passed via `$FLEET_PROMPT`, never shell-interpolated | Any local LLM, API agent CLI, or script drops in without fleet changes. |
| Sharder | `agentfleet/sharder.py` | AST discovery, greedy longest-processing-time bin-packing by historical duration, parallel sandbox runs | Balanced shards without running pytest to collect. No duration history → equal weights. |
| Reviewer | `agentfleet/reviewer/` | Unified-diff parser + static rules (deleted tests, weakened/skipped assertions, new network calls, secrets, large/unrelated edits, CI narrowing) + optional `LLMClient` protocol | Deterministic, explainable, offline by default; LLM is an add-on, never required. |
| Orchestrator | `agentfleet/fleet.py` | In-memory task registry, `ThreadPoolExecutor`, statuses `queued→running→awaiting_review→merged/failed/discarded` | Simple and inspectable; not durable (see Limitations). |
| Dashboard | `agentfleet/web.py` | FastAPI + server-rendered htmx fragments, polling every 2s | No JS build step. Polling, not websockets. |

## Run

```bash
pip install -e .            # or: uv venv && uv pip install -e .

# one agent task in its own worktree + sandbox (repo must be a git repo with a commit)
fleet run "add a retry helper" --repo /path/to/repo \
  --agent 'shell:aider --message "$FLEET_PROMPT" --yes'        # any agent command works
fleet run "write a file" --agent "shell:printf hi > OUT.md"    # built-in demo agent

# shard a pytest suite across 4 sandboxes (optionally with historical durations)
fleet shard path/to/tests -n 4
fleet shard path/to/tests -n 4 --durations durations.json      # {"test_x.py::test_y": 1.23, ...}

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

Laptop sanity run (this repo, 12 tests):

```
12 passed
```

## Bench

```bash
fleet bench --tests 1200 -n 4   # generates a 1,200-test suite (5 ms sleep/test), runs it serially vs 4 shards
fleet eval                      # reviewer on 300 seeded diffs (150 bad, 150 good)
```

Laptop sanity runs (MacBook, arm64, subprocess sandbox — copy of actual output, numbers will vary):

```
$ fleet bench --tests 1200 -n 4
{"tests": 1200, "shards": 4, "serial_s": 7.92, "parallel_s": 2.135, "speedup": 3.71, "all_ok": true}

$ fleet eval
{"bad": 150, "good": 150, "recall": 1.0, "false_positive_rate": 0.0, "tp": 150, "fp": 0}
```

Honest reading of these numbers: the generated tests are uniform 5 ms sleeps, so sharding is near-ideal and the 3.71× is close to the 4-shard ceiling minus pytest startup per shard. Real suites with skewed durations, fixtures, and imports will do worse — that is exactly what `--durations` is for. The reviewer eval is seeded from the same rule families the reviewer implements, so 1.0 recall / 0.0 FPR shows the rules fire as intended on known patterns, **not** that they catch novel bad diffs in the wild.

## Limitations (honest)

- **Subprocess sandbox is not a security boundary.** Without Docker it enforces timeout/rlimits (and network only via `unshare` on Linux, else a proxy blackhole that non-proxy-aware code ignores). Memory rlimits are not enforced on macOS. Use Docker for untrusted agents.
- **State is in-memory.** Fleet/dashboard state dies with the process; no persistence, queue, auth, or multi-user support. Dashboard binds to `127.0.0.1` — do not expose it as-is.
- **Reviewer is static and pattern-based.** It does not run the code, resolve imports, or understand semantics; moved/renamed tests and multi-line constructs can fool it. The LLM pass is only as good as the client you plug in (none is bundled).
- **Sharder** splits at test-function granularity, ignores fixtures/shared state/order dependence, and merges shard pass/fail rather than per-test JUnit detail.
- **Merge is a plain `git merge`** into the repo's checked-out branch — conflicts surface as task failures for a human, there is no auto-rebase.
- Docker image is fixed (`python:3.12-slim` default); per-task images, GPU, and remote/distributed workers are not implemented.
- Nothing here talks to a specific model provider; bring your own agent command.

## License

MIT — see `LICENSE`. Copyright (c) 2026 Ayush Gawai.
