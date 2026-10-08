from __future__ import annotations

import json
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

OUTPUT_TOPIC = "DN250标准弯头连接"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
WHITE = 7
DIAGONAL_UNIT = 2.0 ** -0.5


@dataclass(frozen=True)
class ElbowCaseParameters:
    """One reusable DN250 elbow verification case."""

    case_id: str
    angle_deg: float
    bend_center_mm: tuple[float, float, float]
    run_length_mm: float = 1800.0


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
    """Create a new blank drawing document for the current scripted run."""

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
    """Create the next version directory without overwriting historical outputs."""

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
    """Retry zoom extents because view refresh can fail while CAD is busy."""

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
    top_target = (0.0, 250.0, 0.0)
    view_specs = [
        ("front", model_center, 3800.0, 16000.0, f"{OUTPUT_TOPIC}_front_preview_v{version}.png"),
        ("top", top_target, 5200.0, 19000.0, f"{OUTPUT_TOPIC}_top_preview_v{version}.png"),
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


def build_case_run_specs(case: ElbowCaseParameters, bend_radius: float) -> list[PipeConnectionRunSpec]:
    """Build one case's two run specs using the actual elbow end-port geometry."""

    cx, cy, cz = case.bend_center_mm
    if int(round(case.angle_deg)) == 45:
        return [
            PipeConnectionRunSpec(
                run_id=f"{case.case_id}_pipe_1",
                tangent_point_mm=(cx, cy + bend_radius, cz),
                axis_away_from_fitting="-x",
                length_mm=float(case.run_length_mm),
                connection_end="right_end",
                flow_role="pipe_1",
            ),
            PipeConnectionRunSpec(
                run_id=f"{case.case_id}_pipe_2",
                tangent_point_mm=(cx + bend_radius * DIAGONAL_UNIT, cy + bend_radius * DIAGONAL_UNIT, cz),
                axis_away_from_fitting="+x",
                direction_vector_away_from_fitting=(DIAGONAL_UNIT, -DIAGONAL_UNIT, 0.0),
                length_mm=float(case.run_length_mm),
                connection_end="tangent_end",
                flow_role="pipe_2",
            ),
        ]
    if int(round(case.angle_deg)) == 90:
        return [
            PipeConnectionRunSpec(
                run_id=f"{case.case_id}_pipe_1",
                tangent_point_mm=(cx, cy + bend_radius, cz),
                axis_away_from_fitting="-x",
                length_mm=float(case.run_length_mm),
                connection_end="right_end",
                flow_role="pipe_1",
            ),
            PipeConnectionRunSpec(
                run_id=f"{case.case_id}_pipe_2",
                tangent_point_mm=(cx + bend_radius, cy, cz),
                axis_away_from_fitting="-y",
                length_mm=float(case.run_length_mm),
                connection_end="top_end",
                flow_role="pipe_2",
            ),
        ]
    if int(round(case.angle_deg)) == 180:
        return [
            PipeConnectionRunSpec(
                run_id=f"{case.case_id}_pipe_1",
                tangent_point_mm=(cx - bend_radius, cy, cz),
                axis_away_from_fitting="+y",
                length_mm=float(case.run_length_mm),
                connection_end="bottom_end",
                flow_role="pipe_1",
            ),
            PipeConnectionRunSpec(
                run_id=f"{case.case_id}_pipe_2",
                tangent_point_mm=(cx + bend_radius, cy, cz),
                axis_away_from_fitting="+y",
                length_mm=float(case.run_length_mm),
                connection_end="bottom_end",
                flow_role="pipe_2",
            ),
        ]
    raise ValueError(f"不支持的弯头角度: {case.angle_deg}")


def build_suite_geometry(service: CADService) -> dict[str, Any]:
    """Draw three DN250 straight-pipe elbow cases for 45/90/180 degrees."""

    controller = service.controller
    dn = 250
    elbow_type = "LR"
    bend_radius = resolve_elbow_bend_radius(controller, dn, elbow_type)
    layer = f"PIPE_DN{dn}"
    cases = [
        ElbowCaseParameters(case_id="dn250_45", angle_deg=45.0, bend_center_mm=(-5500.0, 0.0, 0.0)),
        ElbowCaseParameters(case_id="dn250_90", angle_deg=90.0, bend_center_mm=(0.0, 0.0, 0.0)),
        ElbowCaseParameters(case_id="dn250_180", angle_deg=180.0, bend_center_mm=(5500.0, 0.0, 0.0)),
    ]
    built_cases: list[dict[str, Any]] = []
    for case in cases:
        geometry = build_standard_elbow_connection(
            controller,
            dn=dn,
            bend_center=case.bend_center_mm,
            elbow_type=elbow_type,
            angle_deg=case.angle_deg,
            run_specs=build_case_run_specs(case, bend_radius),
            elbow_rotations=[],
            layer=layer,
            color=WHITE,
        )
        built_cases.append(
            {
                "case_id": case.case_id,
                "angle_deg": case.angle_deg,
                "run_length_mm": case.run_length_mm,
                **geometry,
            }
        )

    if not zoom_extents_with_retry(controller):
        raise RuntimeError("缩放到全部对象失败。")

    return {
        "topic": OUTPUT_TOPIC,
        "dn": dn,
        "elbow_type": elbow_type,
        "bend_radius_mm": bend_radius,
        "cases": built_cases,
        "verification_basis": (
            "本轮按串联流路建模并校验：每个案例都固定为 pipe_1 -> elbow -> pipe_2。"
            "两端连接均要求弯头端口轴线与所连直管中心线共线，弯头端面圆心与直管端面圆心重合，"
            "且两端面法向平行、端面贴合；当前结果不是三通，也不存在弯头后分叉成两个泄漏出口。"
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
    note_lines = [
        f"{OUTPUT_TOPIC} 绘制说明 v{version}",
        "",
        f"DWG: {dwg_path}",
        "本轮统一采用空心 DN250 直管 + 国标 LR 标准弯头。",
        f"标准弯头弯曲半径 R = {geometry['bend_radius_mm']:.3f} mm。",
        "流路语义: 每个案例均表示 pipe_1 -> elbow -> pipe_2 的连续流路，而不是弯头后继续分叉成两个出口。",
        "校验准则: 弯头端口轴线与直管中心线共线，端口圆心与直管圆心同轴，端面法向平行且端面贴合。",
        "",
    ]
    for case in geometry["cases"]:
        note_lines.append(f"{case['case_id']} / {int(case['angle_deg'])}°")
        note_lines.append(f"  弯头弯曲中心: {case['bend_center_mm']}")
        note_lines.append(f"  流路: {' -> '.join(case['flow_path']['sequence'])}")
        for run in case["runs"]:
            note_lines.append(
                "  "
                + f"{run['run_id']}: 切点={run['tangent_point_mm']}, 轴向={run['axis']}, "
                + f"matched_port={run['matched_port_id']}, center_distance_mm={run['center_distance_mm']}, "
                + f"face_parallel_cosine={run['face_parallel_cosine']}"
            )
        note_lines.append("")
    notes_path.write_text("\n".join(note_lines), encoding="utf-8")
    return parameters_path, notes_path


def main() -> int:
    """Build the DN250 45/90/180 standard elbow verification drawing."""

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
        model_center=(0.0, 700.0, 0.0),
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
