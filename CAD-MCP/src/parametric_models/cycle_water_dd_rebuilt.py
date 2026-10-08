from __future__ import annotations

import argparse
import json
import sys
import time
from ctypes import windll
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import win32gui

CURRENT_FILE = Path(__file__).resolve()
SRC_DIR = CURRENT_FILE.parents[1]
PROJECT_DIR = CURRENT_FILE.parents[2]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from screen_capture import ScreenCapture
from parametric_models.cycle_water_equipment_from_json import (
    CADService,
    save_drawing_with_retry,
    set_named_view_with_retry,
)
from parametric_models import cycle_water_pipeline_from_json as pipeline


@dataclass
class DDSectionSpec:
    """Store one rebuilt D-D section route compiled from the shared JSONs."""

    route_id: str
    section: str
    view_role: str
    view_direction: str
    branch_dn: int
    anchor_reference: str
    anchor_x_mm: float
    horizontal_center_y_mm: float
    top_elevation_mm: float
    horizontal_elevation_mm: float
    horizontal_offset_length_mm: float
    equipment_short_connect_length_mm: float
    upper_elbow_item_no: int | None
    lower_elbow_item_no: int | None
    sleeve_item_no: int | None
    orifice_item_no: int | None
    upper_elbow_rotation_deg: float
    upper_elbow_rotation_axis: str
    upper_elbow_secondary_rotation_deg: float
    upper_elbow_secondary_rotation_axis: str
    lower_elbow_rotation_deg: float
    lower_elbow_rotation_axis: str
    lower_elbow_secondary_rotation_deg: float
    lower_elbow_secondary_rotation_axis: str
    basis: str
    analysis_lines: list[str]


@dataclass
class DDGeometry:
    """Hold the connection skeleton used to draw the rebuilt D-D route."""

    elbow_connection_trim_mm: float
    upper_elbow_corner: list[float]
    lower_elbow_corner: list[float]
    vertical_start: list[float]
    vertical_tangent: list[float]
    horizontal_start: list[float]
    horizontal_end: list[float]
    equipment_short_start: list[float]
    equipment_short_end: list[float]
    sleeve_center: list[float]
    all_points: list[list[float]]


def create_validation_document(service: CADService) -> None:
    """Create one fresh CAD document with retries for COM jitter."""

    if not service.start_cad():
        raise RuntimeError("无法启动或连接 CAD。")
    docs = getattr(service.controller.app, "Documents", None)
    if docs is None:
        raise RuntimeError("CAD 未暴露 Documents 集合，无法创建新文档。")
    last_error: Exception | None = None
    for _ in range(6):
        try:
            docs.Add()
            time.sleep(2.0)
            service.controller.doc = service.controller.app.ActiveDocument
            return
        except Exception as exc:
            last_error = exc
            time.sleep(1.0)
    raise RuntimeError(f"验证文档创建失败: {last_error}")


def copy_source_image_if_needed(source_image: str | None) -> str | None:
    """Archive the source image into the topic shared folder when provided."""

    if not source_image:
        return None
    source_path = Path(source_image).expanduser()
    if not source_path.exists():
        return None
    destination = pipeline.SHARED_DIR / source_path.name
    pipeline.copy_if_missing(source_path, destination)
    return str(destination)


def find_section_entry(section_route_connection_info: dict[str, Any], section_name: str) -> dict[str, Any]:
    """Return the requested section entry from the route connection JSON."""

    for entry in section_route_connection_info.get("sections", []):
        if str(entry.get("section", "")).strip().upper() == section_name.upper():
            return entry
    raise RuntimeError(f"未在 section_route_connection_info 中找到剖面 {section_name}。")


def find_dd_preview_route(dimension_info: dict[str, Any]) -> dict[str, Any]:
    """Find the D-D dedicated preview route without using the old layout compiler."""

    stack: list[Any] = [dimension_info]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if (
                str(current.get("section", "")).strip().upper() == "D-D"
                and str(current.get("route_kind", "")).strip().lower() == "dd_dn200_turn_preview"
            ):
                return current
            stack.extend(current.values())
            continue
        if isinstance(current, list):
            stack.extend(current)
    raise RuntimeError("未在 dimension_info 中找到 D-D 局部路由参数。")


def build_dd_section_spec(
    *,
    dimension_info: dict[str, Any],
    section_route_connection_info: dict[str, Any],
) -> DDSectionSpec:
    """Compile one standalone D-D route spec directly from the shared JSONs."""

    dd_section_entry = find_section_entry(section_route_connection_info, "D-D")
    dd_preview_route = find_dd_preview_route(dimension_info)
    horizontal_run = pipeline.find_section_route_pipe_run(dd_section_entry, "dd_horizontal_turn_dn200") or {}
    riser_run = pipeline.find_section_route_pipe_run(dd_section_entry, "dd_local_riser_dn200") or {}
    equipment_run = pipeline.find_section_route_pipe_run(dd_section_entry, "dd_equipment_short_connect_dn200") or {}
    view_basis = dd_section_entry.get("view_basis", {})
    text_record = dd_section_entry.get("text_record", {})
    analysis_lines = [str(line) for line in text_record.get("analysis", []) if str(line).strip()]
    basis_lines = [
        str(line)
        for line in text_record.get("modeling_description", [])
        if str(line).strip()
    ]
    basis = "\n".join(basis_lines) if basis_lines else str(dd_preview_route.get("basis", "")).strip()
    return DDSectionSpec(
        route_id=str(dd_preview_route.get("route_id", "dd_rebuilt")),
        section="D-D",
        view_role=str(view_basis.get("role", "D-D 局部 DN200 转向剖面")),
        view_direction=str(view_basis.get("view_direction", "从池内支路一侧看向设备基础侧")),
        branch_dn=int(dd_preview_route.get("branch_dn", horizontal_run.get("dn", 200))),
        anchor_reference=str(dd_preview_route.get("anchor_reference", dd_section_entry.get("plan_anchor", {}).get("resolved_anchor_id", ""))),
        anchor_x_mm=float(dd_preview_route.get("anchor_x_mm", dd_section_entry.get("plan_anchor", {}).get("resolved_anchor_x_mm", 0.0))),
        horizontal_center_y_mm=float(dd_preview_route.get("horizontal_pass_through_y_mm", 9850.0)),
        top_elevation_mm=float(dd_preview_route.get("top_elevation_mm", riser_run.get("from_elevation_mm", 1650.0))),
        horizontal_elevation_mm=float(dd_preview_route.get("horizontal_elevation_mm", horizontal_run.get("elevation_mm", 400.0))),
        horizontal_offset_length_mm=float(dd_preview_route.get("horizontal_offset_length_mm", horizontal_run.get("length_mm", 650.0))),
        equipment_short_connect_length_mm=float(dd_preview_route.get("equipment_short_connect_length_mm", equipment_run.get("length_mm", 1000.0))),
        upper_elbow_item_no=(
            int(dd_preview_route["upper_elbow_item_no"])
            if dd_preview_route.get("upper_elbow_item_no") is not None
            else None
        ),
        lower_elbow_item_no=(
            int(dd_preview_route["lower_elbow_item_no"])
            if dd_preview_route.get("lower_elbow_item_no") is not None
            else None
        ),
        sleeve_item_no=(
            int(dd_preview_route["sleeve_item_no"])
            if dd_preview_route.get("sleeve_item_no") is not None
            else None
        ),
        orifice_item_no=(
            int(dd_preview_route["orifice_item_no"])
            if dd_preview_route.get("orifice_item_no") is not None
            else None
        ),
        upper_elbow_rotation_deg=float(dd_preview_route.get("upper_elbow_rotation_deg", -90.0)),
        upper_elbow_rotation_axis=str(dd_preview_route.get("upper_elbow_rotation_axis", "y")),
        upper_elbow_secondary_rotation_deg=float(dd_preview_route.get("upper_elbow_secondary_rotation_deg", 180.0)),
        upper_elbow_secondary_rotation_axis=str(dd_preview_route.get("upper_elbow_secondary_rotation_axis", "z")),
        lower_elbow_rotation_deg=float(dd_preview_route.get("lower_elbow_rotation_deg", -90.0)),
        lower_elbow_rotation_axis=str(dd_preview_route.get("lower_elbow_rotation_axis", "z")),
        lower_elbow_secondary_rotation_deg=float(dd_preview_route.get("lower_elbow_secondary_rotation_deg", 0.0)),
        lower_elbow_secondary_rotation_axis=str(dd_preview_route.get("lower_elbow_secondary_rotation_axis", "x")),
        basis=basis,
        analysis_lines=analysis_lines,
    )


def build_dd_geometry(spec: DDSectionSpec) -> DDGeometry:
    """Build the D-D connection skeleton before any CAD entities are created."""

    trim = pipeline.resolve_lr_elbow_connection_trim_mm(None, spec.branch_dn)
    upper_elbow_corner = [spec.anchor_x_mm, spec.horizontal_center_y_mm - trim, spec.horizontal_elevation_mm]
    lower_elbow_corner = [
        spec.anchor_x_mm,
        upper_elbow_corner[1] + spec.horizontal_offset_length_mm,
        spec.horizontal_elevation_mm,
    ]
    vertical_start = [spec.anchor_x_mm, upper_elbow_corner[1], spec.top_elevation_mm]
    vertical_tangent = [spec.anchor_x_mm, upper_elbow_corner[1], spec.horizontal_elevation_mm + trim]
    horizontal_start = [spec.anchor_x_mm, upper_elbow_corner[1] + trim, spec.horizontal_elevation_mm]
    horizontal_end = [spec.anchor_x_mm, lower_elbow_corner[1] + trim, spec.horizontal_elevation_mm]
    equipment_short_start = [spec.anchor_x_mm - trim, lower_elbow_corner[1], spec.horizontal_elevation_mm]
    equipment_short_end = [
        equipment_short_start[0] + spec.equipment_short_connect_length_mm,
        equipment_short_start[1],
        equipment_short_start[2],
    ]
    sleeve_center = [spec.anchor_x_mm, spec.horizontal_center_y_mm, spec.horizontal_elevation_mm]
    all_points = [
        vertical_start,
        vertical_tangent,
        horizontal_start,
        horizontal_end,
        equipment_short_start,
        equipment_short_end,
        upper_elbow_corner,
        lower_elbow_corner,
        sleeve_center,
    ]
    return DDGeometry(
        elbow_connection_trim_mm=trim,
        upper_elbow_corner=upper_elbow_corner,
        lower_elbow_corner=lower_elbow_corner,
        vertical_start=vertical_start,
        vertical_tangent=vertical_tangent,
        horizontal_start=horizontal_start,
        horizontal_end=horizontal_end,
        equipment_short_start=equipment_short_start,
        equipment_short_end=equipment_short_end,
        sleeve_center=sleeve_center,
        all_points=all_points,
    )


def draw_dd_route(
    service: CADService,
    *,
    spec: DDSectionSpec,
    geometry: DDGeometry,
    route_records: list[dict[str, Any]],
) -> None:
    """Draw only the D-D pipe route and directly supported fittings."""

    bundle_id = spec.route_id
    branch_layer = f"PIPE_DN{spec.branch_dn}"

    pipeline.draw_pipe_segment(
        service,
        start=geometry.vertical_start,
        end=geometry.vertical_tangent,
        dn=spec.branch_dn,
    )
    pipeline.append_route_segment_record(
        route_records,
        bundle_id=bundle_id,
        segment_id=f"{bundle_id}_vertical_riser",
        category="section_preview",
        dn=spec.branch_dn,
        start=geometry.vertical_start,
        end=geometry.vertical_tangent,
        basis=spec.basis,
        path_id="dd_local_dn200_chain",
        source_run_id="dd_local_riser_dn200",
        from_node_id=None,
        to_node_id="dd_upper_elbow_21",
    )

    pipeline.draw_pipe_segment(
        service,
        start=geometry.horizontal_start,
        end=geometry.horizontal_end,
        dn=spec.branch_dn,
    )
    pipeline.append_route_segment_record(
        route_records,
        bundle_id=bundle_id,
        segment_id=f"{bundle_id}_horizontal_offset",
        category="section_preview",
        dn=spec.branch_dn,
        start=geometry.horizontal_start,
        end=geometry.horizontal_end,
        basis=spec.basis,
        path_id="dd_local_dn200_chain",
        source_run_id="dd_horizontal_turn_dn200",
        from_node_id="dd_upper_elbow_21",
        to_node_id="dd_lower_elbow_21",
    )

    pipeline.draw_pipe_segment(
        service,
        start=geometry.equipment_short_start,
        end=geometry.equipment_short_end,
        dn=spec.branch_dn,
    )
    pipeline.append_route_segment_record(
        route_records,
        bundle_id=bundle_id,
        segment_id=f"{bundle_id}_equipment_short_connect",
        category="section_preview",
        dn=spec.branch_dn,
        start=geometry.equipment_short_start,
        end=geometry.equipment_short_end,
        basis=spec.basis,
        path_id="dd_local_dn200_chain",
        source_run_id="dd_equipment_short_connect_dn200",
        from_node_id="dd_lower_elbow_21",
        to_node_id=None,
    )

    upper_elbow_handle = pipeline.draw_cycle_water_lr_elbow(
        service,
        description=f"{bundle_id} upper elbow",
        corner_point=geometry.upper_elbow_corner,
        dn=spec.branch_dn,
        layer=branch_layer,
        primary_rotation_deg=spec.upper_elbow_rotation_deg,
        primary_rotation_axis=spec.upper_elbow_rotation_axis,
        secondary_rotation_deg=spec.upper_elbow_secondary_rotation_deg,
        secondary_rotation_axis=spec.upper_elbow_secondary_rotation_axis,
    )
    pipeline.append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_upper_elbow",
        category="section_preview_fitting",
        kind="elbow",
        item_no=spec.upper_elbow_item_no,
        center=geometry.upper_elbow_corner,
        handle=upper_elbow_handle,
        basis=spec.basis,
    )

    lower_elbow_handle = pipeline.draw_cycle_water_lr_elbow(
        service,
        description=f"{bundle_id} lower elbow",
        corner_point=geometry.lower_elbow_corner,
        dn=spec.branch_dn,
        layer=branch_layer,
        primary_rotation_deg=spec.lower_elbow_rotation_deg,
        primary_rotation_axis=spec.lower_elbow_rotation_axis,
        secondary_rotation_deg=spec.lower_elbow_secondary_rotation_deg,
        secondary_rotation_axis=spec.lower_elbow_secondary_rotation_axis,
    )
    pipeline.append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_lower_elbow",
        category="section_preview_fitting",
        kind="elbow",
        item_no=spec.lower_elbow_item_no,
        center=geometry.lower_elbow_corner,
        handle=lower_elbow_handle,
        basis=spec.basis,
    )

    sleeve_entity = pipeline.call_controller_with_retry(
        f"{bundle_id} rigid sleeve",
        lambda: service.controller.draw_rigid_waterproof_sleeve(
            center=tuple(geometry.sleeve_center),
            dn=spec.branch_dn,
            axis="y",
            sleeve_type="A",
            layer=branch_layer,
            color=pipeline.WHITE,
        ),
    )
    if sleeve_entity is not None:
        pipeline.append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_rigid_sleeve",
            category="section_preview_fitting",
            kind="rigid_waterproof_sleeve",
            item_no=spec.sleeve_item_no,
            center=geometry.sleeve_center,
            handle=pipeline.extract_entity_handle(sleeve_entity),
            basis=spec.basis,
        )


def estimate_route_view(geometry: DDGeometry) -> tuple[tuple[float, float, float], float, float]:
    """Estimate a focused camera box from the rebuilt D-D route."""

    xs = [point[0] for point in geometry.all_points]
    ys = [point[1] for point in geometry.all_points]
    zs = [point[2] for point in geometry.all_points]
    center = (
        (min(xs) + max(xs)) / 2.0,
        (min(ys) + max(ys)) / 2.0,
        (min(zs) + max(zs)) / 2.0,
    )
    height = max(2600.0, (max(zs) - min(zs)) * 2.0)
    width = max(3600.0, (max(ys) - min(ys)) * 3.0)
    return center, height, width


def capture_previews(
    service: CADService,
    *,
    version_dir: Path,
    version: int,
    output_stem: str,
    model_center: tuple[float, float, float],
    height: float,
    width: float,
) -> list[str]:
    """Capture front, top, and iso previews for the rebuilt D-D route."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    view_specs = [
        ("front", model_center, height, width, f"{output_stem}_front_preview_v{version}.png"),
        ("top", model_center, height * 1.15, width * 1.15, f"{output_stem}_top_preview_v{version}.png"),
        ("swiso", model_center, height * 1.35, width * 1.35, f"{output_stem}_iso_preview_v{version}.png"),
    ]
    saved_paths: list[str] = []
    for view_name, target, view_height, view_width, file_name in view_specs:
        if not set_named_view_with_retry(service.controller, view_name, target=target, height=view_height, width=view_width):
            raise RuntimeError(f"视图切换失败: {view_name}")
        time.sleep(0.6)
        window = capturer.locate_cad_window(
            document_name=document_name,
            title_hint=title_hint,
            preferred_hwnd=preferred_hwnd,
            document_hwnd=document_hwnd,
        )
        try:
            win32gui.ShowWindow(window["hwnd"], 9)
            win32gui.SetForegroundWindow(window["hwnd"])
            time.sleep(0.3)
        except Exception:
            pass
        try:
            windll.user32.SetCursorPos(
                int(window["left"] + window["width"] * 0.4),
                int(window["top"] + window["height"] * 0.45),
            )
            time.sleep(0.2)
        except Exception:
            pass
        final_path = version_dir / file_name
        capturer._capture_region_via_powershell(
            left=window["left"],
            top=window["top"],
            width=window["width"],
            height=window["height"],
            output_path=str(final_path),
        )
        saved_paths.append(str(final_path))
    return saved_paths


def write_artifacts(
    *,
    version_dir: Path,
    version: int,
    output_stem: str,
    spec: DDSectionSpec,
    geometry: DDGeometry,
    route_records: list[dict[str, Any]],
    dwg_path: Path,
    preview_paths: list[str],
    archived_source_image: str | None,
) -> tuple[Path, Path]:
    """Write one parameters snapshot and one notes file for this rebuilt route."""

    parameters_path = version_dir / f"{output_stem}_parameters_v{version}.json"
    notes_path = version_dir / f"{output_stem}_notes_v{version}.txt"
    parameters_payload = {
        "topic": pipeline.OUTPUT_TOPIC,
        "section": spec.section,
        "view_role": spec.view_role,
        "view_direction": spec.view_direction,
        "spec": asdict(spec),
        "geometry": asdict(geometry),
        "route_records": route_records,
    }
    parameters_path.write_text(json.dumps(parameters_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    notes = [
        f"{pipeline.OUTPUT_TOPIC} D-D 剖面独立重绘 v{version}",
        "",
        f"DWG: {dwg_path}",
        f"剖面视图: {spec.section}",
        f"视图解释: {spec.view_role}",
        f"观察方向: {spec.view_direction}",
        f"执行范围: 仅绘制局部 DN200 管道与直接连接件，不绘制泵体和其他设备。",
        f"横管穿套管条件: {spec.anchor_reference} / y={spec.horizontal_center_y_mm:.1f}",
        "",
        "本轮直接读取:",
        f"- {pipeline.DIMENSION_INFO_PATH}",
        f"- {pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH}",
    ]
    if archived_source_image:
        notes.append(f"- 归档源图: {archived_source_image}")
    notes.extend(
        [
            "",
            "当前已绘制:",
            f"- DN{spec.branch_dn} 立管",
            f"- item {spec.upper_elbow_item_no} / {spec.lower_elbow_item_no} 弯头",
            f"- item {spec.sleeve_item_no} 刚性防水套管",
        ]
    )
    if spec.orifice_item_no is not None:
        notes.append(
            f"- item {spec.orifice_item_no} 制孔口仅保留为穿墙语义记录；当前 CAD 库无独立标准件原语，本轮未单独建实体。"
        )
    notes.extend(
        [
            "",
            "预览图:",
            *[f"- {path}" for path in preview_paths],
        ]
    )
    notes_path.write_text("\n".join(notes), encoding="utf-8")
    return parameters_path, notes_path


def main() -> int:
    """Run one rebuilt D-D-only CAD drawing flow."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="dd_only")
    parser.add_argument("--source-image", default="")
    args = parser.parse_args()

    dimension_info = pipeline.load_json(pipeline.DIMENSION_INFO_PATH)
    section_route_connection_info = pipeline.load_json(pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH)
    spec = build_dd_section_spec(
        dimension_info=dimension_info,
        section_route_connection_info=section_route_connection_info,
    )
    geometry = build_dd_geometry(spec)
    archived_source_image = copy_source_image_if_needed(str(args.source_image).strip() or None)

    version, version_dir = pipeline.prepare_version_dir(pipeline.OUTPUT_ROOT_DIR)
    output_stem = f"{pipeline.OUTPUT_TOPIC}_{str(args.label).strip() or 'dd_only'}"
    dwg_path = version_dir / f"{output_stem}_3D_v{version}.dwg"

    service = CADService()
    create_validation_document(service)
    route_records: list[dict[str, Any]] = []
    draw_dd_route(service, spec=spec, geometry=geometry, route_records=route_records)
    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    model_center, height, width = estimate_route_view(geometry)
    preview_paths = capture_previews(
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
        spec=spec,
        geometry=geometry,
        route_records=route_records,
        dwg_path=dwg_path,
        preview_paths=preview_paths,
        archived_source_image=archived_source_image,
    )
    print(json.dumps({"dwg_path": str(dwg_path), "preview_paths": preview_paths}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
