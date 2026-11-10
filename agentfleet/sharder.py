"""Test sharder: split pytest nodes across N sandboxes by historical duration (greedy LPT bin-packing).

Each shard writes a JUnit XML report; reports are merged into one per-test
result, and the measured per-test durations can be saved as the history for
the next run.
"""
from __future__ import annotations
import ast, json, sys, tempfile, time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .sandbox import SandboxLimits, get_sandbox

def discover(root) -> list[str]:
    root = Path(root); nodes = []
    for p in sorted(root.rglob("test_*.py")) + sorted(root.rglob("*_test.py")):
        if ".venv" in p.parts or ".fleet" in p.parts: continue
        try: tree = ast.parse(p.read_text(errors="replace"))
        except SyntaxError: continue
        rel, fn = p.relative_to(root).as_posix(), (ast.FunctionDef, ast.AsyncFunctionDef)
        for n in tree.body:
            if isinstance(n, fn) and n.name.startswith("test"):
                nodes.append(f"{rel}::{n.name}")
            elif isinstance(n, ast.ClassDef) and n.name.startswith("Test"):
                nodes += [f"{rel}::{n.name}::{m.name}" for m in n.body if isinstance(m, fn) and m.name.startswith("test")]
    return nodes

def shard(nodes, durations: dict[str, float] | None, n: int) -> list[list[str]]:
    """Longest-processing-time first: heaviest test goes to the currently lightest shard."""
    durations = durations or {}
    default = (sum(durations.values()) / len(durations)) if durations else 1.0
    bins: list[list[str]] = [[] for _ in range(max(1, n))]; loads = [0.0] * len(bins)
    for node in sorted(nodes, key=lambda x: -durations.get(x, default)):
        i = min(range(len(bins)), key=lambda j: loads[j]); bins[i].append(node); loads[i] += durations.get(node, default)
    return [b for b in bins if b]

def shard_contiguous(nodes, n: int) -> list[list[str]]:
    """Naive baseline: equal-count contiguous chunks in discovery order."""
    size = -(-len(nodes) // max(1, n))
    return [nodes[i:i + size] for i in range(0, len(nodes), size)]

def load_durations(path) -> dict[str, float]:
    if not path or not Path(path).exists(): return {}
    data = json.loads(Path(path).read_text())
    return {k: float(v) for k, v in (data.get("durations", data)).items()}

def parse_junit(path, root: Path) -> list[dict]:
    """JUnit XML -> [{node, outcome, duration_s}] with node ids relative to ``root``."""
    out = []
    for tc in ET.parse(path).getroot().iter("testcase"):
        cls, name = tc.get("classname", ""), tc.get("name", "")
        # pytest writes classname "pkg.test_mod" (or "pkg.test_mod.TestCls"); map back to a file path.
        parts = cls.split(".")
        for cut in range(len(parts), 0, -1):
            f = "/".join(parts[:cut]) + ".py"
            if (root / f).exists():
                node = "::".join([f, *parts[cut:], name]); break
        else:
            node = f"{cls}::{name}"
        outcome = "passed"
        for tag in ("failure", "error", "skipped"):
            if tc.find(tag) is not None: outcome = {"failure": "failed"}.get(tag, tag)
        out.append({"node": node, "outcome": outcome, "duration_s": float(tc.get("time", 0) or 0)})
    return out

def merge(per_shard: list[list[dict]]) -> dict:
    tests = [t for s in per_shard for t in s]
    counts: dict[str, int] = {}
    durations: dict[str, float] = {}
    for t in tests:
        counts[t["outcome"]] = counts.get(t["outcome"], 0) + 1
        fn_node = t["node"].split("[", 1)[0]  # parametrized cases add up to their function's weight
        durations[fn_node] = round(durations.get(fn_node, 0.0) + t["duration_s"], 6)
    return {"counts": counts, "failed": sorted(t["node"] for t in tests if t["outcome"] in ("failed", "error")),
            "durations": durations}

def run_shards(root, n=4, durations=None, sandbox_kind="subprocess", timeout_s=600, groups=None, save_durations=None) -> dict:
    root = Path(root).resolve(); nodes = discover(root)
    groups = groups if groups is not None else shard(nodes, durations, n)
    tmp = Path(tempfile.mkdtemp(prefix="fleet-shards-"))
    def one(i_group):
        i, group = i_group; xml = tmp / f"shard_{i}.xml"; t = time.monotonic()
        sb = get_sandbox(sandbox_kind, SandboxLimits(timeout_s=timeout_s))
        r = sb.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}", *group], cwd=root)
        tests = parse_junit(xml, root) if xml.exists() else []
        return {"nodes": len(group), "ok": r.ok, "returncode": r.returncode, "backend": r.backend,
                "duration_s": round(time.monotonic() - t, 3), "output_tail": r.output[-2000:]}, tests
    t = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, len(groups))) as ex: results = list(ex.map(one, enumerate(groups)))
    wall = round(time.monotonic() - t, 3)
    merged = merge([tests for _, tests in results])
    if save_durations: Path(save_durations).write_text(json.dumps({"durations": merged["durations"]}, indent=1))
    shards = [s for s, _ in results]
    reported = sum(merged["counts"].values())
    return {"total": len(nodes), "reported": reported, "counts": merged["counts"], "failed": merged["failed"][:50],
            "shards": shards, "wall_s": wall,
            # A shard that crashed before writing its report is a failure even if no test says so.
            "all_ok": all(s["ok"] for s in shards) and not merged["failed"] and reported >= len(nodes)}
