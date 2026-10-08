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


OUTPUT_TOPIC = "截止阀三视图修正"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
SHARED_DIR = OUTPUT_ROOT_DIR / "shared"
SOURCE_IMAGE_PATHS = [
    OUTPUT_ROOT_DIR / "shared" / "ref_photo_globe_valve.png",
    OUTPUT_ROOT_DIR / "shared" / "ref_drawing_globe_valve.png",
]
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


def archive_source_images() -> list[str]:
    """Copy the user-provided reference images into the topic shared folder."""

    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    archived_paths: list[str] = []
    for source_path in SOURCE_IMAGE_PATHS:
        if not source_path.exists():
            continue
        archived_path = SHARED_DIR / source_path.name
        if not archived_path.exists():
            shutil.copy2(source_path, archived_path)
        archived_paths.append(str(archived_path))
    return archived_paths


def prepare_smooth_curve_display(service: CADService) -> None:
    """Raise isoline/facet settings and switch to a shaded style for smooth arcs."""

    doc = service.controller.doc
    for name, value in (("ISOLINES", 8), ("FACETRES", 10), ("DISPSILH", 1), ("VIEWRES", 1000)):
        try:
            doc.SetVariable(name, value)
        except Exception:
            pass
    # SendCommand 异步排队，多试几种常见写法，确保预览不是二维线框。
    for cmd in (
        "_.VSCURRENT Conceptual ",
        "_.VSCURRENT\nConceptual\n",
        "_.SHADEMODE\nG\n",
    ):
        try:
            doc.SendCommand(cmd)
            time.sleep(0.8)
            break
        except Exception:
            continue
    try:
        doc.Regen(1)
    except Exception:
        pass


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
    """Capture front, left, top, and isometric screenshots for visual comparison."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    view_specs = [
        ("front", model_center, height, width, f"{output_stem}_front_preview_v{version}.png"),
        ("left", model_center, height, width, f"{output_stem}_left_preview_v{version}.png"),
        ("top", model_center, height * 1.05, width * 1.05, f"{output_stem}_top_preview_v{version}.png"),
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
        # 切视图后可能回到线框，再推一次概念着色。
        try:
            service.controller.doc.SendCommand("_.VSCURRENT Conceptual ")
            time.sleep(0.5)
            service.controller.doc.Regen(1)
        except Exception:
            pass
        time.sleep(0.5)
        capture = capturer.capture_cad_window(
            document_name=document_name,
            title_hint=title_hint,
            preferred_hwnd=preferred_hwnd,
            document_hwnd=document_hwnd,
        )
        final_path = version_dir / file_name
        shutil.copy2(capture["image_path"], final_path)
        saved_paths.append(str(final_path))
    return saved_paths


def main() -> int:
    """Draw one validation globe valve and archive its previews."""

    archived_source_images = archive_source_images()
    version, version_dir = prepare_version_dir(OUTPUT_ROOT_DIR)
    service = CADService()
    create_validation_document(service)
    prepare_smooth_curve_display(service)
    entity = service.controller.draw_globe_valve(
        center=(0.0, 0.0, 0.0),
        dn=150,
        pn=16,
        axis="x",
        layer="GLOBE_VALVE_VALIDATION",
        color=WHITE,
    )
    if entity is None:
        raise RuntimeError("截止阀验证图绘制失败。")

    output_stem = "截止阀三视图修正"
    dwg_path = version_dir / f"{output_stem}_dn150_pn16_v{version}.dwg"
    if not save_drawing_with_retry(service.controller, dwg_path):
        raise RuntimeError(f"保存 DWG 失败: {dwg_path}")

    preview_paths = capture_previews(
        service,
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        model_center=(0.0, 0.0, 280.0),
        height=1200.0,
        width=1050.0,
    )

    parameters_path = version_dir / f"{output_stem}_parameters_v{version}.json"
    parameters_path.write_text(
        json.dumps(
            {
                "topic": OUTPUT_TOPIC,
                "dn": 150,
                "pn": 16,
                "axis": "x",
                "layer": "GLOBE_VALVE_VALIDATION",
                "color": WHITE,
                "source_images": archived_source_images,
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
                "- 弯支架下端贴六螺钉圆盘上方颈柱外壁：先沿颈柱爬升再外扩，禁止沿圆盘顶横扫。",
                "- 上端仍接手轮下连接柱；右侧完整 C 镜像到左侧。",
                "- 前视/轴测应看见下端接在颈柱圆柱面，而不是法兰盘面。",
                "",
                f"DWG: {dwg_path}",
                "参数: DN150 / PN16 / axis=x / white",
                "源图:",
                *[f"- {path}" for path in archived_source_images],
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
