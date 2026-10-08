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
    prepare_version_dir,
    save_drawing_with_retry,
    set_named_view_with_retry,
)
from parametric_models.cycle_water_pipeline_from_json import draw_pipe_segment

OUTPUT_TOPIC = "DN250三通断管示意"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
WHITE = 7


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
    """Capture front, top, and isometric previews for one local tee demo."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    view_specs = [
        ("front", model_center, height, width, f"{output_stem}_front_preview_v{version}.png"),
        ("top", model_center, height * 1.1, width * 1.1, f"{output_stem}_top_preview_v{version}.png"),
        ("swiso", model_center, height * 1.35, width * 1.35, f"{output_stem}_iso_preview_v{version}.png"),
    ]
    saved_paths: list[str] = []
    for view_name, target, view_height, view_width, file_name in view_specs:
        if not set_named_view_with_retry(
            service.controller,
            view_name,
            target=target,
            height=view_height,
            width=view_width,
        ):
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
                int(window["left"] + window["width"] * 0.45),
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


def build_demo_parameters() -> dict[str, Any]:
    """Return one simple tee-break layout that clearly shows main-run interruption."""

    tee_center = (0.0, 0.0, 0.0)
    run_dn = 250
    branch_dn = 100
    left_straight_length = 900.0
    right_straight_length = 1100.0
    branch_straight_length = 950.0
    return {
        "tee_center": tee_center,
        "run_dn": run_dn,
        "branch_dn": branch_dn,
        "run_axis": "x",
        "branch_axis": "z",
        "left_straight_length_mm": left_straight_length,
        "right_straight_length_mm": right_straight_length,
        "branch_straight_length_mm": branch_straight_length,
        "basis": (
            "主管 DN250 沿 x 轴布置，三通交点位于原点；"
            "主管在 DN250x100 三通处中断，分别从三通外轮廓两端重新起画；"
            "DN100 支管自三通外轮廓顶部刚好连接后沿 +z 方向引出。"
        ),
    }


def draw_demo(service: CADService, params: dict[str, Any]) -> dict[str, Any]:
    """Draw one broken-run tee demo and return the resolved geometry."""

    controller = service.controller
    tee_center = tuple(float(value) for value in params["tee_center"])
    run_dn = int(params["run_dn"])
    branch_dn = int(params["branch_dn"])
    run_axis = str(params["run_axis"])
    branch_axis = str(params["branch_axis"])
    left_straight_length = float(params["left_straight_length_mm"])
    right_straight_length = float(params["right_straight_length_mm"])
    branch_straight_length = float(params["branch_straight_length_mm"])

    resolved_center_to_end = controller._resolve_tee_center_to_end_gb12459(run_dn, branch_dn)
    if resolved_center_to_end is None:
        raise RuntimeError(f"无法解析 DN{run_dn}x{branch_dn} 三通中心至端面尺寸。")
    run_center_to_end, branch_center_to_end = (float(resolved_center_to_end[0]), float(resolved_center_to_end[1]))
    run_outer_diameter = float(controller.ELBOW_DN_OD_GB12459[run_dn])
    run_outer_radius = run_outer_diameter / 2.0
    tee_run_display_half_length = max(run_center_to_end, branch_center_to_end + run_outer_radius)
    tee_branch_display_offset = branch_center_to_end + run_outer_radius

    tee_entity = controller.draw_tee(
        center=tee_center,
        dn=run_dn,
        branch_dn=branch_dn,
        run_axis=run_axis,
        branch_axis=branch_axis,
        layer=f"PIPE_DN{run_dn}",
        color=WHITE,
    )
    if tee_entity is None:
        raise RuntimeError("DN250x100 三通绘制失败。")

    left_segment_start = [
        tee_center[0] - tee_run_display_half_length - left_straight_length,
        tee_center[1],
        tee_center[2],
    ]
    left_segment_end = [tee_center[0] - tee_run_display_half_length, tee_center[1], tee_center[2]]
    right_segment_start = [tee_center[0] + tee_run_display_half_length, tee_center[1], tee_center[2]]
    right_segment_end = [
        tee_center[0] + tee_run_display_half_length + right_straight_length,
        tee_center[1],
        tee_center[2],
    ]
    branch_segment_start = [tee_center[0], tee_center[1], tee_center[2] + tee_branch_display_offset]
    branch_segment_end = [
        tee_center[0],
        tee_center[1],
        tee_center[2] + tee_branch_display_offset + branch_straight_length,
    ]

    draw_pipe_segment(service, start=left_segment_start, end=left_segment_end, dn=run_dn)
    draw_pipe_segment(service, start=right_segment_start, end=right_segment_end, dn=run_dn)
    draw_pipe_segment(service, start=branch_segment_start, end=branch_segment_end, dn=branch_dn)

    return {
        **params,
        "run_center_to_end_mm": run_center_to_end,
        "branch_center_to_end_mm": branch_center_to_end,
        "run_outer_radius_mm": run_outer_radius,
        "tee_run_display_half_length_mm": tee_run_display_half_length,
        "tee_branch_display_offset_mm": tee_branch_display_offset,
        "segments": [
            {
                "segment_id": "main_left_dn250",
                "dn": run_dn,
                "start": left_segment_start,
                "end": left_segment_end,
            },
            {
                "segment_id": "main_right_dn250",
                "dn": run_dn,
                "start": right_segment_start,
                "end": right_segment_end,
            },
            {
                "segment_id": "branch_up_dn100",
                "dn": branch_dn,
                "start": branch_segment_start,
                "end": branch_segment_end,
            },
        ],
    }


def write_artifacts(
    *,
    version_dir: Path,
    version: int,
    output_stem: str,
    resolved_geometry: dict[str, Any],
    dwg_path: Path,
    preview_paths: list[str],
) -> tuple[Path, Path]:
    """Write one parameter snapshot and one concise note file."""

    parameters_path = version_dir / f"{output_stem}_parameters_v{version}.json"
    notes_path = version_dir / f"{output_stem}_notes_v{version}.txt"
    parameters_path.write_text(json.dumps(resolved_geometry, ensure_ascii=False, indent=2), encoding="utf-8")
    notes = [
        f"{OUTPUT_TOPIC} v{version}",
        "",
        f"DWG: {dwg_path}",
        "连接说明:",
        "- DN250 主管在 DN250x100 三通处断开。",
        "- 左右两段 DN250 主管分别从三通外轮廓两端刚好连接，不再与三通本体重合。",
        "- DN100 支管从三通外轮廓顶部刚好连接后沿 +z 方向引出，不再插入三通本体内部。",
        "",
        "预览图:",
        *[f"- {path}" for path in preview_paths],
    ]
    notes_path.write_text("\n".join(notes), encoding="utf-8")
    return parameters_path, notes_path


def main() -> int:
    """Draw one local tee-break demonstration and save versioned artifacts."""

    version, version_dir = prepare_version_dir(OUTPUT_ROOT_DIR)
    output_stem = OUTPUT_TOPIC
    dwg_path = version_dir / f"{output_stem}_3D_v{version}.dwg"

    service = CADService()
    create_fresh_document(service)
    resolved_geometry = draw_demo(service, build_demo_parameters())
    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    preview_paths = capture_previews(
        service,
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        model_center=(0.0, 0.0, 350.0),
        height=3200.0,
        width=5200.0,
    )
    parameters_path, notes_path = write_artifacts(
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        resolved_geometry=resolved_geometry,
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
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
