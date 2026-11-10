"""Test sharder: split pytest nodes across N sandboxes by historical duration (greedy LPT bin-packing)."""
from __future__ import annotations
import ast, json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .sandbox import SandboxLimits, get_sandbox

def discover(root) -> list[str]:
    root = Path(root); nodes = []
    for p in sorted(root.rglob("test_*.py")) + sorted(root.rglob("*_test.py")):
        if ".venv" in p.parts or ".fleet" in p.parts: continue
        try: tree = ast.parse(p.read_text(errors="replace"))
        except SyntaxError: continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_"):
                nodes.append(f"{p.relative_to(root)}::{n.name}")
    return nodes

def shard(nodes, durations: dict[str, float] | None, n: int) -> list[list[str]]:
    durations = durations or {}
    default = (sum(durations.values()) / len(durations)) if durations else 1.0
    bins: list[list[str]] = [[] for _ in range(max(1, n))]; loads = [0.0] * len(bins)
    for node in sorted(nodes, key=lambda x: -durations.get(x, default)):
        i = min(range(len(bins)), key=lambda j: loads[j]); bins[i].append(node); loads[i] += durations.get(node, default)
    return [b for b in bins if b]

def load_durations(path) -> dict[str, float]:
    if not path: return {}
    data = json.loads(Path(path).read_text())
    # accept pytest-json / junit-ish {node: seconds} or {"durations": {...}}
    return {k: float(v) for k, v in (data.get("durations", data)).items()}

def run_shards(root, n=4, durations=None, sandbox_kind="subprocess", timeout_s=600) -> dict:
    root = Path(root); nodes = discover(root); groups = shard(nodes, durations, n)
    def one(group):
        t = time.monotonic()
        sb = get_sandbox(sandbox_kind, SandboxLimits(timeout_s=timeout_s))
        r = sb.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *group], cwd=root)
        return {"nodes": len(group), "ok": r.ok, "returncode": r.returncode, "duration_s": round(time.monotonic()-t, 3), "output": r.output[-4000:]}
    t = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, n)) as ex: results = list(ex.map(one, groups))
    return {"total": len(nodes), "shards": results, "passed_shards": sum(r["ok"] for r in results),
            "wall_s": round(time.monotonic()-t, 3), "all_ok": all(r["ok"] for r in results)}
