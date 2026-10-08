from __future__ import annotations

import json
import sys
import time
from ctypes import windll
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
    resolve_basin_geometry,
    save_drawing_with_retry,
    set_named_view_with_retry,
)
from parametric_models import cycle_water_pipeline_from_json as pipeline
from pipeline_connection_helper import (
    PipeConnectionRunSpec,
    RotationInstruction,
    build_orthogonal_elbow_connection,
)


OUTPUT_KIND = "A-A中部19验证"


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


def capture_local_previews(
    service: CADService,
    *,
    version_dir: Path,
    version: int,
    model_center: tuple[float, float, float],
) -> list[str]:
    """Capture focused previews around the A-A P-0204 elbow validation chain."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    view_specs = [
        ("front", model_center, 4500.0, 7000.0, f"{pipeline.OUTPUT_TOPIC}_{OUTPUT_KIND}_front_preview_v{version}.png"),
        ("top", model_center, 5200.0, 7200.0, f"{pipeline.OUTPUT_TOPIC}_{OUTPUT_KIND}_top_preview_v{version}.png"),
        ("swiso", model_center, 6200.0, 9000.0, f"{pipeline.OUTPUT_TOPIC}_{OUTPUT_KIND}_iso_preview_v{version}.png"),
    ]
    saved_paths: list[str] = []
    for view_name, target, height, width, file_name in view_specs:
        if not set_named_view_with_retry(service.controller, view_name, target=target, height=height, width=width):
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
                int(window["left"] + window["width"] * 0.35),
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


def write_validation_artifacts(
    *,
    version_dir: Path,
    version: int,
    dwg_path: Path,
    preview_paths: list[str],
    computed_parameters: dict[str, Any],
) -> None:
    """Write focused notes for the A-A P-0204 elbow validation run."""

    parameters_path = version_dir / f"{pipeline.OUTPUT_TOPIC}_{OUTPUT_KIND}_parameters_v{version}.json"
    notes_path = version_dir / f"{pipeline.OUTPUT_TOPIC}_{OUTPUT_KIND}_notes_v{version}.txt"
    parameters_path.write_text(json.dumps(computed_parameters, ensure_ascii=False, indent=2), encoding="utf-8")
    notes = [
        f"{pipeline.OUTPUT_TOPIC} {OUTPUT_KIND} v{version}",
        "",
        f"DWG: {dwg_path}",
        "本轮用途: 仅验证 A-A 中部 P-0204 泵侧 DN300 上翻转横链与 19 号弯头连接。",
        "本轮脚本直接读取:",
        f"- {pipeline.LIST_INFO_PATH}",
        f"- {pipeline.DIMENSION_INFO_PATH}",
        f"- {pipeline.SECTION_INFO_PATH}",
        f"- {pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH}",
        "",
        "关键几何参数:",
        f"- 立管中心 y: {computed_parameters['pump_vertical_y_mm']}",
        f"- 立管底部 z: {computed_parameters['pump_vertical_bottom_elevation_mm']}",
        f"- 弯头转角点 z: {computed_parameters['pump_vertical_top_elevation_mm']}",
        f"- DN300 弯头 bend_radius: {computed_parameters['pump_bend_radius_mm']}",
        f"- 阀门 3 中心 y: {computed_parameters['pump_valve_center_y_mm']}",
        f"- 伸缩节 11 中心 y: {computed_parameters['pump_joint_11_center_y_mm']}",
        f"- 异径管 25 大端 y: {computed_parameters['pump_reducer_large_end_y_mm']}",
        "",
        "预览图:",
        *[f"- {path}" for path in preview_paths],
    ]
    notes_path.write_text("\n".join(notes), encoding="utf-8")


def build_validation_route() -> dict[str, Any]:
    """Resolve the A-A main route from the shared JSON inputs."""

    list_info = pipeline.load_json(pipeline.LIST_INFO_PATH)
    dimension_info = pipeline.load_json(pipeline.DIMENSION_INFO_PATH)
    section_info = pipeline.load_json(pipeline.SECTION_INFO_PATH)
    section_route_connection_info = pipeline.load_json(pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH)
    basin_geometry = resolve_basin_geometry(dimension_info, section_info)
    _ = list_info
    layout = pipeline.build_dynamic_pipeline_layout(dimension_info, section_info, section_route_connection_info, basin_geometry)
    route = next(
        (
            item
            for item in layout.get("section_preview_routes", [])
            if str(item.get("route_kind", "")).strip().lower() == "aa_main_section_slice"
        ),
        None,
    )
    if route is None:
        raise RuntimeError("未找到 A-A 主剖面验证路由。")
    return route


def draw_aa_p0204_validation_route(
    service: CADService,
    *,
    route: dict[str, Any],
) -> dict[str, Any]:
    """Draw only the A-A middle P-0204 chain around elbow 19."""

    controller = service.controller
    upper_anchor_x = float(route.get("upper_anchor_x_mm", route["anchor_x"]))
    basis = str(route.get("basis", ""))
    pump_vertical_dn = int(route["pump_vertical_dn"])
    pump_vertical_y = float(route["pump_vertical_y"])
    pump_vertical_bottom_elevation = float(route["pump_vertical_bottom_elevation_mm"])
    pump_vertical_top_elevation = float(route["pump_vertical_top_elevation_mm"])
    pump_valve_center_y = float(route["pump_valve_center_y"])
    pump_valve_structure_length = float(route.get("pump_valve_structure_length_mm", 83.0))
    pump_valve_half_length = pump_valve_structure_length / 2.0 if pump_valve_structure_length > 0 else 0.0
    pump_joint_11_length = float(route.get("pump_joint_11_length_mm", 370.0))
    pump_joint_11_half_length = pump_joint_11_length / 2.0 if pump_joint_11_length > 0 else 0.0
    pump_reducer_large_dn = int(route["pump_reducer_large_dn"])
    pump_reducer_small_dn = int(route["pump_reducer_small_dn"])
    pump_reducer_large_end_y = float(route["pump_reducer_large_end_y"])
    pump_reducer_small_end_y = float(route["pump_reducer_small_end_y"])
    pump_reducer_length = float(route.get("pump_reducer_length_mm", pump_reducer_small_end_y - pump_reducer_large_end_y))
    pump_sleeve_30_center_y = float(
        pipeline.resolve_section_route_position_y(
            route.get("section_route_entry"),
            point_id="aa_p0204_item30_center",
            default=pump_vertical_y + pipeline.PIPE_OD[pump_vertical_dn] * 0.55,
        )
    )
    pump_joint_11_center_y = float(
        pipeline.resolve_section_route_position_y(
            route.get("section_route_entry"),
            point_id="aa_p0204_item11_center",
            default=max(pump_valve_center_y + pump_valve_half_length + 180.0, pump_reducer_large_end_y - 240.0),
        )
    )

    pump_bend_radius = pipeline.lr_elbow_bend_radius_mm(pump_vertical_dn)
    pump_vertical_start = [upper_anchor_x, pump_vertical_y, pump_vertical_bottom_elevation]
    pump_vertical_end = [upper_anchor_x, pump_vertical_y, pump_vertical_top_elevation - pump_bend_radius]
    pump_horizontal_a_start = [upper_anchor_x, pump_vertical_y + pump_bend_radius, pump_vertical_top_elevation]
    pump_horizontal_a_end = [upper_anchor_x, pump_valve_center_y - pump_valve_half_length, pump_vertical_top_elevation]
    pump_vertical_length = pump_vertical_end[2] - pump_vertical_start[2]
    pump_horizontal_a_length = pump_horizontal_a_end[1] - pump_horizontal_a_start[1]
    if pump_vertical_length <= 1e-6 or pump_horizontal_a_length <= 1e-6:
        raise RuntimeError("A-A 中部 19 号弯头验证链长度无效。")

    elbow_geometry = build_orthogonal_elbow_connection(
        controller,
        dn=pump_vertical_dn,
        elbow_center=(upper_anchor_x, pump_vertical_y, pump_vertical_top_elevation),
        elbow_type="LR",
        run_specs=[
            PipeConnectionRunSpec(
                run_id="aa_p0204_vertical_helper",
                tangent_point_mm=tuple(pump_vertical_end),
                axis_away_from_fitting="+z",
                length_mm=float(pump_vertical_length),
                connection_end="bottom_end",
                flow_role="pipe_1",
            ),
            PipeConnectionRunSpec(
                run_id="aa_p0204_horizontal_helper",
                tangent_point_mm=tuple(pump_horizontal_a_start),
                axis_away_from_fitting="-y",
                length_mm=float(pump_horizontal_a_length),
                connection_end="right_end",
                flow_role="pipe_2",
            ),
        ],
        elbow_rotations=[
            RotationInstruction(
                angle_deg=float(route.get("pump_top_elbow_rotation_deg", -90.0)),
                axis_direction=pipeline.resolve_rotation_axis(route.get("pump_top_elbow_rotation_axis", "y")),
                axis_name=str(route.get("pump_top_elbow_rotation_axis", "y")),
            ),
            RotationInstruction(
                angle_deg=float(route.get("pump_top_elbow_secondary_rotation_deg", 180.0)),
                axis_direction=pipeline.resolve_rotation_axis(route.get("pump_top_elbow_secondary_rotation_axis", "z")),
                axis_name=str(route.get("pump_top_elbow_secondary_rotation_axis", "z")),
            )
        ],
        layer=f"PIPE_DN{pump_vertical_dn}",
        color=pipeline.WHITE,
        draw_runs=False,
    )
    helper_runs = {item["run_id"]: item for item in elbow_geometry["runs"]}
    vertical_tangent = [float(value) for value in helper_runs["aa_p0204_vertical_helper"]["tangent_point_mm"]]
    horizontal_tangent = [float(value) for value in helper_runs["aa_p0204_horizontal_helper"]["tangent_point_mm"]]
    if vertical_tangent[2] - pump_vertical_start[2] > 1e-6:
        pipeline.draw_pipe_segment(service, start=pump_vertical_start, end=vertical_tangent, dn=pump_vertical_dn)
    if pump_horizontal_a_end[1] - horizontal_tangent[1] > 1e-6:
        pipeline.draw_pipe_segment(service, start=horizontal_tangent, end=pump_horizontal_a_end, dn=pump_vertical_dn)

    pump_horizontal_b_start = [upper_anchor_x, pump_valve_center_y + pump_valve_half_length, pump_vertical_top_elevation]
    pump_horizontal_b_end = [upper_anchor_x, pump_joint_11_center_y - pump_joint_11_half_length, pump_vertical_top_elevation]
    if pump_horizontal_b_end[1] - pump_horizontal_b_start[1] > 1e-6:
        pipeline.draw_pipe_segment(service, start=pump_horizontal_b_start, end=pump_horizontal_b_end, dn=pump_vertical_dn)

    pump_horizontal_c_start = [upper_anchor_x, pump_joint_11_center_y + pump_joint_11_half_length, pump_vertical_top_elevation]
    pump_horizontal_c_end = [upper_anchor_x, pump_reducer_large_end_y, pump_vertical_top_elevation]
    if pump_horizontal_c_end[1] - pump_horizontal_c_start[1] > 1e-6:
        pipeline.draw_pipe_segment(service, start=pump_horizontal_c_start, end=pump_horizontal_c_end, dn=pump_vertical_dn)

    sleeve_30 = pipeline.call_controller_with_retry(
        "A-A P0204 rigid sleeve 30 validation",
        lambda: controller.draw_rigid_waterproof_sleeve(
            center=(upper_anchor_x, pump_sleeve_30_center_y, pump_vertical_top_elevation),
            dn=pump_vertical_dn,
            axis="y",
            sleeve_type="A",
            layer=f"PIPE_DN{pump_vertical_dn}",
            color=pipeline.WHITE,
        ),
    )
    if sleeve_30 is None:
        raise RuntimeError("A-A 中部 30 号穿池壁构件绘制失败。")

    pump_requested_pn = float(route.get("pump_valve_pn", 10.0))
    pump_resolved_pn = pump_requested_pn if controller._get_flange_params(pump_vertical_dn, pump_requested_pn) else 16.0
    pump_valve = pipeline.call_controller_with_retry(
        "A-A P0204 valve 3 validation",
        lambda: controller.draw_butterfly_valve(
            center=(upper_anchor_x, pump_valve_center_y, pump_vertical_top_elevation),
            dn=pump_vertical_dn,
            pn=pump_resolved_pn,
            axis="y",
            layer=f"PIPE_DN{pump_vertical_dn}",
            color=pipeline.WHITE,
        ),
    )
    if pump_valve is None:
        raise RuntimeError("A-A 中部 3 号蝶阀绘制失败。")

    pump_joint_11 = pipeline.call_controller_with_retry(
        "A-A P0204 joint 11 validation",
        lambda: controller.draw_double_flange_limit_expansion_joint(
            center=(upper_anchor_x, pump_joint_11_center_y, pump_vertical_top_elevation),
            dn=pump_vertical_dn,
            model="JSSJA-2",
            pn=1.0,
            axis="y",
            layer=f"PIPE_DN{pump_vertical_dn}",
            color=pipeline.WHITE,
        ),
    )
    if pump_joint_11 is None:
        raise RuntimeError("A-A 中部 11 号伸缩接头绘制失败。")

    pump_reducer = pipeline.call_controller_with_retry(
        "A-A P0204 reducer 25 validation",
        lambda: controller.draw_reducer(
            center=(upper_anchor_x, pump_reducer_large_end_y, pump_vertical_top_elevation),
            dn_large=pump_reducer_large_dn,
            dn_small=pump_reducer_small_dn,
            axis="y",
            reducer_type="concentric",
            length=pump_reducer_length,
            layer=f"PIPE_DN{pump_reducer_large_dn}",
            color=pipeline.WHITE,
        ),
    )
    if pump_reducer is None:
        raise RuntimeError("A-A 中部 25 号异径管绘制失败。")

    return {
        "basis": basis,
        "pump_vertical_y_mm": pump_vertical_y,
        "pump_vertical_bottom_elevation_mm": pump_vertical_bottom_elevation,
        "pump_vertical_top_elevation_mm": pump_vertical_top_elevation,
        "pump_bend_radius_mm": pump_bend_radius,
        "pump_valve_center_y_mm": pump_valve_center_y,
        "pump_joint_11_center_y_mm": pump_joint_11_center_y,
        "pump_reducer_large_end_y_mm": pump_reducer_large_end_y,
        "pump_reducer_small_end_y_mm": pump_reducer_small_end_y,
        "pump_vertical_segment_start_mm": pump_vertical_start,
        "pump_vertical_segment_end_mm": vertical_tangent,
        "pump_horizontal_segment_start_mm": horizontal_tangent,
        "pump_horizontal_segment_end_mm": pump_horizontal_a_end,
        "pump_elbow_corner_point_mm": [upper_anchor_x, pump_vertical_y, pump_vertical_top_elevation],
        "pump_elbow_ports": elbow_geometry["elbow_ports"],
        "pump_elbow_rotation": elbow_geometry["elbow_rotations"],
    }


def main() -> int:
    """Run the focused A-A P-0204 elbow validation drawing."""

    route = build_validation_route()
    version, version_dir = pipeline.prepare_version_dir(pipeline.OUTPUT_ROOT_DIR)
    dwg_path = version_dir / f"{pipeline.OUTPUT_TOPIC}_{OUTPUT_KIND}_3D_v{version}.dwg"

    service = CADService()
    create_validation_document(service)
    computed_parameters = draw_aa_p0204_validation_route(service, route=route)
    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    model_center = (
        float(route.get("upper_anchor_x_mm", route["anchor_x"])),
        float(route["pump_vertical_y"]) + 1200.0,
        float(route["pump_vertical_top_elevation_mm"]) - 150.0,
    )
    preview_paths = capture_local_previews(
        service,
        version_dir=version_dir,
        version=version,
        model_center=model_center,
    )
    write_validation_artifacts(
        version_dir=version_dir,
        version=version,
        dwg_path=dwg_path,
        preview_paths=preview_paths,
        computed_parameters=computed_parameters,
    )
    print(
        json.dumps(
            {
                "success": True,
                "version": version,
                "version_dir": str(version_dir),
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
                "parameters_path": str(version_dir / f"{pipeline.OUTPUT_TOPIC}_{OUTPUT_KIND}_parameters_v{version}.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
