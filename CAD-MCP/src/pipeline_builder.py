from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from pipeline_models import DimensionSpec, FittingInstance, PipeNode, PipeSegment, PipelineSemanticModel

logger = logging.getLogger("pipeline_builder")


class PipelineBuilder:
    """Translate a reviewed semantic model into CAD primitive calls."""

    def __init__(self, cad_service: Any):
        self.cad_service = cad_service

    def build(
        self,
        semantic_model: PipelineSemanticModel,
        require_review: bool = True,
        zoom_extents: bool = True,
    ) -> Dict[str, Any]:
        unresolved = [issue for issue in semantic_model.unresolved_issues if issue.severity in {"warning", "critical"}]
        if require_review and unresolved:
            raise ValueError("语义模型仍存在未确认问题，请先执行 review 后再绘制。")

        node_map = {node.node_id: node for node in semantic_model.graph.nodes if node.node_id}
        created_entities: List[Dict[str, Any]] = []

        for segment in semantic_model.graph.segments:
            created_entities.extend(self._draw_segment(segment, node_map))

        for fitting in semantic_model.graph.fittings:
            entity = self._draw_fitting(fitting)
            if entity:
                created_entities.append(entity)

        for dimension in semantic_model.dimensions:
            entity = self._draw_dimension(dimension)
            if entity:
                created_entities.append(entity)

        if zoom_extents:
            self.cad_service.zoom_extents()

        return {
            "success": True,
            "summary": f"已根据结构化模型绘制 {len(created_entities)} 个配管实体。",
            "entity_count": len(created_entities),
            "entities": created_entities,
            "review_status": semantic_model.review_status,
        }

    def _draw_segment(self, segment: PipeSegment, node_map: Dict[str, PipeNode]) -> List[Dict[str, Any]]:
        path = self._resolve_segment_path(segment, node_map)
        if len(path) < 2:
            raise ValueError(f"管段 {segment.segment_id or '<unknown>'} 缺少有效路径点。")

        created: List[Dict[str, Any]] = []
        for start, end in zip(path, path[1:]):
            entity = self._draw_pipe_run(start, end, segment)
            if entity:
                created.append(entity)
        return created

    def _resolve_segment_path(self, segment: PipeSegment, node_map: Dict[str, PipeNode]) -> List[List[float]]:
        start_point = segment.start_point or self._lookup_node_point(segment.start_node, node_map)
        end_point = segment.end_point or self._lookup_node_point(segment.end_node, node_map)
        path: List[List[float]] = []
        if start_point:
            path.append(list(start_point))
        path.extend([list(point) for point in segment.route_points])
        if end_point:
            path.append(list(end_point))
        deduped_path: List[List[float]] = []
        for point in path:
            if not deduped_path or any(abs(point[index] - deduped_path[-1][index]) > 1e-6 for index in range(3)):
                deduped_path.append(point)
        return deduped_path

    def _lookup_node_point(self, node_id: Optional[str], node_map: Dict[str, PipeNode]) -> Optional[List[float]]:
        if not node_id:
            return None
        node = node_map.get(node_id)
        return list(node.point) if node else None

    def _draw_pipe_run(self, start: List[float], end: List[float], segment: PipeSegment) -> Optional[Dict[str, Any]]:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        dz = end[2] - start[2]
        changed_axes = sum(1 for value in (dx, dy, dz) if abs(value) > 1e-6)
        if changed_axes != 1:
            raise ValueError(
                f"首版仅支持轴向直管段，管段 {segment.segment_id or '<unknown>'} 存在非正交路径: {start} -> {end}"
            )

        radius = self._resolve_pipe_radius(segment)
        layer = segment.layer
        color = segment.color
        handle = None

        if abs(dx) > 1e-6:
            base = [min(start[0], end[0]), start[1], start[2]]
            length = abs(dx)
            result = self._draw_cylinder_with_retry(base, radius, length, layer=layer, color=color)
            handle = self._extract_handle(result)
            if handle:
                self._rotate_entity_with_retry(handle, base, 90, axis_direction=(0, 1, 0))
        elif abs(dy) > 1e-6:
            base = [start[0], min(start[1], end[1]), start[2]]
            length = abs(dy)
            result = self._draw_cylinder_with_retry(base, radius, length, layer=layer, color=color)
            handle = self._extract_handle(result)
            if handle:
                self._rotate_entity_with_retry(handle, base, -90, axis_direction=(1, 0, 0))
        else:
            base = [start[0], start[1], min(start[2], end[2])]
            length = abs(dz)
            result = self._draw_cylinder_with_retry(base, radius, length, layer=layer, color=color)
            handle = self._extract_handle(result)

        return {
            "type": "pipe_run",
            "segment_id": segment.segment_id,
            "start": start,
            "end": end,
            "handle": handle,
            "radius": radius,
        }

    def _draw_cylinder_with_retry(
        self,
        base: List[float],
        radius: float,
        length: float,
        *,
        layer: Optional[str],
        color: Optional[Any],
    ) -> Any:
        last_result = None
        for _ in range(4):
            try:
                last_result = self.cad_service.controller.draw_cylinder(base, radius, length, layer=layer, color=color)
                if last_result is not None:
                    return last_result
            except Exception:
                logger.debug("draw_cylinder failed, retrying.", exc_info=True)
            time.sleep(0.8)
        return last_result

    def _rotate_entity_with_retry(
        self,
        handle: str,
        base_point: List[float],
        angle: float,
        *,
        axis_direction: tuple[float, float, float],
    ) -> None:
        for _ in range(4):
            try:
                if self.cad_service.controller.rotate_entity(
                    handle,
                    base_point,
                    angle,
                    axis_direction=axis_direction,
                ):
                    return
            except Exception:
                logger.debug("rotate_entity failed, retrying.", exc_info=True)
            time.sleep(0.8)

    def _resolve_pipe_radius(self, segment: PipeSegment) -> float:
        if segment.outer_diameter is not None:
            return float(segment.outer_diameter) / 2.0
        if segment.dn is not None:
            dn_table = getattr(self.cad_service.controller, "ELBOW_DN_OD_GB12459", {})
            if int(segment.dn) in dn_table:
                return float(dn_table[int(segment.dn)]) / 2.0
            return float(segment.dn) / 2.0
        raise ValueError(f"管段 {segment.segment_id or '<unknown>'} 缺少管径信息。")

    def _draw_fitting(self, fitting: FittingInstance) -> Optional[Dict[str, Any]]:
        kind = fitting.kind.lower()
        center = fitting.center
        layer = fitting.layer
        color = fitting.color
        orientation = fitting.orientation or {}
        spec = fitting.spec or {}

        if kind == "elbow":
            result = self.cad_service.draw_pipe_elbow(
                center,
                pipe_radius=spec.get("pipe_radius"),
                dn=fitting.dn,
                bend_radius=spec.get("bend_radius"),
                elbow_type=spec.get("elbow_type", "LR"),
                angle=orientation.get("angle", spec.get("angle", 90)),
                plane=orientation.get("plane", spec.get("plane", "xy")),
                wall_thickness=spec.get("wall_thickness"),
                layer=layer,
                color=color,
            )
        elif kind == "tee":
            result = self.cad_service.draw_tee(
                center,
                fitting.dn,
                branch_dn=spec.get("branch_dn"),
                run_axis=orientation.get("run_axis", "x"),
                branch_axis=orientation.get("branch_axis", "-y"),
                wall_thickness=spec.get("wall_thickness"),
                branch_wall_thickness=spec.get("branch_wall_thickness"),
                run_center_to_end=spec.get("run_center_to_end"),
                branch_center_to_end=spec.get("branch_center_to_end"),
                layer=layer,
                color=color,
            )
        elif kind == "reducer":
            result = self.cad_service.draw_reducer(
                center,
                dn_large=spec.get("dn_large"),
                dn_small=spec.get("dn_small"),
                axis=orientation.get("axis", spec.get("axis", "z")),
                reducer_type=spec.get("reducer_type", spec.get("type", "concentric")),
                length=spec.get("length"),
                wall_thickness=spec.get("wall_thickness"),
                eccentric_direction=orientation.get("eccentric_direction", spec.get("eccentric_direction")),
                eccentric_offset=spec.get("eccentric_offset"),
                layer=layer,
                color=color,
            )
        elif kind == "flange":
            result = self.cad_service.draw_flange(
                center,
                specification=spec.get("specification"),
                dn=fitting.dn or spec.get("dn"),
                pn=spec.get("pn"),
                outer_diameter=spec.get("outer_diameter"),
                center_hole_diameter=spec.get("center_hole_diameter"),
                bolt_circle_diameter=spec.get("bolt_circle_diameter"),
                bolt_hole_diameter=spec.get("bolt_hole_diameter"),
                bolt_count=spec.get("bolt_count"),
                thickness=spec.get("thickness"),
                layer=layer,
                color=color,
            )
        elif kind == "blind_flange":
            result = self.cad_service.draw_blind_flange(
                center,
                specification=spec.get("specification"),
                dn=fitting.dn or spec.get("dn"),
                pn=spec.get("pn"),
                outer_diameter=spec.get("outer_diameter"),
                bolt_circle_diameter=spec.get("bolt_circle_diameter"),
                bolt_hole_diameter=spec.get("bolt_hole_diameter"),
                bolt_count=spec.get("bolt_count"),
                thickness=spec.get("thickness"),
                layer=layer,
                color=color,
            )
        elif kind == "ball_valve":
            result = self.cad_service.draw_ball_valve(
                center,
                dn=fitting.dn or spec.get("dn"),
                pn=spec.get("pn"),
                axis=orientation.get("axis", "z"),
                layer=layer,
                color=color,
            )
        elif kind == "butterfly_valve":
            result = self.cad_service.draw_butterfly_valve(
                center,
                dn=fitting.dn or spec.get("dn"),
                pn=spec.get("pn"),
                axis=orientation.get("axis", "z"),
                layer=layer,
                color=color,
            )
        elif kind == "globe_valve":
            result = self.cad_service.draw_globe_valve(
                center,
                dn=fitting.dn or spec.get("dn"),
                pn=spec.get("pn"),
                axis=orientation.get("axis", "z"),
                layer=layer,
                color=color,
            )
        else:
            logger.warning("暂不支持的管件类型: %s", fitting.kind)
            return {
                "type": "unsupported_fitting",
                "fitting_id": fitting.fitting_id,
                "kind": fitting.kind,
                "handle": None,
            }

        return {
            "type": kind,
            "fitting_id": fitting.fitting_id,
            "center": center,
            "handle": self._extract_handle(result),
        }

    def _draw_dimension(self, dimension: DimensionSpec) -> Optional[Dict[str, Any]]:
        result = self.cad_service.add_dimension(
            start_point=dimension.start_point,
            end_point=dimension.end_point,
            text_position=dimension.text_position,
            textheight=dimension.textheight or 5,
            layer=dimension.layer,
            color=dimension.color,
            dimension_type=dimension.dimension_type,
            center=dimension.center,
            chord_point=dimension.chord_point,
            far_chord_point=dimension.far_chord_point,
            leader_length=dimension.leader_length,
        )
        return {
            "type": "dimension",
            "dimension_id": dimension.dimension_id,
            "dimension_type": dimension.dimension_type,
            "handle": self._extract_handle(result),
        } if result else None

    def _extract_handle(self, entity: Any) -> Optional[str]:
        if entity is None:
            return None
        for _ in range(4):
            try:
                return str(entity.Handle)
            except Exception:
                time.sleep(0.5)
        return None
