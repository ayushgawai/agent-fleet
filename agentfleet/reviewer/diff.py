"""Minimal unified-diff parser (git style and plain ``diff -u``)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")


@dataclass
class Line:
    kind: str  # "+", "-" or " "
    text: str
    new_lineno: int | None
    old_lineno: int | None


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None
    lines: list[Line] = field(default_factory=list)
    binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or "?"

    @property
    def status(self) -> str:
        if self.old_path is None:
            return "added"
        if self.new_path is None:
            return "deleted"
        if self.old_path != self.new_path:
            return "renamed"
        return "modified"

    @property
    def added(self) -> list[Line]:
        return [l for l in self.lines if l.kind == "+"]

    @property
    def removed(self) -> list[Line]:
        return [l for l in self.lines if l.kind == "-"]


def _strip_prefix(p: str) -> str | None:
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        return p[2:]
    return p


def parse_diff(text: str) -> list[FileDiff]:
    """Parse a unified diff. Hunk line counts decide where a hunk ends, so
    content lines that look like headers (``--- x``) are handled correctly."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    old_left = new_left = 0
    old_no = new_no = 0
    for raw in text.splitlines():
        if old_left > 0 or new_left > 0:
            tag, body = (raw[:1], raw[1:]) if raw else (" ", "")
            if tag == "+":
                cur.lines.append(Line("+", body, new_no, None))
                new_no += 1
                new_left -= 1
            elif tag == "-":
                cur.lines.append(Line("-", body, None, old_no))
                old_no += 1
                old_left -= 1
            elif tag == " ":
                cur.lines.append(Line(" ", body, new_no, old_no))
                old_no += 1
                new_no += 1
                old_left -= 1
                new_left -= 1
            # "\ No newline at end of file" does not consume a count
            continue

        m = _GIT_HEADER.match(raw)
        if m:
            cur = FileDiff(m.group(1), m.group(2))
            files.append(cur)
            continue
        if raw.startswith("--- "):
            # Plain diff -u starts a file with ---; git diff already opened one.
            if cur is None or cur.lines:
                cur = FileDiff(None, None)
                files.append(cur)
            cur.old_path = _strip_prefix(raw[4:])
            continue
        if cur is None:
            continue
        if raw.startswith("+++ "):
            cur.new_path = _strip_prefix(raw[4:])
        elif raw.startswith("new file mode"):
            cur.old_path = None
        elif raw.startswith("deleted file mode"):
            cur.new_path = None
        elif raw.startswith("rename from "):
            cur.old_path = raw[len("rename from "):]
        elif raw.startswith("rename to "):
            cur.new_path = raw[len("rename to "):]
        elif raw.startswith("Binary files"):
            cur.binary = True
        else:
            h = _HUNK.match(raw)
            if h:
                old_no, new_no = int(h.group(1)), int(h.group(3))
                old_left = int(h.group(2)) if h.group(2) is not None else 1
                new_left = int(h.group(4)) if h.group(4) is not None else 1
    return files
