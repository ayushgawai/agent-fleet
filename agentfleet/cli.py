"""CLI: fleet run|shard|review|bench|eval|serve|demo"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from .bench import bench_shard, eval_reviewer
from .fleet import Fleet
from .reviewer import ReviewContext, Reviewer
from .sharder import load_durations, run_shards

def main(argv=None):
    p = argparse.ArgumentParser(prog="fleet"); sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("prompt"); r.add_argument("--repo", default="."); r.add_argument("--agent", default="shell:printf 'no agent\\n'"); r.add_argument("--sandbox", default="auto")
    s = sub.add_parser("shard"); s.add_argument("path"); s.add_argument("-n", type=int, default=4); s.add_argument("--durations"); s.add_argument("--save-durations")
    v = sub.add_parser("review"); v.add_argument("diff_file"); v.add_argument("--scope", action="append", default=[])
    b = sub.add_parser("bench"); b.add_argument("--tests", type=int, default=1200); b.add_argument("-n", type=int, default=4); b.add_argument("--out", default=".fleet-bench"); b.add_argument("--sandbox", default="subprocess")
    e = sub.add_parser("eval"); e.add_argument("--seed", type=int, default=7)
    w = sub.add_parser("serve"); w.add_argument("--port", type=int, default=8000)
    d = sub.add_parser("demo"); d.add_argument("--repo", default=".")
    a = p.parse_args(argv)
    if a.cmd == "run":
        f = Fleet(a.repo, sandbox_kind=a.sandbox); t = f.submit(a.prompt, a.agent); f.run(t)
        print(json.dumps({"id": t.id, "status": t.status, "branch": t.branch, "verdict": t.verdict, "error": t.error}, indent=2))
    elif a.cmd == "shard": res = run_shards(a.path, a.n, load_durations(a.durations), save_durations=a.save_durations); print(json.dumps(res, indent=2)); return 0 if res["all_ok"] else 1
    elif a.cmd == "review":
        v_ = Reviewer().review(Path(a.diff_file).read_text(), ReviewContext(scope=a.scope)); print(json.dumps(v_.to_dict(), indent=2)); return 1 if v_.blocked else 0
    elif a.cmd == "bench": print(json.dumps(bench_shard(a.out, a.tests, a.n, a.sandbox), indent=2))
    elif a.cmd == "eval": print(json.dumps(eval_reviewer(a.seed), indent=2))
    elif a.cmd == "serve":
        import uvicorn; uvicorn.run("agentfleet.web:app", host="127.0.0.1", port=a.port)
    elif a.cmd == "demo":
        f = Fleet(a.repo, sandbox_kind="subprocess")
        tasks = [f.submit(f"task {i}: write file", "shell:printf 'demo agent\\n'; printf x > DEMO.md") for i in range(3)]
        for t in f.run_many(tasks, workers=3): print(t.id, t.status, t.branch)
    return 0
if __name__ == "__main__": sys.exit(main())
