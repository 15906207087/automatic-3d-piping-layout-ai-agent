from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CURRENT_FILE = Path(__file__).resolve()
SRC_DIR = CURRENT_FILE.parents[1]
PROJECT_DIR = CURRENT_FILE.parents[2]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pipeline_connection_helper import (
    PipeConnectionRunSpec,
    RotationInstruction,
    apply_entity_style,
    build_orthogonal_elbow_connection,
    extract_entity_handle,
    resolve_elbow_bend_radius,
    resolve_pipe_outer_diameter,
    resolve_pipe_wall_thickness,
    rotate_entity_with_retry,
)
from parametric_models.dn250_elbow_demo import (
    CADService,
    WHITE,
    capture_previews,
    create_fresh_document,
    prepare_version_dir,
    save_drawing_with_retry,
    zoom_extents_with_retry,
)

OUTPUT_TOPIC = "EE左侧弯头helper校准"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC


@dataclass(frozen=True)
class ProbeCandidate:
    """One local elbow assembly probe candidate."""

    candidate_id: str
    mode: str
    elbow_center_mm: tuple[float, float, float]
    horizontal_length_mm: float = 1600.0
    vertical_length_mm: float = 900.0
    rotation_deg: float = 90.0
    rotation_axis: str = "y"


def resolve_rotation_axis(axis_name: str) -> tuple[float, float, float]:
    """Map one axis name to a Rotate3D direction vector."""

    axis = str(axis_name or "y").strip().lower()
    if axis == "x":
        return (1.0, 0.0, 0.0)
    if axis == "z":
        return (0.0, 0.0, 1.0)
    return (0.0, 1.0, 0.0)


def draw_probe_pipe(
    controller: Any,
    *,
    base_point: tuple[float, float, float],
    axis: str,
    length_mm: float,
    dn: int,
    layer: str,
) -> dict[str, Any]:
    """Draw one hollow probe pipe from one tangent point."""

    outer_radius = resolve_pipe_outer_diameter(controller, dn) / 2.0
    wall_thickness = resolve_pipe_wall_thickness(controller, dn)
    entity = controller._add_oriented_hollow_cylinder(
        base_point,
        outer_radius,
        float(length_mm),
        axis,
        wall_thickness=wall_thickness,
    )
    if entity is None:
        raise RuntimeError(f"探针直管创建失败: base={base_point}, axis={axis}")
    apply_entity_style(controller, entity, layer, WHITE)
    axis_vector = {
        "+x": (1.0, 0.0, 0.0),
        "-x": (-1.0, 0.0, 0.0),
        "+y": (0.0, 1.0, 0.0),
        "-y": (0.0, -1.0, 0.0),
        "+z": (0.0, 0.0, 1.0),
        "-z": (0.0, 0.0, -1.0),
    }[axis]
    far_end = (
        float(base_point[0]) + axis_vector[0] * float(length_mm),
        float(base_point[1]) + axis_vector[1] * float(length_mm),
        float(base_point[2]) + axis_vector[2] * float(length_mm),
    )
    return {
        "base_point_mm": list(base_point),
        "far_end_point_mm": list(far_end),
        "axis": axis,
        "length_mm": float(length_mm),
    }


def draw_direct_reference(
    service: CADService,
    *,
    candidate: ProbeCandidate,
    dn: int,
    elbow_type: str,
    bend_radius: float,
    layer: str,
) -> dict[str, Any]:
    """Draw one legacy-style direct elbow reference using the expected E-E left mapping."""

    controller = service.controller
    cx, cy, cz = candidate.elbow_center_mm
    horizontal_tangent_point = (cx, cy - bend_radius, cz)
    vertical_tangent_point = (cx, cy, cz + bend_radius)
    unrotated_bend_center = (cx - bend_radius, cy - bend_radius, cz)
    horizontal_pipe = draw_probe_pipe(
        controller,
        base_point=horizontal_tangent_point,
        axis="+y",
        length_mm=candidate.horizontal_length_mm,
        dn=dn,
        layer=layer,
    )
    vertical_pipe = draw_probe_pipe(
        controller,
        base_point=vertical_tangent_point,
        axis="-z",
        length_mm=candidate.vertical_length_mm,
        dn=dn,
        layer=layer,
    )
    elbow_entity = controller.draw_pipe_elbow(
        center=unrotated_bend_center,
        dn=dn,
        elbow_type=elbow_type,
        angle=90,
        plane="xy",
        layer=layer,
        color=WHITE,
    )
    elbow_handle = extract_entity_handle(elbow_entity)
    if elbow_handle is None:
        raise RuntimeError("直接参考弯头创建失败。")
    rotate_entity_with_retry(
        controller,
        elbow_handle,
        base_point=candidate.elbow_center_mm,
        angle=float(candidate.rotation_deg),
        axis_direction=resolve_rotation_axis(candidate.rotation_axis),
    )
    return {
        "candidate_id": candidate.candidate_id,
        "mode": candidate.mode,
        "elbow_center_mm": list(candidate.elbow_center_mm),
        "horizontal_tangent_point_mm": list(horizontal_tangent_point),
        "vertical_tangent_point_mm": list(vertical_tangent_point),
        "horizontal_pipe": horizontal_pipe,
        "vertical_pipe": vertical_pipe,
        "rotation_deg": float(candidate.rotation_deg),
        "rotation_axis": candidate.rotation_axis,
        "basis": "直接参考件：把 candidate.elbow_center_mm 当作理论转角点，先将未旋转的 xy 弯头放在 corner+(-xR)+(-yR) 的实体中心，再绕该转角点沿 y 轴正向旋转 90°，得到 +y 与 -z 两条腿。",
    }


def draw_helper_reference(
    service: CADService,
    *,
    candidate: ProbeCandidate,
    dn: int,
    elbow_type: str,
    bend_radius: float,
    layer: str,
) -> dict[str, Any]:
    """Draw one helper-generated elbow assembly using the same E-E left mapping."""

    cx, cy, cz = candidate.elbow_center_mm
    horizontal_tangent_point = (cx, cy - bend_radius, cz)
    vertical_tangent_point = (cx, cy, cz + bend_radius)
    geometry = build_orthogonal_elbow_connection(
        service.controller,
        dn=dn,
        elbow_center=candidate.elbow_center_mm,
        elbow_type=elbow_type,
        run_specs=[
            PipeConnectionRunSpec(
                run_id=f"{candidate.candidate_id}_horizontal",
                tangent_point_mm=horizontal_tangent_point,
                axis_away_from_fitting="+y",
                length_mm=float(candidate.horizontal_length_mm),
                connection_end="left_end",
            ),
            PipeConnectionRunSpec(
                run_id=f"{candidate.candidate_id}_vertical",
                tangent_point_mm=vertical_tangent_point,
                axis_away_from_fitting="-z",
                length_mm=float(candidate.vertical_length_mm),
                connection_end="top_end",
            ),
        ],
        elbow_rotations=[
            RotationInstruction(
                angle_deg=float(candidate.rotation_deg),
                axis_direction=resolve_rotation_axis(candidate.rotation_axis),
                axis_name=candidate.rotation_axis,
            )
        ],
        layer=layer,
        color=WHITE,
    )
    return {
        "candidate_id": candidate.candidate_id,
        "mode": candidate.mode,
        "elbow_center_mm": geometry["elbow_center_mm"],
        "horizontal_tangent_point_mm": list(horizontal_tangent_point),
        "vertical_tangent_point_mm": list(vertical_tangent_point),
        "runs": geometry["runs"],
        "rotation_deg": float(candidate.rotation_deg),
        "rotation_axis": candidate.rotation_axis,
        "basis": "helper 参考件：沿同一组交点、切点和旋转参数，通过公共 helper 重建左侧弯头连接。",
    }


def build_probe_geometry(service: CADService) -> dict[str, Any]:
    """Draw the direct and helper candidates side by side in one blank drawing."""

    dn = 250
    elbow_type = "LR"
    controller = service.controller
    bend_radius = resolve_elbow_bend_radius(controller, dn, elbow_type)
    layer = f"PIPE_DN{dn}"
    candidates = [
        ProbeCandidate(
            candidate_id="legacy_reference",
            mode="direct_reference",
            elbow_center_mm=(-1200.0, 0.0, 0.0),
        ),
        ProbeCandidate(
            candidate_id="helper_reference",
            mode="helper_reference",
            elbow_center_mm=(1200.0, 0.0, 0.0),
        ),
    ]
    results = []
    for candidate in candidates:
        if candidate.mode == "direct_reference":
            results.append(
                draw_direct_reference(
                    service,
                    candidate=candidate,
                    dn=dn,
                    elbow_type=elbow_type,
                    bend_radius=bend_radius,
                    layer=layer,
                )
            )
            continue
        results.append(
            draw_helper_reference(
                service,
                candidate=candidate,
                dn=dn,
                elbow_type=elbow_type,
                bend_radius=bend_radius,
                layer=layer,
            )
        )
    if not zoom_extents_with_retry(controller):
        raise RuntimeError("局部校准图缩放失败。")
    return {
        "topic": OUTPUT_TOPIC,
        "dn": dn,
        "elbow_type": elbow_type,
        "bend_radius_mm": bend_radius,
        "expected_leg_mapping": {
            "default_xy_90": ["+x", "+y"],
            "rotate_y_plus_90": ["-z", "+y"],
        },
        "candidates": results,
    }


def write_probe_artifacts(
    version_dir: Path,
    version: int,
    *,
    dwg_path: Path,
    preview_paths: list[str],
    geometry: dict[str, Any],
) -> tuple[Path, Path]:
    """Write one JSON snapshot and one notes file for this probe run."""

    parameters_path = version_dir / f"{OUTPUT_TOPIC}_parameters_v{version}.json"
    notes_path = version_dir / f"{OUTPUT_TOPIC}_notes_v{version}.txt"
    parameters_path.write_text(
        json.dumps(
            {
                "topic": OUTPUT_TOPIC,
                "version": version,
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
                "geometry": geometry,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    notes_path.write_text(
        "\n".join(
            [
                f"{OUTPUT_TOPIC} 校准说明 v{version}",
                "",
                f"DWG: {dwg_path}",
                f"国标 LR 弯头半径 R = {geometry['bend_radius_mm']:.3f} mm。",
                "本轮在空白图里并排绘制两组局部：左侧为直接参考件，右侧为 helper 生成件。",
                "目标验证：draw_pipe_elbow 默认 xy 90° 弯头经 y 轴 +90° 旋转后，两条腿应分别对应 +y 与 -z。",
                "人工检查要点：两组局部都应满足横管在 +y 切点接入、竖管以上端在 -z 切点接入，且 helper 生成件不应相对直接参考件出现留缝、断开或翻向错误。",
            ]
        ),
        encoding="utf-8",
    )
    return parameters_path, notes_path


def main() -> int:
    """Build the local E-E left elbow helper probe drawing."""

    version, version_dir = prepare_version_dir(OUTPUT_ROOT_DIR)
    dwg_path = version_dir / f"{OUTPUT_TOPIC}_3D_v{version}.dwg"
    service = CADService()
    create_fresh_document(service)
    geometry = build_probe_geometry(service)
    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")
    preview_paths = capture_previews(
        service,
        version_dir,
        version,
        model_center=(0.0, 900.0, -450.0),
    )
    parameters_path, notes_path = write_probe_artifacts(
        version_dir,
        version,
        dwg_path=dwg_path,
        preview_paths=preview_paths,
        geometry=geometry,
    )
    print(
        json.dumps(
            {
                "success": True,
                "topic": OUTPUT_TOPIC,
                "version": version,
                "version_dir": str(version_dir),
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
                "parameters_path": str(parameters_path),
                "notes_path": str(notes_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
