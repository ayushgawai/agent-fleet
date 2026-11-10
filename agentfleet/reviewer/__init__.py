"""Diff reviewer: static rules + optional pluggable LLM pass."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol
from .diff import parse_diff, FileDiff
from .rules import DEFAULT_RULES, Finding, ReviewContext, SEVERITY_ORDER

class LLMClient(Protocol):
    def review(self, diff_text: str, findings: list[Finding]) -> list[Finding] | str: ...

@dataclass
class Verdict:
    findings: list[Finding] = field(default_factory=list)
    @property
    def blocked(self): return any(f.severity == "high" for f in self.findings)
    @property
    def max_severity(self): return max((f.severity for f in self.findings), key=lambda s: SEVERITY_ORDER[s], default="info")
    def to_dict(self): return {"blocked": self.blocked, "max_severity": self.max_severity, "findings": [f.to_dict() for f in self.findings]}

class Reviewer:
    def __init__(self, rules=DEFAULT_RULES, llm: LLMClient | None = None):
        self.rules, self.llm = list(rules), llm
    def review(self, diff_text: str, ctx: ReviewContext | None = None) -> Verdict:
        ctx = ctx or ReviewContext()
        files = parse_diff(diff_text)
        findings = [f for r in self.rules for f in r(files, ctx)]
        if self.llm and diff_text.strip():
            out = self.llm.review(diff_text, findings)
            if isinstance(out, str):
                if out.strip(): findings.append(Finding("llm", "medium", "*", out.strip()[:500]))
            else: findings.extend(out)
        findings.sort(key=lambda f: -SEVERITY_ORDER[f.severity])
        return Verdict(findings)
__all__ = ["Reviewer", "Verdict", "Finding", "ReviewContext", "LLMClient", "parse_diff"]
