"""Tiny launcher that applies resource limits, then execs the real command.

Used by SubprocessSandbox instead of ``preexec_fn`` (which is not safe to
use from a multi-threaded parent). Invoked as:

    python -m agentfleet._limits '<json limits>' -- cmd arg...
"""

from __future__ import annotations

import json
import os
import sys


def apply(limits: dict) -> list[str]:
    """Apply what the current OS allows. Returns names of limits applied."""
    applied: list[str] = []
    try:
        import resource
    except ImportError:  # Windows
        return applied

    mem_mb = limits.get("memory_mb")
    if mem_mb:
        nbytes = int(mem_mb) * 1024 * 1024
        # RLIMIT_AS is enforced on Linux; macOS accepts or rejects it silently.
        for name in ("RLIMIT_AS", "RLIMIT_DATA"):
            lim = getattr(resource, name, None)
            if lim is None:
                continue
            try:
                resource.setrlimit(lim, (nbytes, nbytes))
                applied.append(name)
                break
            except (ValueError, OSError):
                continue

    cpu_seconds = limits.get("cpu_seconds")
    if cpu_seconds:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (int(cpu_seconds), int(cpu_seconds) + 1))
            applied.append("RLIMIT_CPU")
        except (ValueError, OSError):
            pass

    cpus = limits.get("cpus")
    if cpus and hasattr(os, "sched_setaffinity"):
        try:
            available = sorted(os.sched_getaffinity(0))
            os.sched_setaffinity(0, set(available[: max(1, int(cpus))]))
            applied.append("affinity")
        except OSError:
            pass
    return applied


def main(argv: list[str]) -> None:
    if len(argv) < 3 or argv[1] != "--":
        sys.stderr.write("usage: python -m agentfleet._limits JSON -- cmd...\n")
        raise SystemExit(2)
    limits = json.loads(argv[0])
    apply(limits)
    cmd = argv[2:]
    try:
        os.execvp(cmd[0], cmd)
    except FileNotFoundError:
        sys.stderr.write(f"agentfleet: command not found: {cmd[0]}\n")
        raise SystemExit(127)


if __name__ == "__main__":
    main(sys.argv[1:])
