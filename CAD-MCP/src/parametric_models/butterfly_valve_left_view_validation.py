from __future__ import annotations

import json
import shutil
import sys
import time
from ctypes import windll
from pathlib import Path

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
)


OUTPUT_TOPIC = "蝶阀左视修正"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
SHARED_DIR = OUTPUT_ROOT_DIR / "shared"
SOURCE_IMAGE_PATH = Path(
    r"C:\Users\chenao\.cursor\projects\c-Users-chenao-Desktop-CAD-2\assets\c__Users_chenao_AppData_Roaming_Cursor_User_workspaceStorage_f905f1cc2c1801844e900d7e64c3acf1_images_image-88cc649d-6889-4018-8f64-cb4a714889a5.png"
)
WHITE = 7


def create_validation_document(service: CADService) -> None:
    """Attach to a usable CAD document, creating a new one when supported."""

    if not service.start_cad():
        raise RuntimeError("无法启动或连接 CAD。")
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


def archive_source_image() -> str | None:
    """Copy the user-provided reference image into the topic shared folder."""

    if not SOURCE_IMAGE_PATH.exists():
        return None
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    archived_path = SHARED_DIR / SOURCE_IMAGE_PATH.name
    if not archived_path.exists():
        shutil.copy2(SOURCE_IMAGE_PATH, archived_path)
    return str(archived_path)


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
    """Capture front, left, and isometric screenshots for visual comparison."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    view_specs = [
        ("front", model_center, height, width, f"{output_stem}_front_preview_v{version}.png"),
        ("left", model_center, height, width, f"{output_stem}_left_preview_v{version}.png"),
        ("swiso", model_center, height * 1.30, width * 1.30, f"{output_stem}_iso_preview_v{version}.png"),
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
        time.sleep(0.8)
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


def main() -> int:
    """Draw one validation butterfly valve and archive its previews."""

    archived_source_image = archive_source_image()
    version, version_dir = prepare_version_dir(OUTPUT_ROOT_DIR)
    service = CADService()
    create_validation_document(service)
    entity = service.controller.draw_butterfly_valve(
        center=(0.0, 0.0, 0.0),
        dn=150,
        pn=16,
        axis="y",
        layer="BUTTERFLY_VALVE_VALIDATION",
        color=WHITE,
    )
    if entity is None:
        raise RuntimeError("蝶阀验证图绘制失败。")

    output_stem = "蝶阀左视修正"
    dwg_path = version_dir / f"{output_stem}_dn150_pn16_v{version}.dwg"
    if not save_drawing_with_retry(service.controller, dwg_path):
        raise RuntimeError(f"保存 DWG 失败: {dwg_path}")

    preview_paths = capture_previews(
        service,
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        model_center=(0.0, 0.0, 55.0),
        height=420.0,
        width=520.0,
    )

    parameters_path = version_dir / f"{output_stem}_parameters_v{version}.json"
    parameters_path.write_text(
        json.dumps(
            {
                "topic": OUTPUT_TOPIC,
                "dn": 150,
                "pn": 16,
                "axis": "y",
                "layer": "BUTTERFLY_VALVE_VALIDATION",
                "color": WHITE,
                "source_image": archived_source_image,
                "dwg": str(dwg_path),
                "previews": preview_paths,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    notes_path = version_dir / f"{output_stem}_notes_v{version}.txt"
    notes_path.write_text(
        "\n".join(
            [
                f"{OUTPUT_TOPIC} v{version}",
                "",
                "本轮验证目标：",
                "- 对照用户给定三视图图片，检查蝶阀左视图的阀体高度比例、阀颈层次和手柄基座位置。",
                "",
                f"DWG: {dwg_path}",
                f"源图: {archived_source_image or SOURCE_IMAGE_PATH}",
                "参数: DN150 / PN16 / axis=y / white",
                "",
                "预览图:",
                *[f"- {path}" for path in preview_paths],
            ]
        ),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "version": version,
                "version_dir": str(version_dir),
                "dwg": str(dwg_path),
                "previews": preview_paths,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
