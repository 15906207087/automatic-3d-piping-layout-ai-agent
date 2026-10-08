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

OUTPUT_TOPIC = "国标人孔功能验证"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
SHARED_DIR = OUTPUT_ROOT_DIR / "shared"
SOURCE_IMAGE_PATHS = [
    Path(
        r"C:\Users\chenao\.cursor\projects\c-Users-chenao-Desktop-CAD-2\assets\c__Users_chenao_AppData_Roaming_Cursor_User_workspaceStorage_f905f1cc2c1801844e900d7e64c3acf1_images_image-9c0f1135-2938-471e-8e7a-e2c0a19c4a02.png"
    )
]
WHITE = 7

MANHOLE_SAMPLES = [
    {"dn": 450, "pn": 1.0, "center": (-900.0, 0.0, 0.0)},
    {"dn": 500, "pn": 1.0, "center": (900.0, 0.0, 0.0)},
]


def create_validation_document(service: CADService) -> None:
    """Attach to a usable CAD document, creating a new one when supported."""

    if not service.start_cad():
        raise RuntimeError("无法启动或连接 CAD。")
    docs = getattr(service.controller.app, "Documents", None)
    last_error: Exception | None = None
    if docs is not None:
        try:
            doc_count = int(getattr(docs, "Count", 0))
        except Exception:
            doc_count = 0
        for index in list(range(doc_count)) + list(range(1, doc_count + 1)):
            try:
                candidate = docs.Item(index)
                if candidate is None:
                    continue
                service.controller.doc = candidate
                if service.controller._get_model_space() is not None:
                    return
            except Exception as exc:
                last_error = exc
        try:
            docs.Add()
            time.sleep(4.5)
        except Exception as exc:
            last_error = exc
    for _ in range(14):
        try:
            active_doc = getattr(service.controller.app, "ActiveDocument", None)
            if active_doc is not None:
                service.controller.doc = active_doc
            if service.controller._get_model_space() is not None:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(1.0)
    raise RuntimeError(f"CAD document is not drawable yet: {last_error}")


def archive_source_images() -> list[str]:
    """Copy the user-provided manhole reference images into the topic folder."""

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
    """Capture front, top, and isometric previews for visual comparison."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    view_specs = [
        ("front", model_center, height, width, f"{output_stem}_front_preview_v{version}.png"),
        ("top", model_center, height * 1.08, width * 1.08, f"{output_stem}_top_preview_v{version}.png"),
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
        time.sleep(0.8)
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


def capture_sample_local_previews(
    service: CADService,
    *,
    version_dir: Path,
    version: int,
    output_stem: str,
    samples: list[dict[str, float]],
) -> list[str]:
    """Capture per-sample top and isometric previews for each DN."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    saved_paths: list[str] = []
    for sample in samples:
        dn = int(sample["dn"])
        center_x, center_y, center_z = sample["center"]
        scale = 1.55 if dn == 450 else 1.70 if dn == 500 else 1.90
        for view_name, height, width, file_name in (
            ("top", 950.0 * scale, 950.0 * scale, f"{output_stem}_dn{dn}_top_preview_v{version}.png"),
            ("swiso", 1100.0 * scale, 1100.0 * scale, f"{output_stem}_dn{dn}_iso_preview_v{version}.png"),
        ):
            if not set_named_view_with_retry(
                service.controller,
                view_name,
                target=(center_x, center_y, center_z + 160.0),
                height=height,
                width=width,
            ):
                raise RuntimeError(f"局部视图切换失败: DN{dn} {view_name}")
            time.sleep(0.8)
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


def bring_cad_to_foreground(service: CADService) -> None:
    """Best-effort foregrounding before screen capture."""

    hwnd = service.controller.get_document_hwnd() or service.controller.get_application_hwnd()
    if not hwnd:
        return
    try:
        windll.user32.ShowWindow(hwnd, 9)
        windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass


def main() -> int:
    """Draw one DN450/DN500 standard manhole validation sheet and archive artifacts."""

    archived_source_images = archive_source_images()
    version, version_dir = prepare_version_dir(OUTPUT_ROOT_DIR)
    service = CADService()
    create_validation_document(service)

    resolved_samples: list[dict[str, float]] = []
    for sample in MANHOLE_SAMPLES:
        entity = service.controller.draw_manhole(
            center=sample["center"],
            specification="HG/T 21517",
            dn=sample["dn"],
            pn=sample["pn"],
            layer="MANHOLE_VALIDATION",
            color=WHITE,
        )
        if entity is None:
            raise RuntimeError(f"人孔验证图绘制失败: DN{sample['dn']}")
        resolved_samples.append(
            {
                "standard": "HG/T 21517",
                "dn": sample["dn"],
                "pn": sample["pn"],
                "center": list(sample["center"]),
            }
        )

    output_stem = "国标人孔功能验证"
    dwg_path = version_dir / f"{output_stem}_sample_v{version}.dwg"
    if not save_drawing_with_retry(service.controller, dwg_path):
        raise RuntimeError(f"保存 DWG 失败: {dwg_path}")

    bring_cad_to_foreground(service)
    preview_paths = capture_previews(
        service,
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        model_center=(0.0, 0.0, 180.0),
        height=1100.0,
        width=3000.0,
    )
    preview_paths.extend(
        capture_sample_local_previews(
            service,
            version_dir=version_dir,
            version=version,
            output_stem=output_stem,
            samples=resolved_samples,
        )
    )

    parameters_path = version_dir / f"{output_stem}_parameters_v{version}.json"
    parameters_path.write_text(
        json.dumps(
            {
                "topic": OUTPUT_TOPIC,
                "view_interpretation": "用户提供的是人孔总成等轴 3D 设备视图，本次按 HG/T 21517 首版标准族驱动的低矮圆形人孔总成进行 DN450、DN500 双样件验证。",
                "source_images": archived_source_images,
                "parameters": {"samples": resolved_samples, "color": WHITE},
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
                "- 将用户提供的人孔 3D 设备图解释为等轴视图下的低矮圆形人孔总成。",
                "- 按当前需求仅输出 HG/T 21517 首版中的 DN450、DN500 两个样件。",
                "- 同一张验证图内并排检查 2 个公称直径的人孔比例和层次是否稳定。",
                "",
                f"DWG: {dwg_path}",
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
