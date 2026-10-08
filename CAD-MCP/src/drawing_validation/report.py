from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ValidationIssue:
    """Single deterministic validation finding."""

    rule_id: str
    severity: str
    message: str
    category: str
    path: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "message": self.message,
            "category": self.category,
            "path": self.path,
            "metadata": dict(self.metadata),
        }


@dataclass
class ValidationReport:
    """Validation report grouped by execution stage."""

    stage: str
    issues: List[ValidationIssue] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)

    def summary(self) -> Dict[str, int]:
        counts = {"error": 0, "warning": 0, "info": 0}
        for issue in self.issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        return counts

    def extend(self, issues: List[ValidationIssue]) -> None:
        self.issues.extend(issues)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "issues": [issue.to_dict() for issue in self.issues],
            "summary": self.summary(),
            "has_errors": self.has_errors,
            "metadata": dict(self.metadata),
        }


def merge_reports(reports: List[ValidationReport], stage: str = "combined") -> ValidationReport:
    merged = ValidationReport(stage=stage)
    for report in reports:
        merged.extend(report.issues)
    merged.metadata["sources"] = [report.stage for report in reports]
    return merged
