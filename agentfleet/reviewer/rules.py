"""Static rules that flag risky agent diffs.

Each rule takes the parsed diff plus a :class:`ReviewContext` and yields
:class:`Finding` objects. Rules work on added/removed lines only; they do
not execute or import the code.
"""

from __future__ import annotations

import fnmatch
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .diff import FileDiff

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3}


@dataclass
class Finding:
    rule: str
    severity: str
    path: str
    message: str
    line: int | None = None

    def to_dict(self) -> dict:
        return {"rule": self.rule, "severity": self.severity, "path": self.path, "line": self.line, "message": self.message}


@dataclass
class ReviewContext:
    # Glob patterns the task is expected to touch. Empty means "unknown".
    scope: list[str] = field(default_factory=list)
    max_changed_lines: int = 400
    max_files: int = 20


Rule = Callable[[list[FileDiff], ReviewContext], Iterable[Finding]]


def is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or "/tests/" in f"/{path}"
        or name.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts"))
    )


_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)|^\s*(?:it|test)\(\s*['\"]([^'\"]+)")
_ASSERT = re.compile(r"^\s*(assert\b|self\.assert\w*\(|expect\(|pytest\.raises\()")
_SKIP = re.compile(r"@pytest\.mark\.(skip|xfail)\b|pytest\.skip\(|unittest\.skip|\.skip\(|xit\(|@skip\b")
_TRIVIAL_ASSERT = re.compile(r"^\s*assert\s+(True|1|not\s+False)\s*(#.*)?$|^\s*assert\s+(\w+)\s*==\s*\3\s*$")
_EXCEPT_SWALLOW = re.compile(r"^\s*except(\s+Exception|\s+BaseException)?\s*:\s*(pass)?\s*$")


def _test_names(lines) -> Counter:
    c: Counter = Counter()
    for l in lines:
        m = _TEST_DEF.match(l.text)
        if m:
            c[m.group(1) or m.group(2)] += 1
    return c


def rule_deleted_tests(files: list[FileDiff], ctx: ReviewContext) -> Iterable[Finding]:
    # Tests that move between files are fine, so compare names across the whole diff.
    removed = Counter()
    added = Counter()
    for f in files:
        if not is_test_path(f.path) and not (f.old_path and is_test_path(f.old_path)):
            continue
        removed += _test_names(f.removed)
        added += _test_names(f.added)
    gone = removed - added
    if not gone:
        return
    for f in files:
        names = [n for n in _test_names(f.removed) if n in gone]
        if not names:
            continue
        if f.status == "deleted":
            yield Finding("deleted-test", "high", f.path, f"test file deleted ({len(names)} tests: {', '.join(names[:5])})")
        else:
            yield Finding("deleted-test", "high", f.path, f"tests removed: {', '.join(names[:5])}")


def rule_weakened_assertions(files: list[FileDiff], ctx: ReviewContext) -> Iterable[Finding]:
    for f in files:
        if not is_test_path(f.path) or f.status == "deleted":
            continue
        for l in f.added:
            if _SKIP.search(l.text):
                yield Finding("weakened-assertion", "high", f.path, f"test skipped or xfailed: {l.text.strip()}", l.new_lineno)
            elif _TRIVIAL_ASSERT.match(l.text):
                yield Finding("weakened-assertion", "high", f.path, f"trivial assertion: {l.text.strip()}", l.new_lineno)
            elif _EXCEPT_SWALLOW.match(l.text):
                yield Finding("weakened-assertion", "medium", f.path, "exception swallowed inside a test", l.new_lineno)
        rem = sum(1 for l in f.removed if _ASSERT.match(l.text))
        add = sum(1 for l in f.added if _ASSERT.match(l.text))
        # Removing assertions without adding as many, beyond what deleted tests explain.
        removed_tests = sum(_test_names(f.removed).values())
        added_tests = sum(_test_names(f.added).values())
        if rem > add and removed_tests <= added_tests:
            yield Finding("weakened-assertion", "medium", f.path, f"assertions reduced from {rem} to {add} in changed hunks")


_NETWORK = [
    (re.compile(r"\brequests\.(get|post|put|patch|delete|head|request|Session)\b"), "requests"),
    (re.compile(r"\bhttpx\.(get|post|put|patch|delete|stream|request|Client|AsyncClient)\b"), "httpx"),
    (re.compile(r"\burllib\.request\b|\burlopen\("), "urllib"),
    (re.compile(r"\bhttp\.client\b|HTTPS?Connection\("), "http.client"),
    (re.compile(r"\bsocket\.(socket|create_connection)\("), "socket"),
    (re.compile(r"\baiohttp\.(ClientSession|request)\b"), "aiohttp"),
    (re.compile(r"\b(curl|wget)\s+(-\S+\s+)*['\"]?https?://"), "curl/wget"),
    (re.compile(r"\bfetch\(\s*['\"`]https?://"), "fetch"),
    (re.compile(r"\b(smtplib\.SMTP|ftplib\.FTP|paramiko\.SSHClient)\b"), "remote protocol"),
]
_NET_IMPORT = re.compile(r"^\s*(import|from)\s+(requests|httpx|aiohttp|urllib3|socket|paramiko|websockets?)\b")


def rule_network_calls(files: list[FileDiff], ctx: ReviewContext) -> Iterable[Finding]:
    for f in files:
        removed_text = {l.text.strip() for l in f.removed}
        for l in f.added:
            if l.text.strip() in removed_text:
                continue  # moved line, not new
            if l.text.lstrip().startswith("#"):
                continue
            for pat, label in _NETWORK:
                if pat.search(l.text):
                    yield Finding("network-call", "medium", f.path, f"new network call ({label}): {l.text.strip()[:120]}", l.new_lineno)
                    break
            else:
                m = _NET_IMPORT.match(l.text)
                if m:
                    yield Finding("network-call", "low", f.path, f"imports network library {m.group(2)}", l.new_lineno)


_SECRETS = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY"), "private key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "GitHub token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "API secret key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    (re.compile(r"(?i)\b[a-z0-9_]*(api[_-]?key|secret|password|passwd|token|auth)[a-z0-9_]*\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"), "hard-coded credential"),
    (re.compile(r"(?i)\b(postgres|mysql|mongodb(\+srv)?|redis|amqp)://[^:\s/]+:[^@\s]+@"), "credentials in connection URL"),
]
_PLACEHOLDER = re.compile(r"(?i)(changeme|example|placeholder|dummy|xxxx|your[_-]|<[^>]+>|\$\{|os\.environ|getenv|redacted|\*\*\*)")


def rule_secrets(files: list[FileDiff], ctx: ReviewContext) -> Iterable[Finding]:
    for f in files:
        for l in f.added:
            for pat, label in _SECRETS:
                m = pat.search(l.text)
                if not m:
                    continue
                if label in ("hard-coded credential", "credentials in connection URL") and _PLACEHOLDER.search(m.group(0)):
                    continue
                yield Finding("secret", "high", f.path, f"possible {label}", l.new_lineno)
                break


def rule_large_or_unrelated(files: list[FileDiff], ctx: ReviewContext) -> Iterable[Finding]:
    changed = sum(len(f.added) + len(f.removed) for f in files)
    if changed > ctx.max_changed_lines:
        yield Finding("large-edit", "medium", "*", f"{changed} changed lines (limit {ctx.max_changed_lines})")
    if len(files) > ctx.max_files:
        yield Finding("large-edit", "medium", "*", f"{len(files)} files changed (limit {ctx.max_files})")
    if ctx.scope:
        outside = [f for f in files if not any(fnmatch.fnmatch(f.path, g) for g in ctx.scope)]
        for f in outside:
            n = len(f.added) + len(f.removed)
            sev = "medium" if n >= 20 else "low"
            yield Finding("unrelated-edit", sev, f.path, f"outside task scope {ctx.scope} ({n} lines)")


_LOCKFILES = ("package-lock.json", "yarn.lock", "poetry.lock", "uv.lock", "Cargo.lock", "pnpm-lock.yaml")


def rule_ci_and_config(files: list[FileDiff], ctx: ReviewContext) -> Iterable[Finding]:
    for f in files:
        p = f.path
        if p.startswith(".github/workflows/") or p in (".gitlab-ci.yml", "Jenkinsfile"):
            yield Finding("ci-change", "medium", p, "CI pipeline changed")
        elif p.endswith(("conftest.py", "pytest.ini", "setup.cfg", "tox.ini")) or p.endswith("pyproject.toml"):
            for l in f.added:
                if re.search(r"(?i)(--deselect|-k\s+['\"]?not|ignore\s*=|addopts.*-p\s+no:|collect_ignore)", l.text):
                    yield Finding("weakened-assertion", "high", p, f"test collection narrowed: {l.text.strip()}", l.new_lineno)


DEFAULT_RULES: list[Rule] = [
    rule_deleted_tests,
    rule_weakened_assertions,
    rule_network_calls,
    rule_secrets,
    rule_large_or_unrelated,
    rule_ci_and_config,
]
