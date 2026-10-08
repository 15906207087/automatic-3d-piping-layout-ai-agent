from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

CURRENT_FILE = Path(__file__).resolve()
SRC_DIR = CURRENT_FILE.parents[1]
PROJECT_DIR = CURRENT_FILE.parents[2]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from parametric_models.cycle_water_equipment_from_json import (
    CADService,
    draw_auxiliary_equipment,
    save_drawing_with_retry,
)
from parametric_models import cycle_water_dd_rebuilt as dd_rebuilt
from parametric_models import cycle_water_pipeline_from_json as pipeline

OUTPUT_TOPIC = "循环水"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
TARGET_TAGS = ("Y-0202", "TK-0202")
TARGET_ROUTE_PREFIX = "ii_preview_local_dn150"


def load_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Load the shared cycle-water JSONs used by the main pipeline."""

    dimension_info = pipeline.load_json(pipeline.DIMENSION_INFO_PATH)
    section_info = pipeline.load_json(pipeline.SECTION_INFO_PATH)
    section_route_connection_info = pipeline.load_json(pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH)
    list_info = pipeline.load_json(pipeline.LIST_INFO_PATH)
    return list_info, dimension_info, section_info, section_route_connection_info


def build_compiled_layout(
    dimension_info: dict[str, Any],
    section_info: dict[str, Any],
    section_route_connection_info: dict[str, Any],
) -> dict[str, Any]:
    """Compile the dynamic pipeline layout with absolute equipment anchors."""

    plan_reference = dimension_info.get("model_input", {}).get("plan_reference", {})
    center = [float(value) for value in plan_reference.get("basin_outline_center", [0.0, 0.0, 0.0])]
    basin_geometry = {
        "center_x": center[0],
        "center_y": center[1],
        "center_z": center[2],
        "length": float(plan_reference.get("basin_length", 0.0)),
        "width": float(plan_reference.get("basin_width", 0.0)),
        "height": float(plan_reference.get("basin_height", 0.0)),
        "wall_thickness_mm": float(plan_reference.get("basin_wall_thickness", 0.0)),
    }
    return pipeline.build_dynamic_pipeline_layout(
        dimension_info,
        section_info,
        section_route_connection_info,
        basin_geometry,
    )


def select_ii_routes(compiled_layout: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep only the compiled I-I local and upper-symmetric routes."""

    selected: list[dict[str, Any]] = []
    for route in compiled_layout.get("section_preview_routes", []):
        route_id = str(route.get("route_id", ""))
        if route_id == TARGET_ROUTE_PREFIX or route_id.startswith(f"{TARGET_ROUTE_PREFIX}_"):
            selected.append(route)
    if not selected:
        raise RuntimeError("编译布局中未找到 I-I 相关路由。")
    return selected


def select_equipment(dimension_info: dict[str, Any]) -> list[dict[str, Any]]:
    """Return Y-0202 and TK-0202 auxiliary equipment records."""

    equipment_centers = dimension_info.get("model_input", {}).get("equipment_centers", {})
    selected: list[dict[str, Any]] = []
    for equipment in equipment_centers.get("auxiliary_equipments", []):
        if str(equipment.get("tag", "")).strip() in TARGET_TAGS:
            selected.append(equipment)
    missing = [tag for tag in TARGET_TAGS if tag not in {str(item.get("tag", "")).strip() for item in selected}]
    if missing:
        raise RuntimeError(f"auxiliary_equipments 缺少: {', '.join(missing)}")
    return selected


def estimate_view(route_records: list[dict[str, Any]], equipments: list[dict[str, Any]]) -> tuple[tuple[float, float, float], float, float]:
    """Estimate a local camera frame around the tank and I-I cluster."""

    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for record in route_records:
        for key in ("start", "end", "center"):
            point = record.get(key)
            if isinstance(point, (list, tuple)) and len(point) >= 3:
                xs.append(float(point[0]))
                ys.append(float(point[1]))
                zs.append(float(point[2]))
    for equipment in equipments:
        center_xy = [float(value) for value in equipment.get("center_xy", [0.0, 0.0])]
        bottom = float(equipment.get("bottom_elevation_mm", 0.0))
        height = float(equipment.get("height_mm", equipment.get("proxy_height_mm", 200.0)))
        half = float(
            equipment.get(
                "outer_diameter_mm",
                max(float(value) for value in (equipment.get("plan_size_mm") or [1000.0])),
            )
        ) / 2.0
        xs.extend([center_xy[0] - half, center_xy[0] + half])
        ys.extend([center_xy[1] - half, center_xy[1] + half])
        zs.extend([bottom, bottom + height])
    if not xs:
        return (0.0, 0.0, 0.0), 6000.0, 6000.0
    center = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0, (min(zs) + max(zs)) / 2.0)
    height = max(max(zs) - min(zs), 3000.0) * 1.35
    width = max(max(xs) - min(xs), max(ys) - min(ys), 3000.0) * 1.35
    return center, height, width


def write_artifacts(
    *,
    version_dir: Path,
    version: int,
    output_stem: str,
    equipments: list[dict[str, Any]],
    routes: list[dict[str, Any]],
    route_records: list[dict[str, Any]],
    dwg_path: Path,
    preview_paths: list[str],
) -> tuple[Path, Path]:
    """Persist parameters and notes for the focused tank + I-I validation."""

    parameters_path = version_dir / f"{output_stem}_parameters_v{version}.json"
    notes_path = version_dir / f"{output_stem}_notes_v{version}.txt"
    payload = {
        "topic": OUTPUT_TOPIC,
        "version": version,
        "dwg_path": str(dwg_path),
        "equipments": equipments,
        "routes": [
            {
                "route_id": route.get("route_id"),
                "route_kind": route.get("route_kind"),
                "drop_leg_y": route.get("drop_leg_y"),
                "top_branch_length_mm": route.get("top_branch_length_mm"),
                "top_branch_target_equipment_tag": route.get("top_branch_target_equipment_tag"),
                "top_branch_connect_mode": route.get("top_branch_connect_mode"),
                "section_rotation_deg_z": route.get("section_rotation_deg_z"),
                "basis": route.get("basis"),
            }
            for route in routes
        ],
        "route_records": route_records,
        "preview_paths": preview_paths,
        "shared_json": {
            "list_info": str(pipeline.LIST_INFO_PATH),
            "dimension_info": str(pipeline.DIMENSION_INFO_PATH),
            "section_info": str(pipeline.SECTION_INFO_PATH),
            "section_route_connection_info": str(pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH),
        },
    }
    parameters_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    notes = [
        f"{OUTPUT_TOPIC} Y-0202 顶部罐 + I-I 接入验证 v{version}",
        "",
        f"DWG: {dwg_path}",
        "范围: 只画 Y-0202、TK-0202，以及下侧/上侧 I-I 本体与其平面外接链。",
        "",
        "本轮尺寸决策:",
        "- TK-0202 外径 1110 mm，壁厚 10 mm，高度 5000 mm",
        "- 平面中心左移到 I-I DN150 列位 (-4300, 5850)",
        "- 底标高 +0.200，顶开口、底实心、中间空心",
        "- 上下 I-I 顶部 DN150 横管标高 +4.500，长度按罐外壁射线交点回算",
        "",
        "预览图:",
        *[f"- {path}" for path in preview_paths],
    ]
    notes_path.write_text("\n".join(notes), encoding="utf-8")
    return parameters_path, notes_path


def main() -> int:
    """Draw the absolute-coordinate Y-0202 tank + I-I connection validation."""

    _, dimension_info, section_info, section_route_connection_info = load_inputs()
    compiled_layout = build_compiled_layout(dimension_info, section_info, section_route_connection_info)
    equipments = select_equipment(dimension_info)
    routes = select_ii_routes(compiled_layout)

    version, version_dir = pipeline.prepare_version_dir(OUTPUT_ROOT_DIR, minimum_version=pipeline.PIPELINE_VERSION_FLOOR)
    output_stem = f"{OUTPUT_TOPIC}_y0202_tank_ii"
    dwg_path = version_dir / f"{output_stem}_3D_v{version}.dwg"

    service = CADService()
    try:
        dd_rebuilt.create_validation_document(service)
    except RuntimeError:
        if not service.start_cad():
            raise
        active_doc = getattr(service.controller.app, "ActiveDocument", None)
        if active_doc is None:
            raise
        service.controller.doc = active_doc

    for equipment in equipments:
        draw_auxiliary_equipment(service, equipment)

    route_records: list[dict[str, Any]] = []
    for route in routes:
        pipeline.draw_section_preview_route(service, route=route, route_records=route_records)

    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    model_center, height, width = estimate_view(route_records, equipments)
    preview_paths = dd_rebuilt.capture_previews(
        service,
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        model_center=model_center,
        height=height,
        width=width,
    )
    parameters_path, notes_path = write_artifacts(
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        equipments=equipments,
        routes=routes,
        route_records=route_records,
        dwg_path=dwg_path,
        preview_paths=preview_paths,
    )
    print(
        json.dumps(
            {
                "success": True,
                "version": version,
                "version_dir": str(version_dir),
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
                "parameters_path": str(parameters_path),
                "notes_path": str(notes_path),
                "route_ids": [route.get("route_id") for route in routes],
                "top_branch_lengths_mm": {
                    str(route.get("route_id")): route.get("top_branch_length_mm")
                    for route in routes
                    if route.get("route_kind") == "ii_dn150_local_drop_preview"
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
