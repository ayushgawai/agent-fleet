"""Sandboxes that run one command with CPU, memory, network and time limits.

Two backends:

* ``DockerSandbox``: ``docker run --rm`` with ``--cpus``, ``--memory``,
  ``--pids-limit`` and ``--network none``. The working directory is
  bind-mounted at ``/work``. This is the real isolation boundary.
* ``SubprocessSandbox``: fallback when Docker is missing. Uses rlimits
  (memory, CPU seconds), CPU affinity on Linux, a wall-clock timeout and
  a new process group. Network is cut with ``unshare -rn`` when available
  (Linux with user namespaces); otherwise it only points proxy variables
  at a dead address, which is best effort and reported as not enforced.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

LineCallback = Callable[[str], None]


@dataclass
class SandboxLimits:
    cpus: float = 1.0
    memory_mb: int = 1024
    network: bool = False
    timeout_s: float = 600.0
    pids: int = 256


@dataclass
class SandboxResult:
    returncode: int
    output: str
    duration_s: float
    timed_out: bool = False
    backend: str = ""
    enforced: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class Sandbox:
    name = "base"

    def __init__(self, limits: SandboxLimits | None = None):
        self.limits = limits or SandboxLimits()

    def run(
        self,
        cmd: Sequence[str],
        cwd: str | Path,
        env: Mapping[str, str] | None = None,
        on_line: LineCallback | None = None,
    ) -> SandboxResult:
        raise NotImplementedError

    # Shared helper: run a Popen, stream merged output, enforce timeout.
    def _pump(self, popen_args: list[str], cwd, env, on_line, enforced) -> SandboxResult:
        start = time.monotonic()
        proc = subprocess.Popen(
            popen_args,
            cwd=str(cwd),
            env=dict(env) if env is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        chunks: list[str] = []
        timed_out = threading.Event()

        def killer() -> None:
            timed_out.set()
            _kill_group(proc)

        timer = threading.Timer(self.limits.timeout_s, killer)
        timer.daemon = True
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                chunks.append(line)
                if on_line:
                    on_line(line.rstrip("\n"))
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                _kill_group(proc)
        return SandboxResult(
            returncode=proc.returncode if not timed_out.is_set() else -9,
            output="".join(chunks),
            duration_s=time.monotonic() - start,
            timed_out=timed_out.is_set(),
            backend=self.name,
            enforced=enforced,
        )


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class SubprocessSandbox(Sandbox):
    name = "subprocess"

    def _can_unshare_net(self) -> bool:
        if not sys.platform.startswith("linux") or not shutil.which("unshare"):
            return False
        try:
            r = subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=5)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def run(self, cmd, cwd, env=None, on_line=None) -> SandboxResult:
        lim = self.limits
        # ``env`` holds extra variables layered on top of the host environment.
        base_env = dict(os.environ)
        base_env.update(env or {})
        enforced = {"timeout": True, "network": lim.network}
        prefix: list[str] = []
        if not lim.network:
            if self._can_unshare_net():
                prefix = ["unshare", "-rn"]
                enforced["network"] = "unshare"
            else:
                for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                    base_env[k] = "http://127.0.0.1:9"
                base_env["NO_PROXY"] = ""
                enforced["network"] = "proxy-blackhole (best effort)"
        limits_json = json.dumps(
            {
                "memory_mb": lim.memory_mb,
                # CPU seconds budget: cores x wall timeout, so a runaway loop dies.
                "cpu_seconds": max(1, int(lim.cpus * lim.timeout_s)),
                "cpus": lim.cpus,
            }
        )
        enforced["memory"] = "rlimit" if sys.platform.startswith("linux") else "rlimit (not enforced on macOS)"
        enforced["cpu"] = "affinity+rlimit" if sys.platform.startswith("linux") else "rlimit cpu-seconds"
        args = prefix + [sys.executable, "-m", "agentfleet._limits", limits_json, "--", *cmd]
        # Make sure the launcher module is importable from any cwd.
        pkg_root = str(Path(__file__).resolve().parent.parent)
        base_env["PYTHONPATH"] = pkg_root + (os.pathsep + base_env["PYTHONPATH"] if base_env.get("PYTHONPATH") else "")
        return self._pump(args, cwd, base_env, on_line, enforced)


class DockerSandbox(Sandbox):
    name = "docker"

    def __init__(self, limits: SandboxLimits | None = None, image: str = "python:3.12-slim"):
        super().__init__(limits)
        self.image = image

    @staticmethod
    def available() -> bool:
        if not shutil.which("docker"):
            return False
        try:
            r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def build_args(self, cmd: Sequence[str], cwd, env: Mapping[str, str] | None) -> list[str]:
        lim = self.limits
        args = [
            "docker", "run", "--rm", "-i",
            "--cpus", str(lim.cpus),
            "--memory", f"{lim.memory_mb}m",
            "--memory-swap", f"{lim.memory_mb}m",
            "--pids-limit", str(lim.pids),
            "--network", "bridge" if lim.network else "none",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "-v", f"{Path(cwd).resolve()}:/work",
            "-w", "/work",
        ]
        if hasattr(os, "getuid"):
            args += ["--user", f"{os.getuid()}:{os.getgid()}"]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        return args + [self.image, *cmd]

    def run(self, cmd, cwd, env=None, on_line=None) -> SandboxResult:
        # Only pass explicitly provided env, never the host environment.
        enforced = {"cpu": "cgroup", "memory": "cgroup", "network": "none" if not self.limits.network else "bridge", "timeout": True}
        return self._pump(self.build_args(cmd, cwd, env), cwd, None, on_line, enforced)


def get_sandbox(kind: str = "auto", limits: SandboxLimits | None = None, image: str = "python:3.12-slim") -> Sandbox:
    """Return a sandbox. ``auto`` picks Docker if the daemon answers."""
    if kind == "docker" or (kind == "auto" and DockerSandbox.available()):
        if kind == "docker" and not DockerSandbox.available():
            raise RuntimeError("docker requested but the daemon is not reachable")
        return DockerSandbox(limits, image=image)
    if kind not in ("auto", "subprocess"):
        raise ValueError(f"unknown sandbox kind: {kind}")
    return SubprocessSandbox(limits)
