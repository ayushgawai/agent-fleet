"""Agent plugin interface.

An agent is anything that, given a task prompt and a working directory,
edits files. The fleet only needs to know which command to run and with
which environment; the agent process runs inside the sandbox with its
working directory set to the task's worktree.

The prompt is always passed through the ``FLEET_PROMPT`` environment
variable (and ``FLEET_TASK_ID``), never interpolated into a shell string,
so prompts cannot inject shell syntax.

Ways to plug in an agent:

* ``shell:<command>``: run ``sh -c <command>``. The command reads
  ``$FLEET_PROMPT``. Good for scripts and CLIs (``shell:aider --message "$FLEET_PROMPT" --yes``).
* ``exec:<prog> <args...>``: run a program directly, args split with shlex.
* ``module.path:ClassName``: import a class implementing :class:`Agent`.
* A name registered under the ``agentfleet.agents`` entry-point group.
"""

from __future__ import annotations

import importlib
import shlex
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Protocol, runtime_checkable


@dataclass
class AgentInvocation:
    cmd: list[str]
    env: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class Agent(Protocol):
    name: str

    def invocation(self, task_id: str, prompt: str) -> AgentInvocation: ...


def _base_env(task_id: str, prompt: str) -> dict[str, str]:
    return {"FLEET_TASK_ID": task_id, "FLEET_PROMPT": prompt}


class ShellAgent:
    """Runs ``sh -c <script>`` with the prompt in ``$FLEET_PROMPT``."""

    def __init__(self, script: str, name: str = "shell", env: dict[str, str] | None = None):
        self.script = script
        self.name = name
        self.env = env or {}

    def invocation(self, task_id: str, prompt: str) -> AgentInvocation:
        return AgentInvocation(["sh", "-c", self.script], {**self.env, **_base_env(task_id, prompt)})


class ExecAgent:
    """Runs a program directly. ``{prompt}`` in an argument is replaced by the prompt as one argv item."""

    def __init__(self, argv: list[str], name: str = "exec", env: dict[str, str] | None = None):
        if not argv:
            raise ValueError("ExecAgent needs a program")
        self.argv = argv
        self.name = name
        self.env = env or {}

    def invocation(self, task_id: str, prompt: str) -> AgentInvocation:
        cmd = [prompt if a == "{prompt}" else a for a in self.argv]
        return AgentInvocation(cmd, {**self.env, **_base_env(task_id, prompt)})


def load_agent(spec: str) -> Agent:
    """Build an agent from a spec string (see module docstring)."""
    if spec.startswith("shell:"):
        return ShellAgent(spec[len("shell:"):])
    if spec.startswith("exec:"):
        return ExecAgent(shlex.split(spec[len("exec:"):]))
    if ":" in spec and not spec.startswith(("/", ".")):
        mod_name, _, attr = spec.partition(":")
        obj = getattr(importlib.import_module(mod_name), attr)
        agent = obj() if isinstance(obj, type) else obj
        if not isinstance(agent, Agent):
            raise TypeError(f"{spec} does not implement the Agent protocol")
        return agent
    for ep in entry_points(group="agentfleet.agents"):
        if ep.name == spec:
            obj = ep.load()
            return obj() if isinstance(obj, type) else obj
    raise ValueError(f"unknown agent spec: {spec!r}")
