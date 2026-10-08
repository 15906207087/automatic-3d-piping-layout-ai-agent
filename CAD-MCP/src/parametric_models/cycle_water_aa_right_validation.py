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
    create_fresh_document,
    resolve_basin_geometry,
    save_drawing_with_retry,
    set_named_view_with_retry,
)
from parametric_models import cycle_water_pipeline_from_json as pipeline


OUTPUT_KIND = "A-A右侧验证"


def capture_local_previews(
    service: CADService,
    *,
    version_dir: Path,
    version: int,
    model_center: tuple[float, float, float],
) -> list[str]:
    """Capture focused previews around the A-A right-side validation chain."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    view_specs = [
        ("front", model_center, 5000.0, 7000.0, f"{pipeline.OUTPUT_TOPIC}_{OUTPUT_KIND}_front_preview_v{version}.png"),
        ("top", model_center, 6000.0, 7000.0, f"{pipeline.OUTPUT_TOPIC}_{OUTPUT_KIND}_top_preview_v{version}.png"),
        ("swiso", model_center, 7000.0, 9000.0, f"{pipeline.OUTPUT_TOPIC}_{OUTPUT_KIND}_iso_preview_v{version}.png"),
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
    source_pdf: Path | None,
    computed_parameters: dict[str, Any],
) -> None:
    """Write focused notes for the A-A right-side validation run."""

    parameters_path = version_dir / f"{pipeline.OUTPUT_TOPIC}_{OUTPUT_KIND}_parameters_v{version}.json"
    notes_path = version_dir / f"{pipeline.OUTPUT_TOPIC}_{OUTPUT_KIND}_notes_v{version}.txt"
    parameters_path.write_text(json.dumps(computed_parameters, ensure_ascii=False, indent=2), encoding="utf-8")
    notes = [
        f"{pipeline.OUTPUT_TOPIC} {OUTPUT_KIND} v{version}",
        "",
        f"DWG: {dwg_path}",
        "本轮用途: 仅验证 A-A 右侧回接链的局部几何，不绘制整套循环水模型。",
        "本轮脚本直接读取:",
        f"- {pipeline.LIST_INFO_PATH}",
        f"- {pipeline.DIMENSION_INFO_PATH}",
        f"- {pipeline.SECTION_INFO_PATH}",
        f"- {pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH}",
        "",
        "关键几何参数:",
        f"- 右侧 DN450 主管中心 z: {computed_parameters['right_header_center_z_mm']}",
        f"- 右侧 DN250 立管顶部 z: {computed_parameters['right_riser_top_elevation_mm']}",
        f"- 右侧回接连接高程 z: {computed_parameters['right_vertical_connection_elevation_mm']}",
        f"- 右侧 DN250 可见立管下端 z: {computed_parameters['right_vertical_visible_bottom_elevation_mm']}",
        f"- 右侧三通支口 M: {computed_parameters['right_tee_branch_center_to_end_mm']}",
        f"- 右侧三通中心 z: {computed_parameters['right_tee_center_z_mm']}",
        f"- 右侧三通支口端点 z: {computed_parameters['right_tee_branch_port_z_mm']}",
        f"- 右侧主管/立管中心 y: {computed_parameters['right_header_center_y_mm']}",
        "",
        "预览图:",
        *[f"- {path}" for path in preview_paths],
    ]
    if source_pdf is not None:
        notes.insert(2, f"源 PDF: {source_pdf}")
    notes_path.write_text("\n".join(notes), encoding="utf-8")


def draw_aa_right_validation_route(
    service: CADService,
    *,
    route: dict[str, Any],
) -> dict[str, Any]:
    """Draw only the A-A right return chain for local validation."""

    controller = service.controller
    section_route_entry = route.get("section_route_entry")
    basis = str(route.get("basis", ""))
    anchor_x = float(route["anchor_x"])
    right_header_y = float(route["right_header_y"])
    right_header_z = float(route["right_header_z"])
    right_header_actual_dn = int(route["right_header_actual_dn"])
    right_header_proxy_dn = int(route["right_header_proxy_dn"])
    right_branch_dn = int(route["right_branch_dn"])
    right_horizontal_start_y = float(route["right_horizontal_start_y"])
    right_valve_center_y = float(route["right_valve_center_y"])
    right_valve_structure_length = float(route.get("right_valve_structure_length_mm", 76.0))
    right_valve_half_length = right_valve_structure_length / 2.0 if right_valve_structure_length > 0 else 0.0
    right_riser_top_elevation = float(route["right_riser_top_elevation_mm"])
    right_vertical_connection_elevation = float(route["right_vertical_bottom_elevation_mm"])
    right_elbow_item_no = route.get("right_elbow_item_no")
    right_header_item_no = route.get("right_header_item_no")

    right_joint_12_item_no = pipeline.resolve_section_route_node_value(
        section_route_entry,
        "aa_right_joint_12",
        "item_no",
        12,
    )
    right_valve_1_item_no = pipeline.resolve_section_route_node_value(
        section_route_entry,
        "aa_right_valve_1",
        "item_no",
        1,
    )
    right_gauge_10_item_no = pipeline.resolve_section_route_node_value(
        section_route_entry,
        "aa_right_gauge_10",
        "item_no",
        10,
    )
    right_item26_item_no = pipeline.resolve_section_route_node_value(
        section_route_entry,
        "aa_right_reducer_26",
        "item_no",
        26,
    )
    right_item26_large_dn = int(
        pipeline.resolve_section_route_node_value(
            section_route_entry,
            "aa_right_reducer_26",
            "dn_large",
            right_branch_dn,
        )
    )
    right_item26_small_dn = int(
        pipeline.resolve_section_route_node_value(
            section_route_entry,
            "aa_right_reducer_26",
            "dn_small",
            150,
        )
    )
    right_item26_length = float(
        route.get(
            "right_item26_length_mm",
            max(50.0, (pipeline.PIPE_OD[right_item26_large_dn] - pipeline.PIPE_OD[right_item26_small_dn]) * 1.2),
        )
    )
    right_item26_small_end_y = right_horizontal_start_y
    right_item26_large_end_y = right_item26_small_end_y + right_item26_length

    right_joint_12_spec = controller.DOUBLE_FLANGE_LIMIT_EXPANSION_JOINT_GBT12465.get(
        ("VSSJA-2(B2F)", right_branch_dn, 1.0)
    )
    right_joint_12_length = float(
        route.get(
            "right_joint_12_length_mm",
            right_joint_12_spec["overall_length"] if right_joint_12_spec is not None else 395.0,
        )
    )
    right_joint_12_half_length = right_joint_12_length / 2.0 if right_joint_12_length > 0 else 0.0
    right_joint_12_center_y = right_item26_large_end_y + right_joint_12_half_length

    right_valve_1_structure_length = float(
        route.get(
            "right_valve_1_structure_length_mm",
            controller.VALVE_CHECK_STRUCTURE_LENGTH.get(right_branch_dn, max(220.0, right_branch_dn * 1.9 + 250.0)),
        )
    )
    right_valve_1_half_length = right_valve_1_structure_length / 2.0 if right_valve_1_structure_length > 0 else 0.0
    right_valve_1_center_y = right_item26_large_end_y + right_joint_12_length + right_valve_1_half_length
    right_valve_center_y = right_valve_1_center_y + right_valve_1_half_length + right_valve_half_length
    right_elbow_connection_trim = pipeline.resolve_lr_elbow_connection_trim_mm(
        route.get("right_elbow_connection_trim_mm"),
        right_branch_dn,
    )
    right_post_valve_horizontal_start_y = right_valve_center_y + right_valve_half_length
    right_post_valve_horizontal_end_y = right_header_y - right_elbow_connection_trim
    right_horizontal_gauge_mount_height = float(
        route.get("right_horizontal_gauge_mount_height_mm", max(220.0, pipeline.PIPE_OD[right_branch_dn] * 0.95))
    )
    right_gauge_10_hint_y = pipeline.resolve_section_route_position_y(
        section_route_entry,
        point_id="aa_right_gauge_10_center",
        default=right_post_valve_horizontal_start_y + max(120.0, right_elbow_connection_trim * 0.35),
    )
    if right_post_valve_horizontal_end_y - right_post_valve_horizontal_start_y > 240.0:
        right_gauge_10_center_y = min(
            max(right_gauge_10_hint_y, right_post_valve_horizontal_start_y + 120.0),
            right_post_valve_horizontal_end_y - 120.0,
        )
    else:
        right_gauge_10_center_y = (right_post_valve_horizontal_start_y + right_post_valve_horizontal_end_y) / 2.0

    right_tee_dims = controller._resolve_tee_center_to_end_gb12459(right_header_proxy_dn, right_branch_dn)
    if right_tee_dims is None:
        raise RuntimeError("A-A 右侧验证三通尺寸解析失败。")
    right_tee_branch_center_to_end = float(right_tee_dims[1])
    right_tee_center_z = right_header_z
    right_tee_branch_port_z = right_tee_center_z + right_tee_branch_center_to_end
    right_visible_vertical_bottom_elevation = right_tee_branch_port_z

    header_slice_half_length = 2600.0
    header_slice_start = [anchor_x - header_slice_half_length, right_header_y, right_header_z]
    header_slice_end = [anchor_x + header_slice_half_length, right_header_y, right_header_z]
    pipeline.draw_pipe_segment(service, start=header_slice_start, end=header_slice_end, dn=right_header_actual_dn)

    dn250_horizontal_start = [anchor_x, right_post_valve_horizontal_start_y, right_riser_top_elevation]
    dn250_horizontal_end = [anchor_x, right_post_valve_horizontal_end_y, right_riser_top_elevation]
    if dn250_horizontal_end[1] - dn250_horizontal_start[1] > 1e-6:
        pipeline.draw_pipe_segment(service, start=dn250_horizontal_start, end=dn250_horizontal_end, dn=right_branch_dn)

    right_vertical_start = [anchor_x, right_header_y, right_visible_vertical_bottom_elevation]
    right_vertical_end = [anchor_x, right_header_y, right_riser_top_elevation - right_elbow_connection_trim]
    if right_vertical_end[2] - right_vertical_start[2] > 1e-6:
        pipeline.draw_pipe_segment(service, start=right_vertical_start, end=right_vertical_end, dn=right_branch_dn)

    right_item26_entity = pipeline.call_controller_with_retry(
        "A-A right reducer 26 validation",
        lambda: controller.draw_reducer(
            center=(anchor_x, right_item26_large_end_y, right_riser_top_elevation),
            dn_large=right_item26_large_dn,
            dn_small=right_item26_small_dn,
            axis="-y",
            reducer_type="concentric",
            length=right_item26_length,
            layer=f"PIPE_DN{right_branch_dn}",
            color=pipeline.WHITE,
        ),
    )
    if right_item26_entity is None:
        raise RuntimeError("A-A 右侧验证 26 号异径管绘制失败。")

    right_joint_12_entity = pipeline.call_controller_with_retry(
        "A-A right joint 12 validation",
        lambda: controller.draw_double_flange_limit_expansion_joint(
            center=(anchor_x, right_joint_12_center_y, right_riser_top_elevation),
            dn=right_branch_dn,
            model="JSSJA-2",
            pn=1.0,
            axis="y",
            layer=f"PIPE_DN{right_branch_dn}",
            color=pipeline.WHITE,
        ),
    )
    if right_joint_12_entity is None:
        raise RuntimeError("A-A 右侧验证 12 号伸缩接头绘制失败。")

    right_valve_1_requested_pn = float(route.get("right_valve_1_pn", route.get("right_valve_pn", 10.0)))
    right_valve_1_resolved_pn = (
        right_valve_1_requested_pn
        if controller._get_flange_params(right_branch_dn, right_valve_1_requested_pn)
        else 16.0
    )
    right_valve_1_entity = pipeline.call_controller_with_retry(
        "A-A right valve 1 validation",
        lambda: controller.draw_check_valve(
            center=(anchor_x, right_valve_1_center_y, right_riser_top_elevation),
            dn=right_branch_dn,
            pn=right_valve_1_resolved_pn,
            axis="y",
            layer=f"PIPE_DN{right_branch_dn}",
            color=pipeline.WHITE,
        ),
    )
    if right_valve_1_entity is None:
        raise RuntimeError("A-A 右侧验证 1 号止回阀绘制失败。")

    right_requested_pn = float(route.get("right_valve_pn", 10.0))
    right_resolved_pn = right_requested_pn if controller._get_flange_params(right_branch_dn, right_requested_pn) else 16.0
    right_valve_entity = pipeline.call_controller_with_retry(
        "A-A right valve 8 validation",
        lambda: controller.draw_butterfly_valve(
            center=(anchor_x, right_valve_center_y, right_riser_top_elevation),
            dn=right_branch_dn,
            pn=right_resolved_pn,
            axis="y",
            layer=f"PIPE_DN{right_branch_dn}",
            color=pipeline.WHITE,
        ),
    )
    if right_valve_entity is None:
        raise RuntimeError("A-A 右侧验证 8 号蝶阀绘制失败。")

    right_gauge_entity = pipeline.call_controller_with_retry(
        "A-A right gauge 10 validation",
        lambda: controller.draw_pressure_gauge(
            center=(anchor_x, right_gauge_10_center_y, right_riser_top_elevation + right_horizontal_gauge_mount_height),
            nominal_diameter=100,
            style="radial",
            mount_type="direct",
            face_axis="x",
            layer="INSTRUMENT",
            color=pipeline.WHITE,
        ),
    )
    if right_gauge_entity is None:
        raise RuntimeError("A-A 右侧验证 10 号压力表绘制失败。")

    right_elbow_center = [anchor_x, right_header_y, right_riser_top_elevation]
    right_elbow_handle = pipeline.draw_cycle_water_lr_elbow(
        service,
        description="A-A right elbow 20 validation",
        corner_point=right_elbow_center,
        dn=right_branch_dn,
        layer=f"PIPE_DN{right_branch_dn}",
        primary_rotation_deg=float(route.get("right_elbow_rotation_deg", -90.0)),
        primary_rotation_axis=route.get("right_elbow_rotation_axis", "y"),
    )

    right_tee_entity = pipeline.call_controller_with_retry(
        "A-A right tee 14 validation",
        lambda: controller.draw_tee(
            center=(anchor_x, right_header_y, right_tee_center_z),
            dn=right_header_proxy_dn,
            branch_dn=right_branch_dn,
            run_axis="x",
            branch_axis="z",
            layer=f"PIPE_DN{right_branch_dn}",
            color=pipeline.WHITE,
        ),
    )
    if right_tee_entity is None:
        raise RuntimeError("A-A 右侧验证 14 号异径三通绘制失败。")

    return {
        "basis": basis,
        "anchor_x_mm": anchor_x,
        "right_header_center_y_mm": right_header_y,
        "right_header_center_z_mm": right_header_z,
        "right_header_actual_dn": right_header_actual_dn,
        "right_header_proxy_dn": right_header_proxy_dn,
        "right_branch_dn": right_branch_dn,
        "right_horizontal_start_y_mm": right_horizontal_start_y,
        "right_riser_top_elevation_mm": right_riser_top_elevation,
        "right_vertical_connection_elevation_mm": right_vertical_connection_elevation,
        "right_vertical_visible_bottom_elevation_mm": right_visible_vertical_bottom_elevation,
        "right_vertical_start_point_mm": right_vertical_start,
        "right_vertical_end_point_mm": right_vertical_end,
        "right_tee_branch_center_to_end_mm": right_tee_branch_center_to_end,
        "right_tee_center_z_mm": right_tee_center_z,
        "right_tee_branch_port_z_mm": right_tee_branch_port_z,
        "right_post_valve_horizontal_start_y_mm": right_post_valve_horizontal_start_y,
        "right_post_valve_horizontal_end_y_mm": right_post_valve_horizontal_end_y,
        "right_elbow_connection_trim_mm": right_elbow_connection_trim,
        "right_item26": {
            "item_no": right_item26_item_no,
            "dn_large": right_item26_large_dn,
            "dn_small": right_item26_small_dn,
            "length_mm": right_item26_length,
            "large_end_y_mm": right_item26_large_end_y,
            "small_end_y_mm": right_item26_small_end_y,
        },
        "right_joint_12": {
            "item_no": right_joint_12_item_no,
            "length_mm": right_joint_12_length,
            "center_y_mm": right_joint_12_center_y,
        },
        "right_valve_1": {
            "item_no": right_valve_1_item_no,
            "structure_length_mm": right_valve_1_structure_length,
            "center_y_mm": right_valve_1_center_y,
        },
        "right_valve_8": {
            "item_no": route.get("right_valve_item_no"),
            "structure_length_mm": right_valve_structure_length,
            "center_y_mm": right_valve_center_y,
        },
        "right_elbow_20": {
            "item_no": right_elbow_item_no,
            "center_mm": right_elbow_center,
        },
        "right_tee_14": {
            "item_no": right_header_item_no,
            "center_mm": [anchor_x, right_header_y, right_tee_center_z],
        },
        "right_gauge_10": {
            "item_no": right_gauge_10_item_no,
            "center_y_mm": right_gauge_10_center_y,
            "mount_height_mm": right_horizontal_gauge_mount_height,
        },
    }


def main() -> int:
    """Build a focused A-A right-side validation drawing and archive outputs."""

    for path in (
        pipeline.LIST_INFO_PATH,
        pipeline.DIMENSION_INFO_PATH,
        pipeline.SECTION_INFO_PATH,
        pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH,
    ):
        if not path.exists():
            raise FileNotFoundError(f"缺少输入文件: {path}")

    list_info = pipeline.load_json(pipeline.LIST_INFO_PATH)
    dimension_info = pipeline.load_json(pipeline.DIMENSION_INFO_PATH)
    section_info = pipeline.load_json(pipeline.SECTION_INFO_PATH)
    section_route_connection_info = pipeline.load_json(pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH)
    source_pdf_raw = list_info.get("source_pdf") or dimension_info.get("source_pdf") or section_info.get("source_pdf")
    source_pdf = Path(source_pdf_raw) if source_pdf_raw else None

    basin_geometry = resolve_basin_geometry(dimension_info, section_info)
    layout = pipeline.build_dynamic_pipeline_layout(
        dimension_info,
        section_info,
        section_route_connection_info,
        basin_geometry,
    )
    aa_route = next(
        (
            route
            for route in layout.get("section_preview_routes", [])
            if str(route.get("route_kind", "")).strip().lower() == "aa_main_section_slice"
        ),
        None,
    )
    if aa_route is None:
        raise RuntimeError("未找到 A-A 主剖面路由。")

    version, version_dir = pipeline.prepare_version_dir(
        pipeline.OUTPUT_ROOT_DIR,
        minimum_version=pipeline.PIPELINE_VERSION_FLOOR,
    )
    dwg_path = version_dir / f"{pipeline.OUTPUT_TOPIC}_{OUTPUT_KIND}_3D_v{version}.dwg"

    service = CADService()
    create_fresh_document(service)
    computed_parameters = draw_aa_right_validation_route(service, route=aa_route)

    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    model_center = (
        float(computed_parameters["anchor_x_mm"]),
        float(computed_parameters["right_header_center_y_mm"]),
        (
            float(computed_parameters["right_riser_top_elevation_mm"])
            + float(computed_parameters["right_vertical_visible_bottom_elevation_mm"])
        )
        / 2.0,
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
        source_pdf=source_pdf,
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
