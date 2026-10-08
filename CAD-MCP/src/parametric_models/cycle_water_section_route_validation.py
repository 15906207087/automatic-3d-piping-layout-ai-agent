from __future__ import annotations

import argparse
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
    output_stem: str,
    model_center: tuple[float, float, float],
    height: float,
    width: float,
) -> list[str]:
    """Capture focused previews around one validated local section route."""

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


def load_layout() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Load the shared JSONs and rebuild the current dynamic layout."""

    list_info = pipeline.load_json(pipeline.LIST_INFO_PATH)
    dimension_info = pipeline.load_json(pipeline.DIMENSION_INFO_PATH)
    section_info = pipeline.load_json(pipeline.SECTION_INFO_PATH)
    section_route_connection_info = pipeline.load_json(pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH)
    basin_geometry = resolve_basin_geometry(dimension_info, section_info)
    _ = list_info
    layout = pipeline.build_dynamic_pipeline_layout(
        dimension_info,
        section_info,
        section_route_connection_info,
        basin_geometry,
    )
    return layout, dimension_info, section_info, section_route_connection_info, basin_geometry


def estimate_route_view(
    route: dict[str, Any],
    route_records: list[dict[str, Any]] | None = None,
) -> tuple[tuple[float, float, float], float, float]:
    """Estimate one reasonable local camera box from the drawn route geometry."""

    points: list[list[float]] = []
    for record in route_records or []:
        if str(record.get("record_type")) != "segment":
            continue
        start = record.get("start")
        end = record.get("end")
        if isinstance(start, list) and isinstance(end, list) and len(start) == 3 and len(end) == 3:
            points.append([float(value) for value in start])
            points.append([float(value) for value in end])
    for segment in route.get("segments", []):
        points.append([float(value) for value in segment["start"]])
        points.append([float(value) for value in segment["end"]])
    if not points:
        anchor_x = float(route.get("anchor_x", 0.0))
        base_y = float(route.get("base_y", 0.0))
        base_z = float(route.get("base_z", 0.0))
        return (anchor_x, base_y, base_z), 3200.0, 5200.0
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    zs = [point[2] for point in points]
    center = (
        (min(xs) + max(xs)) / 2.0,
        (min(ys) + max(ys)) / 2.0,
        (min(zs) + max(zs)) / 2.0,
    )
    span_y = max(1200.0, max(ys) - min(ys))
    span_z = max(1200.0, max(zs) - min(zs))
    height = max(3200.0, span_z * 2.2)
    width = max(5200.0, span_y * 2.4)
    return center, height, width


def write_validation_artifacts(
    *,
    version_dir: Path,
    version: int,
    output_stem: str,
    route: dict[str, Any],
    dwg_path: Path,
    preview_paths: list[str],
) -> Path:
    """Write one focused validation summary and parameters snapshot."""

    parameters_path = version_dir / f"{output_stem}_parameters_v{version}.json"
    notes_path = version_dir / f"{output_stem}_notes_v{version}.txt"
    parameters_path.write_text(json.dumps(route, ensure_ascii=False, indent=2), encoding="utf-8")
    notes = [
        f"{pipeline.OUTPUT_TOPIC} local route validation v{version}",
        "",
        f"route_id: {route['route_id']}",
        f"section: {route.get('section')}",
        f"DWG: {dwg_path}",
        "本轮脚本直接读取:",
        f"- {pipeline.LIST_INFO_PATH}",
        f"- {pipeline.DIMENSION_INFO_PATH}",
        f"- {pipeline.SECTION_INFO_PATH}",
        f"- {pipeline.SECTION_ROUTE_CONNECTION_INFO_PATH}",
        "",
        "预览图:",
        *[f"- {path}" for path in preview_paths],
    ]
    notes_path.write_text("\n".join(notes), encoding="utf-8")
    return parameters_path


def main() -> int:
    """Run one focused section route validation drawing."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--route-id", required=True)
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    layout, _, _, _, _ = load_layout()
    route = next(
        (item for item in layout.get("section_preview_routes", []) if str(item.get("route_id")) == str(args.route_id)),
        None,
    )
    if route is None:
        raise RuntimeError(f"未找到路由: {args.route_id}")

    version, version_dir = pipeline.prepare_version_dir(pipeline.OUTPUT_ROOT_DIR)
    label = str(args.label).strip() or str(route["route_id"])
    output_stem = f"{pipeline.OUTPUT_TOPIC}_{label}_validation"
    dwg_path = version_dir / f"{output_stem}_3D_v{version}.dwg"

    service = CADService()
    create_validation_document(service)
    route_records: list[dict[str, Any]] = []
    pipeline.draw_section_preview_route(service, route=route, route_records=route_records)
    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    model_center, height, width = estimate_route_view(route, route_records)
    preview_paths = capture_local_previews(
        service,
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        model_center=model_center,
        height=height,
        width=width,
    )
    parameters_path = write_validation_artifacts(
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        route=route,
        dwg_path=dwg_path,
        preview_paths=preview_paths,
    )
    print(
        json.dumps(
            {
                "success": True,
                "route_id": route["route_id"],
                "version": version,
                "version_dir": str(version_dir),
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
                "parameters_path": str(parameters_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
