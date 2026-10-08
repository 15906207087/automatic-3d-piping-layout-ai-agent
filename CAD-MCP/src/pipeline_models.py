from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def normalize_point(point: Any) -> Optional[List[float]]:
    """Normalize a point-like value into a 3D point list."""
    if point is None:
        return None
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        return None
    coords = [float(point[0]), float(point[1])]
    coords.append(float(point[2]) if len(point) >= 3 else 0.0)
    return coords


def _to_list_of_points(points: Any) -> List[List[float]]:
    if not isinstance(points, list):
        return []
    normalized: List[List[float]] = []
    for point in points:
        parsed = normalize_point(point)
        if parsed is not None:
            normalized.append(parsed)
    return normalized


def _to_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _to_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _set_nested_value(target: Dict[str, Any], field_path: str, value: Any) -> None:
    """Set a nested dict/list value using dot-separated path."""
    parts = [part for part in str(field_path).split(".") if part]
    if not parts:
        return

    current: Any = target
    for index, part in enumerate(parts[:-1]):
        next_part = parts[index + 1]
        try:
            list_index = int(part)
        except ValueError:
            if not isinstance(current, dict):
                return
            if part not in current or current[part] is None:
                current[part] = [] if next_part.isdigit() else {}
            current = current[part]
        else:
            if not isinstance(current, list) or list_index < 0:
                return
            while len(current) <= list_index:
                current.append([] if next_part.isdigit() else {})
            current = current[list_index]

    last_part = parts[-1]
    try:
        list_index = int(last_part)
    except ValueError:
        if isinstance(current, dict):
            current[last_part] = value
    else:
        if isinstance(current, list) and list_index >= 0:
            while len(current) <= list_index:
                current.append(None)
            current[list_index] = value


@dataclass
class DrawingDocument:
    source_path: str
    source_type: str
    drawing_type: str = "isometric"
    page_number: int = 1
    page_count: int = 1
    image_path: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_type": self.source_type,
            "drawing_type": self.drawing_type,
            "page_number": self.page_number,
            "page_count": self.page_count,
            "image_path": self.image_path,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DrawingDocument":
        payload = _to_dict(payload)
        return cls(
            source_path=str(payload.get("source_path", "")),
            source_type=str(payload.get("source_type", "image")),
            drawing_type=str(payload.get("drawing_type", "isometric")),
            page_number=int(payload.get("page_number", 1) or 1),
            page_count=int(payload.get("page_count", 1) or 1),
            image_path=payload.get("image_path"),
            metadata=_to_dict(payload.get("metadata")),
        )


@dataclass
class PipeNode:
    node_id: str
    point: List[float]
    elevation: Optional[float] = None
    label: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "point": list(self.point),
            "elevation": self.elevation,
            "label": self.label,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "PipeNode":
        payload = _to_dict(payload)
        point = normalize_point(payload.get("point")) or [0.0, 0.0, 0.0]
        elevation = payload.get("elevation")
        return cls(
            node_id=str(payload.get("node_id", "")),
            point=point,
            elevation=float(elevation) if elevation is not None else None,
            label=payload.get("label"),
            metadata=_to_dict(payload.get("metadata")),
        )


@dataclass
class PipeSegment:
    segment_id: str
    start_node: Optional[str] = None
    end_node: Optional[str] = None
    start_point: Optional[List[float]] = None
    end_point: Optional[List[float]] = None
    route_points: List[List[float]] = field(default_factory=list)
    dn: Optional[int] = None
    outer_diameter: Optional[float] = None
    wall_thickness: Optional[float] = None
    layer: Optional[str] = None
    color: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "start_node": self.start_node,
            "end_node": self.end_node,
            "start_point": list(self.start_point) if self.start_point else None,
            "end_point": list(self.end_point) if self.end_point else None,
            "route_points": [list(point) for point in self.route_points],
            "dn": self.dn,
            "outer_diameter": self.outer_diameter,
            "wall_thickness": self.wall_thickness,
            "layer": self.layer,
            "color": self.color,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "PipeSegment":
        payload = _to_dict(payload)
        dn = payload.get("dn")
        return cls(
            segment_id=str(payload.get("segment_id", "")),
            start_node=payload.get("start_node"),
            end_node=payload.get("end_node"),
            start_point=normalize_point(payload.get("start_point")),
            end_point=normalize_point(payload.get("end_point")),
            route_points=_to_list_of_points(payload.get("route_points")),
            dn=int(dn) if dn is not None else None,
            outer_diameter=float(payload["outer_diameter"]) if payload.get("outer_diameter") is not None else None,
            wall_thickness=float(payload["wall_thickness"]) if payload.get("wall_thickness") is not None else None,
            layer=payload.get("layer"),
            color=payload.get("color"),
            metadata=_to_dict(payload.get("metadata")),
        )


@dataclass
class FittingInstance:
    fitting_id: str
    kind: str
    center: List[float]
    dn: Optional[int] = None
    layer: Optional[str] = None
    color: Optional[Any] = None
    connection_nodes: List[str] = field(default_factory=list)
    orientation: Dict[str, Any] = field(default_factory=dict)
    spec: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fitting_id": self.fitting_id,
            "kind": self.kind,
            "center": list(self.center),
            "dn": self.dn,
            "layer": self.layer,
            "color": self.color,
            "connection_nodes": list(self.connection_nodes),
            "orientation": dict(self.orientation),
            "spec": dict(self.spec),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "FittingInstance":
        payload = _to_dict(payload)
        dn = payload.get("dn")
        connection_nodes = [
            str(item) for item in _to_list(payload.get("connection_nodes")) if item is not None
        ]
        return cls(
            fitting_id=str(payload.get("fitting_id", "")),
            kind=str(payload.get("kind", "")),
            center=normalize_point(payload.get("center")) or [0.0, 0.0, 0.0],
            dn=int(dn) if dn is not None else None,
            layer=payload.get("layer"),
            color=payload.get("color"),
            connection_nodes=connection_nodes,
            orientation=_to_dict(payload.get("orientation")),
            spec=_to_dict(payload.get("spec")),
            metadata=_to_dict(payload.get("metadata")),
        )


@dataclass
class ReviewIssue:
    issue_id: str
    severity: str
    category: str
    message: str
    field_path: Optional[str] = None
    suggested_value: Any = None
    options: List[Any] = field(default_factory=list)
    resolved: bool = False
    resolution: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "field_path": self.field_path,
            "suggested_value": self.suggested_value,
            "options": list(self.options),
            "resolved": self.resolved,
            "resolution": self.resolution,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ReviewIssue":
        payload = _to_dict(payload)
        return cls(
            issue_id=str(payload.get("issue_id", "")),
            severity=str(payload.get("severity", "warning")),
            category=str(payload.get("category", "review")),
            message=str(payload.get("message", "")),
            field_path=payload.get("field_path"),
            suggested_value=payload.get("suggested_value"),
            options=_to_list(payload.get("options")),
            resolved=bool(payload.get("resolved", False)),
            resolution=payload.get("resolution"),
            metadata=_to_dict(payload.get("metadata")),
        )


@dataclass
class DimensionSpec:
    dimension_id: str = ""
    dimension_type: str = "aligned"
    start_point: Optional[List[float]] = None
    end_point: Optional[List[float]] = None
    text_position: Optional[List[float]] = None
    center: Optional[List[float]] = None
    chord_point: Optional[List[float]] = None
    far_chord_point: Optional[List[float]] = None
    leader_length: Optional[float] = None
    textheight: Optional[float] = None
    layer: Optional[str] = None
    color: Optional[Any] = None
    source_view: Optional[str] = "front"
    orientation: Optional[str] = None
    value: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension_id": self.dimension_id,
            "dimension_type": self.dimension_type,
            "start_point": list(self.start_point) if self.start_point else None,
            "end_point": list(self.end_point) if self.end_point else None,
            "text_position": list(self.text_position) if self.text_position else None,
            "center": list(self.center) if self.center else None,
            "chord_point": list(self.chord_point) if self.chord_point else None,
            "far_chord_point": list(self.far_chord_point) if self.far_chord_point else None,
            "leader_length": self.leader_length,
            "textheight": self.textheight,
            "layer": self.layer,
            "color": self.color,
            "source_view": self.source_view,
            "orientation": self.orientation,
            "value": self.value,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DimensionSpec":
        payload = _to_dict(payload)
        leader_length = payload.get("leader_length")
        textheight = payload.get("textheight")
        value = payload.get("value")
        return cls(
            dimension_id=str(payload.get("dimension_id", "")),
            dimension_type=str(payload.get("dimension_type", "aligned")),
            start_point=normalize_point(payload.get("start_point")),
            end_point=normalize_point(payload.get("end_point")),
            text_position=normalize_point(payload.get("text_position")),
            center=normalize_point(payload.get("center")),
            chord_point=normalize_point(payload.get("chord_point")),
            far_chord_point=normalize_point(payload.get("far_chord_point")),
            leader_length=float(leader_length) if leader_length is not None else None,
            textheight=float(textheight) if textheight is not None else None,
            layer=payload.get("layer"),
            color=payload.get("color"),
            source_view=payload.get("source_view") or "front",
            orientation=payload.get("orientation"),
            value=float(value) if value is not None else None,
            metadata=_to_dict(payload.get("metadata")),
        )


@dataclass
class RecognitionRegion:
    region_id: str = ""
    region_type: str = "unknown"
    page_number: int = 1
    source_view: Optional[str] = None
    label: str = ""
    description: str = ""
    bbox: List[float] = field(default_factory=list)
    extracted_fields: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "region_id": self.region_id,
            "region_type": self.region_type,
            "page_number": self.page_number,
            "source_view": self.source_view,
            "label": self.label,
            "description": self.description,
            "bbox": list(self.bbox),
            "extracted_fields": list(self.extracted_fields),
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RecognitionRegion":
        payload = _to_dict(payload)
        confidence = payload.get("confidence")
        bbox = [float(item) for item in _to_list(payload.get("bbox")) if item is not None]
        extracted_fields = [str(item) for item in _to_list(payload.get("extracted_fields")) if item is not None]
        return cls(
            region_id=str(payload.get("region_id", "")),
            region_type=str(payload.get("region_type", "unknown")),
            page_number=int(payload.get("page_number", 1) or 1),
            source_view=payload.get("source_view"),
            label=str(payload.get("label", "")),
            description=str(payload.get("description", "")),
            bbox=bbox,
            extracted_fields=extracted_fields,
            confidence=float(confidence) if confidence is not None else None,
            metadata=_to_dict(payload.get("metadata")),
        )


@dataclass
class ViewInterpretation:
    view_id: str = ""
    source_view: str = ""
    canonical_role: str = ""
    confidence: Optional[float] = None
    evidence: List[str] = field(default_factory=list)
    region_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "view_id": self.view_id,
            "source_view": self.source_view,
            "canonical_role": self.canonical_role,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "region_id": self.region_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ViewInterpretation":
        payload = _to_dict(payload)
        confidence = payload.get("confidence")
        evidence = [str(item) for item in _to_list(payload.get("evidence")) if item is not None]
        return cls(
            view_id=str(payload.get("view_id", "")),
            source_view=str(payload.get("source_view", "")),
            canonical_role=str(payload.get("canonical_role", "")),
            confidence=float(confidence) if confidence is not None else None,
            evidence=evidence,
            region_id=payload.get("region_id"),
            metadata=_to_dict(payload.get("metadata")),
        )


@dataclass
class DimensionLedgerEntry:
    dimension_id: str = ""
    dimension_type: str = "aligned"
    start_point: Optional[List[float]] = None
    end_point: Optional[List[float]] = None
    text_position: Optional[List[float]] = None
    center: Optional[List[float]] = None
    chord_point: Optional[List[float]] = None
    far_chord_point: Optional[List[float]] = None
    leader_length: Optional[float] = None
    textheight: Optional[float] = None
    layer: Optional[str] = None
    color: Optional[Any] = None
    source_view: Optional[str] = "front"
    orientation: Optional[str] = None
    value: Optional[float] = None
    unit: str = "mm"
    applies_to: List[str] = field(default_factory=list)
    used_by_model: bool = False
    confidence: Optional[float] = None
    verified: bool = False
    is_key: bool = False
    source_region: Optional[str] = None
    source_text: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension_id": self.dimension_id,
            "dimension_type": self.dimension_type,
            "start_point": list(self.start_point) if self.start_point else None,
            "end_point": list(self.end_point) if self.end_point else None,
            "text_position": list(self.text_position) if self.text_position else None,
            "center": list(self.center) if self.center else None,
            "chord_point": list(self.chord_point) if self.chord_point else None,
            "far_chord_point": list(self.far_chord_point) if self.far_chord_point else None,
            "leader_length": self.leader_length,
            "textheight": self.textheight,
            "layer": self.layer,
            "color": self.color,
            "source_view": self.source_view,
            "orientation": self.orientation,
            "value": self.value,
            "unit": self.unit,
            "applies_to": list(self.applies_to),
            "used_by_model": self.used_by_model,
            "confidence": self.confidence,
            "verified": self.verified,
            "is_key": self.is_key,
            "source_region": self.source_region,
            "source_text": self.source_text,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DimensionLedgerEntry":
        payload = _to_dict(payload)
        leader_length = payload.get("leader_length")
        textheight = payload.get("textheight")
        value = payload.get("value")
        confidence = payload.get("confidence")
        applies_to = [str(item) for item in _to_list(payload.get("applies_to")) if item is not None]
        return cls(
            dimension_id=str(payload.get("dimension_id", "")),
            dimension_type=str(payload.get("dimension_type", "aligned")),
            start_point=normalize_point(payload.get("start_point")),
            end_point=normalize_point(payload.get("end_point")),
            text_position=normalize_point(payload.get("text_position")),
            center=normalize_point(payload.get("center")),
            chord_point=normalize_point(payload.get("chord_point")),
            far_chord_point=normalize_point(payload.get("far_chord_point")),
            leader_length=float(leader_length) if leader_length is not None else None,
            textheight=float(textheight) if textheight is not None else None,
            layer=payload.get("layer"),
            color=payload.get("color"),
            source_view=payload.get("source_view") or "front",
            orientation=payload.get("orientation"),
            value=float(value) if value is not None else None,
            unit=str(payload.get("unit", "mm")),
            applies_to=applies_to,
            used_by_model=bool(payload.get("used_by_model", False)),
            confidence=float(confidence) if confidence is not None else None,
            verified=bool(payload.get("verified", False)),
            is_key=bool(payload.get("is_key", False)),
            source_region=payload.get("source_region"),
            source_text=payload.get("source_text"),
            metadata=_to_dict(payload.get("metadata")),
        )

    @classmethod
    def from_dimension_spec(
        cls,
        dimension: DimensionSpec,
        *,
        applies_to: Optional[List[str]] = None,
        used_by_model: bool = False,
        confidence: Optional[float] = None,
        verified: bool = False,
        is_key: bool = False,
        source_region: Optional[str] = None,
        source_text: Optional[str] = None,
    ) -> "DimensionLedgerEntry":
        return cls(
            dimension_id=dimension.dimension_id,
            dimension_type=dimension.dimension_type,
            start_point=list(dimension.start_point) if dimension.start_point else None,
            end_point=list(dimension.end_point) if dimension.end_point else None,
            text_position=list(dimension.text_position) if dimension.text_position else None,
            center=list(dimension.center) if dimension.center else None,
            chord_point=list(dimension.chord_point) if dimension.chord_point else None,
            far_chord_point=list(dimension.far_chord_point) if dimension.far_chord_point else None,
            leader_length=dimension.leader_length,
            textheight=dimension.textheight,
            layer=dimension.layer,
            color=dimension.color,
            source_view=dimension.source_view,
            orientation=dimension.orientation,
            value=dimension.value,
            applies_to=list(applies_to or []),
            used_by_model=used_by_model,
            confidence=confidence,
            verified=verified,
            is_key=is_key,
            source_region=source_region,
            source_text=source_text,
            metadata=dict(dimension.metadata),
        )

    def to_dimension_spec(self) -> DimensionSpec:
        return DimensionSpec(
            dimension_id=self.dimension_id,
            dimension_type=self.dimension_type,
            start_point=list(self.start_point) if self.start_point else None,
            end_point=list(self.end_point) if self.end_point else None,
            text_position=list(self.text_position) if self.text_position else None,
            center=list(self.center) if self.center else None,
            chord_point=list(self.chord_point) if self.chord_point else None,
            far_chord_point=list(self.far_chord_point) if self.far_chord_point else None,
            leader_length=self.leader_length,
            textheight=self.textheight,
            layer=self.layer,
            color=self.color,
            source_view=self.source_view,
            orientation=self.orientation,
            value=self.value,
            metadata=dict(self.metadata),
        )


@dataclass
class BomClosureEntry:
    item_no: int = 0
    name: str = ""
    quantity: int = 0
    spec: str = ""
    material: str = ""
    standard: str = ""
    decoded_params: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    model_status: str = "pending"
    review_status: str = "pending"
    exception_reason: str = ""
    modeled_as: str = ""
    note: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_no": self.item_no,
            "name": self.name,
            "quantity": self.quantity,
            "spec": self.spec,
            "material": self.material,
            "standard": self.standard,
            "decoded_params": dict(self.decoded_params),
            "status": self.status,
            "model_status": self.model_status,
            "review_status": self.review_status,
            "exception_reason": self.exception_reason,
            "modeled_as": self.modeled_as,
            "note": self.note,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "BomClosureEntry":
        payload = _to_dict(payload)
        return cls(
            item_no=int(payload.get("item_no", 0) or 0),
            name=str(payload.get("name", "")),
            quantity=int(payload.get("quantity", 0) or 0),
            spec=str(payload.get("spec", "")),
            material=str(payload.get("material", "")),
            standard=str(payload.get("standard", "")),
            decoded_params=_to_dict(payload.get("decoded_params")),
            status=str(payload.get("status", "pending")),
            model_status=str(payload.get("model_status", payload.get("status", "pending"))),
            review_status=str(payload.get("review_status", "pending")),
            exception_reason=str(payload.get("exception_reason", "")),
            modeled_as=str(payload.get("modeled_as", "")),
            note=str(payload.get("note", "")),
            metadata=_to_dict(payload.get("metadata")),
        )


@dataclass
class SectionDetail:
    section_id: str = ""
    section_type: str = "detail_view"
    title: str = ""
    source_view: Optional[str] = None
    region_id: Optional[str] = None
    purpose: str = ""
    cut_line_description: str = ""
    view_direction: str = ""
    removed_side: str = ""
    retained_side: str = ""
    projection_basis: str = ""
    related_items: List[str] = field(default_factory=list)
    extracted_fields: List[str] = field(default_factory=list)
    modeling_notes: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    direction_confidence: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "section_id": self.section_id,
            "section_type": self.section_type,
            "title": self.title,
            "source_view": self.source_view,
            "region_id": self.region_id,
            "purpose": self.purpose,
            "cut_line_description": self.cut_line_description,
            "view_direction": self.view_direction,
            "removed_side": self.removed_side,
            "retained_side": self.retained_side,
            "projection_basis": self.projection_basis,
            "related_items": list(self.related_items),
            "extracted_fields": list(self.extracted_fields),
            "modeling_notes": list(self.modeling_notes),
            "confidence": self.confidence,
            "direction_confidence": self.direction_confidence,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "SectionDetail":
        payload = _to_dict(payload)
        confidence = payload.get("confidence")
        direction_confidence = payload.get("direction_confidence")
        related_items = [str(item) for item in _to_list(payload.get("related_items")) if item is not None]
        extracted_fields = [str(item) for item in _to_list(payload.get("extracted_fields")) if item is not None]
        modeling_notes = [str(item) for item in _to_list(payload.get("modeling_notes")) if item is not None]
        return cls(
            section_id=str(payload.get("section_id", "")),
            section_type=str(payload.get("section_type", "detail_view")),
            title=str(payload.get("title", "")),
            source_view=payload.get("source_view"),
            region_id=payload.get("region_id"),
            purpose=str(payload.get("purpose", "")),
            cut_line_description=str(payload.get("cut_line_description", "")),
            view_direction=str(payload.get("view_direction", "")),
            removed_side=str(payload.get("removed_side", "")),
            retained_side=str(payload.get("retained_side", "")),
            projection_basis=str(payload.get("projection_basis", "")),
            related_items=related_items,
            extracted_fields=extracted_fields,
            modeling_notes=modeling_notes,
            confidence=float(confidence) if confidence is not None else None,
            direction_confidence=float(direction_confidence) if direction_confidence is not None else None,
            metadata=_to_dict(payload.get("metadata")),
        )


@dataclass
class PipelineGraph:
    nodes: List[PipeNode] = field(default_factory=list)
    segments: List[PipeSegment] = field(default_factory=list)
    fittings: List[FittingInstance] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "segments": [segment.to_dict() for segment in self.segments],
            "fittings": [fitting.to_dict() for fitting in self.fittings],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "PipelineGraph":
        payload = _to_dict(payload)
        return cls(
            nodes=[PipeNode.from_dict(item) for item in _to_list(payload.get("nodes"))],
            segments=[PipeSegment.from_dict(item) for item in _to_list(payload.get("segments"))],
            fittings=[FittingInstance.from_dict(item) for item in _to_list(payload.get("fittings"))],
            metadata=_to_dict(payload.get("metadata")),
        )


@dataclass
class PipelineSemanticModel:
    document: DrawingDocument
    graph: PipelineGraph = field(default_factory=PipelineGraph)
    dimensions: List[DimensionSpec] = field(default_factory=list)
    recognition_regions: List[RecognitionRegion] = field(default_factory=list)
    view_interpretations: List[ViewInterpretation] = field(default_factory=list)
    dimension_ledger: List[DimensionLedgerEntry] = field(default_factory=list)
    bom_closure: List[BomClosureEntry] = field(default_factory=list)
    section_details: List[SectionDetail] = field(default_factory=list)
    summary: str = ""
    confidence: Optional[float] = None
    view_semantics: Dict[str, Any] = field(default_factory=dict)
    review_status: str = "pending"
    review_issues: List[ReviewIssue] = field(default_factory=list)
    recognition_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document": self.document.to_dict(),
            "graph": self.graph.to_dict(),
            "dimensions": [dimension.to_dict() for dimension in self.dimensions],
            "recognition_regions": [region.to_dict() for region in self.recognition_regions],
            "view_interpretations": [item.to_dict() for item in self.view_interpretations],
            "dimension_ledger": [entry.to_dict() for entry in self.dimension_ledger],
            "bom_closure": [entry.to_dict() for entry in self.bom_closure],
            "section_details": [entry.to_dict() for entry in self.section_details],
            "summary": self.summary,
            "confidence": self.confidence,
            "view_semantics": dict(self.view_semantics),
            "review_status": self.review_status,
            "review_issues": [issue.to_dict() for issue in self.review_issues],
            "recognition_metadata": dict(self.recognition_metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "PipelineSemanticModel":
        payload = _to_dict(payload)
        confidence = payload.get("confidence")
        return cls(
            document=DrawingDocument.from_dict(payload.get("document", {})),
            graph=PipelineGraph.from_dict(payload.get("graph", {})),
            dimensions=[DimensionSpec.from_dict(item) for item in _to_list(payload.get("dimensions"))],
            recognition_regions=[RecognitionRegion.from_dict(item) for item in _to_list(payload.get("recognition_regions"))],
            view_interpretations=[ViewInterpretation.from_dict(item) for item in _to_list(payload.get("view_interpretations"))],
            dimension_ledger=[DimensionLedgerEntry.from_dict(item) for item in _to_list(payload.get("dimension_ledger"))],
            bom_closure=[BomClosureEntry.from_dict(item) for item in _to_list(payload.get("bom_closure"))],
            section_details=[SectionDetail.from_dict(item) for item in _to_list(payload.get("section_details"))],
            summary=str(payload.get("summary", "")),
            confidence=float(confidence) if confidence is not None else None,
            view_semantics=_to_dict(payload.get("view_semantics")),
            review_status=str(payload.get("review_status", "pending")),
            review_issues=[ReviewIssue.from_dict(item) for item in _to_list(payload.get("review_issues"))],
            recognition_metadata=_to_dict(payload.get("recognition_metadata")),
        )

    @property
    def unresolved_issues(self) -> List[ReviewIssue]:
        return [issue for issue in self.review_issues if not issue.resolved]

    def apply_review_resolutions(self, resolutions: Dict[str, Any]) -> None:
        """Apply user-confirmed values back onto the semantic model."""
        if not isinstance(resolutions, dict):
            return

        model_payload = self.to_dict()
        for field_path, value in resolutions.items():
            _set_nested_value(model_payload, str(field_path), value)

        updated_model = PipelineSemanticModel.from_dict(model_payload)
        self.document = updated_model.document
        self.graph = updated_model.graph
        self.dimensions = updated_model.dimensions
        self.recognition_regions = updated_model.recognition_regions
        self.view_interpretations = updated_model.view_interpretations
        self.dimension_ledger = updated_model.dimension_ledger
        self.bom_closure = updated_model.bom_closure
        self.section_details = updated_model.section_details
        self.summary = updated_model.summary
        self.confidence = updated_model.confidence
        self.view_semantics = updated_model.view_semantics
        self.recognition_metadata = updated_model.recognition_metadata

        for issue in self.review_issues:
            field_path = issue.field_path or ""
            if field_path and field_path in resolutions:
                issue.resolved = True
                issue.resolution = resolutions[field_path]

        unresolved = self.unresolved_issues
        self.review_status = "approved" if not unresolved else "pending"


def pipeline_model_from_payload(payload: Dict[str, Any]) -> PipelineSemanticModel:
    """Build a semantic model from a loosely structured payload."""
    payload = _to_dict(payload)
    graph_payload = payload.get("graph", {})

    if not graph_payload:
        graph_payload = {
            "nodes": payload.get("nodes", []),
            "segments": payload.get("segments", []),
            "fittings": payload.get("fittings", []),
            "metadata": payload.get("graph_metadata", {}),
        }

    document_payload = payload.get("document", {})
    if not document_payload:
        document_payload = {
            "source_path": payload.get("source_path", ""),
            "source_type": payload.get("source_type", "image"),
            "drawing_type": payload.get("drawing_type", "isometric"),
            "page_number": payload.get("page_number", 1),
            "page_count": payload.get("page_count", 1),
            "image_path": payload.get("image_path"),
            "metadata": payload.get("document_metadata", {}),
        }

    model_payload = {
        "document": document_payload,
        "graph": graph_payload,
        "dimensions": payload.get("dimensions", []),
        "recognition_regions": payload.get("recognition_regions", []),
        "view_interpretations": payload.get("view_interpretations", []),
        "dimension_ledger": payload.get("dimension_ledger", []),
        "bom_closure": payload.get("bom_closure", []),
        "section_details": payload.get("section_details", []),
        "summary": payload.get("summary", ""),
        "confidence": payload.get("confidence"),
        "view_semantics": payload.get("view_semantics", {}),
        "review_status": payload.get("review_status", "pending"),
        "review_issues": payload.get("review_issues", []),
        "recognition_metadata": payload.get("recognition_metadata", {}),
    }
    return PipelineSemanticModel.from_dict(model_payload)
