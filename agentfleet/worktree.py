"""Git worktree management: one branch + worktree per agent task."""

from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

AGENT_IDENT = ["-c", "user.name=agent-fleet", "-c", "user.email=agent-fleet@localhost"]


class GitError(RuntimeError):
    pass


def git(args: list[str], cwd: str | Path, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


@dataclass
class Worktree:
    task_id: str
    path: Path
    branch: str
    base_sha: str


class WorktreeManager:
    """Creates worktrees under ``<repo>/.fleet/worktrees/<task_id>``.

    Git operations that touch the shared ``.git`` directory are serialized
    with a lock, because concurrent ``git worktree add`` calls race on
    ``.git/worktrees`` and the index lock.
    """

    def __init__(self, repo: str | Path, root: str | Path | None = None, branch_prefix: str = "fleet/"):
        self.repo = Path(repo).resolve()
        if not (self.repo / ".git").exists():
            raise GitError(f"{self.repo} is not a git repository")
        self.root = Path(root).resolve() if root else self.repo / ".fleet" / "worktrees"
        self.prefix = branch_prefix
        self._lock = threading.Lock()

    def head(self) -> str:
        return git(["rev-parse", "HEAD"], self.repo).strip()

    def current_branch(self) -> str:
        return git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo).strip()

    def create(self, task_id: str, base: str = "HEAD") -> Worktree:
        with self._lock:
            base_sha = git(["rev-parse", base], self.repo).strip()
            path = self.root / task_id
            branch = f"{self.prefix}{task_id}"
            if path.exists():
                self._remove_locked(path, branch)
            self.root.mkdir(parents=True, exist_ok=True)
            # Keep fleet state out of the user's `git status`.
            fleet_dir = self.repo / ".fleet"
            if self.root.is_relative_to(fleet_dir) and not (fleet_dir / ".gitignore").exists():
                (fleet_dir / ".gitignore").write_text("*\n")
            git(["worktree", "add", "-B", branch, str(path), base_sha], self.repo)
            return Worktree(task_id, path, branch, base_sha)

    def commit_all(self, wt: Worktree, message: str) -> bool:
        """Commit everything the agent changed. Returns False if nothing changed."""
        git(["add", "-A"], wt.path)
        if not git(["status", "--porcelain"], wt.path).strip():
            return False
        git([*AGENT_IDENT, "commit", "-q", "--no-verify", "-m", message], wt.path)
        return True

    def diff(self, wt: Worktree) -> str:
        return git(["diff", "--no-color", "--no-ext-diff", f"{wt.base_sha}..HEAD"], wt.path)

    def merge(self, wt: Worktree, message: str | None = None) -> str:
        """Merge the task branch into whatever branch the main repo has checked out."""
        with self._lock:
            msg = message or f"Merge agent task {wt.task_id}"
            git([*AGENT_IDENT, "merge", "--no-ff", "-q", "-m", msg, wt.branch], self.repo)
            return git(["rev-parse", "HEAD"], self.repo).strip()

    def reset(self, wt: Worktree) -> None:
        """Throw away agent changes so the task can be retried from the base commit."""
        git(["reset", "-q", "--hard", wt.base_sha], wt.path)
        git(["clean", "-fdq"], wt.path)

    def remove(self, wt: Worktree) -> None:
        with self._lock:
            self._remove_locked(wt.path, wt.branch)

    def _remove_locked(self, path: Path, branch: str) -> None:
        git(["worktree", "remove", "--force", str(path)], self.repo, check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        git(["worktree", "prune"], self.repo, check=False)
        git(["branch", "-D", branch], self.repo, check=False)
