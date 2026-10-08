from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class PreviewArtifact:
    """Normalized preview artifact metadata used by validation rules."""

    view_name: str
    path: str
    window_title: Optional[str] = None
    exists: Optional[bool] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "view_name": self.view_name,
            "path": self.path,
            "window_title": self.window_title,
            "exists": self.exists if self.exists is not None else Path(self.path).exists(),
            "metadata": dict(self.metadata),
        }


@dataclass
class ValidationContext:
    """Carries stage-specific inputs for deterministic validation rules."""

    stage: str
    topic: str
    params: Any = None
    script_path: Optional[str] = None
    dwg_path: Optional[str] = None
    document_name: Optional[str] = None
    dimension_ledger: List[Dict[str, Any]] = field(default_factory=list)
    bom_closure: List[Dict[str, Any]] = field(default_factory=list)
    recognition_summary: Dict[str, Any] = field(default_factory=dict)
    preview_artifacts: List[PreviewArtifact] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def iter_nozzles(self) -> List[Any]:
        nozzles = getattr(self.params, "nozzles", None)
        return list(nozzles) if isinstance(nozzles, list) else []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "topic": self.topic,
            "script_path": self.script_path,
            "dwg_path": self.dwg_path,
            "document_name": self.document_name,
            "dimension_ledger": list(self.dimension_ledger),
            "bom_closure": list(self.bom_closure),
            "recognition_summary": dict(self.recognition_summary),
            "preview_artifacts": [artifact.to_dict() for artifact in self.preview_artifacts],
            "metadata": dict(self.metadata),
        }
