from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CURRENT_FILE = Path(__file__).resolve()
SRC_DIR = CURRENT_FILE.parents[1]
PROJECT_DIR = CURRENT_FILE.parents[2]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from parametric_models.cycle_water_equipment_from_json import CADService, save_drawing_with_retry
from parametric_models import cycle_water_dd_rebuilt as dd_rebuilt
from parametric_models import cycle_water_pipeline_from_json as pipeline

LOCAL_OUTPUT_TOPIC = "循环水_局部剖面图"
LOCAL_OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / LOCAL_OUTPUT_TOPIC


@dataclass(frozen=True)
class LocalSectionJob:
    """Describe one local-only validation drawing target."""

    section: str
    route_id: str
    output_suffix: str
    local_anchor_x_mm: float
    local_y_mm: float


LOCAL_SECTION_JOBS: dict[str, LocalSectionJob] = {
    "A-A": LocalSectionJob(
        section="A-A",
        route_id="aa_preview_second_embedded_pipe",
        output_suffix="aa_local",
        local_anchor_x_mm=0.0,
        local_y_mm=0.0,
    ),
    "F-F": LocalSectionJob(
        section="F-F",
        route_id="ff_preview_local_dn150",
        output_suffix="ff_local",
        local_anchor_x_mm=0.0,
        local_y_mm=0.0,
    ),
    "H-H": LocalSectionJob(
        section="H-H",
        route_id="hh_preview_left_tower_drop",
        output_suffix="hh_local",
        local_anchor_x_mm=0.0,
        local_y_mm=0.0,
    ),
    "I-I": LocalSectionJob(
        section="I-I",
        route_id="ii_preview_local_dn150",
        output_suffix="ii_local",
        local_anchor_x_mm=0.0,
        local_y_mm=0.0,
    ),
    "B-B": LocalSectionJob(
        section="B-B",
        route_id="bb_preview_local_dn100",
        output_suffix="bb_local",
        local_anchor_x_mm=0.0,
        local_y_mm=0.0,
    ),
    "C-C": LocalSectionJob(
        section="C-C",
        route_id="cc_preview_left_wall_dn200",
        output_suffix="cc_local",
        local_anchor_x_mm=0.0,
        local_y_mm=0.0,
    ),
    "D-D": LocalSectionJob(
        section="D-D",
        route_id="dd_preview_p0204_left_dn200_branch",
        output_suffix="dd_local",
        local_anchor_x_mm=0.0,
        local_y_mm=0.0,
    ),
    "E-E": LocalSectionJob(
        section="E-E",
        route_id="ee_preview_third_sleeve",
        output_suffix="ee_local",
        local_anchor_x_mm=0.0,
        local_y_mm=0.0,
    ),
    "G-G": LocalSectionJob(
        section="G-G",
        route_id="gg_preview_first_embedded_pipe",
        output_suffix="gg_local",
        local_anchor_x_mm=0.0,
        local_y_mm=0.0,
    ),
}


def normalize_section_name(raw_value: str) -> str:
    """Normalize one user-provided section token like 'ff' or 'F-F'."""

    token = str(raw_value).strip().upper().replace("—", "-").replace("_", "-")
    if token and "-" not in token and len(token) == 2:
        token = f"{token[0]}-{token[1]}"
    return token


def sanitize_stem(value: str) -> str:
    """Convert one display label into a filesystem-safe stem."""

    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", str(value).strip())
    return cleaned.strip("_") or "local_section"


def find_section_entry(section_route_connection_info: dict[str, Any], section_name: str) -> dict[str, Any]:
    """Return one section entry from the shared route connection JSON."""

    for entry in section_route_connection_info.get("sections", []):
        if normalize_section_name(str(entry.get("section", ""))) == normalize_section_name(section_name):
            return entry
    raise RuntimeError(f"未在 section_route_connection_info 中找到剖面 {section_name}。")


def find_preview_route(dimension_info: dict[str, Any], route_id: str) -> dict[str, Any]:
    """Return one preview route entry by route_id from the shared layout JSON."""

    layout = (
        dimension_info.get("model_input", {})
        .get("pipeline_layout_v2", {})
    )
    for route in layout.get("section_preview_routes", []):
        if str(route.get("route_id", "")).strip() == route_id:
            return dict(route)
    raise RuntimeError(f"未在 dimension_info 中找到局部路由 {route_id}。")


def build_basin_geometry_for_layout(dimension_info: dict[str, Any]) -> dict[str, Any]:
    """Build the minimal basin geometry bundle required by the compiled pipeline layout."""

    plan_reference = dimension_info.get("model_input", {}).get("plan_reference", {})
    center = [float(value) for value in plan_reference.get("basin_outline_center", [0.0, 0.0, 0.0])]
    if len(center) != 3:
        raise RuntimeError("dimension_info.model_input.plan_reference.basin_outline_center 格式无效。")
    return {
        "center_x": center[0],
        "center_y": center[1],
        "center_z": center[2],
        "length": float(plan_reference.get("basin_length", 0.0)),
        "width": float(plan_reference.get("basin_width", 0.0)),
        "height": float(plan_reference.get("basin_height", 0.0)),
        "wall_thickness_mm": float(plan_reference.get("basin_wall_thickness", 0.0)),
    }


def find_compiled_preview_route(compiled_layout: dict[str, Any], route_id: str) -> dict[str, Any]:
    """Return one compiled preview route from the dynamic layout."""

    for route in compiled_layout.get("section_preview_routes", []):
        if str(route.get("route_id", "")).strip() == route_id:
            return dict(route)
    raise RuntimeError(f"未在编译后的局部路由中找到 {route_id}。")


def build_local_route(
    *,
    job: LocalSectionJob,
    dimension_info: dict[str, Any],
    compiled_layout: dict[str, Any],
    section_route_connection_info: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one relocated local-preview route for standalone validation drawing."""

    section_entry = find_section_entry(section_route_connection_info, job.section)
    normalized = normalize_section_name(job.section)
    if normalized in {"A-A", "C-C", "D-D"}:
        local_route = find_compiled_preview_route(compiled_layout, job.route_id)
        local_route["dn"] = int(local_route.get("dn", local_route.get("branch_dn", 0)))
        local_route["section_route_entry"] = section_entry
        local_route["section_route_source"] = str(pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH)
        local_route["basis"] = (
            f"{str(local_route.get('basis', '')).strip()} "
            f"当前为局部验证图，仅保留 {job.section} 的局部剖面链，不代表总图绝对定位。"
        ).strip()
        return local_route, section_entry

    preview_route = find_preview_route(dimension_info, job.route_id)
    local_route = dict(preview_route)
    local_route["dn"] = int(preview_route.get("branch_dn", preview_route.get("dn", 0)))
    local_route["anchor_x"] = float(job.local_anchor_x_mm)
    local_route["section_route_entry"] = section_entry
    local_route["section_route_source"] = str(pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH)
    local_route["basis"] = (
        f"{preview_route.get('basis', '').strip()} "
        f"当前为局部验证图，仅保留 {job.section} 的局部剖面链，不代表总图绝对定位。"
    ).strip()
    if normalized == "F-F":
        local_route["left_vertical_y"] = float(job.local_y_mm)
    elif normalized == "B-B":
        local_route["section_plane_y"] = float(job.local_y_mm)
    elif normalized == "G-G":
        branch_dn = int(preview_route.get("branch_dn", preview_route.get("dn", 0)))
        right_riser_dn = int(preview_route.get("right_riser_dn", 250))
        left_vertical_offset = float(preview_route.get("left_vertical_center_offset_from_right_riser_mm", 1000.0))
        valve_structure_length = float(preview_route.get("valve_structure_length_mm", 0.0))
        local_route["base_y"] = 0.0
        local_route["base_z"] = float(preview_route.get("header_circle_elevation_mm", -1200.0))
        local_route["right_riser_dn"] = right_riser_dn
        local_route["left_vertical_y"] = -left_vertical_offset
        local_route["top_horizontal_elevation_mm"] = float(preview_route.get("top_horizontal_elevation_mm", 1000.0))
        local_route["bottom_horizontal_elevation_mm"] = float(preview_route.get("bottom_horizontal_elevation_mm", -650.0))
        local_route["bottom_stub_length_mm"] = float(preview_route.get("bottom_stub_length_mm", 0.0))
        local_route["header_tee_branch_center_to_end_mm"] = float(
            preview_route.get("header_tee_branch_center_to_end_mm", 0.0)
        )
        local_route["header_tee_item_no"] = preview_route.get("header_tee_item_no")
        local_route["header_tee_proxy_run_dn"] = int(preview_route.get("header_tee_proxy_run_dn", 450))
        local_route["riser_tee_item_no"] = preview_route.get("riser_tee_item_no")
        local_route["riser_tee_run_dn"] = int(preview_route.get("riser_tee_run_dn", right_riser_dn))
        local_route["riser_tee_branch_dn"] = int(preview_route.get("riser_tee_branch_dn", branch_dn))
        local_route["valve_item_no"] = preview_route.get("valve_item_no")
        local_route["valve_pn"] = float(preview_route.get("valve_pn", 10.0))
        local_route["valve_structure_length_mm"] = valve_structure_length
        local_route["valve_center_y"] = -float(
            preview_route.get("valve_center_offset_from_right_riser_mm", left_vertical_offset / 2.0)
        )
        local_route["elbow_item_no"] = preview_route.get("elbow_item_no")
        local_route["top_elbow_rotation_deg"] = float(preview_route.get("top_elbow_rotation_deg", -90.0))
        local_route["top_elbow_rotation_axis"] = str(preview_route.get("top_elbow_rotation_axis", "y"))
        local_route["top_elbow_secondary_rotation_deg"] = float(
            preview_route.get("top_elbow_secondary_rotation_deg", 180.0)
        )
        local_route["top_elbow_secondary_rotation_axis"] = str(
            preview_route.get("top_elbow_secondary_rotation_axis", "z")
        )
        local_route["bottom_elbow_rotation_deg"] = float(preview_route.get("bottom_elbow_rotation_deg", 90.0))
        local_route["bottom_elbow_rotation_axis"] = str(preview_route.get("bottom_elbow_rotation_axis", "y"))
        local_route["bottom_elbow_secondary_rotation_deg"] = float(
            preview_route.get("bottom_elbow_secondary_rotation_deg", 0.0)
        )
        local_route["bottom_elbow_secondary_rotation_axis"] = str(
            preview_route.get("bottom_elbow_secondary_rotation_axis", "x")
        )
    elif normalized == "E-E":
        local_route["base_y"] = 0.0
        local_route["base_z"] = -1200.0
        local_route["base_header_dn"] = 500
        local_route["right_vertical_y"] = 1500.0
        local_route["top_horizontal_elevation_mm"] = float(preview_route.get("top_horizontal_elevation_mm", 2200.0))
        local_route["right_vertical_bottom_elevation_mm"] = float(
            preview_route.get("right_vertical_bottom_elevation_mm", 1900.0)
        )
        local_route["tee_branch_center_to_end_mm"] = float(preview_route.get("tee_branch_center_to_end_mm", 308.0))
        local_route["tee_branch_display_offset_mm"] = float(preview_route.get("tee_branch_center_to_end_mm", 308.0))
        local_route["tee_proxy_run_dn"] = int(preview_route.get("tee_proxy_run_dn", 450))
        local_route["fitting_item_no"] = preview_route.get("fitting_item_no")
        local_route["fitting_specification"] = preview_route.get("fitting_specification")
        local_route["valve_item_no"] = preview_route.get("valve_item_no")
        local_route["valve_center_elevation_mm"] = float(preview_route.get("valve_center_elevation_mm", 1200.0))
        local_route["valve_structure_length_mm"] = float(preview_route.get("valve_structure_length_mm", 76.0))
        local_route["valve_pn"] = float(preview_route.get("valve_pn", 10.0))
        local_route["elbow_item_no"] = preview_route.get("elbow_item_no")
        local_route["left_elbow_type"] = str(preview_route.get("left_elbow_type", "LR"))
        local_route["right_elbow_type"] = str(preview_route.get("right_elbow_type", "LR"))
        local_route["left_elbow_connection_trim_mm"] = float(preview_route.get("left_elbow_connection_trim_mm", 409.5))
        local_route["right_elbow_connection_trim_mm"] = (
            float(preview_route["right_elbow_connection_trim_mm"])
            if preview_route.get("right_elbow_connection_trim_mm") is not None
            else None
        )
        local_route["left_elbow_rotation_deg"] = float(preview_route.get("left_elbow_rotation_deg", -90.0))
        local_route["left_elbow_rotation_axis"] = str(preview_route.get("left_elbow_rotation_axis", "y"))
        local_route["left_elbow_secondary_rotation_deg"] = float(
            preview_route.get("left_elbow_secondary_rotation_deg", 180.0)
        )
        local_route["left_elbow_secondary_rotation_axis"] = str(
            preview_route.get("left_elbow_secondary_rotation_axis", "z")
        )
        local_route["right_elbow_rotation_deg"] = float(preview_route.get("right_elbow_rotation_deg", -90.0))
        local_route["right_elbow_rotation_axis"] = str(preview_route.get("right_elbow_rotation_axis", "y"))
        local_route["right_elbow_secondary_rotation_deg"] = float(
            preview_route.get("right_elbow_secondary_rotation_deg", 0.0)
        )
        local_route["right_elbow_secondary_rotation_axis"] = str(
            preview_route.get("right_elbow_secondary_rotation_axis", "x")
        )
    elif normalized in {"H-H", "I-I"}:
        local_route["drop_leg_y"] = float(job.local_y_mm)
    else:
        raise RuntimeError(f"暂不支持的局部剖面: {job.section}")
    return local_route, section_entry


def draw_local_route(
    service: CADService,
    *,
    job: LocalSectionJob,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw one standalone local section by reusing the route-specific drawer."""

    normalized = normalize_section_name(job.section)
    if normalized == "A-A":
        pipeline.draw_aa_section_preview_route(service, route=route, route_records=route_records)
        return
    if normalized == "F-F":
        pipeline.draw_ff_section_preview_route(service, route=route, route_records=route_records)
        return
    if normalized == "H-H":
        pipeline.draw_hh_section_preview_route(service, route=route, route_records=route_records)
        return
    if normalized == "I-I":
        pipeline.draw_ii_section_preview_route(service, route=route, route_records=route_records)
        return
    if normalized == "B-B":
        pipeline.draw_bb_section_preview_route(service, route=route, route_records=route_records)
        return
    if normalized == "C-C":
        pipeline.draw_cc_section_preview_route(service, route=route, route_records=route_records)
        return
    if normalized == "D-D":
        pipeline.draw_dd_section_preview_route(service, route=route, route_records=route_records)
        return
    if normalized == "E-E":
        pipeline.draw_ee_section_preview_route(service, route=route, route_records=route_records)
        return
    if normalized == "G-G":
        pipeline.draw_gg_section_preview_route(service, route=route, route_records=route_records)
        return
    raise RuntimeError(f"暂不支持的局部剖面: {job.section}")


def collect_route_points(route_records: list[dict[str, Any]]) -> list[list[float]]:
    """Collect every route point used for viewport estimation."""

    points: list[list[float]] = []
    for record in route_records:
        if record.get("record_type") == "segment":
            points.append([float(value) for value in record["start"]])
            points.append([float(value) for value in record["end"]])
        elif record.get("record_type") == "fitting":
            points.append([float(value) for value in record["center"]])
    if not points:
        raise RuntimeError("局部图未生成任何几何点，无法估算视图。")
    return points


def estimate_local_view(route_records: list[dict[str, Any]]) -> tuple[tuple[float, float, float], float, float]:
    """Estimate a focused camera box for one standalone local section."""

    points = collect_route_points(route_records)
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    zs = [point[2] for point in points]
    center = (
        (min(xs) + max(xs)) / 2.0,
        (min(ys) + max(ys)) / 2.0,
        (min(zs) + max(zs)) / 2.0,
    )
    height = max(3200.0, (max(zs) - min(zs)) * 1.8)
    width = max(3600.0, max(max(xs) - min(xs), max(ys) - min(ys)) * 2.2)
    return center, height, width


def write_artifacts(
    *,
    version_dir: Path,
    version: int,
    output_stem: str,
    job: LocalSectionJob,
    section_entry: dict[str, Any],
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
    dwg_path: Path,
    preview_paths: list[str],
) -> tuple[Path, Path]:
    """Write one parameters snapshot and one notes file for the local validation drawing."""

    parameters_path = version_dir / f"{output_stem}_parameters_v{version}.json"
    notes_path = version_dir / f"{output_stem}_notes_v{version}.txt"
    parameters_payload = {
        "topic": LOCAL_OUTPUT_TOPIC,
        "section": job.section,
        "route_id": route.get("route_id"),
        "dwg_path": str(dwg_path),
        "preview_paths": preview_paths,
        "source_jsons": {
            "dimension_info": str(pipeline.DIMENSION_INFO_PATH),
            "section_info": str(pipeline.SECTION_INFO_PATH),
            "section_route_connection_info": str(pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH),
        },
        "section_entry": section_entry,
        "route": route,
        "route_records": route_records,
    }
    parameters_path.write_text(json.dumps(parameters_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    text_record = section_entry.get("text_record", {})
    analysis_lines = [str(line) for line in text_record.get("analysis", []) if str(line).strip()]
    notes = [
        f"{LOCAL_OUTPUT_TOPIC} {job.section} 局部验证 v{version}",
        "",
        f"DWG: {dwg_path}",
        f"剖面: {job.section}",
        f"route_id: {route.get('route_id')}",
        "用途: 只验证局部剖面里的管道走向和直接零件链，不进入整体总图。",
        "",
        "本轮读取:",
        f"- {pipeline.DIMENSION_INFO_PATH}",
        f"- {pipeline.SECTION_INFO_PATH}",
        f"- {pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH}",
        "",
        "识图摘要:",
        *([f"- {line}" for line in analysis_lines] or ["- 无"]),
        "",
        "预览图:",
        *[f"- {path}" for path in preview_paths],
    ]
    notes_path.write_text("\n".join(notes), encoding="utf-8")
    return parameters_path, notes_path


def run_one_section(
    *,
    job: LocalSectionJob,
    version_dir: Path,
    version: int,
    dimension_info: dict[str, Any],
    compiled_layout: dict[str, Any],
    section_route_connection_info: dict[str, Any],
) -> dict[str, Any]:
    """Draw one standalone local section and return generated artifact paths."""

    route, section_entry = build_local_route(
        job=job,
        dimension_info=dimension_info,
        compiled_layout=compiled_layout,
        section_route_connection_info=section_route_connection_info,
    )
    output_stem = sanitize_stem(f"{pipeline.OUTPUT_TOPIC}_{job.output_suffix}")
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
    route_records: list[dict[str, Any]] = []
    draw_local_route(service, job=job, route=route, route_records=route_records)
    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"{job.section} 局部 DWG 保存失败: {dwg_path}")

    model_center, height, width = estimate_local_view(route_records)
    preview_paths = dd_rebuilt.capture_previews(
        service,
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        model_center=model_center,
        height=height,
        width=width,
    )
    write_artifacts(
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        job=job,
        section_entry=section_entry,
        route=route,
        route_records=route_records,
        dwg_path=dwg_path,
        preview_paths=preview_paths,
    )
    return {
        "section": job.section,
        "dwg_path": str(dwg_path),
        "preview_paths": preview_paths,
    }


def parse_requested_sections(raw_value: str) -> list[LocalSectionJob]:
    """Parse one comma-separated section list into runnable jobs."""

    requested: list[LocalSectionJob] = []
    for token in [part for part in str(raw_value).split(",") if str(part).strip()]:
        normalized = normalize_section_name(token)
        job = LOCAL_SECTION_JOBS.get(normalized)
        if job is None:
            raise RuntimeError(f"不支持的局部剖面: {token}。当前仅支持 A-A,B-B,C-C,D-D,E-E,F-F,G-G,H-H,I-I。")
        requested.append(job)
    if not requested:
        raise RuntimeError("未指定任何局部剖面。")
    return requested


def main() -> int:
    """Run standalone local validation drawings for supported sections."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--sections", default="A-A,B-B,C-C,D-D,E-E,F-F,G-G,H-H,I-I")
    args = parser.parse_args()

    jobs = parse_requested_sections(args.sections)
    dimension_info = pipeline.load_json(pipeline.DIMENSION_INFO_PATH)
    section_info = pipeline.load_json(pipeline.SECTION_INFO_PATH)
    section_route_connection_info = pipeline.load_json(pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH)
    basin_geometry = build_basin_geometry_for_layout(dimension_info)
    compiled_layout = pipeline.build_dynamic_pipeline_layout(
        dimension_info,
        section_info,
        section_route_connection_info,
        basin_geometry,
    )

    version, version_dir = pipeline.prepare_version_dir(LOCAL_OUTPUT_ROOT_DIR, minimum_version=1)
    results = [
        run_one_section(
            job=job,
            version_dir=version_dir,
            version=version,
            dimension_info=dimension_info,
            compiled_layout=compiled_layout,
            section_route_connection_info=section_route_connection_info,
        )
        for job in jobs
    ]
    print(json.dumps({"version": version, "version_dir": str(version_dir), "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
