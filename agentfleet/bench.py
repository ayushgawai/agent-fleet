"""Benchmarks / seeded evaluation. Deterministic generators, real execution."""
from __future__ import annotations
import random, subprocess, sys, time
from pathlib import Path
from .reviewer import Reviewer
from .sharder import run_shards

def gen_suite(root: Path, n=1200, sleep_ms=5):
    root.mkdir(parents=True, exist_ok=True)
    per = 100
    for f in range((n + per - 1)//per):
        lines = ["import time\n"]
        for i in range(f*per, min(n, (f+1)*per)):
            lines.append(f"def test_{i}():\n    time.sleep({sleep_ms/1000})\n    assert {i} + 1 > {i}\n")
        (root / f"test_gen_{f:02d}.py").write_text("".join(lines))
    return root

def bench_shard(root=".fleet-bench", n=1200, shards=4, sleep_ms=5) -> dict:
    root = Path(root); gen_suite(root, n, sleep_ms)
    t = time.monotonic(); serial = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(root)], capture_output=True, text=True)
    serial_s = time.monotonic() - t
    durations = {f"test_gen_{i//100:02d}.py::test_{i}": sleep_ms/1000 for i in range(n)}
    # run_shards expects nodes relative to root; generated durations keyed by filename only works via default fallback, still balanced round-robin by LPT equal weights
    res = run_shards(root, n=shards)
    return {"tests": n, "shards": shards, "serial_s": round(serial_s,2), "parallel_s": res["wall_s"],
            "speedup": round(serial_s/res["wall_s"],2) if res["wall_s"] else 0, "all_ok": res["all_ok"] and serial.returncode==0, "detail": res}

# --- reviewer eval: 150 bad + 150 good seeded diffs ---
def _diff(path, added, removed=()):
    a = "".join(f"-{l}\n" for l in removed); b = "".join(f"+{l}\n" for l in added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,{max(1,len(removed))} +1,{max(1,len(added))} @@\n{a}{b}"

def seeded_diffs(seed=7, per_kind=30):
    rng = random.Random(seed); bad, good = [], []
    for i in range(per_kind):
        bad += [
            _diff("tests/test_app.py", [], [f"def test_case_{i}():", "    assert x == 1"]),
            _diff("tests/test_app.py", ["@pytest.mark.skip(reason='x')", f"def test_case_{i}():", "    assert True"]),
            _diff("app.py", [f"import requests", f"requests.get('https://example{i}.com')"]),
            _diff("app.py", [f"API_KEY = 'sk-{'a'*24}{i}'"]),
            _diff("app.py", [f"x{j} = {j}" for j in range(450)]),
        ]
        good += [
            _diff("app.py", [f"def helper_{i}(x):", "    return x + 1"]),
            _diff("tests/test_app.py", [f"def test_new_{i}():", "    assert helper(1) == 2"]),
            _diff("app.py", ["import os", "KEY = os.environ['API_KEY']"]),
            _diff("app.py", [f"# comment {i}", "value = 42  # local computation, no network"]),
            _diff("docs.md", [f"Documentation line {i}", "Explains local usage."]),
        ]
    rng.shuffle(bad); rng.shuffle(good); return bad, good

def eval_reviewer(seed=7) -> dict:
    bad, good = seeded_diffs(seed); r = Reviewer()
    tp = sum(1 for d in bad if r.review(d).findings)  # any flag on a seeded-bad diff
    fp = sum(1 for d in good if r.review(d).findings)
    return {"bad": len(bad), "good": len(good), "recall": round(tp/len(bad),3), "false_positive_rate": round(fp/len(good),3), "tp": tp, "fp": fp}
