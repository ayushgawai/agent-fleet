"""Fleet orchestrator: one worktree + one sandbox per task, human gate before merge."""
from __future__ import annotations
import threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from .plugins import load_agent
from .reviewer import ReviewContext, Reviewer
from .sandbox import SandboxLimits, get_sandbox
from .worktree import WorktreeManager

@dataclass
class Task:
    prompt: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    agent: str = "shell:printf 'no agent configured\\n'"
    status: str = "queued"  # queued|running|awaiting_review|approved|merged|failed|discarded
    logs: list[str] = field(default_factory=list)
    verdict: dict | None = None
    branch: str = ""; worktree: str = ""; error: str = ""

class Fleet:
    def __init__(self, repo, sandbox_kind="auto", limits=None, reviewer=None):
        self.repo = Path(repo); self.wm = WorktreeManager(self.repo)
        self.sandbox_kind = sandbox_kind; self.limits = limits or SandboxLimits()
        self.reviewer = reviewer or Reviewer(); self.tasks: dict[str, Task] = {}
        self._wts = {}; self._lock = threading.Lock()
    def submit(self, prompt, agent="shell:printf 'no agent configured\\n'") -> Task:
        t = Task(prompt=prompt, agent=agent); self.tasks[t.id] = t; return t
    def run(self, task: Task) -> Task:
        task.status = "running"
        try:
            wt = self.wm.create(task.id); self._wts[task.id] = wt
            task.branch, task.worktree = wt.branch, str(wt.path)
            inv = load_agent(task.agent).invocation(task.id, task.prompt)
            sb = get_sandbox(self.sandbox_kind, self.limits)
            r = sb.run(inv.cmd, cwd=wt.path, env=inv.env, on_line=lambda l: task.logs.append(l))
            task.logs.append(r.output if not task.logs else "")
            if not r.ok: task.status, task.error = "failed", f"agent exit {r.returncode}"; return task
            self.wm.commit_all(wt, f"agent({task.id}): {task.prompt[:60]}")
            v = self.reviewer.review(self.wm.diff(wt)); task.verdict = v.to_dict()
            task.status = "awaiting_review"
        except Exception as e:  # keep fleet alive per-task
            task.status, task.error = "failed", str(e)
        return task
    def run_many(self, tasks, workers=4):
        with ThreadPoolExecutor(max_workers=workers) as ex: return list(ex.map(self.run, tasks))
    def approve(self, tid):
        t = self.tasks[tid]
        if t.verdict and t.verdict["blocked"]: raise ValueError("review blocked: high-severity findings")
        self.wm.merge(self._wts[tid]); t.status = "merged"; return t
    def discard(self, tid):
        t = self.tasks[tid]; wt = self._wts.pop(tid, None)
        if wt: self.wm.remove(wt)
        t.status = "discarded"; return t
    def retry(self, tid):
        t = self.tasks[tid]; wt = self._wts.get(tid)
        if wt: self.wm.reset(wt)
        t.logs.clear(); t.status = "queued"; return self.run(t)
