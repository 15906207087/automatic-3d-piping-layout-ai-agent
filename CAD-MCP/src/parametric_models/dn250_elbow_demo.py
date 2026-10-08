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
    build_orthogonal_elbow_connection,
    resolve_elbow_bend_radius,
)
from screen_capture import ScreenCapture

OUTPUT_TOPIC = "DN250弯头连接管"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
WHITE = 7


@dataclass(frozen=True)
class ElbowDemoParameters:
    """One reusable parameter bundle for the DN250 elbow demo."""

    dn: int = 250
    elbow_type: str = "LR"
    horizontal_length_mm: float = 1800.0
    vertical_length_mm: float = 1800.0
    elbow_rotation_deg: float = 90.0


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
    view_specs = [
        ("front", model_center, 2600.0, 3400.0, f"{OUTPUT_TOPIC}_front_preview_v{version}.png"),
        ("top", model_center, 2600.0, 3400.0, f"{OUTPUT_TOPIC}_top_preview_v{version}.png"),
        ("swiso", model_center, 3200.0, 4000.0, f"{OUTPUT_TOPIC}_iso_preview_v{version}.png"),
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


def build_demo_geometry(service: CADService, params: ElbowDemoParameters) -> dict[str, Any]:
    """Draw one DN250 horizontal pipe, one DN250 vertical pipe and one elbow."""

    controller = service.controller
    dn = int(params.dn)
    bend_radius = resolve_elbow_bend_radius(controller, dn, params.elbow_type)
    layer = f"PIPE_DN{dn}"
    elbow_center = (0.0, 0.0, 0.0)
    horizontal_tangent_point = (0.0, -bend_radius, 0.0)
    vertical_tangent_point = (0.0, 0.0, bend_radius)
    connection_geometry = build_orthogonal_elbow_connection(
        controller,
        dn=dn,
        elbow_center=elbow_center,
        elbow_type=params.elbow_type,
        run_specs=[
            PipeConnectionRunSpec(
                run_id="horizontal_pipe",
                tangent_point_mm=horizontal_tangent_point,
                axis_away_from_fitting="+y",
                length_mm=float(params.horizontal_length_mm),
                connection_end="left_end",
            ),
            PipeConnectionRunSpec(
                run_id="vertical_pipe",
                tangent_point_mm=vertical_tangent_point,
                axis_away_from_fitting="-z",
                length_mm=float(params.vertical_length_mm),
                connection_end="top_end",
            ),
        ],
        elbow_rotations=[
            RotationInstruction(
                angle_deg=float(params.elbow_rotation_deg),
                axis_direction=(0.0, 1.0, 0.0),
                axis_name="y",
            )
        ],
        layer=layer,
        color=WHITE,
    )

    if not zoom_extents_with_retry(controller):
        raise RuntimeError("缩放到全部对象失败。")

    runs_by_id = {item["run_id"]: item for item in connection_geometry["runs"]}
    return {
        **connection_geometry,
        "horizontal_pipe": runs_by_id["horizontal_pipe"],
        "vertical_pipe": runs_by_id["vertical_pipe"],
        "elbow_rotation": connection_geometry["elbow_rotations"][0] if connection_geometry["elbow_rotations"] else {},
        "verification_basis": "本轮通过公共端口链 helper 生成连接骨架：输入中心取两根直管中心线理论转角点；横管切点位于转角点沿 -y 方向一个弯曲半径处，并沿 +y 方向远离弯头延伸；竖管切点位于转角点沿 +z 方向一个弯曲半径处，并沿 -z 方向远离弯头延伸。",
    }


def write_version_artifacts(
    version_dir: Path,
    version: int,
    *,
    dwg_path: Path,
    preview_paths: list[str],
    geometry: dict[str, Any],
    params: ElbowDemoParameters,
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
                "parameters": asdict(params),
                "geometry": geometry,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    notes_path.write_text(
        "\n".join(
            [
                f"{OUTPUT_TOPIC} 绘制说明 v{version}",
                "",
                f"DWG: {dwg_path}",
                "本轮采用空心 DN250 直管 + 国标 LR 90° 弯头。",
                f"弯头弯曲半径 R = {geometry['bend_radius_mm']:.3f} mm。",
                f"横管切点: {geometry['horizontal_pipe']['tangent_point_mm']}",
                f"竖管上端切点: {geometry['vertical_pipe']['tangent_point_mm']}",
                f"理论转角点: {geometry['corner_point_mm']}",
                "校验要点: 输入中心是两根直管中心线理论转角点，不是弯头实体的弯曲中心；横管在转角点沿 -y 一个半径处与弯头相切并沿 +y 延伸，竖管在转角点沿 +z 一个半径处与弯头相切并沿 -z 延伸，无穿插、无断开。",
            ]
        ),
        encoding="utf-8",
    )
    return parameters_path, notes_path


def main() -> int:
    """Build the blank-DWG DN250 elbow demo and archive versioned outputs."""

    version, version_dir = prepare_version_dir(OUTPUT_ROOT_DIR)
    dwg_path = version_dir / f"{OUTPUT_TOPIC}_3D_v{version}.dwg"

    params = ElbowDemoParameters()
    service = CADService()
    create_fresh_document(service)
    geometry = build_demo_geometry(service, params)

    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    preview_paths = capture_previews(
        service,
        version_dir,
        version,
        model_center=(-700.0, 0.0, 900.0),
    )
    parameters_path, notes_path = write_version_artifacts(
        version_dir,
        version,
        dwg_path=dwg_path,
        preview_paths=preview_paths,
        geometry=geometry,
        params=params,
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
