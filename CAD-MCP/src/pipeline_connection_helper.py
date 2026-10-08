from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable

import pythoncom
import win32com.client

POINT_TOLERANCE_MM = 1.0
VECTOR_TOLERANCE = 1e-6


@dataclass(frozen=True)
class PipeConnectionRunSpec:
    """One straight pipe run that starts at a fitting tangent point."""

    run_id: str
    tangent_point_mm: tuple[float, float, float]
    axis_away_from_fitting: str
    length_mm: float
    connection_end: str
    direction_vector_away_from_fitting: tuple[float, float, float] | None = None
    flow_role: str = ""


@dataclass(frozen=True)
class RotationInstruction:
    """One explicit fitting rotation applied after the solid is created."""

    angle_deg: float
    axis_direction: tuple[float, float, float]
    axis_name: str = ""


@dataclass(frozen=True)
class ElbowConnectionPort:
    """One elbow port after all rotations have been applied."""

    port_id: str
    tangent_point_mm: tuple[float, float, float]
    axis_away_from_fitting: str
    axis_vector: tuple[float, float, float]
    radial_vector_mm: tuple[float, float, float]
    connection_end: str


@dataclass(frozen=True)
class ResolvedRunDirection:
    """One normalized pipe run direction used during validation and drawing."""

    direction_vector: tuple[float, float, float]
    direction_label: str
    axis_name: str | None


def _normalize_point3d(point: tuple[float, float, float] | list[float]) -> tuple[float, float, float]:
    """Normalize one point-like value into a 3D tuple."""

    return (float(point[0]), float(point[1]), float(point[2]))


def _normalize_axis_name(axis_name: str) -> str:
    """Normalize one signed axis string into +x/-x/+y/-y/+z/-z."""

    normalized = str(axis_name or "z").strip().lower()
    if normalized.startswith("+"):
        normalized = normalized[1:]
    sign = "-" if normalized.startswith("-") else "+"
    base_axis = normalized[1:] if normalized.startswith("-") else normalized
    if base_axis not in {"x", "y", "z"}:
        raise ValueError(f"不支持的轴向: {axis_name}")
    return f"{sign}{base_axis}"


def axis_unit_vector(axis_name: str) -> tuple[float, float, float]:
    """Convert one signed axis string into a unit direction vector."""

    normalized = _normalize_axis_name(axis_name)
    sign = -1.0 if normalized.startswith("-") else 1.0
    base_axis = normalized[1:]
    if base_axis == "x":
        return (sign, 0.0, 0.0)
    if base_axis == "y":
        return (0.0, sign, 0.0)
    if base_axis == "z":
        return (0.0, 0.0, sign)
    raise ValueError(f"不支持的轴向: {axis_name}")


def advance_point(point: tuple[float, float, float], axis_name: str, distance: float) -> tuple[float, float, float]:
    """Advance one point along a signed axis by the given distance."""

    ux, uy, uz = axis_unit_vector(axis_name)
    return (
        float(point[0]) + ux * float(distance),
        float(point[1]) + uy * float(distance),
        float(point[2]) + uz * float(distance),
    )


def _vector_add(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    """Add two 3D vectors."""

    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _vector_subtract(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    """Subtract one 3D vector from another."""

    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _vector_scale(vector: tuple[float, float, float], factor: float) -> tuple[float, float, float]:
    """Scale one 3D vector."""

    return (vector[0] * factor, vector[1] * factor, vector[2] * factor)


def _dot_product(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """Compute one dot product."""

    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross_product(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    """Compute one cross product."""

    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _vector_length(vector: tuple[float, float, float]) -> float:
    """Compute one 3D vector length."""

    return math.sqrt(_dot_product(vector, vector))


def _normalize_vector(vector: tuple[float, float, float], *, label: str) -> tuple[float, float, float]:
    """Normalize one non-zero 3D vector."""

    length = _vector_length(vector)
    if length <= VECTOR_TOLERANCE:
        raise ValueError(f"{label} 不能为零向量。")
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def _distance_between_points(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
) -> float:
    """Compute one Euclidean distance between two points."""

    return _vector_length(_vector_subtract(a, b))


def _axis_name_from_vector(vector: tuple[float, float, float]) -> str:
    """Resolve one signed axis name from a unit vector."""

    unit = _normalize_vector(vector, label="旋转后的端口轴向")
    absolute_components = [abs(component) for component in unit]
    dominant_index = absolute_components.index(max(absolute_components))
    if abs(absolute_components[dominant_index] - 1.0) > VECTOR_TOLERANCE:
        raise ValueError(f"旋转后的端口轴向未保持轴对齐: {unit}")
    for index, component in enumerate(unit):
        if index == dominant_index:
            continue
        if abs(component) > VECTOR_TOLERANCE:
            raise ValueError(f"旋转后的端口轴向未保持轴对齐: {unit}")
    axis_name = ("x", "y", "z")[dominant_index]
    sign = "+" if unit[dominant_index] >= 0 else "-"
    return f"{sign}{axis_name}"


def _try_axis_name_from_vector(vector: tuple[float, float, float]) -> str | None:
    """Resolve one signed axis name when the vector stays axis-aligned."""

    try:
        return _axis_name_from_vector(vector)
    except ValueError:
        return None


def _vector_label(vector: tuple[float, float, float]) -> str:
    """Format one unit direction vector for logs and returned metadata."""

    axis_name = _try_axis_name_from_vector(vector)
    if axis_name is not None:
        return axis_name
    return f"vector({vector[0]:.6f},{vector[1]:.6f},{vector[2]:.6f})"


def _connection_end_for_axis(axis_name: str) -> str:
    """Resolve which pipe end sits at the tangent point for one run axis."""

    normalized = _normalize_axis_name(axis_name)
    if normalized in {"+x", "+y"}:
        return "left_end"
    if normalized in {"-x", "-y"}:
        return "right_end"
    if normalized == "+z":
        return "bottom_end"
    if normalized == "-z":
        return "top_end"
    raise ValueError(f"不支持的轴向: {axis_name}")


def _resolve_run_direction(spec: PipeConnectionRunSpec) -> ResolvedRunDirection:
    """Resolve one run direction from either a signed axis or an explicit vector."""

    if spec.direction_vector_away_from_fitting is not None:
        direction_vector = _normalize_vector(
            _normalize_point3d(spec.direction_vector_away_from_fitting),
            label=f"{spec.run_id} 的 direction_vector_away_from_fitting",
        )
        axis_name = _try_axis_name_from_vector(direction_vector)
        return ResolvedRunDirection(
            direction_vector=direction_vector,
            direction_label=_vector_label(direction_vector),
            axis_name=axis_name,
        )
    axis_name = _normalize_axis_name(spec.axis_away_from_fitting)
    return ResolvedRunDirection(
        direction_vector=axis_unit_vector(axis_name),
        direction_label=axis_name,
        axis_name=axis_name,
    )


def _rotate_vector_about_axis(
    vector: tuple[float, float, float],
    *,
    axis_direction: tuple[float, float, float],
    angle_deg: float,
) -> tuple[float, float, float]:
    """Rotate one vector around one axis using Rodrigues' formula."""

    if abs(float(angle_deg)) <= VECTOR_TOLERANCE:
        return vector
    axis_unit = _normalize_vector(axis_direction, label="旋转轴方向")
    angle_rad = math.radians(float(angle_deg))
    cosine_value = math.cos(angle_rad)
    sine_value = math.sin(angle_rad)
    parallel_term = _vector_scale(vector, cosine_value)
    cross_term = _vector_scale(_cross_product(axis_unit, vector), sine_value)
    axis_term = _vector_scale(axis_unit, _dot_product(axis_unit, vector) * (1.0 - cosine_value))
    return _vector_add(_vector_add(parallel_term, cross_term), axis_term)


def _rotate_point_about_axis(
    point: tuple[float, float, float],
    *,
    base_point: tuple[float, float, float],
    axis_direction: tuple[float, float, float],
    angle_deg: float,
) -> tuple[float, float, float]:
    """Rotate one point around one axis that passes through base_point."""

    local_vector = _vector_subtract(point, base_point)
    rotated_vector = _rotate_vector_about_axis(
        local_vector,
        axis_direction=axis_direction,
        angle_deg=angle_deg,
    )
    return _vector_add(base_point, rotated_vector)


def _build_rotated_elbow_ports(
    *,
    elbow_center: tuple[float, float, float],
    bend_radius: float,
    elbow_rotations: list[RotationInstruction],
) -> list[ElbowConnectionPort]:
    """Build the two final elbow ports after all explicit rotations.

    The helper input elbow_center is the theoretical corner point where the two
    straight run centerlines would intersect. draw_pipe_elbow(), however,
    creates its local xy elbow from the unrotated corner layout:
    - port_x: tangent point at corner - R * x, run axis +x
    - port_y: tangent point at corner - R * y, run axis +y
    The solid is then rotated around the same corner point.
    """

    default_ports = [
        ElbowConnectionPort(
            port_id="leg_x",
            tangent_point_mm=advance_point(elbow_center, "-x", bend_radius),
            axis_away_from_fitting="+x",
            axis_vector=axis_unit_vector("+x"),
            radial_vector_mm=_vector_scale(axis_unit_vector("-x"), bend_radius),
            connection_end=_connection_end_for_axis("+x"),
        ),
        ElbowConnectionPort(
            port_id="leg_y",
            tangent_point_mm=advance_point(elbow_center, "-y", bend_radius),
            axis_away_from_fitting="+y",
            axis_vector=axis_unit_vector("+y"),
            radial_vector_mm=_vector_scale(axis_unit_vector("-y"), bend_radius),
            connection_end=_connection_end_for_axis("+y"),
        ),
    ]

    rotated_ports: list[ElbowConnectionPort] = []
    for port in default_ports:
        rotated_point = port.tangent_point_mm
        rotated_axis_vector = port.axis_vector
        rotated_radial_vector = port.radial_vector_mm
        for rotation in elbow_rotations:
            rotated_point = _rotate_point_about_axis(
                rotated_point,
                base_point=elbow_center,
                axis_direction=rotation.axis_direction,
                angle_deg=float(rotation.angle_deg),
            )
            rotated_axis_vector = _rotate_vector_about_axis(
                rotated_axis_vector,
                axis_direction=rotation.axis_direction,
                angle_deg=float(rotation.angle_deg),
            )
            rotated_radial_vector = _rotate_vector_about_axis(
                rotated_radial_vector,
                axis_direction=rotation.axis_direction,
                angle_deg=float(rotation.angle_deg),
            )
        rotated_axis_name = _axis_name_from_vector(rotated_axis_vector)
        rotated_ports.append(
            ElbowConnectionPort(
                port_id=port.port_id,
                tangent_point_mm=_normalize_point3d(rotated_point),
                axis_away_from_fitting=rotated_axis_name,
                axis_vector=axis_unit_vector(rotated_axis_name),
                radial_vector_mm=_normalize_point3d(rotated_radial_vector),
                connection_end=_connection_end_for_axis(rotated_axis_name),
            )
        )
    return rotated_ports


def _rotate_solid_from_z_to_direction(
    solid: Any,
    *,
    rotate_center: tuple[float, float, float],
    direction_vector: tuple[float, float, float],
) -> None:
    """Rotate one +Z-created solid so its axis follows the given direction."""

    target = _normalize_vector(direction_vector, label="目标直管方向")
    source = (0.0, 0.0, 1.0)
    alignment = max(-1.0, min(1.0, _dot_product(source, target)))
    if alignment >= 1.0 - VECTOR_TOLERANCE:
        return

    center_variant = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(rotate_center))
    if alignment <= -1.0 + VECTOR_TOLERANCE:
        axis_point = (
            rotate_center[0] + 1.0,
            rotate_center[1],
            rotate_center[2],
        )
        axis_variant = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(axis_point))
        solid.Rotate3D(center_variant, axis_variant, math.pi)
        return

    rotation_axis = _normalize_vector(_cross_product(source, target), label="圆管旋转轴")
    axis_point = _vector_add(rotate_center, rotation_axis)
    axis_variant = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(axis_point))
    solid.Rotate3D(center_variant, axis_variant, math.acos(alignment))


def _add_directional_cylinder(
    controller: Any,
    *,
    start_point: tuple[float, float, float],
    direction_vector: tuple[float, float, float],
    radius: float,
    length: float,
) -> Any:
    """Create one solid cylinder from one start point along an arbitrary direction."""

    if float(radius) <= 0 or float(length) <= 0:
        return None
    direction_unit = _normalize_vector(direction_vector, label="直管方向")
    start_point_mm = _normalize_point3d(start_point)
    center_point_mm = _vector_add(start_point_mm, _vector_scale(direction_unit, float(length) / 2.0))
    center_variant = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(center_point_mm))
    last_error: Exception | None = None
    for _ in range(4):
        try:
            model_space = controller._get_model_space()
            if model_space is None:
                time.sleep(0.4)
                continue
            solid = controller._call_with_retry(
                model_space.AddCylinder,
                center_variant,
                float(radius),
                float(length),
            )
            _rotate_solid_from_z_to_direction(
                solid,
                rotate_center=center_point_mm,
                direction_vector=direction_unit,
            )
            return solid
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    if last_error is not None:
        raise RuntimeError(f"任意方向圆柱创建失败: {last_error}") from last_error
    return None


def _add_directional_hollow_cylinder(
    controller: Any,
    *,
    start_point: tuple[float, float, float],
    direction_vector: tuple[float, float, float],
    outer_radius: float,
    length: float,
    wall_thickness: float,
    through_cut_extra: float = 1.0,
) -> Any:
    """Create one hollow pipe run from one start point along an arbitrary direction."""

    if float(outer_radius) <= 0 or float(length) <= 0:
        return None
    if wall_thickness is None or float(wall_thickness) <= 0:
        return _add_directional_cylinder(
            controller,
            start_point=start_point,
            direction_vector=direction_vector,
            radius=outer_radius,
            length=length,
        )
    inner_radius = float(outer_radius) - float(wall_thickness)
    if inner_radius <= 0:
        return _add_directional_cylinder(
            controller,
            start_point=start_point,
            direction_vector=direction_vector,
            radius=outer_radius,
            length=length,
        )
    direction_unit = _normalize_vector(direction_vector, label="空心直管方向")
    outer = _add_directional_cylinder(
        controller,
        start_point=start_point,
        direction_vector=direction_unit,
        radius=outer_radius,
        length=length,
    )
    if outer is None:
        return None
    inner_start = _vector_add(
        _normalize_point3d(start_point),
        _vector_scale(direction_unit, -float(through_cut_extra)),
    )
    inner = _add_directional_cylinder(
        controller,
        start_point=inner_start,
        direction_vector=direction_unit,
        radius=inner_radius,
        length=float(length) + float(through_cut_extra) * 2.0,
    )
    if inner is None:
        controller._safe_delete_entity(outer)
        return None
    if not controller._solid_boolean_subtract(outer, inner):
        controller._safe_delete_entity(inner)
        controller._safe_delete_entity(outer)
        raise RuntimeError("任意方向空心圆管布尔减失败。")
    controller._safe_delete_entity(inner)
    return outer


def _build_local_standard_elbow_ports(
    *,
    bend_center: tuple[float, float, float],
    bend_radius: float,
    angle_deg: float,
) -> list[ElbowConnectionPort]:
    """Build unrotated local elbow ports using the actual draw_pipe_elbow geometry.

    For standard planar elbow demos, the chosen axis direction should also match the
    visible top-view run extension so the drawn straight pipes land on the same side
    as the engineering reference sketch.
    """

    normalized_angle = int(round(float(angle_deg)))
    cx, cy, cz = bend_center
    root_half = math.sqrt(0.5)
    if normalized_angle == 45:
        port_definitions = [
            (
                "port_45_diag",
                (cx + bend_radius * root_half, cy + bend_radius * root_half, cz),
                (root_half, -root_half, 0.0),
                "tangent_end",
            ),
            (
                "port_45_top",
                (cx, cy + bend_radius, cz),
                (-1.0, 0.0, 0.0),
                "right_end",
            ),
        ]
    elif normalized_angle == 90:
        port_definitions = [
            (
                "port_90_right",
                (cx + bend_radius, cy, cz),
                (0.0, -1.0, 0.0),
                "top_end",
            ),
            (
                "port_90_top",
                (cx, cy + bend_radius, cz),
                (-1.0, 0.0, 0.0),
                "right_end",
            ),
        ]
    elif normalized_angle == 180:
        port_definitions = [
            (
                "port_180_left",
                (cx - bend_radius, cy, cz),
                (0.0, 1.0, 0.0),
                "bottom_end",
            ),
            (
                "port_180_right",
                (cx + bend_radius, cy, cz),
                (0.0, 1.0, 0.0),
                "bottom_end",
            ),
        ]
    else:
        raise ValueError(f"当前仅支持 45/90/180 度弯头，收到 angle={angle_deg}")

    local_ports: list[ElbowConnectionPort] = []
    for port_id, tangent_point, axis_vector, connection_end in port_definitions:
        normalized_axis = _normalize_vector(axis_vector, label=f"{port_id} 端口轴向")
        local_ports.append(
            ElbowConnectionPort(
                port_id=port_id,
                tangent_point_mm=_normalize_point3d(tangent_point),
                axis_away_from_fitting=_vector_label(normalized_axis),
                axis_vector=normalized_axis,
                radial_vector_mm=_vector_subtract(_normalize_point3d(tangent_point), bend_center),
                connection_end=connection_end,
            )
        )
    return local_ports


def _build_rotated_standard_elbow_ports(
    *,
    bend_center: tuple[float, float, float],
    bend_radius: float,
    angle_deg: float,
    elbow_rotations: list[RotationInstruction],
) -> list[ElbowConnectionPort]:
    """Build one 45/90/180 elbow's final ports after all rotations."""

    local_ports = _build_local_standard_elbow_ports(
        bend_center=bend_center,
        bend_radius=bend_radius,
        angle_deg=angle_deg,
    )
    rotated_ports: list[ElbowConnectionPort] = []
    for port in local_ports:
        tangent_point = port.tangent_point_mm
        axis_vector = port.axis_vector
        radial_vector = port.radial_vector_mm
        for rotation in elbow_rotations:
            tangent_point = _rotate_point_about_axis(
                tangent_point,
                base_point=bend_center,
                axis_direction=rotation.axis_direction,
                angle_deg=float(rotation.angle_deg),
            )
            axis_vector = _rotate_vector_about_axis(
                axis_vector,
                axis_direction=rotation.axis_direction,
                angle_deg=float(rotation.angle_deg),
            )
            radial_vector = _rotate_vector_about_axis(
                radial_vector,
                axis_direction=rotation.axis_direction,
                angle_deg=float(rotation.angle_deg),
            )
        normalized_axis = _normalize_vector(axis_vector, label=f"{port.port_id} 旋转后轴向")
        rotated_ports.append(
            ElbowConnectionPort(
                port_id=port.port_id,
                tangent_point_mm=_normalize_point3d(tangent_point),
                axis_away_from_fitting=_vector_label(normalized_axis),
                axis_vector=normalized_axis,
                radial_vector_mm=_normalize_point3d(radial_vector),
                connection_end=port.connection_end,
            )
        )
    return rotated_ports


def _prepare_standard_elbow_runs(run_specs: list[PipeConnectionRunSpec]) -> list[dict[str, Any]]:
    """Normalize pipe run specs for the generic 45/90/180 elbow builder."""

    if len(run_specs) != 2:
        raise ValueError("标准弯头连接当前仅支持两根直管。")
    prepared_runs: list[dict[str, Any]] = []
    for spec in run_specs:
        if float(spec.length_mm) <= 0:
            raise ValueError(f"{spec.run_id} 的长度必须大于 0。")
        resolved_direction = _resolve_run_direction(spec)
        connection_end = str(spec.connection_end or "").strip().lower()
        flow_role = str(spec.flow_role or "").strip()
        if not connection_end:
            raise ValueError(f"{spec.run_id} 缺少 connection_end。")
        prepared_runs.append(
            {
                "spec": spec,
                "tangent_point": _normalize_point3d(spec.tangent_point_mm),
                "direction": resolved_direction,
                "connection_end": connection_end,
                "flow_role": flow_role,
            }
        )
    return prepared_runs


def _validate_standard_elbow_run_specs(
    *,
    bend_center: tuple[float, float, float],
    bend_radius: float,
    angle_deg: float,
    run_specs: list[PipeConnectionRunSpec],
    elbow_rotations: list[RotationInstruction],
) -> tuple[list[dict[str, Any]], list[ElbowConnectionPort]]:
    """Validate geometric contact against the actual 45/90/180 elbow ports."""

    prepared_runs = _prepare_standard_elbow_runs(run_specs)
    rotated_ports = _build_rotated_standard_elbow_ports(
        bend_center=bend_center,
        bend_radius=bend_radius,
        angle_deg=angle_deg,
        elbow_rotations=elbow_rotations,
    )
    matched_port_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for run in prepared_runs:
        spec = run["spec"]
        tangent_point = run["tangent_point"]
        direction: ResolvedRunDirection = run["direction"]

        matched_port: ElbowConnectionPort | None = None
        matched_axial_shift = 0.0
        matched_lateral_distance = 0.0
        for port in rotated_ports:
            if port.port_id in matched_port_ids:
                continue
            point_delta = _vector_subtract(tangent_point, port.tangent_point_mm)
            axial_shift = _dot_product(point_delta, port.axis_vector)
            lateral_vector = _vector_subtract(point_delta, _vector_scale(port.axis_vector, axial_shift))
            lateral_distance = _vector_length(lateral_vector)
            axis_alignment = _dot_product(direction.direction_vector, port.axis_vector)
            if lateral_distance <= POINT_TOLERANCE_MM and axis_alignment > 1.0 - VECTOR_TOLERANCE:
                expected_end = port.connection_end
                if expected_end != "tangent_end" and run["connection_end"] != expected_end:
                    raise ValueError(
                        f"{spec.run_id} 的 connection_end={run['connection_end']} 与端口 {port.port_id} "
                        f"要求的 {expected_end} 不一致。"
                    )
                matched_port = port
                matched_axial_shift = axial_shift
                matched_lateral_distance = lateral_distance
                break

        if matched_port is None:
            port_summary = "; ".join(
                f"{port.port_id}: 点={tuple(round(v, 3) for v in port.tangent_point_mm)}, "
                f"轴向={port.axis_away_from_fitting}, end={port.connection_end}"
                for port in rotated_ports
            )
            raise ValueError(
                f"{spec.run_id} 未能匹配到弯头端口。当前 run: 点={tuple(round(v, 3) for v in tangent_point)}, "
                f"轴向={direction.direction_label}, connection_end={run['connection_end']}。可用端口: {port_summary}"
            )

        matched_port_ids.add(matched_port.port_id)
        validated.append(
            {
                **run,
                "matched_port": matched_port,
                "face_parallel_cosine": _dot_product(direction.direction_vector, matched_port.axis_vector),
                "center_distance_mm": _distance_between_points(tangent_point, matched_port.tangent_point_mm),
                "axial_shift_mm": float(matched_axial_shift),
                "lateral_offset_mm": float(matched_lateral_distance),
            }
        )
    return validated, rotated_ports


def _resolve_run_draw_pose(run: dict[str, Any]) -> dict[str, Any]:
    """Resolve the exact draw anchor after optional elbow-port snapping."""

    matched_port = run.get("matched_port")
    if matched_port is not None:
        direction_vector = _normalize_vector(
            matched_port.axis_vector,
            label=f"{run['spec'].run_id} 的弯头端口轴向",
        )
        axis_name = _try_axis_name_from_vector(direction_vector)
        return {
            "tangent_point": _normalize_point3d(matched_port.tangent_point_mm),
            "direction_vector": direction_vector,
            "direction_label": matched_port.axis_away_from_fitting,
            "axis_name": axis_name,
        }

    if "direction" in run:
        direction: ResolvedRunDirection = run["direction"]
        return {
            "tangent_point": _normalize_point3d(run["tangent_point"]),
            "direction_vector": direction.direction_vector,
            "direction_label": direction.direction_label,
            "axis_name": direction.axis_name,
        }

    axis_vector = _normalize_vector(run["axis_vector"], label=f"{run['spec'].run_id} 的直管轴向")
    axis_name = run.get("axis_name") or _axis_name_from_vector(axis_vector)
    return {
        "tangent_point": _normalize_point3d(run["tangent_point"]),
        "direction_vector": axis_vector,
        "direction_label": axis_name,
        "axis_name": axis_name,
    }


def build_standard_elbow_connection(
    controller: Any,
    *,
    dn: int,
    bend_center: tuple[float, float, float],
    elbow_type: str,
    angle_deg: float,
    run_specs: Iterable[PipeConnectionRunSpec],
    elbow_rotations: Iterable[RotationInstruction],
    layer: str,
    color: int,
    validate_run_specs: bool = True,
    draw_runs: bool = True,
) -> dict[str, Any]:
    """Build one 45/90/180 standard elbow and two pipes with flush, colinear ports."""

    bend_center_mm = _normalize_point3d(bend_center)
    run_spec_list = list(run_specs)
    rotation_list = list(elbow_rotations)
    outer_diameter = resolve_pipe_outer_diameter(controller, dn)
    outer_radius = outer_diameter / 2.0
    wall_thickness = resolve_pipe_wall_thickness(controller, dn)
    bend_radius = resolve_elbow_bend_radius(controller, dn, elbow_type)
    if validate_run_specs:
        validated_runs, elbow_ports = _validate_standard_elbow_run_specs(
            bend_center=bend_center_mm,
            bend_radius=bend_radius,
            angle_deg=angle_deg,
            run_specs=run_spec_list,
            elbow_rotations=rotation_list,
        )
    else:
        validated_runs = _prepare_standard_elbow_runs(run_spec_list)
        elbow_ports = _build_rotated_standard_elbow_ports(
            bend_center=bend_center_mm,
            bend_radius=bend_radius,
            angle_deg=angle_deg,
            elbow_rotations=rotation_list,
        )

    built_runs: list[dict[str, Any]] = []
    for index, run in enumerate(validated_runs, start=1):
        spec = run["spec"]
        requested_tangent_point = _normalize_point3d(run["tangent_point"])
        draw_pose = _resolve_run_draw_pose(run)
        tangent_point = draw_pose["tangent_point"]
        direction_vector = draw_pose["direction_vector"]
        direction_label = draw_pose["direction_label"]
        flow_role = run["flow_role"] or f"pipe_{index}"
        if draw_runs:
            entity = _add_directional_hollow_cylinder(
                controller,
                start_point=tangent_point,
                direction_vector=direction_vector,
                outer_radius=outer_radius,
                length=float(spec.length_mm),
                wall_thickness=wall_thickness,
            )
            if entity is None:
                raise RuntimeError(f"{spec.run_id} 创建失败。")
            apply_entity_style(controller, entity, layer, color)
        built_runs.append(
            {
                "run_id": spec.run_id,
                "base_point_mm": list(tangent_point),
                "tangent_point_mm": list(tangent_point),
                "requested_tangent_point_mm": list(requested_tangent_point),
                "snap_shift_mm": float(_distance_between_points(requested_tangent_point, tangent_point)),
                "far_end_point_mm": list(
                    _vector_add(tangent_point, _vector_scale(direction_vector, float(spec.length_mm)))
                ),
                "length_mm": float(spec.length_mm),
                "axis": direction_label,
                "axis_vector": list(direction_vector),
                "connection_end": run["connection_end"],
                "flow_role": flow_role,
                "matched_port_id": run["matched_port"].port_id if "matched_port" in run else None,
                "center_distance_mm": float(run["center_distance_mm"]) if "center_distance_mm" in run else None,
                "face_parallel_cosine": float(run["face_parallel_cosine"]) if "face_parallel_cosine" in run else None,
            }
        )

    elbow_entity = None
    for _ in range(4):
        try:
            elbow_entity = controller.draw_pipe_elbow(
                center=bend_center_mm,
                dn=int(dn),
                elbow_type=elbow_type,
                angle=float(angle_deg),
                plane="xy",
                layer=layer,
                color=color,
            )
            if elbow_entity is not None:
                break
        except Exception:
            pass
        time.sleep(0.6)
    elbow_handle = extract_entity_handle(elbow_entity)
    if elbow_handle is None:
        raise RuntimeError("弯头创建失败。")

    for rotation in rotation_list:
        if abs(float(rotation.angle_deg)) <= VECTOR_TOLERANCE:
            continue
        rotate_entity_with_retry(
            controller,
            elbow_handle,
            base_point=bend_center_mm,
            angle=float(rotation.angle_deg),
            axis_direction=rotation.axis_direction,
        )

    flow_path = {
        "kind": "serial_pipe_elbow_pipe",
        "sequence": [
            built_runs[0]["flow_role"] if built_runs else "pipe_1",
            f"elbow_{int(round(float(angle_deg)))}deg",
            built_runs[1]["flow_role"] if len(built_runs) > 1 else "pipe_2",
        ],
        "entry_run_id": built_runs[0]["run_id"] if built_runs else None,
        "exit_run_id": built_runs[1]["run_id"] if len(built_runs) > 1 else None,
        "description": "流体沿 pipe_1 进入弯头，再经弯头转向后流入 pipe_2；当前模型不是三通，不存在第三个泄漏出口。",
    }

    return {
        "dn": int(dn),
        "outer_diameter_mm": outer_diameter,
        "outer_radius_mm": outer_radius,
        "wall_thickness_mm": wall_thickness,
        "elbow_type": elbow_type,
        "angle_deg": float(angle_deg),
        "bend_radius_mm": bend_radius,
        "bend_center_mm": list(bend_center_mm),
        "runs": built_runs,
        "draw_runs": bool(draw_runs),
        "validation_mode": "standard_port_match" if validate_run_specs else "compatibility_no_run_match",
        "elbow_ports": [asdict(port) for port in elbow_ports],
        "elbow_rotations": [asdict(rotation) for rotation in rotation_list],
        "flow_path": flow_path,
    }


def _prepare_run_specs(run_specs: list[PipeConnectionRunSpec]) -> list[dict[str, Any]]:
    """Normalize and pre-validate the raw pipe run specifications."""

    if len(run_specs) != 2:
        raise ValueError("当前 helper 仅支持两根正交直管通过一个 90° 弯头连接。")

    prepared_runs: list[dict[str, Any]] = []
    for spec in run_specs:
        if float(spec.length_mm) <= 0:
            raise ValueError(f"{spec.run_id} 的长度必须大于 0。")
        axis_name = _normalize_axis_name(spec.axis_away_from_fitting)
        connection_end = str(spec.connection_end or "").strip().lower()
        if not connection_end:
            raise ValueError(f"{spec.run_id} 缺少 connection_end。")
        prepared_runs.append(
            {
                "spec": spec,
                "axis_name": axis_name,
                "axis_vector": axis_unit_vector(axis_name),
                "tangent_point": _normalize_point3d(spec.tangent_point_mm),
                "connection_end": connection_end,
                "flow_role": str(spec.flow_role or "").strip(),
            }
        )
    return prepared_runs


def _validate_run_specs(
    *,
    elbow_center: tuple[float, float, float],
    bend_radius: float,
    run_specs: list[PipeConnectionRunSpec],
    elbow_rotations: list[RotationInstruction],
) -> tuple[list[dict[str, Any]], list[ElbowConnectionPort]]:
    """Validate that every run matches one final rotated elbow port exactly."""

    prepared_runs = _prepare_run_specs(run_specs)
    rotated_ports = _build_rotated_elbow_ports(
        elbow_center=elbow_center,
        bend_radius=bend_radius,
        elbow_rotations=elbow_rotations,
    )

    matched_port_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for run in prepared_runs:
        spec = run["spec"]
        tangent_point = run["tangent_point"]
        axis_vector = run["axis_vector"]

        matched_port: ElbowConnectionPort | None = None
        matched_axial_shift = 0.0
        matched_lateral_distance = 0.0
        for port in rotated_ports:
            if port.port_id in matched_port_ids:
                continue
            point_delta = _vector_subtract(tangent_point, port.tangent_point_mm)
            axial_shift = _dot_product(point_delta, port.axis_vector)
            lateral_vector = _vector_subtract(point_delta, _vector_scale(port.axis_vector, axial_shift))
            lateral_distance = _vector_length(lateral_vector)
            port_axis_alignment = _dot_product(axis_vector, port.axis_vector)
            if (
                lateral_distance <= POINT_TOLERANCE_MM
                and port_axis_alignment > 1.0 - VECTOR_TOLERANCE
                and run["connection_end"] == port.connection_end
            ):
                matched_port = port
                matched_axial_shift = axial_shift
                matched_lateral_distance = lateral_distance
                break

        if matched_port is None:
            port_summary = "; ".join(
                f"{port.port_id}: 点={tuple(round(value, 3) for value in port.tangent_point_mm)}, "
                f"轴向={port.axis_away_from_fitting}, end={port.connection_end}"
                for port in rotated_ports
            )
            raise ValueError(
                f"{spec.run_id} 未能匹配到旋转后的弯头端口。"
                f"当前 run: 点={tuple(round(value, 3) for value in tangent_point)}, 轴向={run['axis_name']}, "
                f"connection_end={run['connection_end']}。可用端口: {port_summary}"
            )

        matched_port_ids.add(matched_port.port_id)
        validated.append(
            {
                **run,
                "radial_vector": matched_port.radial_vector_mm,
                "matched_port": matched_port,
                "face_parallel_cosine": _dot_product(axis_vector, matched_port.axis_vector),
                "center_distance_mm": _distance_between_points(tangent_point, matched_port.tangent_point_mm),
                "axial_shift_mm": float(matched_axial_shift),
                "lateral_offset_mm": float(matched_lateral_distance),
            }
        )

    axis_dot = _dot_product(validated[0]["axis_vector"], validated[1]["axis_vector"])
    if abs(axis_dot) > VECTOR_TOLERANCE:
        raise ValueError("两根相连直管的轴向必须相互垂直。")

    radial_unit_a = _normalize_vector(validated[0]["radial_vector"], label="第一个切点半径向量")
    radial_unit_b = _normalize_vector(validated[1]["radial_vector"], label="第二个切点半径向量")
    radial_dot = _dot_product(radial_unit_a, radial_unit_b)
    if abs(radial_dot) > VECTOR_TOLERANCE:
        raise ValueError("两处切点对应的弯头半径向量必须相互垂直。")

    return validated, rotated_ports


def apply_entity_style(controller: Any, entity: Any, layer: str, color: int) -> None:
    """Apply layer and color after a CAD solid has been created."""

    for _ in range(4):
        try:
            controller.create_layer(layer)
            entity.Layer = layer
            entity.Color = color
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"实体样式设置失败: {layer}")


def extract_entity_handle(entity: Any) -> str | None:
    """Extract one CAD entity handle."""

    if entity is None:
        return None
    for _ in range(4):
        try:
            return str(entity.Handle)
        except Exception:
            time.sleep(0.4)
    return None


def rotate_entity_with_retry(
    controller: Any,
    handle: str,
    *,
    base_point: tuple[float, float, float],
    angle: float,
    axis_direction: tuple[float, float, float],
    retries: int = 5,
    wait_seconds: float = 0.8,
) -> None:
    """Rotate one entity reliably when the CAD COM layer is busy."""

    for _ in range(retries):
        try:
            if controller.rotate_entity(handle, base_point, angle, axis_direction=axis_direction):
                return
        except Exception:
            pass
        time.sleep(wait_seconds)
    raise RuntimeError(f"实体旋转失败: handle={handle}, angle={angle}, axis={axis_direction}")


def resolve_pipe_outer_diameter(controller: Any, dn: int) -> float:
    """Resolve one standard outer diameter from DN."""

    dn_value = int(dn)
    table = getattr(controller, "ELBOW_DN_OD_GB12459", {})
    if dn_value not in table:
        raise KeyError(f"缺少 DN{dn_value} 对应外径。")
    return float(table[dn_value])


def resolve_pipe_wall_thickness(controller: Any, dn: int) -> float:
    """Resolve one standard wall thickness from DN."""

    wall_thickness = controller._resolve_default_fitting_wall_thickness_gb12459(int(dn))
    if wall_thickness is None or float(wall_thickness) <= 0:
        raise ValueError(f"缺少 DN{dn} 对应壁厚。")
    return float(wall_thickness)


def resolve_elbow_bend_radius(controller: Any, dn: int, elbow_type: str) -> float:
    """Resolve one standard bend radius using the same convention as draw_pipe_elbow."""

    outer_diameter = resolve_pipe_outer_diameter(controller, dn)
    normalized = str(elbow_type or "LR").strip().upper()
    if normalized == "SR":
        return outer_diameter
    if normalized == "3D":
        return outer_diameter * 3.0
    return outer_diameter * 1.5


def build_orthogonal_elbow_connection(
    controller: Any,
    *,
    dn: int,
    elbow_center: tuple[float, float, float],
    elbow_type: str,
    run_specs: Iterable[PipeConnectionRunSpec],
    elbow_rotations: Iterable[RotationInstruction],
    layer: str,
    color: int,
    validate_run_specs: bool = True,
    draw_runs: bool = True,
) -> dict[str, Any]:
    """Build one standard 90-degree elbow and optionally its two orthogonal pipe runs."""

    corner_point_mm = _normalize_point3d(elbow_center)
    run_spec_list = list(run_specs)
    rotation_list = list(elbow_rotations)
    outer_diameter = resolve_pipe_outer_diameter(controller, dn)
    outer_radius = outer_diameter / 2.0
    wall_thickness = resolve_pipe_wall_thickness(controller, dn)
    bend_radius = resolve_elbow_bend_radius(controller, dn, elbow_type)
    unrotated_bend_center_mm = (
        corner_point_mm[0] - bend_radius,
        corner_point_mm[1] - bend_radius,
        corner_point_mm[2],
    )
    elbow_ports = _build_rotated_elbow_ports(
        elbow_center=corner_point_mm,
        bend_radius=bend_radius,
        elbow_rotations=rotation_list,
    )
    if validate_run_specs:
        validated_runs, elbow_ports = _validate_run_specs(
            elbow_center=corner_point_mm,
            bend_radius=bend_radius,
            run_specs=run_spec_list,
            elbow_rotations=rotation_list,
        )
    else:
        validated_runs = _prepare_run_specs(run_spec_list)

    built_runs: list[dict[str, Any]] = []
    for index, run in enumerate(validated_runs, start=1):
        spec = run["spec"]
        requested_tangent_point = _normalize_point3d(run["tangent_point"])
        draw_pose = _resolve_run_draw_pose(run)
        tangent_point = draw_pose["tangent_point"]
        axis_name = draw_pose["axis_name"]
        if axis_name is None:
            raise ValueError(f"{spec.run_id} 的弯头端口轴向不是正交轴，不能使用正交 helper 绘制。")
        flow_role = run["flow_role"] or f"pipe_{index}"
        if draw_runs:
            entity = controller._add_oriented_hollow_cylinder(
                tangent_point,
                outer_radius,
                float(spec.length_mm),
                axis_name,
                wall_thickness=wall_thickness,
            )
            if entity is None:
                raise RuntimeError(f"{spec.run_id} 创建失败。")
            apply_entity_style(controller, entity, layer, color)
        built_runs.append(
            {
                "run_id": spec.run_id,
                "base_point_mm": list(tangent_point),
                "tangent_point_mm": list(tangent_point),
                "requested_tangent_point_mm": list(requested_tangent_point),
                "snap_shift_mm": float(_distance_between_points(requested_tangent_point, tangent_point)),
                "far_end_point_mm": list(advance_point(tangent_point, axis_name, float(spec.length_mm))),
                "length_mm": float(spec.length_mm),
                "axis": axis_name,
                "connection_end": run["connection_end"],
                "flow_role": flow_role,
                "matched_port_id": run["matched_port"].port_id if "matched_port" in run else None,
                "center_distance_mm": float(run["center_distance_mm"]) if "center_distance_mm" in run else None,
                "face_parallel_cosine": float(run["face_parallel_cosine"]) if "face_parallel_cosine" in run else None,
            }
        )

    elbow_entity = None
    for _ in range(4):
        try:
            elbow_entity = controller.draw_pipe_elbow(
                center=unrotated_bend_center_mm,
                dn=int(dn),
                elbow_type=elbow_type,
                angle=90,
                plane="xy",
                layer=layer,
                color=color,
            )
            if elbow_entity is not None:
                break
        except Exception:
            pass
        time.sleep(0.6)
    elbow_handle = extract_entity_handle(elbow_entity)
    if elbow_handle is None:
        raise RuntimeError("弯头创建失败。")

    for rotation in rotation_list:
        if abs(float(rotation.angle_deg)) <= 1e-6:
            continue
        rotate_entity_with_retry(
            controller,
            elbow_handle,
            base_point=corner_point_mm,
            angle=float(rotation.angle_deg),
            axis_direction=rotation.axis_direction,
        )

    radial_vector_a = elbow_ports[0].radial_vector_mm
    radial_vector_b = elbow_ports[1].radial_vector_mm
    bend_center_mm = (
        corner_point_mm[0] + radial_vector_a[0] + radial_vector_b[0],
        corner_point_mm[1] + radial_vector_a[1] + radial_vector_b[1],
        corner_point_mm[2] + radial_vector_a[2] + radial_vector_b[2],
    )
    flow_path = {
        "kind": "serial_pipe_elbow_pipe",
        "sequence": [
            built_runs[0]["flow_role"] if built_runs else "pipe_1",
            "elbow_90deg",
            built_runs[1]["flow_role"] if len(built_runs) > 1 else "pipe_2",
        ],
        "entry_run_id": built_runs[0]["run_id"] if built_runs else None,
        "exit_run_id": built_runs[1]["run_id"] if len(built_runs) > 1 else None,
        "description": "流体沿 pipe_1 进入 90 度弯头，经弯头改变方向后流入 pipe_2；当前模型是串联弯头连接，不是三通分流。",
    }

    return {
        "dn": int(dn),
        "outer_diameter_mm": outer_diameter,
        "outer_radius_mm": outer_radius,
        "wall_thickness_mm": wall_thickness,
        "elbow_type": elbow_type,
        "bend_radius_mm": bend_radius,
        "elbow_center_mm": list(bend_center_mm),
        "corner_point_mm": list(corner_point_mm),
        "runs": built_runs,
        "draw_runs": bool(draw_runs),
        "validation_mode": "rotated_port_match" if validate_run_specs else "compatibility_no_run_match",
        "elbow_handle": elbow_handle,
        "elbow_ports": [asdict(port) for port in elbow_ports],
        "elbow_rotations": [asdict(rotation) for rotation in rotation_list],
        "flow_path": flow_path,
    }

