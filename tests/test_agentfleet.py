import subprocess, sys
from pathlib import Path
import pytest
from agentfleet.plugins import load_agent, ShellAgent
from agentfleet.reviewer import Reviewer
from agentfleet.sandbox import SandboxLimits, SubprocessSandbox, get_sandbox
from agentfleet.sharder import discover, shard
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
    (tmp_path / "test_a.py").write_text("def test_x():\n pass\ndef helper():\n pass\n")
    assert discover(tmp_path) == ["test_a.py::test_x"]

BAD = "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1,2 +1,1 @@\n-def test_x():\n-    assert 1 == 1\n+value = 1\n"
@pytest.mark.parametrize("diff,rules", [
    (BAD, {"deleted-test"}),
    ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x=1\n+import requests; requests.get('https://x.com')\n", {"network-call"}),
    ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x=1\n+KEY='sk-aaaaaaaaaaaaaaaaaaaaaaaa'\n", {"secret"}),
])
def test_reviewer_flags(diff, rules):
    assert rules <= {f.rule for f in Reviewer().review(diff).findings}

def test_reviewer_clean():
    d = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x=1\n+def f():\n+    return 1\n"
    assert Reviewer().review(d).findings == []

def test_reviewer_eval_quality():
    r = eval_reviewer(); assert r["recall"] >= 0.9 and r["false_positive_rate"] <= 0.1, r

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
    assert c.post(f"/api/tasks/{tid}/approve").status_code == 200
