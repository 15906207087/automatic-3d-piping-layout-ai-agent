from __future__ import annotations

import json
import sys
import time
from ctypes import windll
from pathlib import Path
from typing import Any

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

OUTPUT_TOPIC = "boolean_spheres_demo"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
WHITE = 7
SOLID_LAYER = "BOOLEAN_SPHERES_SOLID"
TEXT_LAYER = "BOOLEAN_SPHERES_TEXT"
SPHERE_RADIUS_MM = 320.0
SPHERE_CENTER_GAP_MM = 360.0
SOURCE_X_MM = 0.0
RESULT_X_MM = 2400.0
TEXT_HEIGHT_MM = 110.0

CASE_DEFINITIONS = [
    {
        "name": "intersection",
        "title": "Intersection",
        "operation": "intersection",
        "row_y": 1500.0,
    },
    {
        "name": "union",
        "title": "Union",
        "operation": "union",
        "row_y": 0.0,
    },
    {
        "name": "difference",
        "title": "Difference",
        "operation": "difference",
        "row_y": -1500.0,
    },
]


def ensure_drawable_document(service: CADService) -> None:
    """Attach to a usable CAD document, creating a fresh one when possible."""

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
    callback: Any,
    *,
    description: str,
    retries: int = 4,
    wait_seconds: float = 0.8,
) -> Any:
    """Retry one CAD creation or boolean call because COM operations can fail transiently."""

    last_result = None
    for _ in range(retries):
        try:
            last_result = callback()
        except Exception:
            last_result = None
        if last_result:
            return last_result
        time.sleep(wait_seconds)
    raise RuntimeError(f"Failed to complete {description}.")


def safe_delete(entity: Any) -> None:
    """Delete one transient CAD entity without failing the whole run."""

    if entity is None:
        return
    try:
        entity.Delete()
    except Exception:
        pass


def draw_text(service: CADService, position: tuple[float, float, float], text: str, height: float) -> None:
    """Draw one label and fail loudly if the text cannot be created."""

    draw_with_retry(
        lambda: service.controller.draw_text(
            position=position,
            text=text,
            height=height,
            layer=TEXT_LAYER,
            color=WHITE,
        ),
        description=f"text '{text}'",
    )


def draw_sphere_pair(
    service: CADService,
    *,
    base_x: float,
    base_y: float,
    z: float,
) -> tuple[Any, Any, tuple[float, float, float], tuple[float, float, float]]:
    """Draw one overlapping sphere pair and return both solids plus their centers."""

    left_center = (base_x - SPHERE_CENTER_GAP_MM / 2.0, base_y, z)
    right_center = (base_x + SPHERE_CENTER_GAP_MM / 2.0, base_y, z)
    sphere_a = draw_with_retry(
        lambda: service.controller.draw_sphere(
            center=left_center,
            radius=SPHERE_RADIUS_MM,
            layer=SOLID_LAYER,
            color=WHITE,
        ),
        description="sphere A",
    )
    sphere_b = draw_with_retry(
        lambda: service.controller.draw_sphere(
            center=right_center,
            radius=SPHERE_RADIUS_MM,
            layer=SOLID_LAYER,
            color=WHITE,
        ),
        description="sphere B",
    )
    return sphere_a, sphere_b, left_center, right_center


def run_boolean(service: CADService, *, operation: str, solid_a: Any, solid_b: Any) -> None:
    """Apply one boolean operation in place to `solid_a`."""

    if operation == "intersection":
        result = draw_with_retry(
            lambda: service.controller._solid_boolean_intersect(solid_a, solid_b),
            description="boolean intersection",
        )
        if result is None:
            raise RuntimeError("Boolean intersection returned no result.")
    elif operation == "union":
        result = draw_with_retry(
            lambda: service.controller._solid_boolean_union(solid_a, solid_b),
            description="boolean union",
        )
        if result is None:
            raise RuntimeError("Boolean union returned no result.")
    elif operation == "difference":
        ok = draw_with_retry(
            lambda: service.controller._solid_boolean_subtract(solid_a, solid_b),
            description="boolean difference",
        )
        if not ok:
            raise RuntimeError("Boolean difference returned failure.")
    else:
        raise ValueError(f"Unsupported boolean operation: {operation}")
    safe_delete(solid_b)
    service.controller.refresh_view()
    time.sleep(0.4)


def draw_case(service: CADService, case: dict[str, Any]) -> dict[str, Any]:
    """Draw one source/result row for a boolean operation."""

    row_y = float(case["row_y"])
    z = SPHERE_RADIUS_MM
    draw_text(
        service,
        position=(-950.0, row_y + 520.0, 0.0),
        text=case["title"],
        height=TEXT_HEIGHT_MM,
    )
    draw_text(
        service,
        position=(SOURCE_X_MM - 500.0, row_y - 650.0, 0.0),
        text="Source spheres",
        height=90.0,
    )
    draw_text(
        service,
        position=(RESULT_X_MM - 220.0, row_y - 650.0, 0.0),
        text="Result",
        height=90.0,
    )

    _, _, source_a_center, source_b_center = draw_sphere_pair(
        service,
        base_x=SOURCE_X_MM,
        base_y=row_y,
        z=z,
    )

    result_a, result_b, result_a_center, result_b_center = draw_sphere_pair(
        service,
        base_x=RESULT_X_MM,
        base_y=row_y,
        z=z,
    )
    run_boolean(
        service,
        operation=str(case["operation"]),
        solid_a=result_a,
        solid_b=result_b,
    )

    return {
        "name": case["name"],
        "title": case["title"],
        "operation": case["operation"],
        "radius_mm": SPHERE_RADIUS_MM,
        "sphere_center_gap_mm": SPHERE_CENTER_GAP_MM,
        "source_centers": [list(source_a_center), list(source_b_center)],
        "result_input_centers": [list(result_a_center), list(result_b_center)],
    }


def draw_cases(service: CADService) -> list[dict[str, Any]]:
    """Draw all requested boolean cases."""

    records: list[dict[str, Any]] = []
    for case in CASE_DEFINITIONS:
        records.append(draw_case(service, case))
    return records


def measure_model_bounds(service: CADService) -> dict[str, tuple[float, float, float]]:
    """Measure the model extents from drawable entities in model space."""

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
    """Capture front, top, and isometric previews for the boolean demo."""

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
    base_span = max(x_span, y_span, z_span)

    view_specs = [
        (
            "front",
            model_center,
            max(z_span * 3.0, 5200.0),
            max(x_span * 1.6, 11000.0),
            f"{output_stem}_front_preview_v{version}.png",
        ),
        (
            "top",
            model_center,
            max(y_span * 1.55, 9800.0),
            max(x_span * 1.6, 11000.0),
            f"{output_stem}_top_preview_v{version}.png",
        ),
        (
            "swiso",
            model_center,
            max(base_span * 2.7, 9800.0),
            max(base_span * 3.0, 10400.0),
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
        time.sleep(1.2)
        zoom_extents_with_retry(service.controller)
        time.sleep(1.0)
        zoom_extents_with_retry(service.controller)
        time.sleep(1.0)
        service.controller.refresh_view()
        time.sleep(1.0)
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
    cases: list[dict[str, Any]],
    dwg_path: Path,
    preview_paths: list[str],
) -> tuple[Path, Path]:
    """Write one parameter snapshot and one short note for the deliverable."""

    parameters_path = version_dir / f"{output_stem}_parameters_v{version}.json"
    notes_path = version_dir / f"{output_stem}_notes_v{version}.txt"
    payload = {
        "topic": OUTPUT_TOPIC,
        "version": version,
        "layer": SOLID_LAYER,
        "text_layer": TEXT_LAYER,
        "color_index": WHITE,
        "sphere_radius_mm": SPHERE_RADIUS_MM,
        "sphere_center_gap_mm": SPHERE_CENTER_GAP_MM,
        "cases": cases,
    }
    parameters_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    notes = [
        f"{OUTPUT_TOPIC} v{version}",
        "",
        f"DWG: {dwg_path}",
        "Layout:",
        "- Three rows: boolean intersection, boolean union, boolean difference.",
        "- Each row keeps one source sphere pair on the left and the boolean result on the right.",
        "- All geometry and labels use white for consistent CAD presentation.",
        "",
        "Previews:",
        *[f"- {path}" for path in preview_paths],
    ]
    notes_path.write_text("\n".join(notes), encoding="utf-8")
    return parameters_path, notes_path


def main() -> int:
    """Create one versioned DWG demonstrating boolean operations with two spheres."""

    version, version_dir = prepare_version_dir(OUTPUT_ROOT_DIR)
    output_stem = OUTPUT_TOPIC
    dwg_path = version_dir / f"{output_stem}_3D_v{version}.dwg"

    service = CADService()
    ensure_drawable_document(service)
    cases = draw_cases(service)
    zoom_extents_with_retry(service.controller)
    model_bounds = measure_model_bounds(service)

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
        cases=cases,
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
