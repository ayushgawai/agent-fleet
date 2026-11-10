"""Benchmarks / seeded evaluation. Deterministic generators, real execution."""
from __future__ import annotations
import random, tempfile
from pathlib import Path
from .reviewer import Reviewer, ReviewContext
from .sharder import discover, load_durations, run_shards, shard, shard_contiguous

# --- sharder bench -------------------------------------------------------------
# Skewed on purpose, like real suites: 9 "unit" files of mostly 2 ms tests and
# 3 "integration" files at the end where 30% of tests take 60 ms.
def gen_suite(root: Path, n=1200, seed=1) -> Path:
    rng = random.Random(seed); root.mkdir(parents=True, exist_ok=True)
    per = 100; files = -(-n // per)
    for f in range(files):
        integration = f >= files - max(1, files // 4)
        lines = ["import time\n"]
        for i in range(f * per, min(n, (f + 1) * per)):
            if integration: ms = 60 if rng.random() < 0.30 else 5
            else: ms = 25 if rng.random() < 0.05 else 2
            lines.append(f"def test_{i}():\n    time.sleep({ms / 1000})\n    assert {i} + 1 > {i}\n")
        (root / f"test_gen_{f:02d}.py").write_text("".join(lines))
    return root

def bench_shard(root=None, n=1200, shards=4, sandbox_kind="subprocess") -> dict:
    root = Path(root or tempfile.mkdtemp(prefix="fleet-bench-"))
    for old in root.glob("test_gen_*.py"): old.unlink()  # only ever delete files we generated
    gen_suite(root, n)
    nodes = discover(root)
    # 1) serial run: one sandbox, all tests. Its JUnit timings become the "history".
    serial = run_shards(root, groups=[nodes], sandbox_kind=sandbox_kind, save_durations=root / "durations.json")
    history = load_durations(root / "durations.json")
    # 2) naive: equal-count contiguous chunks.  3) LPT using the serial run's durations.
    naive = run_shards(root, groups=shard_contiguous(nodes, shards), sandbox_kind=sandbox_kind)
    lpt = run_shards(root, groups=shard(nodes, history, shards), sandbox_kind=sandbox_kind)
    s = serial["wall_s"]
    return {"tests": len(nodes), "shards": shards, "backend": serial["shards"][0]["backend"],
            "serial_s": s, "naive_split_s": naive["wall_s"], "duration_lpt_s": lpt["wall_s"],
            "speedup_naive": round(s / naive["wall_s"], 2), "speedup_lpt": round(s / lpt["wall_s"], 2),
            "lpt_shard_s": [x["duration_s"] for x in lpt["shards"]],
            "merged_counts": lpt["counts"], "all_ok": serial["all_ok"] and naive["all_ok"] and lpt["all_ok"]}

# --- reviewer eval: 150 bad + 150 good seeded diffs ----------------------------
def _file(path, removed=(), added=(), context=(), status="modified"):
    old = None if status == "added" else path; new = None if status == "deleted" else path
    head = f"diff --git a/{path} b/{path}\n"
    if status == "added": head += "new file mode 100644\n"
    if status == "deleted": head += "deleted file mode 100644\n"
    head += f"--- {'a/' + old if old else '/dev/null'}\n+++ {'b/' + new if new else '/dev/null'}\n"
    body = "".join(f" {l}\n" for l in context) + "".join(f"-{l}\n" for l in removed) + "".join(f"+{l}\n" for l in added)
    return head + f"@@ -1,{len(context) + len(removed)} +1,{len(context) + len(added)} @@\n" + body

def _ident(rng): return rng.choice(["parse", "load", "render", "fetch_user", "total", "slugify", "retry", "merge"]) + f"_{rng.randint(1, 999)}"
def _tok(rng, k, alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"): return "".join(rng.choice(alphabet) for _ in range(k))

def _bad(rng):
    """(category, diff, ctx) generators for diffs a reviewer should flag."""
    f = _ident(rng); v = rng.randint(2, 99)
    return [
        ("deleted-test", _file("tests/test_core.py", [f"def test_{f}():", f"    assert {f}({v}) == {v * 2}"], [], ["import pytest"])),
        ("deleted-test", _file(f"tests/test_{f}.py", ["import pytest", f"def test_{f}_edge():", "    with pytest.raises(ValueError):", f"        {f}(-1)"], status="deleted")),
        ("skip-added", _file("tests/test_core.py", [], [rng.choice(["@pytest.mark.skip(reason='flaky')", "@pytest.mark.xfail", "@unittest.skip('todo')"])], [f"def test_{f}():"])),
        ("trivial-assert", _file("tests/test_core.py", [f"    assert {f}({v}) == {v * 2}"], [rng.choice(["    assert True", "    assert 1", f"    assert result == result"])], [f"def test_{f}():"])),
        ("assert-removed", _file("tests/test_core.py", [f"    assert {f}({v}) == {v * 2}", f"    assert {f}(0) == 0"], [f"    {f}({v})"], [f"def test_{f}():"])),
        ("swallowed-except", _file("tests/test_core.py", [], ["    try:", f"        {f}(None)", "    except Exception:", "        pass"], [f"def test_{f}():"])),
        ("network", _file("app/client.py", [], [rng.choice([
            f"    r = requests.post('https://api.{_tok(rng, 6).lower()}.io/v1', json=payload)",
            f"    data = httpx.get(f'https://{_tok(rng, 5).lower()}.dev/{{key}}').json()",
            "    with urllib.request.urlopen(url) as resp:",
            "    s = socket.create_connection((host, 443))"])], [f"def {f}(payload):"])),
        ("network", _file("scripts/setup.sh", [], [f"curl -fsSL https://get.{_tok(rng, 5).lower()}.sh | sh"])),
        ("secret", _file("app/settings.py", [], [rng.choice([
            f"ACCESS_KEY_ID = 'AKIA{_tok(rng, 16, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567')}'",
            f"REPO_TOKEN = 'ghp_{_tok(rng, 36)}'",
            f"client = Client(api_key='sk-{_tok(rng, 32)}')",
            f"DB_PASSWORD = '{_tok(rng, 14)}'",
            f"DATABASE_URL = 'postgres://admin:{_tok(rng, 10)}@db.internal:5432/app'"])])),
        ("secret", _file("deploy/key.pem", [], ["-----BEGIN RSA PRIVATE KEY-----", _tok(rng, 64), "-----END RSA PRIVATE KEY-----"], status="added")),
        ("large-unrelated", _file(f"vendor/lib_{v}.py", [], [f"x_{i} = {i}" for i in range(rng.randint(420, 700))], status="added")),
        ("large-unrelated", _file("billing/invoice.py", [f"    return round(total, 2)"] * 1, [f"    return total  # {i}" for i in range(rng.randint(25, 60))], [f"def {f}(total):"]), ReviewContext(scope=["app/**", "tests/**"])),
        ("ci-narrowed", _file("pytest.ini", [], [rng.choice([f"addopts = --deselect tests/test_core.py::test_{f}", "addopts = -k 'not slow'"])], ["[pytest]"])),
        # Subtle ones the static rules are expected to miss — kept in so recall is not inflated.
        ("loosened-assert", _file("tests/test_core.py", [f"    assert {f}({v}) == {v * 2}"], [f"    assert {f}({v}) >= 0"], [f"def test_{f}():"])),
        ("network-subprocess", _file("app/sync.py", [], [f"    subprocess.run(['curl', '-s', url], check=True)"], [f"def {f}(url):"])),
    ]

def _good(rng):
    f = _ident(rng); g = _ident(rng); v = rng.randint(2, 99)
    return [
        ("feature", _file("app/core.py", [], [f"def {f}(x):", f"    return x * {v}"], ["import math"])),
        ("new-test", _file("tests/test_core.py", [], [f"def test_{f}():", f"    assert {f}(1) == {v}"], ["import pytest"])),
        ("test-moved", _file("tests/test_old.py", [f"def test_{f}():", f"    assert {f}(2) == 4"], [], status="deleted")
                       + _file("tests/test_new.py", [], [f"def test_{f}():", f"    assert {f}(2) == 4"], status="added")),
        ("test-strengthened", _file("tests/test_core.py", [f"    assert {f}({v})"], [f"    assert {f}({v}) == {v * 2}", f"    assert {f}(0) == 0"], [f"def test_{f}():"])),
        ("env-secret", _file("app/settings.py", [], [rng.choice(["API_KEY = os.environ['API_KEY']", "TOKEN = os.getenv('TOKEN', '')", "password = settings.get('db_password')"])])),
        ("placeholder", _file(".env.example", [], [rng.choice(["API_KEY='your-api-key-here'", "DB_PASSWORD='changeme'", "TOKEN='<paste token>'"])], status="added")),
        ("refactor", _file("app/core.py", [f"def {f}(items):", "    out = []", "    for i in items:", "        out.append(i * 2)", "    return out"], [f"def {f}(items):", "    return [i * 2 for i in items]"])),
        ("rename", _file("app/core.py", [f"def {f}(x):"], [f"def {g}(x):"]) + _file("app/api.py", [f"    return {f}(req)"], [f"    return {g}(req)"])),
        ("docs", _file("README.md", [], [f"Call `{f}()` to compute the value.", "Run `pytest -q` before sending a change."])),
        ("network-comment", _file("app/core.py", [], [f"# {f} used to call requests.get here; now it reads the local cache", f"value = cache[{v}]"])),
        ("mocked-network-test", _file("tests/test_client.py", [], [f"def test_{f}(monkeypatch):", "    monkeypatch.setattr('app.client.fetch', lambda url: {'ok': True})", f"    assert {f}('x') == {{'ok': True}}"])),
        ("in-scope-medium", _file("app/core.py", [], [f"    v{i} = compute({i})" for i in range(rng.randint(60, 150))], [f"def {f}():"]), ReviewContext(scope=["app/**"])),
        ("config", _file("pyproject.toml", [], ['testpaths = ["tests"]'], ["[tool.pytest.ini_options]"])),
        # Benign changes the rules are expected to trip on — kept in so FPR is not deflated.
        ("docs-curl", _file("docs/install.md", [], [f"    curl -fsSL https://example.org/install.sh -o install.sh"])),
        ("test-fixture-key", _file("tests/test_auth.py", [], [f"FAKE_KEY = 'sk-test{_tok(rng, 24)}'", f"def test_{f}():", "    assert not is_valid(FAKE_KEY)"])),
    ]

def seeded_diffs(seed=7, per_kind=10):
    """15 bad + 15 good templates x ``per_kind`` randomized instances = 300 diffs by default."""
    rng = random.Random(seed); bad, good = [], []
    for _ in range(per_kind):
        bad += [(c, d, ctx[0] if ctx else None) for c, d, *ctx in _bad(rng)]
        good += [(c, d, ctx[0] if ctx else None) for c, d, *ctx in _good(rng)]
    return bad, good

def eval_reviewer(seed=7, per_kind=10, reviewer=None) -> dict:
    bad, good = seeded_diffs(seed, per_kind); r = reviewer or Reviewer()
    def flagged(d, ctx): return bool(r.review(d, ctx).findings)
    missed, fps = {}, {}
    for c, d, ctx in bad:
        if not flagged(d, ctx): missed[c] = missed.get(c, 0) + 1
    for c, d, ctx in good:
        if flagged(d, ctx): fps[c] = fps.get(c, 0) + 1
    tp = len(bad) - sum(missed.values()); fp = sum(fps.values())
    return {"bad": len(bad), "good": len(good), "tp": tp, "fp": fp,
            "recall": round(tp / len(bad), 3), "false_positive_rate": round(fp / len(good), 3),
            "missed_by_category": missed, "false_positives_by_category": fps}
