from __future__ import annotations

import json
import math
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ctypes import windll

CURRENT_FILE = Path(__file__).resolve()
SRC_DIR = CURRENT_FILE.parents[1]
PROJECT_DIR = CURRENT_FILE.parents[2]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cad_controller import CADController
from pipeline_connection_helper import (
    PipeConnectionRunSpec,
    RotationInstruction,
    build_standard_elbow_connection,
    resolve_elbow_bend_radius,
)
from screen_capture import ScreenCapture

OUTPUT_TOPIC = "DN250弯头45_90_180连接验证"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
WHITE = 7


@dataclass(frozen=True)
class ElbowAngleCase:
    """One DN250 elbow verification case."""

    case_id: str
    angle_deg: float
    bend_center_mm: tuple[float, float, float]
    primary_length_mm: float = 1800.0
    secondary_length_mm: float = 1800.0


class CADService:
    """Lightweight scripted CAD wrapper used by local parametric demos."""

    def __init__(self) -> None:
        self.controller = CADController()
        self.drawing_state: dict[str, Any] = {
            "entities": [],
            "current_layer": "0",
            "last_command": "",
            "last_result": "",
        }

    def start_cad(self) -> bool:
        """Start or connect to CAD through the underlying controller."""

        return bool(self.controller.start_cad())


def create_fresh_document(service: CADService) -> None:
    """Create one new blank drawing for the current scripted run."""

    if not service.start_cad():
        raise RuntimeError("无法启动或连接 CAD。")
    docs = getattr(service.controller.app, "Documents", None)
    if docs is None:
        raise RuntimeError("CAD 未暴露 Documents 集合，无法创建新文档。")
    docs.Add()
    time.sleep(2.5)
    service.controller.doc = service.controller.app.ActiveDocument
    service.drawing_state["entities"] = []


def prepare_version_dir(output_root: Path) -> tuple[int, Path]:
    """Create the next version directory without overwriting history."""

    output_root.mkdir(parents=True, exist_ok=True)
    version_pattern = re.compile(r"^v(\d+)$", re.IGNORECASE)
    max_version = 0
    for item in output_root.iterdir():
        if item.is_dir():
            match = version_pattern.match(item.name)
            if match:
                max_version = max(max_version, int(match.group(1)))
    version = max_version + 1
    version_dir = output_root / f"v{version}"
    version_dir.mkdir(parents=True, exist_ok=False)
    return version, version_dir


def zoom_extents_with_retry(controller: Any, retries: int = 4, wait_seconds: float = 0.8) -> bool:
    """Retry zoom extents because CAD view refresh can be busy."""

    for _ in range(retries):
        try:
            if controller.zoom_extents():
                return True
        except Exception:
            pass
        time.sleep(wait_seconds)
    return False


def save_drawing_with_retry(controller: Any, path: str, retries: int = 4, wait_seconds: float = 1.2) -> bool:
    """Retry SaveAs because the CAD COM layer can reject the first attempt."""

    for _ in range(retries):
        try:
            if controller.save_drawing(path):
                return True
        except Exception:
            pass
        time.sleep(wait_seconds)
    return False


def set_named_view_with_retry(
    controller: Any,
    view_name: str,
    *,
    target: tuple[float, float, float],
    height: float,
    width: float,
    retries: int = 4,
    wait_seconds: float = 1.0,
) -> bool:
    """Switch views with retries so preview capture stays stable."""

    for _ in range(retries):
        try:
            if controller.set_named_view(view_name, target=target, center=(0.0, 0.0), height=height, width=width):
                time.sleep(wait_seconds)
                return True
        except Exception:
            pass
        time.sleep(wait_seconds)
    return False


def capture_previews(
    service: CADService,
    version_dir: Path,
    version: int,
    *,
    model_center: tuple[float, float, float],
) -> list[str]:
    """Capture front, top and isometric previews for manual verification."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = service.controller.get_document_name()
    title_hint = service.controller.get_application_caption()
    view_specs = [
        ("front", model_center, 5000.0, 16000.0, f"{OUTPUT_TOPIC}_front_preview_v{version}.png"),
        ("top", model_center, 5000.0, 16000.0, f"{OUTPUT_TOPIC}_top_preview_v{version}.png"),
        ("swiso", model_center, 7000.0, 18000.0, f"{OUTPUT_TOPIC}_iso_preview_v{version}.png"),
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
            windll.user32.SetCursorPos(
                int(window["left"] + window["width"] * 0.1),
                int(window["top"] + window["height"] * 0.35),
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


def build_case_run_specs(case: ElbowAngleCase, bend_radius: float) -> list[PipeConnectionRunSpec]:
    """Build the two pipe runs that should mate with one local elbow case."""

    cx, cy, cz = case.bend_center_mm
    root_half = math.sqrt(0.5)
    if int(round(case.angle_deg)) == 45:
        return [
            PipeConnectionRunSpec(
                run_id=f"{case.case_id}_diag_pipe",
                tangent_point_mm=(cx + bend_radius * root_half, cy + bend_radius * root_half, cz),
                axis_away_from_fitting="custom_45_diag",
                length_mm=float(case.primary_length_mm),
                connection_end="tangent_end",
                direction_vector_away_from_fitting=(-root_half, root_half, 0.0),
            ),
            PipeConnectionRunSpec(
                run_id=f"{case.case_id}_left_pipe",
                tangent_point_mm=(cx, cy + bend_radius, cz),
                axis_away_from_fitting="-x",
                length_mm=float(case.secondary_length_mm),
                connection_end="right_end",
            ),
        ]
    if int(round(case.angle_deg)) == 90:
        return [
            PipeConnectionRunSpec(
                run_id=f"{case.case_id}_up_pipe",
                tangent_point_mm=(cx + bend_radius, cy, cz),
                axis_away_from_fitting="+y",
                length_mm=float(case.primary_length_mm),
                connection_end="left_end",
            ),
            PipeConnectionRunSpec(
                run_id=f"{case.case_id}_left_pipe",
                tangent_point_mm=(cx, cy + bend_radius, cz),
                axis_away_from_fitting="-x",
                length_mm=float(case.secondary_length_mm),
                connection_end="right_end",
            ),
        ]
    if int(round(case.angle_deg)) == 180:
        return [
            PipeConnectionRunSpec(
                run_id=f"{case.case_id}_up_pipe",
                tangent_point_mm=(cx + bend_radius, cy, cz),
                axis_away_from_fitting="+y",
                length_mm=float(case.primary_length_mm),
                connection_end="left_end",
            ),
            PipeConnectionRunSpec(
                run_id=f"{case.case_id}_down_pipe",
                tangent_point_mm=(cx - bend_radius, cy, cz),
                axis_away_from_fitting="-y",
                length_mm=float(case.secondary_length_mm),
                connection_end="right_end",
            ),
        ]
    raise ValueError(f"不支持的弯头角度: {case.angle_deg}")


def build_suite_geometry(service: CADService, dn: int = 250) -> dict[str, Any]:
    """Draw three DN250 pipe-to-elbow connection cases for 45/90/180 degrees."""

    controller = service.controller
    bend_radius = resolve_elbow_bend_radius(controller, dn, "LR")
    layer = f"PIPE_DN{dn}"
    cases = [
        ElbowAngleCase(case_id="elbow_45", angle_deg=45.0, bend_center_mm=(-5000.0, 0.0, 0.0)),
        ElbowAngleCase(case_id="elbow_90", angle_deg=90.0, bend_center_mm=(0.0, 0.0, 0.0)),
        ElbowAngleCase(case_id="elbow_180", angle_deg=180.0, bend_center_mm=(5000.0, 0.0, 0.0)),
    ]
    case_results: list[dict[str, Any]] = []
    for case in cases:
        geometry = build_standard_elbow_connection(
            controller,
            dn=dn,
            bend_center=case.bend_center_mm,
            elbow_type="LR",
            angle_deg=case.angle_deg,
            run_specs=build_case_run_specs(case, bend_radius),
            elbow_rotations=[],
            layer=layer,
            color=WHITE,
        )
        geometry["case_id"] = case.case_id
        geometry["case_angle_deg"] = case.angle_deg
        case_results.append(geometry)

    if not zoom_extents_with_retry(controller):
        raise RuntimeError("缩放到全部对象失败。")

    return {
        "topic": OUTPUT_TOPIC,
        "dn": int(dn),
        "elbow_type": "LR",
        "bend_radius_mm": bend_radius,
        "cases": case_results,
        "verification_basis": (
            "本轮统一按弯头端口几何校验：弯头端口中心与直管端口中心重合，"
            "弯头端口轴线与直管中心线共线，且两侧端面法向一致，从而保证端面平行并贴合。"
        ),
    }


def write_version_artifacts(
    version_dir: Path,
    version: int,
    *,
    dwg_path: Path,
    preview_paths: list[str],
    geometry: dict[str, Any],
) -> tuple[Path, Path]:
    """Write one parameter snapshot and one notes file for this revision."""

    parameters_path = version_dir / f"{OUTPUT_TOPIC}_parameters_v{version}.json"
    notes_path = version_dir / f"{OUTPUT_TOPIC}_notes_v{version}.txt"
    parameters_path.write_text(
        json.dumps(
            {
                "topic": OUTPUT_TOPIC,
                "version": version,
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
                "geometry": geometry,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    lines = [
        f"{OUTPUT_TOPIC} 绘制说明 v{version}",
        "",
        f"DWG: {dwg_path}",
        "本轮绘制三组空心 DN250 直管通过国标 LR 弯头连接的对比案例，分别为 45°、90°、180°。",
        "统一校验准则：弯头端口轴线与对应直管中心线共线、端口圆心重合、两侧端面法向一致且贴合。",
        "",
    ]
    for case in geometry["cases"]:
        lines.append(f"案例 {case['case_id']} / 角度 {case['case_angle_deg']}°")
        lines.append(f"  弯曲中心: {case['bend_center_mm']}")
        for run in case["runs"]:
            lines.append(
                "  "
                + f"{run['run_id']}: tangent={run['tangent_point_mm']}, axis={run['axis']}, "
                + f"center_distance_mm={run['center_distance_mm']}, face_parallel_cosine={run['face_parallel_cosine']}"
            )
        lines.append("")
    notes_path.write_text("\n".join(lines), encoding="utf-8")
    return parameters_path, notes_path


def main() -> int:
    """Build the DN250 45/90/180 elbow connection verification drawing."""

    version, version_dir = prepare_version_dir(OUTPUT_ROOT_DIR)
    dwg_path = version_dir / f"{OUTPUT_TOPIC}_3D_v{version}.dwg"

    service = CADService()
    create_fresh_document(service)
    geometry = build_suite_geometry(service)

    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    preview_paths = capture_previews(
        service,
        version_dir,
        version,
        model_center=(0.0, 900.0, 0.0),
    )
    parameters_path, notes_path = write_version_artifacts(
        version_dir,
        version,
        dwg_path=dwg_path,
        preview_paths=preview_paths,
        geometry=geometry,
    )

    print(
        json.dumps(
            {
                "success": True,
                "topic": OUTPUT_TOPIC,
                "version": version,
                "version_dir": str(version_dir),
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
                "parameters_path": str(parameters_path),
                "notes_path": str(notes_path),
                "geometry": geometry,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
