from __future__ import annotations

import json
import re
import shutil
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
from cad_controller import CADController
OUTPUT_TOPIC = "循环水"
EQUIPMENT_TOPIC = "设备图"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC / EQUIPMENT_TOPIC
SHARED_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC / "shared"
LIST_INFO_PATH = SHARED_DIR / f"{OUTPUT_TOPIC}_list_info.json"
DIMENSION_INFO_PATH = SHARED_DIR / f"{OUTPUT_TOPIC}_dimension_info.json"
SECTION_INFO_PATH = SHARED_DIR / f"{OUTPUT_TOPIC}_section_info.json"

WHITE = 7
BASIN_WALL_THICKNESS_MM = 300.0
BASIN_BOTTOM_THICKNESS_MM = 300.0
DEFAULT_OPENING_PROXY_BAND_MM = 20.0
DEFAULT_OPENING_PROXY_HEIGHT_MM = 20.0


class CADService:
    """Lightweight local CAD service wrapper for scripted redraws.

    This keeps the equipment-model script runnable even when the full MCP
    server dependency stack is unavailable in the current Python environment.
    """

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

    def draw_flange(self, **kwargs: Any) -> Any:
        """Forward one flange draw request to the controller."""

        return self.controller.draw_flange(**kwargs)

    def draw_rigid_waterproof_sleeve(self, **kwargs: Any) -> Any:
        """Forward one rigid-waterproof-sleeve draw request to the controller."""

        return self.controller.draw_rigid_waterproof_sleeve(**kwargs)


def create_fresh_document(service: CADService) -> None:
    """Create a fresh drawing document for this scripted build."""

    if not service.start_cad():
        raise RuntimeError("无法启动或连接 CAD。")
    docs = getattr(service.controller.app, "Documents", None)
    if docs is None:
        raise RuntimeError("CAD 未暴露 Documents 集合，无法创建新文档。")
    docs.Add()
    # AutoCAD/COM often needs extra warm-up time before the first solids/layer calls.
    time.sleep(4.0)
    service.controller.doc = service.controller.app.ActiveDocument
    service.drawing_state["entities"] = []


def draw_box_with_retry(
    controller: Any,
    *,
    center: tuple[float, float, float],
    length: float,
    width: float,
    height: float,
    layer: str,
    color: int,
    retries: int = 6,
    wait_seconds: float = 1.2,
) -> Any:
    """Retry box creation because a new CAD document may reject the first COM call."""

    last_result = None
    for _ in range(retries):
        last_result = controller.draw_box(
            center=center,
            length=length,
            width=width,
            height=height,
            layer=layer,
            color=color,
        )
        if last_result is not None:
            return last_result
        time.sleep(wait_seconds)
    return last_result


def draw_text_with_retry(
    controller: Any,
    *,
    position: tuple[float, float, float],
    text: str,
    height: float,
    layer: str,
    color: int,
    retries: int = 5,
    wait_seconds: float = 0.8,
) -> Any:
    """Retry text creation because early COM calls can fail on a fresh document."""

    last_result = None
    for _ in range(retries):
        last_result = controller.draw_text(
            position=position,
            text=text,
            height=height,
            layer=layer,
            color=color,
        )
        if last_result is not None:
            return last_result
        time.sleep(wait_seconds)
    return last_result


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
    """Retry SaveAs because AutoCAD can temporarily reject the call."""

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
    """Retry named view switching because viewport updates can fail intermittently."""

    for _ in range(retries):
        try:
            if controller.set_named_view(view_name, target=target, center=(0.0, 0.0), height=height, width=width):
                time.sleep(wait_seconds)
                return True
        except Exception:
            pass
        time.sleep(wait_seconds)
    return False


def load_json(path: Path) -> dict[str, Any]:
    """Load one UTF-8 JSON file."""

    return json.loads(path.read_text(encoding="utf-8"))


def copy_if_missing(source: Path, destination: Path) -> None:
    """Copy one file when the target does not already exist."""

    if destination.exists() or not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def prepare_version_dir(output_root: Path) -> tuple[int, Path]:
    """Create the next version directory without overwriting prior outputs."""

    output_root.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(r"^v(\d+)$", re.IGNORECASE)
    versions: list[int] = []
    for item in output_root.iterdir():
        if item.is_dir():
            match = pattern.match(item.name)
            if match:
                versions.append(int(match.group(1)))
    next_version = max(versions, default=0) + 1
    version_dir = output_root / f"v{next_version}"
    version_dir.mkdir(parents=True, exist_ok=False)
    return next_version, version_dir


def collect_equipment_dimension_entries(dimension_info: dict[str, Any]) -> list[dict[str, Any]]:
    """Return page-level basin dimensions used to validate the equipment drawing."""

    entries: list[dict[str, Any]] = []
    for page in dimension_info.get("page_dimension_ledger", []):
        if str(page.get("page")) != "page_3":
            continue
        for entry in page.get("entries", []):
            entries.append(
                {
                    **entry,
                    "_ledger_scope": "page",
                    "_scope_name": "page_3",
                    "_view_name": str(page.get("view_name", "")),
                }
            )
    return entries


def draw_solid_box(
    service: CADService,
    *,
    center: tuple[float, float, float],
    length: float,
    width: float,
    height: float,
    layer: str,
) -> Any:
    """Draw one solid box and fail loudly when the geometry is not created."""

    entity = draw_box_with_retry(
        service.controller,
        center=center,
        length=length,
        width=width,
        height=height,
        layer=layer,
        color=WHITE,
    )
    if entity is None:
        raise RuntimeError(f"实体创建失败: {layer} @ {center}")
    return entity


def draw_line_with_retry(
    controller: Any,
    *,
    start_point: tuple[float, float, float],
    end_point: tuple[float, float, float],
    layer: str,
    color: int,
    retries: int = 5,
    wait_seconds: float = 0.6,
) -> Any:
    """Retry one line creation because CAD COM calls can fail intermittently."""

    last_result = None
    for _ in range(retries):
        last_result = controller.draw_line(
            start_point=start_point,
            end_point=end_point,
            layer=layer,
            color=color,
        )
        if last_result is not None:
            return last_result
        time.sleep(wait_seconds)
    return last_result


def draw_rectangle_with_retry(
    controller: Any,
    *,
    corner1: tuple[float, float, float],
    corner2: tuple[float, float, float],
    layer: str,
    color: int,
    retries: int = 5,
    wait_seconds: float = 0.6,
) -> Any:
    """Retry one rectangle creation because CAD COM calls can fail intermittently."""

    last_result = None
    for _ in range(retries):
        last_result = controller.draw_rectangle(
            corner1=corner1,
            corner2=corner2,
            layer=layer,
            color=color,
        )
        if last_result is not None:
            return last_result
        time.sleep(wait_seconds)
    return last_result


def draw_wireframe_box(
    service: CADService,
    *,
    center: tuple[float, float, float],
    length: float,
    width: float,
    height: float,
    layer: str,
) -> None:
    """Draw one rectangular prism as visible outline edges instead of a solid box."""

    cx, cy, cz = center
    x_min = cx - length / 2.0
    x_max = cx + length / 2.0
    y_min = cy - width / 2.0
    y_max = cy + width / 2.0
    z_min = cz - height / 2.0
    z_max = cz + height / 2.0

    bottom = draw_rectangle_with_retry(
        service.controller,
        corner1=(x_min, y_min, z_min),
        corner2=(x_max, y_max, z_min),
        layer=layer,
        color=WHITE,
    )
    if bottom is None:
        raise RuntimeError(f"线框底框创建失败: {layer} @ {center}")

    top = draw_rectangle_with_retry(
        service.controller,
        corner1=(x_min, y_min, z_max),
        corner2=(x_max, y_max, z_max),
        layer=layer,
        color=WHITE,
    )
    if top is None:
        raise RuntimeError(f"线框顶框创建失败: {layer} @ {center}")

    vertical_edges = [
        ((x_min, y_min, z_min), (x_min, y_min, z_max)),
        ((x_min, y_max, z_min), (x_min, y_max, z_max)),
        ((x_max, y_min, z_min), (x_max, y_min, z_max)),
        ((x_max, y_max, z_min), (x_max, y_max, z_max)),
    ]
    for start_point, end_point in vertical_edges:
        edge = draw_line_with_retry(
            service.controller,
            start_point=start_point,
            end_point=end_point,
            layer=layer,
            color=WHITE,
        )
        if edge is None:
            raise RuntimeError(f"线框立边创建失败: {layer} @ {center}")


def draw_hollow_partitioned_box(
    service: CADService,
    *,
    center: tuple[float, float, float],
    length: float,
    width: float,
    height: float,
    wall_thickness: float,
    separator_count: int,
    separator_thickness: float,
    layer: str,
) -> None:
    """Draw one hollow rectangular attachment with outer walls and internal separators."""

    cx, cy, cz = center
    half_length = length / 2.0
    half_width = width / 2.0

    draw_solid_box(
        service,
        center=(cx, cy - half_width + wall_thickness / 2.0, cz),
        length=length,
        width=wall_thickness,
        height=height,
        layer=layer,
    )
    draw_solid_box(
        service,
        center=(cx, cy + half_width - wall_thickness / 2.0, cz),
        length=length,
        width=wall_thickness,
        height=height,
        layer=layer,
    )

    inner_side_wall_span = max(0.0, width - 2.0 * wall_thickness)
    draw_solid_box(
        service,
        center=(cx - half_length + wall_thickness / 2.0, cy, cz),
        length=wall_thickness,
        width=inner_side_wall_span,
        height=height,
        layer=layer,
    )
    draw_solid_box(
        service,
        center=(cx + half_length - wall_thickness / 2.0, cy, cz),
        length=wall_thickness,
        width=inner_side_wall_span,
        height=height,
        layer=layer,
    )

    if separator_count <= 0:
        return
    clear_width = width - 2.0 * wall_thickness
    spacing = clear_width / float(separator_count + 1)
    lower_inner_edge_y = cy - clear_width / 2.0
    separator_length = max(0.0, length - 2.0 * wall_thickness)
    for index in range(separator_count):
        separator_center_y = lower_inner_edge_y + spacing * float(index + 1)
        draw_solid_box(
            service,
            center=(cx, separator_center_y, cz),
            length=separator_length,
            width=separator_thickness,
            height=height,
            layer=layer,
        )


def parse_elevation_marker_mm(raw_value: str) -> float:
    """Convert one section elevation marker like +1.900 or ±0.000 into millimeters."""

    normalized = str(raw_value).strip().replace("m", "").replace("M", "")
    if normalized.startswith("±"):
        normalized = normalized[1:]
    return float(normalized) * 1000.0


def resolve_basin_geometry(dimension_info: dict[str, Any], section_info: dict[str, Any]) -> dict[str, Any]:
    """Resolve basin footprint, wall thickness and depth from the current JSON inputs."""

    plan_reference = dimension_info.get("model_input", {}).get("plan_reference", {})
    basin_center = plan_reference.get("basin_outline_center") or [8750.0, 5000.0, 0.0]
    basin_length = float(plan_reference.get("basin_length", 17500.0))
    basin_width = float(plan_reference.get("basin_width", 10000.0))
    wall_thickness = float(plan_reference.get("basin_wall_thickness", BASIN_WALL_THICKNESS_MM))
    bottom_thickness = float(plan_reference.get("basin_bottom_thickness", BASIN_BOTTOM_THICKNESS_MM))

    if "basin_top_elevation" in plan_reference and "basin_bottom_elevation" in plan_reference:
        basin_top_mm = float(plan_reference["basin_top_elevation"])
        basin_bottom_mm = float(plan_reference["basin_bottom_elevation"])
    else:
        basin_top_mm = 1900.0
        basin_bottom_mm = 0.0
        for section in section_info.get("sections", []):
            if str(section.get("section")) != "B-B":
                continue
            for marker in section.get("key_readings", {}).get("elevation_markers_m", []):
                value_m = str(marker.get("value_m", "")).strip()
                if value_m in {"+1.900", "1.900"}:
                    basin_top_mm = parse_elevation_marker_mm(value_m)
                if value_m in {"±0.000", "+0.000", "0.000"}:
                    basin_bottom_mm = parse_elevation_marker_mm(value_m)

    basin_height = basin_top_mm - basin_bottom_mm
    basin_center_z = (basin_top_mm + basin_bottom_mm) / 2.0
    inner_length = basin_length - 2.0 * wall_thickness
    inner_width = basin_width - 2.0 * wall_thickness
    if inner_length <= 0 or inner_width <= 0:
        raise RuntimeError("池体内净尺寸无效，无法按壁厚生成池壁。")

    basin_cells: list[dict[str, Any]] = []
    for cell in plan_reference.get("basin_internal_cells", []):
        if not bool(cell.get("draw_in_equipment_model", True)):
            continue
        cell_size = cell.get("size_mm", [4200.0, 4200.0, basin_height])
        cell_wall_thickness = float(cell.get("wall_thickness_mm", wall_thickness))
        cell_bottom_thickness = float(cell.get("bottom_thickness_mm", bottom_thickness))
        basin_cells.append(
            {
                "cell_id": str(cell.get("cell_id", f"cell_{len(basin_cells) + 1}")),
                "center": [float(value) for value in cell.get("center", [0.0, 0.0, basin_center_z])],
                "length": float(cell_size[0]),
                "width": float(cell_size[1]),
                "height": float(cell_size[2]),
                "wall_thickness_mm": cell_wall_thickness,
                "bottom_thickness_mm": cell_bottom_thickness,
                "basis": str(cell.get("basis", "")),
            }
        )

    width_zones: list[dict[str, Any]] = []
    for zone in plan_reference.get("basin_width_zones", []):
        y_start = float(zone["y_start"])
        y_end = float(zone["y_end"])
        top_elevation = float(zone["top_elevation"])
        bottom_elevation = float(zone["bottom_elevation"])
        width_zones.append(
            {
                "zone_id": str(zone["zone_id"]),
                "y_start": y_start,
                "y_end": y_end,
                "center_y": (y_start + y_end) / 2.0,
                "width": y_end - y_start,
                "top_elevation": top_elevation,
                "bottom_elevation": bottom_elevation,
                "center_z": (top_elevation + bottom_elevation) / 2.0,
                "height": top_elevation - bottom_elevation,
                "wall_thickness_mm": float(zone.get("wall_thickness_mm", wall_thickness)),
                "bottom_thickness_mm": float(zone.get("bottom_thickness_mm", bottom_thickness)),
                "basis": str(zone.get("basis", "")),
            }
        )

    partition_gratings: list[dict[str, Any]] = []
    for grating in plan_reference.get("partition_wall_gratings", []):
        grating_size = grating.get("size_mm", [1400.0, 400.0, 30.0])
        partition_gratings.append(
            {
                "grating_id": str(grating.get("grating_id", f"grating_{len(partition_gratings) + 1}")),
                "center": [float(value) for value in grating.get("center", [0.0, 0.0, 0.0])],
                "length": float(grating_size[0]),
                "width": float(grating_size[1]),
                "height": float(grating_size[2]),
                "basis": str(grating.get("basis", "")),
            }
        )

    sump_pits: list[dict[str, Any]] = []
    for pit in plan_reference.get("sump_pits", []):
        pit_size = pit.get("size_mm", [1000.0, 1000.0, 1000.0])
        sump_pits.append(
            {
                "pit_id": str(pit.get("pit_id", f"sump_pit_{len(sump_pits) + 1}")),
                "center": [float(value) for value in pit.get("center", [0.0, 0.0, 0.0])],
                "length": float(pit_size[0]),
                "width": float(pit_size[1]),
                "height": float(pit_size[2]),
                "top_elevation_mm": float(pit.get("top_elevation_mm", -1400.0)),
                "bottom_elevation_mm": float(pit.get("bottom_elevation_mm", -2400.0)),
                "wall_thickness_mm": float(pit.get("wall_thickness_mm", wall_thickness)),
                "bottom_thickness_mm": float(pit.get("bottom_thickness_mm", bottom_thickness)),
                "host_zone_id": str(pit.get("host_zone_id", "main_basin_zone")),
                "shared_edges": [str(value) for value in pit.get("shared_edges", [])],
                "basis": str(pit.get("basis", "")),
            }
        )

    return {
        "center_x": float(basin_center[0]),
        "center_y": float(basin_center[1]),
        "center_z": basin_center_z,
        "length": basin_length,
        "width": basin_width,
        "height": basin_height,
        "bottom_elevation_mm": basin_bottom_mm,
        "top_elevation_mm": basin_top_mm,
        "wall_thickness_mm": wall_thickness,
        "bottom_thickness_mm": bottom_thickness,
        "inner_length": inner_length,
        "inner_width": inner_width,
        "width_zones": width_zones,
        "partition_gratings": partition_gratings,
        "sump_pits": sump_pits,
        "internal_cells": basin_cells,
    }


def draw_open_top_basin_component(
    service: CADService,
    *,
    center: tuple[float, float, float],
    length: float,
    width: float,
    height: float,
    wall_thickness: float,
    bottom_thickness: float,
    layer: str,
    draw_x_min_wall: bool = True,
    draw_x_max_wall: bool = True,
    draw_y_min_wall: bool = True,
    draw_y_max_wall: bool = True,
    floor_openings: list[dict[str, float]] | None = None,
) -> None:
    """Draw one open-top basin-like component with side walls and a solid bottom slab."""

    cx, cy, cz = center
    inner_length = length - ((wall_thickness if draw_x_min_wall else 0.0) + (wall_thickness if draw_x_max_wall else 0.0))
    side_wall_y_min = cy - width / 2.0 + (wall_thickness if draw_y_min_wall else 0.0)
    side_wall_y_max = cy + width / 2.0 - (wall_thickness if draw_y_max_wall else 0.0)
    side_wall_span = side_wall_y_max - side_wall_y_min
    if inner_length <= 0 or side_wall_span <= 0:
        raise RuntimeError(f"池格内净尺寸无效: {center}")

    wall_specs = []
    if draw_y_min_wall:
        wall_specs.append(((cx, cy - width / 2.0 + wall_thickness / 2.0, cz), length, wall_thickness, height))
    if draw_y_max_wall:
        wall_specs.append(((cx, cy + width / 2.0 - wall_thickness / 2.0, cz), length, wall_thickness, height))
    if draw_x_min_wall:
        wall_specs.append(
            (
                (cx - length / 2.0 + wall_thickness / 2.0, (side_wall_y_min + side_wall_y_max) / 2.0, cz),
                wall_thickness,
                side_wall_span,
                height,
            )
        )
    if draw_x_max_wall:
        wall_specs.append(
            (
                (cx + length / 2.0 - wall_thickness / 2.0, (side_wall_y_min + side_wall_y_max) / 2.0, cz),
                wall_thickness,
                side_wall_span,
                height,
            )
        )
    for center, wall_length, wall_width, wall_height in wall_specs:
        entity = draw_box_with_retry(
            service.controller,
            center=center,
            length=wall_length,
            width=wall_width,
            height=wall_height,
            layer=layer,
            color=WHITE,
        )
        if entity is None:
            raise RuntimeError(f"池壁创建失败: {center}")

    slab_center_z = cz - height / 2.0 + bottom_thickness / 2.0
    slab_rectangles: list[tuple[float, float, float, float]] = [
        (cx - length / 2.0, cx + length / 2.0, cy - width / 2.0, cy + width / 2.0)
    ]
    for opening in floor_openings or []:
        opening_x_min = max(float(opening["x_min"]), cx - length / 2.0)
        opening_x_max = min(float(opening["x_max"]), cx + length / 2.0)
        opening_y_min = max(float(opening["y_min"]), cy - width / 2.0)
        opening_y_max = min(float(opening["y_max"]), cy + width / 2.0)
        if opening_x_min >= opening_x_max or opening_y_min >= opening_y_max:
            continue
        next_rectangles: list[tuple[float, float, float, float]] = []
        for rect_x_min, rect_x_max, rect_y_min, rect_y_max in slab_rectangles:
            if (
                opening_x_max <= rect_x_min
                or opening_x_min >= rect_x_max
                or opening_y_max <= rect_y_min
                or opening_y_min >= rect_y_max
            ):
                next_rectangles.append((rect_x_min, rect_x_max, rect_y_min, rect_y_max))
                continue
            if opening_x_min > rect_x_min:
                next_rectangles.append((rect_x_min, opening_x_min, rect_y_min, rect_y_max))
            if opening_x_max < rect_x_max:
                next_rectangles.append((opening_x_max, rect_x_max, rect_y_min, rect_y_max))
            middle_x_min = max(rect_x_min, opening_x_min)
            middle_x_max = min(rect_x_max, opening_x_max)
            if middle_x_max > middle_x_min and opening_y_min > rect_y_min:
                next_rectangles.append((middle_x_min, middle_x_max, rect_y_min, opening_y_min))
            if middle_x_max > middle_x_min and opening_y_max < rect_y_max:
                next_rectangles.append((middle_x_min, middle_x_max, opening_y_max, rect_y_max))
        slab_rectangles = next_rectangles

    for rect_x_min, rect_x_max, rect_y_min, rect_y_max in slab_rectangles:
        rect_length = rect_x_max - rect_x_min
        rect_width = rect_y_max - rect_y_min
        if rect_length <= 0.0 or rect_width <= 0.0:
            continue
        slab = draw_box_with_retry(
            service.controller,
            center=((rect_x_min + rect_x_max) / 2.0, (rect_y_min + rect_y_max) / 2.0, slab_center_z),
            length=rect_length,
            width=rect_width,
            height=bottom_thickness,
            layer=layer,
            color=WHITE,
        )
        if slab is None:
            raise RuntimeError(f"池底板创建失败: {((rect_x_min + rect_x_max) / 2.0, (rect_y_min + rect_y_max) / 2.0, slab_center_z)}")


def draw_partition_wall_zone(service: CADService, basin_geometry: dict[str, Any], zone: dict[str, Any]) -> None:
    """Draw the 400-thick longitudinal partition wall from the A-A section."""

    draw_solid_box(
        service,
        center=(basin_geometry["center_x"], zone["center_y"], zone["center_z"]),
        length=basin_geometry["length"],
        width=zone["width"],
        height=zone["height"],
        layer="BASIN_WALL",
    )


def draw_partition_gratings(service: CADService, basin_geometry: dict[str, Any]) -> int:
    """Draw the five steel grating panels placed on the partition wall."""

    grating_count = 0
    for grating in basin_geometry.get("partition_gratings", []):
        center_x, center_y, center_z = (float(value) for value in grating["center"])
        grating_length = float(grating["length"])
        grating_width = float(grating["width"])
        grating_height = float(grating["height"])
        draw_solid_box(
            service,
            center=(center_x, center_y, center_z),
            length=grating_length,
            width=grating_width,
            height=grating_height,
            layer="GRATING",
        )
        # The plan symbol shows two inner transverse bars; draw them as thin
        # top ribs so the preview keeps the same grating reading as P3.
        rib_width = min(16.0, grating_width / 10.0)
        rib_height = max(8.0, grating_height)
        rib_z = center_z + rib_height / 2.0
        rib_offsets = (-grating_width / 6.0, grating_width / 6.0)
        for offset_y in rib_offsets:
            draw_solid_box(
                service,
                center=(center_x, center_y + offset_y, rib_z),
                length=grating_length,
                width=rib_width,
                height=rib_height,
                layer="GRATING",
            )
        grating_count += 1
    return grating_count


def draw_sump_pits(service: CADService, basin_geometry: dict[str, Any]) -> int:
    """Draw every local sump pit recorded in the basin geometry."""

    pit_count = 0
    for pit in basin_geometry.get("sump_pits", []):
        shared_edges = {str(value) for value in pit.get("shared_edges", [])}
        opening_wall_thickness = max(0.0, float(pit["wall_thickness_mm"]))
        display_wall_thickness = max(opening_wall_thickness, 1.0)
        draw_open_top_basin_component(
            service,
            center=tuple(pit["center"]),
            length=pit["length"],
            width=pit["width"],
            height=pit["height"],
            wall_thickness=display_wall_thickness,
            bottom_thickness=pit["bottom_thickness_mm"],
            layer="SUMP_PIT",
            draw_x_min_wall="left_with_pool_wall" not in shared_edges,
            draw_x_max_wall="right_with_pool_wall" not in shared_edges,
            draw_y_min_wall="bottom_with_pool_wall" not in shared_edges,
            draw_y_max_wall=not ({"top_with_partition_wall", "top_with_pool_wall"} & shared_edges),
        )
        pit_count += 1
    return pit_count


def draw_vertical_cylinder(
    service: CADService,
    *,
    base_center: tuple[float, float, float],
    radius: float,
    height: float,
    layer: str,
) -> Any:
    """Draw one vertical cylinder and apply the current white-by-layer style."""

    entity = service.controller._add_oriented_cylinder(base_center, radius, height, "z")
    if entity is None:
        raise RuntimeError(f"圆柱创建失败: {layer} @ {base_center}")
    try:
        entity.Layer = layer
    except Exception:
        pass
    try:
        entity.Color = WHITE
    except Exception:
        pass
    return entity


def draw_embedded_plates(service: CADService, dimension_info: dict[str, Any]) -> int:
    """Draw every pool-top embedded steel plate recorded in plan_reference."""

    plan_reference = dimension_info.get("model_input", {}).get("plan_reference", {})
    plate_count = 0
    for plate in plan_reference.get("embedded_plates", []):
        size_mm = [float(value) for value in plate.get("size_mm", [300.0, 300.0, 10.0])]
        draw_solid_box(
            service,
            center=tuple(float(value) for value in plate.get("center", [0.0, 0.0, 0.0])),
            length=size_mm[0],
            width=size_mm[1],
            height=size_mm[2],
            layer="EMBEDDED_PLATE",
        )
        plate_count += 1
    return plate_count


def draw_top_flange_pipes(service: CADService, dimension_info: dict[str, Any]) -> int:
    """Draw pool-top vertical flange pipe proxies from plan_reference.top_flange_pipes."""

    plan_reference = dimension_info.get("model_input", {}).get("plan_reference", {})
    pipe_count = 0
    for pipe in plan_reference.get("top_flange_pipes", []):
        center = [float(value) for value in pipe.get("center", [0.0, 0.0, 0.0])]
        outer_diameter = float(pipe.get("outer_diameter_mm", 60.3))
        pipe_radius = outer_diameter / 2.0
        pipe_height = float(pipe.get("pipe_height_mm", 100.0))
        draw_vertical_cylinder(
            service,
            base_center=(center[0], center[1], center[2]),
            radius=pipe_radius,
            height=pipe_height,
            layer="TOP_PIPE",
        )

        flange_proxy = pipe.get("flange_proxy") or {}
        if flange_proxy:
            flange = service.draw_flange(
                center=(center[0], center[1], center[2]),
                outer_diameter=float(flange_proxy["outer_diameter_mm"]),
                center_hole_diameter=float(flange_proxy["center_hole_diameter_mm"]),
                bolt_circle_diameter=float(flange_proxy["bolt_circle_diameter_mm"]),
                bolt_hole_diameter=float(flange_proxy["bolt_hole_diameter_mm"]),
                bolt_count=int(flange_proxy["bolt_count"]),
                thickness=float(flange_proxy["thickness_mm"]),
                layer="TOP_PIPE",
                color=WHITE,
            )
            if flange is None:
                raise RuntimeError(f"池顶法兰钢管代理法兰创建失败: {pipe.get('pipe_id', '<unknown>')}")
        pipe_count += 1
    return pipe_count


def draw_top_opening_proxies(service: CADService, dimension_info: dict[str, Any]) -> int:
    """Draw ring proxies for pool-top openings when the current model has no full roof slab."""

    plan_reference = dimension_info.get("model_input", {}).get("plan_reference", {})
    opening_count = 0
    for opening in plan_reference.get("top_openings", []):
        center = [float(value) for value in opening.get("center", [0.0, 0.0, 0.0])]
        hole_radius = float(opening.get("diameter_mm", 300.0)) / 2.0
        ring_band = float(opening.get("proxy_ring_band_mm", DEFAULT_OPENING_PROXY_BAND_MM))
        ring_height = float(opening.get("proxy_height_mm", DEFAULT_OPENING_PROXY_HEIGHT_MM))
        ring = draw_vertical_cylinder(
            service,
            base_center=(center[0], center[1], center[2]),
            radius=hole_radius + ring_band,
            height=ring_height,
            layer="OPENING_PROXY",
        )
        inner_cut = service.controller._add_oriented_cylinder(
            (center[0], center[1], center[2] - 1.0),
            hole_radius,
            ring_height + 2.0,
            "z",
        )
        if inner_cut is None:
            raise RuntimeError(f"池顶开孔代理内切实体创建失败: {opening.get('opening_id', '<unknown>')}")
        if not solid_boolean_subtract_with_retry(service, ring, inner_cut):
            raise RuntimeError(f"池顶开孔代理布尔减失败: {opening.get('opening_id', '<unknown>')}")
        try:
            inner_cut.Delete()
        except Exception:
            pass
        opening_count += 1
    return opening_count


def draw_cooling_tower(service: CADService, tower: dict[str, Any], tower_template: dict[str, Any]) -> None:
    """Draw one DFDNP-300 cooling tower using the P5 top view and P6 A-A front view proxy."""

    center_x = float(tower["center"][0])
    center_y = float(tower["center"][1])
    base_elevation = float(tower_template["base_elevation_mm"])
    lower_height = float(tower_template["lower_body_height_mm"])
    louver_height = float(tower_template["louver_band_height_mm"])
    upper_height = float(tower_template["upper_body_height_mm"])
    top_cap_height = float(tower_template["top_cap_height_mm"])
    body_plan = [float(value) for value in tower_template["body_plan_size_mm"]]
    louver_plan = [float(value) for value in tower_template["louver_band_size_mm"]]
    top_cap_plan = [float(value) for value in tower_template["top_cap_size_mm"]]
    fan_cowl_radius = float(tower_template["fan_cowl_diameter_mm"]) / 2.0
    fan_cowl_height = float(tower_template["fan_cowl_height_mm"])
    fan_vent_radius = float(tower_template.get("fan_vent_diameter_mm", 180.0)) / 2.0
    fan_vent_height = float(tower_template.get("fan_vent_height_mm", 90.0))
    louver_slat_count = int(tower_template.get("louver_slat_count", 0))
    louver_slat_thickness = float(tower_template.get("louver_slat_thickness_mm", 30.0))
    upper_panel_rib_count = int(tower_template.get("upper_panel_rib_count", 0))
    upper_panel_rib_thickness = float(tower_template.get("upper_panel_rib_thickness_mm", 30.0))

    draw_solid_box(
        service,
        center=(center_x, center_y, base_elevation + lower_height / 2.0),
        length=body_plan[0],
        width=body_plan[1],
        height=lower_height,
        layer="COOLING_TOWER",
    )
    draw_solid_box(
        service,
        center=(center_x, center_y, base_elevation + lower_height + louver_height / 2.0),
        length=louver_plan[0],
        width=louver_plan[1],
        height=louver_height,
        layer="COOLING_TOWER",
    )
    if louver_slat_count > 0:
        slat_spacing = louver_height / float(louver_slat_count + 1)
        for index in range(louver_slat_count):
            slat_center_z = base_elevation + lower_height + slat_spacing * float(index + 1)
            draw_solid_box(
                service,
                center=(center_x, center_y, slat_center_z),
                length=louver_plan[0],
                width=louver_plan[1],
                height=louver_slat_thickness,
                layer="COOLING_TOWER",
            )
    draw_solid_box(
        service,
        center=(center_x, center_y, base_elevation + lower_height + louver_height + upper_height / 2.0),
        length=body_plan[0],
        width=body_plan[1],
        height=upper_height,
        layer="COOLING_TOWER",
    )
    if upper_panel_rib_count > 1:
        rib_spacing = body_plan[0] / float(upper_panel_rib_count + 1)
        left_edge_x = center_x - body_plan[0] / 2.0
        for index in range(upper_panel_rib_count):
            rib_center_x = left_edge_x + rib_spacing * float(index + 1)
            draw_solid_box(
                service,
                center=(rib_center_x, center_y, base_elevation + lower_height + louver_height + upper_height / 2.0),
                length=upper_panel_rib_thickness,
                width=body_plan[1],
                height=upper_height,
                layer="COOLING_TOWER",
            )
    draw_solid_box(
        service,
        center=(center_x, center_y, base_elevation + lower_height + louver_height + upper_height + top_cap_height / 2.0),
        length=top_cap_plan[0],
        width=top_cap_plan[1],
        height=top_cap_height,
        layer="COOLING_TOWER",
    )
    draw_vertical_cylinder(
        service,
        base_center=(
            center_x,
            center_y,
            base_elevation + lower_height + louver_height + upper_height + top_cap_height,
        ),
        radius=fan_cowl_radius,
        height=fan_cowl_height,
        layer="COOLING_TOWER",
    )
    draw_vertical_cylinder(
        service,
        base_center=(
            center_x,
            center_y,
            base_elevation + lower_height + louver_height + upper_height + top_cap_height + fan_cowl_height,
        ),
        radius=fan_vent_radius,
        height=fan_vent_height,
        layer="COOLING_TOWER",
    )


def draw_auxiliary_equipment(service: CADService, equipment: dict[str, Any]) -> None:
    """Draw one auxiliary-equipment proxy: box by default, or vertical tank when requested."""

    center_xy = [float(value) for value in equipment.get("center_xy", [0.0, 0.0])]
    shape = str(equipment.get("shape", "box")).strip().lower()
    bottom_elevation = float(equipment.get("bottom_elevation_mm", 0.0))

    if shape in {"vertical_cylinder", "vertical_tank", "tank"}:
        outer_diameter = float(
            equipment.get(
                "outer_diameter_mm",
                max(float(value) for value in equipment.get("plan_size_mm", [1000.0, 1000.0])),
            )
        )
        height = float(
            equipment.get(
                "height_mm",
                equipment.get("proxy_height_mm", max(outer_diameter, 1000.0)),
            )
        )
        wall_thickness = float(equipment.get("wall_thickness_mm", 10.0))
        bottom_cap = bool(equipment.get("bottom_cap", True))
        top_cap = bool(equipment.get("top_cap", False))
        outer_radius = outer_diameter / 2.0
        base_center = (center_xy[0], center_xy[1], bottom_elevation)
        # Open-top hollow shell: keep a solid bottom by cutting the bore only above the floor.
        bottom_thickness = min(20.0, max(8.0, wall_thickness * 2.0)) if bottom_cap else 0.0
        if wall_thickness > 0 and height - bottom_thickness > 1e-6:
            outer = service.controller._add_oriented_cylinder(base_center, outer_radius, height, "z")
            if outer is None:
                raise RuntimeError(f"立式罐外壳创建失败: {equipment.get('tag', '<unknown>')}")
            inner_height = height - bottom_thickness + 2.0
            inner_base = (
                center_xy[0],
                center_xy[1],
                bottom_elevation + bottom_thickness - 1.0,
            )
            inner = service.controller._add_oriented_cylinder(
                inner_base,
                max(outer_radius - wall_thickness, 1.0),
                inner_height,
                "z",
            )
            if inner is None:
                raise RuntimeError(f"立式罐内孔创建失败: {equipment.get('tag', '<unknown>')}")
            if not solid_boolean_subtract_with_retry(service, outer, inner):
                raise RuntimeError(f"立式罐掏空失败: {equipment.get('tag', '<unknown>')}")
            entity = outer
        else:
            entity = service.controller._add_oriented_hollow_cylinder(
                base_center,
                outer_radius,
                height,
                "z",
                wall_thickness=wall_thickness,
            )
            if entity is None:
                raise RuntimeError(f"立式罐创建失败: {equipment.get('tag', '<unknown>')}")
        try:
            entity.Layer = "AUX_EQUIPMENT"
        except Exception:
            pass
        try:
            entity.Color = WHITE
        except Exception:
            pass

        # Optional discrete caps. Default: solid bottom only, open top.
        lid_thickness = min(20.0, max(8.0, wall_thickness * 2.0))
        lid_levels: list[float] = []
        if bottom_cap and bottom_thickness <= 0:
            lid_levels.append(bottom_elevation)
        if top_cap:
            lid_levels.append(bottom_elevation + height - lid_thickness)
        for lid_z in lid_levels:
            lid = service.controller._add_oriented_cylinder(
                (center_xy[0], center_xy[1], lid_z),
                outer_radius,
                lid_thickness,
                "z",
            )
            if lid is None:
                raise RuntimeError(f"立式罐端盖创建失败: {equipment.get('tag', '<unknown>')}")
            try:
                lid.Layer = "AUX_EQUIPMENT"
            except Exception:
                pass
            try:
                lid.Color = WHITE
            except Exception:
                pass

        if not bool(equipment.get("show_label", False)):
            return
        tag = str(equipment.get("tag", "")).strip()
        if not tag:
            return
        text_height = float(equipment.get("label_height_mm", max(250.0, outer_diameter * 0.15)))
        text_width_estimate = max(text_height, len(tag) * text_height * 0.6)
        text_position = (
            center_xy[0] - text_width_estimate / 2.0,
            center_xy[1] - text_height / 2.0,
            bottom_elevation + height + 10.0,
        )
        text_obj = draw_text_with_retry(
            service.controller,
            position=text_position,
            text=tag,
            height=text_height,
            layer="AUX_EQUIPMENT_TEXT",
            color=WHITE,
        )
        if text_obj is None:
            raise RuntimeError(f"辅助设备文字创建失败: {tag}")
        return

    plan_size = [float(value) for value in equipment.get("plan_size_mm", [1000.0, 1000.0])]
    proxy_height = float(equipment.get("proxy_height_mm", max(plan_size, default=1000.0)))
    center_z = bottom_elevation + proxy_height / 2.0
    draw_solid_box(
        service,
        center=(center_xy[0], center_xy[1], center_z),
        length=plan_size[0],
        width=plan_size[1],
        height=proxy_height,
        layer="AUX_EQUIPMENT",
    )

    left_attachment = equipment.get("left_attachment")
    if isinstance(left_attachment, dict):
        attachment_length = float(left_attachment.get("length_mm", 500.0))
        attachment_width = float(left_attachment.get("width_mm", plan_size[1] * 0.6))
        attachment_center_x = center_xy[0] - plan_size[0] / 2.0 - attachment_length / 2.0
        attachment_center_y = center_xy[1] + float(left_attachment.get("center_offset_y_mm", 0.0))
        render_mode = str(left_attachment.get("render_mode", "wireframe")).strip().lower()
        separator_count = int(left_attachment.get("separator_count", 0))
        separator_thickness = float(left_attachment.get("separator_thickness_mm", 40.0))
        if render_mode == "hollow":
            draw_hollow_partitioned_box(
                service,
                center=(attachment_center_x, attachment_center_y, center_z),
                length=attachment_length,
                width=attachment_width,
                height=proxy_height,
                wall_thickness=float(left_attachment.get("wall_thickness_mm", 40.0)),
                separator_count=separator_count,
                separator_thickness=separator_thickness,
                layer="AUX_EQUIPMENT",
            )
        elif render_mode == "wireframe":
            draw_wireframe_box(
                service,
                center=(attachment_center_x, attachment_center_y, center_z),
                length=attachment_length,
                width=attachment_width,
                height=proxy_height,
                layer="AUX_EQUIPMENT",
            )
        else:
            draw_solid_box(
                service,
                center=(attachment_center_x, attachment_center_y, center_z),
                length=attachment_length,
                width=attachment_width,
                height=proxy_height,
                layer="AUX_EQUIPMENT",
            )

        if render_mode == "wireframe" and separator_count > 0:
            spacing = attachment_width / float(separator_count + 1)
            bottom_edge_y = attachment_center_y - attachment_width / 2.0
            for index in range(separator_count):
                separator_center_y = bottom_edge_y + spacing * float(index + 1)
                separator = draw_line_with_retry(
                    service.controller,
                    start_point=(
                        attachment_center_x - attachment_length / 2.0,
                        separator_center_y,
                        center_z + proxy_height / 2.0,
                    ),
                    end_point=(
                        attachment_center_x + attachment_length / 2.0,
                        separator_center_y,
                        center_z + proxy_height / 2.0,
                    ),
                    layer="AUX_EQUIPMENT",
                    color=WHITE,
                )
                if separator is None:
                    raise RuntimeError(f"附属段分隔线创建失败: {equipment.get('tag', '<unknown>')}")

    if not bool(equipment.get("show_label", False)):
        return

    tag = str(equipment.get("tag", "")).strip()
    if not tag:
        return
    text_height = float(equipment.get("label_height_mm", max(250.0, min(plan_size) * 0.2)))
    text_width_estimate = max(text_height, len(tag) * text_height * 0.6)
    text_position = (
        center_xy[0] - text_width_estimate / 2.0,
        center_xy[1] - text_height / 2.0,
        bottom_elevation + proxy_height + 10.0,
    )
    text_obj = draw_text_with_retry(
        service.controller,
        position=text_position,
        text=tag,
        height=text_height,
        layer="AUX_EQUIPMENT_TEXT",
        color=WHITE,
    )
    if text_obj is None:
        raise RuntimeError(f"辅助设备文字创建失败: {tag}")


def draw_basin_structure(service: CADService, basin_geometry: dict[str, Any]) -> tuple[int, int, int]:
    """Draw the basin zones, local pits, inner cells and partition gratings from the JSON inputs."""

    for zone in basin_geometry.get("width_zones", []):
        if zone["zone_id"] == "partition_wall_zone":
            draw_partition_wall_zone(service, basin_geometry, zone)
            continue
        draw_y_min_wall = True
        draw_y_max_wall = True
        floor_openings: list[dict[str, float]] = []
        if zone["zone_id"] == "main_basin_zone":
            draw_y_max_wall = False
        elif zone["zone_id"] == "upper_deep_basin_zone":
            draw_y_min_wall = False
        for pit in basin_geometry.get("sump_pits", []):
            if pit.get("host_zone_id") != zone["zone_id"]:
                continue
            shared_edges = {str(value) for value in pit.get("shared_edges", [])}
            pit_center_x = float(pit["center"][0])
            pit_center_y = float(pit["center"][1])
            pit_length = float(pit["length"])
            pit_width = float(pit["width"])
            opening_wall_thickness = max(0.0, float(pit["wall_thickness_mm"]))
            opening_x_min = pit_center_x - pit_length / 2.0 + (0.0 if "left_with_pool_wall" in shared_edges else opening_wall_thickness)
            opening_x_max = pit_center_x + pit_length / 2.0 - (0.0 if "right_with_pool_wall" in shared_edges else opening_wall_thickness)
            opening_y_min = pit_center_y - pit_width / 2.0 + (0.0 if "bottom_with_pool_wall" in shared_edges else opening_wall_thickness)
            opening_y_max = pit_center_y + pit_width / 2.0 - (0.0 if ({"top_with_partition_wall", "top_with_pool_wall"} & shared_edges) else opening_wall_thickness)
            floor_openings.append(
                {
                    "x_min": opening_x_min,
                    "x_max": opening_x_max,
                    "y_min": opening_y_min,
                    "y_max": opening_y_max,
                }
            )
        draw_open_top_basin_component(
            service,
            center=(basin_geometry["center_x"], zone["center_y"], zone["center_z"]),
            length=basin_geometry["length"],
            width=zone["width"],
            height=zone["height"],
            wall_thickness=zone["wall_thickness_mm"],
            bottom_thickness=zone["bottom_thickness_mm"],
            layer="BASIN",
            draw_y_min_wall=draw_y_min_wall,
            draw_y_max_wall=draw_y_max_wall,
            floor_openings=floor_openings,
        )
    cell_count = 0
    for cell in basin_geometry.get("internal_cells", []):
        draw_open_top_basin_component(
            service,
            center=tuple(cell["center"]),
            length=cell["length"],
            width=cell["width"],
            height=cell["height"],
            wall_thickness=cell["wall_thickness_mm"],
            bottom_thickness=cell["bottom_thickness_mm"],
            layer="BASIN_CELL",
        )
        cell_count += 1
    sump_pit_count = draw_sump_pits(service, basin_geometry)
    grating_count = draw_partition_gratings(service, basin_geometry)
    return cell_count, grating_count, sump_pit_count


def draw_wall_sleeves(service: CADService, dimension_info: dict[str, Any]) -> int:
    """Draw every rigid waterproof sleeve recorded in plan_reference.wall_sleeves."""

    plan_reference = dimension_info.get("model_input", {}).get("plan_reference", {})
    sleeve_count = 0
    for sleeve in plan_reference.get("wall_sleeves", []):
        center = tuple(float(value) for value in sleeve.get("center", [0.0, 0.0, 0.0]))
        dn = int(sleeve["dn"])
        axis = str(sleeve.get("axis", "x"))
        length = sleeve.get("length_mm")
        result = service.draw_rigid_waterproof_sleeve(
            center=center,
            dn=dn,
            axis=axis,
            sleeve_type="A",
            length=float(length) if length is not None else None,
            layer="SLEEVE",
            color=WHITE,
        )
        if result is None:
            raise RuntimeError(f"刚性防水套管创建失败: {sleeve.get('sleeve_id', '<unknown>')}")
        sleeve_count += 1
    return sleeve_count


def solid_boolean_subtract_with_retry(
    service: CADService,
    main_solid: Any,
    tool_solid: Any,
    *,
    attempts: int = 5,
    initial_delay_s: float = 0.2,
) -> bool:
    """Retry boolean subtraction because CAD COM often rejects rapid consecutive calls."""

    delay_s = initial_delay_s
    for _ in range(attempts):
        if service.controller._solid_boolean_subtract(main_solid, tool_solid):
            return True
        time.sleep(delay_s)
        delay_s *= 1.8
    return False


def draw_pump_foundation_with_holes(
    service: CADService,
    pump: dict[str, Any],
    pump_template: dict[str, Any],
) -> int:
    """Draw one pump foundation and subtract the six reserved holes from the P3 detail."""

    foundation_center_xy = (
        float(pump["center"][0]),
        float(pump["center"][1]),
    )
    foundation_length = float(pump["size_mm"][0])
    foundation_width = float(pump["size_mm"][1])
    top_elevation = float(pump.get("top_elevation_mm", pump_template.get("top_elevation_mm", 150.0)))
    bottom_elevation = float(pump.get("bottom_elevation_mm", pump_template.get("bottom_elevation_mm", top_elevation - float(pump["size_mm"][2]))))
    foundation_height = top_elevation - bottom_elevation
    foundation_center = (
        foundation_center_xy[0],
        foundation_center_xy[1],
        (top_elevation + bottom_elevation) / 2.0,
    )
    foundation = draw_solid_box(
        service,
        center=foundation_center,
        length=foundation_length,
        width=foundation_width,
        height=foundation_height,
        layer="PUMP",
    )

    holes = pump_template.get("preformed_holes", {})
    column_centers = [float(value) for value in holes.get("column_center_x_from_left_mm", [])]
    row_centers = [float(value) for value in holes.get("row_center_y_from_bottom_mm", [])]
    opening_plan_size = [float(value) for value in holes.get("opening_plan_size_mm", [130.0, 130.0])]
    opening_depth = float(holes.get("opening_depth_mm", foundation_height))
    if not column_centers or not row_centers:
        return 0

    left_x = foundation_center[0] - foundation_length / 2.0
    bottom_y = foundation_center[1] - foundation_width / 2.0
    hole_length = opening_plan_size[0]
    hole_width = opening_plan_size[1]
    hole_height = opening_depth + 20.0
    hole_center_z = top_elevation - opening_depth / 2.0

    hole_count = 0
    for column_center in column_centers:
        for row_center in row_centers:
            tool_center = (
                left_x + column_center,
                bottom_y + row_center,
                hole_center_z,
            )
            tool = draw_solid_box(
                service,
                center=tool_center,
                length=hole_length,
                width=hole_width,
                height=hole_height,
                layer="PUMP",
            )
            if not solid_boolean_subtract_with_retry(service, foundation, tool):
                raise RuntimeError(f"泵基础预留洞布尔减失败: {pump.get('tag', 'unknown')} @ {tool_center}")
            try:
                tool.Delete()
            except Exception:
                pass
            time.sleep(0.15)
            hole_count += 1
    return hole_count


def build_equipment_model(
    service: CADService,
    dimension_info: dict[str, Any],
    section_info: dict[str, Any],
) -> tuple[dict[str, int], dict[str, Any]]:
    """Draw the basin structure and pump row from the current JSON inputs."""

    model_input = dimension_info.get("model_input", {})
    equipment_centers = model_input.get("equipment_centers", {})
    basin_count = 0
    cooling_tower_count = 0
    pump_count = 0
    pump_opening_count = 0
    sleeve_count = 0
    sump_pit_count = 0
    embedded_plate_count = 0
    top_pipe_count = 0
    top_opening_count = 0
    auxiliary_equipment_count = 0

    basin_geometry = resolve_basin_geometry(dimension_info, section_info)
    basin_cell_count, grating_count, sump_pit_count = draw_basin_structure(service, basin_geometry)
    basin_count += 1
    embedded_plate_count += draw_embedded_plates(service, dimension_info)
    top_pipe_count += draw_top_flange_pipes(service, dimension_info)
    top_opening_count += draw_top_opening_proxies(service, dimension_info)

    cooling_tower_template = equipment_centers.get("cooling_tower_template", {})
    for tower in equipment_centers.get("cooling_towers", []):
        draw_cooling_tower(service, tower, cooling_tower_template)
        cooling_tower_count += 1

    pump_template = equipment_centers.get("pump_foundation_template", {})
    pump_body_template = equipment_centers.get("vertical_inline_pump_template", {})
    pump_body_enabled = bool(pump_body_template.get("enabled", False))
    complete_pump_count = 0
    local_pump_spec: dict[str, Any] = {}
    pump_body: dict[str, Any] = {}
    draw_vertical_inline_pump = None
    if pump_body_enabled:
        # Lazy import avoids circular import with cycle_water_pump_complete_local.
        from parametric_models.cycle_water_pump_complete_local import (
            build_pump_local_spec,
            draw_vertical_inline_pump as _draw_vertical_inline_pump,
            resolve_aa_pump_connection_placements,
        )

        draw_vertical_inline_pump = _draw_vertical_inline_pump
        local_pump_spec = build_pump_local_spec()
        pump_body = dict(local_pump_spec.get("pump", {}))
        pump_body["suction_dn"] = int(pump_body_template.get("suction_dn", pump_body.get("suction_dn", 250)))
        pump_body["discharge_dn"] = int(
            pump_body_template.get("discharge_dn", pump_body.get("discharge_dn", 150))
        )
        aa_placements = (
            resolve_aa_pump_connection_placements(dimension_info)
            if bool(pump_body_template.get("place_by_aa_item_25_26", True))
            else {}
        )
    else:
        aa_placements = {}
    for pump in equipment_centers.get("pumps", []):
        pump_opening_count += draw_pump_foundation_with_holes(service, pump, pump_template)
        pump_count += 1
        if pump_body_enabled and draw_vertical_inline_pump is not None:
            tag = str(pump.get("tag", ""))
            placement = aa_placements.get(tag)
            top_elevation = float(pump.get("top_elevation_mm", pump_template.get("top_elevation_mm", 150.0)))
            if placement is not None:
                origin_xy = (float(placement["origin_xy"][0]), float(placement["origin_xy"][1]))
                face_to_face = float(placement["face_to_face_mm"])
                suction_z = float(placement["suction_port_z_mm"])
                discharge_z = float(placement["discharge_port_z_mm"])
                port_z = (suction_z + discharge_z) / 2.0
                pump_body["suction_dn"] = int(placement["suction_dn"])
                pump_body["discharge_dn"] = int(placement["discharge_dn"])
            else:
                origin_xy = (float(pump["center"][0]), float(pump["center"][1]))
                face_to_face = float(pump_body_template.get("face_to_face_mm", 1000.0))
                suction_z = float(pump_body_template.get("item_25_pump_face_elevation_mm", 535.0))
                discharge_z = float(pump_body_template.get("item_26_pump_face_elevation_mm", 510.0))
                port_z = float(pump_body_template.get("port_center_elevation_mm", (suction_z + discharge_z) / 2.0))
            draw_vertical_inline_pump(
                service,
                origin_xy=origin_xy,
                base_top_z=top_elevation,
                port_center_z=port_z,
                flow_axis=str(pump_body_template.get("flow_axis", "y")),
                face_to_face_mm=face_to_face,
                pump=pump_body,
                include_base_plate=bool(pump_body_template.get("include_base_plate", False)),
                include_reducers=bool(pump_body_template.get("include_reducers", False)),
                include_stubs=bool(pump_body_template.get("include_stubs", False)),
                item_25=local_pump_spec.get("item_25"),
                item_26=local_pump_spec.get("item_26"),
                tag=tag,
                suction_port_z=suction_z,
                discharge_port_z=discharge_z,
            )
            complete_pump_count += 1

    for equipment in equipment_centers.get("auxiliary_equipments", []):
        draw_auxiliary_equipment(service, equipment)
        auxiliary_equipment_count += 1

    sleeve_count += draw_wall_sleeves(service, dimension_info)

    return (
        {
            "basins": basin_count,
            "basin_cells": basin_cell_count,
            "partition_gratings": grating_count,
            "cooling_towers": cooling_tower_count,
            "pumps": pump_count,
            "complete_pumps": complete_pump_count,
            "pump_openings": pump_opening_count,
            "wall_sleeves": sleeve_count,
            "sump_pits": sump_pit_count,
            "embedded_plates": embedded_plate_count,
            "top_flange_pipes": top_pipe_count,
            "top_openings": top_opening_count,
            "auxiliary_equipments": auxiliary_equipment_count,
        },
        basin_geometry,
    )


def add_equipment_dimensions(service: CADService, dimension_info: dict[str, Any]) -> set[str]:
    """Keep this drawing unannotated per the latest user requirement."""

    _ = service, dimension_info
    return set()


def build_closure_report(
    list_info: dict[str, Any],
    dimension_info: dict[str, Any],
    section_info: dict[str, Any],
    object_counts: dict[str, int],
    basin_geometry: dict[str, Any],
    used_dimension_labels: set[str],
    dimension_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a small closure report for the equipment-only validation run."""

    plan_reference = dimension_info.get("model_input", {}).get("plan_reference", {})
    equipment_centers = dimension_info.get("model_input", {}).get("equipment_centers", {})
    expected_basin = 1 if plan_reference.get("basin_outline_center") else 0
    expected_basin_cells = sum(
        1
        for cell in plan_reference.get("basin_internal_cells", [])
        if bool(cell.get("draw_in_equipment_model", True))
    )
    expected_partition_gratings = len(plan_reference.get("partition_wall_gratings", []))
    expected_cooling_towers = len(equipment_centers.get("cooling_towers", []))
    expected_pumps = len(equipment_centers.get("pumps", []))
    expected_wall_sleeves = len(plan_reference.get("wall_sleeves", []))
    expected_sump_pits = len(plan_reference.get("sump_pits", []))
    expected_embedded_plates = len(plan_reference.get("embedded_plates", []))
    expected_top_flange_pipes = len(plan_reference.get("top_flange_pipes", []))
    expected_top_openings = len(plan_reference.get("top_openings", []))
    expected_auxiliary_equipments = len(equipment_centers.get("auxiliary_equipments", []))
    hole_template = equipment_centers.get("pump_foundation_template", {}).get("preformed_holes", {})
    expected_pump_openings = expected_pumps * int(hole_template.get("count", 0))

    dimension_results = []
    for entry in dimension_entries:
        label = str(entry.get("label", ""))
        dimension_results.append(
            {
                "label": label,
                "result": "drawn_as_dimension" if label in used_dimension_labels else "loaded_from_json",
                "scope": entry.get("_ledger_scope"),
                "scope_name": entry.get("_scope_name"),
                "dimension_type": entry.get("dimension_type"),
                "raw_text": entry.get("raw_text"),
            }
        )

    return {
        "topic": OUTPUT_TOPIC,
        "drawing_kind": EQUIPMENT_TOPIC,
        "source_pdf": list_info.get("source_pdf") or dimension_info.get("source_pdf") or section_info.get("source_pdf"),
        "geometry_closure": {
            "expected": {
                "basins": expected_basin,
                "basin_cells": expected_basin_cells,
                "partition_gratings": expected_partition_gratings,
                "cooling_towers": expected_cooling_towers,
                "pumps": expected_pumps,
                "pump_openings": expected_pump_openings,
                "wall_sleeves": expected_wall_sleeves,
                "sump_pits": expected_sump_pits,
                "embedded_plates": expected_embedded_plates,
                "top_flange_pipes": expected_top_flange_pipes,
                "top_openings": expected_top_openings,
                "auxiliary_equipments": expected_auxiliary_equipments,
            },
            "drawn": object_counts,
            "basin_geometry": basin_geometry,
        },
        "dimension_closure": dimension_results,
        "summary": {
            "drawn_basins": object_counts["basins"],
            "drawn_basin_cells": object_counts["basin_cells"],
            "drawn_partition_gratings": object_counts["partition_gratings"],
            "drawn_cooling_towers": object_counts["cooling_towers"],
            "drawn_pumps": object_counts["pumps"],
            "drawn_complete_pumps": object_counts.get("complete_pumps", 0),
            "drawn_pump_openings": object_counts["pump_openings"],
            "drawn_wall_sleeves": object_counts["wall_sleeves"],
            "drawn_sump_pits": object_counts["sump_pits"],
            "drawn_embedded_plates": object_counts["embedded_plates"],
            "drawn_top_flange_pipes": object_counts["top_flange_pipes"],
            "drawn_top_openings": object_counts["top_openings"],
            "drawn_auxiliary_equipments": object_counts["auxiliary_equipments"],
            "basin_wall_thickness_mm": basin_geometry["wall_thickness_mm"],
            "basin_bottom_thickness_mm": basin_geometry["bottom_thickness_mm"],
            "basin_height_mm": basin_geometry["height"],
            "loaded_equipment_dimensions": len(dimension_results),
            "drawn_dimension_annotations": len(used_dimension_labels),
        },
    }


def capture_previews(service: CADService, version_dir: Path, version: int) -> list[str]:
    """Capture front, top and isometric previews for the equipment drawing."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    model_center = (6000.0, 5000.0, 1400.0)
    view_specs = [
        ("front", model_center, 14000.0, 28000.0, f"{OUTPUT_TOPIC}_{EQUIPMENT_TOPIC}_front_preview_v{version}.png"),
        ("top", model_center, 16000.0, 30000.0, f"{OUTPUT_TOPIC}_{EQUIPMENT_TOPIC}_top_preview_v{version}.png"),
        ("swiso", model_center, 20000.0, 32000.0, f"{OUTPUT_TOPIC}_{EQUIPMENT_TOPIC}_iso_preview_v{version}.png"),
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
            win32gui.ShowWindow(window["hwnd"], 9)
            win32gui.SetForegroundWindow(window["hwnd"])
            time.sleep(0.3)
        except Exception:
            pass
        try:
            windll.user32.SetCursorPos(
                int(window["left"] + window["width"] * 0.5),
                int(window["top"] + window["height"] * 0.6),
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


def write_version_artifacts(
    version_dir: Path,
    version: int,
    source_pdf: Path | None,
    dwg_path: Path,
    preview_paths: list[str],
    closure_report: dict[str, Any],
    dimension_entries: list[dict[str, Any]],
) -> None:
    """Archive script parameters, closure report and validation notes."""

    parameters_path = version_dir / f"{OUTPUT_TOPIC}_{EQUIPMENT_TOPIC}_parameters_v{version}.json"
    closure_path = version_dir / f"{OUTPUT_TOPIC}_{EQUIPMENT_TOPIC}_closure_report_v{version}.json"
    notes_path = version_dir / f"{OUTPUT_TOPIC}_{EQUIPMENT_TOPIC}_notes_v{version}.txt"
    dimension_snapshot_path = version_dir / f"{OUTPUT_TOPIC}_{EQUIPMENT_TOPIC}_dimensions_v{version}.json"

    parameters_path.write_text(
        json.dumps(
            {
                "topic": OUTPUT_TOPIC,
                "drawing_kind": EQUIPMENT_TOPIC,
                "version": version,
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
                "shared_jsons": {
                    "list_info": str(LIST_INFO_PATH),
                    "dimension_info": str(DIMENSION_INFO_PATH),
                    "section_info": str(SECTION_INFO_PATH),
                },
                "source_mode": "equipment drawing only; geometry is loaded from current JSON files without piping entities",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    closure_path.write_text(json.dumps(closure_report, ensure_ascii=False, indent=2), encoding="utf-8")
    dimension_snapshot_path.write_text(json.dumps(dimension_entries, ensure_ascii=False, indent=2), encoding="utf-8")

    notes = [
        f"{OUTPUT_TOPIC} {EQUIPMENT_TOPIC} JSON 驱动验证 v{version}",
        "",
        f"DWG: {dwg_path}",
        "本轮脚本直接读取:",
        f"- {LIST_INFO_PATH}",
        f"- {DIMENSION_INFO_PATH}",
        f"- {SECTION_INFO_PATH}",
        "",
        "几何摘要:",
        f"- 池体代理数: {closure_report['summary']['drawn_basins']}",
        f"- 方格池格数: {closure_report['summary']['drawn_basin_cells']}",
        f"- 分隔壁格栅数: {closure_report['summary']['drawn_partition_gratings']}",
        f"- 冷却塔数量: {closure_report['summary']['drawn_cooling_towers']}",
        f"- 泵数量: {closure_report['summary']['drawn_pumps']}",
        f"- 完整立式泵体: {closure_report['summary'].get('drawn_complete_pumps', 0)}",
        f"- 泵基础预留洞数: {closure_report['summary']['drawn_pump_openings']}",
        f"- 刚性防水套管数: {closure_report['summary']['drawn_wall_sleeves']}",
        f"- 集水坑数: {closure_report['summary']['drawn_sump_pits']}",
        f"- 池顶预埋钢板数: {closure_report['summary']['drawn_embedded_plates']}",
        f"- 池顶 DN50 法兰钢管数: {closure_report['summary']['drawn_top_flange_pipes']}",
        f"- 池顶开孔代理数: {closure_report['summary']['drawn_top_openings']}",
        f"- 左侧补充设备代理数: {closure_report['summary']['drawn_auxiliary_equipments']}",
        f"- 池壁厚度(mm): {closure_report['summary']['basin_wall_thickness_mm']}",
        f"- 池底厚度(mm): {closure_report['summary']['basin_bottom_thickness_mm']}",
        f"- 池体高度(mm): {closure_report['summary']['basin_height_mm']}",
        f"- 已读取设备尺寸条目数: {closure_report['summary']['loaded_equipment_dimensions']}",
        f"- 已落 CAD 标注数: {closure_report['summary']['drawn_dimension_annotations']}",
    ]
    if source_pdf is not None:
        notes.insert(2, f"源 PDF: {source_pdf}")
    notes_path.write_text("\n".join(notes), encoding="utf-8")

    if source_pdf is not None:
        copy_if_missing(source_pdf, SHARED_DIR / source_pdf.name)


def main() -> int:
    """Load current shared JSONs, build the equipment-only CAD model and archive output."""

    for path in (LIST_INFO_PATH, DIMENSION_INFO_PATH, SECTION_INFO_PATH):
        if not path.exists():
            raise FileNotFoundError(f"缺少输入文件: {path}")

    list_info = load_json(LIST_INFO_PATH)
    dimension_info = load_json(DIMENSION_INFO_PATH)
    section_info = load_json(SECTION_INFO_PATH)
    source_pdf_raw = list_info.get("source_pdf") or dimension_info.get("source_pdf") or section_info.get("source_pdf")
    source_pdf = Path(source_pdf_raw) if source_pdf_raw else None

    version, version_dir = prepare_version_dir(OUTPUT_ROOT_DIR)
    dwg_path = version_dir / f"{OUTPUT_TOPIC}_{EQUIPMENT_TOPIC}_3D_v{version}.dwg"

    service = CADService()
    create_fresh_document(service)
    object_counts, basin_geometry = build_equipment_model(service, dimension_info, section_info)
    used_dimension_labels = add_equipment_dimensions(service, dimension_info)
    dimension_entries = collect_equipment_dimension_entries(dimension_info)
    zoom_extents_with_retry(service.controller)

    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    preview_paths = capture_previews(service, version_dir, version)
    closure_report = build_closure_report(
        list_info=list_info,
        dimension_info=dimension_info,
        section_info=section_info,
        object_counts=object_counts,
        basin_geometry=basin_geometry,
        used_dimension_labels=used_dimension_labels,
        dimension_entries=dimension_entries,
    )
    write_version_artifacts(
        version_dir=version_dir,
        version=version,
        source_pdf=source_pdf,
        dwg_path=dwg_path,
        preview_paths=preview_paths,
        closure_report=closure_report,
        dimension_entries=dimension_entries,
    )

    print(
        json.dumps(
            {
                "success": True,
                "version": version,
                "version_dir": str(version_dir),
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
                "closure_summary": closure_report["summary"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
