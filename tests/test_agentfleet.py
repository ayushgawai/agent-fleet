import subprocess, sys
from pathlib import Path
import pytest
from agentfleet.plugins import load_agent, ShellAgent
from agentfleet.reviewer import Reviewer
from agentfleet.sandbox import DockerSandbox, SandboxLimits, SubprocessSandbox, get_sandbox
from agentfleet.sharder import discover, load_durations, run_shards, shard, shard_contiguous
from agentfleet.bench import eval_reviewer, seeded_diffs
from agentfleet.fleet import Fleet
from agentfleet.worktree import WorktreeManager

def test_sandbox_runs_and_limits():
    r = SubprocessSandbox(SandboxLimits(timeout_s=10)).run([sys.executable, "-c", "print('hi')"], cwd=".")
    assert r.ok and "hi" in r.output

def test_sandbox_timeout():
    r = SubprocessSandbox(SandboxLimits(timeout_s=1)).run([sys.executable, "-c", "import time; time.sleep(5)"], cwd=".")
    assert r.timed_out

def test_plugins():
    inv = load_agent("shell:echo $FLEET_PROMPT").invocation("t1", "hello")
    assert inv.cmd[:2] == ["sh", "-c"] and inv.env["FLEET_PROMPT"] == "hello"
    assert load_agent("exec:echo {prompt}").invocation("t", "hi").cmd == ["echo", "hi"]

def test_shard_balances_by_duration():
    nodes = [f"t::test_{i}" for i in range(4)]
    groups = shard(nodes, {"t::test_0": 10, "t::test_1": 1, "t::test_2": 1, "t::test_3": 1}, 2)
    assert sorted(len(g) for g in groups) == [1, 3]  # heavy test alone

def test_discover(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_x():\n pass\ndef helper():\n pass\nclass TestK:\n def test_m(self):\n  pass\n")
    assert discover(tmp_path) == ["test_a.py::test_x", "test_a.py::TestK::test_m"]

def test_contiguous_baseline():
    assert shard_contiguous(list("abcdefg"), 3) == [list("abc"), list("def"), list("g")]

def test_run_shards_merges_junit(tmp_path):
    (tmp_path / "test_a.py").write_text("import pytest\ndef test_ok():\n assert 1\ndef test_bad():\n assert 0\n@pytest.mark.parametrize('x', [1, 2])\ndef test_p(x):\n assert x\n")
    (tmp_path / "test_b.py").write_text("class TestK:\n def test_m(self):\n  assert 1\n")
    hist = tmp_path / "hist.json"
    r = run_shards(tmp_path, n=2, save_durations=hist)
    assert r["counts"] == {"passed": 4, "failed": 1} and r["failed"] == ["test_a.py::test_bad"]
    assert not r["all_ok"] and len(r["shards"]) == 2
    assert set(load_durations(hist)) == set(discover(tmp_path))  # history keys line up with shard keys

def test_docker_args_enforce_limits():
    args = DockerSandbox(SandboxLimits(cpus=2, memory_mb=512)).build_args(["pytest"], ".", {"A": "1"})
    joined = " ".join(args)
    assert "--cpus 2" in joined and "--memory 512m" in joined and "--network none" in joined and "-e A=1" in joined

BAD = "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1,2 +1,1 @@\n-def test_x():\n-    assert 1 == 1\n+value = 1\n"
@pytest.mark.parametrize("diff,rules", [
    (BAD, {"deleted-test"}),
    ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x=1\n+import requests; requests.get('https://x.com')\n", {"network-call"}),
    ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x=1\n+KEY='sk-aaaaaaaaaaaaaaaaaaaaaaaa'\n", {"secret"}),
    ("diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1,2 +1,2 @@\n def test_x():\n-    assert f(2) == 4\n+    assert f(2) >= 0\n", {"weakened-assertion"}),
    ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x=1\n+subprocess.run(['curl', url])\n", {"network-call"}),
])
def test_reviewer_flags(diff, rules):
    assert rules <= {f.rule for f in Reviewer().review(diff).findings}

def test_reviewer_clean():
    d = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x=1\n+def f():\n+    return 1\n"
    assert Reviewer().review(d).findings == []

def test_reviewer_eval_quality():
    r = eval_reviewer()
    assert r["bad"] == r["good"] == 150
    assert r["recall"] >= 0.95 and r["false_positive_rate"] <= 0.1, r

def _repo(tmp_path):
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=tmp_path)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], cwd=tmp_path)
    return tmp_path

def test_worktree_fleet_flow(tmp_path):
    repo = _repo(tmp_path)
    f = Fleet(repo, sandbox_kind="subprocess")
    t = f.submit("write file", "shell:printf hi > OUT.md"); f.run(t)
    assert t.status == "awaiting_review" and t.branch.startswith("fleet/")
    f.approve(t.id); assert t.status == "merged" and (repo / "OUT.md").exists()
    t2 = f.submit("bad", "shell:printf hi > B.md"); f.run(t2); f.discard(t2.id); assert t2.status == "discarded"
    with pytest.raises(ValueError): f.approve(t2.id)

def test_parallel_tasks_get_separate_worktrees(tmp_path):
    f = Fleet(_repo(tmp_path), sandbox_kind="subprocess")
    ts = f.run_many([f.submit(f"t{i}", "shell:echo $FLEET_TASK_ID > ID.txt") for i in range(3)], workers=3)
    assert [t.status for t in ts] == ["awaiting_review"] * 3
    assert len({t.worktree for t in ts}) == 3
    assert all((Path(t.worktree) / "ID.txt").read_text().strip() == t.id for t in ts)

def test_blocked_review_cannot_merge(tmp_path):
    f = Fleet(_repo(tmp_path), sandbox_kind="subprocess")
    t = f.run(f.submit("leak", "shell:echo \"KEY='sk-aaaaaaaaaaaaaaaaaaaaaaaaa'\" > k.py"))
    assert t.verdict["blocked"]
    with pytest.raises(ValueError): f.approve(t.id)

def test_web(tmp_path):
    from fastapi.testclient import TestClient
    from agentfleet import web
    web.set_fleet(Fleet(_repo(tmp_path), sandbox_kind="subprocess"))
    c = TestClient(web.app)
    assert c.get("/").status_code == 200 and "agent-fleet" in c.get("/").text
    tid = c.post("/api/tasks", json={"prompt": "x", "agent": "shell:printf ok > W.md"}).json()["id"]
    import time
    for _ in range(50):
        if c.get("/api/tasks").json()[0]["status"] == "awaiting_review": break
        time.sleep(0.2)
    assert c.get(f"/api/tasks/{tid}/logs").json()["logs"] is not None
    body = c.get(f"/api/tasks/{tid}/stream").text
    assert "event: status" in body and "awaiting_review" in body
    assert c.post(f"/api/tasks/{tid}/approve").status_code == 200
    assert c.post(f"/api/tasks/{tid}/approve").status_code == 409  # already merged
    assert c.get("/api/tasks/nope/logs").status_code == 404
