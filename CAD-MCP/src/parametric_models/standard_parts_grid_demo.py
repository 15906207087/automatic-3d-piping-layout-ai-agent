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

OUTPUT_TOPIC = "standard_parts_grid_demo"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
WHITE = 7
PART_LAYER = "STANDARD_PARTS_GRID"
GRID_SPACING_X_MM = 2400.0
GRID_SPACING_Y_MM = 1600.0


def ensure_drawable_document(service: CADService) -> None:
    """Attach to a usable CAD document, creating a new one when supported."""

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
    """Retry one CAD solid/text creation because COM calls can fail transiently."""

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
    """Return one 3x3 grid of representative standard-library parts."""

    return [
        {
            "name": "45 deg elbow",
            "center": (-GRID_SPACING_X_MM, GRID_SPACING_Y_MM, 0.0),
            "builder": lambda controller, center: controller.draw_pipe_elbow(
                center=center,
                dn=100,
                angle=45,
                layer=PART_LAYER,
                color=WHITE,
            ),
            "parameters": {"standard": "GB/T 12459-2017", "dn": 100, "angle_deg": 45, "elbow_type": "LR"},
        },
        {
            "name": "90 deg elbow",
            "center": (0.0, GRID_SPACING_Y_MM, 0.0),
            "builder": lambda controller, center: controller.draw_pipe_elbow(
                center=center,
                dn=100,
                angle=90,
                layer=PART_LAYER,
                color=WHITE,
            ),
            "parameters": {"standard": "GB/T 12459-2017", "dn": 100, "angle_deg": 90, "elbow_type": "LR"},
        },
        {
            "name": "180 deg elbow",
            "center": (GRID_SPACING_X_MM, GRID_SPACING_Y_MM, 0.0),
            "builder": lambda controller, center: controller.draw_pipe_elbow(
                center=center,
                dn=100,
                angle=180,
                layer=PART_LAYER,
                color=WHITE,
            ),
            "parameters": {"standard": "GB/T 12459-2017", "dn": 100, "angle_deg": 180, "elbow_type": "LR"},
        },
        {
            "name": "Concentric reducer",
            "center": (-GRID_SPACING_X_MM, 0.0, 0.0),
            "builder": lambda controller, center: controller.draw_reducer(
                center=center,
                dn_large=150,
                dn_small=100,
                reducer_type="concentric",
                layer=PART_LAYER,
                color=WHITE,
            ),
            "parameters": {
                "standard": "GB/T 12459-2017",
                "dn_large": 150,
                "dn_small": 100,
                "reducer_type": "concentric",
            },
        },
        {
            "name": "Equal tee",
            "center": (0.0, 0.0, 0.0),
            "builder": lambda controller, center: controller.draw_tee(
                center=center,
                dn=100,
                run_axis="x",
                branch_axis="z",
                layer=PART_LAYER,
                color=WHITE,
            ),
            "parameters": {"standard": "GB/T 12459-2017", "run_dn": 100, "branch_dn": 100},
        },
        {
            "name": "Blind flange",
            "center": (GRID_SPACING_X_MM, 0.0, 0.0),
            "builder": lambda controller, center: controller.draw_blind_flange(
                center=center,
                specification="GB/T 9119",
                dn=100,
                pn=16,
                layer=PART_LAYER,
                color=WHITE,
            ),
            "parameters": {"standard": "GB/T 9119", "dn": 100, "pn": 16},
        },
        {
            "name": "Flange",
            "center": (-GRID_SPACING_X_MM, -GRID_SPACING_Y_MM, 0.0),
            "builder": lambda controller, center: controller.draw_flange(
                center=center,
                specification="GB/T 9119",
                dn=100,
                pn=16,
                layer=PART_LAYER,
                color=WHITE,
            ),
            "parameters": {"standard": "GB/T 9119", "dn": 100, "pn": 16},
        },
        {
            "name": "Bolt",
            "center": (0.0, -GRID_SPACING_Y_MM, 0.0),
            "builder": lambda controller, center: controller.draw_bolt(
                base_center=center,
                specification="GB/T 5782",
                thread_size="M16",
                length=80.0,
                axis="z",
                layer=PART_LAYER,
                color=WHITE,
            ),
            "parameters": {"standard": "GB/T 5782", "thread_size": "M16", "length_mm": 80.0},
        },
        {
            "name": "Hex nut",
            "center": (GRID_SPACING_X_MM, -GRID_SPACING_Y_MM, 0.0),
            "builder": lambda controller, center: controller.draw_hex_nut(
                center=center,
                specification="GB/T 6170.1",
                thread_size="M16",
                axis="z",
                layer=PART_LAYER,
                color=WHITE,
            ),
            "parameters": {"standard": "GB/T 6170.1", "thread_size": "M16"},
        },
    ]


def draw_grid(service: CADService) -> list[dict[str, Any]]:
    """Draw every part in the layout and return the resolved placement table."""

    controller = service.controller
    resolved_parts: list[dict[str, Any]] = []
    for item in build_layout():
        center = tuple(float(value) for value in item["center"])
        draw_with_retry(
            lambda item=item, center=center: item["builder"](controller, center),
            description=item["name"],
        )
        resolved_parts.append(
            {
                "name": item["name"],
                "center": list(center),
                "parameters": item["parameters"],
            }
        )
    return resolved_parts


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
    """Capture front, top, and isometric previews for the part grid."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    view_specs = [
        ("front", model_center, height, width, f"{output_stem}_front_preview_v{version}.png"),
        ("top", model_center, height * 1.05, width * 1.05, f"{output_stem}_top_preview_v{version}.png"),
        ("swiso", model_center, height * 1.2, width * 1.2, f"{output_stem}_iso_preview_v{version}.png"),
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
    parts: list[dict[str, Any]],
    dwg_path: Path,
    preview_paths: list[str],
) -> tuple[Path, Path]:
    """Write one parameter snapshot and one human-readable note."""

    parameters_path = version_dir / f"{output_stem}_parameters_v{version}.json"
    notes_path = version_dir / f"{output_stem}_notes_v{version}.txt"
    payload = {
        "topic": OUTPUT_TOPIC,
        "version": version,
        "grid": {"rows": 3, "columns": 3, "spacing_x_mm": GRID_SPACING_X_MM, "spacing_y_mm": GRID_SPACING_Y_MM},
        "parts": parts,
    }
    parameters_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    notes = [
        f"{OUTPUT_TOPIC} v{version}",
        "",
        f"DWG: {dwg_path}",
        "Layout:",
        "- 3 rows x 3 columns, evenly spaced to avoid overlap in front/top/isometric previews.",
        "- All parts drawn in white on one shared layer using default standard-library dimensions.",
        "",
        "Cell order:",
        "- Row 1: 45 deg elbow | 90 deg elbow | 180 deg elbow",
        "- Row 2: concentric reducer | equal tee | blind flange",
        "- Row 3: flange | bolt | hex nut",
        "",
        "Previews:",
        *[f"- {path}" for path in preview_paths],
    ]
    notes_path.write_text("\n".join(notes), encoding="utf-8")
    return parameters_path, notes_path


def main() -> int:
    """Create one versioned CAD sheet showing nine standard parts in a grid."""

    version, version_dir = prepare_version_dir(OUTPUT_ROOT_DIR)
    output_stem = OUTPUT_TOPIC
    dwg_path = version_dir / f"{output_stem}_3D_v{version}.dwg"

    service = CADService()
    ensure_drawable_document(service)
    parts = draw_grid(service)
    zoom_extents_with_retry(service.controller)

    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG save failed: {dwg_path}")

    preview_paths = capture_previews(
        service,
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        model_center=(0.0, 0.0, 120.0),
        height=9600.0,
        width=11000.0,
    )
    parameters_path, notes_path = write_artifacts(
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        parts=parts,
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
