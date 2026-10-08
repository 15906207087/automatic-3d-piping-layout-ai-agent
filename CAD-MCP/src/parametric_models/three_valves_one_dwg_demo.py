from __future__ import annotations

import json
import sys
import time
from ctypes import windll
from pathlib import Path
from typing import Any, Callable

import win32con
import win32gui

CURRENT_FILE = Path(__file__).resolve()
SRC_DIR = CURRENT_FILE.parents[1]
PROJECT_DIR = CURRENT_FILE.parents[2]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from screen_capture import ScreenCapture
from parametric_models.cycle_water_equipment_from_json import (
    CADService,
    prepare_version_dir,
    save_drawing_with_retry,
    set_named_view_with_retry,
    zoom_extents_with_retry,
)

OUTPUT_TOPIC = "three_valves_one_dwg_demo"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
WHITE = 7
PART_LAYER = "THREE_VALVES_DEMO"
VALVE_SPACING_X_MM = 2200.0


def ensure_drawable_document(service: CADService) -> None:
    """Attach to a usable CAD document, creating a fresh one when supported."""

    if not service.start_cad():
        raise RuntimeError("Failed to start or attach to CAD.")

    docs = getattr(service.controller.app, "Documents", None)
    if docs is not None:
        try:
            docs.Add()
            time.sleep(3.0)
        except Exception:
            pass

    active_doc = getattr(service.controller.app, "ActiveDocument", None)
    if active_doc is None:
        raise RuntimeError("CAD did not expose an active document.")
    service.controller.doc = active_doc
    last_error: Exception | None = None
    for _ in range(8):
        try:
            if service.controller._get_model_space() is not None:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.8)
    raise RuntimeError(f"CAD document is not drawable yet: {last_error}")


def draw_with_retry(
    callback: Callable[[], Any],
    *,
    description: str,
    retries: int = 4,
    wait_seconds: float = 0.8,
) -> Any:
    """Retry a CAD creation call because COM can fail transiently."""

    last_result = None
    for _ in range(retries):
        try:
            last_result = callback()
        except Exception:
            last_result = None
        if last_result is not None:
            return last_result
        time.sleep(wait_seconds)
    raise RuntimeError(f"Failed to draw {description}.")


def build_layout() -> list[dict[str, Any]]:
    """Return the three-valve layout placed in one shared drawing."""

    return [
        {
            "name": "butterfly valve",
            "center": (-VALVE_SPACING_X_MM, 0.0, 0.0),
            "builder": lambda controller, center: controller.draw_butterfly_valve(
                center=center,
                dn=150,
                pn=16,
                axis="y",
                layer=PART_LAYER,
                color=WHITE,
            ),
            "parameters": {
                "display_name": "蝶阀",
                "standard": "GB/T 12238",
                "dn": 150,
                "pn": 16,
                "axis": "y",
            },
        },
        {
            "name": "ball valve",
            "center": (0.0, 0.0, 0.0),
            "builder": lambda controller, center: controller.draw_ball_valve(
                center=center,
                dn=150,
                pn=16,
                axis="x",
                connection_type="flanged",
                layer=PART_LAYER,
                color=WHITE,
            ),
            "parameters": {
                "display_name": "球阀",
                "standard": "GB/T 12237",
                "dn": 150,
                "pn": 16,
                "axis": "x",
                "connection_type": "flanged",
            },
        },
        {
            "name": "globe valve",
            "center": (VALVE_SPACING_X_MM, 0.0, 0.0),
            "builder": lambda controller, center: controller.draw_globe_valve(
                center=center,
                dn=150,
                pn=16,
                axis="x",
                layer=PART_LAYER,
                color=WHITE,
            ),
            "parameters": {
                "display_name": "截止阀",
                "standard": "GB/T 12235",
                "dn": 150,
                "pn": 16,
                "axis": "x",
            },
        },
    ]


def draw_valves(service: CADService) -> list[dict[str, Any]]:
    """Draw each valve and return the resolved placement table."""

    controller = service.controller
    resolved_valves: list[dict[str, Any]] = []
    for item in build_layout():
        center = tuple(float(value) for value in item["center"])
        draw_with_retry(
            lambda item=item, center=center: item["builder"](controller, center),
            description=item["name"],
        )
        resolved_valves.append(
            {
                "name": item["name"],
                "center": list(center),
                "parameters": item["parameters"],
            }
        )
    return resolved_valves


def measure_model_bounds(service: CADService) -> dict[str, tuple[float, float, float]]:
    """Measure the current model extents from all entities in model space."""

    model_space = service.controller._get_model_space()
    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")
    found = False
    for index in range(model_space.Count):
        entity = model_space.Item(index)
        try:
            bbox_min, bbox_max = entity.GetBoundingBox()
        except Exception:
            continue
        found = True
        min_x = min(min_x, float(bbox_min[0]))
        min_y = min(min_y, float(bbox_min[1]))
        min_z = min(min_z, float(bbox_min[2]))
        max_x = max(max_x, float(bbox_max[0]))
        max_y = max(max_y, float(bbox_max[1]))
        max_z = max(max_z, float(bbox_max[2]))
    if not found:
        raise RuntimeError("No drawable entities found in model space.")
    return {
        "min": (min_x, min_y, min_z),
        "max": (max_x, max_y, max_z),
        "center": ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0, (min_z + max_z) / 2.0),
    }


def capture_previews(
    service: CADService,
    *,
    version_dir: Path,
    version: int,
    output_stem: str,
    model_bounds: dict[str, tuple[float, float, float]],
) -> list[str]:
    """Capture stable top and isometric previews for the valve demo."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    min_pt = model_bounds["min"]
    max_pt = model_bounds["max"]
    model_center = model_bounds["center"]
    x_span = max_pt[0] - min_pt[0]
    y_span = max_pt[1] - min_pt[1]
    z_span = max_pt[2] - min_pt[2]
    view_specs = [
        (
            "top",
            model_center,
            max(y_span * 12.0, 2200.0),
            max(x_span * 1.7, 8200.0),
            f"{output_stem}_top_preview_v{version}.png",
        ),
        (
            "swiso",
            model_center,
            max(max(x_span, z_span) * 2.2, 8200.0),
            max(max(x_span, y_span, z_span) * 2.2, 8200.0),
            f"{output_stem}_iso_preview_v{version}.png",
        ),
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
            raise RuntimeError(f"Failed to switch view: {view_name}")
        time.sleep(0.6)
        service.controller.refresh_view()
        time.sleep(0.6)
        window = capturer.locate_cad_window(
            document_name=document_name,
            title_hint=title_hint,
            preferred_hwnd=preferred_hwnd,
            document_hwnd=document_hwnd,
        )
        try:
            win32gui.ShowWindow(window["hwnd"], 9)
            win32gui.BringWindowToTop(window["hwnd"])
            win32gui.SetWindowPos(
                window["hwnd"],
                win32con.HWND_TOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
            )
            win32gui.SetForegroundWindow(window["hwnd"])
            win32gui.SetWindowPos(
                window["hwnd"],
                win32con.HWND_NOTOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
            )
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


def write_artifacts(
    *,
    version_dir: Path,
    version: int,
    output_stem: str,
    valves: list[dict[str, Any]],
    dwg_path: Path,
    preview_paths: list[str],
) -> tuple[Path, Path]:
    """Write one parameter snapshot and one human-readable note."""

    parameters_path = version_dir / f"{output_stem}_parameters_v{version}.json"
    notes_path = version_dir / f"{output_stem}_notes_v{version}.txt"
    payload = {
        "topic": OUTPUT_TOPIC,
        "version": version,
        "layout": {"count": 3, "spacing_x_mm": VALVE_SPACING_X_MM, "layer": PART_LAYER, "color": WHITE},
        "valves": valves,
    }
    parameters_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    notes = [
        f"{OUTPUT_TOPIC} v{version}",
        "",
        f"DWG: {dwg_path}",
        "Layout:",
        "- One drawing file contains three valves placed from left to right.",
        "- Left: butterfly valve | Center: ball valve | Right: globe valve.",
        "- All valves use DN150 / PN16 and are drawn in white on one shared layer.",
        "",
        "Previews:",
        *[f"- {path}" for path in preview_paths],
    ]
    notes_path.write_text("\n".join(notes), encoding="utf-8")
    return parameters_path, notes_path


def main() -> int:
    """Create one versioned DWG that contains butterfly, ball, and globe valves."""

    version, version_dir = prepare_version_dir(OUTPUT_ROOT_DIR)
    output_stem = OUTPUT_TOPIC
    dwg_path = version_dir / f"{output_stem}_3D_v{version}.dwg"

    service = CADService()
    ensure_drawable_document(service)
    valves = draw_valves(service)
    model_bounds = measure_model_bounds(service)
    zoom_extents_with_retry(service.controller)

    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG save failed: {dwg_path}")

    preview_paths = capture_previews(
        service,
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        model_bounds=model_bounds,
    )
    parameters_path, notes_path = write_artifacts(
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        valves=valves,
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
