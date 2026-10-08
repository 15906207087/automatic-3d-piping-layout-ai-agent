from __future__ import annotations

import json
import math
import re
import shutil
import sys
import time
from ctypes import windll
from pathlib import Path
from typing import Any, Callable

import win32gui

CURRENT_FILE = Path(__file__).resolve()
SRC_DIR = CURRENT_FILE.parents[1]
PROJECT_DIR = CURRENT_FILE.parents[2]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from screen_capture import ScreenCapture
from cad_controller import CADController
from pipeline_connection_helper import (
    _build_rotated_elbow_ports,
    apply_entity_style,
    advance_point,
    PipeConnectionRunSpec,
    RotationInstruction,
    build_standard_elbow_connection,
    build_orthogonal_elbow_connection,
    resolve_elbow_bend_radius,
)
from parametric_models.cycle_water_equipment_from_json import CADService, build_equipment_model
from parametric_models.cycle_water_equipment_from_json import (
    create_fresh_document,
    draw_box_with_retry,
    save_drawing_with_retry,
    set_named_view_with_retry,
    zoom_extents_with_retry,
)
OUTPUT_TOPIC = "循环水"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
PIPELINE_VERSION_FLOOR = 22
SHARED_DIR = OUTPUT_ROOT_DIR / "shared"
LIST_INFO_PATH = SHARED_DIR / f"{OUTPUT_TOPIC}_list_info.json"
DIMENSION_INFO_PATH = SHARED_DIR / f"{OUTPUT_TOPIC}_dimension_info.json"
SECTION_INFO_PATH = SHARED_DIR / f"{OUTPUT_TOPIC}_section_info.json"
SECTION_ROUTE_CONNECTION_INFO_PATH = SHARED_DIR / f"{OUTPUT_TOPIC}_section_route_connection_info.json"
EQUIPMENT_TOPIC = "设备图"
def resolve_latest_equipment_base_version() -> int:
    """Return the latest archived equipment-drawing version."""

    equipment_root = OUTPUT_ROOT_DIR / EQUIPMENT_TOPIC
    pattern = re.compile(r"^v(\d+)$", re.IGNORECASE)
    versions: list[int] = []
    if equipment_root.exists():
        for item in equipment_root.iterdir():
            if not item.is_dir():
                continue
            match = pattern.match(item.name)
            if match:
                versions.append(int(match.group(1)))
    if not versions:
        raise FileNotFoundError(f"未找到任何设备图版本目录: {equipment_root}")
    return max(versions)


EQUIPMENT_BASE_VERSION = resolve_latest_equipment_base_version()
EQUIPMENT_BASE_DIR = OUTPUT_ROOT_DIR / EQUIPMENT_TOPIC / f"v{EQUIPMENT_BASE_VERSION}"
EQUIPMENT_BASE_DWG_PATH = EQUIPMENT_BASE_DIR / f"{OUTPUT_TOPIC}_{EQUIPMENT_TOPIC}_3D_v{EQUIPMENT_BASE_VERSION}.dwg"

WHITE = 7
GRAY = 8
PIPE_OD = {
    100: 108.0,
    125: 133.0,
    150: 159.0,
    200: 219.0,
    250: 273.0,
    300: 325.0,
    450: 457.0,
    500: 508.0,
}

DD_LOCAL_DETAIL_CENTER = (24000.0, -4500.0, 550.0)
DD_LOCAL_DETAIL_VIEW_HEIGHT_MM = 5200.0
DD_LOCAL_DETAIL_VIEW_WIDTH_MM = 6200.0
DD_LOCAL_DETAIL_DEPTH_MM = 120.0


def lr_elbow_bend_radius_mm(dn: int) -> float:
    """Return the long-radius 90-degree elbow bend radius for one DN."""

    return float(PIPE_OD[int(dn)]) * 1.5


def xy_90_elbow_draw_center_from_corner(
    corner_point: tuple[float, float, float] | list[float],
    dn: int,
) -> tuple[float, float, float]:
    """Convert one theoretical 90-degree corner point into draw_pipe_elbow() center."""

    bend_radius = lr_elbow_bend_radius_mm(dn)
    corner_x, corner_y, corner_z = (float(corner_point[0]), float(corner_point[1]), float(corner_point[2]))
    return (
        corner_x - bend_radius,
        corner_y - bend_radius,
        corner_z,
    )


def load_json(path: Path) -> dict[str, Any]:
    """Load one UTF-8 JSON file."""

    return json.loads(path.read_text(encoding="utf-8"))


def build_section_route_connection_index(section_route_connection_info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index section route/connection entries by section name."""

    index: dict[str, dict[str, Any]] = {}
    for entry in section_route_connection_info.get("sections", []):
        section_name = str(entry.get("section", "")).strip()
        if section_name:
            index[section_name] = entry
    return index


def find_section_route_node(section_route_entry: dict[str, Any] | None, node_id: str) -> dict[str, Any] | None:
    """Return one node entry from section_route_connection_info."""

    if not section_route_entry:
        return None
    route_graph = section_route_entry.get("route_graph", {})
    for node in route_graph.get("nodes", []):
        if str(node.get("node_id", "")).strip() == node_id:
            return node
    return None


def find_section_route_pipe_run(section_route_entry: dict[str, Any] | None, run_id: str) -> dict[str, Any] | None:
    """Return one pipe_run entry from section_route_connection_info."""

    if not section_route_entry:
        return None
    route_graph = section_route_entry.get("route_graph", {})
    for pipe_run in route_graph.get("pipe_runs", []):
        if str(pipe_run.get("run_id", "")).strip() == run_id:
            return pipe_run
    return None


def find_section_route_flow_path(section_route_entry: dict[str, Any] | None, path_id: str) -> dict[str, Any] | None:
    """Return one flow_path entry from section_route_connection_info."""

    if not section_route_entry:
        return None
    route_graph = section_route_entry.get("route_graph", {})
    for flow_path in route_graph.get("flow_paths", []):
        if str(flow_path.get("path_id", "")).strip() == path_id:
            return flow_path
    return None


def resolve_section_route_flow_path_sequence(
    section_route_entry: dict[str, Any] | None,
    *,
    path_id: str,
) -> list[str]:
    """Return one cleaned flow path sequence."""

    flow_path = find_section_route_flow_path(section_route_entry, path_id)
    if flow_path is None:
        return []
    sequence = flow_path.get("sequence", [])
    if not isinstance(sequence, list):
        return []
    cleaned: list[str] = []
    for value in sequence:
        text = str(value).strip()
        if text:
            cleaned.append(text)
    return cleaned


def resolve_section_route_anchor_x(section_route_entry: dict[str, Any] | None, fallback_anchor_x: float) -> float:
    """Prefer the anchor x recorded in section_route_connection_info."""

    if not section_route_entry:
        return fallback_anchor_x
    plan_anchor = section_route_entry.get("plan_anchor", {})
    resolved_anchor_x = plan_anchor.get("resolved_anchor_x_mm")
    if resolved_anchor_x is None:
        return fallback_anchor_x
    return float(resolved_anchor_x)


def resolve_section_route_node_value(
    section_route_entry: dict[str, Any] | None,
    node_id: str,
    key: str,
    default: Any,
) -> Any:
    """Return one node field, preferring section_route_connection_info when available."""

    node = find_section_route_node(section_route_entry, node_id)
    if node is None or node.get(key) is None:
        return default
    return node.get(key)


def resolve_section_route_pipe_run_value(
    section_route_entry: dict[str, Any] | None,
    run_id: str,
    key: str,
    default: Any,
) -> Any:
    """Return one pipe_run field, preferring section_route_connection_info when available."""

    pipe_run = find_section_route_pipe_run(section_route_entry, run_id)
    if pipe_run is None or pipe_run.get(key) is None:
        return default
    return pipe_run.get(key)


def resolve_section_route_linear_dimension(
    section_route_entry: dict[str, Any] | None,
    *,
    meaning_contains: str,
    default: float,
) -> float:
    """Return one linear dimension whose meaning contains the given text."""

    if not section_route_entry:
        return float(default)
    structured_fields = section_route_entry.get("structured_fields", {})
    for entry in structured_fields.get("linear_dimension_chain_mm", []):
        meaning = str(entry.get("meaning", ""))
        if meaning_contains in meaning and entry.get("value") is not None:
            return float(entry["value"])
    return float(default)


def resolve_section_route_text_block(
    section_route_entry: dict[str, Any] | None,
    *,
    block_key: str,
) -> list[str]:
    """Return one text_record block as a cleaned string list."""

    if not section_route_entry:
        return []
    text_record = section_route_entry.get("text_record", {})
    values = text_record.get(block_key, [])
    if not isinstance(values, list):
        return []
    cleaned: list[str] = []
    for value in values:
        text = str(value).strip()
        if text:
            cleaned.append(text)
    return cleaned


def resolve_section_verbatim_analysis_lines(section_route_entry: dict[str, Any] | None) -> list[str]:
    """Return one verbatim section analysis snapshot as cleaned text lines."""

    if not section_route_entry:
        return []
    snapshot = section_route_entry.get("verbatim_analysis_snapshot", {})
    if not isinstance(snapshot, dict):
        return []
    values = snapshot.get("lines", [])
    if not isinstance(values, list):
        return []
    cleaned: list[str] = []
    for value in values:
        text = str(value)
        if text.strip() or text == "":
            cleaned.append(text)
    return cleaned


def resolve_section_route_modeling_description(
    section_route_entry: dict[str, Any] | None,
    *,
    default: str,
) -> str:
    """Prefer text_record.modeling_description as the modeling basis text."""

    modeling_description = resolve_section_route_text_block(
        section_route_entry,
        block_key="modeling_description",
    )
    if modeling_description:
        return " ".join(modeling_description)
    analysis_text = resolve_section_route_text_block(section_route_entry, block_key="analysis")
    if analysis_text:
        return " ".join(analysis_text)
    return str(default)


def resolve_section_route_execution_text(
    section_route_entry: dict[str, Any] | None,
    *,
    preferred_blocks: tuple[str, ...] = ("modeling_description", "analysis"),
) -> str:
    """Join text_record blocks into one execution text string."""

    text_parts: list[str] = []
    for block_key in preferred_blocks:
        text_parts.extend(resolve_section_route_text_block(section_route_entry, block_key=block_key))
    return " ".join(text_parts).strip()


def convert_text_metric_to_mm(raw_value: str) -> float:
    """Convert one text metric token to millimeters."""

    value = float(raw_value)
    return value * 1000.0 if abs(value) <= 20.0 else value


def resolve_dd_route_text_overrides(section_route_entry: dict[str, Any] | None) -> dict[str, Any]:
    """Parse D-D execution parameters from text_record first."""

    execution_text = resolve_section_route_execution_text(section_route_entry)
    if not execution_text:
        return {}

    overrides: dict[str, Any] = {"execution_text": execution_text}

    dn_match = re.search(r"DN\s*(\d+)", execution_text, re.IGNORECASE)
    if dn_match:
        overrides["branch_dn"] = int(dn_match.group(1))

    anchor_match = re.search(r"锚点取\s*([A-Za-z][A-Za-z0-9_]*)", execution_text)
    if anchor_match:
        overrides["anchor_reference"] = anchor_match.group(1)
        overrides["horizontal_pass_through_reference"] = anchor_match.group(1)
    else:
        sleeve_anchor_match = re.search(r"防水套管\(\s*([A-Za-z][A-Za-z0-9_]*)", execution_text)
        if sleeve_anchor_match:
            overrides["horizontal_pass_through_reference"] = sleeve_anchor_match.group(1)

    anchor_x_match = re.search(r"x\s*=\s*([+-]?\d+(?:\.\d+)?)", execution_text, re.IGNORECASE)
    if anchor_x_match:
        overrides["anchor_x_mm"] = float(anchor_x_match.group(1))

    horizontal_y_match = re.search(r"y\s*=\s*([+-]?\d+(?:\.\d+)?)", execution_text, re.IGNORECASE)
    if horizontal_y_match:
        overrides["horizontal_pass_through_y_mm"] = float(horizontal_y_match.group(1))
    else:
        centerline_y_match = re.search(r"中心线\s*y\s*=\s*([+-]?\d+(?:\.\d+)?)", execution_text, re.IGNORECASE)
        if centerline_y_match:
            overrides["horizontal_pass_through_y_mm"] = float(centerline_y_match.group(1))

    route_shape_match = re.search(
        r"自\s*([+-]?\d+(?:\.\d+)?)\s*下落到\s*([+-]?\d+(?:\.\d+)?)",
        execution_text,
    )
    if route_shape_match:
        riser_start_elevation_mm = convert_text_metric_to_mm(route_shape_match.group(1))
        horizontal_elevation_mm = convert_text_metric_to_mm(route_shape_match.group(2))
        overrides["vertical_direction"] = "down"
        overrides["riser_start_elevation_mm"] = riser_start_elevation_mm
        overrides["horizontal_elevation_mm"] = horizontal_elevation_mm
        overrides["top_elevation_mm"] = max(riser_start_elevation_mm, horizontal_elevation_mm)
        overrides["bottom_elevation_mm"] = min(riser_start_elevation_mm, horizontal_elevation_mm)

    horizontal_z_match = re.search(r"z\s*=\s*([+-]?\d+(?:\.\d+)?)", execution_text, re.IGNORECASE)
    if horizontal_z_match:
        horizontal_elevation_mm = convert_text_metric_to_mm(horizontal_z_match.group(1))
        overrides["horizontal_elevation_mm"] = horizontal_elevation_mm
        if "bottom_elevation_mm" not in overrides:
            overrides["bottom_elevation_mm"] = horizontal_elevation_mm

    offset_match = re.search(r"偏移\s*([+-]?\d+(?:\.\d+)?)", execution_text)
    if offset_match:
        overrides["horizontal_offset_length_mm"] = float(offset_match.group(1))

    short_connect_match = re.search(r"短接(?:长度)?\s*\(?\s*([+-]?\d+(?:\.\d+)?)\s*\)?", execution_text)
    if short_connect_match:
        overrides["equipment_short_connect_length_mm"] = float(short_connect_match.group(1))

    elbow_corner_distance_match = re.search(
        r"第一只弯头与第二只弯头(?:之间)?(?:的理论转角点)?(?:间距|距离).*?=\s*([+-]?\d+(?:\.\d+)?)",
        execution_text,
    )
    if elbow_corner_distance_match:
        overrides["elbow_corner_distance_mm"] = float(elbow_corner_distance_match.group(1))
    else:
        elbow_corner_distance_sum_match = re.search(
            r"第一只弯头与第二只弯头(?:之间)?(?:的理论转角点)?(?:间距|距离).*?([+-]?\d+(?:\.\d+)?)\s*\+\s*([+-]?\d+(?:\.\d+)?)",
            execution_text,
        )
        if elbow_corner_distance_sum_match:
            overrides["elbow_corner_distance_mm"] = float(elbow_corner_distance_sum_match.group(1)) + float(
                elbow_corner_distance_sum_match.group(2)
            )

    if re.search(r"(?:不再|不需要|无需).{0,8}保留.{0,8}(?:接口)?短接", execution_text):
        overrides["draw_visible_equipment_stub"] = False

    upper_elbow_match = re.search(r"第(?:一|1)只\s*(\d+)\s*号", execution_text)
    if upper_elbow_match:
        overrides["upper_elbow_item_no"] = int(upper_elbow_match.group(1))

    lower_elbow_match = re.search(r"第(?:二|2)只\s*(\d+)\s*号", execution_text)
    if lower_elbow_match:
        overrides["lower_elbow_item_no"] = int(lower_elbow_match.group(1))

    orifice_match = re.search(r"(\d+)\s*号\s*(?:DN\d+\s*)?制孔口", execution_text)
    if orifice_match:
        overrides["orifice_item_no"] = int(orifice_match.group(1))

    sleeve_match = re.search(r"(\d+)\s*号\s*(?:刚性|钢性)?防水套管", execution_text)
    if sleeve_match:
        overrides["sleeve_item_no"] = int(sleeve_match.group(1))

    return overrides


def resolve_section_route_position_y(
    section_route_entry: dict[str, Any] | None,
    *,
    node_id: str | None = None,
    point_id: str | None = None,
    default: float,
) -> float:
    """Return one A-A/G-G/E-E positioning y value from position_constraints when present."""

    if not section_route_entry:
        return float(default)
    structured_fields = section_route_entry.get("structured_fields", {})
    position_constraints = structured_fields.get("position_constraints", {})
    if node_id:
        node_center_y_mm = position_constraints.get("node_center_y_mm", {})
        if node_id in node_center_y_mm:
            return float(node_center_y_mm[node_id])
    if point_id:
        derived_points_y_mm = position_constraints.get("derived_points_y_mm", {})
        if point_id in derived_points_y_mm:
            return float(derived_points_y_mm[point_id])
    return float(default)


def copy_if_missing(source: Path, destination: Path) -> None:
    """Copy one file when the target does not already exist."""

    if destination.exists() or not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def prepare_version_dir(output_root: Path, *, minimum_version: int = 1) -> tuple[int, Path]:
    """Create the next reusable version directory without overwriting retained outputs."""

    output_root.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(r"^v(\d+)$", re.IGNORECASE)
    occupied_versions: set[int] = set()
    reusable_versions: set[int] = set()
    for item in output_root.iterdir():
        if not item.is_dir():
            continue
        match = pattern.match(item.name)
        if not match:
            continue
        version = int(match.group(1))
        child_files = [child for child in item.iterdir() if child.is_file()]
        meaningful_files = [child for child in child_files if child.suffix.lower() not in {".dwl", ".dwl2"}]
        if meaningful_files:
            occupied_versions.add(version)
            continue
        reusable_versions.add(version)
        for child in child_files:
            try:
                child.unlink()
            except OSError:
                pass
    next_version = max(1, int(minimum_version))
    while next_version in occupied_versions:
        next_version += 1
    version_dir = output_root / f"v{next_version}"
    if next_version in reusable_versions:
        version_dir.mkdir(parents=True, exist_ok=True)
    else:
        version_dir.mkdir(parents=True, exist_ok=False)
    return next_version, version_dir


def flatten_dimension_entries(dimension_info: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every page-level dimension entry kept in dimension_info.json."""

    flattened: list[dict[str, Any]] = []
    for page in dimension_info.get("page_dimension_ledger", []):
        page_name = str(page.get("page", ""))
        view_name = str(page.get("view_name", ""))
        for entry in page.get("entries", []):
            flattened.append(
                {
                    **entry,
                    "_ledger_scope": "page",
                    "_scope_name": page_name,
                    "_view_name": view_name,
                }
            )
    return flattened


def build_profile_index(section_info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index every pipeline profile by profile_id for later route compilation."""

    profile_index: dict[str, dict[str, Any]] = {}
    profiles = section_info.get("pipeline_profiles_v2", {})
    for group_name in ("global_profiles", "section_only_profiles"):
        for profile in profiles.get(group_name, []):
            profile_id = str(profile.get("profile_id", "")).strip()
            if profile_id:
                profile_index[profile_id] = profile
    return profile_index


def build_tag_index(items: list[dict[str, Any]], *, tag_key: str) -> dict[str, dict[str, Any]]:
    """Index one list of equipment records by tag-like key."""

    index: dict[str, dict[str, Any]] = {}
    for item in items:
        tag = str(item.get(tag_key, "")).strip()
        if tag:
            index[tag] = item
    return index


def require_bundle(layout: dict[str, Any], group_key: str, id_key: str, bundle_id: str) -> dict[str, Any]:
    """Return one route bundle from pipeline_layout_v2 and fail clearly when absent."""

    for item in layout.get(group_key, []):
        if str(item.get(id_key, "")) == bundle_id:
            return item
    raise KeyError(f"pipeline_layout_v2.{group_key} 缺少 {bundle_id}")


def resolve_header_span(
    basin_geometry: dict[str, Any],
    *,
    total_length_mm: float | None,
    fallback_start_x: float,
    fallback_end_x: float,
    horizontal_anchor: dict[str, Any] | None = None,
) -> tuple[float, float]:
    """Resolve one main header span, preferring an explicit total length."""

    if total_length_mm is None or total_length_mm <= 0:
        total_length_mm = fallback_end_x - fallback_start_x
    if horizontal_anchor:
        anchor_mode = str(horizontal_anchor.get("mode", "")).strip()
        anchor_offset = float(horizontal_anchor.get("offset_mm", 0.0))
        basin_min_x = float(basin_geometry["center_x"]) - float(basin_geometry["length"]) / 2.0
        basin_max_x = float(basin_geometry["center_x"]) + float(basin_geometry["length"]) / 2.0
        wall_thickness_mm = float(basin_geometry.get("wall_thickness_mm", 0.0))
        basin_inner_min_x = basin_min_x + wall_thickness_mm
        basin_inner_max_x = basin_max_x - wall_thickness_mm
        if anchor_mode == "right_end_offset_from_right_outer_wall":
            end_x = basin_max_x + anchor_offset
            return end_x - total_length_mm, end_x
        if anchor_mode == "right_end_offset_from_right_inner_wall":
            end_x = basin_inner_max_x - anchor_offset
            return end_x - total_length_mm, end_x
        if anchor_mode == "left_start_offset_from_left_outer_wall":
            start_x = basin_min_x - anchor_offset
            return start_x, start_x + total_length_mm
        if anchor_mode == "left_start_offset_from_left_inner_wall":
            start_x = basin_inner_min_x + anchor_offset
            return start_x, start_x + total_length_mm
    center_x = float(basin_geometry["center_x"])
    half_length = total_length_mm / 2.0
    return center_x - half_length, center_x + half_length


def extract_anchor_context(
    dimension_info: dict[str, Any],
    basin_geometry: dict[str, Any],
) -> dict[str, Any]:
    """Collect equipment anchors shared by the V31 equipment and pipeline scripts."""

    model_input = dimension_info.get("model_input", {})
    equipment_centers = model_input.get("equipment_centers", {})
    pump_index = build_tag_index(list(equipment_centers.get("pumps", [])), tag_key="tag")
    tower_index = build_tag_index(list(equipment_centers.get("cooling_towers", [])), tag_key="tag")
    pump_template = equipment_centers.get("pump_foundation_template", {})
    pump_plan_size = [float(value) for value in pump_template.get("plan_size_mm", [0.0, 0.0])]
    pump_half_width = pump_plan_size[1] / 2.0 if len(pump_plan_size) >= 2 else 0.0
    basin_min_x = float(basin_geometry["center_x"]) - float(basin_geometry["length"]) / 2.0
    basin_max_x = float(basin_geometry["center_x"]) + float(basin_geometry["length"]) / 2.0
    basin_min_y = float(basin_geometry["center_y"]) - float(basin_geometry["width"]) / 2.0
    basin_max_y = float(basin_geometry["center_y"]) + float(basin_geometry["width"]) / 2.0
    wall_thickness_mm = float(basin_geometry.get("wall_thickness_mm", 0.0))
    basin_inner_min_x = basin_min_x + wall_thickness_mm
    basin_inner_max_x = basin_max_x - wall_thickness_mm
    basin_inner_min_y = basin_min_y + wall_thickness_mm
    basin_inner_max_y = basin_max_y - wall_thickness_mm
    pump_row_center_y = (
        sum(float(pump["center"][1]) for pump in pump_index.values()) / len(pump_index) if pump_index else basin_max_y
    )
    return {
        "pump_index": pump_index,
        "tower_index": tower_index,
        "pump_half_width": pump_half_width,
        "pump_row_center_y": pump_row_center_y,
        "basin_min_x": basin_min_x,
        "basin_max_x": basin_max_x,
        "basin_min_y": basin_min_y,
        "basin_max_y": basin_max_y,
        "basin_inner_min_x": basin_inner_min_x,
        "basin_inner_max_x": basin_inner_max_x,
        "basin_inner_min_y": basin_inner_min_y,
        "basin_inner_max_y": basin_inner_max_y,
        "wall_thickness_mm": wall_thickness_mm,
    }


def build_dynamic_pipeline_layout(
    dimension_info: dict[str, Any],
    section_info: dict[str, Any],
    section_route_connection_info: dict[str, Any],
    basin_geometry: dict[str, Any],
) -> dict[str, Any]:
    """Compile anchor-driven routes so the piping follows the V31 equipment layout."""

    model_input = dimension_info.get("model_input", {})
    layout = model_input.get("pipeline_layout_v2", {})
    if not layout:
        raise RuntimeError("缺少 model_input.pipeline_layout_v2，无法按 V31 设备布置图重建配管。")

    profile_index = build_profile_index(section_info)
    context = extract_anchor_context(dimension_info, basin_geometry)
    section_route_connection_index = build_section_route_connection_index(section_route_connection_info)
    plan_reference = model_input.get("plan_reference", {})
    wall_sleeve_index = build_tag_index(list(plan_reference.get("wall_sleeves", [])), tag_key="sleeve_id")
    embedded_plate_index = build_tag_index(list(plan_reference.get("embedded_plates", [])), tag_key="plate_id")
    equipment_centers = model_input.get("equipment_centers", {})
    auxiliary_equipment_index = build_tag_index(list(equipment_centers.get("auxiliary_equipments", [])), tag_key="tag")
    pump_index = context["pump_index"]
    tower_index = context["tower_index"]
    pump_half_width = float(context["pump_half_width"])
    basin_min_x = float(context["basin_min_x"])
    basin_max_x = float(context["basin_max_x"])
    basin_min_y = float(context["basin_min_y"])
    basin_max_y = float(context["basin_max_y"])
    basin_inner_min_x = float(context["basin_inner_min_x"])
    basin_inner_max_x = float(context["basin_inner_max_x"])
    basin_inner_min_y = float(context["basin_inner_min_y"])
    basin_inner_max_y = float(context["basin_inner_max_y"])
    pump_row_center_y = float(context["pump_row_center_y"])
    main_header_constraints = layout.get("p4_main_header_constraints_v4") or layout.get("p4_lower_header_constraints_v3", {})
    delete_legacy_long_pipe_group = bool(
        main_header_constraints.get(
            "delete_legacy_long_pipe_group",
            main_header_constraints.get("delete_bottom_long_pipe_group", False),
        )
    )
    delete_all_non_header_pipes = bool(main_header_constraints.get("delete_all_non_header_pipes", False))
    section_preview_sections = {
        str(section_name).strip()
        for section_name in main_header_constraints.get("section_preview_sections", [])
        if str(section_name).strip()
    }
    draw_auxiliary_routes_in_header_only_mode = bool(
        main_header_constraints.get("draw_auxiliary_routes_in_header_only_mode", False)
    )
    draw_pump_branch_routes_in_header_only_mode = bool(
        main_header_constraints.get("draw_pump_branch_routes_in_header_only_mode", False)
    )
    draw_tower_drop_routes_in_header_only_mode = bool(
        main_header_constraints.get("draw_tower_drop_routes_in_header_only_mode", False)
    )
    enabled_auxiliary_route_ids = {
        str(route_id).strip()
        for route_id in main_header_constraints.get("enabled_auxiliary_route_ids", [])
        if str(route_id).strip()
    }
    enabled_pump_branch_tags = {
        str(pump_tag).strip()
        for pump_tag in main_header_constraints.get("enabled_pump_branch_tags", [])
        if str(pump_tag).strip()
    }
    enabled_tower_drop_tags = {
        str(tower_tag).strip()
        for tower_tag in main_header_constraints.get("enabled_tower_drop_tags", [])
        if str(tower_tag).strip()
    }
    header_templates = build_tag_index(list(layout.get("global_headers", [])), tag_key="header_id")
    using_plan_main_headers = bool(main_header_constraints.get("upper_header") and main_header_constraints.get("lower_header"))

    if using_plan_main_headers:
        upper_header_config = dict(main_header_constraints.get("upper_header", {}))
        lower_header_config = dict(main_header_constraints.get("lower_header", {}))
        upper_header_template = header_templates.get(str(upper_header_config.get("header_id", "")).strip())
        lower_header_template = header_templates.get(str(lower_header_config.get("header_id", "")).strip())
        if upper_header_template is None:
            raise KeyError("pipeline_layout_v2.global_headers 缺少 upper_header 对应模板")
        if lower_header_template is None:
            raise KeyError("pipeline_layout_v2.global_headers 缺少 lower_header 对应模板")
        upper_header_template_segment = list(upper_header_template.get("segments", []))[0]
        lower_header_template_segment = list(lower_header_template.get("segments", []))[0]
        main_run_length_mm = float(main_header_constraints.get("main_run_length_mm", 0.0))
        horizontal_anchor = dict(main_header_constraints.get("horizontal_anchor", {}))
        upper_horizontal_anchor = dict(upper_header_config.get("horizontal_anchor", horizontal_anchor))
        lower_horizontal_anchor = dict(lower_header_config.get("horizontal_anchor", horizontal_anchor))
        upper_header_start_x, upper_header_end_x = resolve_header_span(
            basin_geometry,
            total_length_mm=main_run_length_mm,
            fallback_start_x=float(upper_header_template_segment["start"][0]),
            fallback_end_x=float(upper_header_template_segment["end"][0]),
            horizontal_anchor=upper_horizontal_anchor,
        )
        lower_header_start_x, lower_header_end_x = resolve_header_span(
            basin_geometry,
            total_length_mm=main_run_length_mm,
            fallback_start_x=float(lower_header_template_segment["start"][0]),
            fallback_end_x=float(lower_header_template_segment["end"][0]),
            horizontal_anchor=lower_horizontal_anchor,
        )
        upper_header_y = basin_inner_max_y + float(upper_header_config["center_offset_from_inner_wall_mm"])
        lower_header_y = basin_inner_min_y - float(lower_header_config["center_offset_from_inner_wall_mm"])
        upper_header_z = float(upper_header_config["center_elevation_mm"])
        lower_header_z = float(lower_header_config["center_elevation_mm"])
        upper_header_dn = int(upper_header_config["dn"])
        lower_header_dn = int(lower_header_config["dn"])
    else:
        upper_header_template = require_bundle(layout, "global_headers", "header_id", "top_header_dn450")
        lower_header_template = require_bundle(layout, "global_headers", "header_id", "bottom_header_dn500")
        upper_header_template_segment = list(upper_header_template.get("segments", []))[0]
        lower_header_template_segment = list(lower_header_template.get("segments", []))[0]
        upper_header_y_offset = float(upper_header_template_segment["start"][1]) - pump_row_center_y
        lower_header_y_offset = float(lower_header_template_segment["start"][1]) - basin_max_y
        upper_header_y = pump_row_center_y + upper_header_y_offset
        lower_header_y = basin_max_y + lower_header_y_offset
        upper_header_z = float(profile_index["top_header_profile"]["main_elevation_mm"])
        lower_header_z = float(profile_index["bottom_header_profile"]["main_elevation_mm"])
        if main_header_constraints:
            dn500_constraints = main_header_constraints.get("dn500_header", {})
            lower_header_y = basin_inner_max_y + float(
                dn500_constraints.get("center_offset_from_inner_wall_mm", lower_header_y)
            )
            lower_header_z = float(dn500_constraints.get("center_elevation_mm", lower_header_z))
        upper_header_start_x = basin_min_x + (float(upper_header_template_segment["start"][0]) - basin_min_x)
        upper_header_end_x = basin_max_x + (float(upper_header_template_segment["end"][0]) - basin_max_x)
        lower_header_start_x = basin_inner_min_x
        lower_header_end_x = basin_inner_max_x
        upper_header_dn = int(upper_header_template["dn"])
        lower_header_dn = int(lower_header_template["dn"])

    compiled_layout: dict[str, Any] = {
        "version_tag": f"{layout.get('version_tag', 'unknown')}-v{EQUIPMENT_BASE_VERSION}-anchored",
        "basis": (
            f"以设备图 v{EQUIPMENT_BASE_VERSION} 的设备锚点为基础重编译平面与高程路线；"
            "平面偏移继承 pipeline_layout_v2，标高模板继承 pipeline_profiles_v2。"
        ),
        "global_headers": [
            {
                "header_id": str(upper_header_template["header_id"]),
                "dn": upper_header_dn,
                "segments": [
                    {
                        "start": [upper_header_start_x, upper_header_y, upper_header_z],
                        "end": [upper_header_end_x, upper_header_y, upper_header_z],
                    }
                ],
                "basis": str(upper_header_template.get("basis", "")),
            },
            {
                "header_id": str(lower_header_template["header_id"]),
                "dn": lower_header_dn,
                "segments": [
                    {
                        "start": [lower_header_start_x, lower_header_y, lower_header_z],
                        "end": [lower_header_end_x, lower_header_y, lower_header_z],
                    }
                ],
                "basis": str(lower_header_template.get("basis", "")),
            },
        ],
        "pump_branch_routes": [],
        "tower_drop_routes": [],
        "section_preview_routes": [],
        "auxiliary_routes": [],
        "support_proxies_v2": [],
    }

    if not delete_all_non_header_pipes or draw_pump_branch_routes_in_header_only_mode:
        upper_profile = profile_index["pump_upper_connection_profile"]
        lower_profile = profile_index["pump_lower_connection_profile"]
        upper_connection_z = float(upper_profile["pump_side_elevation_mm"])
        lower_connection_z = float(lower_profile["pump_side_elevation_mm"])
        lower_header_connection_z = (
            lower_header_z if using_plan_main_headers else float(lower_profile["header_connection_elevation_mm"])
        )
        pump_branch_route_templates = {
            str(route.get("pump_tag", "")).strip(): route
            for route in layout.get("pump_branch_routes", [])
            if isinstance(route, dict) and str(route.get("pump_tag", "")).strip()
        }
        top_connection_y = 0.0
        bottom_connection_y = 0.0

        for pump_tag in ("P-0204", "P-0203", "P-0202", "P-0201"):
            if enabled_pump_branch_tags and pump_tag not in enabled_pump_branch_tags:
                continue
            pump = pump_index.get(pump_tag)
            if pump is None:
                raise KeyError(f"equipment_centers.pumps 缺少 {pump_tag}")
            pump_center_x = float(pump["center"][0])
            pump_center_y = float(pump["center"][1])
            pump_branch_template = pump_branch_route_templates.get(pump_tag, {})
            upper_branch_anchor_reference = str(
                pump_branch_template.get("upper_anchor_reference", "")
            ).strip()
            upper_branch_anchor_x = pump_branch_template.get("upper_anchor_x_mm")
            if upper_branch_anchor_x is None and upper_branch_anchor_reference:
                upper_anchor_bundle = wall_sleeve_index.get(upper_branch_anchor_reference)
                if upper_anchor_bundle is not None:
                    upper_branch_anchor_x = float(upper_anchor_bundle["center"][0])
            upper_branch_anchor_x = (
                float(upper_branch_anchor_x) if upper_branch_anchor_x is not None else pump_center_x
            )
            top_connection_y = pump_center_y + pump_half_width
            bottom_connection_y = pump_center_y - pump_half_width
            compiled_layout["pump_branch_routes"].append(
                {
                    "pump_tag": pump_tag,
                    "upper_branch_dn": int(upper_profile["dn"]),
                    "upper_segments": [
                        {
                            "start": [upper_branch_anchor_x, upper_header_y, upper_header_z],
                            "end": [upper_branch_anchor_x, top_connection_y, upper_header_z],
                        },
                        {
                            "start": [upper_branch_anchor_x, top_connection_y, upper_header_z],
                            "end": [upper_branch_anchor_x, top_connection_y, upper_connection_z],
                        },
                    ],
                    "lower_branch_dn": int(lower_profile["dn"]),
                    "lower_segments": (
                        [
                            {
                                "start": [pump_center_x, bottom_connection_y, lower_connection_z],
                                "end": [pump_center_x, bottom_connection_y, lower_header_connection_z],
                            }
                        ]
                        if delete_legacy_long_pipe_group
                        else [
                            {
                                "start": [pump_center_x, bottom_connection_y, lower_connection_z],
                                "end": [pump_center_x, bottom_connection_y, lower_header_connection_z],
                            },
                            {
                                "start": [pump_center_x, bottom_connection_y, lower_header_connection_z],
                                "end": [pump_center_x, lower_header_y, lower_header_connection_z],
                            },
                        ]
                    ),
                    "basis": (
                        (
                            f"{pump_tag} 上支管 x 锚点按 {upper_branch_anchor_reference}="
                            f"{upper_branch_anchor_x:.1f} 覆盖泵中心；"
                            if abs(upper_branch_anchor_x - pump_center_x) > 1e-6
                            else f"{pump_tag} 以上下泵基础中心为 x 锚点；"
                        )
                        + f"上支管读取 +{upper_connection_z/1000.0:.3f}；"
                        + (
                            f"下支管长横向连段按用户要求删除，仅保留降到 {lower_header_connection_z/1000.0:.3f} 的局部立管。"
                            if delete_legacy_long_pipe_group
                            else f"下支管读取 +{lower_connection_z/1000.0:.3f}，并接至下方主管 {lower_header_connection_z/1000.0:.3f}。"
                        )
                    ),
                }
            )

    if not delete_all_non_header_pipes or draw_tower_drop_routes_in_header_only_mode:
        tower_template_map = {
            str(item["tower_tag"]): item for item in layout.get("tower_drop_routes", [])
        }
        default_tower_connection_z = float(
            profile_index.get("right_tower_drop_profile", {}).get(
                "connection_elevation_mm",
                model_input.get("elevations", {}).get("tower_drop_connection", 3200.0),
            )
        )
        right_tower_start_z = float(
            profile_index.get("right_tower_drop_profile", {}).get("main_elevation_mm", upper_header_z)
        )
        for tower_tag in ("CT-0203", "CT-0202", "CT-0201"):
            if enabled_tower_drop_tags and tower_tag not in enabled_tower_drop_tags:
                continue
            tower = tower_index.get(tower_tag)
            if tower is None:
                raise KeyError(f"equipment_centers.cooling_towers 缺少 {tower_tag}")
            template = tower_template_map.get(tower_tag)
            if template is None:
                raise KeyError(f"pipeline_layout_v2.tower_drop_routes 缺少 {tower_tag}")
            if not bool(template.get("enabled", True)):
                continue
            tower_center_x = float(tower["center"][0])
            tower_center_y = float(tower["center"][1])
            template_connection_offset_y = float(template["segments"][-1]["end"][1]) - float(template["segments"][-1]["start"][1])
            tower_connection_y = tower_center_y + template_connection_offset_y
            tower_connection_z = default_tower_connection_z
            initial_drop_z = right_tower_start_z if tower_tag == "CT-0201" else upper_header_z
            segments = []
            if abs(initial_drop_z - upper_header_z) > 1e-6:
                segments.append(
                    {
                        "start": [tower_center_x, upper_header_y, upper_header_z],
                        "end": [tower_center_x, upper_header_y, initial_drop_z],
                    }
                )
            segments.extend(
                [
                    {
                        "start": [tower_center_x, upper_header_y, initial_drop_z],
                        "end": [tower_center_x, upper_header_y, tower_connection_z],
                    },
                    {
                        "start": [tower_center_x, upper_header_y, tower_connection_z],
                        "end": [tower_center_x, tower_connection_y, tower_connection_z],
                    },
                ]
            )
            compiled_layout["tower_drop_routes"].append(
                {
                    "tower_tag": tower_tag,
                    "dn": int(template["dn"]),
                    "segments": segments,
                    "basis": (
                        f"{tower_tag} 以设备图 v{EQUIPMENT_BASE_VERSION} 冷却塔中心为 x 锚点，"
                        f"保留 P4 到塔口的 y 偏移 {template_connection_offset_y:.0f}；"
                        f"接塔高程按 {tower_connection_z:.0f}，"
                        f"{'顶部先降到右侧剖面 +4.500 后再下落。' if tower_tag == 'CT-0201' else '顶部主管沿 H-H 的 +5.000 高程入塔。'}"
                    ),
                }
            )

    if (
        (not delete_all_non_header_pipes and not delete_legacy_long_pipe_group)
        or draw_auxiliary_routes_in_header_only_mode
    ):
        auxiliary_route_templates = [
            route
            for route in layout.get("auxiliary_routes", [])
            if isinstance(route, dict) and str(route.get("route_id", "")).strip()
        ]
        auxiliary_profile_map = {
            "left_wall_dn200_upper": profile_index["left_wall_dn200_upper_profile"],
            "left_wall_dn200_lower": profile_index["left_wall_dn200_lower_profile"],
        }
        for template in auxiliary_route_templates:
            route_id = str(template.get("route_id", "")).strip()
            if enabled_auxiliary_route_ids and route_id not in enabled_auxiliary_route_ids:
                continue
            if bool(template.get("use_direct_segments", False)):
                compiled_layout["auxiliary_routes"].append(
                    {
                        "route_id": route_id,
                        "dn": int(template["dn"]),
                        "segments": [
                            {
                                "start": [float(value) for value in segment["start"]],
                                "end": [float(value) for value in segment["end"]],
                            }
                            for segment in list(template.get("segments", []))
                        ],
                        "basis": str(template.get("basis", "")),
                    }
                )
                continue
            profile = auxiliary_profile_map.get(route_id)
            if profile is None:
                raise KeyError(f"未识别的 auxiliary_routes.route_id: {route_id}")
            first_segment = list(template.get("segments", []))[0]
            second_segment = list(template.get("segments", []))[1]
            route_y = basin_max_y + (float(first_segment["start"][1]) - basin_max_y)
            outer_start_x = basin_min_x + (float(first_segment["start"][0]) - basin_min_x)
            wall_center_x = basin_min_x + (float(first_segment["end"][0]) - basin_min_x)
            wall_internal_y = basin_max_y + (float(second_segment["end"][1]) - basin_max_y)
            route_z = float(profile["main_elevation_mm"])
            compiled_layout["auxiliary_routes"].append(
                {
                    "route_id": route_id,
                    "dn": int(profile["dn"]),
                    "segments": [
                        {
                            "start": [outer_start_x, route_y, route_z],
                            "end": [wall_center_x, route_y, route_z],
                        },
                        {
                            "start": [wall_center_x, route_y, route_z],
                            "end": [wall_center_x, wall_internal_y, route_z],
                        },
                    ],
                    "basis": (
                        f"{route_id} 以 v{EQUIPMENT_BASE_VERSION} 池体左外壁和池顶边为锚点重定位；"
                        f"高程读取 {profile['profile_id']}={route_z:.0f}。"
                    ),
                }
            )

    for route in layout.get("section_preview_routes", []):
        if not bool(route.get("enabled", True)):
            continue
        section_name = str(route.get("section", "")).strip()
        if section_preview_sections and section_name and section_name not in section_preview_sections:
            continue
        section_route_entry = section_route_connection_index.get(section_name)
        route_kind = str(route.get("route_kind", "")).strip().lower()
        anchor_reference = str(route.get("anchor_reference", "")).strip()
        use_section_route_anchor_x = bool(route.get("use_section_route_anchor_x", True))
        if route_kind == "aa_dn200_plan_external_connection":
            source_aa_route_id = str(route.get("source_aa_route_id", "")).strip()
            if not source_aa_route_id:
                raise KeyError("aa_dn200_plan_external_connection 缺少 source_aa_route_id。")
            source_aa_route = require_bundle(layout, "section_preview_routes", "route_id", source_aa_route_id)
            if "anchor_x_mm" in source_aa_route:
                source_anchor_x = float(source_aa_route["anchor_x_mm"])
            elif "anchor_x" in source_aa_route:
                source_anchor_x = float(source_aa_route["anchor_x"])
            else:
                raise KeyError(f"{source_aa_route_id} 缺少 anchor_x_mm/anchor_x，无法派生 DN200 外接链列位。")
            anchor_x = source_anchor_x + float(route.get("connection_x_offset_from_source_anchor_mm", 0.0))
        elif route.get("anchor_x_mm") is not None:
            anchor_x = float(route["anchor_x_mm"])
        else:
            anchor_bundle = wall_sleeve_index.get(anchor_reference)
            if anchor_bundle is None:
                anchor_bundle = embedded_plate_index.get(anchor_reference)
            if anchor_bundle is None:
                raise KeyError(f"section_preview_routes 缺少可用锚点 {anchor_reference}")
            anchor_x = float(anchor_bundle["center"][0])
        if use_section_route_anchor_x:
            anchor_x = resolve_section_route_anchor_x(section_route_entry, anchor_x)
        base_header_id = str(route.get("base_header_id", "")).strip()
        if base_header_id == str(lower_header_template["header_id"]):
            base_y = lower_header_y
            base_z = lower_header_z
            base_header_dn = lower_header_dn
        elif base_header_id == str(upper_header_template["header_id"]):
            base_y = upper_header_y
            base_z = upper_header_z
            base_header_dn = upper_header_dn
        else:
            raise KeyError(f"section_preview_routes.base_header_id 未匹配到当前主管: {base_header_id}")
        if route_kind == "aa_main_section_slice":
            aa_route_basis = resolve_section_route_modeling_description(
                section_route_entry,
                default=str(route.get("basis", "")),
            )
            left_branch_dn = int(
                resolve_section_route_pipe_run_value(
                    section_route_entry,
                    "aa_left_riser_dn250",
                    "dn",
                    route.get("left_branch_dn", 250),
                )
            )
            left_valve_item_no = resolve_section_route_node_value(
                section_route_entry,
                "aa_left_valve_4",
                "item_no",
                route.get("left_valve_item_no"),
            )
            left_valve_center_elevation = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_left_valve_4",
                    "elevation_mm",
                    route.get("left_valve_center_elevation_mm", 1200.0),
                )
            )
            left_valve_structure_length = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_left_valve_4",
                    "structure_length_mm",
                    route.get("left_valve_structure_length_mm", 76.0),
                )
            )
            left_top_horizontal_elevation = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_left_top_elbow_dn250",
                    "elevation_mm",
                    route.get("left_top_horizontal_elevation_mm", left_valve_center_elevation),
                )
            )
            left_top_elbow_item_no = resolve_section_route_node_value(
                section_route_entry,
                "aa_left_top_elbow_dn250",
                "item_no",
                route.get("left_top_elbow_item_no"),
            )
            left_header_item_no = resolve_section_route_node_value(
                section_route_entry,
                "aa_left_header_circle",
                "proxy_fitting_item_no",
                route.get("left_header_item_no"),
            )
            left_header_id = str(route.get("left_header_id", "")).strip()
            if left_header_id == str(lower_header_template["header_id"]):
                left_header_y = lower_header_y
                left_header_z = lower_header_z
                left_header_actual_dn = lower_header_dn
            elif left_header_id == str(upper_header_template["header_id"]):
                left_header_y = upper_header_y
                left_header_z = upper_header_z
                left_header_actual_dn = upper_header_dn
            else:
                raise KeyError(f"A-A route 左侧主管未匹配: {left_header_id}")
            left_header_y = resolve_section_route_position_y(
                section_route_entry,
                node_id="aa_left_header_circle",
                default=float(left_header_y),
            )

            right_header_id = str(route.get("right_header_id", "")).strip()
            if right_header_id == str(lower_header_template["header_id"]):
                right_header_y = lower_header_y
                right_header_z = lower_header_z
                right_header_actual_dn = lower_header_dn
            elif right_header_id == str(upper_header_template["header_id"]):
                right_header_y = upper_header_y
                right_header_z = upper_header_z
                right_header_actual_dn = upper_header_dn
            else:
                raise KeyError(f"A-A route 右侧主管未匹配: {right_header_id}")
            right_header_y = resolve_section_route_position_y(
                section_route_entry,
                node_id="aa_right_header_circle",
                default=float(right_header_y),
            )
            pump_tag = str(route.get("pump_tag", "")).strip()
            pump = pump_index.get(pump_tag)
            if pump is None:
                raise KeyError(f"A-A route 缺少泵锚点 {pump_tag}")
            pump_center_y = float(pump["center"][1])
            pump_half_width = float(pump["size_mm"][1]) / 2.0
            pump_left_edge_y = pump_center_y - pump_half_width

            support_id = str(route.get("pump_support_id", "")).strip()
            support_template = next(
                (proxy for proxy in layout.get("support_proxies_v2", []) if str(proxy.get("support_id")) == support_id),
                None,
            )
            if support_template is None:
                support_center = [
                    anchor_x,
                    resolve_section_route_position_y(
                        section_route_entry,
                        node_id="aa_p0204_support_29",
                        default=pump_left_edge_y - 1850.0,
                    ),
                    -400.0,
                ]
                support_size_mm = [280.0, 280.0, 600.0]
            else:
                support_center = [
                    anchor_x,
                    resolve_section_route_position_y(
                        section_route_entry,
                        node_id="aa_p0204_support_29",
                        default=float(support_template["center"][1]),
                    ),
                    float(support_template["center"][2]),
                ]
                support_size_mm = [float(value) for value in support_template["size_mm"]]

            left_horizontal_end_reference = str(route.get("left_top_horizontal_end_reference", "basin_inner_min_y")).strip()
            if left_horizontal_end_reference == "basin_inner_min_y":
                left_top_horizontal_end_y = basin_inner_min_y
            elif left_horizontal_end_reference == "basin_inner_max_y":
                left_top_horizontal_end_y = basin_inner_max_y
            else:
                left_top_horizontal_end_y = basin_inner_min_y
            left_top_horizontal_end_y = resolve_section_route_position_y(
                section_route_entry,
                point_id="aa_left_top_horizontal_target",
                default=float(left_top_horizontal_end_y),
            )
            pump_vertical_dn = int(
                resolve_section_route_pipe_run_value(
                    section_route_entry,
                    "aa_p0204_riser_dn300",
                    "dn",
                    route.get("pump_vertical_dn", 300),
                )
            )
            pump_vertical_top_elevation = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_p0204_elbow_19",
                    "elevation_mm",
                    route.get("pump_vertical_top_elevation_mm", 535.0),
                )
            )
            pump_top_elbow_item_no = resolve_section_route_node_value(
                section_route_entry,
                "aa_p0204_elbow_19",
                "item_no",
                route.get("pump_top_elbow_item_no"),
            )
            pump_valve_item_no = resolve_section_route_node_value(
                section_route_entry,
                "aa_p0204_valve_3",
                "item_no",
                route.get("pump_valve_item_no"),
            )
            pump_valve_structure_length = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_p0204_valve_3",
                    "structure_length_mm",
                    route.get("pump_valve_structure_length_mm", 83.0),
                )
            )
            pump_joint_11_length = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_p0204_joint_11",
                    "structure_length_mm",
                    route.get("pump_joint_11_length_mm", 370.0),
                )
            )
            pump_reducer_item_no = resolve_section_route_node_value(
                section_route_entry,
                "aa_p0204_reducer_25",
                "item_no",
                route.get("pump_reducer_item_no"),
            )
            pump_reducer_large_dn = int(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_p0204_reducer_25",
                    "dn_large",
                    route.get("pump_reducer_large_dn", 300),
                )
            )
            pump_reducer_small_dn = int(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_p0204_reducer_25",
                    "dn_small",
                    route.get("pump_reducer_small_dn", 250),
                )
            )
            pump_reducer_large_end_y = resolve_section_route_position_y(
                section_route_entry,
                point_id="aa_p0204_reducer_25_large_end",
                default=(
                    pump_left_edge_y - float(route.get("pump_reducer_large_end_offset_from_pump_left_edge_mm", 450.0))
                ),
            )
            pump_reducer_large_end_y = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_p0204_reducer_25",
                    "upstream_y_mm",
                    pump_reducer_large_end_y,
                )
            )
            pump_reducer_small_end_y = resolve_section_route_position_y(
                section_route_entry,
                point_id="aa_p0204_reducer_25_small_end",
                default=float(pump_reducer_large_end_y) + float(route.get("pump_reducer_length_mm", 300.0)),
            )
            pump_reducer_small_end_y = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_p0204_reducer_25",
                    "downstream_y_mm",
                    pump_reducer_small_end_y,
                )
            )
            pump_reducer_length = float(route.get("pump_reducer_length_mm", 300.0))
            if pump_reducer_small_end_y - pump_reducer_large_end_y > 1e-6:
                pump_reducer_length = pump_reducer_small_end_y - pump_reducer_large_end_y
            right_header_item_no = resolve_section_route_node_value(
                section_route_entry,
                "aa_right_header_circle",
                "proxy_fitting_item_no",
                route.get("right_header_item_no"),
            )
            right_branch_dn = int(
                resolve_section_route_pipe_run_value(
                    section_route_entry,
                    "aa_right_riser_dn250",
                    "dn",
                    route.get("right_branch_dn", 250),
                )
            )
            right_valve_item_no = resolve_section_route_node_value(
                section_route_entry,
                "aa_right_valve_8",
                "item_no",
                route.get("right_valve_item_no"),
            )
            right_valve_structure_length = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_right_valve_8",
                    "structure_length_mm",
                    route.get("right_valve_structure_length_mm", 76.0),
                )
            )
            right_riser_top_elevation = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_right_elbow_20",
                    "elevation_mm",
                    route.get("right_riser_top_elevation_mm", 510.0),
                )
            )
            right_elbow_item_no = resolve_section_route_node_value(
                section_route_entry,
                "aa_right_elbow_20",
                "item_no",
                route.get("right_elbow_item_no"),
            )
            right_vertical_bottom_elevation = float(
                resolve_section_route_pipe_run_value(
                    section_route_entry,
                    "aa_right_riser_dn250",
                    "to_elevation_mm",
                    route.get("right_vertical_bottom_elevation_mm", right_header_z),
                )
            )
            right_horizontal_start_y = resolve_section_route_position_y(
                section_route_entry,
                point_id="aa_right_horizontal_start",
                default=float(pump_center_y + pump_half_width),
            )
            right_horizontal_start_y = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_right_reducer_26",
                    "upstream_y_mm",
                    right_horizontal_start_y,
                )
            )
            right_item26_length = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_right_reducer_26",
                    "structure_length_mm",
                    route.get("right_item26_length_mm", 250.0),
                )
            )
            right_item26_large_end_y = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_right_reducer_26",
                    "downstream_y_mm",
                    right_horizontal_start_y + right_item26_length,
                )
            )
            if right_item26_large_end_y - right_horizontal_start_y > 1e-6:
                right_item26_length = right_item26_large_end_y - right_horizontal_start_y
            right_joint_12_length = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_right_joint_12",
                    "structure_length_mm",
                    route.get("right_joint_12_length_mm", 395.0),
                )
            )
            right_valve_1_structure_length = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "aa_right_valve_1",
                    "structure_length_mm",
                    route.get("right_valve_1_structure_length_mm", 725.0),
                )
            )
            support_item_no = resolve_section_route_node_value(
                section_route_entry,
                "aa_p0204_support_29",
                "item_no",
                route.get("support_item_no"),
            )

            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": str(route["route_id"]),
                    "route_kind": route_kind,
                    "section": section_name,
                    "anchor_x": anchor_x,
                    "upper_anchor_x_mm": float(route.get("upper_anchor_x_mm", anchor_x)),
                    "right_return_anchor_x_mm": float(route.get("right_return_anchor_x_mm", route.get("upper_anchor_x_mm", anchor_x))),
                    "draw_left_lower_branch": bool(route.get("draw_left_lower_branch", True)),
                    "draw_right_lower_branch": bool(route.get("draw_right_lower_branch", True)),
                    "draw_right_upper_branch": bool(route.get("draw_right_upper_branch", route.get("draw_right_lower_branch", True))),
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH),
                    "left_header_y": left_header_y,
                    "left_header_z": left_header_z,
                    "left_header_actual_dn": int(left_header_actual_dn),
                    "left_header_proxy_dn": int(route.get("left_header_proxy_dn", left_header_actual_dn)),
                    "left_header_item_no": left_header_item_no,
                    "left_branch_dn": left_branch_dn,
                    "left_valve_item_no": left_valve_item_no,
                    "left_valve_center_elevation_mm": left_valve_center_elevation,
                    "left_valve_structure_length_mm": left_valve_structure_length,
                    "left_valve_pn": float(route.get("left_valve_pn", 10.0)),
                    "left_top_horizontal_elevation_mm": left_top_horizontal_elevation,
                    "left_top_elbow_item_no": left_top_elbow_item_no,
                    "left_top_elbow_rotation_deg": float(route.get("left_top_elbow_rotation_deg", 90.0)),
                    "left_top_elbow_rotation_axis": str(route.get("left_top_elbow_rotation_axis", "y")).strip().lower(),
                    "left_top_elbow_secondary_rotation_deg": float(
                        route.get("left_top_elbow_secondary_rotation_deg", 0.0)
                    ),
                    "left_top_elbow_secondary_rotation_axis": str(
                        route.get("left_top_elbow_secondary_rotation_axis", "x")
                    ).strip().lower(),
                    "left_top_horizontal_end_y": float(left_top_horizontal_end_y),
                    "pump_tag": pump_tag,
                    "pump_support_center": support_center,
                    "pump_support_size_mm": support_size_mm,
                    "pump_support_item_no": support_item_no,
                    "pump_vertical_dn": pump_vertical_dn,
                    "pump_vertical_y": resolve_section_route_position_y(
                        section_route_entry,
                        node_id="aa_p0204_elbow_19",
                        default=float(support_center[1]),
                    ),
                    "pump_vertical_bottom_elevation_mm": float(route.get("pump_vertical_bottom_elevation_mm", 0.0)),
                    "pump_vertical_top_elevation_mm": pump_vertical_top_elevation,
                    "pump_top_elbow_item_no": pump_top_elbow_item_no,
                    "pump_top_elbow_rotation_deg": float(route.get("pump_top_elbow_rotation_deg", -90.0)),
                    "pump_top_elbow_rotation_axis": str(route.get("pump_top_elbow_rotation_axis", "y")).strip().lower(),
                    "pump_top_elbow_secondary_rotation_deg": float(route.get("pump_top_elbow_secondary_rotation_deg", 180.0)),
                    "pump_top_elbow_secondary_rotation_axis": str(
                        route.get("pump_top_elbow_secondary_rotation_axis", "z")
                    ).strip().lower(),
                    "aa_left_flow_sequence": resolve_section_route_flow_path_sequence(
                        section_route_entry,
                        path_id="aa_left_side_chain",
                    ),
                    "aa_pump_flow_sequence": resolve_section_route_flow_path_sequence(
                        section_route_entry,
                        path_id="aa_p0204_left_chain",
                    ),
                    "aa_right_flow_sequence": resolve_section_route_flow_path_sequence(
                        section_route_entry,
                        path_id="aa_right_return_chain",
                    ),
                    "pump_valve_item_no": pump_valve_item_no,
                    "pump_valve_center_y": resolve_section_route_position_y(
                        section_route_entry,
                        node_id="aa_p0204_valve_3",
                        default=float(support_center[1]) + float(route.get("pump_valve_center_offset_from_support_mm", 850.0)),
                    ),
                    "pump_valve_structure_length_mm": pump_valve_structure_length,
                    "pump_joint_11_length_mm": pump_joint_11_length,
                    "pump_valve_pn": float(route.get("pump_valve_pn", 10.0)),
                    "pump_reducer_item_no": pump_reducer_item_no,
                    "pump_reducer_large_dn": pump_reducer_large_dn,
                    "pump_reducer_small_dn": pump_reducer_small_dn,
                    "pump_reducer_length_mm": pump_reducer_length,
                    "pump_reducer_large_end_y": float(pump_reducer_large_end_y),
                    "pump_reducer_small_end_y": float(pump_reducer_small_end_y),
                    "right_header_y": right_header_y,
                    "right_header_z": right_header_z,
                    "right_header_actual_dn": int(right_header_actual_dn),
                    "right_header_proxy_dn": int(route.get("right_header_proxy_dn", right_header_actual_dn)),
                    "right_header_item_no": right_header_item_no,
                    "right_branch_dn": right_branch_dn,
                    "right_horizontal_start_y": float(right_horizontal_start_y),
                    "right_item26_length_mm": right_item26_length,
                    "right_item26_large_end_y": right_item26_large_end_y,
                    "right_joint_12_length_mm": right_joint_12_length,
                    "right_valve_1_structure_length_mm": right_valve_1_structure_length,
                    "right_valve_item_no": right_valve_item_no,
                    "right_valve_center_y": resolve_section_route_position_y(
                        section_route_entry,
                        node_id="aa_right_valve_8",
                        default=(
                            right_header_y - float(route.get("right_valve_center_offset_from_right_header_mm", 900.0))
                        ),
                    ),
                    "right_valve_structure_length_mm": right_valve_structure_length,
                    "right_valve_pn": float(route.get("right_valve_pn", 10.0)),
                    "right_riser_top_elevation_mm": right_riser_top_elevation,
                    "right_vertical_bottom_elevation_mm": right_vertical_bottom_elevation,
                    "right_elbow_item_no": right_elbow_item_no,
                    "right_elbow_rotation_deg": float(route.get("right_elbow_rotation_deg", -90.0)),
                    "right_elbow_rotation_axis": str(route.get("right_elbow_rotation_axis", "y")).strip().lower(),
                    "draw_fittings": bool(route.get("draw_fittings", True)),
                    "segments": [],
                    "basis": aa_route_basis,
                    "modeling_description": aa_route_basis,
                }
            )
            continue
        if route_kind == "aa_dn200_plan_external_connection":
            source_aa_route_id = str(route.get("source_aa_route_id", "")).strip()
            if not source_aa_route_id:
                raise KeyError("aa_dn200_plan_external_connection 缺少 source_aa_route_id。")
            source_aa_route = require_bundle(layout, "section_preview_routes", "route_id", source_aa_route_id)
            source_x_reference = str(route.get("source_x_reference", "anchor_x_mm")).strip()
            if source_x_reference and source_x_reference in source_aa_route:
                source_anchor_x = float(source_aa_route[source_x_reference])
            elif "anchor_x_mm" in source_aa_route:
                source_anchor_x = float(source_aa_route["anchor_x_mm"])
            elif "anchor_x" in source_aa_route:
                source_anchor_x = float(source_aa_route["anchor_x"])
            else:
                raise KeyError(
                    f"{source_aa_route_id} 缺少 {source_x_reference}/anchor_x_mm/anchor_x，无法派生 DN200 外接链列位。"
                )
            connection_x = source_anchor_x + float(route.get("connection_x_offset_from_source_anchor_mm", 0.0))
            turn_up_y = resolve_section_route_position_y(
                section_route_entry,
                node_id=str(route.get("turn_up_y_source_node_id", "aa_p0204_valve_3")),
                default=10650.0,
            )
            if route.get("target_y_mm") is not None:
                target_y = float(route["target_y_mm"])
            else:
                target_inner_wall_side = str(route.get("target_inner_wall_side", "bottom")).strip().lower()
                target_clearance = float(route.get("target_clearance_from_inner_wall_mm", 600.0))
                if target_inner_wall_side == "bottom":
                    target_y = basin_inner_min_y + target_clearance
                elif target_inner_wall_side == "top":
                    target_y = basin_inner_max_y - target_clearance
                else:
                    raise ValueError(f"aa_dn200_plan_external_connection 未识别的 target_inner_wall_side: {target_inner_wall_side}")
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": str(route["route_id"]),
                    "route_kind": route_kind,
                    "section": section_name,
                    "anchor_x": float(connection_x),
                    "base_y": float(upper_header_y),
                    "base_z": float(upper_header_z),
                    "branch_dn": int(route.get("branch_dn", 200)),
                    "tee_proxy_run_dn": int(route.get("tee_proxy_run_dn", upper_header_dn)),
                    "turn_up_y": float(turn_up_y),
                    "top_elevation_mm": float(route.get("top_elevation_mm", 535.0)),
                    "target_y": float(target_y),
                    "drop_end_elevation_mm": float(route.get("drop_end_elevation_mm", -1400.0)),
                    "through_embedded_plate_id": route.get("through_embedded_plate_id"),
                    "target_opening_id": route.get("target_opening_id"),
                    "tee_item_no": route.get("tee_item_no"),
                    "elbow_item_no": route.get("elbow_item_no"),
                    "draw_fittings": bool(route.get("draw_fittings", True)),
                    "basis": str(route.get("basis", "")),
                }
            )
            continue
        if route_kind == "gg_dn100_local_loop":
            left_side_main_template = require_bundle(layout, "auxiliary_routes", "route_id", "left_side_dn300_main")
            left_side_main_segment = list(left_side_main_template.get("segments", []))[0]
            branch_dn = int(
                resolve_section_route_pipe_run_value(
                    section_route_entry,
                    "gg_left_vertical_dn100",
                    "dn",
                    route["branch_dn"],
                )
            )
            right_riser_dn = int(
                resolve_section_route_pipe_run_value(
                    section_route_entry,
                    "gg_right_riser_dn250",
                    "dn",
                    route.get("right_riser_dn", 250),
                )
            )
            left_vertical_offset = resolve_section_route_linear_dimension(
                section_route_entry,
                meaning_contains="水平距离",
                default=float(route.get("left_vertical_center_offset_from_right_riser_mm", 0.0)),
            )
            left_vertical_y = base_y - left_vertical_offset
            valve_structure_length = float(route.get("valve_structure_length_mm", 0.0))
            valve_half_length = valve_structure_length / 2.0 if valve_structure_length > 0 else 0.0
            valve_center_y = base_y - float(
                route.get("valve_center_offset_from_right_riser_mm", left_vertical_offset / 2.0)
            )
            top_horizontal_elevation = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "gg_top_elbow_24",
                    "elevation_mm",
                    route["top_horizontal_elevation_mm"],
                )
            )
            bottom_horizontal_elevation = float(
                resolve_section_route_node_value(
                    section_route_entry,
                    "gg_bottom_elbow_24",
                    "elevation_mm",
                    route["bottom_horizontal_elevation_mm"],
                )
            )
            bottom_stub_length = resolve_section_route_linear_dimension(
                section_route_entry,
                meaning_contains="短 DN100 横管长度",
                default=float(route.get("bottom_stub_length_mm", 0.0)),
            )
            draw_bottom_stub = bool(route.get("draw_bottom_stub", True))
            branch_bend_radius = PIPE_OD[branch_dn] * 1.5
            top_horizontal_left_start_y = left_vertical_y + branch_bend_radius
            top_horizontal_left_end_y = valve_center_y - valve_half_length
            top_horizontal_right_start_y = valve_center_y + valve_half_length
            left_vertical_start_z = bottom_horizontal_elevation + (branch_bend_radius if draw_bottom_stub else 0.0)
            left_vertical_end_z = top_horizontal_elevation - branch_bend_radius
            bottom_stub_start_y = left_vertical_y - branch_bend_radius - bottom_stub_length
            bottom_stub_end_y = left_vertical_y - branch_bend_radius
            segments: list[dict[str, Any]] = []
            header_tee_branch_center_to_end = float(route.get("header_tee_branch_center_to_end_mm", 0.0))
            header_tee_branch_display_offset, _ = resolve_tee_display_offsets_mm(
                run_dn=int(route.get("header_tee_proxy_run_dn", base_header_dn)),
                branch_center_to_end_mm=header_tee_branch_center_to_end,
            )
            resolved_riser_tee = resolve_tee_center_to_end_gb12459(
                run_dn=int(route.get("riser_tee_run_dn", right_riser_dn)),
                branch_dn=int(route.get("riser_tee_branch_dn", branch_dn)),
            )
            riser_tee_run_center_to_end = float(resolved_riser_tee[0]) if resolved_riser_tee else 0.0
            riser_tee_branch_center_to_end = float(resolved_riser_tee[1]) if resolved_riser_tee else 0.0
            riser_tee_branch_display_offset, riser_tee_run_display_half_length = resolve_tee_display_offsets_mm(
                run_dn=int(route.get("riser_tee_run_dn", right_riser_dn)),
                branch_center_to_end_mm=riser_tee_branch_center_to_end,
                run_center_to_end_mm=riser_tee_run_center_to_end,
            )
            top_horizontal_right_end_y = base_y - riser_tee_branch_display_offset
            if top_horizontal_elevation - (base_z + header_tee_branch_display_offset) > 1e-6:
                segments.append(
                    {
                        "start": [anchor_x, base_y, base_z + header_tee_branch_display_offset],
                        "end": [anchor_x, base_y, top_horizontal_elevation - riser_tee_run_display_half_length],
                    }
                )
            if left_vertical_end_z - left_vertical_start_z > 1e-6:
                segments.append(
                    {
                        "start": [anchor_x, left_vertical_y, left_vertical_start_z],
                        "end": [anchor_x, left_vertical_y, left_vertical_end_z],
                    }
                )
            if top_horizontal_left_end_y - top_horizontal_left_start_y > 1e-6:
                segments.append(
                    {
                        "start": [anchor_x, top_horizontal_left_start_y, top_horizontal_elevation],
                        "end": [anchor_x, top_horizontal_left_end_y, top_horizontal_elevation],
                    }
                )
            if top_horizontal_right_end_y - top_horizontal_right_start_y > 1e-6:
                segments.append(
                    {
                        "start": [anchor_x, top_horizontal_right_start_y, top_horizontal_elevation],
                        "end": [anchor_x, top_horizontal_right_end_y, top_horizontal_elevation],
                    }
                )
            if draw_bottom_stub and bottom_stub_end_y - bottom_stub_start_y > 1e-6:
                segments.append(
                    {
                        "start": [anchor_x, bottom_stub_start_y, bottom_horizontal_elevation],
                        "end": [anchor_x, bottom_stub_end_y, bottom_horizontal_elevation],
                    }
                )
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": str(route["route_id"]),
                    "route_kind": route_kind,
                    "section": section_name,
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH),
                    "dn": branch_dn,
                    "right_riser_dn": right_riser_dn,
                    "anchor_x": anchor_x,
                    "base_y": base_y,
                    "base_z": base_z,
                    "base_header_dn": int(base_header_dn),
                    "left_vertical_y": left_vertical_y,
                    "top_horizontal_elevation_mm": top_horizontal_elevation,
                    "bottom_horizontal_elevation_mm": bottom_horizontal_elevation,
                    "bottom_stub_length_mm": bottom_stub_length,
                    "draw_bottom_stub": draw_bottom_stub,
                    "header_tee_branch_center_to_end_mm": header_tee_branch_center_to_end,
                    "header_tee_branch_display_offset_mm": header_tee_branch_display_offset,
                    "header_tee_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "gg_header_circle",
                        "proxy_fitting_item_no",
                        route.get("header_tee_item_no"),
                    ),
                    "header_tee_proxy_run_dn": int(route.get("header_tee_proxy_run_dn", base_header_dn)),
                    "header_tee_specification": route.get("header_tee_specification"),
                    "riser_tee_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "gg_riser_tee_17",
                        "item_no",
                        route.get("riser_tee_item_no"),
                    ),
                    "riser_tee_run_dn": int(route.get("riser_tee_run_dn", right_riser_dn)),
                    "riser_tee_branch_dn": int(route.get("riser_tee_branch_dn", branch_dn)),
                    "riser_tee_branch_display_offset_mm": riser_tee_branch_display_offset,
                    "riser_tee_run_display_half_length_mm": riser_tee_run_display_half_length,
                    "valve_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "gg_valve_7",
                        "item_no",
                        route.get("valve_item_no"),
                    ),
                    "valve_pn": float(route.get("valve_pn", 10.0)),
                    "valve_structure_length_mm": valve_structure_length,
                    "valve_center_y": valve_center_y,
                    "elbow_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "gg_top_elbow_24",
                        "item_no",
                        route.get("elbow_item_no"),
                    ),
                    "top_elbow_rotation_deg": float(route.get("top_elbow_rotation_deg", -90.0)),
                    "top_elbow_rotation_axis": str(route.get("top_elbow_rotation_axis", "y")).strip().lower(),
                    "top_elbow_secondary_rotation_deg": float(route.get("top_elbow_secondary_rotation_deg", 180.0)),
                    "top_elbow_secondary_rotation_axis": str(
                        route.get("top_elbow_secondary_rotation_axis", "z")
                    ).strip().lower(),
                    "bottom_elbow_rotation_deg": float(route.get("bottom_elbow_rotation_deg", 90.0)),
                    "bottom_elbow_rotation_axis": str(route.get("bottom_elbow_rotation_axis", "y")).strip().lower(),
                    "bottom_elbow_secondary_rotation_deg": float(route.get("bottom_elbow_secondary_rotation_deg", 0.0)),
                    "bottom_elbow_secondary_rotation_axis": str(
                        route.get("bottom_elbow_secondary_rotation_axis", "x")
                    ).strip().lower(),
                    "draw_fittings": bool(route.get("draw_fittings", False)),
                    "segments": segments,
                    "basis": str(route.get("basis", "")),
                }
            )
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": f"{route['route_id']}_plan_external",
                    "section": section_name,
                    "route_kind": "gg_dn100_plan_external_connection",
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH),
                    "dn": branch_dn,
                    "anchor_x": anchor_x,
                    "base_header_y": float(base_y),
                    "left_vertical_y": left_vertical_y,
                    "bottom_horizontal_elevation_mm": bottom_horizontal_elevation,
                    "outlet_length_mm": float(route.get("plan_external_outlet_length_mm", bottom_stub_length)),
                    "target_main_x": float(left_side_main_segment["start"][0]),
                    "second_elbow_offset_from_base_header_mm": float(
                        route.get("plan_external_second_elbow_offset_from_base_header_mm", 1500.0)
                    ),
                    "elbow_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "gg_bottom_elbow_24",
                        "item_no",
                        route.get("elbow_item_no"),
                    ),
                    "draw_fittings": bool(route.get("draw_fittings", False)),
                    "basis": (
                        f"{route.get('basis', '').strip()} "
                        "回到整体后，G-G 左侧 DN100 立管底部通过弯头沿 -X 接出 DN100 横管；"
                        "当前外接横管先按剖面里可稳定读取的 500 长度执行。"
                    ).strip(),
                }
            )
            continue
        if route_kind == "bb_dn100_bottom_elbow_preview":
            bb_branch_dn = int(route.get("branch_dn", 100))
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": str(route["route_id"]),
                    "section": section_name,
                    "route_kind": route_kind,
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                    "dn": bb_branch_dn,
                    "anchor_x": float(anchor_x),
                    "base_y": base_y,
                    "base_z": base_z,
                    "section_plane_y": float(route.get("section_plane_y_mm", base_y)),
                    "top_elevation_mm": float(route.get("top_elevation_mm", 2050.0)),
                    "bottom_elevation_mm": float(route.get("bottom_elevation_mm", -500.0)),
                    "top_horizontal_elevation_mm": float(route.get("top_horizontal_elevation_mm", 1900.0)),
                    "top_horizontal_length_mm": float(route.get("top_horizontal_length_mm", 700.0)),
                    "right_elbow_connection_trim_mm": (
                        float(route["right_elbow_connection_trim_mm"])
                        if route.get("right_elbow_connection_trim_mm") is not None
                        else None
                    ),
                    "right_drop_bottom_elevation_mm": float(
                        route.get("right_drop_bottom_elevation_mm", 1900.0)
                    ),
                    "elbow_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "bb_left_turn_24",
                        "item_no",
                        route.get("elbow_item_no"),
                    ),
                    "valve_7_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "bb_valve_7",
                        "item_no",
                        route.get("valve_7_item_no", 7),
                    ),
                    "valve_2_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "bb_valve_2",
                        "item_no",
                        route.get("valve_2_item_no", 2),
                    ),
                    "valve_2_connection_type": str(route.get("valve_2_connection_type", "threaded")).strip().lower(),
                    "valve_7_pn": float(route.get("valve_7_pn", 10.0)),
                    "valve_2_pn": float(route.get("valve_2_pn", 10.0)),
                    "draw_fittings": bool(route.get("draw_fittings", True)),
                    "basis": str(route.get("basis", "")),
                }
            )
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": f"{route['route_id']}_plan_external",
                    "section": section_name,
                    "route_kind": "bb_dn100_plan_external_connection",
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                    "dn": bb_branch_dn,
                    "anchor_x": float(anchor_x),
                    "section_plane_y": float(route.get("section_plane_y_mm", base_y)),
                    "bottom_elevation_mm": float(route.get("bottom_elevation_mm", -500.0)),
                    "base_header_y": float(base_y),
                    "plan_external_target_offset_from_base_header_mm": float(
                        route.get("plan_external_target_offset_from_base_header_mm", 4000.0)
                    ),
                    "elbow_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "bb_left_turn_24",
                        "item_no",
                        route.get("elbow_item_no"),
                    ),
                    "draw_fittings": bool(route.get("draw_fittings", True)),
                    "basis": (
                        f"{route.get('basis', '').strip()} "
                        "回到整体后，B-B 左侧 DN100 立管底部通过弯头沿 -Y 接出 DN100 横管，"
                        "终点取下方 DN500 主管所在 y 再向下 4000。"
                    ).strip(),
                }
            )
            continue
        if route_kind == "dd_dn200_turn_preview":
            dd_route_basis = resolve_section_route_modeling_description(
                section_route_entry,
                default=str(route.get("basis", "")),
            )
            dd_text_overrides = resolve_dd_route_text_overrides(section_route_entry)
            dd_anchor_x = float(dd_text_overrides.get("anchor_x_mm", anchor_x))
            pump_tag = str(route.get("pump_tag", "")).strip()
            pump = pump_index.get(pump_tag) if pump_tag else None
            pump_center_y = float(pump["center"][1]) if pump is not None else float(base_y)
            pump_half_width_local = float(pump["size_mm"][1]) / 2.0 if pump is not None else float(pump_half_width)
            equipment_side_y = float(route.get("equipment_side_y_mm", pump_center_y - pump_half_width_local))
            branch_dn = int(
                dd_text_overrides.get(
                    "branch_dn",
                    resolve_section_route_pipe_run_value(
                        section_route_entry,
                        "dd_local_riser_dn200",
                        "dn",
                        route.get("branch_dn", 200),
                    ),
                )
            )
            bottom_elevation = float(
                dd_text_overrides.get(
                    "bottom_elevation_mm",
                    route.get(
                        "bottom_elevation_mm",
                        resolve_section_route_pipe_run_value(
                            section_route_entry,
                            "dd_local_riser_dn200",
                            "to_elevation_mm",
                            400.0,
                        ),
                    ),
                )
            )
            top_elevation = float(
                dd_text_overrides.get(
                    "top_elevation_mm",
                    route.get(
                        "top_elevation_mm",
                        resolve_section_route_pipe_run_value(
                            section_route_entry,
                            "dd_local_riser_dn200",
                            "from_elevation_mm",
                            1650.0,
                        ),
                    ),
                )
            )
            horizontal_offset_length = float(
                dd_text_overrides.get(
                    "horizontal_offset_length_mm",
                    resolve_section_route_linear_dimension(
                        section_route_entry,
                        meaning_contains="水平偏移",
                        default=float(route.get("horizontal_offset_length_mm", 650.0)),
                    ),
                )
            )
            equipment_short_connect_length = float(
                dd_text_overrides.get(
                    "equipment_short_connect_length_mm",
                    resolve_section_route_linear_dimension(
                        section_route_entry,
                        meaning_contains="短接长度",
                        default=float(route.get("equipment_short_connect_length_mm", 1000.0)),
                    ),
                )
            )
            sleeve_offset_from_upper_elbow = float(
                dd_text_overrides.get(
                    "sleeve_offset_from_upper_elbow_mm",
                    route.get("sleeve_offset_from_upper_elbow_mm", horizontal_offset_length),
                )
            )
            elbow_corner_distance = float(
                dd_text_overrides.get(
                    "elbow_corner_distance_mm",
                    route.get("elbow_corner_distance_mm", horizontal_offset_length + equipment_short_connect_length),
                )
            )
            draw_visible_equipment_stub = bool(
                dd_text_overrides.get(
                    "draw_visible_equipment_stub",
                    route.get("draw_visible_equipment_stub", False),
                )
            )
            elbow_connection_trim = resolve_lr_elbow_connection_trim_mm(
                route.get("upper_elbow_connection_trim_mm"),
                branch_dn,
            )
            vertical_direction = str(
                dd_text_overrides.get("vertical_direction", route.get("vertical_direction", "up"))
            ).strip().lower()
            horizontal_elevation = float(
                dd_text_overrides.get(
                    "horizontal_elevation_mm",
                    route.get(
                        "horizontal_elevation_mm",
                        bottom_elevation if vertical_direction == "down" else top_elevation,
                    ),
                )
            )
            riser_start_elevation = float(
                dd_text_overrides.get(
                    "riser_start_elevation_mm",
                    route.get(
                        "riser_start_elevation_mm",
                        top_elevation if vertical_direction == "down" else bottom_elevation,
                    ),
                )
            )
            horizontal_pass_through_reference = str(
                dd_text_overrides.get(
                    "horizontal_pass_through_reference",
                    route.get("horizontal_pass_through_reference", ""),
                )
            ).strip()
            horizontal_pass_through_y = dd_text_overrides.get(
                "horizontal_pass_through_y_mm",
                route.get("horizontal_pass_through_y_mm"),
            )
            if horizontal_pass_through_y is None and horizontal_pass_through_reference:
                sleeve_bundle = wall_sleeve_index.get(horizontal_pass_through_reference)
                if sleeve_bundle is not None:
                    horizontal_pass_through_y = float(sleeve_bundle["center"][1])
            horizontal_pass_through_y = (
                float(horizontal_pass_through_y) if horizontal_pass_through_y is not None else None
            )
            if horizontal_pass_through_y is not None:
                upper_elbow_y = horizontal_pass_through_y - sleeve_offset_from_upper_elbow
                lower_elbow_y = upper_elbow_y + elbow_corner_distance
                equipment_side_y = lower_elbow_y
            else:
                upper_elbow_y = equipment_side_y - elbow_corner_distance
                lower_elbow_y = equipment_side_y
            segments: list[dict[str, Any]] = []
            vertical_segment_start_z = riser_start_elevation
            vertical_segment_end_z = (
                horizontal_elevation + elbow_connection_trim
                if vertical_direction == "down"
                else horizontal_elevation - elbow_connection_trim
            )
            if abs(vertical_segment_start_z - vertical_segment_end_z) > 1e-6:
                segments.append(
                    {
                        "start": [dd_anchor_x, upper_elbow_y, vertical_segment_start_z],
                        "end": [dd_anchor_x, upper_elbow_y, vertical_segment_end_z],
                    }
                )
            horizontal_start_y = upper_elbow_y + elbow_connection_trim
            horizontal_end_y = lower_elbow_y - elbow_connection_trim
            if horizontal_end_y - horizontal_start_y > 1e-6:
                segments.append(
                    {
                        "start": [dd_anchor_x, horizontal_start_y, horizontal_elevation],
                        "end": [dd_anchor_x, horizontal_end_y, horizontal_elevation],
                    }
                )
            equipment_short_start_x = dd_anchor_x - elbow_connection_trim
            equipment_short_end_x = equipment_short_start_x + equipment_short_connect_length
            if draw_visible_equipment_stub and equipment_short_end_x - equipment_short_start_x > 1e-6:
                segments.append(
                    {
                        "start": [equipment_short_start_x, lower_elbow_y, horizontal_elevation],
                        "end": [equipment_short_end_x, lower_elbow_y, horizontal_elevation],
                    }
                )
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": str(route["route_id"]),
                    "section": section_name,
                    "route_kind": route_kind,
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                    "pump_tag": pump_tag,
                    "dn": branch_dn,
                    "anchor_x": dd_anchor_x,
                    "base_y": base_y,
                    "base_z": base_z,
                    "base_header_dn": int(base_header_dn),
                    "equipment_side_y": equipment_side_y,
                    "upper_elbow_y": upper_elbow_y,
                    "lower_elbow_y": lower_elbow_y,
                    "horizontal_pass_through_y_mm": horizontal_pass_through_y,
                    "vertical_direction": vertical_direction,
                    "horizontal_elevation_mm": horizontal_elevation,
                    "riser_start_elevation_mm": riser_start_elevation,
                    "bottom_elevation_mm": bottom_elevation,
                    "top_elevation_mm": top_elevation,
                    "horizontal_offset_length_mm": horizontal_offset_length,
                    "sleeve_offset_from_upper_elbow_mm": sleeve_offset_from_upper_elbow,
                    "elbow_corner_distance_mm": elbow_corner_distance,
                    "equipment_short_connect_length_mm": equipment_short_connect_length,
                    "draw_visible_equipment_stub": draw_visible_equipment_stub,
                    "draw_header_tee": bool(route.get("draw_header_tee", False)),
                    "header_tee_item_no": route.get("header_tee_item_no"),
                    "header_tee_run_dn": int(route.get("header_tee_run_dn", base_header_dn)),
                    "header_tee_branch_axis": str(route.get("header_tee_branch_axis", "-y")).strip().lower(),
                    "upper_elbow_item_no": dd_text_overrides.get(
                        "upper_elbow_item_no",
                        resolve_section_route_node_value(
                            section_route_entry,
                            "dd_upper_elbow_21",
                            "item_no",
                            route.get("upper_elbow_item_no"),
                        ),
                    ),
                    "upper_elbow_rotation_deg": float(route.get("upper_elbow_rotation_deg", -90.0)),
                    "upper_elbow_rotation_axis": str(route.get("upper_elbow_rotation_axis", "x")).strip().lower(),
                    "upper_elbow_secondary_rotation_deg": float(
                        route.get("upper_elbow_secondary_rotation_deg", -90.0)
                    ),
                    "upper_elbow_secondary_rotation_axis": str(
                        route.get("upper_elbow_secondary_rotation_axis", "z")
                    ).strip().lower(),
                    "lower_elbow_item_no": dd_text_overrides.get(
                        "lower_elbow_item_no",
                        resolve_section_route_node_value(
                            section_route_entry,
                            "dd_lower_elbow_21",
                            "item_no",
                            route.get("lower_elbow_item_no"),
                        ),
                    ),
                    "lower_elbow_rotation_deg": float(route.get("lower_elbow_rotation_deg", -90.0)),
                    "lower_elbow_rotation_axis": str(route.get("lower_elbow_rotation_axis", "y")).strip().lower(),
                    "lower_elbow_secondary_rotation_deg": float(
                        route.get("lower_elbow_secondary_rotation_deg", 0.0)
                    ),
                    "lower_elbow_secondary_rotation_axis": str(
                        route.get("lower_elbow_secondary_rotation_axis", "x")
                    ).strip().lower(),
                    "orifice_item_no": dd_text_overrides.get(
                        "orifice_item_no",
                        resolve_section_route_node_value(
                            section_route_entry,
                            "dd_orifice_28",
                            "item_no",
                            route.get("orifice_item_no"),
                        ),
                    ),
                    "sleeve_item_no": dd_text_overrides.get(
                        "sleeve_item_no",
                        resolve_section_route_node_value(
                            section_route_entry,
                            "dd_sleeve_31",
                            "item_no",
                            route.get("sleeve_item_no"),
                        ),
                    ),
                    "draw_fittings": bool(route.get("draw_fittings", True)),
                    "segments": segments,
                    "basis": dd_route_basis,
                    "execution_text": dd_text_overrides.get("execution_text"),
                }
            )
            continue
        if route_kind == "ff_dn150_local_return_preview":
            ff_route_basis = resolve_section_route_modeling_description(
                section_route_entry,
                default=str(route.get("basis", "")),
            )
            ff_branch_dn = int(
                resolve_section_route_pipe_run_value(
                    section_route_entry,
                    "ff_left_riser_dn150",
                    "dn",
                    route.get("branch_dn", 150),
                )
            )
            ff_top_horizontal_length = float(
                resolve_section_route_pipe_run_value(
                    section_route_entry,
                    "ff_top_return_dn150",
                    "length_mm",
                    route.get("top_horizontal_length_mm", 0.0),
                )
            )
            ff_top_horizontal_center_x = float(route.get("top_horizontal_center_x_mm", anchor_x))
            ff_left_vertical_x = float(anchor_x)
            if ff_top_horizontal_length > 1e-6:
                ff_left_vertical_x = (
                    ff_top_horizontal_center_x
                    - ff_top_horizontal_length / 2.0
                    - resolve_lr_elbow_connection_trim_mm(None, ff_branch_dn)
                )
            y0202_bundle = auxiliary_equipment_index.get("Y-0202")
            if y0202_bundle is None:
                raise KeyError("auxiliary_equipments 缺少 Y-0202，无法为 F-F 回算平面外接管定位。")
            ff_spine_template = require_bundle(layout, "auxiliary_routes", "route_id", "y0202_right_dn150_spine")
            ff_spine_segments = list(ff_spine_template.get("segments", []))
            if not ff_spine_segments:
                raise ValueError("y0202_right_dn150_spine 缺少 segments，无法为 F-F 读取 DN150 母线列位。")
            ff_spine_x = float(ff_spine_segments[0]["start"][0])
            y0202_top_y = float(y0202_bundle["top_y_mm"])
            y0202_bottom_y = float(y0202_bundle["bottom_y_mm"])
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": str(route["route_id"]),
                    "section": section_name,
                    "route_kind": route_kind,
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                    "dn": ff_branch_dn,
                    "anchor_x": ff_left_vertical_x,
                    "base_y": base_y,
                    "base_z": base_z,
                    "left_vertical_y": float(route.get("left_vertical_y_mm", 5400.0)),
                    "return_span_mm": float(
                        resolve_section_route_linear_dimension(
                            section_route_entry,
                            meaning_contains="中心线间距",
                            default=float(route.get("return_span_mm", 600.0)),
                        )
                    ),
                    "bottom_elevation_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "ff_bottom_tee_18",
                            "elevation_mm",
                            route.get("bottom_elevation_mm", -600.0),
                        )
                    ),
                    "top_elevation_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "ff_top_left_elbow_22",
                            "elevation_mm",
                            route.get("top_elevation_mm", 2150.0),
                        )
                    ),
                    "return_drop_bottom_elevation_mm": float(
                        resolve_section_route_pipe_run_value(
                            section_route_entry,
                            "ff_return_drop_dn150",
                            "to_virtual_target_elevation_mm",
                            route.get("return_drop_bottom_elevation_mm", 1500.0),
                        )
                    ),
                    "top_horizontal_length_mm": ff_top_horizontal_length,
                    "top_horizontal_center_x_mm": ff_top_horizontal_center_x,
                    "left_stub_length_mm": float(route.get("left_stub_length_mm", 250.0)),
                    "tee_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "ff_bottom_tee_18",
                        "item_no",
                        route.get("tee_item_no"),
                    ),
                    "elbow_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "ff_top_left_elbow_22",
                        "item_no",
                        route.get("elbow_item_no"),
                    ),
                    "draw_fittings": bool(route.get("draw_fittings", True)),
                    "basis": ff_route_basis,
                }
            )
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": f"{route['route_id']}_plan_external",
                    "section": section_name,
                    "route_kind": "ff_dn150_plan_external_connection",
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                    "dn": int(
                        resolve_section_route_pipe_run_value(
                            section_route_entry,
                            "ff_left_riser_dn150",
                            "dn",
                            route.get("branch_dn", 150),
                        )
                    ),
                    "section_anchor_x_mm": ff_left_vertical_x,
                    "spine_x_mm": ff_spine_x,
                    "tee_center_y_mm": float(route.get("left_vertical_y_mm", 5400.0)),
                    "top_turn_y_mm": y0202_top_y + 300.0,
                    "bottom_turn_y_mm": y0202_bottom_y - 300.0,
                    "elevation_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "ff_bottom_tee_18",
                            "elevation_mm",
                            route.get("bottom_elevation_mm", -600.0),
                        )
                    ),
                    "tee_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "ff_bottom_tee_18",
                        "item_no",
                        route.get("tee_item_no"),
                    ),
                    "elbow_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "ff_top_left_elbow_22",
                        "item_no",
                        route.get("elbow_item_no"),
                    ),
                    "draw_fittings": bool(route.get("draw_fittings", True)),
                    "basis": (
                        f"{ff_route_basis}；本条为由平面图直接控制、并与 F-F 底部三通相连的外接 DN150 管链："
                        "沿 y 方向上下延伸到 Y-0202 外轮廓上/下各外侧 300 后，通过弯头转向 -x 接到现有 DN150 母线。"
                    ),
                }
            )
            continue
        if route_kind == "cc_dn200_left_wall_section":
            cc_route_basis = resolve_section_route_modeling_description(
                section_route_entry,
                default=str(route.get("basis", "")),
            )
            anchor_bundle = wall_sleeve_index.get(anchor_reference)
            if anchor_bundle is None:
                raise KeyError(f"C-C 缺少左墙套管锚点 {anchor_reference}")
            wall_center_y = float(
                route.get(
                    "wall_center_y_mm",
                    anchor_bundle["center"][1],
                )
            )
            center_elevation = float(
                route.get(
                    "center_elevation_mm",
                    resolve_section_route_node_value(
                        section_route_entry,
                        "cc_wall_sleeve_31",
                        "center_elevation_mm",
                        anchor_bundle["center"][2],
                    ),
                )
            )
            hh_template = require_bundle(layout, "section_preview_routes", "route_id", "hh_preview_left_tower_drop")
            hh_anchor_x = float(hh_template.get("anchor_x_mm", anchor_x))
            hh_lower_outlet_length = float(hh_template.get("lower_outlet_length_mm", 250.0))
            hh_dn = int(hh_template.get("branch_dn", 125))
            hh_elbow_trim = resolve_lr_elbow_connection_trim_mm(None, hh_dn)
            hh_connection_x = hh_anchor_x + hh_lower_outlet_length + hh_elbow_trim * 2.0
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": str(route["route_id"]),
                    "section": section_name,
                    "route_kind": route_kind,
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                    "dn": int(
                        route.get(
                            "branch_dn",
                            resolve_section_route_pipe_run_value(
                                section_route_entry,
                                "cc_left_wall_dn200",
                                "dn",
                                200,
                            ),
                        )
                    ),
                    "anchor_x": float(anchor_x),
                    "base_y": base_y,
                    "base_z": base_z,
                    "wall_center_y": wall_center_y,
                    "center_elevation_mm": center_elevation,
                    "outside_projection_length_mm": float(route.get("outside_projection_length_mm", 1350.0)),
                    "outside_start_x_mm": (
                        float(route["outside_start_x_mm"])
                        if route.get("outside_start_x_mm") is not None
                        else None
                    ),
                    "inside_projection_length_mm": float(route.get("inside_projection_length_mm", 0.0)),
                    "valve_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "cc_gate_valve_9",
                        "item_no",
                        route.get("valve_item_no"),
                    ),
                    "valve_pn": float(route.get("valve_pn", 10.0)),
                    "valve_structure_length_mm": float(route.get("valve_structure_length_mm", 550.0)),
                    "valve_center_offset_from_wall_mm": float(route.get("valve_center_offset_from_wall_mm", 650.0)),
                    "valve_offset_from_hh_connection_mm": float(route.get("valve_offset_from_hh_connection_mm", 1000.0)),
                    "hh_connection_x": hh_connection_x,
                    "right_projection_length_mm": float(route.get("right_projection_length_mm", 300.0)),
                    "draw_valve_proxy": bool(route.get("draw_valve_proxy", True)),
                    "sleeve_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "cc_wall_sleeve_31",
                        "item_no",
                        route.get("sleeve_item_no"),
                    ),
                    "basis": cc_route_basis,
                }
            )
            continue
        if route_kind == "cc_dn200_plan_extension_preview":
            hh_template = require_bundle(layout, "section_preview_routes", "route_id", "hh_preview_left_tower_drop")
            hh_anchor_x = float(hh_template.get("anchor_x_mm", anchor_x))
            hh_lower_outlet_length = float(hh_template.get("lower_outlet_length_mm", 250.0))
            hh_dn = int(hh_template.get("branch_dn", 125))
            hh_elbow_trim = resolve_lr_elbow_connection_trim_mm(None, hh_dn)
            hh_connection_x = hh_anchor_x + hh_lower_outlet_length + hh_elbow_trim * 2.0
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": str(route["route_id"]),
                    "section": section_name,
                    "route_kind": route_kind,
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                    "dn": int(route.get("branch_dn", 200)),
                    "line_y": float(route.get("line_y_mm", 5850.0)),
                    "center_elevation_mm": float(route.get("center_elevation_mm", -720.0)),
                    "hh_connection_x": hh_connection_x,
                    "left_projection_length_mm": float(route.get("left_projection_length_mm", 1200.0)),
                    "right_projection_length_mm": float(route.get("right_projection_length_mm", 300.0)),
                    "valve_item_no": route.get("valve_item_no"),
                    "valve_pn": float(route.get("valve_pn", 10.0)),
                    "valve_structure_length_mm": float(route.get("valve_structure_length_mm", 550.0)),
                    "valve_offset_from_hh_connection_mm": float(
                        route.get("valve_offset_from_hh_connection_mm", 1000.0)
                    ),
                    "draw_valve_proxy": bool(route.get("draw_valve_proxy", True)),
                    "basis": str(route.get("basis", "")),
                }
            )
            continue
        if route_kind == "hh_dn125_tower_drop_preview":
            hh_route_basis = resolve_section_route_modeling_description(
                section_route_entry,
                default=str(route.get("basis", "")),
            )
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": str(route["route_id"]),
                    "section": section_name,
                    "route_kind": route_kind,
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                    "dn": int(route.get("branch_dn", 125)),
                    "anchor_x": float(anchor_x),
                    "base_y": base_y,
                    "base_z": base_z,
                    "drop_leg_y": float(route.get("drop_leg_y_mm", base_y)),
                    "top_elevation_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "hh_branch_tee_16",
                            "elevation_mm",
                            route.get("top_elevation_mm", 5000.0),
                        )
                    ),
                    "bottom_elevation_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "hh_lower_elbow_23",
                            "elevation_mm",
                            route.get("bottom_elevation_mm", -900.0),
                        )
                    ),
                    "top_branch_length_mm": float(
                        resolve_section_route_linear_dimension(
                            section_route_entry,
                            meaning_contains="横向接入段",
                            default=float(route.get("top_branch_length_mm", 500.0)),
                        )
                    ),
                    "lower_outlet_length_mm": float(route.get("lower_outlet_length_mm", 250.0)),
                    "header_branch_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "hh_branch_tee_16",
                        "item_no",
                        route.get("header_branch_item_no"),
                    ),
                    "valve_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "hh_valve_6",
                        "item_no",
                        route.get("valve_item_no", 6),
                    ),
                    "valve_center_elevation_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "hh_valve_6",
                            "elevation_mm",
                            route.get("valve_center_elevation_mm", 1200.0),
                        )
                    ),
                    "valve_structure_length_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "hh_valve_6",
                            "structure_length_mm",
                            route.get("valve_structure_length_mm", 64.0),
                        )
                    ),
                    "valve_pn": float(route.get("valve_pn", 10.0)),
                    "elbow_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "hh_upper_elbow_23",
                        "item_no",
                        route.get("elbow_item_no"),
                    ),
                    "draw_fittings": bool(route.get("draw_fittings", True)),
                    "basis": hh_route_basis,
                }
            )
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": f"{route['route_id']}_plan_external",
                    "section": section_name,
                    "route_kind": "hh_dn125_plan_external_connection",
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                    "dn": int(route.get("branch_dn", 125)),
                    "anchor_x": float(anchor_x),
                    "base_y": base_y,
                    "base_z": base_z,
                    "drop_leg_y": float(route.get("drop_leg_y_mm", base_y)),
                    "bottom_elevation_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "hh_lower_elbow_23",
                            "elevation_mm",
                            route.get("bottom_elevation_mm", -900.0),
                        )
                    ),
                    "lower_outlet_length_mm": float(route.get("lower_outlet_length_mm", 250.0)),
                    "header_branch_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "hh_branch_tee_16",
                        "item_no",
                        route.get("header_branch_item_no"),
                    ),
                    "elbow_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "hh_upper_elbow_23",
                        "item_no",
                        route.get("elbow_item_no"),
                    ),
                    "draw_fittings": bool(route.get("draw_fittings", True)),
                    "basis": (
                        f"{hh_route_basis}；本条为由平面图直接控制、并与 H-H 底部横管相连的外接 DN125 管链："
                        "从 H-H 底部横管末端经弯头转向 +y，平面延伸到 DN450 主管列位后，再经弯头下翻并通过 DN450x125 三通接入 DN450 主管。"
                    ),
                }
            )
            continue
        if route_kind == "ii_dn150_local_drop_preview":
            ii_route_basis = resolve_section_route_modeling_description(
                section_route_entry,
                default=str(route.get("basis", "")),
            )
            y0202_bundle = auxiliary_equipment_index.get("Y-0202")
            if y0202_bundle is None:
                raise KeyError("auxiliary_equipments 缺少 Y-0202，无法为 I-I 回算上下对称列位。")
            ff_spine_template = require_bundle(layout, "auxiliary_routes", "route_id", "y0202_right_dn150_spine")
            ff_spine_segments = list(ff_spine_template.get("segments", []))
            if not ff_spine_segments:
                raise ValueError("y0202_right_dn150_spine 缺少 segments，无法为 I-I 读取 DN150 母线列位。")
            ff_spine_x = float(ff_spine_segments[0]["start"][0])
            y0202_top_y = float(y0202_bundle["top_y_mm"])
            lower_drop_leg_y = float(route.get("drop_leg_y_mm", base_y))
            upper_drop_leg_y = y0202_top_y + 300.0
            lower_section_rotation_deg_z = float(route.get("section_rotation_deg_z", -90.0))
            upper_section_rotation_deg_z = 90.0
            fallback_top_branch_length = float(route.get("top_branch_length_mm", 500.0))
            lower_top_branch_length = fallback_top_branch_length
            upper_top_branch_length = fallback_top_branch_length
            top_branch_target_tag = str(route.get("top_branch_target_equipment_tag", "")).strip()
            top_branch_connect_mode = str(route.get("top_branch_connect_mode", "")).strip().lower()
            tank_bundle = auxiliary_equipment_index.get(top_branch_target_tag) if top_branch_target_tag else None
            if tank_bundle is not None and top_branch_connect_mode == "tank_shell":
                lower_top_branch_length = resolve_ii_top_branch_length_to_tank_mm(
                    drop_leg_x=float(anchor_x),
                    drop_leg_y=lower_drop_leg_y,
                    section_rotation_deg_z=lower_section_rotation_deg_z,
                    tank_equipment=tank_bundle,
                    fallback_length_mm=fallback_top_branch_length,
                )
                upper_top_branch_length = resolve_ii_top_branch_length_to_tank_mm(
                    drop_leg_x=float(anchor_x),
                    drop_leg_y=upper_drop_leg_y,
                    section_rotation_deg_z=upper_section_rotation_deg_z,
                    tank_equipment=tank_bundle,
                    fallback_length_mm=fallback_top_branch_length,
                )
                ii_route_basis = (
                    f"{ii_route_basis}；顶部 DN150 横管按 {top_branch_target_tag} 外壁回算："
                    f"下侧长度 {lower_top_branch_length:.1f} mm，上侧对称长度 {upper_top_branch_length:.1f} mm。"
                )
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": str(route["route_id"]),
                    "section": section_name,
                    "route_kind": route_kind,
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                    "dn": int(route.get("branch_dn", 150)),
                    "anchor_x": float(anchor_x),
                    "base_y": base_y,
                    "base_z": base_z,
                    "drop_leg_y": lower_drop_leg_y,
                    "top_elevation_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "ii_upper_elbow_22",
                            "elevation_mm",
                            route.get("top_elevation_mm", 4500.0),
                        )
                    ),
                    "bottom_elevation_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "ii_lower_elbow_22",
                            "elevation_mm",
                            route.get("bottom_elevation_mm", -600.0),
                        )
                    ),
                    "top_branch_length_mm": lower_top_branch_length,
                    "top_branch_target_equipment_tag": top_branch_target_tag or None,
                    "top_branch_connect_mode": top_branch_connect_mode or None,
                    "lower_outlet_length_mm": float(route.get("lower_outlet_length_mm", 250.0)),
                    "header_branch_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "ii_header_branch_semantic",
                        "item_no",
                        route.get("header_branch_item_no"),
                    ),
                    "elbow_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "ii_upper_elbow_22",
                        "item_no",
                        route.get("elbow_item_no"),
                    ),
                    "section_rotation_deg_z": lower_section_rotation_deg_z,
                    "mirror_local_outlet_y": bool(route.get("mirror_local_outlet_y", False)),
                    "draw_fittings": bool(route.get("draw_fittings", True)),
                    "basis": ii_route_basis,
                }
            )
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": f"{route['route_id']}_plan_external",
                    "section": section_name,
                    "route_kind": "ii_dn150_plan_external_connection",
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                    "dn": int(route.get("branch_dn", 150)),
                    "anchor_x": float(anchor_x),
                    "drop_leg_y": lower_drop_leg_y,
                    "bottom_elevation_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "ii_lower_elbow_22",
                            "elevation_mm",
                            route.get("bottom_elevation_mm", -600.0),
                        )
                    ),
                    "lower_outlet_length_mm": float(route.get("lower_outlet_length_mm", 250.0)),
                    "target_x_mm": ff_spine_x,
                    "section_rotation_deg_z": lower_section_rotation_deg_z,
                    "mirror_local_outlet_y": bool(route.get("mirror_local_outlet_y", False)),
                    "draw_fittings": bool(route.get("draw_fittings", True)),
                    "basis": (
                        f"{ii_route_basis}；本条为下侧 I-I 与 F-F 下侧外接 DN150 管的平面连通链，"
                        "连接位置取 Y-0202 下侧外偏 300 对应的 y=4200。"
                    ),
                }
            )
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": f"{route['route_id']}_upper_symmetric",
                    "section": section_name,
                    "route_kind": route_kind,
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                    "dn": int(route.get("branch_dn", 150)),
                    "anchor_x": float(anchor_x),
                    "base_y": base_y,
                    "base_z": base_z,
                    "drop_leg_y": upper_drop_leg_y,
                    "top_elevation_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "ii_upper_elbow_22",
                            "elevation_mm",
                            route.get("top_elevation_mm", 4500.0),
                        )
                    ),
                    "bottom_elevation_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "ii_lower_elbow_22",
                            "elevation_mm",
                            route.get("bottom_elevation_mm", -600.0),
                        )
                    ),
                    "top_branch_length_mm": upper_top_branch_length,
                    "top_branch_target_equipment_tag": top_branch_target_tag or None,
                    "top_branch_connect_mode": top_branch_connect_mode or None,
                    "lower_outlet_length_mm": float(route.get("lower_outlet_length_mm", 250.0)),
                    "header_branch_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "ii_header_branch_semantic",
                        "item_no",
                        route.get("header_branch_item_no"),
                    ),
                    "elbow_item_no": resolve_section_route_node_value(
                        section_route_entry,
                        "ii_upper_elbow_22",
                        "item_no",
                        route.get("elbow_item_no"),
                    ),
                    "section_rotation_deg_z": upper_section_rotation_deg_z,
                    "mirror_local_outlet_y": True,
                    "draw_fittings": bool(route.get("draw_fittings", True)),
                    "basis": (
                        f"{ii_route_basis}；按用户当前要求，在 Y-0202 上侧外偏 300 处对称补一套上侧 I-I，"
                        "并使该对称剖面的上部弯头朝 DN500 主管方向；顶部 DN150 横管同样接入 "
                        f"{top_branch_target_tag or '目标罐体'}。"
                    ),
                }
            )
            compiled_layout["section_preview_routes"].append(
                {
                    "route_id": f"{route['route_id']}_upper_symmetric_plan_external",
                    "section": section_name,
                    "route_kind": "ii_dn150_plan_external_connection",
                    "section_route_entry": section_route_entry,
                    "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                    "dn": int(route.get("branch_dn", 150)),
                    "anchor_x": float(anchor_x),
                    "drop_leg_y": upper_drop_leg_y,
                    "bottom_elevation_mm": float(
                        resolve_section_route_node_value(
                            section_route_entry,
                            "ii_lower_elbow_22",
                            "elevation_mm",
                            route.get("bottom_elevation_mm", -600.0),
                        )
                    ),
                    "lower_outlet_length_mm": float(route.get("lower_outlet_length_mm", 250.0)),
                    "target_x_mm": ff_spine_x,
                    "section_rotation_deg_z": upper_section_rotation_deg_z,
                    "mirror_local_outlet_y": True,
                    "draw_fittings": bool(route.get("draw_fittings", True)),
                    "basis": (
                        f"{ii_route_basis}；本条为上侧对称 I-I 与 F-F 上侧外接 DN150 管的平面连通链，"
                        "连接位置取 Y-0202 上侧外偏 300 对应的 y=7500。"
                    ),
                }
            )
            continue
        inner_wall_side = str(route.get("inner_wall_side", "top")).strip().lower()
        right_vertical_offset = float(
            resolve_section_route_node_value(
                section_route_entry,
                "ee_right_inner_wall_reference",
                "offset_mm",
                route.get("right_vertical_center_offset_from_inner_wall_mm", 0.0),
            )
        )
        if inner_wall_side == "top":
            right_vertical_y = basin_inner_max_y - right_vertical_offset
        elif inner_wall_side == "bottom":
            right_vertical_y = basin_inner_min_y + right_vertical_offset
        else:
            raise ValueError(f"暂不支持的 inner_wall_side: {inner_wall_side}")
        branch_dn = int(
            resolve_section_route_pipe_run_value(
                section_route_entry,
                "ee_left_riser_dn250",
                "dn",
                route["branch_dn"],
            )
        )
        top_horizontal_elevation = float(
            resolve_section_route_node_value(
                section_route_entry,
                "ee_left_elbow_20",
                "elevation_mm",
                route["top_horizontal_elevation_mm"],
            )
        )
        right_vertical_bottom_elevation = float(
            resolve_section_route_pipe_run_value(
                section_route_entry,
                "ee_right_drop_dn250",
                "to_elevation_mm",
                route.get("right_vertical_bottom_elevation_mm", top_horizontal_elevation),
            )
        )
        tee_branch_center_to_end = float(route.get("tee_branch_center_to_end_mm", 0.0))
        tee_branch_display_offset, _ = resolve_tee_display_offsets_mm(
            run_dn=int(route.get("tee_proxy_run_dn", base_header_dn)),
            branch_center_to_end_mm=tee_branch_center_to_end,
        )
        valve_center_elevation = float(
            resolve_section_route_node_value(
                section_route_entry,
                "ee_valve_4",
                "elevation_mm",
                route.get("valve_center_elevation_mm", top_horizontal_elevation),
            )
        )
        valve_structure_length = float(route.get("valve_structure_length_mm", 0.0))
        valve_half_length = valve_structure_length / 2.0 if valve_structure_length > 0 else 0.0
        left_elbow_connection_trim = resolve_lr_elbow_connection_trim_mm(
            route.get("left_elbow_connection_trim_mm"),
            branch_dn,
        )
        right_elbow_connection_trim = resolve_lr_elbow_connection_trim_mm(
            route.get("right_elbow_connection_trim_mm"),
            branch_dn,
        )
        segments: list[dict[str, Any]] = []
        lower_vertical_start_z = base_z + tee_branch_display_offset
        lower_vertical_end_z = valve_center_elevation - valve_half_length
        if lower_vertical_end_z - lower_vertical_start_z > 1e-6:
            segments.append(
                {
                    "start": [anchor_x, base_y, lower_vertical_start_z],
                    "end": [anchor_x, base_y, lower_vertical_end_z],
                }
            )
        upper_vertical_start_z = valve_center_elevation + valve_half_length
        upper_vertical_end_z = top_horizontal_elevation - left_elbow_connection_trim
        if upper_vertical_end_z - upper_vertical_start_z > 1e-6:
            segments.append(
                {
                    "start": [anchor_x, base_y, upper_vertical_start_z],
                    "end": [anchor_x, base_y, top_horizontal_elevation - left_elbow_connection_trim],
                }
            )
        top_horizontal_start_y = base_y + left_elbow_connection_trim
        top_horizontal_end_y = right_vertical_y - right_elbow_connection_trim
        if top_horizontal_end_y - top_horizontal_start_y > 1e-6:
            segments.append(
                {
                    "start": [anchor_x, top_horizontal_start_y, top_horizontal_elevation],
                    "end": [anchor_x, top_horizontal_end_y, top_horizontal_elevation],
                }
            )
        right_vertical_start_z = top_horizontal_elevation - right_elbow_connection_trim
        if right_vertical_start_z - right_vertical_bottom_elevation > 1e-6:
            segments.append(
                {
                    "start": [anchor_x, right_vertical_y, right_vertical_start_z],
                    "end": [anchor_x, right_vertical_y, right_vertical_bottom_elevation],
                }
            )
        compiled_layout["section_preview_routes"].append(
            {
                "route_id": str(route["route_id"]),
                "section": section_name,
                "route_kind": route_kind,
                "section_route_entry": section_route_entry,
                "section_route_source": str(SECTION_ROUTE_CONNECTION_INFO_PATH) if section_route_entry else None,
                "dn": branch_dn,
                "anchor_x": anchor_x,
                "base_y": base_y,
                "base_z": base_z,
                "base_header_dn": int(base_header_dn),
                "right_vertical_y": right_vertical_y,
                "top_horizontal_elevation_mm": top_horizontal_elevation,
                "right_vertical_bottom_elevation_mm": right_vertical_bottom_elevation,
                "tee_branch_center_to_end_mm": tee_branch_center_to_end,
                "tee_branch_display_offset_mm": tee_branch_display_offset,
                "valve_structure_length_mm": valve_structure_length,
                "left_elbow_connection_trim_mm": left_elbow_connection_trim,
                "right_elbow_connection_trim_mm": right_elbow_connection_trim,
                "draw_fittings": bool(route.get("draw_fittings", False)),
                "tee_proxy_run_dn": int(route.get("tee_proxy_run_dn", base_header_dn)),
                "fitting_item_no": resolve_section_route_node_value(
                    section_route_entry,
                    "ee_header_circle",
                    "proxy_fitting_item_no",
                    route.get("fitting_item_no"),
                ),
                "fitting_specification": route.get("fitting_specification"),
                "reducer_length_mm": float(route.get("reducer_length_mm", 0.0)),
                "reducer_center_elevation_mm": float(route.get("reducer_center_elevation_mm", base_z)),
                "valve_item_no": resolve_section_route_node_value(
                    section_route_entry,
                    "ee_valve_4",
                    "item_no",
                    route.get("valve_item_no"),
                ),
                "valve_center_elevation_mm": valve_center_elevation,
                "valve_pn": float(route.get("valve_pn", 10.0)),
                "elbow_item_no": resolve_section_route_node_value(
                    section_route_entry,
                    "ee_left_elbow_20",
                    "item_no",
                    route.get("elbow_item_no"),
                ),
                "left_elbow_rotation_deg": float(route.get("left_elbow_rotation_deg", -90.0)),
                "left_elbow_rotation_axis": str(route.get("left_elbow_rotation_axis", "y")).strip().lower(),
                "left_elbow_secondary_rotation_deg": float(route.get("left_elbow_secondary_rotation_deg", 180.0)),
                "left_elbow_secondary_rotation_axis": str(route.get("left_elbow_secondary_rotation_axis", "z")).strip().lower(),
                "left_elbow_center_drop_mm": float(route.get("left_elbow_center_drop_mm", 0.0)),
                "right_elbow_rotation_deg": float(route.get("right_elbow_rotation_deg", -90.0)),
                "right_elbow_rotation_axis": str(route.get("right_elbow_rotation_axis", "y")).strip().lower(),
                "right_elbow_secondary_rotation_deg": float(route.get("right_elbow_secondary_rotation_deg", 0.0)),
                "right_elbow_secondary_rotation_axis": str(route.get("right_elbow_secondary_rotation_axis", "x")).strip().lower(),
                "right_elbow_center_drop_mm": float(route.get("right_elbow_center_drop_mm", 0.0)),
                "segments": segments,
                "basis": str(route.get("basis", "")),
            }
        )

    if not delete_all_non_header_pipes:
        for proxy in layout.get("support_proxies_v2", []):
            proxy_center = [float(value) for value in proxy["center"]]
            nearest_pump = min(
                pump_index.values(),
                key=lambda item: abs(float(item["center"][0]) - proxy_center[0]),
            )
            nearest_pump_x = float(nearest_pump["center"][0])
            x_offset = proxy_center[0] - nearest_pump_x
            y_offset = proxy_center[1] - float(lower_header_template_segment["start"][1])
            z_offset = proxy_center[2] - float(lower_header_template_segment["start"][2])
            compiled_layout["support_proxies_v2"].append(
                {
                    "support_id": proxy["support_id"],
                    "center": [
                        nearest_pump_x + x_offset,
                        lower_header_y + y_offset,
                        lower_header_z + z_offset,
                    ],
                    "size_mm": [float(value) for value in proxy["size_mm"]],
                    "basis": (
                        f"{proxy['support_id']} 继续挂靠最近泵基础中心 {nearest_pump['tag']}，"
                        "并继承与底部主管的相对位置。"
                    ),
                }
            )

    return compiled_layout


def segment_to_axis(start: list[float], end: list[float]) -> tuple[list[float], str, float]:
    """Convert one orthogonal segment into base point, axis and length."""

    dx = end[0] - start[0]
    dy = end[1] - start[1]
    dz = end[2] - start[2]
    changed_axes = sum(1 for value in (dx, dy, dz) if abs(value) > 1e-6)
    if changed_axes != 1:
        raise ValueError(f"仅支持正交直管段: {start} -> {end}")
    if abs(dx) > 1e-6:
        return start, ("x" if dx > 0 else "-x"), abs(dx)
    if abs(dy) > 1e-6:
        return start, ("y" if dy > 0 else "-y"), abs(dy)
    return start, ("z" if dz > 0 else "-z"), abs(dz)


def draw_pipe_segment(service: CADService, *, start: list[float], end: list[float], dn: int) -> None:
    """Draw one orthogonal pipe segment."""

    base_point, axis, length = segment_to_axis(start, end)
    radius = resolve_pipe_outer_radius_mm(dn)
    controller = service.controller
    try:
        wall_thickness = controller._resolve_default_fitting_wall_thickness_gb12459(dn)
    except Exception:
        wall_thickness = max(6.0, radius * 0.08)
    for _ in range(4):
        try:
            entity = controller._add_oriented_hollow_cylinder(
                tuple(base_point),
                radius,
                length,
                axis,
                wall_thickness=wall_thickness,
            )
            if entity is not None:
                apply_entity_style(controller, entity, f"PIPE_DN{dn}", WHITE)
                return
        except Exception:
            pass
        time.sleep(0.6)
    raise RuntimeError(f"管段创建失败: DN{dn} {start} -> {end}")


def draw_dd_orifice_collar(
    service: CADService,
    *,
    center: list[float],
    dn: int,
) -> str | None:
    """Draw item 28 as a formal annular collar at the top of the D-D riser."""

    controller = service.controller
    pipe_outer_radius = PIPE_OD[int(dn)] / 2.0
    band_height = max(160.0, pipe_outer_radius * 1.6)
    outer_radius = pipe_outer_radius + 45.0
    wall_thickness = max(18.0, outer_radius - pipe_outer_radius - 6.0)
    base_point = (
        float(center[0]),
        float(center[1]),
        float(center[2]) - band_height / 2.0,
    )
    entity = None
    for _ in range(4):
        try:
            entity = controller._add_oriented_hollow_cylinder(
                base_point,
                outer_radius,
                band_height,
                "z",
                wall_thickness=wall_thickness,
            )
            if entity is not None:
                apply_entity_style(controller, entity, f"PIPE_FITTING_DN{dn}", WHITE)
                return extract_entity_handle(entity)
        except Exception:
            pass
        time.sleep(0.6)
    return extract_entity_handle(entity)


def extract_entity_handle(entity: Any) -> str | None:
    """Return one CAD handle when the entity is available."""

    if entity is None:
        return None
    for _ in range(4):
        try:
            return str(entity.Handle)
        except Exception:
            time.sleep(0.5)
    return None


def rotate_entity_with_retry(
    service: CADService,
    *,
    handle: str,
    base_point: list[float],
    angle: float,
    axis_direction: tuple[float, float, float],
) -> None:
    """Rotate one entity with retries so section fittings can be oriented reliably."""

    for _ in range(4):
        try:
            if service.controller.rotate_entity(handle, tuple(base_point), angle, axis_direction=axis_direction):
                return
        except Exception:
            pass
        time.sleep(0.6)
    raise RuntimeError(f"实体旋转失败: handle={handle}, angle={angle}, axis={axis_direction}")


def resolve_rotation_axis(axis_name: Any) -> tuple[float, float, float]:
    """Map one axis name to a Rotate3D direction vector."""

    axis = str(axis_name or "y").strip().lower()
    if axis == "x":
        return (1.0, 0.0, 0.0)
    if axis == "z":
        return (0.0, 0.0, 1.0)
    return (0.0, 1.0, 0.0)


def rotate_point_around_z(point: list[float], *, center_xy: tuple[float, float], angle_deg: float) -> list[float]:
    """Rotate one XYZ point around the z axis while preserving elevation."""

    if abs(float(angle_deg)) <= 1e-9:
        return [float(point[0]), float(point[1]), float(point[2])]
    angle_rad = math.radians(float(angle_deg))
    dx = float(point[0]) - float(center_xy[0])
    dy = float(point[1]) - float(center_xy[1])
    rotated_x = float(center_xy[0]) + dx * math.cos(angle_rad) - dy * math.sin(angle_rad)
    rotated_y = float(center_xy[1]) + dx * math.sin(angle_rad) + dy * math.cos(angle_rad)
    if abs(rotated_x - float(center_xy[0])) <= 1e-6:
        rotated_x = float(center_xy[0])
    if abs(rotated_y - float(center_xy[1])) <= 1e-6:
        rotated_y = float(center_xy[1])
    return [rotated_x, rotated_y, float(point[2])]


def measure_orthogonal_segment_length_mm(start: list[float], end: list[float]) -> float:
    """Return the dominant-axis length for an orthogonal segment."""

    return max(abs(float(end[index]) - float(start[index])) for index in range(3))


def resolve_port_contract(
    center_point: list[float] | tuple[float, float, float],
    tangent_point: list[float] | tuple[float, float, float],
) -> dict[str, Any]:
    """Infer one elbow port contract from center/tangent geometry."""

    deltas = [float(tangent_point[index]) - float(center_point[index]) for index in range(3)]
    coordinate_index = max(range(3), key=lambda index: abs(deltas[index]))
    delta = deltas[coordinate_index]
    if abs(delta) <= 1e-6:
        raise ValueError(f"无法从 center={center_point} 和 tangent={tangent_point} 推断端口方向。")
    if coordinate_index == 0:
        axis = "+x" if delta < 0 else "-x"
        connection_end = "left_end" if delta < 0 else "right_end"
    elif coordinate_index == 1:
        axis = "+y" if delta < 0 else "-y"
        connection_end = "left_end" if delta < 0 else "right_end"
    else:
        axis = "+z" if delta < 0 else "-z"
        connection_end = "bottom_end" if delta < 0 else "top_end"
    return {
        "axis": axis,
        "connection_end": connection_end,
        "coordinate_index": coordinate_index,
        "relation": "lt" if delta < 0 else "gt",
        "reference_value": float(center_point[coordinate_index]),
    }


def call_controller_with_retry(
    description: str,
    factory: Any,
    *,
    retries: int = 4,
    wait_seconds: float = 0.8,
) -> Any:
    """Call one CAD controller factory several times for unstable COM primitives."""

    last_entity = None
    for _ in range(retries):
        try:
            last_entity = factory()
            if last_entity is not None:
                return last_entity
        except Exception:
            pass
        time.sleep(wait_seconds)
    return last_entity


def append_route_segment_record(
    route_records: list[dict[str, Any]],
    *,
    bundle_id: str,
    segment_id: str,
    category: str,
    dn: int,
    start: list[float],
    end: list[float],
    basis: str,
    helper_generated: bool = False,
    path_id: str | None = None,
    source_run_id: str | None = None,
    from_node_id: str | None = None,
    to_node_id: str | None = None,
) -> None:
    """Append one segment record using the same schema as the existing bundle flow."""

    route_records.append(
        {
            "record_type": "segment",
            "bundle_id": bundle_id,
            "segment_id": segment_id,
            "category": category,
            "dn": dn,
            "start": start,
            "end": end,
            "basis": basis,
            "helper_generated": helper_generated,
            "path_id": path_id,
            "source_run_id": source_run_id,
            "from_node_id": from_node_id,
            "to_node_id": to_node_id,
        }
    )


def append_route_fitting_record(
    route_records: list[dict[str, Any]],
    *,
    bundle_id: str,
    fitting_id: str,
    category: str,
    kind: str,
    item_no: Any,
    center: list[float],
    handle: str | None,
    basis: str,
    helper_generated: bool = False,
) -> None:
    """Append one fitting record using the same schema as the existing bundle flow."""

    route_records.append(
        {
            "record_type": "fitting",
            "bundle_id": bundle_id,
            "fitting_id": fitting_id,
            "category": category,
            "kind": kind,
            "item_no": item_no,
            "center": center,
            "handle": handle,
            "basis": basis,
            "helper_generated": helper_generated,
        }
    )


def resolve_lr_elbow_connection_trim_mm(raw_value: Any, dn: int) -> float:
    """Resolve one LR elbow trim distance using bend radius by default."""

    if raw_value is None:
        return lr_elbow_bend_radius_mm(dn)
    return float(raw_value)


def resolve_ray_circle_hit_length_mm(
    *,
    origin_xy: tuple[float, float],
    direction_xy: tuple[float, float],
    center_xy: tuple[float, float],
    radius_mm: float,
) -> float | None:
    """Return the nearest positive ray length that hits a horizontal circle."""

    dir_x, dir_y = float(direction_xy[0]), float(direction_xy[1])
    dir_len = math.hypot(dir_x, dir_y)
    if dir_len <= 1e-9 or radius_mm <= 0:
        return None
    unit_x = dir_x / dir_len
    unit_y = dir_y / dir_len
    ox = float(origin_xy[0]) - float(center_xy[0])
    oy = float(origin_xy[1]) - float(center_xy[1])
    # |O + t D|^2 = R^2  =>  t^2 + 2(O·D)t + (|O|^2 - R^2) = 0
    half_b = ox * unit_x + oy * unit_y
    c = ox * ox + oy * oy - float(radius_mm) * float(radius_mm)
    discriminant = half_b * half_b - c
    if discriminant < 0:
        return None
    root = math.sqrt(discriminant)
    candidates = [-half_b - root, -half_b + root]
    positive = [value for value in candidates if value > 1e-6]
    if not positive:
        return None
    return float(min(positive))


def resolve_ii_top_branch_length_to_tank_mm(
    *,
    drop_leg_x: float,
    drop_leg_y: float,
    section_rotation_deg_z: float,
    tank_equipment: dict[str, Any],
    fallback_length_mm: float,
) -> float:
    """Extend the I-I top DN150 stub until it meets the target tank outer shell."""

    center_xy = [float(value) for value in tank_equipment.get("center_xy", [0.0, 0.0])]
    outer_diameter = float(
        tank_equipment.get(
            "outer_diameter_mm",
            max(float(value) for value in tank_equipment.get("plan_size_mm", [0.0, 0.0]) or [0.0]),
        )
    )
    if outer_diameter <= 0:
        return float(fallback_length_mm)
    angle = math.radians(float(section_rotation_deg_z))
    # Local top-branch free end starts along -X before the section rotation.
    direction_xy = (-math.cos(angle), -math.sin(angle))
    hit_length = resolve_ray_circle_hit_length_mm(
        origin_xy=(float(drop_leg_x), float(drop_leg_y)),
        direction_xy=direction_xy,
        center_xy=(center_xy[0], center_xy[1]),
        radius_mm=outer_diameter / 2.0,
    )
    if hit_length is None:
        return float(fallback_length_mm)
    return float(max(hit_length, float(fallback_length_mm)))


def resolve_pipe_outer_radius_mm(dn: int) -> float:
    """Return one pipe outer radius using the local OD table."""

    outer_diameter = float(PIPE_OD.get(int(dn), max(float(dn) * 1.22, float(dn) + 8.0)))
    return outer_diameter / 2.0


def resolve_tee_display_offsets_mm(
    *,
    run_dn: int,
    branch_center_to_end_mm: float,
    run_center_to_end_mm: float | None = None,
) -> tuple[float, float]:
    """Return tee display offsets used by the CAD primitive outer contour.

    The tee primitive extends the visible branch by one run outer radius beyond
    `M`, and the visible run half-length is `max(C, M + R_run)`.
    """

    run_outer_radius = resolve_pipe_outer_radius_mm(int(run_dn))
    branch_display_offset = float(branch_center_to_end_mm) + run_outer_radius
    if run_center_to_end_mm is None:
        return branch_display_offset, branch_display_offset
    run_display_half_length = max(float(run_center_to_end_mm), branch_display_offset)
    return branch_display_offset, run_display_half_length


def resolve_tee_center_to_end_gb12459(
    *,
    run_dn: int,
    branch_dn: int,
    run_center_to_end_mm: float | None = None,
    branch_center_to_end_mm: float | None = None,
) -> tuple[float, float] | None:
    """Resolve tee center-to-end values without requiring one live controller."""

    if run_dn not in CADController.TEE_CENTER_TO_END_GB12459 and run_center_to_end_mm is None:
        return None
    if branch_dn > run_dn:
        return None
    run_center_to_end = (
        float(run_center_to_end_mm)
        if run_center_to_end_mm is not None
        else float(CADController.TEE_CENTER_TO_END_GB12459[int(run_dn)])
    )
    if branch_center_to_end_mm is not None:
        branch_center_to_end = float(branch_center_to_end_mm)
    elif branch_dn == run_dn:
        branch_center_to_end = run_center_to_end
    else:
        branch_center_to_end = CADController.REDUCING_TEE_BRANCH_CENTER_TO_END_GB12459.get(
            (int(run_dn), int(branch_dn))
        )
        if branch_center_to_end is None:
            return None
        branch_center_to_end = float(branch_center_to_end)
    return run_center_to_end, branch_center_to_end


def compile_centered_inline_span(
    *,
    center: list[float] | tuple[float, float, float],
    axis: str,
    structure_length_mm: float,
) -> tuple[list[float], list[float]]:
    """Compile one centered inline fitting into upstream/downstream end points."""

    if float(structure_length_mm) <= 0:
        point = [float(value) for value in center]
        return point, point
    half_length = float(structure_length_mm) / 2.0
    center_tuple = (float(center[0]), float(center[1]), float(center[2]))
    start = advance_point(center_tuple, axis, -half_length)
    end = advance_point(center_tuple, axis, half_length)
    return [float(value) for value in start], [float(value) for value in end]


def build_compiled_pipe_segment(
    *,
    segment_id: str,
    dn: int,
    start: list[float] | tuple[float, float, float],
    end: list[float] | tuple[float, float, float],
    path_id: str,
    source_run_id: str,
    from_node_id: str | None,
    to_node_id: str | None,
    helper_generated: bool = False,
) -> dict[str, Any] | None:
    """Return one normalized segment spec when the span is still positive."""

    start_point = [float(value) for value in start]
    end_point = [float(value) for value in end]
    if (
        abs(start_point[0] - end_point[0]) <= 1e-6
        and abs(start_point[1] - end_point[1]) <= 1e-6
        and abs(start_point[2] - end_point[2]) <= 1e-6
    ):
        return None
    return {
        "segment_id": segment_id,
        "dn": int(dn),
        "start": start_point,
        "end": end_point,
        "path_id": path_id,
        "source_run_id": source_run_id,
        "from_node_id": from_node_id,
        "to_node_id": to_node_id,
        "helper_generated": bool(helper_generated),
    }


def compile_aa_connection_skeleton(
    context: dict[str, Any],
) -> dict[str, Any]:
    """Compile one A-A route into fitting occupancy, helper specs, and pipe subsegments."""

    route_id = str(context["route_id"])
    anchor_x = float(context["anchor_x"])
    upper_anchor_x = float(context["upper_anchor_x"])
    left_header_y = float(context["left_header_y"])
    left_branch_dn = int(context["left_branch_dn"])
    left_valve_center_elevation = float(context["left_valve_center_elevation_mm"])
    left_valve_structure_length = float(context["left_valve_structure_length_mm"])
    left_top_horizontal_elevation = float(context["left_top_horizontal_elevation_mm"])
    left_top_horizontal_end_y = float(context["left_top_horizontal_end_y"])
    left_branch_start_z = float(context["left_branch_start_z"])

    pump_vertical_dn = int(context["pump_vertical_dn"])
    pump_vertical_y = float(context["pump_vertical_y"])
    pump_vertical_bottom_elevation = float(context["pump_vertical_bottom_elevation_mm"])
    pump_vertical_top_elevation = float(context["pump_vertical_top_elevation_mm"])
    pump_valve_center_y = float(context["pump_valve_center_y"])
    pump_valve_structure_length = float(context["pump_valve_structure_length_mm"])
    pump_joint_11_center_y = float(context["pump_joint_11_center_y"])
    pump_joint_11_length = float(context["pump_joint_11_length_mm"])
    pump_reducer_large_end_y = float(context["pump_reducer_large_end_y"])
    pump_reducer_small_end_y = float(context["pump_reducer_small_end_y"])

    right_header_y = float(context["right_header_y"])
    right_branch_dn = int(context["right_branch_dn"])
    right_horizontal_start_y = float(context["right_horizontal_start_y"])
    right_valve_center_y = float(context["right_valve_center_y"])
    right_valve_structure_length = float(context["right_valve_structure_length_mm"])
    right_riser_top_elevation = float(context["right_riser_top_elevation_mm"])
    right_visible_vertical_bottom_elevation = float(context["right_visible_vertical_bottom_elevation_mm"])
    right_item26_large_end_y = float(context["right_item26_large_end_y"])
    right_joint_12_center_y = float(context["right_joint_12_center_y"])
    right_joint_12_length = float(context["right_joint_12_length_mm"])
    right_valve_1_center_y = float(context["right_valve_1_center_y"])
    right_valve_1_structure_length = float(context["right_valve_1_structure_length_mm"])
    draw_left_lower_branch = bool(context.get("draw_left_lower_branch", True))
    draw_right_upper_branch = bool(context.get("draw_right_upper_branch", True))
    draw_right_lower_branch = bool(context.get("draw_right_lower_branch", True))
    right_upper_anchor_x = upper_anchor_x

    fittings: dict[str, dict[str, Any]] = {}
    segments: list[dict[str, Any]] = []
    helper_specs: dict[str, dict[str, Any]] = {}

    left_valve_center = [anchor_x, left_header_y, left_valve_center_elevation]
    left_valve_start, left_valve_end = compile_centered_inline_span(
        center=left_valve_center,
        axis="+z",
        structure_length_mm=left_valve_structure_length,
    )
    fittings["aa_left_valve_4"] = {
        "node_id": "aa_left_valve_4",
        "kind": "valve",
        "center": left_valve_center,
        "upstream_point": left_valve_start,
        "downstream_point": left_valve_end,
        "axis": "+z",
    }
    left_bend_radius = PIPE_OD[left_branch_dn] * 1.5
    left_elbow_requested_vertical = [anchor_x, left_header_y, left_top_horizontal_elevation - left_bend_radius]
    left_elbow_requested_horizontal = [anchor_x, left_header_y + left_bend_radius, left_top_horizontal_elevation]
    helper_specs["left_top_elbow"] = {
        "helper_id": "left_top_elbow",
        "path_id": "aa_left_side_chain",
        "fitting_node_id": "aa_left_top_elbow_dn250",
        "run_id": "aa_left_post_valve_riser_dn250",
        "horizontal_run_id": "aa_left_top_horizontal_dn250",
        "segment_ids": {
            "vertical": f"{route_id}_left_post_valve_vertical",
            "horizontal": f"{route_id}_left_top_horizontal",
        },
        "dn": left_branch_dn,
        "elbow_center": [anchor_x, left_header_y, left_top_horizontal_elevation],
        "requested_vertical_tangent": left_elbow_requested_vertical,
        "requested_horizontal_tangent": left_elbow_requested_horizontal,
        "requested_vertical_start": left_valve_end,
        "requested_horizontal_end": [anchor_x, left_top_horizontal_end_y, left_top_horizontal_elevation],
        "run_specs": [
            PipeConnectionRunSpec(
                run_id=f"{route_id}_left_post_valve_vertical",
                tangent_point_mm=tuple(left_elbow_requested_vertical),
                axis_away_from_fitting="+z",
                length_mm=max(0.0, left_elbow_requested_vertical[2] - left_valve_end[2]),
                connection_end="bottom_end",
                flow_role="pipe_1",
            ),
            PipeConnectionRunSpec(
                run_id=f"{route_id}_left_top_horizontal",
                tangent_point_mm=tuple(left_elbow_requested_horizontal),
                axis_away_from_fitting="-y",
                length_mm=max(0.0, left_top_horizontal_end_y - left_elbow_requested_horizontal[1]),
                connection_end="right_end",
                flow_role="pipe_2",
            ),
        ],
    }
    if draw_left_lower_branch:
        left_lower_segment = build_compiled_pipe_segment(
            segment_id=f"{route_id}_left_vertical",
            dn=left_branch_dn,
            start=[anchor_x, left_header_y, left_branch_start_z],
            end=left_valve_start,
            path_id="aa_left_side_chain",
            source_run_id="aa_left_riser_dn250",
            from_node_id="aa_left_header_circle",
            to_node_id="aa_left_valve_4",
        )
        if left_lower_segment is not None:
            segments.append(left_lower_segment)

    pump_valve_center = [upper_anchor_x, pump_valve_center_y, pump_vertical_top_elevation]
    pump_valve_start, pump_valve_end = compile_centered_inline_span(
        center=pump_valve_center,
        axis="+y",
        structure_length_mm=pump_valve_structure_length,
    )
    fittings["aa_p0204_valve_3"] = {
        "node_id": "aa_p0204_valve_3",
        "kind": "valve",
        "center": pump_valve_center,
        "upstream_point": pump_valve_start,
        "downstream_point": pump_valve_end,
        "axis": "+y",
    }
    pump_joint_center = [upper_anchor_x, pump_joint_11_center_y, pump_vertical_top_elevation]
    pump_joint_start, pump_joint_end = compile_centered_inline_span(
        center=pump_joint_center,
        axis="+y",
        structure_length_mm=pump_joint_11_length,
    )
    fittings["aa_p0204_joint_11"] = {
        "node_id": "aa_p0204_joint_11",
        "kind": "expansion_joint",
        "center": pump_joint_center,
        "upstream_point": pump_joint_start,
        "downstream_point": pump_joint_end,
        "axis": "+y",
    }
    pump_reducer_start = [upper_anchor_x, pump_reducer_large_end_y, pump_vertical_top_elevation]
    pump_reducer_end = [upper_anchor_x, pump_reducer_small_end_y, pump_vertical_top_elevation]
    fittings["aa_p0204_reducer_25"] = {
        "node_id": "aa_p0204_reducer_25",
        "kind": "reducer",
        "upstream_point": pump_reducer_start,
        "downstream_point": pump_reducer_end,
        "axis": "+y",
    }
    pump_bend_radius = PIPE_OD[pump_vertical_dn] * 1.5
    pump_elbow_requested_vertical = [upper_anchor_x, pump_vertical_y, pump_vertical_top_elevation - pump_bend_radius]
    pump_elbow_requested_horizontal = [upper_anchor_x, pump_vertical_y + pump_bend_radius, pump_vertical_top_elevation]
    helper_specs["pump_top_elbow"] = {
        "helper_id": "pump_top_elbow",
        "path_id": "aa_p0204_left_chain",
        "fitting_node_id": "aa_p0204_elbow_19",
        "run_id": "aa_p0204_riser_dn300",
        "horizontal_run_id": "aa_p0204_horizontal_dn300",
        "segment_ids": {
            "vertical": f"{route_id}_pump_vertical",
            "horizontal": f"{route_id}_pump_horizontal_dn300_a",
        },
        "dn": pump_vertical_dn,
        "elbow_center": [upper_anchor_x, pump_vertical_y, pump_vertical_top_elevation],
        "requested_vertical_tangent": pump_elbow_requested_vertical,
        "requested_horizontal_tangent": pump_elbow_requested_horizontal,
        "requested_vertical_start": [upper_anchor_x, pump_vertical_y, pump_vertical_bottom_elevation],
        "requested_horizontal_end": pump_valve_start,
        "run_specs": [
            PipeConnectionRunSpec(
                run_id=f"{route_id}_pump_vertical",
                tangent_point_mm=tuple(pump_elbow_requested_vertical),
                axis_away_from_fitting="+z",
                length_mm=max(0.0, pump_elbow_requested_vertical[2] - pump_vertical_bottom_elevation),
                connection_end="bottom_end",
                flow_role="pipe_1",
            ),
            PipeConnectionRunSpec(
                run_id=f"{route_id}_pump_horizontal_dn300_a",
                tangent_point_mm=tuple(pump_elbow_requested_horizontal),
                axis_away_from_fitting="-y",
                length_mm=max(0.0, pump_valve_start[1] - pump_elbow_requested_horizontal[1]),
                connection_end="right_end",
                flow_role="pipe_2",
            ),
        ],
    }
    pump_horizontal_chain = [
        node_id
        for node_id in context.get("aa_pump_flow_sequence", [])
        if node_id in {"aa_p0204_valve_3", "aa_p0204_joint_11", "aa_p0204_reducer_25"}
    ]
    pump_previous_node_id = "aa_p0204_elbow_19"
    pump_previous_point = pump_valve_start
    if pump_horizontal_chain:
        pump_previous_point = pump_valve_start
    if pump_horizontal_chain == ["aa_p0204_valve_3", "aa_p0204_joint_11", "aa_p0204_reducer_25"]:
        pump_pairs = [
            ("aa_p0204_valve_3", pump_valve_end, "aa_p0204_joint_11", pump_joint_start, "aa_p0204_horizontal_dn300"),
            ("aa_p0204_joint_11", pump_joint_end, "aa_p0204_reducer_25", pump_reducer_start, "aa_p0204_horizontal_dn300"),
        ]
        for index, (from_node_id, start_point, to_node_id, end_point, run_id) in enumerate(pump_pairs, start=1):
            segment = build_compiled_pipe_segment(
                segment_id=f"{route_id}_pump_horizontal_dn300_{chr(ord('a') + index)}",
                dn=pump_vertical_dn,
                start=start_point,
                end=end_point,
                path_id="aa_p0204_left_chain",
                source_run_id=run_id,
                from_node_id=from_node_id,
                to_node_id=to_node_id,
            )
            if segment is not None:
                segments.append(segment)

    right_valve_center = [right_upper_anchor_x, right_valve_center_y, right_riser_top_elevation]
    right_valve_start, right_valve_end = compile_centered_inline_span(
        center=right_valve_center,
        axis="+y",
        structure_length_mm=right_valve_structure_length,
    )
    fittings["aa_right_valve_8"] = {
        "node_id": "aa_right_valve_8",
        "kind": "valve",
        "center": right_valve_center,
        "upstream_point": right_valve_start,
        "downstream_point": right_valve_end,
        "axis": "+y",
    }
    right_joint_center = [right_upper_anchor_x, right_joint_12_center_y, right_riser_top_elevation]
    right_joint_start, right_joint_end = compile_centered_inline_span(
        center=right_joint_center,
        axis="+y",
        structure_length_mm=right_joint_12_length,
    )
    fittings["aa_right_joint_12"] = {
        "node_id": "aa_right_joint_12",
        "kind": "expansion_joint",
        "center": right_joint_center,
        "upstream_point": right_joint_start,
        "downstream_point": right_joint_end,
        "axis": "+y",
    }
    right_valve_1_center = [right_upper_anchor_x, right_valve_1_center_y, right_riser_top_elevation]
    right_valve_1_start, right_valve_1_end = compile_centered_inline_span(
        center=right_valve_1_center,
        axis="+y",
        structure_length_mm=right_valve_1_structure_length,
    )
    fittings["aa_right_valve_1"] = {
        "node_id": "aa_right_valve_1",
        "kind": "check_valve",
        "center": right_valve_1_center,
        "upstream_point": right_valve_1_start,
        "downstream_point": right_valve_1_end,
        "axis": "+y",
    }
    right_reducer_start = [right_upper_anchor_x, right_horizontal_start_y, right_riser_top_elevation]
    right_reducer_end = [right_upper_anchor_x, right_item26_large_end_y, right_riser_top_elevation]
    fittings["aa_right_reducer_26"] = {
        "node_id": "aa_right_reducer_26",
        "kind": "reducer",
        "upstream_point": right_reducer_start,
        "downstream_point": right_reducer_end,
        "axis": "+y",
    }
    right_header_anchor_x = right_upper_anchor_x
    right_elbow_trim = resolve_lr_elbow_connection_trim_mm(
        context.get("right_elbow_connection_trim_mm"),
        right_branch_dn,
    )
    right_elbow_requested_horizontal = [
        right_upper_anchor_x,
        right_header_y - right_elbow_trim,
        right_riser_top_elevation,
    ]
    right_elbow_requested_vertical = [
        right_upper_anchor_x,
        right_header_y,
        right_riser_top_elevation - right_elbow_trim,
    ]
    if draw_right_lower_branch:
        right_lower_segment = build_compiled_pipe_segment(
            segment_id=f"{route_id}_right_vertical",
            dn=right_branch_dn,
            start=[right_header_anchor_x, right_header_y, right_visible_vertical_bottom_elevation],
            end=right_elbow_requested_vertical,
            path_id="aa_right_return_chain",
            source_run_id="aa_right_riser_dn250",
            from_node_id="aa_right_header_circle",
            to_node_id="aa_right_elbow_20",
        )
        if right_lower_segment is not None:
            segments.append(right_lower_segment)
    helper_specs["right_return_elbow"] = {
        "helper_id": "right_return_elbow",
        "path_id": "aa_right_return_chain",
        "fitting_node_id": "aa_right_elbow_20",
        "run_id": "aa_right_horizontal_dn250",
        "vertical_run_id": "aa_right_riser_dn250",
        "segment_ids": {
            "horizontal": f"{route_id}_right_horizontal_d",
            "vertical": f"{route_id}_right_vertical",
        },
        "dn": right_branch_dn,
        "elbow_center": [right_upper_anchor_x, right_header_y, right_riser_top_elevation],
        "requested_horizontal_tangent": right_elbow_requested_horizontal,
        "requested_vertical_tangent": right_elbow_requested_vertical,
        "requested_horizontal_start": right_valve_end,
        "requested_vertical_end": [right_upper_anchor_x, right_header_y, right_visible_vertical_bottom_elevation],
        "run_specs": [
            PipeConnectionRunSpec(
                run_id=f"{route_id}_right_horizontal_d",
                tangent_point_mm=tuple(right_elbow_requested_horizontal),
                axis_away_from_fitting="+y",
                length_mm=max(0.0, right_elbow_requested_horizontal[1] - right_valve_end[1]),
                connection_end="left_end",
                flow_role="pipe_2",
            ),
            PipeConnectionRunSpec(
                run_id=f"{route_id}_right_vertical",
                tangent_point_mm=tuple(right_elbow_requested_vertical),
                axis_away_from_fitting="+z",
                length_mm=max(0.0, right_elbow_requested_vertical[2] - right_visible_vertical_bottom_elevation),
                connection_end="bottom_end",
                flow_role="pipe_1",
            ),
        ],
    }
    right_pairs = [
        ("aa_right_reducer_26", right_reducer_end, "aa_right_joint_12", right_joint_start),
        ("aa_right_joint_12", right_joint_end, "aa_right_valve_1", right_valve_1_start),
        ("aa_right_valve_1", right_valve_1_end, "aa_right_valve_8", right_valve_start),
    ]
    if draw_right_upper_branch:
        for index, (from_node_id, start_point, to_node_id, end_point) in enumerate(right_pairs, start=1):
            segment = build_compiled_pipe_segment(
                segment_id=f"{route_id}_right_horizontal_{chr(ord('a') + index)}",
                dn=right_branch_dn,
                start=start_point,
                end=end_point,
                path_id="aa_right_return_chain",
                source_run_id="aa_right_horizontal_dn250",
                from_node_id=from_node_id,
                to_node_id=to_node_id,
            )
            if segment is not None:
                segments.append(segment)

    return {
        "segments": segments,
        "helper_specs": helper_specs,
        "fittings": fittings,
        "flow_sequences": {
            "aa_left_side_chain": list(context.get("aa_left_flow_sequence", [])),
            "aa_p0204_left_chain": list(context.get("aa_pump_flow_sequence", [])),
            "aa_right_return_chain": list(context.get("aa_right_flow_sequence", [])),
        },
    }


def draw_cycle_water_lr_elbow(
    service: CADService,
    *,
    description: str,
    corner_point: list[float] | tuple[float, float, float],
    dn: int,
    layer: str,
    primary_rotation_deg: float = 0.0,
    primary_rotation_axis: Any = "y",
    secondary_rotation_deg: float = 0.0,
    secondary_rotation_axis: Any = "x",
    color: int = WHITE,
) -> str:
    """Draw one LR 90-degree elbow from the theoretical pipe centerline corner."""

    controller = service.controller
    elbow_corner = [float(corner_point[0]), float(corner_point[1]), float(corner_point[2])]
    draw_center = xy_90_elbow_draw_center_from_corner(elbow_corner, dn)
    elbow_entity = call_controller_with_retry(
        description,
        lambda: controller.draw_pipe_elbow(
            center=draw_center,
            dn=dn,
            elbow_type="LR",
            angle=90,
            plane="xy",
            layer=layer,
            color=color,
        ),
        retries=8,
        wait_seconds=1.2,
    )
    elbow_handle = extract_entity_handle(elbow_entity)
    if elbow_handle is None:
        raise RuntimeError(f"{description} 绘制失败。")
    if abs(float(primary_rotation_deg)) > 1e-6:
        rotate_entity_with_retry(
            service,
            handle=elbow_handle,
            base_point=elbow_corner,
            angle=float(primary_rotation_deg),
            axis_direction=resolve_rotation_axis(primary_rotation_axis),
        )
    if abs(float(secondary_rotation_deg)) > 1e-6:
        rotate_entity_with_retry(
            service,
            handle=elbow_handle,
            base_point=elbow_corner,
            angle=float(secondary_rotation_deg),
            axis_direction=resolve_rotation_axis(secondary_rotation_axis),
        )
    return elbow_handle


def draw_dd_section_local_detail(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw one dedicated D-D local detail using the same tangent logic as v6."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    detail_center_x, detail_y, _ = DD_LOCAL_DETAIL_CENTER
    horizontal_elevation = float(route["horizontal_elevation_mm"])
    riser_start_elevation = float(route["riser_start_elevation_mm"])
    horizontal_offset_length = float(route["horizontal_offset_length_mm"])
    elbow_corner_distance = float(route.get("elbow_corner_distance_mm", horizontal_offset_length))
    controller = service.controller

    bend_radius = resolve_lr_elbow_connection_trim_mm(route.get("upper_elbow_connection_trim_mm"), branch_dn)
    local_upper_corner_x = detail_center_x - 900.0
    local_lower_corner_x = local_upper_corner_x + elbow_corner_distance

    upper_vertical_requested = [local_upper_corner_x, detail_y, horizontal_elevation + bend_radius]
    upper_horizontal_requested = [local_upper_corner_x + bend_radius, detail_y, horizontal_elevation]
    lower_horizontal_requested = [local_lower_corner_x - bend_radius, detail_y, horizontal_elevation]
    lower_vertical_requested = [local_lower_corner_x, detail_y, horizontal_elevation - bend_radius]
    upper_vertical_length = riser_start_elevation - upper_vertical_requested[2]
    horizontal_run_length = lower_horizontal_requested[0] - upper_horizontal_requested[0]
    lower_vertical_validation_length = horizontal_elevation - lower_vertical_requested[2]

    upper_elbow_geometry = build_orthogonal_elbow_connection(
        controller,
        dn=branch_dn,
        elbow_center=(local_upper_corner_x, detail_y, horizontal_elevation),
        elbow_type="LR",
        run_specs=[
            PipeConnectionRunSpec(
                run_id=f"{bundle_id}_detail_upper_vertical",
                tangent_point_mm=tuple(upper_vertical_requested),
                axis_away_from_fitting="-z",
                length_mm=float(upper_vertical_length),
                connection_end="top_end",
                flow_role="pipe_1",
            ),
            PipeConnectionRunSpec(
                run_id=f"{bundle_id}_detail_upper_horizontal",
                tangent_point_mm=tuple(upper_horizontal_requested),
                axis_away_from_fitting="-x",
                length_mm=float(horizontal_run_length),
                connection_end="right_end",
                flow_role="pipe_2",
            ),
        ],
        elbow_rotations=[
            RotationInstruction(angle_deg=-90.0, axis_direction=(1.0, 0.0, 0.0), axis_name="x"),
            RotationInstruction(angle_deg=90.0, axis_direction=(0.0, 1.0, 0.0), axis_name="y"),
        ],
        layer=f"PIPE_DN{branch_dn}",
        color=WHITE,
        draw_runs=False,
    )
    lower_elbow_geometry = build_orthogonal_elbow_connection(
        controller,
        dn=branch_dn,
        elbow_center=(local_lower_corner_x, detail_y, horizontal_elevation),
        elbow_type="LR",
        run_specs=[
            PipeConnectionRunSpec(
                run_id=f"{bundle_id}_detail_lower_horizontal",
                tangent_point_mm=tuple(lower_horizontal_requested),
                axis_away_from_fitting="+x",
                length_mm=float(horizontal_run_length),
                connection_end="left_end",
                flow_role="pipe_1",
            ),
            PipeConnectionRunSpec(
                run_id=f"{bundle_id}_detail_lower_vertical_validation",
                tangent_point_mm=tuple(lower_vertical_requested),
                axis_away_from_fitting="+z",
                length_mm=float(lower_vertical_validation_length),
                connection_end="bottom_end",
                flow_role="pipe_2",
            ),
        ],
        elbow_rotations=[
            RotationInstruction(angle_deg=-90.0, axis_direction=(0.0, 1.0, 0.0), axis_name="y"),
            RotationInstruction(angle_deg=-90.0, axis_direction=(0.0, 0.0, 1.0), axis_name="z"),
        ],
        layer=f"PIPE_DN{branch_dn}",
        color=WHITE,
        draw_runs=False,
    )

    upper_runs_by_id = {run["run_id"]: run for run in upper_elbow_geometry["runs"]}
    lower_runs_by_id = {run["run_id"]: run for run in lower_elbow_geometry["runs"]}
    vertical_start = [float(value) for value in upper_runs_by_id[f"{bundle_id}_detail_upper_vertical"]["tangent_point_mm"]]
    vertical_end = [local_upper_corner_x, detail_y, riser_start_elevation]
    horizontal_start = [float(value) for value in upper_runs_by_id[f"{bundle_id}_detail_upper_horizontal"]["tangent_point_mm"]]
    horizontal_end = [float(value) for value in lower_runs_by_id[f"{bundle_id}_detail_lower_horizontal"]["tangent_point_mm"]]

    vertical_pipe_handle: str | None = None
    horizontal_pipe_handle: str | None = None
    if vertical_end[2] - vertical_start[2] > 1e-6:
        draw_pipe_segment(service, start=vertical_start, end=vertical_end, dn=branch_dn)
    if horizontal_end[0] - horizontal_start[0] > 1e-6:
        draw_pipe_segment(service, start=horizontal_start, end=horizontal_end, dn=branch_dn)

    route_records.append(
        {
            "bundle_id": bundle_id,
            "segment_id": f"{bundle_id}_local_detail",
            "category": "section_detail_preview",
            "detail_center": [DD_LOCAL_DETAIL_CENTER[0], DD_LOCAL_DETAIL_CENTER[1], DD_LOCAL_DETAIL_CENTER[2]],
            "detail_view_height_mm": DD_LOCAL_DETAIL_VIEW_HEIGHT_MM,
            "detail_view_width_mm": DD_LOCAL_DETAIL_VIEW_WIDTH_MM,
            "entities": {
                "vertical_pipe": vertical_pipe_handle,
                "horizontal_pipe": horizontal_pipe_handle,
            },
        }
    )


def helper_run_to_segment_endpoints(run: dict[str, Any]) -> tuple[list[float], list[float]]:
    """Convert one helper run record into ordered segment endpoints."""

    tangent = [float(value) for value in run["tangent_point_mm"]]
    far_end = [float(value) for value in run["far_end_point_mm"]]
    axis = str(run.get("axis", "")).strip().lower()
    if axis in {"-x", "-y", "-z"}:
        return far_end, tangent
    return tangent, far_end


def draw_section_preview_fittings(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
    skip_left_elbow: bool = False,
    skip_right_elbow: bool = False,
) -> None:
    """Draw the fittings explicitly called out by one section preview route."""

    if not bool(route.get("draw_fittings", False)):
        return

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    tee_proxy_run_dn = int(route.get("tee_proxy_run_dn", route.get("base_header_dn", branch_dn)))
    anchor_x = float(route["anchor_x"])
    base_y = float(route["base_y"])
    base_z = float(route["base_z"])
    right_vertical_y = float(route["right_vertical_y"])
    top_horizontal_elevation = float(route["top_horizontal_elevation_mm"])
    valve_center_elevation = float(route["valve_center_elevation_mm"])
    tee_run_center_to_end = route.get("tee_run_center_to_end_mm")
    tee_branch_center_to_end = route.get("tee_branch_center_to_end_mm")
    branch_layer = f"PIPE_DN{branch_dn}"
    tee_layer = f"PIPE_DN{tee_proxy_run_dn}"
    controller = service.controller
    # 弯头的 center 必须就是相邻两根直管中心线的理论交点，
    # 不能再通过额外上/下偏移把弯头从该交点挪开。
    left_elbow_center = [
        anchor_x,
        base_y,
        top_horizontal_elevation,
    ]
    right_elbow_center = [
        anchor_x,
        right_vertical_y,
        top_horizontal_elevation,
    ]

    tee_entity = call_controller_with_retry(
        f"{bundle_id} tee",
        lambda: controller.draw_tee(
            center=(anchor_x, base_y, base_z),
            dn=tee_proxy_run_dn,
            branch_dn=branch_dn,
            run_axis="x",
            branch_axis="z",
            run_center_to_end=tee_run_center_to_end,
            branch_center_to_end=tee_branch_center_to_end,
            layer=tee_layer,
            color=WHITE,
        ),
    )
    if tee_entity is None:
        raise RuntimeError(f"{bundle_id} 的 13 号异径三通绘制失败。")
    route_records.append(
        {
            "record_type": "fitting",
            "bundle_id": bundle_id,
            "fitting_id": f"{bundle_id}_item_{route.get('fitting_item_no', 'tee')}",
            "category": "section_preview_fitting",
            "kind": "reducing_tee" if branch_dn != tee_proxy_run_dn else "tee",
            "item_no": route.get("fitting_item_no"),
            "center": [anchor_x, base_y, base_z],
            "handle": extract_entity_handle(tee_entity),
            "basis": str(route.get("basis", "")),
        }
    )

    requested_valve_pn = float(route.get("valve_pn", 10.0))
    resolved_valve_pn = requested_valve_pn
    if controller._get_flange_params(branch_dn, requested_valve_pn) is None:
        fallback_valve_pn = 16.0
        if controller._get_flange_params(branch_dn, fallback_valve_pn) is None:
            raise RuntimeError(f"{bundle_id} 的 4 号蝶阀缺少可用法兰规格: DN{branch_dn} PN{requested_valve_pn}")
        resolved_valve_pn = fallback_valve_pn

    valve_entity = call_controller_with_retry(
        f"{bundle_id} butterfly valve",
        lambda: controller.draw_butterfly_valve(
            center=(anchor_x, base_y, valve_center_elevation),
            dn=branch_dn,
            pn=resolved_valve_pn,
            axis="z",
            layer=branch_layer,
            color=WHITE,
        ),
    )
    if valve_entity is None:
        raise RuntimeError(f"{bundle_id} 的 4 号蝶阀绘制失败。")
    route_records.append(
        {
            "record_type": "fitting",
            "bundle_id": bundle_id,
            "fitting_id": f"{bundle_id}_item_{route.get('valve_item_no', 'valve')}",
            "category": "section_preview_fitting",
            "kind": "butterfly_valve",
            "item_no": route.get("valve_item_no"),
            "center": [anchor_x, base_y, valve_center_elevation],
            "handle": extract_entity_handle(valve_entity),
            "requested_pn": requested_valve_pn,
            "resolved_pn": resolved_valve_pn,
            "basis": str(route.get("basis", "")),
        }
    )

    if not skip_left_elbow:
        left_elbow_handle = draw_cycle_water_lr_elbow(
            service,
            description=f"{bundle_id} left elbow",
            corner_point=left_elbow_center,
            dn=branch_dn,
            layer=branch_layer,
            primary_rotation_deg=float(route.get("left_elbow_rotation_deg", 90.0)),
            primary_rotation_axis=route.get("left_elbow_rotation_axis", "y"),
            secondary_rotation_deg=float(route.get("left_elbow_secondary_rotation_deg", 0.0)),
            secondary_rotation_axis=route.get("left_elbow_secondary_rotation_axis", "x"),
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_left_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=route.get("elbow_item_no"),
            center=left_elbow_center,
            handle=left_elbow_handle,
            basis=str(route.get("basis", "")),
        )

    if not skip_right_elbow:
        right_elbow_handle = draw_cycle_water_lr_elbow(
            service,
            description=f"{bundle_id} right elbow",
            corner_point=right_elbow_center,
            dn=branch_dn,
            layer=branch_layer,
            primary_rotation_deg=float(route.get("right_elbow_rotation_deg", -90.0)),
            primary_rotation_axis=route.get("right_elbow_rotation_axis", "y"),
            secondary_rotation_deg=float(route.get("right_elbow_secondary_rotation_deg", 0.0)),
            secondary_rotation_axis=route.get("right_elbow_secondary_rotation_axis", "x"),
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_right_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=route.get("elbow_item_no"),
            center=right_elbow_center,
            handle=right_elbow_handle,
            basis=str(route.get("basis", "")),
        )


def draw_cycle_water_lr_elbow_from_rotation_sequence(
    service: CADService,
    *,
    description: str,
    corner_point: list[float] | tuple[float, float, float],
    dn: int,
    layer: str,
    elbow_rotations: list[RotationInstruction] | None,
    color: int = WHITE,
) -> str | None:
    """Draw one LR elbow directly from one matched rotation sequence."""

    if not elbow_rotations:
        return None
    primary_rotation = elbow_rotations[0]
    secondary_rotation = elbow_rotations[1] if len(elbow_rotations) > 1 else None
    return draw_cycle_water_lr_elbow(
        service,
        description=description,
        corner_point=corner_point,
        dn=dn,
        layer=layer,
        primary_rotation_deg=float(primary_rotation.angle_deg),
        primary_rotation_axis=primary_rotation.axis_name or primary_rotation.axis_direction,
        secondary_rotation_deg=float(secondary_rotation.angle_deg) if secondary_rotation is not None else 0.0,
        secondary_rotation_axis=(
            secondary_rotation.axis_name or secondary_rotation.axis_direction
            if secondary_rotation is not None
            else "x"
        ),
        color=color,
    )


def draw_section_preview_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw one section preview route using the stable V23 legacy geometry."""

    if str(route.get("route_kind", "")).strip().lower() == "aa_main_section_slice":
        draw_aa_section_preview_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "gg_dn100_local_loop":
        draw_gg_section_preview_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "gg_dn100_plan_external_connection":
        draw_gg_plan_external_connection_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "aa_dn200_plan_external_connection":
        draw_aa_dn200_plan_external_connection_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "bb_dn100_bottom_elbow_preview":
        draw_bb_section_preview_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "bb_dn100_plan_external_connection":
        draw_bb_plan_external_connection_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "dd_dn200_turn_preview":
        draw_dd_section_preview_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "ff_dn150_local_return_preview":
        draw_ff_section_preview_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "ff_dn150_plan_external_connection":
        draw_ff_plan_external_connection_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "cc_dn200_left_wall_section":
        draw_cc_section_preview_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "cc_dn200_plan_extension_preview":
        draw_cc_plan_extension_preview_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "hh_dn125_tower_drop_preview":
        draw_hh_section_preview_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "hh_dn125_plan_external_connection":
        draw_hh_plan_external_connection_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "ii_dn150_local_drop_preview":
        draw_ii_section_preview_route(service, route=route, route_records=route_records)
        return
    if str(route.get("route_kind", "")).strip().lower() == "ii_dn150_plan_external_connection":
        draw_ii_plan_external_connection_route(service, route=route, route_records=route_records)
        return

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    anchor_x = float(route["anchor_x"])
    base_y = float(route["base_y"])
    base_z = float(route["base_z"])
    right_vertical_y = float(route["right_vertical_y"])
    top_horizontal_elevation = float(route["top_horizontal_elevation_mm"])
    right_vertical_bottom_elevation = float(route["right_vertical_bottom_elevation_mm"])
    tee_branch_center_to_end = float(route.get("tee_branch_center_to_end_mm", 0.0))
    tee_branch_display_offset = float(route.get("tee_branch_display_offset_mm", tee_branch_center_to_end))
    valve_center_elevation = float(route.get("valve_center_elevation_mm", top_horizontal_elevation))
    valve_structure_length = float(route.get("valve_structure_length_mm", 0.0))
    valve_half_length = valve_structure_length / 2.0 if valve_structure_length > 0 else 0.0
    left_elbow_connection_trim = resolve_lr_elbow_connection_trim_mm(
        route.get("left_elbow_connection_trim_mm"),
        branch_dn,
    )
    left_elbow_type = str(route.get("left_elbow_type", "LR")).strip().upper()
    right_elbow_type = str(route.get("right_elbow_type", "LR")).strip().upper()
    left_elbow_connection_trim = float(
        route.get("left_elbow_connection_trim_mm")
        if route.get("left_elbow_connection_trim_mm") is not None
        else resolve_elbow_bend_radius(controller, branch_dn, left_elbow_type)
    )
    right_elbow_connection_trim = float(
        route.get("right_elbow_connection_trim_mm")
        if route.get("right_elbow_connection_trim_mm") is not None
        else resolve_elbow_bend_radius(controller, branch_dn, right_elbow_type)
    )
    basis = str(route.get("basis", ""))
    lower_vertical_start = [anchor_x, base_y, base_z + tee_branch_display_offset]
    lower_vertical_end = [anchor_x, base_y, valve_center_elevation - valve_half_length]
    if lower_vertical_end[2] - lower_vertical_start[2] > 1e-6:
        draw_pipe_segment(service, start=lower_vertical_start, end=lower_vertical_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_lower_vertical",
            category="section_preview",
            dn=branch_dn,
            start=lower_vertical_start,
            end=lower_vertical_end,
            basis=basis,
        )

    upper_vertical_far_end = (anchor_x, base_y, valve_center_elevation + valve_half_length)
    left_vertical_tangent_point = (anchor_x, base_y, top_horizontal_elevation - left_elbow_connection_trim)
    left_horizontal_tangent_point = (anchor_x, base_y + left_elbow_connection_trim, top_horizontal_elevation)
    right_horizontal_tangent_point = (anchor_x, right_vertical_y - right_elbow_connection_trim, top_horizontal_elevation)
    right_vertical_tangent_point = (anchor_x, right_vertical_y, top_horizontal_elevation - right_elbow_connection_trim)

    left_vertical_available = left_vertical_tangent_point[2] - upper_vertical_far_end[2]
    top_horizontal_available = right_horizontal_tangent_point[1] - left_horizontal_tangent_point[1]
    if left_vertical_available > 1e-6:
        upper_vertical_start = list(upper_vertical_far_end)
        upper_vertical_end = list(left_vertical_tangent_point)
        draw_pipe_segment(service, start=upper_vertical_start, end=upper_vertical_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_upper_vertical",
            category="section_preview",
            dn=branch_dn,
            start=upper_vertical_start,
            end=upper_vertical_end,
            basis=basis,
        )

    if top_horizontal_available > 1e-6:
        horizontal_start = list(left_horizontal_tangent_point)
        horizontal_end = list(right_horizontal_tangent_point)
        draw_pipe_segment(service, start=horizontal_start, end=horizontal_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_top_horizontal",
            category="section_preview",
            dn=branch_dn,
            start=horizontal_start,
            end=horizontal_end,
            basis=basis,
        )

    right_vertical_start = list(right_vertical_tangent_point)
    right_vertical_end = [anchor_x, right_vertical_y, right_vertical_bottom_elevation]
    if right_vertical_start[2] - right_vertical_end[2] > 1e-6:
        draw_pipe_segment(service, start=right_vertical_start, end=right_vertical_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_right_vertical",
            category="section_preview",
            dn=branch_dn,
            start=right_vertical_start,
            end=right_vertical_end,
            basis=basis,
        )

    draw_section_preview_fittings(
        service,
        route=route,
        route_records=route_records,
        skip_left_elbow=False,
        skip_right_elbow=False,
    )


def draw_bb_section_preview_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the B-B local DN100 riser, valve chain, and short right drop."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    anchor_x = float(route["anchor_x"])
    section_plane_y = float(route["section_plane_y"])
    top_peak_elevation = float(route["top_elevation_mm"])
    bottom_elevation = float(route["bottom_elevation_mm"])
    top_horizontal_elevation = float(route.get("top_horizontal_elevation_mm", top_peak_elevation))
    top_horizontal_length = float(route.get("top_horizontal_length_mm", 0.0))
    right_drop_bottom_elevation = float(route.get("right_drop_bottom_elevation_mm", 1900.0))
    elbow_item_no = route.get("elbow_item_no")
    valve_7_item_no = route.get("valve_7_item_no")
    valve_2_item_no = route.get("valve_2_item_no")
    basis = str(route.get("basis", ""))
    branch_layer = f"PIPE_DN{branch_dn}"
    controller = service.controller
    valve_7_structure_length = float(controller.VALVE_BUTTERFLY_STRUCTURE_LENGTH.get(branch_dn, 0.0))
    valve_2_structure_length = float(controller.VALVE_BALL_STRUCTURE_LENGTH.get(branch_dn, valve_7_structure_length))
    valve_chain_gap = float(route.get("valve_chain_gap_mm", 60.0))
    pipe_outer_radius = resolve_pipe_outer_radius_mm(branch_dn)
    minimum_valve_chain_length = valve_7_structure_length + valve_2_structure_length + valve_chain_gap
    requested_top_horizontal_length = top_horizontal_length
    effective_top_horizontal_length = top_horizontal_length
    if (
        bool(route.get("draw_fittings", True))
        and valve_7_structure_length > 1e-6
        and valve_2_structure_length > 1e-6
    ):
        minimum_right_stub_length = max(valve_chain_gap, pipe_outer_radius + 60.0)
        effective_top_horizontal_length = max(
            top_horizontal_length,
            valve_7_structure_length + valve_2_structure_length + valve_chain_gap * 2.0 + minimum_right_stub_length,
        )

    left_elbow_trim = resolve_lr_elbow_connection_trim_mm(None, branch_dn)
    right_elbow_trim = resolve_lr_elbow_connection_trim_mm(
        route.get("right_elbow_connection_trim_mm"),
        branch_dn,
    )

    vertical_start = [anchor_x, section_plane_y, bottom_elevation]
    left_vertical_requested_tangent = [anchor_x, section_plane_y, top_horizontal_elevation - left_elbow_trim]
    left_horizontal_requested_tangent = [anchor_x + left_elbow_trim, section_plane_y, top_horizontal_elevation]
    right_turn_x = anchor_x + left_elbow_trim + effective_top_horizontal_length + right_elbow_trim
    right_horizontal_requested_tangent = [right_turn_x - right_elbow_trim, section_plane_y, top_horizontal_elevation]
    right_elbow_corner = [
        right_horizontal_requested_tangent[0] + right_elbow_trim,
        section_plane_y,
        top_horizontal_elevation,
    ]
    right_drop_requested_tangent = [
        right_elbow_corner[0],
        section_plane_y,
        top_horizontal_elevation - right_elbow_trim,
    ]
    right_drop_end = [right_drop_requested_tangent[0], section_plane_y, right_drop_bottom_elevation]
    left_vertical_length = left_vertical_requested_tangent[2] - vertical_start[2]
    top_horizontal_length_available = right_horizontal_requested_tangent[0] - left_horizontal_requested_tangent[0]
    right_drop_length = right_drop_requested_tangent[2] - right_drop_end[2]

    left_elbow_geometry = None
    right_elbow_geometry = None
    right_elbow_handle: str | None = None
    if bool(route.get("draw_fittings", True)) and left_vertical_length > 1e-6 and top_horizontal_length_available > 1e-6:
        left_elbow_geometry = select_matching_elbow_geometry(
            controller,
            dn=branch_dn,
            elbow_center=(anchor_x, section_plane_y, top_horizontal_elevation),
            bend_radius=left_elbow_trim,
            run_specs=[
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_left_vertical_helper",
                    tangent_point_mm=tuple(left_vertical_requested_tangent),
                    axis_away_from_fitting="+z",
                    length_mm=float(left_vertical_length),
                    connection_end="bottom_end",
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_top_horizontal_left_helper",
                    tangent_point_mm=tuple(left_horizontal_requested_tangent),
                    axis_away_from_fitting="-x",
                    length_mm=float(top_horizontal_length_available),
                    connection_end="right_end",
                    flow_role="pipe_2",
                ),
            ],
            rotation_candidates=build_common_elbow_rotation_candidates(),
            layer=branch_layer,
            validator=lambda ports: ports_cover_exact_targets(
                ports,
                [
                    {
                        "axis": "+z",
                        "connection_end": "bottom_end",
                        "expected_point_mm": tuple(left_vertical_requested_tangent),
                    },
                    {
                        "axis": "-x",
                        "connection_end": "right_end",
                        "expected_point_mm": tuple(left_horizontal_requested_tangent),
                    },
                ],
                tolerance_mm=1.0,
            ),
        )

    if bool(route.get("draw_fittings", True)) and right_drop_length > 1e-6 and top_horizontal_length_available > 1e-6:
        right_elbow_handle = draw_cycle_water_lr_elbow(
            service,
            description=f"{bundle_id} right top elbow",
            corner_point=right_elbow_corner,
            dn=branch_dn,
            layer=branch_layer,
            primary_rotation_deg=90.0,
            primary_rotation_axis="x",
            secondary_rotation_deg=0.0,
            secondary_rotation_axis="z",
            color=WHITE,
        )

    left_vertical_tangent = left_vertical_requested_tangent
    top_horizontal_left_tangent = left_horizontal_requested_tangent
    top_horizontal_right_tangent = right_horizontal_requested_tangent
    right_drop_top_tangent = right_drop_requested_tangent
    if left_elbow_geometry is not None:
        left_helper_runs = {run["run_id"]: run for run in left_elbow_geometry["runs"]}
        left_vertical_tangent = [
            float(value) for value in left_helper_runs[f"{bundle_id}_left_vertical_helper"]["tangent_point_mm"]
        ]
        top_horizontal_left_tangent = [
            float(value) for value in left_helper_runs[f"{bundle_id}_top_horizontal_left_helper"]["tangent_point_mm"]
        ]
    if right_elbow_geometry is not None:
        right_helper_runs = {run["run_id"]: run for run in right_elbow_geometry["runs"]}
        top_horizontal_right_tangent = [
            float(value) for value in right_helper_runs[f"{bundle_id}_top_horizontal_right_helper"]["tangent_point_mm"]
        ]
        right_drop_top_tangent = [
            float(value) for value in right_helper_runs[f"{bundle_id}_right_drop_helper"]["tangent_point_mm"]
        ]

    if left_vertical_tangent[2] - vertical_start[2] > 1e-6:
        draw_pipe_segment(service, start=vertical_start, end=left_vertical_tangent, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_vertical",
            category="section_preview",
            dn=branch_dn,
            start=vertical_start,
            end=left_vertical_tangent,
            basis=basis,
            path_id="bb_local_dn100_chain",
            source_run_id="bb_local_riser_dn100",
            from_node_id=None,
            to_node_id="bb_left_turn_24",
        )
    valve_7_center: list[float] | None = None
    valve_2_center: list[float] | None = None
    requested_valve_7_pn = float(route.get("valve_7_pn", 10.0))
    resolved_valve_7_pn = requested_valve_7_pn
    if controller._get_flange_params(branch_dn, requested_valve_7_pn) is None:
        resolved_valve_7_pn = 16.0
    requested_valve_2_pn = float(route.get("valve_2_pn", 10.0))
    resolved_valve_2_pn = requested_valve_2_pn
    if controller._get_flange_params(branch_dn, requested_valve_2_pn) is None:
        resolved_valve_2_pn = 16.0
    valve_2_connection_type = str(route.get("valve_2_connection_type", "threaded")).strip().lower()
    requested_top_horizontal_total_length = right_horizontal_requested_tangent[0] - left_horizontal_requested_tangent[0]

    # When helper tangents over-trim the short B-B chain, keep the validated requested
    # tangent positions so the 7/2 valve zone and right drop are not swallowed.
    if (
        requested_top_horizontal_total_length > minimum_valve_chain_length
        and top_horizontal_right_tangent[0] - top_horizontal_left_tangent[0] <= minimum_valve_chain_length
    ):
        top_horizontal_left_tangent = left_horizontal_requested_tangent
        top_horizontal_right_tangent = right_horizontal_requested_tangent
    if requested_top_horizontal_total_length > minimum_valve_chain_length:
        # B-B 右端必须显式保留“球阀 -> 短横管 -> 弯头 -> 短竖管”这条链；
        # 即使 helper 落图成功，也不允许继续吃掉右侧短横管或下翻短竖管。
        top_horizontal_right_tangent = right_horizontal_requested_tangent
        right_drop_top_tangent = right_drop_requested_tangent
    if (
        right_drop_requested_tangent[2] - right_drop_end[2] > 1e-6
        and right_drop_top_tangent[2] - right_drop_end[2] <= 1e-6
    ):
        right_drop_top_tangent = right_drop_requested_tangent

    top_horizontal_total_length = top_horizontal_right_tangent[0] - top_horizontal_left_tangent[0]
    should_draw_bb_valve_chain = (
        bool(route.get("draw_fittings", True))
        and valve_7_structure_length > 1e-6
        and valve_2_structure_length > 1e-6
        and top_horizontal_total_length > minimum_valve_chain_length
    )
    if should_draw_bb_valve_chain:
        inter_segment_gap = max(
            valve_chain_gap,
            (top_horizontal_total_length - valve_7_structure_length - valve_2_structure_length) / 3.0,
        )
        valve_7_center_x = top_horizontal_left_tangent[0] + inter_segment_gap + valve_7_structure_length / 2.0
        valve_2_center_x = valve_7_center_x + valve_7_structure_length / 2.0 + inter_segment_gap + valve_2_structure_length / 2.0
        valve_7_start = [valve_7_center_x - valve_7_structure_length / 2.0, section_plane_y, top_horizontal_elevation]
        valve_7_end = [valve_7_center_x + valve_7_structure_length / 2.0, section_plane_y, top_horizontal_elevation]
        valve_2_start = [valve_2_center_x - valve_2_structure_length / 2.0, section_plane_y, top_horizontal_elevation]
        valve_2_end = [valve_2_center_x + valve_2_structure_length / 2.0, section_plane_y, top_horizontal_elevation]
        valve_7_center = [valve_7_center_x, section_plane_y, top_horizontal_elevation]
        valve_2_center = [valve_2_center_x, section_plane_y, top_horizontal_elevation]

        if valve_7_start[0] - top_horizontal_left_tangent[0] > 1e-6:
            draw_pipe_segment(service, start=top_horizontal_left_tangent, end=valve_7_start, dn=branch_dn)
            append_route_segment_record(
                route_records,
                bundle_id=bundle_id,
                segment_id=f"{bundle_id}_top_horizontal_left_stub",
                category="section_preview",
                dn=branch_dn,
                start=top_horizontal_left_tangent,
                end=valve_7_start,
                basis=basis,
                path_id="bb_local_dn100_chain",
                source_run_id="bb_top_horizontal_dn100",
                from_node_id="bb_left_turn_24",
                to_node_id="bb_valve_7",
            )
        if valve_2_start[0] - valve_7_end[0] > 1e-6:
            draw_pipe_segment(service, start=valve_7_end, end=valve_2_start, dn=branch_dn)
            append_route_segment_record(
                route_records,
                bundle_id=bundle_id,
                segment_id=f"{bundle_id}_top_horizontal_middle_stub",
                category="section_preview",
                dn=branch_dn,
                start=valve_7_end,
                end=valve_2_start,
                basis=basis,
                path_id="bb_local_dn100_chain",
                source_run_id="bb_top_horizontal_dn100",
                from_node_id="bb_valve_7",
                to_node_id="bb_valve_2",
            )
        if top_horizontal_right_tangent[0] - valve_2_end[0] > 1e-6:
            draw_pipe_segment(service, start=valve_2_end, end=top_horizontal_right_tangent, dn=branch_dn)
            append_route_segment_record(
                route_records,
                bundle_id=bundle_id,
                segment_id=f"{bundle_id}_top_horizontal_right_stub",
                category="section_preview",
                dn=branch_dn,
                start=valve_2_end,
                end=top_horizontal_right_tangent,
                basis=basis,
                path_id="bb_local_dn100_chain",
                source_run_id="bb_top_horizontal_dn100",
                from_node_id="bb_valve_2",
                to_node_id=None,
            )
    elif top_horizontal_total_length > 1e-6:
        draw_pipe_segment(
            service,
            start=top_horizontal_left_tangent,
            end=top_horizontal_right_tangent,
            dn=branch_dn,
        )
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_top_horizontal",
            category="section_preview",
            dn=branch_dn,
            start=top_horizontal_left_tangent,
            end=top_horizontal_right_tangent,
            basis=basis,
            path_id="bb_local_dn100_chain",
            source_run_id="bb_top_horizontal_dn100",
            from_node_id="bb_left_turn_24",
            to_node_id=None,
        )
    if right_drop_top_tangent[2] - right_drop_end[2] > 1e-6:
        draw_pipe_segment(service, start=right_drop_top_tangent, end=right_drop_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_right_drop",
            category="section_preview",
            dn=branch_dn,
            start=right_drop_top_tangent,
            end=right_drop_end,
            basis=basis,
            path_id="bb_local_dn100_chain",
            source_run_id="bb_right_drop_dn100",
            from_node_id="bb_valve_2" if valve_2_center is not None else None,
            to_node_id=None,
        )

    if bool(route.get("draw_fittings", True)):
        if valve_7_center is not None:
            valve_7_entity = call_controller_with_retry(
                f"{bundle_id} valve 7",
                lambda: controller.draw_butterfly_valve(
                    center=tuple(valve_7_center),
                    dn=branch_dn,
                    pn=resolved_valve_7_pn,
                    axis="x",
                    layer=branch_layer,
                    color=WHITE,
                ),
            )
            if valve_7_entity is None:
                valve_7_entity = draw_box_with_retry(
                    controller,
                    center=tuple(valve_7_center),
                    length=max(valve_7_structure_length, 48.0),
                    width=max(pipe_outer_radius * 2.2, 52.0),
                    height=max(pipe_outer_radius * 2.2, 52.0),
                    layer=branch_layer,
                    color=WHITE,
                )
            append_route_fitting_record(
                route_records,
                bundle_id=bundle_id,
                fitting_id=f"{bundle_id}_valve_7",
                category="section_preview_fitting",
                kind="butterfly_valve",
                item_no=valve_7_item_no,
                center=valve_7_center,
                handle=extract_entity_handle(valve_7_entity),
                basis=basis,
            )
        if valve_2_center is not None:
            valve_2_entity = call_controller_with_retry(
                f"{bundle_id} valve 2",
                lambda: controller.draw_ball_valve(
                    center=tuple(valve_2_center),
                    dn=branch_dn,
                    pn=resolved_valve_2_pn,
                    axis="x",
                    layer=branch_layer,
                    color=WHITE,
                    connection_type=valve_2_connection_type,
                ),
            )
            if valve_2_entity is None:
                valve_2_entity = draw_box_with_retry(
                    controller,
                    center=tuple(valve_2_center),
                    length=max(valve_2_structure_length, 64.0),
                    width=max(pipe_outer_radius * 2.4, 56.0),
                    height=max(pipe_outer_radius * 2.4, 56.0),
                    layer=branch_layer,
                    color=WHITE,
                )
            append_route_fitting_record(
                route_records,
                bundle_id=bundle_id,
                fitting_id=f"{bundle_id}_valve_2",
                category="section_preview_fitting",
                kind="ball_valve",
                item_no=valve_2_item_no,
                center=valve_2_center,
                handle=extract_entity_handle(valve_2_entity),
                basis=(
                    f"{basis} 按用户最新指出的 PDF 真值，2 号件改按 DN100 球阀表达，"
                    "不再按液控蝶阀或执行机构代理处理。"
                ),
            )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_left_top_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=[anchor_x, section_plane_y, top_horizontal_elevation],
            handle=left_elbow_geometry["elbow_handle"] if left_elbow_geometry is not None else None,
            basis=basis,
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_right_top_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=None,
            center=right_elbow_corner,
            handle=right_elbow_handle,
            basis=basis,
        )


def draw_gg_plan_external_connection_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the plan-only DN100 outlet extending from the frozen G-G bottom elbow."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    anchor_x = float(route["anchor_x"])
    base_header_y = float(route.get("base_header_y", 0.0))
    left_vertical_y = float(route["left_vertical_y"])
    bottom_elevation = float(route["bottom_horizontal_elevation_mm"])
    outlet_length = float(route.get("outlet_length_mm", 0.0))
    target_main_x = float(route.get("target_main_x", anchor_x - outlet_length))
    second_elbow_offset = float(route.get("second_elbow_offset_from_base_header_mm", 1500.0))
    elbow_item_no = route.get("elbow_item_no")
    basis = str(route.get("basis", ""))
    branch_layer = f"PIPE_DN{branch_dn}"
    controller = service.controller
    elbow_trim = resolve_lr_elbow_connection_trim_mm(None, branch_dn)
    second_elbow_y = base_header_y - second_elbow_offset

    if target_main_x >= anchor_x - 1e-6:
        return

    first_elbow_outlet_requested = [anchor_x, left_vertical_y - elbow_trim, bottom_elevation]
    second_elbow_inlet_requested = [anchor_x, second_elbow_y + elbow_trim, bottom_elevation]
    second_elbow_outlet_requested = [anchor_x - elbow_trim, second_elbow_y, bottom_elevation]
    outlet_end = [target_main_x, second_elbow_y, bottom_elevation]
    section_vertical_requested = [anchor_x, left_vertical_y, bottom_elevation + elbow_trim]

    first_elbow_geometry = None
    if bool(route.get("draw_fittings", True)):
        first_elbow_geometry = select_matching_elbow_geometry(
            controller,
            dn=branch_dn,
            elbow_center=(anchor_x, left_vertical_y, bottom_elevation),
            bend_radius=elbow_trim,
            run_specs=[
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_section_vertical_helper",
                    tangent_point_mm=tuple(section_vertical_requested),
                    axis_away_from_fitting="-z",
                    length_mm=float(elbow_trim),
                    connection_end="top_end",
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_pipe1_from_first_elbow",
                    tangent_point_mm=tuple(first_elbow_outlet_requested),
                    axis_away_from_fitting="+y",
                    length_mm=float(max(left_vertical_y - second_elbow_y, elbow_trim)),
                    connection_end="left_end",
                    flow_role="pipe_2",
                ),
            ],
            rotation_candidates=build_common_elbow_rotation_candidates(),
            layer=branch_layer,
            validator=lambda ports: ports_cover_targets(
                ports,
                [
                    {
                        "axis": "-z",
                        "connection_end": "top_end",
                        "coordinate_index": 2,
                        "relation": "gt",
                        "reference_value": bottom_elevation,
                    },
                    {
                        "axis": "+y",
                        "connection_end": "left_end",
                        "coordinate_index": 1,
                        "relation": "lt",
                        "reference_value": left_vertical_y,
                    },
                ],
            ),
        )

    second_elbow_geometry = None
    if bool(route.get("draw_fittings", True)):
        second_elbow_geometry = select_matching_elbow_geometry(
            controller,
            dn=branch_dn,
            elbow_center=(anchor_x, second_elbow_y, bottom_elevation),
            bend_radius=elbow_trim,
            run_specs=[
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_pipe1_to_second_elbow",
                    tangent_point_mm=tuple(second_elbow_inlet_requested),
                    axis_away_from_fitting="-y",
                    length_mm=float(max(left_vertical_y - second_elbow_y, elbow_trim)),
                    connection_end="right_end",
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_outlet_helper",
                    tangent_point_mm=tuple(second_elbow_outlet_requested),
                    axis_away_from_fitting="+x",
                    length_mm=float(max(anchor_x - target_main_x, elbow_trim)),
                    connection_end="left_end",
                    flow_role="pipe_2",
                ),
            ],
            rotation_candidates=build_common_elbow_rotation_candidates(),
            layer=branch_layer,
            validator=lambda ports: ports_cover_targets(
                ports,
                [
                    {
                        "axis": "-y",
                        "connection_end": "right_end",
                        "coordinate_index": 1,
                        "relation": "gt",
                        "reference_value": second_elbow_y,
                    },
                    {
                        "axis": "+x",
                        "connection_end": "left_end",
                        "coordinate_index": 0,
                        "relation": "lt",
                        "reference_value": anchor_x,
                    },
                ],
            ),
        )

    first_pipe_actual_start = first_elbow_outlet_requested
    if first_elbow_geometry is not None:
        helper_runs = {run["run_id"]: run for run in first_elbow_geometry["runs"]}
        first_pipe_actual_start = [
            float(value) for value in helper_runs[f"{bundle_id}_pipe1_from_first_elbow"]["tangent_point_mm"]
        ]

    first_pipe_actual_end = second_elbow_inlet_requested
    outlet_actual_start = second_elbow_outlet_requested
    if second_elbow_geometry is not None:
        helper_runs = {run["run_id"]: run for run in second_elbow_geometry["runs"]}
        first_pipe_actual_end = [
            float(value) for value in helper_runs[f"{bundle_id}_pipe1_to_second_elbow"]["tangent_point_mm"]
        ]
        outlet_actual_start = [float(value) for value in helper_runs[f"{bundle_id}_outlet_helper"]["tangent_point_mm"]]

    if first_pipe_actual_start[1] - first_pipe_actual_end[1] > 1e-6:
        draw_pipe_segment(service, start=first_pipe_actual_end, end=first_pipe_actual_start, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_pipe1_y",
            category="section_preview",
            dn=branch_dn,
            start=first_pipe_actual_end,
            end=first_pipe_actual_start,
            basis=basis,
            path_id="gg_plan_external_chain",
            source_run_id="gg_plan_bottom_outlet_dn100",
            from_node_id=f"{bundle_id}_second_turn_elbow",
            to_node_id=f"{bundle_id}_bottom_turn_elbow",
        )

    if outlet_actual_start[0] - outlet_end[0] > 1e-6:
        draw_pipe_segment(service, start=outlet_end, end=outlet_actual_start, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_outlet_x",
            category="section_preview",
            dn=branch_dn,
            start=outlet_end,
            end=outlet_actual_start,
            basis=basis,
            path_id="gg_plan_external_chain",
            source_run_id="gg_plan_bottom_outlet_dn100",
            from_node_id=None,
            to_node_id=f"{bundle_id}_bottom_turn_elbow",
        )

    if bool(route.get("draw_fittings", True)):
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_bottom_turn_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=[anchor_x, left_vertical_y, bottom_elevation],
            handle=first_elbow_geometry["elbow_handle"] if first_elbow_geometry is not None else None,
            basis=basis,
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_second_turn_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=[anchor_x, second_elbow_y, bottom_elevation],
            handle=second_elbow_geometry["elbow_handle"] if second_elbow_geometry is not None else None,
            basis=basis,
        )


def draw_aa_dn200_plan_external_connection_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the new DN200 external chain near the second copied A-A route."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["branch_dn"])
    anchor_x = float(route["anchor_x"])
    header_connection_y = float(route["base_y"])
    header_connection_z = float(route["base_z"])
    rise_turn_y = float(route["turn_up_y"])
    top_elevation = float(route["top_elevation_mm"])
    target_y = float(route["target_y"])
    drop_end_elevation = float(route["drop_end_elevation_mm"])
    tee_proxy_run_dn = int(route.get("tee_proxy_run_dn", 450))
    tee_item_no = route.get("tee_item_no")
    elbow_item_no = route.get("elbow_item_no")
    basis = str(route.get("basis", ""))
    branch_layer = f"PIPE_DN{branch_dn}"
    controller = service.controller

    elbow_trim = resolve_lr_elbow_connection_trim_mm(None, branch_dn)
    rotation_candidates = build_common_elbow_rotation_candidates()
    resolved_tee = resolve_tee_center_to_end_gb12459(run_dn=tee_proxy_run_dn, branch_dn=branch_dn)
    tee_run_center_to_end = float(resolved_tee[0]) if resolved_tee else 0.0
    tee_branch_center_to_end = float(resolved_tee[1]) if resolved_tee else 0.0
    tee_branch_display_offset, _ = resolve_tee_display_offsets_mm(
        run_dn=tee_proxy_run_dn,
        branch_center_to_end_mm=tee_branch_center_to_end,
        run_center_to_end_mm=tee_run_center_to_end,
    )

    tee_branch_port = [anchor_x, header_connection_y - tee_branch_display_offset, header_connection_z]
    lower_turn_center = [anchor_x, rise_turn_y, header_connection_z]
    lower_horizontal_requested_end = [anchor_x, rise_turn_y + elbow_trim, header_connection_z]
    rise_vertical_requested_start = [anchor_x, rise_turn_y, header_connection_z + elbow_trim]
    rise_vertical_requested_end = [anchor_x, rise_turn_y, top_elevation - elbow_trim]
    upper_turn_center = [anchor_x, rise_turn_y, top_elevation]
    top_horizontal_requested_start = [anchor_x, rise_turn_y - elbow_trim, top_elevation]
    top_horizontal_requested_end = [anchor_x, target_y + elbow_trim, top_elevation]
    down_turn_center = [anchor_x, target_y, top_elevation]
    drop_vertical_requested_start = [anchor_x, target_y, top_elevation - elbow_trim]
    drop_vertical_end = [anchor_x, target_y, drop_end_elevation]

    lower_horizontal_length = tee_branch_port[1] - lower_horizontal_requested_end[1]
    rise_vertical_length = rise_vertical_requested_end[2] - rise_vertical_requested_start[2]
    top_horizontal_length = top_horizontal_requested_start[1] - top_horizontal_requested_end[1]
    drop_vertical_length = drop_vertical_requested_start[2] - drop_vertical_end[2]
    lower_turn_validator = lambda ports: ports_cover_exact_targets(
        ports,
        [
            {
                "axis": "-y",
                "connection_end": "right_end",
                "expected_point_mm": tuple(lower_horizontal_requested_end),
            },
            {
                "axis": "-z",
                "connection_end": "top_end",
                "expected_point_mm": tuple(rise_vertical_requested_start),
            },
        ],
    )
    upper_turn_validator = lambda ports: ports_cover_exact_targets(
        ports,
        [
            {
                "axis": "+z",
                "connection_end": "bottom_end",
                "expected_point_mm": tuple(rise_vertical_requested_end),
            },
            {
                "axis": "+y",
                "connection_end": "left_end",
                "expected_point_mm": tuple(top_horizontal_requested_start),
            },
        ],
    )
    down_turn_validator = lambda ports: ports_cover_exact_targets(
        ports,
        [
            {
                "axis": "-y",
                "connection_end": "right_end",
                "expected_point_mm": tuple(top_horizontal_requested_end),
            },
            {
                "axis": "+z",
                "connection_end": "bottom_end",
                "expected_point_mm": tuple(drop_vertical_requested_start),
            },
        ],
    )

    lower_turn_elbow = None
    upper_turn_elbow = None
    down_turn_elbow = None
    if bool(route.get("draw_fittings", True)):
        if lower_horizontal_length > 1e-6 and rise_vertical_length > 1e-6:
            lower_turn_elbow = select_matching_elbow_geometry(
                controller,
                dn=branch_dn,
                elbow_center=tuple(lower_turn_center),
                bend_radius=elbow_trim,
                run_specs=[
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_header_horizontal_helper",
                        tangent_point_mm=tuple(lower_horizontal_requested_end),
                        axis_away_from_fitting="-y",
                        length_mm=float(max(lower_horizontal_length, elbow_trim)),
                        connection_end="right_end",
                        flow_role="pipe_1",
                    ),
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_rise_vertical_lower_helper",
                        tangent_point_mm=tuple(rise_vertical_requested_start),
                        axis_away_from_fitting="-z",
                        length_mm=float(max(rise_vertical_length, elbow_trim)),
                        connection_end="top_end",
                        flow_role="pipe_2",
                    ),
                ],
                rotation_candidates=rotation_candidates,
                layer=branch_layer,
                validator=lower_turn_validator,
            )
        if rise_vertical_length > 1e-6 and top_horizontal_length > 1e-6:
            upper_turn_elbow = select_matching_elbow_geometry(
                controller,
                dn=branch_dn,
                elbow_center=tuple(upper_turn_center),
                bend_radius=elbow_trim,
                run_specs=[
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_rise_vertical_upper_helper",
                        tangent_point_mm=tuple(rise_vertical_requested_end),
                        axis_away_from_fitting="+z",
                        length_mm=float(max(rise_vertical_length, elbow_trim)),
                        connection_end="bottom_end",
                        flow_role="pipe_1",
                    ),
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_top_horizontal_from_upper_helper",
                        tangent_point_mm=tuple(top_horizontal_requested_start),
                        axis_away_from_fitting="+y",
                        length_mm=float(max(top_horizontal_length, elbow_trim)),
                        connection_end="left_end",
                        flow_role="pipe_2",
                    ),
                ],
                rotation_candidates=rotation_candidates,
                layer=branch_layer,
                validator=upper_turn_validator,
            )
        if top_horizontal_length > 1e-6 and drop_vertical_length > 1e-6:
            down_turn_elbow = select_matching_elbow_geometry(
                controller,
                dn=branch_dn,
                elbow_center=tuple(down_turn_center),
                bend_radius=elbow_trim,
                run_specs=[
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_top_horizontal_to_down_helper",
                        tangent_point_mm=tuple(top_horizontal_requested_end),
                        axis_away_from_fitting="-y",
                        length_mm=float(max(top_horizontal_length, elbow_trim)),
                        connection_end="right_end",
                        flow_role="pipe_1",
                    ),
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_drop_vertical_helper",
                        tangent_point_mm=tuple(drop_vertical_requested_start),
                        axis_away_from_fitting="+z",
                        length_mm=float(max(drop_vertical_length, elbow_trim)),
                        connection_end="bottom_end",
                        flow_role="pipe_2",
                    ),
                ],
                rotation_candidates=rotation_candidates,
                layer=branch_layer,
                validator=down_turn_validator,
            )

    lower_horizontal_actual_end = lower_horizontal_requested_end
    rise_vertical_actual_start = rise_vertical_requested_start
    rise_vertical_actual_end = rise_vertical_requested_end
    top_horizontal_actual_start = top_horizontal_requested_start
    top_horizontal_actual_end = top_horizontal_requested_end
    drop_vertical_actual_start = drop_vertical_requested_start

    if lower_turn_elbow is not None:
        helper_runs = {run["run_id"]: run for run in lower_turn_elbow["runs"]}
        lower_horizontal_actual_end = [
            float(value) for value in helper_runs[f"{bundle_id}_header_horizontal_helper"]["tangent_point_mm"]
        ]
        rise_vertical_actual_start = [
            float(value) for value in helper_runs[f"{bundle_id}_rise_vertical_lower_helper"]["tangent_point_mm"]
        ]
    if upper_turn_elbow is not None:
        helper_runs = {run["run_id"]: run for run in upper_turn_elbow["runs"]}
        rise_vertical_actual_end = [
            float(value) for value in helper_runs[f"{bundle_id}_rise_vertical_upper_helper"]["tangent_point_mm"]
        ]
        top_horizontal_actual_start = [
            float(value) for value in helper_runs[f"{bundle_id}_top_horizontal_from_upper_helper"]["tangent_point_mm"]
        ]
    if down_turn_elbow is not None:
        helper_runs = {run["run_id"]: run for run in down_turn_elbow["runs"]}
        top_horizontal_actual_end = [
            float(value) for value in helper_runs[f"{bundle_id}_top_horizontal_to_down_helper"]["tangent_point_mm"]
        ]
        drop_vertical_actual_start = [
            float(value) for value in helper_runs[f"{bundle_id}_drop_vertical_helper"]["tangent_point_mm"]
        ]

    if tee_branch_port[1] - lower_horizontal_actual_end[1] > 1e-6:
        draw_pipe_segment(service, start=tee_branch_port, end=lower_horizontal_actual_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_header_horizontal",
            category="section_preview",
            dn=branch_dn,
            start=tee_branch_port,
            end=lower_horizontal_actual_end,
            basis=basis,
            path_id="aa_dn200_plan_external_chain",
            source_run_id="aa_dn200_header_horizontal",
            from_node_id=f"{bundle_id}_header_branch_tee",
            to_node_id=f"{bundle_id}_lower_turn_elbow",
        )
    if rise_vertical_actual_end[2] - rise_vertical_actual_start[2] > 1e-6:
        draw_pipe_segment(service, start=rise_vertical_actual_start, end=rise_vertical_actual_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_rise_vertical",
            category="section_preview",
            dn=branch_dn,
            start=rise_vertical_actual_start,
            end=rise_vertical_actual_end,
            basis=basis,
            path_id="aa_dn200_plan_external_chain",
            source_run_id="aa_dn200_rise_vertical",
            from_node_id=f"{bundle_id}_lower_turn_elbow",
            to_node_id=f"{bundle_id}_upper_turn_elbow",
        )
    if top_horizontal_actual_start[1] - top_horizontal_actual_end[1] > 1e-6:
        draw_pipe_segment(service, start=top_horizontal_actual_start, end=top_horizontal_actual_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_top_horizontal",
            category="section_preview",
            dn=branch_dn,
            start=top_horizontal_actual_start,
            end=top_horizontal_actual_end,
            basis=basis,
            path_id="aa_dn200_plan_external_chain",
            source_run_id="aa_dn200_top_horizontal",
            from_node_id=f"{bundle_id}_upper_turn_elbow",
            to_node_id=f"{bundle_id}_down_turn_elbow",
        )
    if drop_vertical_actual_start[2] - drop_vertical_end[2] > 1e-6:
        draw_pipe_segment(service, start=drop_vertical_actual_start, end=drop_vertical_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_drop_vertical",
            category="section_preview",
            dn=branch_dn,
            start=drop_vertical_actual_start,
            end=drop_vertical_end,
            basis=basis,
            path_id="aa_dn200_plan_external_chain",
            source_run_id="aa_dn200_drop_vertical",
            from_node_id=f"{bundle_id}_down_turn_elbow",
            to_node_id=None,
        )

    if bool(route.get("draw_fittings", True)):
        lower_turn_handle = lower_turn_elbow["elbow_handle"] if lower_turn_elbow is not None else None
        if lower_turn_handle is None and lower_horizontal_length > 1e-6 and rise_vertical_length > 1e-6:
            lower_turn_handle = draw_cycle_water_lr_elbow_from_rotation_sequence(
                service,
                description=f"{bundle_id} lower turn elbow fallback",
                corner_point=lower_turn_center,
                dn=branch_dn,
                layer=branch_layer,
                elbow_rotations=select_matching_elbow_rotation_sequence(
                    elbow_center=tuple(lower_turn_center),
                    bend_radius=elbow_trim,
                    rotation_candidates=rotation_candidates,
                    validator=lower_turn_validator,
                ),
            )
        upper_turn_handle = upper_turn_elbow["elbow_handle"] if upper_turn_elbow is not None else None
        if upper_turn_handle is None and rise_vertical_length > 1e-6 and top_horizontal_length > 1e-6:
            upper_turn_handle = draw_cycle_water_lr_elbow_from_rotation_sequence(
                service,
                description=f"{bundle_id} upper turn elbow fallback",
                corner_point=upper_turn_center,
                dn=branch_dn,
                layer=branch_layer,
                elbow_rotations=select_matching_elbow_rotation_sequence(
                    elbow_center=tuple(upper_turn_center),
                    bend_radius=elbow_trim,
                    rotation_candidates=rotation_candidates,
                    validator=upper_turn_validator,
                ),
            )
        down_turn_handle = down_turn_elbow["elbow_handle"] if down_turn_elbow is not None else None
        if down_turn_handle is None and top_horizontal_length > 1e-6 and drop_vertical_length > 1e-6:
            down_turn_handle = draw_cycle_water_lr_elbow_from_rotation_sequence(
                service,
                description=f"{bundle_id} down turn elbow fallback",
                corner_point=down_turn_center,
                dn=branch_dn,
                layer=branch_layer,
                elbow_rotations=select_matching_elbow_rotation_sequence(
                    elbow_center=tuple(down_turn_center),
                    bend_radius=elbow_trim,
                    rotation_candidates=rotation_candidates,
                    validator=down_turn_validator,
                ),
            )
        missing_elbows: list[str] = []
        if lower_horizontal_length > 1e-6 and rise_vertical_length > 1e-6 and lower_turn_handle is None:
            missing_elbows.append("lower_turn_elbow")
        if rise_vertical_length > 1e-6 and top_horizontal_length > 1e-6 and upper_turn_handle is None:
            missing_elbows.append("upper_turn_elbow")
        if top_horizontal_length > 1e-6 and drop_vertical_length > 1e-6 and down_turn_handle is None:
            missing_elbows.append("down_turn_elbow")
        if missing_elbows:
            raise RuntimeError(f"{bundle_id} 真实弯头创建失败: {', '.join(missing_elbows)}")
        tee_entity = call_controller_with_retry(
            f"{bundle_id} header tee",
            lambda: controller.draw_tee(
                center=(anchor_x, header_connection_y, header_connection_z),
                dn=tee_proxy_run_dn,
                branch_dn=branch_dn,
                run_axis="x",
                branch_axis="-y",
                run_center_to_end=tee_run_center_to_end,
                branch_center_to_end=tee_branch_center_to_end,
                layer=f"PIPE_DN{tee_proxy_run_dn}",
                color=WHITE,
            ),
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_header_branch_tee",
            category="section_preview_fitting",
            kind="reducing_tee",
            item_no=tee_item_no,
            center=[anchor_x, header_connection_y, header_connection_z],
            handle=extract_entity_handle(tee_entity),
            basis=basis,
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_lower_turn_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=lower_turn_center,
            handle=lower_turn_handle,
            basis=basis,
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_upper_turn_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=upper_turn_center,
            handle=upper_turn_handle,
            basis=basis,
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_down_turn_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=down_turn_center,
            handle=down_turn_handle,
            basis=basis,
        )


def draw_bb_plan_external_connection_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the plan-only DN100 outlet extending from the frozen B-B bottom riser."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    anchor_x = float(route["anchor_x"])
    section_plane_y = float(route["section_plane_y"])
    bottom_elevation = float(route["bottom_elevation_mm"])
    base_header_y = float(route["base_header_y"])
    target_offset = float(route.get("plan_external_target_offset_from_base_header_mm", 4000.0))
    elbow_item_no = route.get("elbow_item_no")
    basis = str(route.get("basis", ""))
    branch_layer = f"PIPE_DN{branch_dn}"
    controller = service.controller
    elbow_trim = resolve_lr_elbow_connection_trim_mm(None, branch_dn)
    target_y = base_header_y - target_offset

    if section_plane_y - target_y <= 1e-6:
        return

    outlet_requested_start = [anchor_x, section_plane_y - elbow_trim, bottom_elevation]
    outlet_end = [anchor_x, target_y, bottom_elevation]
    section_vertical_requested = [anchor_x, section_plane_y, bottom_elevation + elbow_trim]

    outlet_elbow_geometry = None
    if bool(route.get("draw_fittings", True)):
        outlet_elbow_geometry = select_matching_elbow_geometry(
            controller,
            dn=branch_dn,
            elbow_center=(anchor_x, section_plane_y, bottom_elevation),
            bend_radius=elbow_trim,
            run_specs=[
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_section_vertical_helper",
                    tangent_point_mm=tuple(section_vertical_requested),
                    axis_away_from_fitting="-z",
                    length_mm=float(elbow_trim),
                    connection_end="top_end",
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_outlet_helper",
                    tangent_point_mm=tuple(outlet_requested_start),
                    axis_away_from_fitting="+y",
                    length_mm=float(section_plane_y - target_y),
                    connection_end="left_end",
                    flow_role="pipe_2",
                ),
            ],
            rotation_candidates=build_common_elbow_rotation_candidates(),
            layer=branch_layer,
            validator=lambda ports: ports_cover_targets(
                ports,
                [
                    {
                        "axis": "-z",
                        "connection_end": "top_end",
                        "coordinate_index": 2,
                        "relation": "gt",
                        "reference_value": bottom_elevation,
                    },
                    {
                        "axis": "+y",
                        "connection_end": "left_end",
                        "coordinate_index": 1,
                        "relation": "lt",
                        "reference_value": section_plane_y,
                    },
                ],
            ),
        )

    outlet_actual_start = outlet_requested_start
    if outlet_elbow_geometry is not None:
        helper_runs = {run["run_id"]: run for run in outlet_elbow_geometry["runs"]}
        outlet_actual_start = [float(value) for value in helper_runs[f"{bundle_id}_outlet_helper"]["tangent_point_mm"]]

    if outlet_actual_start[1] - outlet_end[1] > 1e-6:
        draw_pipe_segment(service, start=outlet_end, end=outlet_actual_start, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_outlet_y",
            category="section_preview",
            dn=branch_dn,
            start=outlet_end,
            end=outlet_actual_start,
            basis=basis,
            path_id="bb_plan_external_chain",
            source_run_id="bb_plan_bottom_outlet_dn100",
            from_node_id=None,
            to_node_id=f"{bundle_id}_bottom_turn_elbow",
        )

    if bool(route.get("draw_fittings", True)):
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_bottom_turn_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=[anchor_x, section_plane_y, bottom_elevation],
            handle=outlet_elbow_geometry["elbow_handle"] if outlet_elbow_geometry is not None else None,
            basis=basis,
        )


def draw_dd_section_preview_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the D-D local DN200 turn using one vertical leg and two elbows."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    anchor_x = float(route["anchor_x"])
    base_y = float(route["base_y"])
    base_z = float(route["base_z"])
    upper_elbow_y = float(route["upper_elbow_y"])
    lower_elbow_y = float(route["lower_elbow_y"])
    bottom_elevation = float(route["bottom_elevation_mm"])
    top_elevation = float(route["top_elevation_mm"])
    vertical_direction = str(route.get("vertical_direction", "up")).strip().lower()
    horizontal_elevation = float(
        route.get(
            "horizontal_elevation_mm",
            bottom_elevation if vertical_direction == "down" else top_elevation,
        )
    )
    riser_start_elevation = float(
        route.get(
            "riser_start_elevation_mm",
            top_elevation if vertical_direction == "down" else bottom_elevation,
        )
    )
    horizontal_offset_length = float(route["horizontal_offset_length_mm"])
    basis = str(route.get("basis", ""))
    controller = service.controller

    elbow_connection_trim = resolve_lr_elbow_connection_trim_mm(
        route.get("upper_elbow_connection_trim_mm"),
        branch_dn,
    )
    if vertical_direction != "down":
        raise ValueError("D-D 当前整体集成仅支持与已验证局部一致的立管下落语义。")

    upper_vertical_requested = [anchor_x, upper_elbow_y, horizontal_elevation + elbow_connection_trim]
    upper_horizontal_requested = [anchor_x, upper_elbow_y + elbow_connection_trim, horizontal_elevation]
    lower_horizontal_requested = [anchor_x, lower_elbow_y - elbow_connection_trim, horizontal_elevation]
    lower_vertical_validation_requested = [anchor_x, lower_elbow_y, horizontal_elevation - elbow_connection_trim]
    upper_vertical_length = riser_start_elevation - upper_vertical_requested[2]
    horizontal_length = lower_horizontal_requested[1] - upper_horizontal_requested[1]
    lower_vertical_validation_length = horizontal_elevation - lower_vertical_validation_requested[2]

    upper_elbow_geometry = None
    lower_elbow_geometry = None
    if upper_vertical_length > 1e-6 and horizontal_length > 1e-6:
        upper_elbow_geometry = build_orthogonal_elbow_connection(
            controller,
            dn=branch_dn,
            elbow_center=(anchor_x, upper_elbow_y, horizontal_elevation),
            elbow_type="LR",
            run_specs=[
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_upper_vertical",
                    tangent_point_mm=tuple(upper_vertical_requested),
                    axis_away_from_fitting="-z",
                    length_mm=float(upper_vertical_length),
                    connection_end="top_end",
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_upper_horizontal",
                    tangent_point_mm=tuple(upper_horizontal_requested),
                    axis_away_from_fitting="-y",
                    length_mm=float(horizontal_length),
                    connection_end="right_end",
                    flow_role="pipe_2",
                ),
            ],
            elbow_rotations=[
                RotationInstruction(
                    angle_deg=float(route.get("upper_elbow_rotation_deg", -90.0)),
                    axis_direction=resolve_rotation_axis(route.get("upper_elbow_rotation_axis", "x")),
                    axis_name=str(route.get("upper_elbow_rotation_axis", "x")),
                ),
                RotationInstruction(
                    angle_deg=float(route.get("upper_elbow_secondary_rotation_deg", -90.0)),
                    axis_direction=resolve_rotation_axis(route.get("upper_elbow_secondary_rotation_axis", "z")),
                    axis_name=str(route.get("upper_elbow_secondary_rotation_axis", "z")),
                ),
            ],
            layer=f"PIPE_DN{branch_dn}",
            color=WHITE,
            draw_runs=False,
        )

    if lower_vertical_validation_length > 1e-6 and horizontal_length > 1e-6:
        lower_elbow_geometry = build_orthogonal_elbow_connection(
            controller,
            dn=branch_dn,
            elbow_center=(anchor_x, lower_elbow_y, horizontal_elevation),
            elbow_type="LR",
            run_specs=[
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_lower_horizontal",
                    tangent_point_mm=tuple(lower_horizontal_requested),
                    axis_away_from_fitting="+y",
                    length_mm=float(horizontal_length),
                    connection_end="left_end",
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_lower_vertical_validation",
                    tangent_point_mm=tuple(lower_vertical_validation_requested),
                    axis_away_from_fitting="+z",
                    length_mm=float(lower_vertical_validation_length),
                    connection_end="bottom_end",
                    flow_role="pipe_2",
                ),
            ],
            elbow_rotations=[
                RotationInstruction(
                    angle_deg=float(route.get("lower_elbow_rotation_deg", -90.0)),
                    axis_direction=resolve_rotation_axis(route.get("lower_elbow_rotation_axis", "y")),
                    axis_name=str(route.get("lower_elbow_rotation_axis", "y")),
                ),
                RotationInstruction(
                    angle_deg=float(route.get("lower_elbow_secondary_rotation_deg", 0.0)),
                    axis_direction=resolve_rotation_axis(route.get("lower_elbow_secondary_rotation_axis", "x")),
                    axis_name=str(route.get("lower_elbow_secondary_rotation_axis", "x")),
                ),
            ],
            layer=f"PIPE_DN{branch_dn}",
            color=WHITE,
            draw_runs=False,
        )

    vertical_start = [anchor_x, upper_elbow_y, riser_start_elevation]
    if upper_elbow_geometry is not None:
        upper_runs_by_id = {run["run_id"]: run for run in upper_elbow_geometry["runs"]}
        vertical_tangent = [float(value) for value in upper_runs_by_id[f"{bundle_id}_upper_vertical"]["tangent_point_mm"]]
        upper_horizontal_tangent = [
            float(value) for value in upper_runs_by_id[f"{bundle_id}_upper_horizontal"]["tangent_point_mm"]
        ]
    else:
        vertical_tangent = upper_vertical_requested
        upper_horizontal_tangent = upper_horizontal_requested

    if lower_elbow_geometry is not None:
        lower_runs_by_id = {run["run_id"]: run for run in lower_elbow_geometry["runs"]}
        lower_horizontal_tangent = [
            float(value) for value in lower_runs_by_id[f"{bundle_id}_lower_horizontal"]["tangent_point_mm"]
        ]
    else:
        lower_horizontal_tangent = lower_horizontal_requested

    if abs(vertical_tangent[2] - vertical_start[2]) > 1e-6:
        draw_pipe_segment(service, start=vertical_start, end=vertical_tangent, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_vertical_riser",
            category="section_preview",
            dn=branch_dn,
            start=vertical_start,
            end=vertical_tangent,
            basis=basis,
            path_id="dd_local_dn200_chain",
            source_run_id="dd_local_riser_dn200",
            from_node_id=None,
            to_node_id="dd_upper_elbow_21",
        )

    if lower_horizontal_tangent[1] - upper_horizontal_tangent[1] > 1e-6:
        draw_pipe_segment(service, start=upper_horizontal_tangent, end=lower_horizontal_tangent, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_horizontal_offset",
            category="section_preview",
            dn=branch_dn,
            start=upper_horizontal_tangent,
            end=lower_horizontal_tangent,
            basis=basis,
            path_id="dd_local_dn200_chain",
            source_run_id="dd_horizontal_turn_dn200",
            from_node_id="dd_upper_elbow_21",
            to_node_id="dd_lower_elbow_21",
        )

    if not bool(route.get("draw_fittings", True)):
        return

    branch_layer = f"PIPE_DN{branch_dn}"
    if bool(route.get("draw_header_tee", False)):
        header_tee_run_dn = int(route.get("header_tee_run_dn", route.get("base_header_dn", branch_dn)))
        header_tee_branch_axis = str(route.get("header_tee_branch_axis", "-y")).strip().lower()
        header_tee_entity = call_controller_with_retry(
            f"{bundle_id} header tee",
            lambda: service.controller.draw_tee(
                center=(anchor_x, base_y, base_z),
                dn=header_tee_run_dn,
                branch_dn=branch_dn,
                run_axis="x",
                branch_axis=header_tee_branch_axis,
                layer=f"PIPE_DN{header_tee_run_dn}",
                color=WHITE,
            ),
        )
        if header_tee_entity is None:
            raise RuntimeError(f"{bundle_id} 的顶部 DN450x200 三通绘制失败。")
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_header_tee",
            category="section_preview_fitting",
            kind="reducing_tee",
            item_no=route.get("header_tee_item_no"),
            center=[anchor_x, base_y, base_z],
            handle=extract_entity_handle(header_tee_entity),
            basis=basis,
        )

    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_upper_elbow",
        category="section_preview_fitting",
        kind="elbow",
        item_no=route.get("upper_elbow_item_no"),
        center=[anchor_x, upper_elbow_y, horizontal_elevation],
        handle=None,
        basis=basis,
        helper_generated=upper_elbow_geometry is not None,
    )

    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_lower_elbow",
        category="section_preview_fitting",
        kind="elbow",
        item_no=route.get("lower_elbow_item_no"),
        center=[anchor_x, lower_elbow_y, horizontal_elevation],
        handle=None,
        basis=basis,
        helper_generated=lower_elbow_geometry is not None,
    )
    orifice_item_no = route.get("orifice_item_no")
    if orifice_item_no is not None:
        orifice_center = [anchor_x, upper_elbow_y, riser_start_elevation]
        orifice_handle = draw_dd_orifice_collar(service, center=orifice_center, dn=branch_dn)
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_orifice_28",
            category="section_preview_fitting",
            kind="orifice_collar",
            item_no=orifice_item_no,
            center=orifice_center,
            handle=orifice_handle,
            basis=basis,
        )


def draw_cc_section_preview_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the C-C left-wall DN200 chain using a local valve proxy and sleeve anchor."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    anchor_x = float(route["anchor_x"])
    wall_center_y = float(route["wall_center_y"])
    center_elevation = float(route["center_elevation_mm"])
    outside_projection_length = float(route.get("outside_projection_length_mm", 0.0))
    outside_start_x = route.get("outside_start_x_mm")
    inside_projection_length = float(route.get("inside_projection_length_mm", 0.0))
    valve_center_offset = float(route.get("valve_center_offset_from_wall_mm", 0.0))
    valve_offset_from_hh_connection = float(route.get("valve_offset_from_hh_connection_mm", 0.0))
    valve_structure_length = float(route.get("valve_structure_length_mm", 0.0))
    valve_half_length = valve_structure_length / 2.0 if valve_structure_length > 0.0 else 0.0
    valve_item_no = route.get("valve_item_no")
    sleeve_item_no = route.get("sleeve_item_no")
    valve_pn = float(route.get("valve_pn", 10.0))
    basis = str(route.get("basis", ""))
    branch_layer = f"PIPE_DN{branch_dn}"
    hh_connection_x = route.get("hh_connection_x")
    right_projection_length = float(route.get("right_projection_length_mm", 0.0))

    outside_start = [
        float(outside_start_x) if outside_start_x is not None else anchor_x - outside_projection_length,
        wall_center_y,
        center_elevation,
    ]
    wall_center = [anchor_x, wall_center_y, center_elevation]
    if hh_connection_x is not None:
        valve_center_x = float(hh_connection_x) - valve_offset_from_hh_connection
        inside_end_x = float(hh_connection_x) + right_projection_length
    else:
        valve_center_x = anchor_x - valve_center_offset
        inside_end_x = anchor_x + inside_projection_length
    valve_center = [valve_center_x, wall_center_y, center_elevation]
    upstream_stop = [valve_center[0] - valve_half_length, wall_center_y, center_elevation]
    downstream_start = [valve_center[0] + valve_half_length, wall_center_y, center_elevation]
    inside_end = [inside_end_x, wall_center_y, center_elevation]

    if upstream_stop[0] - outside_start[0] > 1e-6:
        draw_pipe_segment(service, start=outside_start, end=upstream_stop, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_outer_stub",
            category="section_preview",
            dn=branch_dn,
            start=outside_start,
            end=upstream_stop,
            basis=basis,
            path_id="cc_left_wall_chain",
            source_run_id="cc_left_wall_dn200",
            from_node_id=None,
            to_node_id="cc_gate_valve_9",
        )

    if wall_center[0] - downstream_start[0] > 1e-6:
        draw_pipe_segment(service, start=downstream_start, end=wall_center, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_valve_to_sleeve",
            category="section_preview",
            dn=branch_dn,
            start=downstream_start,
            end=wall_center,
            basis=basis,
            path_id="cc_left_wall_chain",
            source_run_id="cc_left_wall_dn200",
            from_node_id="cc_gate_valve_9",
            to_node_id="cc_wall_sleeve_31",
        )

    if inside_end[0] - wall_center[0] > 1e-6:
        draw_pipe_segment(service, start=wall_center, end=inside_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_inside_stub",
            category="section_preview",
            dn=branch_dn,
            start=wall_center,
            end=inside_end,
            basis=basis,
            path_id="cc_left_wall_chain",
            source_run_id="cc_left_wall_dn200",
            from_node_id="cc_wall_sleeve_31",
            to_node_id=None,
        )

    if bool(route.get("draw_valve_proxy", True)):
        valve_entity = call_controller_with_retry(
            f"{bundle_id} C-C valve proxy",
            lambda: service.controller.draw_globe_valve(
                center=tuple(valve_center),
                dn=branch_dn,
                pn=valve_pn,
                axis="x",
                layer=branch_layer,
                color=WHITE,
            ),
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_valve_proxy",
            category="section_preview_fitting",
            kind="gate_valve_proxy",
            item_no=valve_item_no,
            center=valve_center,
            handle=extract_entity_handle(valve_entity),
            basis=f"{basis} 当前 CAD 库无专用闸阀原语，先用法兰截止阀代理保留 9 号件的口径与串联位置。",
        )

    sleeve_entity = call_controller_with_retry(
        f"{bundle_id} C-C sleeve",
        lambda: service.controller.draw_rigid_waterproof_sleeve(
            center=tuple(wall_center),
            dn=branch_dn,
            axis="x",
            layer=branch_layer,
            color=WHITE,
        ),
    )
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_sleeve",
        category="section_preview_fitting",
        kind="rigid_waterproof_sleeve",
        item_no=sleeve_item_no,
        center=wall_center,
        handle=extract_entity_handle(sleeve_entity),
        basis=basis,
    )


def draw_cc_plan_extension_preview_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the screenshot-driven C-C DN200 plan extension near the H-H column."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    line_y = float(route["line_y"])
    center_elevation = float(route["center_elevation_mm"])
    hh_connection_x = float(route["hh_connection_x"])
    valve_structure_length = float(route.get("valve_structure_length_mm", 0.0))
    valve_half_length = valve_structure_length / 2.0 if valve_structure_length > 0 else 0.0
    valve_offset = float(route.get("valve_offset_from_hh_connection_mm", 1000.0))
    left_projection_length = float(route.get("left_projection_length_mm", 0.0))
    right_projection_length = float(route.get("right_projection_length_mm", 0.0))
    valve_item_no = route.get("valve_item_no")
    valve_pn = float(route.get("valve_pn", 10.0))
    basis = str(route.get("basis", ""))
    branch_layer = f"PIPE_DN{branch_dn}"

    valve_center_x = hh_connection_x - valve_offset
    left_start = [valve_center_x - valve_half_length - left_projection_length, line_y, center_elevation]
    upstream_stop = [valve_center_x - valve_half_length, line_y, center_elevation]
    downstream_start = [valve_center_x + valve_half_length, line_y, center_elevation]
    right_end = [hh_connection_x + right_projection_length, line_y, center_elevation]
    valve_center = [valve_center_x, line_y, center_elevation]

    if upstream_stop[0] - left_start[0] > 1e-6:
        draw_pipe_segment(service, start=left_start, end=upstream_stop, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_left_span",
            category="section_preview",
            dn=branch_dn,
            start=left_start,
            end=upstream_stop,
            basis=basis,
            path_id="cc_plan_extension_chain",
            source_run_id="cc_plan_extension_dn200",
            from_node_id=None,
            to_node_id="cc_gate_valve_9",
        )
    if right_end[0] - downstream_start[0] > 1e-6:
        draw_pipe_segment(service, start=downstream_start, end=right_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_right_span",
            category="section_preview",
            dn=branch_dn,
            start=downstream_start,
            end=right_end,
            basis=basis,
            path_id="cc_plan_extension_chain",
            source_run_id="cc_plan_extension_dn200",
            from_node_id="cc_gate_valve_9",
            to_node_id="hh_plan_external_connection_dn125",
        )
    if bool(route.get("draw_valve_proxy", True)):
        valve_entity = call_controller_with_retry(
            f"{bundle_id} C-C plan valve proxy",
            lambda: service.controller.draw_globe_valve(
                center=tuple(valve_center),
                dn=branch_dn,
                pn=valve_pn,
                axis="x",
                layer=branch_layer,
                color=WHITE,
            ),
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_valve_proxy",
            category="section_preview_fitting",
            kind="gate_valve_proxy",
            item_no=valve_item_no,
            center=valve_center,
            handle=extract_entity_handle(valve_entity),
            basis=basis,
        )


def draw_ff_section_preview_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the F-F local DN150 return in the x-z section plane."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    anchor_x = float(route["anchor_x"])
    section_plane_y = float(route["left_vertical_y"])
    left_vertical_x = anchor_x
    bottom_elevation = float(route["bottom_elevation_mm"])
    top_elevation = float(route["top_elevation_mm"])
    return_drop_bottom_elevation = float(route["return_drop_bottom_elevation_mm"])
    left_stub_length = float(route.get("left_stub_length_mm", 0.0))
    tee_item_no = route.get("tee_item_no")
    elbow_item_no = route.get("elbow_item_no")
    basis = str(route.get("basis", ""))
    branch_layer = f"PIPE_DN{branch_dn}"
    controller = service.controller

    resolved_tee = resolve_tee_center_to_end_gb12459(run_dn=branch_dn, branch_dn=branch_dn)
    tee_run_center_to_end = float(resolved_tee[0]) if resolved_tee else 0.0
    tee_branch_center_to_end = float(resolved_tee[1]) if resolved_tee else 0.0
    tee_branch_display_offset, tee_run_display_half_length = resolve_tee_display_offsets_mm(
        run_dn=branch_dn,
        branch_center_to_end_mm=tee_branch_center_to_end,
        run_center_to_end_mm=tee_run_center_to_end,
    )
    elbow_trim = resolve_lr_elbow_connection_trim_mm(None, branch_dn)
    top_horizontal_length = float(route.get("top_horizontal_length_mm", 0.0))
    top_horizontal_center_x = float(route.get("top_horizontal_center_x_mm", left_vertical_x))
    if top_horizontal_length > 1e-6:
        left_vertical_x = top_horizontal_center_x - top_horizontal_length / 2.0 - elbow_trim
        right_vertical_x = top_horizontal_center_x + top_horizontal_length / 2.0 + elbow_trim
    else:
        right_vertical_x = left_vertical_x + float(route["return_span_mm"])

    left_vertical_start = [left_vertical_x, section_plane_y, bottom_elevation + tee_branch_display_offset]
    left_vertical_requested_top = [left_vertical_x, section_plane_y, top_elevation - elbow_trim]
    top_horizontal_requested_start = [left_vertical_x + elbow_trim, section_plane_y, top_elevation]
    top_horizontal_requested_end = [right_vertical_x - elbow_trim, section_plane_y, top_elevation]
    right_vertical_requested_start = [right_vertical_x, section_plane_y, top_elevation]
    right_vertical_end = [right_vertical_x, section_plane_y, return_drop_bottom_elevation]
    left_stub_end = [left_vertical_x, section_plane_y - tee_run_display_half_length, bottom_elevation]
    left_stub_start = [left_vertical_x, left_stub_end[1] - left_stub_length, bottom_elevation]

    left_elbow_geometry = None
    right_elbow_geometry = None
    if bool(route.get("draw_fittings", True)):
        left_vertical_length = left_vertical_requested_top[2] - left_vertical_start[2]
        top_return_length = top_horizontal_requested_end[0] - top_horizontal_requested_start[0]
        right_drop_helper_length = (top_elevation - elbow_trim) - right_vertical_end[2]
        if left_vertical_length > 1e-6 and top_return_length > 1e-6:
            left_elbow_geometry = build_orthogonal_elbow_connection(
                controller,
                dn=branch_dn,
                elbow_center=(left_vertical_x, section_plane_y, top_elevation),
                elbow_type="LR",
                run_specs=[
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_left_riser_helper",
                        tangent_point_mm=tuple(left_vertical_requested_top),
                        axis_away_from_fitting="+z",
                        length_mm=float(left_vertical_length),
                        connection_end="bottom_end",
                        flow_role="pipe_1",
                    ),
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_top_return_left_helper",
                        tangent_point_mm=tuple(top_horizontal_requested_start),
                        axis_away_from_fitting="-x",
                        length_mm=float(top_return_length),
                        connection_end="right_end",
                        flow_role="pipe_2",
                    ),
                ],
                elbow_rotations=[
                    RotationInstruction(angle_deg=90.0, axis_direction=(1.0, 0.0, 0.0), axis_name="x"),
                    RotationInstruction(angle_deg=180.0, axis_direction=(0.0, 0.0, 1.0), axis_name="z"),
                ],
                layer=branch_layer,
                color=WHITE,
                draw_runs=False,
            )
        if right_drop_helper_length > 1e-6 and top_return_length > 1e-6:
            right_elbow_geometry = build_orthogonal_elbow_connection(
                controller,
                dn=branch_dn,
                elbow_center=(right_vertical_x, section_plane_y, top_elevation),
                elbow_type="LR",
                run_specs=[
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_top_return_right_helper",
                        tangent_point_mm=tuple(top_horizontal_requested_end),
                        axis_away_from_fitting="+x",
                        length_mm=float(top_return_length),
                        connection_end="left_end",
                        flow_role="pipe_1",
                    ),
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_right_drop_helper",
                        tangent_point_mm=(right_vertical_x, section_plane_y, top_elevation - elbow_trim),
                        axis_away_from_fitting="+z",
                        length_mm=float(right_drop_helper_length),
                        connection_end="bottom_end",
                        flow_role="pipe_2",
                    ),
                ],
                elbow_rotations=[
                    RotationInstruction(angle_deg=-90.0, axis_direction=(0.0, 1.0, 0.0), axis_name="y"),
                    RotationInstruction(angle_deg=-90.0, axis_direction=(0.0, 0.0, 1.0), axis_name="z"),
                ],
                layer=branch_layer,
                color=WHITE,
                draw_runs=False,
            )

    left_vertical_top_tangent = left_vertical_requested_top
    top_return_left_tangent = top_horizontal_requested_start
    top_return_right_tangent = top_horizontal_requested_end
    right_drop_top_tangent = right_vertical_requested_start
    if left_elbow_geometry is not None:
        left_helper_runs = {run["run_id"]: run for run in left_elbow_geometry["runs"]}
        left_vertical_top_tangent = [
            float(value) for value in left_helper_runs[f"{bundle_id}_left_riser_helper"]["tangent_point_mm"]
        ]
        top_return_left_tangent = [
            float(value) for value in left_helper_runs[f"{bundle_id}_top_return_left_helper"]["tangent_point_mm"]
        ]
    if right_elbow_geometry is not None:
        right_helper_runs = {run["run_id"]: run for run in right_elbow_geometry["runs"]}
        top_return_right_tangent = [
            float(value) for value in right_helper_runs[f"{bundle_id}_top_return_right_helper"]["tangent_point_mm"]
        ]
        right_drop_top_tangent = [
            float(value) for value in right_helper_runs[f"{bundle_id}_right_drop_helper"]["tangent_point_mm"]
        ]

    if left_stub_end[1] - left_stub_start[1] > 1e-6:
        draw_pipe_segment(service, start=left_stub_start, end=left_stub_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_left_stub",
            category="section_preview",
            dn=branch_dn,
            start=left_stub_start,
            end=left_stub_end,
            basis=basis,
            path_id="ff_local_dn150_chain",
            source_run_id="ff_left_exposed_stub_dn150",
            from_node_id=None,
            to_node_id="ff_bottom_tee_18",
        )

    if left_vertical_top_tangent[2] - left_vertical_start[2] > 1e-6:
        draw_pipe_segment(service, start=left_vertical_start, end=left_vertical_top_tangent, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_left_riser",
            category="section_preview",
            dn=branch_dn,
            start=left_vertical_start,
            end=left_vertical_top_tangent,
            basis=basis,
            path_id="ff_local_dn150_chain",
            source_run_id="ff_left_riser_dn150",
            from_node_id="ff_bottom_tee_18",
            to_node_id="ff_top_left_elbow_22",
        )

    if top_return_right_tangent[0] - top_return_left_tangent[0] > 1e-6:
        draw_pipe_segment(service, start=top_return_left_tangent, end=top_return_right_tangent, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_top_return",
            category="section_preview",
            dn=branch_dn,
            start=top_return_left_tangent,
            end=top_return_right_tangent,
            basis=basis,
            path_id="ff_local_dn150_chain",
            source_run_id="ff_top_return_dn150",
            from_node_id="ff_top_left_elbow_22",
            to_node_id="ff_top_right_elbow_22",
        )

    if right_drop_top_tangent[2] - right_vertical_end[2] > 1e-6:
        draw_pipe_segment(service, start=right_drop_top_tangent, end=right_vertical_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_right_drop",
            category="section_preview",
            dn=branch_dn,
            start=right_drop_top_tangent,
            end=right_vertical_end,
            basis=basis,
            path_id="ff_local_dn150_chain",
            source_run_id="ff_return_drop_dn150",
            from_node_id="ff_top_right_elbow_22",
            to_node_id=None,
        )

    if not bool(route.get("draw_fittings", True)):
        return

    tee_entity = call_controller_with_retry(
        f"{bundle_id} bottom tee",
        lambda: controller.draw_tee(
            center=(left_vertical_x, section_plane_y, bottom_elevation),
            dn=branch_dn,
            branch_dn=branch_dn,
            run_axis="y",
            branch_axis="z",
            run_center_to_end=tee_run_center_to_end,
            branch_center_to_end=tee_branch_center_to_end,
            layer=branch_layer,
            color=WHITE,
        ),
    )
    if tee_entity is None:
        raise RuntimeError(f"{bundle_id} 的 18 号 DN150 三通绘制失败。")
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_tee_18",
        category="section_preview_fitting",
        kind="tee",
        item_no=tee_item_no,
        center=[left_vertical_x, section_plane_y, bottom_elevation],
        handle=extract_entity_handle(tee_entity),
        basis=basis,
    )
    left_elbow_handle = left_elbow_geometry["elbow_handle"] if left_elbow_geometry is not None else None
    right_elbow_handle = right_elbow_geometry["elbow_handle"] if right_elbow_geometry is not None else None
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_top_left_elbow",
        category="section_preview_fitting",
        kind="elbow",
        item_no=elbow_item_no,
        center=[left_vertical_x, section_plane_y, top_elevation],
        handle=left_elbow_handle,
        basis=basis,
    )
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_top_right_elbow",
        category="section_preview_fitting",
        kind="elbow",
        item_no=elbow_item_no,
        center=[right_vertical_x, section_plane_y, top_elevation],
        handle=right_elbow_handle,
        basis=basis,
    )


def select_matching_elbow_geometry(
    controller: Any,
    *,
    dn: int,
    elbow_center: tuple[float, float, float],
    bend_radius: float,
    run_specs: list[PipeConnectionRunSpec],
    rotation_candidates: list[list[RotationInstruction]],
    layer: str,
    validator: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    """Return the first elbow geometry whose final tangents match the expected quadrant."""

    for rotation_sequence in rotation_candidates:
        rotated_ports = _build_rotated_elbow_ports(
            elbow_center=elbow_center,
            bend_radius=bend_radius,
            elbow_rotations=rotation_sequence,
        )
        ports_by_id = {port.port_id: port for port in rotated_ports}
        if not validator(
            {
                port_id: {
                    "tangent_point_mm": port.tangent_point_mm,
                    "axis_away_from_fitting": port.axis_away_from_fitting,
                    "connection_end": port.connection_end,
                }
                for port_id, port in ports_by_id.items()
            }
        ):
            continue
        for _ in range(3):
            try:
                return build_orthogonal_elbow_connection(
                    controller,
                    dn=dn,
                    elbow_center=elbow_center,
                    elbow_type="LR",
                    run_specs=run_specs,
                    elbow_rotations=rotation_sequence,
                    layer=layer,
                    color=WHITE,
                    draw_runs=False,
                )
            except Exception:
                time.sleep(0.3)
        continue
    return None


def select_matching_elbow_rotation_sequence(
    *,
    elbow_center: tuple[float, float, float],
    bend_radius: float,
    rotation_candidates: list[list[RotationInstruction]],
    validator: Callable[[dict[str, Any]], bool],
) -> list[RotationInstruction] | None:
    """Return the first rotation sequence whose rotated elbow ports satisfy the validator."""

    for rotation_sequence in rotation_candidates:
        rotated_ports = _build_rotated_elbow_ports(
            elbow_center=elbow_center,
            bend_radius=bend_radius,
            elbow_rotations=rotation_sequence,
        )
        ports_by_id = {port.port_id: port for port in rotated_ports}
        if validator(
            {
                port_id: {
                    "tangent_point_mm": port.tangent_point_mm,
                    "axis_away_from_fitting": port.axis_away_from_fitting,
                    "connection_end": port.connection_end,
                }
                for port_id, port in ports_by_id.items()
            }
        ):
            return rotation_sequence
    return None


def build_common_elbow_rotation_candidates() -> list[list[RotationInstruction]]:
    """Enumerate common 90-degree elbow rotation pairs for helper-based port matching."""

    operations = (
        ("x", 90.0),
        ("x", -90.0),
        ("x", 180.0),
        ("y", 90.0),
        ("y", -90.0),
        ("y", 180.0),
        ("z", 90.0),
        ("z", -90.0),
        ("z", 180.0),
    )
    candidates: list[list[RotationInstruction]] = []
    seen: set[tuple[tuple[str, float], ...]] = set()
    for first_axis, first_angle in operations:
        for second in (None, *operations):
            signature = ((first_axis, first_angle),)
            candidate = [
                RotationInstruction(
                    angle_deg=first_angle,
                    axis_direction=resolve_rotation_axis(first_axis),
                    axis_name=first_axis,
                )
            ]
            if second is not None:
                second_axis, second_angle = second
                signature = ((first_axis, first_angle), (second_axis, second_angle))
                candidate.append(
                    RotationInstruction(
                        angle_deg=second_angle,
                        axis_direction=resolve_rotation_axis(second_axis),
                        axis_name=second_axis,
                    )
                )
            if signature in seen:
                continue
            seen.add(signature)
            candidates.append(candidate)
    return candidates


def port_matches_target(
    port: dict[str, Any],
    *,
    axis: str,
    connection_end: str,
    coordinate_index: int,
    relation: str,
    reference_value: float,
) -> bool:
    """Check whether one rotated elbow port lands on the expected side of the corner."""

    coordinate = float(port["tangent_point_mm"][coordinate_index])
    if relation == "lt":
        coordinate_ok = coordinate < reference_value - 1e-6
    elif relation == "gt":
        coordinate_ok = coordinate > reference_value + 1e-6
    else:
        raise ValueError(f"未识别的 relation: {relation}")
    return (
        coordinate_ok
        and str(port["axis_away_from_fitting"]) == axis
        and str(port["connection_end"]) == connection_end
    )


def ports_cover_targets(ports: dict[str, dict[str, Any]], targets: list[dict[str, Any]]) -> bool:
    """Match two rotated elbow ports against two expected target descriptors."""

    if len(ports) != 2 or len(targets) != 2:
        return False
    port_values = list(ports.values())
    return (
        port_matches_target(port_values[0], **targets[0]) and port_matches_target(port_values[1], **targets[1])
    ) or (
        port_matches_target(port_values[0], **targets[1]) and port_matches_target(port_values[1], **targets[0])
    )


def port_matches_exact_target(
    port: dict[str, Any],
    *,
    axis: str,
    connection_end: str,
    expected_point_mm: tuple[float, float, float],
    tolerance_mm: float = 1.0,
) -> bool:
    """Check one rotated elbow port against an exact tangent point."""

    tangent = tuple(float(value) for value in port["tangent_point_mm"])
    return (
        str(port["axis_away_from_fitting"]) == axis
        and str(port["connection_end"]) == connection_end
        and all(abs(tangent[index] - float(expected_point_mm[index])) <= tolerance_mm for index in range(3))
    )


def ports_cover_exact_targets(
    ports: dict[str, dict[str, Any]],
    targets: list[dict[str, Any]],
    *,
    tolerance_mm: float = 1.0,
) -> bool:
    """Match two rotated elbow ports against two exact tangent descriptors."""

    if len(ports) != 2 or len(targets) != 2:
        return False
    port_values = list(ports.values())
    return (
        port_matches_exact_target(port_values[0], tolerance_mm=tolerance_mm, **targets[0])
        and port_matches_exact_target(port_values[1], tolerance_mm=tolerance_mm, **targets[1])
    ) or (
        port_matches_exact_target(port_values[0], tolerance_mm=tolerance_mm, **targets[1])
        and port_matches_exact_target(port_values[1], tolerance_mm=tolerance_mm, **targets[0])
    )


def draw_ff_plan_external_connection_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw plan-derived external DN150 pipes that connect to the frozen F-F section."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    section_x = float(route["section_anchor_x_mm"])
    spine_x = float(route["spine_x_mm"])
    tee_center_y = float(route["tee_center_y_mm"])
    top_turn_y = float(route["top_turn_y_mm"])
    bottom_turn_y = float(route["bottom_turn_y_mm"])
    elevation = float(route["elevation_mm"])
    elbow_item_no = route.get("elbow_item_no")
    basis = str(route.get("basis", ""))
    branch_layer = f"PIPE_DN{branch_dn}"
    controller = service.controller

    resolved_tee = resolve_tee_center_to_end_gb12459(run_dn=branch_dn, branch_dn=branch_dn)
    tee_run_center_to_end = float(resolved_tee[0]) if resolved_tee else 0.0
    tee_branch_center_to_end = float(resolved_tee[1]) if resolved_tee else 0.0
    _, tee_run_display_half_length = resolve_tee_display_offsets_mm(
        run_dn=branch_dn,
        branch_center_to_end_mm=tee_branch_center_to_end,
        run_center_to_end_mm=tee_run_center_to_end,
    )
    elbow_trim = resolve_lr_elbow_connection_trim_mm(None, branch_dn)
    rotation_candidates = build_common_elbow_rotation_candidates()

    upper_vertical_start = [section_x, tee_center_y + tee_run_display_half_length, elevation]
    upper_vertical_requested_end = [section_x, top_turn_y - elbow_trim, elevation]
    lower_vertical_requested_start = [section_x, bottom_turn_y + elbow_trim, elevation]
    lower_vertical_end = [section_x, tee_center_y - tee_run_display_half_length, elevation]
    top_outlet_requested_start = [section_x - elbow_trim, top_turn_y, elevation]
    top_outlet_end = [spine_x, top_turn_y, elevation]
    bottom_outlet_requested_start = [section_x - elbow_trim, bottom_turn_y, elevation]
    bottom_outlet_end = [spine_x, bottom_turn_y, elevation]

    top_elbow_geometry = None
    bottom_elbow_geometry = None
    if bool(route.get("draw_fittings", True)):
        upper_vertical_length = upper_vertical_requested_end[1] - upper_vertical_start[1]
        lower_vertical_length = lower_vertical_end[1] - lower_vertical_requested_start[1]
        top_outlet_length = top_outlet_requested_start[0] - top_outlet_end[0]
        bottom_outlet_length = bottom_outlet_requested_start[0] - bottom_outlet_end[0]
        if upper_vertical_length > 1e-6 and top_outlet_length > 1e-6:
            top_elbow_geometry = select_matching_elbow_geometry(
                controller,
                dn=branch_dn,
                elbow_center=(section_x, top_turn_y, elevation),
                bend_radius=elbow_trim,
                run_specs=[
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_upper_vertical_helper",
                        tangent_point_mm=tuple(upper_vertical_requested_end),
                        axis_away_from_fitting="+y",
                        length_mm=float(upper_vertical_length),
                        connection_end="left_end",
                        flow_role="pipe_1",
                    ),
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_top_outlet_helper",
                        tangent_point_mm=tuple(top_outlet_requested_start),
                        axis_away_from_fitting="+x",
                        length_mm=float(top_outlet_length),
                        connection_end="left_end",
                        flow_role="pipe_2",
                    ),
                ],
                rotation_candidates=rotation_candidates,
                layer=branch_layer,
                validator=lambda ports: ports_cover_targets(
                    ports,
                    [
                        {
                            "axis": "+y",
                            "connection_end": "left_end",
                            "coordinate_index": 1,
                            "relation": "lt",
                            "reference_value": top_turn_y,
                        },
                        {
                            "axis": "+x",
                            "connection_end": "left_end",
                            "coordinate_index": 0,
                            "relation": "lt",
                            "reference_value": section_x,
                        },
                    ],
                ),
            )
        if lower_vertical_length > 1e-6 and bottom_outlet_length > 1e-6:
            bottom_elbow_geometry = select_matching_elbow_geometry(
                controller,
                dn=branch_dn,
                elbow_center=(section_x, bottom_turn_y, elevation),
                bend_radius=elbow_trim,
                run_specs=[
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_lower_vertical_helper",
                        tangent_point_mm=tuple(lower_vertical_requested_start),
                        axis_away_from_fitting="-y",
                        length_mm=float(lower_vertical_length),
                        connection_end="right_end",
                        flow_role="pipe_1",
                    ),
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_bottom_outlet_helper",
                        tangent_point_mm=tuple(bottom_outlet_requested_start),
                        axis_away_from_fitting="+x",
                        length_mm=float(bottom_outlet_length),
                        connection_end="left_end",
                        flow_role="pipe_2",
                    ),
                ],
                rotation_candidates=rotation_candidates,
                layer=branch_layer,
                validator=lambda ports: ports_cover_targets(
                    ports,
                    [
                        {
                            "axis": "-y",
                            "connection_end": "right_end",
                            "coordinate_index": 1,
                            "relation": "gt",
                            "reference_value": bottom_turn_y,
                        },
                        {
                            "axis": "+x",
                            "connection_end": "left_end",
                            "coordinate_index": 0,
                            "relation": "lt",
                            "reference_value": section_x,
                        },
                    ],
                ),
            )

    upper_vertical_actual_end = upper_vertical_requested_end
    lower_vertical_actual_start = lower_vertical_requested_start
    top_outlet_actual_start = top_outlet_requested_start
    bottom_outlet_actual_start = bottom_outlet_requested_start
    if top_elbow_geometry is not None:
        top_helper_runs = {run["run_id"]: run for run in top_elbow_geometry["runs"]}
        upper_vertical_actual_end = [
            float(value) for value in top_helper_runs[f"{bundle_id}_upper_vertical_helper"]["tangent_point_mm"]
        ]
        top_outlet_actual_start = [
            float(value) for value in top_helper_runs[f"{bundle_id}_top_outlet_helper"]["tangent_point_mm"]
        ]
    if bottom_elbow_geometry is not None:
        bottom_helper_runs = {run["run_id"]: run for run in bottom_elbow_geometry["runs"]}
        lower_vertical_actual_start = [
            float(value) for value in bottom_helper_runs[f"{bundle_id}_lower_vertical_helper"]["tangent_point_mm"]
        ]
        bottom_outlet_actual_start = [
            float(value) for value in bottom_helper_runs[f"{bundle_id}_bottom_outlet_helper"]["tangent_point_mm"]
        ]

    if upper_vertical_actual_end[1] - upper_vertical_start[1] > 1e-6:
        draw_pipe_segment(service, start=upper_vertical_start, end=upper_vertical_actual_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_upper_vertical",
            category="section_preview",
            dn=branch_dn,
            start=upper_vertical_start,
            end=upper_vertical_actual_end,
            basis=basis,
            path_id="ff_plan_external_chain",
            source_run_id="ff_plan_upper_vertical_dn150",
            from_node_id="ff_bottom_tee_18",
            to_node_id=f"{bundle_id}_top_elbow",
        )
    if lower_vertical_end[1] - lower_vertical_actual_start[1] > 1e-6:
        draw_pipe_segment(service, start=lower_vertical_actual_start, end=lower_vertical_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_lower_vertical",
            category="section_preview",
            dn=branch_dn,
            start=lower_vertical_actual_start,
            end=lower_vertical_end,
            basis=basis,
            path_id="ff_plan_external_chain",
            source_run_id="ff_plan_lower_vertical_dn150",
            from_node_id=f"{bundle_id}_bottom_elbow",
            to_node_id="ff_bottom_tee_18",
        )
    if top_outlet_actual_start[0] - top_outlet_end[0] > 1e-6:
        draw_pipe_segment(service, start=top_outlet_actual_start, end=top_outlet_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_top_outlet",
            category="section_preview",
            dn=branch_dn,
            start=top_outlet_actual_start,
            end=top_outlet_end,
            basis=basis,
            path_id="ff_plan_external_chain",
            source_run_id="ff_plan_top_outlet_dn150",
            from_node_id=f"{bundle_id}_top_elbow",
            to_node_id=None,
        )
    if bottom_outlet_actual_start[0] - bottom_outlet_end[0] > 1e-6:
        draw_pipe_segment(service, start=bottom_outlet_actual_start, end=bottom_outlet_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_bottom_outlet",
            category="section_preview",
            dn=branch_dn,
            start=bottom_outlet_actual_start,
            end=bottom_outlet_end,
            basis=basis,
            path_id="ff_plan_external_chain",
            source_run_id="ff_plan_bottom_outlet_dn150",
            from_node_id=f"{bundle_id}_bottom_elbow",
            to_node_id=None,
        )

    if bool(route.get("draw_fittings", True)):
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_top_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=[section_x, top_turn_y, elevation],
            handle=top_elbow_geometry["elbow_handle"] if top_elbow_geometry is not None else None,
            basis=basis,
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_bottom_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=[section_x, bottom_turn_y, elevation],
            handle=bottom_elbow_geometry["elbow_handle"] if bottom_elbow_geometry is not None else None,
            basis=basis,
        )


def draw_hh_plan_external_connection_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw plan-derived external DN125 pipes that connect to the frozen H-H section."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    anchor_x = float(route["anchor_x"])
    drop_leg_y = float(route["drop_leg_y"])
    bottom_elevation = float(route["bottom_elevation_mm"])
    lower_outlet_length = float(route["lower_outlet_length_mm"])
    header_connection_y = float(route["base_y"])
    header_connection_z = float(route["base_z"])
    elbow_item_no = route.get("elbow_item_no")
    header_branch_item_no = route.get("header_branch_item_no")
    basis = str(route.get("basis", ""))
    branch_layer = f"PIPE_DN{branch_dn}"
    controller = service.controller

    elbow_trim = resolve_lr_elbow_connection_trim_mm(None, branch_dn)
    rotation_candidates = build_common_elbow_rotation_candidates()
    resolved_tee = resolve_tee_center_to_end_gb12459(run_dn=450, branch_dn=branch_dn)
    tee_run_center_to_end = float(resolved_tee[0]) if resolved_tee else 0.0
    tee_branch_center_to_end = float(resolved_tee[1]) if resolved_tee else 0.0
    tee_branch_display_offset, _ = resolve_tee_display_offsets_mm(
        run_dn=450,
        branch_center_to_end_mm=tee_branch_center_to_end,
        run_center_to_end_mm=tee_run_center_to_end,
    )

    lower_outlet_end = [anchor_x + elbow_trim + lower_outlet_length, drop_leg_y, bottom_elevation]
    lower_turn_center_x = lower_outlet_end[0] + elbow_trim
    middle_y_requested_start = [lower_turn_center_x, drop_leg_y + elbow_trim, bottom_elevation]
    middle_y_requested_end = [lower_turn_center_x, header_connection_y - elbow_trim, bottom_elevation]
    header_vertical_requested_start = [lower_turn_center_x, header_connection_y, bottom_elevation - elbow_trim]
    header_vertical_end = [lower_turn_center_x, header_connection_y, header_connection_z + tee_branch_display_offset]

    lower_turn_elbow = None
    header_turn_elbow = None
    if bool(route.get("draw_fittings", True)):
        middle_y_length = middle_y_requested_end[1] - middle_y_requested_start[1]
        header_vertical_length = header_vertical_requested_start[2] - header_vertical_end[2]
        if middle_y_length > 1e-6:
            lower_turn_elbow = select_matching_elbow_geometry(
                controller,
                dn=branch_dn,
                elbow_center=(lower_turn_center_x, drop_leg_y, bottom_elevation),
                bend_radius=elbow_trim,
                run_specs=[
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_section_outlet_helper",
                        tangent_point_mm=tuple(lower_outlet_end),
                        axis_away_from_fitting="+x",
                        length_mm=float(elbow_trim),
                        connection_end="left_end",
                        flow_role="pipe_1",
                    ),
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_middle_y_lower_helper",
                        tangent_point_mm=tuple(middle_y_requested_start),
                        axis_away_from_fitting="-y",
                        length_mm=float(middle_y_length),
                        connection_end="right_end",
                        flow_role="pipe_2",
                    ),
                ],
                rotation_candidates=rotation_candidates,
                layer=branch_layer,
                validator=lambda ports: ports_cover_targets(
                    ports,
                    [
                        {
                            "axis": "+x",
                            "connection_end": "left_end",
                            "coordinate_index": 0,
                            "relation": "lt",
                            "reference_value": lower_turn_center_x,
                        },
                        {
                            "axis": "-y",
                            "connection_end": "right_end",
                            "coordinate_index": 1,
                            "relation": "gt",
                            "reference_value": drop_leg_y,
                        },
                    ],
                ),
            )
        if middle_y_length > 1e-6 and header_vertical_length > 1e-6:
            header_turn_elbow = select_matching_elbow_geometry(
                controller,
                dn=branch_dn,
                elbow_center=(lower_turn_center_x, header_connection_y, bottom_elevation),
                bend_radius=elbow_trim,
                run_specs=[
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_middle_y_upper_helper",
                        tangent_point_mm=tuple(middle_y_requested_end),
                        axis_away_from_fitting="+y",
                        length_mm=float(middle_y_length),
                        connection_end="left_end",
                        flow_role="pipe_1",
                    ),
                    PipeConnectionRunSpec(
                        run_id=f"{bundle_id}_header_vertical_helper",
                        tangent_point_mm=tuple(header_vertical_requested_start),
                        axis_away_from_fitting="+z",
                        length_mm=float(header_vertical_length),
                        connection_end="bottom_end",
                        flow_role="pipe_2",
                    ),
                ],
                rotation_candidates=rotation_candidates,
                layer=branch_layer,
                validator=lambda ports: ports_cover_targets(
                    ports,
                    [
                        {
                            "axis": "+y",
                            "connection_end": "left_end",
                            "coordinate_index": 1,
                            "relation": "lt",
                            "reference_value": header_connection_y,
                        },
                        {
                            "axis": "+z",
                            "connection_end": "bottom_end",
                            "coordinate_index": 2,
                            "relation": "lt",
                            "reference_value": bottom_elevation,
                        },
                    ],
                ),
            )

    middle_y_actual_start = middle_y_requested_start
    middle_y_actual_end = middle_y_requested_end
    header_vertical_actual_start = header_vertical_requested_start
    if lower_turn_elbow is not None:
        lower_helper_runs = {run["run_id"]: run for run in lower_turn_elbow["runs"]}
        middle_y_actual_start = [
            float(value) for value in lower_helper_runs[f"{bundle_id}_middle_y_lower_helper"]["tangent_point_mm"]
        ]
    if header_turn_elbow is not None:
        header_helper_runs = {run["run_id"]: run for run in header_turn_elbow["runs"]}
        middle_y_actual_end = [
            float(value) for value in header_helper_runs[f"{bundle_id}_middle_y_upper_helper"]["tangent_point_mm"]
        ]
        header_vertical_actual_start = [
            float(value) for value in header_helper_runs[f"{bundle_id}_header_vertical_helper"]["tangent_point_mm"]
        ]

    if middle_y_actual_end[1] - middle_y_actual_start[1] > 1e-6:
        draw_pipe_segment(service, start=middle_y_actual_start, end=middle_y_actual_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_middle_y",
            category="section_preview",
            dn=branch_dn,
            start=middle_y_actual_start,
            end=middle_y_actual_end,
            basis=basis,
            path_id="hh_plan_external_chain",
            source_run_id="hh_plan_middle_y_dn125",
            from_node_id=f"{bundle_id}_lower_turn_elbow",
            to_node_id=f"{bundle_id}_header_turn_elbow",
        )
    if header_vertical_actual_start[2] - header_vertical_end[2] > 1e-6:
        draw_pipe_segment(service, start=header_vertical_actual_start, end=header_vertical_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_header_vertical",
            category="section_preview",
            dn=branch_dn,
            start=header_vertical_actual_start,
            end=header_vertical_end,
            basis=basis,
            path_id="hh_plan_external_chain",
            source_run_id="hh_plan_header_drop_dn125",
            from_node_id=f"{bundle_id}_header_turn_elbow",
            to_node_id=f"{bundle_id}_header_branch_tee",
        )

    if bool(route.get("draw_fittings", True)):
        tee_entity = call_controller_with_retry(
            f"{bundle_id} header tee",
            lambda: controller.draw_tee(
                center=(lower_turn_center_x, header_connection_y, header_connection_z),
                dn=450,
                branch_dn=branch_dn,
                run_axis="x",
                branch_axis="z",
                run_center_to_end=tee_run_center_to_end,
                branch_center_to_end=tee_branch_center_to_end,
                layer="PIPE_DN450",
                color=WHITE,
            ),
        )
        if tee_entity is None:
            raise RuntimeError(f"{bundle_id} 的 DN450x125 三通绘制失败。")
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_header_branch_tee",
            category="section_preview_fitting",
            kind="reducing_tee",
            item_no=header_branch_item_no,
            center=[lower_turn_center_x, header_connection_y, header_connection_z],
            handle=extract_entity_handle(tee_entity),
            basis=basis,
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_lower_turn_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=[lower_turn_center_x, drop_leg_y, bottom_elevation],
            handle=lower_turn_elbow["elbow_handle"] if lower_turn_elbow is not None else None,
            basis=basis,
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_header_turn_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=[lower_turn_center_x, header_connection_y, bottom_elevation],
            handle=header_turn_elbow["elbow_handle"] if header_turn_elbow is not None else None,
            basis=basis,
        )


def draw_hh_section_preview_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the H-H DN125 local drop using the v18 local topology."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    anchor_x = float(route["anchor_x"])
    section_plane_y = float(route["drop_leg_y"])
    drop_leg_x = anchor_x
    top_elevation = float(route["top_elevation_mm"])
    bottom_elevation = float(route["bottom_elevation_mm"])
    top_branch_length = float(route.get("top_branch_length_mm", 0.0))
    lower_outlet_length = float(route.get("lower_outlet_length_mm", 0.0))
    elbow_item_no = route.get("elbow_item_no")
    header_branch_item_no = route.get("header_branch_item_no")
    section_route_entry = route.get("section_route_entry")
    valve_item_no = resolve_section_route_node_value(
        section_route_entry,
        "hh_valve_6",
        "item_no",
        route.get("valve_item_no", 6),
    )
    valve_center_elevation = float(
        resolve_section_route_node_value(
            section_route_entry,
            "hh_valve_6",
            "elevation_mm",
            route.get("valve_center_elevation_mm", 1200.0),
        )
    )
    valve_structure_length = float(
        resolve_section_route_node_value(
            section_route_entry,
            "hh_valve_6",
            "structure_length_mm",
            route.get("valve_structure_length_mm", 64.0),
        )
    )
    valve_half_length = valve_structure_length / 2.0 if valve_structure_length > 0 else 0.0
    valve_pn = float(route.get("valve_pn", 10.0))
    basis = str(route.get("basis", ""))
    branch_layer = f"PIPE_DN{branch_dn}"
    controller = service.controller
    elbow_trim = resolve_lr_elbow_connection_trim_mm(None, branch_dn)

    top_branch_start = [drop_leg_x - top_branch_length, section_plane_y, top_elevation]
    top_branch_tangent = [drop_leg_x - elbow_trim, section_plane_y, top_elevation]
    vertical_top_tangent = [drop_leg_x, section_plane_y, top_elevation - elbow_trim]
    vertical_bottom_tangent = [drop_leg_x, section_plane_y, bottom_elevation + elbow_trim]
    lower_outlet_tangent = [drop_leg_x + elbow_trim, section_plane_y, bottom_elevation]
    lower_outlet_end = [drop_leg_x + elbow_trim + lower_outlet_length, section_plane_y, bottom_elevation]

    upper_elbow_geometry = None
    lower_elbow_geometry = None
    lower_elbow_handle: str | None = None
    if bool(route.get("draw_fittings", True)):
        top_branch_length_available = top_branch_tangent[0] - top_branch_start[0]
        vertical_drop_length = vertical_top_tangent[2] - vertical_bottom_tangent[2]
        lower_outlet_length_available = lower_outlet_end[0] - lower_outlet_tangent[0]
        if top_branch_length_available > 1e-6 and vertical_drop_length > 1e-6:
            upper_run_specs = [
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_top_branch_helper",
                    tangent_point_mm=tuple(top_branch_tangent),
                    axis_away_from_fitting="+x",
                    length_mm=float(top_branch_length_available),
                    connection_end="left_end",
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_vertical_top_helper",
                    tangent_point_mm=tuple(vertical_top_tangent),
                    axis_away_from_fitting="+z",
                    length_mm=float(vertical_drop_length),
                    connection_end="bottom_end",
                    flow_role="pipe_2",
                ),
            ]
            upper_elbow_geometry = select_matching_elbow_geometry(
                controller,
                dn=branch_dn,
                elbow_center=(drop_leg_x, section_plane_y, top_elevation),
                bend_radius=elbow_trim,
                run_specs=upper_run_specs,
                rotation_candidates=[
                    [
                        RotationInstruction(angle_deg=-90.0, axis_direction=(1.0, 0.0, 0.0), axis_name="x"),
                        RotationInstruction(angle_deg=90.0, axis_direction=(0.0, 1.0, 0.0), axis_name="y"),
                    ],
                    [
                        RotationInstruction(angle_deg=90.0, axis_direction=(1.0, 0.0, 0.0), axis_name="x"),
                        RotationInstruction(angle_deg=90.0, axis_direction=(0.0, 1.0, 0.0), axis_name="y"),
                    ],
                    [
                        RotationInstruction(angle_deg=-90.0, axis_direction=(0.0, 1.0, 0.0), axis_name="y"),
                        RotationInstruction(angle_deg=-90.0, axis_direction=(0.0, 0.0, 1.0), axis_name="z"),
                    ],
                    [
                        RotationInstruction(angle_deg=90.0, axis_direction=(0.0, 1.0, 0.0), axis_name="y"),
                        RotationInstruction(angle_deg=-90.0, axis_direction=(0.0, 0.0, 1.0), axis_name="z"),
                    ],
                ],
                layer=branch_layer,
                validator=lambda ports: (
                    float(ports["leg_y"]["tangent_point_mm"][0]) < drop_leg_x - 1e-6
                    and str(ports["leg_y"]["axis_away_from_fitting"]) == "+x"
                    and str(ports["leg_y"]["connection_end"]) == "left_end"
                    and float(ports["leg_x"]["tangent_point_mm"][2]) < top_elevation - 1e-6
                    and str(ports["leg_x"]["axis_away_from_fitting"]) == "+z"
                    and str(ports["leg_x"]["connection_end"]) == "bottom_end"
                ),
            )
        if vertical_drop_length > 1e-6 and lower_outlet_length_available > 1e-6:
            lower_run_specs = [
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_vertical_bottom_helper",
                    tangent_point_mm=tuple(vertical_bottom_tangent),
                    axis_away_from_fitting="-z",
                    length_mm=float(vertical_drop_length),
                    connection_end="top_end",
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_lower_outlet_helper",
                    tangent_point_mm=tuple(lower_outlet_tangent),
                    axis_away_from_fitting="-x",
                    length_mm=float(lower_outlet_length_available),
                    connection_end="right_end",
                    flow_role="pipe_2",
                ),
            ]
            lower_elbow_geometry = select_matching_elbow_geometry(
                controller,
                dn=branch_dn,
                elbow_center=(drop_leg_x, section_plane_y, bottom_elevation),
                bend_radius=elbow_trim,
                run_specs=lower_run_specs,
                rotation_candidates=[
                    [
                        RotationInstruction(angle_deg=-90.0, axis_direction=(0.0, 1.0, 0.0), axis_name="y"),
                        RotationInstruction(angle_deg=-90.0, axis_direction=(0.0, 0.0, 1.0), axis_name="z"),
                    ],
                    [
                        RotationInstruction(angle_deg=90.0, axis_direction=(0.0, 1.0, 0.0), axis_name="y"),
                        RotationInstruction(angle_deg=-90.0, axis_direction=(0.0, 0.0, 1.0), axis_name="z"),
                    ],
                    [
                        RotationInstruction(angle_deg=180.0, axis_direction=(1.0, 0.0, 0.0), axis_name="x"),
                        RotationInstruction(angle_deg=90.0, axis_direction=(0.0, 1.0, 0.0), axis_name="y"),
                    ],
                    [
                        RotationInstruction(angle_deg=180.0, axis_direction=(1.0, 0.0, 0.0), axis_name="x"),
                        RotationInstruction(angle_deg=-90.0, axis_direction=(0.0, 1.0, 0.0), axis_name="y"),
                    ],
                ],
                layer=branch_layer,
                validator=lambda ports: (
                    float(ports["leg_x"]["tangent_point_mm"][2]) > bottom_elevation + 1e-6
                    and str(ports["leg_x"]["axis_away_from_fitting"]) == "-z"
                    and str(ports["leg_x"]["connection_end"]) == "top_end"
                    and float(ports["leg_y"]["tangent_point_mm"][0]) > drop_leg_x + 1e-6
                    and str(ports["leg_y"]["axis_away_from_fitting"]) == "-x"
                    and str(ports["leg_y"]["connection_end"]) == "right_end"
                ),
            )
            if lower_elbow_geometry is not None:
                lower_elbow_handle = str(lower_elbow_geometry["elbow_handle"])
            else:
                lower_elbow_handle = draw_cycle_water_lr_elbow(
                    service,
                    description=f"{bundle_id} 下弯头回退绘制",
                    corner_point=(drop_leg_x, section_plane_y, bottom_elevation),
                    dn=branch_dn,
                    layer=branch_layer,
                    primary_rotation_deg=-90.0,
                    primary_rotation_axis="x",
                    secondary_rotation_deg=90.0,
                    secondary_rotation_axis="y",
                    color=WHITE,
                )

    top_branch_actual_tangent = top_branch_tangent
    vertical_top_actual_tangent = vertical_top_tangent
    vertical_bottom_actual_tangent = vertical_bottom_tangent
    lower_outlet_actual_tangent = lower_outlet_tangent
    if upper_elbow_geometry is not None:
        upper_helper_runs = {run["run_id"]: run for run in upper_elbow_geometry["runs"]}
        top_branch_actual_tangent = [
            float(value) for value in upper_helper_runs[f"{bundle_id}_top_branch_helper"]["tangent_point_mm"]
        ]
        vertical_top_actual_tangent = [
            float(value) for value in upper_helper_runs[f"{bundle_id}_vertical_top_helper"]["tangent_point_mm"]
        ]
    if lower_elbow_geometry is not None:
        lower_helper_runs = {run["run_id"]: run for run in lower_elbow_geometry["runs"]}
        vertical_bottom_actual_tangent = [
            float(value) for value in lower_helper_runs[f"{bundle_id}_vertical_bottom_helper"]["tangent_point_mm"]
        ]
        lower_outlet_actual_tangent = [
            float(value) for value in lower_helper_runs[f"{bundle_id}_lower_outlet_helper"]["tangent_point_mm"]
        ]

    if top_branch_actual_tangent[0] - top_branch_start[0] > 1e-6:
        draw_pipe_segment(service, start=top_branch_start, end=top_branch_actual_tangent, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_top_branch",
            category="section_preview",
            dn=branch_dn,
            start=top_branch_start,
            end=top_branch_actual_tangent,
            basis=basis,
            path_id="hh_left_tower_drop_chain",
            source_run_id="hh_top_branch_dn125",
            from_node_id="hh_branch_tee_16",
            to_node_id="hh_upper_elbow_23",
        )

    valve_top_end = [drop_leg_x, section_plane_y, valve_center_elevation + valve_half_length]
    valve_bottom_end = [drop_leg_x, section_plane_y, valve_center_elevation - valve_half_length]

    if vertical_top_actual_tangent[2] - valve_top_end[2] > 1e-6:
        draw_pipe_segment(service, start=vertical_top_actual_tangent, end=valve_top_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_upper_riser",
            category="section_preview",
            dn=branch_dn,
            start=vertical_top_actual_tangent,
            end=valve_top_end,
            basis=basis,
            path_id="hh_left_tower_drop_chain",
            source_run_id="hh_upper_riser_dn125",
            from_node_id="hh_upper_elbow_23",
            to_node_id="hh_valve_6",
        )

    if valve_bottom_end[2] - vertical_bottom_actual_tangent[2] > 1e-6:
        draw_pipe_segment(service, start=valve_bottom_end, end=vertical_bottom_actual_tangent, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_lower_riser",
            category="section_preview",
            dn=branch_dn,
            start=valve_bottom_end,
            end=vertical_bottom_actual_tangent,
            basis=basis,
            path_id="hh_left_tower_drop_chain",
            source_run_id="hh_lower_riser_dn125",
            from_node_id="hh_valve_6",
            to_node_id="hh_lower_elbow_23",
        )

    if lower_outlet_end[0] - lower_outlet_actual_tangent[0] > 1e-6:
        draw_pipe_segment(service, start=lower_outlet_actual_tangent, end=lower_outlet_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_lower_outlet",
            category="section_preview",
            dn=branch_dn,
            start=lower_outlet_actual_tangent,
            end=lower_outlet_end,
            basis=basis,
            path_id="hh_left_tower_drop_chain",
            source_run_id="hh_lower_outlet_dn125",
            from_node_id="hh_lower_elbow_23",
            to_node_id=None,
        )

    if bool(route.get("draw_fittings", True)):
        requested_valve_pn = valve_pn
        resolved_valve_pn = requested_valve_pn
        if controller._get_flange_params(branch_dn, requested_valve_pn) is None:
            fallback_valve_pn = 16.0
            if controller._get_flange_params(branch_dn, fallback_valve_pn) is None:
                raise RuntimeError(f"{bundle_id} 的 6 号蝶阀缺少可用法兰规格: DN{branch_dn} PN{requested_valve_pn}")
            resolved_valve_pn = fallback_valve_pn
        valve_entity = call_controller_with_retry(
            f"{bundle_id} valve 6",
            lambda: controller.draw_butterfly_valve(
                center=(drop_leg_x, section_plane_y, valve_center_elevation),
                dn=branch_dn,
                pn=resolved_valve_pn,
                axis="z",
                layer=branch_layer,
                color=WHITE,
            ),
        )
        if valve_entity is None:
            pipe_outer_radius = PIPE_OD.get(branch_dn, float(branch_dn)) / 2.0
            valve_entity = draw_box_with_retry(
                controller,
                center=(drop_leg_x, section_plane_y, valve_center_elevation),
                length=max(pipe_outer_radius * 2.4, 56.0),
                width=max(pipe_outer_radius * 2.4, 56.0),
                height=max(valve_structure_length, 64.0),
                layer=branch_layer,
                color=WHITE,
            )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_header_branch_semantic",
            category="section_preview_fitting",
            kind="reducing_tee_semantic",
            item_no=header_branch_item_no,
            center=top_branch_start,
            handle=None,
            basis=basis,
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_upper_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=[drop_leg_x, section_plane_y, top_elevation],
            handle=upper_elbow_geometry["elbow_handle"] if upper_elbow_geometry is not None else None,
            basis=basis,
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_valve_6",
            category="section_preview_fitting",
            kind="butterfly_valve",
            item_no=valve_item_no,
            center=[drop_leg_x, section_plane_y, valve_center_elevation],
            handle=extract_entity_handle(valve_entity),
            basis=basis,
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_lower_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=[drop_leg_x, section_plane_y, bottom_elevation],
            handle=lower_elbow_geometry["elbow_handle"] if lower_elbow_geometry is not None else lower_elbow_handle,
            basis=basis,
        )


def draw_ii_section_preview_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the I-I DN150 local drop in the x-z section plane."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    anchor_x = float(route["anchor_x"])
    section_plane_y = float(route["drop_leg_y"])
    drop_leg_x = anchor_x
    top_elevation = float(route["top_elevation_mm"])
    bottom_elevation = float(route["bottom_elevation_mm"])
    top_branch_length = float(route.get("top_branch_length_mm", 0.0))
    lower_outlet_length = float(route.get("lower_outlet_length_mm", 0.0))
    section_rotation_deg_z = float(route.get("section_rotation_deg_z", 0.0))
    mirror_local_outlet_y = bool(route.get("mirror_local_outlet_y", False))
    elbow_item_no = route.get("elbow_item_no")
    header_branch_item_no = route.get("header_branch_item_no")
    basis = str(route.get("basis", ""))
    branch_layer = f"PIPE_DN{branch_dn}"
    controller = service.controller
    elbow_trim = resolve_lr_elbow_connection_trim_mm(None, branch_dn)
    rotation_center_xy = (drop_leg_x, section_plane_y)
    upper_elbow_center = [drop_leg_x, section_plane_y, top_elevation]
    lower_elbow_center = [drop_leg_x, section_plane_y, bottom_elevation]

    top_branch_start = rotate_point_around_z(
        [drop_leg_x - top_branch_length, section_plane_y, top_elevation],
        center_xy=rotation_center_xy,
        angle_deg=section_rotation_deg_z,
    )
    top_branch_tangent = rotate_point_around_z(
        [drop_leg_x - elbow_trim, section_plane_y, top_elevation],
        center_xy=rotation_center_xy,
        angle_deg=section_rotation_deg_z,
    )
    vertical_top_tangent = [drop_leg_x, section_plane_y, top_elevation - elbow_trim]
    vertical_bottom_tangent = [drop_leg_x, section_plane_y, bottom_elevation + elbow_trim]
    local_outlet_sign = -1.0 if mirror_local_outlet_y else 1.0
    lower_outlet_tangent = rotate_point_around_z(
        [drop_leg_x, section_plane_y + local_outlet_sign * elbow_trim, bottom_elevation],
        center_xy=rotation_center_xy,
        angle_deg=section_rotation_deg_z,
    )
    lower_outlet_end = rotate_point_around_z(
        [drop_leg_x, section_plane_y + local_outlet_sign * (elbow_trim + lower_outlet_length), bottom_elevation],
        center_xy=rotation_center_xy,
        angle_deg=section_rotation_deg_z,
    )
    top_branch_contract = resolve_port_contract(upper_elbow_center, top_branch_tangent)
    vertical_top_contract = resolve_port_contract(upper_elbow_center, vertical_top_tangent)
    vertical_bottom_contract = resolve_port_contract(lower_elbow_center, vertical_bottom_tangent)
    lower_outlet_contract = resolve_port_contract(lower_elbow_center, lower_outlet_tangent)

    upper_elbow_geometry = None
    lower_elbow_geometry = None
    if bool(route.get("draw_fittings", True)):
        top_branch_length_available = measure_orthogonal_segment_length_mm(top_branch_start, top_branch_tangent)
        vertical_drop_length = measure_orthogonal_segment_length_mm(vertical_top_tangent, vertical_bottom_tangent)
        lower_outlet_length_available = measure_orthogonal_segment_length_mm(lower_outlet_end, lower_outlet_tangent)
        if top_branch_length_available > 1e-6 and vertical_drop_length > 1e-6:
            upper_run_specs = [
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_top_branch_helper",
                    tangent_point_mm=tuple(top_branch_tangent),
                    axis_away_from_fitting=str(top_branch_contract["axis"]),
                    length_mm=float(top_branch_length_available),
                    connection_end=str(top_branch_contract["connection_end"]),
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_vertical_top_helper",
                    tangent_point_mm=tuple(vertical_top_tangent),
                    axis_away_from_fitting=str(vertical_top_contract["axis"]),
                    length_mm=float(vertical_drop_length),
                    connection_end=str(vertical_top_contract["connection_end"]),
                    flow_role="pipe_2",
                ),
            ]
            upper_elbow_geometry = select_matching_elbow_geometry(
                controller,
                dn=branch_dn,
                elbow_center=(drop_leg_x, section_plane_y, top_elevation),
                bend_radius=elbow_trim,
                run_specs=upper_run_specs,
                rotation_candidates=build_common_elbow_rotation_candidates(),
                layer=branch_layer,
                validator=lambda ports: ports_cover_targets(
                    ports,
                    [top_branch_contract, vertical_top_contract],
                ),
            )
        if vertical_drop_length > 1e-6 and lower_outlet_length_available > 1e-6:
            lower_run_specs = [
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_vertical_bottom_helper",
                    tangent_point_mm=tuple(vertical_bottom_tangent),
                    axis_away_from_fitting=str(vertical_bottom_contract["axis"]),
                    length_mm=float(vertical_drop_length),
                    connection_end=str(vertical_bottom_contract["connection_end"]),
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_lower_outlet_helper",
                    tangent_point_mm=tuple(lower_outlet_tangent),
                    axis_away_from_fitting=str(lower_outlet_contract["axis"]),
                    length_mm=float(lower_outlet_length_available),
                    connection_end=str(lower_outlet_contract["connection_end"]),
                    flow_role="pipe_2",
                ),
            ]
            lower_elbow_geometry = select_matching_elbow_geometry(
                controller,
                dn=branch_dn,
                elbow_center=(drop_leg_x, section_plane_y, bottom_elevation),
                bend_radius=elbow_trim,
                run_specs=lower_run_specs,
                rotation_candidates=build_common_elbow_rotation_candidates(),
                layer=branch_layer,
                validator=lambda ports: ports_cover_targets(
                    ports,
                    [vertical_bottom_contract, lower_outlet_contract],
                ),
            )

    top_branch_actual_tangent = top_branch_tangent
    vertical_top_actual_tangent = vertical_top_tangent
    vertical_bottom_actual_tangent = vertical_bottom_tangent
    lower_outlet_actual_tangent = lower_outlet_tangent
    if upper_elbow_geometry is not None:
        upper_helper_runs = {run["run_id"]: run for run in upper_elbow_geometry["runs"]}
        top_branch_actual_tangent = [
            float(value) for value in upper_helper_runs[f"{bundle_id}_top_branch_helper"]["tangent_point_mm"]
        ]
        vertical_top_actual_tangent = [
            float(value) for value in upper_helper_runs[f"{bundle_id}_vertical_top_helper"]["tangent_point_mm"]
        ]
    if lower_elbow_geometry is not None:
        lower_helper_runs = {run["run_id"]: run for run in lower_elbow_geometry["runs"]}
        vertical_bottom_actual_tangent = [
            float(value) for value in lower_helper_runs[f"{bundle_id}_vertical_bottom_helper"]["tangent_point_mm"]
        ]
        lower_outlet_actual_tangent = [
            float(value) for value in lower_helper_runs[f"{bundle_id}_lower_outlet_helper"]["tangent_point_mm"]
        ]

    if measure_orthogonal_segment_length_mm(top_branch_start, top_branch_actual_tangent) > 1e-6:
        draw_pipe_segment(service, start=top_branch_start, end=top_branch_actual_tangent, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_top_branch",
            category="section_preview",
            dn=branch_dn,
            start=top_branch_start,
            end=top_branch_actual_tangent,
            basis=basis,
            path_id="ii_local_drop_chain",
            source_run_id="ii_top_branch_dn150",
            from_node_id=(
                "ii_tank_shell_tk0202"
                if str(route.get("top_branch_connect_mode", "")).strip().lower() == "tank_shell"
                else "ii_header_branch_semantic"
            ),
            to_node_id="ii_upper_elbow_22",
        )

    if measure_orthogonal_segment_length_mm(vertical_top_actual_tangent, vertical_bottom_actual_tangent) > 1e-6:
        draw_pipe_segment(service, start=vertical_top_actual_tangent, end=vertical_bottom_actual_tangent, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_vertical_drop",
            category="section_preview",
            dn=branch_dn,
            start=vertical_top_actual_tangent,
            end=vertical_bottom_actual_tangent,
            basis=basis,
            path_id="ii_local_drop_chain",
            source_run_id="ii_vertical_drop_dn150",
            from_node_id="ii_upper_elbow_22",
            to_node_id="ii_lower_elbow_22",
        )

    if measure_orthogonal_segment_length_mm(lower_outlet_end, lower_outlet_actual_tangent) > 1e-6:
        draw_pipe_segment(service, start=lower_outlet_actual_tangent, end=lower_outlet_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_lower_outlet",
            category="section_preview",
            dn=branch_dn,
            start=lower_outlet_actual_tangent,
            end=lower_outlet_end,
            basis=basis,
            path_id="ii_local_drop_chain",
            source_run_id="ii_lower_outlet_dn150",
            from_node_id="ii_lower_elbow_22",
            to_node_id=None,
        )

    if bool(route.get("draw_fittings", True)):
        top_branch_target_tag = str(route.get("top_branch_target_equipment_tag", "")).strip()
        top_branch_connect_mode = str(route.get("top_branch_connect_mode", "")).strip().lower()
        if top_branch_target_tag and top_branch_connect_mode == "tank_shell":
            append_route_fitting_record(
                route_records,
                bundle_id=bundle_id,
                fitting_id=f"{bundle_id}_tank_shell_{top_branch_target_tag.lower()}",
                category="section_preview_fitting",
                kind="equipment_shell",
                item_no=None,
                center=[top_branch_start[0], top_branch_start[1], top_elevation],
                handle=None,
                basis=basis,
            )
        else:
            append_route_fitting_record(
                route_records,
                bundle_id=bundle_id,
                fitting_id=f"{bundle_id}_header_branch_semantic",
                category="section_preview_fitting",
                kind="branch_semantic",
                item_no=header_branch_item_no,
                center=[top_branch_start[0], section_plane_y, top_elevation],
                handle=None,
                basis=basis,
            )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_upper_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=[drop_leg_x, section_plane_y, top_elevation],
            handle=upper_elbow_geometry["elbow_handle"] if upper_elbow_geometry is not None else None,
            basis=basis,
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_lower_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=elbow_item_no,
            center=[drop_leg_x, section_plane_y, bottom_elevation],
            handle=lower_elbow_geometry["elbow_handle"] if lower_elbow_geometry is not None else None,
            basis=basis,
        )


def draw_ii_plan_external_connection_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the straight DN150 plan chain linking I-I to the F-F external run."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    anchor_x = float(route["anchor_x"])
    section_plane_y = float(route["drop_leg_y"])
    bottom_elevation = float(route["bottom_elevation_mm"])
    lower_outlet_length = float(route.get("lower_outlet_length_mm", 0.0))
    section_rotation_deg_z = float(route.get("section_rotation_deg_z", 0.0))
    mirror_local_outlet_y = bool(route.get("mirror_local_outlet_y", False))
    target_x = float(route["target_x_mm"])
    basis = str(route.get("basis", ""))
    elbow_trim = resolve_lr_elbow_connection_trim_mm(None, branch_dn)
    local_outlet_sign = -1.0 if mirror_local_outlet_y else 1.0

    chain_start = rotate_point_around_z(
        [anchor_x, section_plane_y + local_outlet_sign * (elbow_trim + lower_outlet_length), bottom_elevation],
        center_xy=(anchor_x, section_plane_y),
        angle_deg=section_rotation_deg_z,
    )
    chain_end = [target_x, section_plane_y, bottom_elevation]
    if measure_orthogonal_segment_length_mm(chain_start, chain_end) <= 1e-6:
        return

    draw_pipe_segment(service, start=chain_start, end=chain_end, dn=branch_dn)
    append_route_segment_record(
        route_records,
        bundle_id=bundle_id,
        segment_id=f"{bundle_id}_ff_connection",
        category="section_preview",
        dn=branch_dn,
        start=chain_start,
        end=chain_end,
        basis=basis,
        path_id="ii_ff_plan_connection_chain",
        source_run_id="ii_ff_plan_connection_dn150",
        from_node_id="ii_lower_elbow_22",
        to_node_id="ff_plan_external_connection_dn150",
    )


def draw_ee_section_preview_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the E-E section using helper-validated elbow ports."""

    bundle_id = str(route["route_id"])
    branch_dn = int(route["dn"])
    anchor_x = float(route["anchor_x"])
    base_y = float(route["base_y"])
    base_z = float(route["base_z"])
    right_vertical_y = float(route["right_vertical_y"])
    top_horizontal_elevation = float(route["top_horizontal_elevation_mm"])
    right_vertical_bottom_elevation = float(route["right_vertical_bottom_elevation_mm"])
    tee_branch_center_to_end = float(route.get("tee_branch_center_to_end_mm", 0.0))
    tee_branch_display_offset = float(route.get("tee_branch_display_offset_mm", tee_branch_center_to_end))
    valve_center_elevation = float(route.get("valve_center_elevation_mm", top_horizontal_elevation))
    valve_structure_length = float(route.get("valve_structure_length_mm", 0.0))
    valve_half_length = valve_structure_length / 2.0 if valve_structure_length > 0 else 0.0
    basis = str(route.get("basis", ""))
    controller = service.controller
    left_elbow_type = str(route.get("left_elbow_type", "LR")).strip().upper()
    right_elbow_type = str(route.get("right_elbow_type", "LR")).strip().upper()
    left_elbow_connection_trim = float(
        route.get("left_elbow_connection_trim_mm")
        if route.get("left_elbow_connection_trim_mm") is not None
        else resolve_elbow_bend_radius(controller, branch_dn, left_elbow_type)
    )
    right_elbow_connection_trim = float(
        route.get("right_elbow_connection_trim_mm")
        if route.get("right_elbow_connection_trim_mm") is not None
        else resolve_elbow_bend_radius(controller, branch_dn, right_elbow_type)
    )

    lower_vertical_start = [anchor_x, base_y, base_z + tee_branch_display_offset]
    lower_vertical_end = [anchor_x, base_y, valve_center_elevation - valve_half_length]
    if lower_vertical_end[2] - lower_vertical_start[2] > 1e-6:
        draw_pipe_segment(service, start=lower_vertical_start, end=lower_vertical_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_lower_vertical",
            category="section_preview",
            dn=branch_dn,
            start=lower_vertical_start,
            end=lower_vertical_end,
            basis=basis,
        )

    upper_vertical_far_end = [anchor_x, base_y, valve_center_elevation + valve_half_length]
    left_vertical_requested = [anchor_x, base_y, top_horizontal_elevation - left_elbow_connection_trim]
    left_horizontal_requested = [anchor_x, base_y + left_elbow_connection_trim, top_horizontal_elevation]
    right_horizontal_requested = [anchor_x, right_vertical_y - right_elbow_connection_trim, top_horizontal_elevation]
    right_vertical_requested = [anchor_x, right_vertical_y, top_horizontal_elevation - right_elbow_connection_trim]
    left_vertical_length = left_vertical_requested[2] - upper_vertical_far_end[2]
    top_horizontal_length = right_horizontal_requested[1] - left_horizontal_requested[1]
    right_vertical_length = right_vertical_requested[2] - right_vertical_bottom_elevation

    left_elbow_geometry = None
    right_elbow_geometry = None
    if left_vertical_length > 1e-6 and top_horizontal_length > 1e-6:
        left_elbow_geometry = build_orthogonal_elbow_connection(
            controller,
            dn=branch_dn,
            elbow_center=(anchor_x, base_y, top_horizontal_elevation),
            elbow_type=left_elbow_type,
            run_specs=[
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_upper_vertical",
                    tangent_point_mm=tuple(left_vertical_requested),
                    axis_away_from_fitting="+z",
                    length_mm=float(left_vertical_length),
                    connection_end="bottom_end",
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_top_horizontal_left",
                    tangent_point_mm=tuple(left_horizontal_requested),
                    axis_away_from_fitting="-y",
                    length_mm=float(top_horizontal_length),
                    connection_end="right_end",
                    flow_role="pipe_2",
                ),
            ],
            elbow_rotations=[
                RotationInstruction(
                    angle_deg=float(route.get("left_elbow_rotation_deg", -90.0)),
                    axis_direction=resolve_rotation_axis(route.get("left_elbow_rotation_axis", "y")),
                    axis_name=str(route.get("left_elbow_rotation_axis", "y")),
                ),
                RotationInstruction(
                    angle_deg=float(route.get("left_elbow_secondary_rotation_deg", 180.0)),
                    axis_direction=resolve_rotation_axis(route.get("left_elbow_secondary_rotation_axis", "z")),
                    axis_name=str(route.get("left_elbow_secondary_rotation_axis", "z")),
                ),
            ],
            layer=f"PIPE_DN{branch_dn}",
            color=WHITE,
            draw_runs=False,
        )

    if right_vertical_length > 1e-6 and top_horizontal_length > 1e-6:
        right_elbow_geometry = build_orthogonal_elbow_connection(
            controller,
            dn=branch_dn,
            elbow_center=(anchor_x, right_vertical_y, top_horizontal_elevation),
            elbow_type=right_elbow_type,
            run_specs=[
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_right_vertical",
                    tangent_point_mm=tuple(right_vertical_requested),
                    axis_away_from_fitting="+z",
                    length_mm=float(right_vertical_length),
                    connection_end="bottom_end",
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_top_horizontal_right",
                    tangent_point_mm=tuple(right_horizontal_requested),
                    axis_away_from_fitting="+y",
                    length_mm=float(top_horizontal_length),
                    connection_end="left_end",
                    flow_role="pipe_2",
                ),
            ],
            elbow_rotations=[
                RotationInstruction(
                    angle_deg=float(route.get("right_elbow_rotation_deg", -90.0)),
                    axis_direction=resolve_rotation_axis(route.get("right_elbow_rotation_axis", "y")),
                    axis_name=str(route.get("right_elbow_rotation_axis", "y")),
                ),
                RotationInstruction(
                    angle_deg=float(route.get("right_elbow_secondary_rotation_deg", 0.0)),
                    axis_direction=resolve_rotation_axis(route.get("right_elbow_secondary_rotation_axis", "x")),
                    axis_name=str(route.get("right_elbow_secondary_rotation_axis", "x")),
                ),
            ],
            layer=f"PIPE_DN{branch_dn}",
            color=WHITE,
            draw_runs=False,
        )

    if left_elbow_geometry is not None:
        left_helper_runs = {run["run_id"]: run for run in left_elbow_geometry["runs"]}
        left_vertical_tangent = [float(value) for value in left_helper_runs[f"{bundle_id}_upper_vertical"]["tangent_point_mm"]]
        left_horizontal_tangent = [float(value) for value in left_helper_runs[f"{bundle_id}_top_horizontal_left"]["tangent_point_mm"]]
    else:
        left_vertical_tangent = left_vertical_requested
        left_horizontal_tangent = left_horizontal_requested

    if right_elbow_geometry is not None:
        right_helper_runs = {run["run_id"]: run for run in right_elbow_geometry["runs"]}
        right_vertical_tangent = [float(value) for value in right_helper_runs[f"{bundle_id}_right_vertical"]["tangent_point_mm"]]
        right_horizontal_tangent = [float(value) for value in right_helper_runs[f"{bundle_id}_top_horizontal_right"]["tangent_point_mm"]]
    else:
        right_vertical_tangent = right_vertical_requested
        right_horizontal_tangent = right_horizontal_requested

    if left_vertical_tangent[2] - upper_vertical_far_end[2] > 1e-6:
        draw_pipe_segment(service, start=upper_vertical_far_end, end=left_vertical_tangent, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_upper_vertical",
            category="section_preview",
            dn=branch_dn,
            start=upper_vertical_far_end,
            end=left_vertical_tangent,
            basis=basis,
            helper_generated=left_elbow_geometry is not None,
        )

    if right_horizontal_tangent[1] - left_horizontal_tangent[1] > 1e-6:
        draw_pipe_segment(service, start=left_horizontal_tangent, end=right_horizontal_tangent, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_top_horizontal",
            category="section_preview",
            dn=branch_dn,
            start=left_horizontal_tangent,
            end=right_horizontal_tangent,
            basis=basis,
            helper_generated=left_elbow_geometry is not None or right_elbow_geometry is not None,
        )

    right_vertical_end = [anchor_x, right_vertical_y, right_vertical_bottom_elevation]
    right_vertical_draw_start = list(right_vertical_tangent)
    if right_vertical_draw_start[2] - right_vertical_end[2] > 1e-6:
        draw_pipe_segment(service, start=right_vertical_draw_start, end=right_vertical_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_right_vertical",
            category="section_preview",
            dn=branch_dn,
            start=right_vertical_draw_start,
            end=right_vertical_end,
            basis=basis,
            helper_generated=right_elbow_geometry is not None,
        )

    draw_section_preview_fittings(
        service,
        route=route,
        route_records=route_records,
        skip_left_elbow=True,
        skip_right_elbow=True,
    )
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_left_elbow",
        category="section_preview_fitting",
        kind="elbow",
        item_no=route.get("elbow_item_no"),
        center=[anchor_x, base_y, top_horizontal_elevation],
        handle=None,
        basis=basis,
        helper_generated=True,
    )
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_right_elbow",
        category="section_preview_fitting",
        kind="elbow",
        item_no=route.get("right_elbow_item_no", route.get("elbow_item_no")),
        center=[anchor_x, right_vertical_y, top_horizontal_elevation],
        handle=None,
        basis=basis,
        helper_generated=True,
    )


def draw_aa_section_preview_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the A-A section preview anchored to the second embedded sleeve."""

    bundle_id = str(route["route_id"])
    anchor_x = float(route["anchor_x"])
    upper_anchor_x = float(route.get("upper_anchor_x_mm", anchor_x))
    right_upper_anchor_x = upper_anchor_x
    basis = str(route.get("basis", ""))
    controller = service.controller
    section_route_entry = route.get("section_route_entry") if isinstance(route.get("section_route_entry"), dict) else None
    draw_left_lower_branch = bool(route.get("draw_left_lower_branch", True))
    draw_right_lower_branch = bool(route.get("draw_right_lower_branch", True))
    draw_right_upper_branch = bool(route.get("draw_right_upper_branch", draw_right_lower_branch))

    left_header_y = float(route["left_header_y"])
    left_header_z = float(route["left_header_z"])
    left_header_proxy_dn = int(route["left_header_proxy_dn"])
    left_branch_dn = int(route["left_branch_dn"])
    left_valve_center_elevation = float(route["left_valve_center_elevation_mm"])
    left_valve_structure_length = float(route.get("left_valve_structure_length_mm", 76.0))
    left_valve_half_length = left_valve_structure_length / 2.0 if left_valve_structure_length > 0 else 0.0
    left_top_horizontal_elevation = float(route.get("left_top_horizontal_elevation_mm", left_valve_center_elevation))
    left_top_horizontal_end_y = float(route["left_top_horizontal_end_y"])

    pump_vertical_dn = int(route["pump_vertical_dn"])
    pump_vertical_y = float(route["pump_vertical_y"])
    pump_vertical_bottom_elevation = float(route["pump_vertical_bottom_elevation_mm"])
    pump_vertical_top_elevation = float(route["pump_vertical_top_elevation_mm"])
    pump_valve_center_y = float(route["pump_valve_center_y"])
    pump_valve_structure_length = float(route.get("pump_valve_structure_length_mm", 83.0))
    pump_valve_half_length = pump_valve_structure_length / 2.0 if pump_valve_structure_length > 0 else 0.0
    pump_reducer_large_dn = int(route["pump_reducer_large_dn"])
    pump_reducer_small_dn = int(route["pump_reducer_small_dn"])
    pump_reducer_large_end_y = float(route["pump_reducer_large_end_y"])
    pump_reducer_small_end_y = float(route.get("pump_reducer_small_end_y", pump_reducer_large_end_y + float(route.get("pump_reducer_length_mm", 300.0))))
    pump_reducer_length = pump_reducer_small_end_y - pump_reducer_large_end_y
    if pump_reducer_length <= 1e-6:
        pump_reducer_length = float(route.get("pump_reducer_length_mm", 300.0))
        pump_reducer_small_end_y = pump_reducer_large_end_y + pump_reducer_length

    right_header_y = float(route["right_header_y"])
    right_header_z = float(route["right_header_z"])
    right_header_proxy_dn = int(route["right_header_proxy_dn"])
    right_branch_dn = int(route["right_branch_dn"])
    right_vertical_bottom_elevation = float(route.get("right_vertical_bottom_elevation_mm", right_header_z))
    right_horizontal_start_y = float(
        route.get(
            "right_horizontal_start_y",
            resolve_section_route_position_y(
                section_route_entry,
                point_id="aa_right_horizontal_start",
                default=right_header_y,
            ),
        )
    )
    right_valve_center_y = float(route["right_valve_center_y"])
    right_valve_structure_length = float(route.get("right_valve_structure_length_mm", 76.0))
    right_valve_half_length = right_valve_structure_length / 2.0 if right_valve_structure_length > 0 else 0.0
    right_riser_top_elevation = float(route["right_riser_top_elevation_mm"])
    support_center = [float(value) for value in route["pump_support_center"]]
    support_size_mm = [float(value) for value in route.get("pump_support_size_mm", [280.0, 280.0, 600.0])]
    pump_support_bottom_elevation = float(route.get("pump_support_bottom_elevation_mm", -1400.0))
    pump_support_spec = controller.SUCTION_BELLMOUTH_SUPPORT_02S403.get(("ZB3", "J", "B"))
    pump_support_height = float(
        pump_support_spec["support_height"] if pump_support_spec is not None else support_size_mm[2]
    )
    support_center[0] = upper_anchor_x
    support_center[2] = pump_support_bottom_elevation + pump_support_height / 2.0
    pump_blind_flange_thickness = float(route.get("pump_blind_flange_thickness_mm", 80.0))
    pump_blind_flange_base_z = pump_support_bottom_elevation + pump_support_height
    pump_blind_flange_center_z = pump_blind_flange_base_z + pump_blind_flange_thickness / 2.0
    pump_vertical_bottom_elevation = min(
        pump_vertical_bottom_elevation,
        pump_blind_flange_base_z + pump_blind_flange_thickness,
    )

    pump_sleeve_30_center_y = resolve_section_route_position_y(
        section_route_entry,
        point_id="aa_p0204_item30_center",
        default=max(
            pump_vertical_y + PIPE_OD[pump_vertical_dn] * 0.55,
            pump_valve_center_y - pump_valve_half_length - 140.0,
        ),
    )
    right_joint_12_item_no = resolve_section_route_node_value(
        section_route_entry,
        "aa_right_joint_12",
        "item_no",
        12,
    )
    right_valve_1_item_no = resolve_section_route_node_value(
        section_route_entry,
        "aa_right_valve_1",
        "item_no",
        1,
    )
    right_gauge_10_item_no = resolve_section_route_node_value(
        section_route_entry,
        "aa_right_gauge_10",
        "item_no",
        10,
    )
    right_item26_item_no = resolve_section_route_node_value(
        section_route_entry,
        "aa_right_reducer_26",
        "item_no",
        26,
    )
    right_item26_large_dn = int(
        resolve_section_route_node_value(
            section_route_entry,
            "aa_right_reducer_26",
            "dn_large",
            right_branch_dn,
        )
    )
    right_item26_small_dn = int(
        resolve_section_route_node_value(
            section_route_entry,
            "aa_right_reducer_26",
            "dn_small",
            150,
        )
    )
    right_item26_length = float(
        route.get(
            "right_item26_length_mm",
            max(50.0, (PIPE_OD[right_item26_large_dn] - PIPE_OD[right_item26_small_dn]) * 1.2),
        )
    )
    right_item26_small_end_y = right_horizontal_start_y
    right_item26_large_end_y = float(route.get("right_item26_large_end_y", right_item26_small_end_y + right_item26_length))
    right_joint_12_spec = controller.DOUBLE_FLANGE_LIMIT_EXPANSION_JOINT_GBT12465.get(
        ("VSSJA-2(B2F)", right_branch_dn, 1.0)
    )
    right_joint_12_length = float(
        route.get(
            "right_joint_12_length_mm",
            right_joint_12_spec["overall_length"] if right_joint_12_spec is not None else 395.0,
        )
    )
    right_joint_12_half_length = right_joint_12_length / 2.0 if right_joint_12_length > 0 else 0.0
    right_joint_12_center_y = right_item26_large_end_y + right_joint_12_half_length
    right_valve_1_structure_length = float(
        route.get(
            "right_valve_1_structure_length_mm",
            controller.VALVE_CHECK_STRUCTURE_LENGTH.get(right_branch_dn, max(220.0, right_branch_dn * 1.9 + 250.0)),
        )
    )
    right_valve_1_half_length = (
        right_valve_1_structure_length / 2.0 if right_valve_1_structure_length > 0 else 0.0
    )
    right_valve_1_center_y = right_item26_large_end_y + right_joint_12_length + right_valve_1_half_length
    right_valve_center_y = (
        right_valve_1_center_y
        + right_valve_1_half_length
        + right_valve_half_length
    )
    right_elbow_connection_trim = resolve_lr_elbow_connection_trim_mm(
        route.get("right_elbow_connection_trim_mm"),
        right_branch_dn,
    )
    right_horizontal_gauge_mount_height = float(
        route.get("right_horizontal_gauge_mount_height_mm", max(220.0, PIPE_OD[right_branch_dn] * 0.95))
    )
    right_post_valve_horizontal_start_y = right_valve_center_y + right_valve_half_length
    right_post_valve_horizontal_end_y = right_header_y - right_elbow_connection_trim
    right_gauge_10_hint_y = resolve_section_route_position_y(
        section_route_entry,
        point_id="aa_right_gauge_10_center",
        default=right_post_valve_horizontal_start_y + max(120.0, right_elbow_connection_trim * 0.35),
    )
    if right_post_valve_horizontal_end_y - right_post_valve_horizontal_start_y > 240.0:
        right_gauge_10_center_y = min(
            max(right_gauge_10_hint_y, right_post_valve_horizontal_start_y + 120.0),
            right_post_valve_horizontal_end_y - 120.0,
        )
    else:
        right_gauge_10_center_y = (right_post_valve_horizontal_start_y + right_post_valve_horizontal_end_y) / 2.0
    pump_joint_11_center_y = resolve_section_route_position_y(
        section_route_entry,
        point_id="aa_p0204_item11_center",
        default=max(pump_valve_center_y + pump_valve_half_length + 180.0, pump_reducer_large_end_y - 240.0),
    )
    pump_joint_11_item_no = resolve_section_route_node_value(
        section_route_entry,
        "aa_p0204_joint_11",
        "item_no",
        11,
    )
    pump_joint_11_length = float(route.get("pump_joint_11_length_mm", 370.0))
    pump_joint_11_half_length = pump_joint_11_length / 2.0 if pump_joint_11_length > 0 else 0.0

    left_bend_radius = PIPE_OD[left_branch_dn] * 1.5
    pump_bend_radius = PIPE_OD[pump_vertical_dn] * 1.5

    left_tee_dims = controller._resolve_tee_center_to_end_gb12459(left_header_proxy_dn, left_branch_dn)
    if left_tee_dims is None:
        raise RuntimeError(f"{bundle_id} 左侧三通尺寸解析失败。")
    left_branch_display_offset, _ = resolve_tee_display_offsets_mm(
        run_dn=left_header_proxy_dn,
        branch_center_to_end_mm=float(left_tee_dims[1]),
        run_center_to_end_mm=float(left_tee_dims[0]),
    )
    left_branch_start_z = left_header_z + left_branch_display_offset
    right_tee_dims = controller._resolve_tee_center_to_end_gb12459(right_header_proxy_dn, right_branch_dn)
    if right_tee_dims is None:
        raise RuntimeError(f"{bundle_id} 右侧三通尺寸解析失败。")
    right_tee_center_z = right_header_z
    right_branch_display_offset, _ = resolve_tee_display_offsets_mm(
        run_dn=right_header_proxy_dn,
        branch_center_to_end_mm=float(right_tee_dims[1]),
        run_center_to_end_mm=float(right_tee_dims[0]),
    )
    right_visible_vertical_bottom_elevation = right_tee_center_z + right_branch_display_offset
    compiled_skeleton = compile_aa_connection_skeleton(
        {
            "route_id": bundle_id,
            "anchor_x": anchor_x,
            "upper_anchor_x": upper_anchor_x,
            "draw_left_lower_branch": draw_left_lower_branch,
            "draw_right_lower_branch": draw_right_lower_branch,
            "draw_right_upper_branch": draw_right_upper_branch,
            "left_header_y": left_header_y,
            "left_branch_dn": left_branch_dn,
            "left_branch_start_z": left_branch_start_z,
            "left_valve_center_elevation_mm": left_valve_center_elevation,
            "left_valve_structure_length_mm": left_valve_structure_length,
            "left_top_horizontal_elevation_mm": left_top_horizontal_elevation,
            "left_top_horizontal_end_y": left_top_horizontal_end_y,
            "pump_vertical_dn": pump_vertical_dn,
            "pump_vertical_y": pump_vertical_y,
            "pump_vertical_bottom_elevation_mm": pump_vertical_bottom_elevation,
            "pump_vertical_top_elevation_mm": pump_vertical_top_elevation,
            "pump_valve_center_y": pump_valve_center_y,
            "pump_valve_structure_length_mm": pump_valve_structure_length,
            "pump_joint_11_center_y": pump_joint_11_center_y,
            "pump_joint_11_length_mm": pump_joint_11_length,
            "pump_reducer_large_end_y": pump_reducer_large_end_y,
            "pump_reducer_small_end_y": pump_reducer_small_end_y,
            "right_header_y": right_header_y,
            "right_branch_dn": right_branch_dn,
            "right_horizontal_start_y": right_horizontal_start_y,
            "right_item26_large_end_y": right_item26_large_end_y,
            "right_joint_12_center_y": right_joint_12_center_y,
            "right_joint_12_length_mm": right_joint_12_length,
            "right_valve_1_center_y": right_valve_1_center_y,
            "right_valve_1_structure_length_mm": right_valve_1_structure_length,
            "right_valve_center_y": right_valve_center_y,
            "right_valve_structure_length_mm": right_valve_structure_length,
            "right_riser_top_elevation_mm": right_riser_top_elevation,
            "right_visible_vertical_bottom_elevation_mm": right_visible_vertical_bottom_elevation,
            "right_elbow_connection_trim_mm": right_elbow_connection_trim,
            "aa_left_flow_sequence": route.get("aa_left_flow_sequence", []),
            "aa_pump_flow_sequence": route.get("aa_pump_flow_sequence", []),
            "aa_right_flow_sequence": route.get("aa_right_flow_sequence", []),
        }
    )

    for compiled_segment in compiled_skeleton["segments"]:
        draw_pipe_segment(
            service,
            start=[float(value) for value in compiled_segment["start"]],
            end=[float(value) for value in compiled_segment["end"]],
            dn=int(compiled_segment["dn"]),
        )
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=str(compiled_segment["segment_id"]),
            category="section_preview",
            dn=int(compiled_segment["dn"]),
            start=[float(value) for value in compiled_segment["start"]],
            end=[float(value) for value in compiled_segment["end"]],
            basis=basis,
            helper_generated=bool(compiled_segment.get("helper_generated", False)),
            path_id=str(compiled_segment.get("path_id") or ""),
            source_run_id=str(compiled_segment.get("source_run_id") or ""),
            from_node_id=(
                str(compiled_segment["from_node_id"])
                if compiled_segment.get("from_node_id") is not None
                else None
            ),
            to_node_id=(
                str(compiled_segment["to_node_id"])
                if compiled_segment.get("to_node_id") is not None
                else None
            ),
        )

    if draw_left_lower_branch:
        left_helper_spec = compiled_skeleton["helper_specs"]["left_top_elbow"]
        left_vertical_spec = left_helper_spec["run_specs"][0]
        left_horizontal_spec = left_helper_spec["run_specs"][1]
        if float(left_vertical_spec.length_mm) > 1e-6 and float(left_horizontal_spec.length_mm) > 1e-6:
            left_top_elbow_geometry = build_orthogonal_elbow_connection(
                controller,
                dn=left_helper_spec["dn"],
                elbow_center=tuple(left_helper_spec["elbow_center"]),
                elbow_type="LR",
                run_specs=left_helper_spec["run_specs"],
                elbow_rotations=[
                    RotationInstruction(
                        angle_deg=float(route.get("left_top_elbow_rotation_deg", -90.0)),
                        axis_direction=resolve_rotation_axis(route.get("left_top_elbow_rotation_axis", "y")),
                        axis_name=str(route.get("left_top_elbow_rotation_axis", "y")),
                    ),
                    RotationInstruction(
                        angle_deg=float(route.get("left_top_elbow_secondary_rotation_deg", 180.0)),
                        axis_direction=resolve_rotation_axis(route.get("left_top_elbow_secondary_rotation_axis", "z")),
                        axis_name=str(route.get("left_top_elbow_secondary_rotation_axis", "z")),
                    ),
                ],
                layer=f"PIPE_DN{left_branch_dn}",
                color=WHITE,
                draw_runs=False,
            )
            left_helper_runs = {run["run_id"]: run for run in left_top_elbow_geometry["runs"]}
            left_vertical_tangent = [
                float(value)
                for value in left_helper_runs[left_vertical_spec.run_id]["tangent_point_mm"]
            ]
            left_horizontal_tangent = [
                float(value)
                for value in left_helper_runs[left_horizontal_spec.run_id]["tangent_point_mm"]
            ]
            if left_vertical_tangent[2] - left_helper_spec["requested_vertical_start"][2] > 1e-6:
                draw_pipe_segment(
                    service,
                    start=left_helper_spec["requested_vertical_start"],
                    end=left_vertical_tangent,
                    dn=left_branch_dn,
                )
                append_route_segment_record(
                    route_records,
                    bundle_id=bundle_id,
                    segment_id=str(left_helper_spec["segment_ids"]["vertical"]),
                    category="section_preview",
                    dn=left_branch_dn,
                    start=left_helper_spec["requested_vertical_start"],
                    end=left_vertical_tangent,
                    basis=basis,
                    helper_generated=True,
                    path_id="aa_left_side_chain",
                    source_run_id="aa_left_post_valve_riser_dn250",
                    from_node_id="aa_left_valve_4",
                    to_node_id="aa_left_top_elbow_dn250",
                )
            if left_helper_spec["requested_horizontal_end"][1] - left_horizontal_tangent[1] > 1e-6:
                draw_pipe_segment(
                    service,
                    start=left_horizontal_tangent,
                    end=left_helper_spec["requested_horizontal_end"],
                    dn=left_branch_dn,
                )
                append_route_segment_record(
                    route_records,
                    bundle_id=bundle_id,
                    segment_id=str(left_helper_spec["segment_ids"]["horizontal"]),
                    category="section_preview",
                    dn=left_branch_dn,
                    start=left_horizontal_tangent,
                    end=left_helper_spec["requested_horizontal_end"],
                    basis=basis,
                    helper_generated=True,
                    path_id="aa_left_side_chain",
                    source_run_id="aa_left_top_horizontal_dn250",
                    from_node_id="aa_left_top_elbow_dn250",
                    to_node_id=None,
                )

    pump_helper_spec = compiled_skeleton["helper_specs"]["pump_top_elbow"]
    pump_vertical_spec = pump_helper_spec["run_specs"][0]
    pump_horizontal_spec = pump_helper_spec["run_specs"][1]
    if float(pump_vertical_spec.length_mm) > 1e-6 and float(pump_horizontal_spec.length_mm) > 1e-6:
        pump_elbow_geometry = build_orthogonal_elbow_connection(
            controller,
            dn=pump_helper_spec["dn"],
            elbow_center=tuple(pump_helper_spec["elbow_center"]),
            elbow_type="LR",
            run_specs=pump_helper_spec["run_specs"],
            elbow_rotations=[
                RotationInstruction(
                    angle_deg=float(route.get("pump_top_elbow_rotation_deg", -90.0)),
                    axis_direction=resolve_rotation_axis(route.get("pump_top_elbow_rotation_axis", "y")),
                    axis_name=str(route.get("pump_top_elbow_rotation_axis", "y")),
                ),
                RotationInstruction(
                    angle_deg=float(route.get("pump_top_elbow_secondary_rotation_deg", 180.0)),
                    axis_direction=resolve_rotation_axis(route.get("pump_top_elbow_secondary_rotation_axis", "z")),
                    axis_name=str(route.get("pump_top_elbow_secondary_rotation_axis", "z")),
                ),
            ],
            layer=f"PIPE_DN{pump_vertical_dn}",
            color=WHITE,
            draw_runs=False,
        )
        pump_helper_runs = {run["run_id"]: run for run in pump_elbow_geometry["runs"]}
        pump_vertical_tangent = [
            float(value)
            for value in pump_helper_runs[pump_vertical_spec.run_id]["tangent_point_mm"]
        ]
        pump_horizontal_tangent = [
            float(value)
            for value in pump_helper_runs[pump_horizontal_spec.run_id]["tangent_point_mm"]
        ]
        if pump_vertical_tangent[2] - pump_helper_spec["requested_vertical_start"][2] > 1e-6:
            draw_pipe_segment(
                service,
                start=pump_helper_spec["requested_vertical_start"],
                end=pump_vertical_tangent,
                dn=pump_vertical_dn,
            )
            append_route_segment_record(
                route_records,
                bundle_id=bundle_id,
                segment_id=str(pump_helper_spec["segment_ids"]["vertical"]),
                category="section_preview",
                dn=pump_vertical_dn,
                start=pump_helper_spec["requested_vertical_start"],
                end=pump_vertical_tangent,
                basis=basis,
                helper_generated=True,
                path_id="aa_p0204_left_chain",
                source_run_id="aa_p0204_riser_dn300",
                from_node_id=None,
                to_node_id="aa_p0204_elbow_19",
            )
        if pump_helper_spec["requested_horizontal_end"][1] - pump_horizontal_tangent[1] > 1e-6:
            draw_pipe_segment(
                service,
                start=pump_horizontal_tangent,
                end=pump_helper_spec["requested_horizontal_end"],
                dn=pump_vertical_dn,
            )
            append_route_segment_record(
                route_records,
                bundle_id=bundle_id,
                segment_id=str(pump_helper_spec["segment_ids"]["horizontal"]),
                category="section_preview",
                dn=pump_vertical_dn,
                start=pump_horizontal_tangent,
                end=pump_helper_spec["requested_horizontal_end"],
                basis=basis,
                helper_generated=True,
                path_id="aa_p0204_left_chain",
                source_run_id="aa_p0204_horizontal_dn300",
                from_node_id="aa_p0204_elbow_19",
                to_node_id="aa_p0204_valve_3",
            )

    right_helper_spec = compiled_skeleton["helper_specs"]["right_return_elbow"]
    right_vertical_spec = right_helper_spec["run_specs"][1]
    right_horizontal_spec = right_helper_spec["run_specs"][0]
    if float(right_vertical_spec.length_mm) > 1e-6 and float(right_horizontal_spec.length_mm) > 1e-6:
        right_elbow_geometry = build_orthogonal_elbow_connection(
            controller,
            dn=right_helper_spec["dn"],
            elbow_center=tuple(right_helper_spec["elbow_center"]),
            elbow_type="LR",
            run_specs=right_helper_spec["run_specs"],
            elbow_rotations=[
                RotationInstruction(
                    angle_deg=float(route.get("right_elbow_rotation_deg", -90.0)),
                    axis_direction=resolve_rotation_axis(route.get("right_elbow_rotation_axis", "y")),
                    axis_name=str(route.get("right_elbow_rotation_axis", "y")),
                ),
                RotationInstruction(
                    angle_deg=float(route.get("right_elbow_secondary_rotation_deg", 0.0)),
                    axis_direction=resolve_rotation_axis(route.get("right_elbow_secondary_rotation_axis", "x")),
                    axis_name=str(route.get("right_elbow_secondary_rotation_axis", "x")),
                ),
            ],
            layer=f"PIPE_DN{right_branch_dn}",
            color=WHITE,
            draw_runs=False,
        )
        right_helper_runs = {run["run_id"]: run for run in right_elbow_geometry["runs"]}
        right_vertical_tangent = [
            float(value)
            for value in right_helper_runs[right_vertical_spec.run_id]["tangent_point_mm"]
        ]
        right_horizontal_tangent = [
            float(value)
            for value in right_helper_runs[right_horizontal_spec.run_id]["tangent_point_mm"]
        ]
        if draw_right_upper_branch and right_horizontal_tangent[1] - right_helper_spec["requested_horizontal_start"][1] > 1e-6:
            draw_pipe_segment(
                service,
                start=right_helper_spec["requested_horizontal_start"],
                end=right_horizontal_tangent,
                dn=right_branch_dn,
            )
            append_route_segment_record(
                route_records,
                bundle_id=bundle_id,
                segment_id=str(right_helper_spec["segment_ids"]["horizontal"]),
                category="section_preview",
                dn=right_branch_dn,
                start=right_helper_spec["requested_horizontal_start"],
                end=right_horizontal_tangent,
                basis=basis,
                helper_generated=True,
                path_id="aa_right_return_chain",
                source_run_id="aa_right_horizontal_dn250",
                from_node_id="aa_right_valve_8",
                to_node_id="aa_right_elbow_20",
            )
    if not bool(route.get("draw_fittings", False)):
        return

    support_center_tuple = tuple(float(value) for value in support_center)
    support_entity = call_controller_with_retry(
        f"{bundle_id} support 29",
        lambda: controller.draw_suction_bellmouth_support(
            center=support_center_tuple,
            support_model="ZB3",
            bellmouth_form="J",
            support_type="B",
            axis="-z",
            layer="SUPPORT",
            color=WHITE,
        ),
    )
    if support_entity is None:
        raise RuntimeError(f"{bundle_id} 的 29 号支架绘制失败。")
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_support_29",
        category="section_preview_fitting",
        kind="suction_bellmouth_support",
        item_no=route.get("pump_support_item_no"),
        center=list(support_center_tuple),
        handle=extract_entity_handle(support_entity),
        basis=basis,
    )

    blind_flange_entity = call_controller_with_retry(
        f"{bundle_id} blind flange 27",
        lambda: controller.draw_blind_flange(
            center=(upper_anchor_x, pump_vertical_y, pump_blind_flange_base_z),
            specification="GB/T 9119",
            dn=pump_vertical_dn,
            pn=16.0,
            layer=f"PIPE_DN{pump_vertical_dn}",
            color=WHITE,
        ),
    )
    if blind_flange_entity is not None:
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_blind_flange_27",
            category="section_preview_fitting",
            kind="blind_flange",
            item_no=27,
        center=[upper_anchor_x, pump_vertical_y, pump_blind_flange_center_z],
            handle=extract_entity_handle(blind_flange_entity),
            basis=basis,
        )

    if draw_left_lower_branch:
        left_tee_entity = call_controller_with_retry(
            f"{bundle_id} left tee",
            lambda: controller.draw_tee(
                center=(anchor_x, left_header_y, left_header_z),
                dn=left_header_proxy_dn,
                branch_dn=left_branch_dn,
                run_axis="x",
                branch_axis="z",
                layer=f"PIPE_DN{left_branch_dn}",
                color=WHITE,
            ),
        )
        if left_tee_entity is None:
            raise RuntimeError(f"{bundle_id} 左侧 13 号异径三通绘制失败。")
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_left_tee",
            category="section_preview_fitting",
            kind="reducing_tee",
            item_no=route.get("left_header_item_no"),
            center=[anchor_x, left_header_y, left_header_z],
            handle=extract_entity_handle(left_tee_entity),
            basis=basis,
        )

        left_requested_pn = float(route.get("left_valve_pn", 10.0))
        left_resolved_pn = left_requested_pn if controller._get_flange_params(left_branch_dn, left_requested_pn) else 16.0
        left_valve_entity = call_controller_with_retry(
            f"{bundle_id} left valve",
            lambda: controller.draw_butterfly_valve(
                center=(anchor_x, left_header_y, left_valve_center_elevation),
                dn=left_branch_dn,
                pn=left_resolved_pn,
                axis="z",
                layer=f"PIPE_DN{left_branch_dn}",
                color=WHITE,
            ),
        )
        if left_valve_entity is None:
            raise RuntimeError(f"{bundle_id} 左侧 4 号蝶阀绘制失败。")
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_left_valve",
            category="section_preview_fitting",
            kind="butterfly_valve",
            item_no=route.get("left_valve_item_no"),
            center=[anchor_x, left_header_y, left_valve_center_elevation],
            handle=extract_entity_handle(left_valve_entity),
            basis=basis,
        )

        left_top_elbow_center = [anchor_x, left_header_y, left_top_horizontal_elevation]
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_left_top_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=route.get("left_top_elbow_item_no"),
            center=left_top_elbow_center,
            handle=None,
            basis=basis,
            helper_generated=True,
        )

    pump_elbow_center = [upper_anchor_x, pump_vertical_y, pump_vertical_top_elevation]
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_pump_elbow",
        category="section_preview_fitting",
        kind="elbow",
        item_no=route.get("pump_top_elbow_item_no"),
        center=pump_elbow_center,
        handle=None,
        basis=basis,
        helper_generated=True,
    )

    sleeve_30_entity = call_controller_with_retry(
        f"{bundle_id} rigid sleeve 30",
        lambda: controller.draw_rigid_waterproof_sleeve(
            center=(upper_anchor_x, pump_sleeve_30_center_y, pump_vertical_top_elevation),
            dn=pump_vertical_dn,
            axis="y",
            sleeve_type="A",
            layer=f"PIPE_DN{pump_vertical_dn}",
            color=WHITE,
        ),
    )
    if sleeve_30_entity is not None:
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_rigid_sleeve_30",
            category="section_preview_fitting",
            kind="rigid_waterproof_sleeve",
            item_no=30,
            center=[upper_anchor_x, pump_sleeve_30_center_y, pump_vertical_top_elevation],
            handle=extract_entity_handle(sleeve_30_entity),
            basis=basis,
        )

    pump_requested_pn = float(route.get("pump_valve_pn", 10.0))
    pump_resolved_pn = pump_requested_pn if controller._get_flange_params(pump_vertical_dn, pump_requested_pn) else 16.0
    pump_valve_entity = call_controller_with_retry(
        f"{bundle_id} pump valve",
        lambda: controller.draw_butterfly_valve(
            center=(upper_anchor_x, pump_valve_center_y, pump_vertical_top_elevation),
            dn=pump_vertical_dn,
            pn=pump_resolved_pn,
            axis="y",
            layer=f"PIPE_DN{pump_vertical_dn}",
            color=WHITE,
        ),
    )
    if pump_valve_entity is None:
        raise RuntimeError(f"{bundle_id} 的 3 号蝶阀绘制失败。")
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_pump_valve",
        category="section_preview_fitting",
        kind="butterfly_valve",
        item_no=route.get("pump_valve_item_no"),
        center=[upper_anchor_x, pump_valve_center_y, pump_vertical_top_elevation],
        handle=extract_entity_handle(pump_valve_entity),
        basis=basis,
    )

    pump_joint_11_entity = call_controller_with_retry(
        f"{bundle_id} pump joint 11",
        lambda: controller.draw_double_flange_limit_expansion_joint(
            center=(upper_anchor_x, pump_joint_11_center_y, pump_vertical_top_elevation),
            dn=pump_vertical_dn,
            model="JSSJA-2",
            pn=1.0,
            axis="y",
            layer=f"PIPE_DN{pump_vertical_dn}",
            color=WHITE,
        ),
    )
    if pump_joint_11_entity is None:
        raise RuntimeError(f"{bundle_id} 的 11 号伸缩接头绘制失败。")
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_pump_joint_11",
        category="section_preview_fitting",
        kind="double_flange_limit_expansion_joint",
        item_no=pump_joint_11_item_no,
        center=[upper_anchor_x, pump_joint_11_center_y, pump_vertical_top_elevation],
        handle=extract_entity_handle(pump_joint_11_entity),
        basis=basis,
    )

    reducer_entity = call_controller_with_retry(
        f"{bundle_id} pump reducer",
        lambda: controller.draw_reducer(
            center=(upper_anchor_x, pump_reducer_large_end_y, pump_vertical_top_elevation),
            dn_large=pump_reducer_large_dn,
            dn_small=pump_reducer_small_dn,
            axis="y",
            reducer_type="concentric",
            length=pump_reducer_length,
            layer=f"PIPE_DN{pump_reducer_large_dn}",
            color=WHITE,
        ),
    )
    if reducer_entity is None:
        raise RuntimeError(f"{bundle_id} 的 25 号异径管绘制失败。")
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_pump_reducer",
        category="section_preview_fitting",
        kind="reducer",
        item_no=route.get("pump_reducer_item_no"),
        center=[upper_anchor_x, pump_reducer_large_end_y, pump_vertical_top_elevation],
        handle=extract_entity_handle(reducer_entity),
        basis=basis,
    )

    if draw_right_upper_branch:
        right_item26_entity = call_controller_with_retry(
            f"{bundle_id} right reducer 26",
            lambda: controller.draw_reducer(
                center=(right_upper_anchor_x, right_item26_large_end_y, right_riser_top_elevation),
                dn_large=right_item26_large_dn,
                dn_small=right_item26_small_dn,
                axis="-y",
                reducer_type="concentric",
                length=right_item26_length,
                layer=f"PIPE_DN{right_branch_dn}",
                color=WHITE,
            ),
        )
        if right_item26_entity is None:
            raise RuntimeError(f"{bundle_id} 的 26 号异径管绘制失败。")
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_right_reducer_26",
            category="section_preview_fitting",
            kind="reducer",
            item_no=right_item26_item_no,
            center=[right_upper_anchor_x, right_item26_large_end_y, right_riser_top_elevation],
            handle=extract_entity_handle(right_item26_entity),
            basis=basis,
        )

        right_joint_12_entity = call_controller_with_retry(
            f"{bundle_id} right joint 12",
            lambda: controller.draw_double_flange_limit_expansion_joint(
                center=(right_upper_anchor_x, right_joint_12_center_y, right_riser_top_elevation),
                dn=right_branch_dn,
                model="JSSJA-2",
                pn=1.0,
                axis="y",
                layer=f"PIPE_DN{right_branch_dn}",
                color=WHITE,
            ),
        )
        if right_joint_12_entity is None:
            raise RuntimeError(f"{bundle_id} 的 12 号伸缩接头绘制失败。")
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_right_joint_12",
            category="section_preview_fitting",
            kind="double_flange_limit_expansion_joint",
            item_no=right_joint_12_item_no,
            center=[right_upper_anchor_x, right_joint_12_center_y, right_riser_top_elevation],
            handle=extract_entity_handle(right_joint_12_entity),
            basis=basis,
        )

        right_valve_1_requested_pn = float(route.get("right_valve_1_pn", route.get("right_valve_pn", 10.0)))
        right_valve_1_resolved_pn = (
            right_valve_1_requested_pn
            if controller._get_flange_params(right_branch_dn, right_valve_1_requested_pn)
            else 16.0
        )
        right_valve_1_entity = call_controller_with_retry(
            f"{bundle_id} right valve 1",
            lambda: controller.draw_check_valve(
                center=(right_upper_anchor_x, right_valve_1_center_y, right_riser_top_elevation),
                dn=right_branch_dn,
                pn=right_valve_1_resolved_pn,
                axis="y",
                layer=f"PIPE_DN{right_branch_dn}",
                color=WHITE,
            ),
            retries=8,
            wait_seconds=1.0,
        )
        if right_valve_1_entity is None:
            raise RuntimeError(f"{bundle_id} 的 1 号止回阀绘制失败。")
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_right_valve_1",
            category="section_preview_fitting",
            kind="check_valve",
            item_no=right_valve_1_item_no,
            center=[right_upper_anchor_x, right_valve_1_center_y, right_riser_top_elevation],
            handle=extract_entity_handle(right_valve_1_entity),
            basis=basis,
        )

        right_requested_pn = float(route.get("right_valve_pn", 10.0))
        right_resolved_pn = right_requested_pn if controller._get_flange_params(right_branch_dn, right_requested_pn) else 16.0
        right_valve_entity = call_controller_with_retry(
            f"{bundle_id} right valve",
            lambda: controller.draw_butterfly_valve(
                center=(right_upper_anchor_x, right_valve_center_y, right_riser_top_elevation),
                dn=right_branch_dn,
                pn=right_resolved_pn,
                axis="y",
                layer=f"PIPE_DN{right_branch_dn}",
                color=WHITE,
            ),
        )
        if right_valve_entity is None:
            raise RuntimeError(f"{bundle_id} 的 8 号蝶阀绘制失败。")
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_right_valve",
            category="section_preview_fitting",
            kind="butterfly_valve",
            item_no=route.get("right_valve_item_no"),
            center=[right_upper_anchor_x, right_valve_center_y, right_riser_top_elevation],
            handle=extract_entity_handle(right_valve_entity),
            basis=basis,
        )

        right_gauge_10_entity = call_controller_with_retry(
            f"{bundle_id} right gauge 10",
            lambda: controller.draw_pressure_gauge(
                center=(
                    right_upper_anchor_x,
                    right_gauge_10_center_y,
                    right_riser_top_elevation + right_horizontal_gauge_mount_height,
                ),
                nominal_diameter=100,
                style="radial",
                mount_type="direct",
                face_axis="x",
                layer="INSTRUMENT",
                color=WHITE,
            ),
        )
        if right_gauge_10_entity is not None:
            append_route_fitting_record(
                route_records,
                bundle_id=bundle_id,
                fitting_id=f"{bundle_id}_right_gauge_10",
                category="section_preview_fitting",
                kind="pressure_gauge",
                item_no=right_gauge_10_item_no,
                center=[
                    right_upper_anchor_x,
                    right_gauge_10_center_y,
                    right_riser_top_elevation + right_horizontal_gauge_mount_height,
                ],
                handle=extract_entity_handle(right_gauge_10_entity),
                basis=basis,
            )

        right_elbow_center = [right_upper_anchor_x, right_header_y, right_riser_top_elevation]
        right_elbow_handle = draw_cycle_water_lr_elbow(
            service,
            description=f"{bundle_id} right elbow",
            corner_point=right_elbow_center,
            dn=right_branch_dn,
            layer=f"PIPE_DN{right_branch_dn}",
            primary_rotation_deg=float(route.get("right_elbow_rotation_deg", -90.0)),
            primary_rotation_axis=route.get("right_elbow_rotation_axis", "y"),
        )
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_right_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=route.get("right_elbow_item_no"),
            center=right_elbow_center,
            handle=right_elbow_handle,
            basis=basis,
        )

    if draw_right_lower_branch or draw_right_upper_branch:
        right_tee_entity = call_controller_with_retry(
            f"{bundle_id} right tee",
            lambda: controller.draw_tee(
                center=(right_upper_anchor_x, right_header_y, right_tee_center_z),
                dn=right_header_proxy_dn,
                branch_dn=right_branch_dn,
                run_axis="x",
                branch_axis="z",
                layer=f"PIPE_DN{right_branch_dn}",
                color=WHITE,
            ),
        )
        if right_tee_entity is None:
            raise RuntimeError(f"{bundle_id} 右侧 14 号异径三通绘制失败。")
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_right_tee",
            category="section_preview_fitting",
            kind="reducing_tee",
            item_no=route.get("right_header_item_no"),
            center=[right_upper_anchor_x, right_header_y, right_tee_center_z],
            handle=extract_entity_handle(right_tee_entity),
            basis=basis,
        )


def draw_gg_section_preview_route(
    service: CADService,
    *,
    route: dict[str, Any],
    route_records: list[dict[str, Any]],
) -> None:
    """Draw the G-G local DN100/DN250 loop preview driven by the re-read section."""

    bundle_id = str(route["route_id"])
    anchor_x = float(route["anchor_x"])
    base_y = float(route["base_y"])
    base_z = float(route["base_z"])
    right_riser_dn = int(route.get("right_riser_dn", 250))
    branch_dn = int(route["dn"])
    left_vertical_y = float(route["left_vertical_y"])
    top_horizontal_elevation = float(route["top_horizontal_elevation_mm"])
    bottom_horizontal_elevation = float(route["bottom_horizontal_elevation_mm"])
    bottom_stub_length = float(route.get("bottom_stub_length_mm", 0.0))
    header_tee_branch_center_to_end = float(route.get("header_tee_branch_center_to_end_mm", 0.0))
    header_tee_branch_display_offset = float(
        route.get("header_tee_branch_display_offset_mm", header_tee_branch_center_to_end)
    )
    valve_center_y = float(route["valve_center_y"])
    valve_structure_length = float(route.get("valve_structure_length_mm", 0.0))
    valve_half_length = valve_structure_length / 2.0 if valve_structure_length > 0 else 0.0
    basis = str(route.get("basis", ""))
    controller = service.controller
    branch_layer = f"PIPE_DN{branch_dn}"
    right_riser_layer = f"PIPE_DN{right_riser_dn}"

    branch_bend_radius = PIPE_OD[branch_dn] * 1.5
    draw_bottom_stub = bool(route.get("draw_bottom_stub", True))
    resolved_riser_tee = controller._resolve_tee_center_to_end_gb12459(
        int(route.get("riser_tee_run_dn", right_riser_dn)),
        int(route.get("riser_tee_branch_dn", branch_dn)),
    )
    riser_tee_run_center_to_end = float(resolved_riser_tee[0]) if resolved_riser_tee else 0.0
    riser_tee_branch_center_to_end = float(resolved_riser_tee[1]) if resolved_riser_tee else 0.0
    riser_tee_branch_display_offset, riser_tee_run_display_half_length = resolve_tee_display_offsets_mm(
        run_dn=int(route.get("riser_tee_run_dn", right_riser_dn)),
        branch_center_to_end_mm=riser_tee_branch_center_to_end,
        run_center_to_end_mm=riser_tee_run_center_to_end,
    )

    right_riser_start = [anchor_x, base_y, base_z + header_tee_branch_display_offset]
    right_riser_end = [anchor_x, base_y, top_horizontal_elevation - riser_tee_run_display_half_length]
    if right_riser_end[2] - right_riser_start[2] > 1e-6:
        draw_pipe_segment(service, start=right_riser_start, end=right_riser_end, dn=right_riser_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_right_riser",
            category="section_preview",
            dn=right_riser_dn,
            start=right_riser_start,
            end=right_riser_end,
            basis=basis,
        )

    left_vertical_start = [
        anchor_x,
        left_vertical_y,
        bottom_horizontal_elevation + (branch_bend_radius if draw_bottom_stub else 0.0),
    ]
    left_vertical_end = [anchor_x, left_vertical_y, top_horizontal_elevation - branch_bend_radius]
    top_horizontal_left_start = [anchor_x, left_vertical_y + branch_bend_radius, top_horizontal_elevation]
    top_horizontal_left_end = [anchor_x, valve_center_y - valve_half_length, top_horizontal_elevation]

    top_horizontal_right_start = [anchor_x, valve_center_y + valve_half_length, top_horizontal_elevation]
    top_horizontal_right_end = [anchor_x, base_y - riser_tee_branch_display_offset, top_horizontal_elevation]
    if top_horizontal_right_end[1] - top_horizontal_right_start[1] > 1e-6:
        draw_pipe_segment(service, start=top_horizontal_right_start, end=top_horizontal_right_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_top_horizontal_right",
            category="section_preview",
            dn=branch_dn,
            start=top_horizontal_right_start,
            end=top_horizontal_right_end,
            basis=basis,
        )

    bottom_stub_start = [anchor_x, left_vertical_y - branch_bend_radius - bottom_stub_length, bottom_horizontal_elevation]
    bottom_stub_end = [anchor_x, left_vertical_y - branch_bend_radius, bottom_horizontal_elevation]
    left_vertical_length = left_vertical_end[2] - left_vertical_start[2]
    top_horizontal_left_length = top_horizontal_left_end[1] - top_horizontal_left_start[1]
    bottom_stub_length_available = bottom_stub_end[1] - bottom_stub_start[1]
    top_elbow_geometry = None
    bottom_elbow_geometry = None
    if left_vertical_length > 1e-6 and top_horizontal_left_length > 1e-6:
        top_elbow_geometry = build_orthogonal_elbow_connection(
            controller,
            dn=branch_dn,
            elbow_center=(anchor_x, left_vertical_y, top_horizontal_elevation),
            elbow_type="LR",
            run_specs=[
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_left_vertical_top",
                    tangent_point_mm=tuple(left_vertical_end),
                    axis_away_from_fitting="+z",
                    length_mm=float(left_vertical_length),
                    connection_end="bottom_end",
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_top_horizontal_left",
                    tangent_point_mm=tuple(top_horizontal_left_start),
                    axis_away_from_fitting="-y",
                    length_mm=float(top_horizontal_left_length),
                    connection_end="right_end",
                    flow_role="pipe_2",
                ),
            ],
            elbow_rotations=[
                RotationInstruction(
                    angle_deg=float(route.get("top_elbow_rotation_deg", -90.0)),
                    axis_direction=resolve_rotation_axis(route.get("top_elbow_rotation_axis", "y")),
                    axis_name=str(route.get("top_elbow_rotation_axis", "y")),
                ),
                RotationInstruction(
                    angle_deg=float(route.get("top_elbow_secondary_rotation_deg", 180.0)),
                    axis_direction=resolve_rotation_axis(route.get("top_elbow_secondary_rotation_axis", "z")),
                    axis_name=str(route.get("top_elbow_secondary_rotation_axis", "z")),
                ),
            ],
            layer=branch_layer,
            color=WHITE,
            draw_runs=False,
        )
    if draw_bottom_stub and left_vertical_length > 1e-6 and bottom_stub_length_available > 1e-6:
        bottom_elbow_geometry = build_orthogonal_elbow_connection(
            controller,
            dn=branch_dn,
            elbow_center=(anchor_x, left_vertical_y, bottom_horizontal_elevation),
            elbow_type="LR",
            run_specs=[
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_left_vertical_bottom",
                    tangent_point_mm=tuple(left_vertical_start),
                    axis_away_from_fitting="-z",
                    length_mm=float(left_vertical_length),
                    connection_end="top_end",
                    flow_role="pipe_1",
                ),
                PipeConnectionRunSpec(
                    run_id=f"{bundle_id}_bottom_stub",
                    tangent_point_mm=tuple(bottom_stub_end),
                    axis_away_from_fitting="+y",
                    length_mm=float(bottom_stub_length_available),
                    connection_end="left_end",
                    flow_role="pipe_2",
                ),
            ],
            elbow_rotations=[
                RotationInstruction(
                    angle_deg=float(route.get("bottom_elbow_rotation_deg", 90.0)),
                    axis_direction=resolve_rotation_axis(route.get("bottom_elbow_rotation_axis", "y")),
                    axis_name=str(route.get("bottom_elbow_rotation_axis", "y")),
                ),
                RotationInstruction(
                    angle_deg=float(route.get("bottom_elbow_secondary_rotation_deg", 0.0)),
                    axis_direction=resolve_rotation_axis(route.get("bottom_elbow_secondary_rotation_axis", "x")),
                    axis_name=str(route.get("bottom_elbow_secondary_rotation_axis", "x")),
                ),
            ],
            layer=branch_layer,
            color=WHITE,
            draw_runs=False,
        )
    if top_elbow_geometry is not None:
        top_helper_runs = {run["run_id"]: run for run in top_elbow_geometry["runs"]}
        left_vertical_top_tangent = [float(value) for value in top_helper_runs[f"{bundle_id}_left_vertical_top"]["tangent_point_mm"]]
        top_horizontal_left_tangent = [float(value) for value in top_helper_runs[f"{bundle_id}_top_horizontal_left"]["tangent_point_mm"]]
    else:
        left_vertical_top_tangent = left_vertical_end
        top_horizontal_left_tangent = top_horizontal_left_start
    if bottom_elbow_geometry is not None:
        bottom_helper_runs = {run["run_id"]: run for run in bottom_elbow_geometry["runs"]}
        left_vertical_bottom_tangent = [
            float(value) for value in bottom_helper_runs[f"{bundle_id}_left_vertical_bottom"]["tangent_point_mm"]
        ]
        bottom_stub_tangent = [float(value) for value in bottom_helper_runs[f"{bundle_id}_bottom_stub"]["tangent_point_mm"]]
    else:
        left_vertical_bottom_tangent = left_vertical_start
        bottom_stub_tangent = bottom_stub_end
    if left_vertical_top_tangent[2] - left_vertical_bottom_tangent[2] > 1e-6:
        draw_pipe_segment(service, start=left_vertical_bottom_tangent, end=left_vertical_top_tangent, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_left_vertical",
            category="section_preview",
            dn=branch_dn,
            start=left_vertical_bottom_tangent,
            end=left_vertical_top_tangent,
            basis=basis,
            helper_generated=top_elbow_geometry is not None or bottom_elbow_geometry is not None,
        )
    if top_horizontal_left_end[1] - top_horizontal_left_tangent[1] > 1e-6:
        draw_pipe_segment(service, start=top_horizontal_left_tangent, end=top_horizontal_left_end, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_top_horizontal_left",
            category="section_preview",
            dn=branch_dn,
            start=top_horizontal_left_tangent,
            end=top_horizontal_left_end,
            basis=basis,
            helper_generated=top_elbow_geometry is not None,
        )
    if draw_bottom_stub and bottom_stub_tangent[1] - bottom_stub_start[1] > 1e-6:
        draw_pipe_segment(service, start=bottom_stub_start, end=bottom_stub_tangent, dn=branch_dn)
        append_route_segment_record(
            route_records,
            bundle_id=bundle_id,
            segment_id=f"{bundle_id}_bottom_stub",
            category="section_preview",
            dn=branch_dn,
            start=bottom_stub_start,
            end=bottom_stub_tangent,
            basis=basis,
            helper_generated=bottom_elbow_geometry is not None,
        )

    if not bool(route.get("draw_fittings", False)):
        return

    branch_layer = f"PIPE_DN{branch_dn}"
    right_riser_layer = f"PIPE_DN{right_riser_dn}"
    header_tee_entity = call_controller_with_retry(
        f"{bundle_id} header tee",
        lambda: controller.draw_tee(
            center=(anchor_x, base_y, base_z),
            dn=int(route.get("header_tee_proxy_run_dn", route.get("base_header_dn", right_riser_dn))),
            branch_dn=right_riser_dn,
            run_axis="x",
            branch_axis="z",
            branch_center_to_end=header_tee_branch_center_to_end,
            layer=right_riser_layer,
            color=WHITE,
        ),
    )
    if header_tee_entity is None:
        raise RuntimeError(f"{bundle_id} 的 13 号异径三通绘制失败。")
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_item_{route.get('header_tee_item_no', 'header_tee')}",
        category="section_preview_fitting",
        kind="reducing_tee",
        item_no=route.get("header_tee_item_no"),
        center=[anchor_x, base_y, base_z],
        handle=extract_entity_handle(header_tee_entity),
        basis=basis,
    )

    riser_tee_entity = call_controller_with_retry(
        f"{bundle_id} riser tee",
        lambda: controller.draw_tee(
            center=(anchor_x, base_y, top_horizontal_elevation),
            dn=int(route.get("riser_tee_run_dn", right_riser_dn)),
            branch_dn=int(route.get("riser_tee_branch_dn", branch_dn)),
            run_axis="z",
            branch_axis="-y",
            run_center_to_end=riser_tee_run_center_to_end,
            branch_center_to_end=riser_tee_branch_center_to_end,
            layer=right_riser_layer,
            color=WHITE,
        ),
    )
    if riser_tee_entity is None:
        raise RuntimeError(f"{bundle_id} 的 17 号异径三通绘制失败。")
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_item_{route.get('riser_tee_item_no', 'riser_tee')}",
        category="section_preview_fitting",
        kind="reducing_tee",
        item_no=route.get("riser_tee_item_no"),
        center=[anchor_x, base_y, top_horizontal_elevation],
        handle=extract_entity_handle(riser_tee_entity),
        basis=basis,
    )

    requested_valve_pn = float(route.get("valve_pn", 10.0))
    resolved_valve_pn = requested_valve_pn
    if controller._get_flange_params(branch_dn, requested_valve_pn) is None:
        fallback_valve_pn = 16.0
        if controller._get_flange_params(branch_dn, fallback_valve_pn) is None:
            raise RuntimeError(f"{bundle_id} 的 7 号蝶阀缺少可用法兰规格: DN{branch_dn} PN{requested_valve_pn}")
        resolved_valve_pn = fallback_valve_pn
    valve_entity = call_controller_with_retry(
        f"{bundle_id} branch valve",
        lambda: controller.draw_butterfly_valve(
            center=(anchor_x, valve_center_y, top_horizontal_elevation),
            dn=branch_dn,
            pn=resolved_valve_pn,
            axis="y",
            layer=branch_layer,
            color=WHITE,
        ),
    )
    if valve_entity is None:
        raise RuntimeError(f"{bundle_id} 的 7 号蝶阀绘制失败。")
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_item_{route.get('valve_item_no', 'valve')}",
        category="section_preview_fitting",
        kind="butterfly_valve",
        item_no=route.get("valve_item_no"),
        center=[anchor_x, valve_center_y, top_horizontal_elevation],
        handle=extract_entity_handle(valve_entity),
        basis=basis,
    )

    top_elbow_center = [anchor_x, left_vertical_y, top_horizontal_elevation]
    append_route_fitting_record(
        route_records,
        bundle_id=bundle_id,
        fitting_id=f"{bundle_id}_top_elbow",
        category="section_preview_fitting",
        kind="elbow",
        item_no=route.get("elbow_item_no"),
        center=top_elbow_center,
        handle=None,
        basis=basis,
        helper_generated=True,
    )

    if draw_bottom_stub:
        bottom_elbow_center = [anchor_x, left_vertical_y, bottom_horizontal_elevation]
        append_route_fitting_record(
            route_records,
            bundle_id=bundle_id,
            fitting_id=f"{bundle_id}_bottom_elbow",
            category="section_preview_fitting",
            kind="elbow",
            item_no=route.get("elbow_item_no"),
            center=bottom_elbow_center,
            handle=None,
            basis=basis,
            helper_generated=True,
        )


def draw_support_proxy(service: CADService, proxy: dict[str, Any]) -> bool:
    """Draw one support proxy box and report whether it succeeded."""

    center = tuple(float(value) for value in proxy["center"])
    size_mm = [float(value) for value in proxy["size_mm"]]
    entity = draw_box_with_retry(
        service.controller,
        center=center,
        length=size_mm[0],
        width=size_mm[1],
        height=size_mm[2],
        layer="SUPPORT",
        color=WHITE,
    )
    if entity is None:
        print(f"支架代理创建失败，已跳过: {proxy.get('support_id', '<unknown>')}")
        return False
    return True


def draw_segment_bundle(
    service: CADService,
    *,
    bundle_id: str,
    dn: int,
    segments: list[dict[str, Any]],
    route_records: list[dict[str, Any]],
    category: str,
    basis: str,
) -> None:
    """Draw every segment in one logical route bundle."""

    for index, segment in enumerate(segments, start=1):
        start = [float(value) for value in segment["start"]]
        end = [float(value) for value in segment["end"]]
        draw_pipe_segment(service, start=start, end=end, dn=dn)
        route_records.append(
            {
                "record_type": "segment",
                "bundle_id": bundle_id,
                "segment_id": f"{bundle_id}_segment_{index}",
                "category": category,
                "dn": dn,
                "start": start,
                "end": end,
                "basis": basis,
            }
        )


def draw_pipeline_layout_v2(
    service: CADService,
    dimension_info: dict[str, Any],
    section_info: dict[str, Any],
    section_route_connection_info: dict[str, Any],
    basin_geometry: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Draw the pipeline layout compiled from V31 equipment anchors."""

    layout = build_dynamic_pipeline_layout(dimension_info, section_info, section_route_connection_info, basin_geometry)

    route_records: list[dict[str, Any]] = []
    counts = {
        "headers": 0,
        "pump_branches": 0,
        "tower_drops": 0,
        "section_preview_routes": 0,
        "auxiliary_routes": 0,
        "support_proxies": 0,
        "pipe_segments": 0,
    }

    for header in layout.get("global_headers", []):
        draw_segment_bundle(
            service,
            bundle_id=str(header["header_id"]),
            dn=int(header["dn"]),
            segments=list(header.get("segments", [])),
            route_records=route_records,
            category="header",
            basis=str(header.get("basis", "")),
        )
        counts["headers"] += 1

    for branch in layout.get("pump_branch_routes", []):
        pump_tag = str(branch["pump_tag"])
        draw_segment_bundle(
            service,
            bundle_id=f"{pump_tag}_upper_branch",
            dn=int(branch["upper_branch_dn"]),
            segments=list(branch.get("upper_segments", [])),
            route_records=route_records,
            category="pump_upper_branch",
            basis=str(branch.get("basis", "")),
        )
        draw_segment_bundle(
            service,
            bundle_id=f"{pump_tag}_lower_branch",
            dn=int(branch["lower_branch_dn"]),
            segments=list(branch.get("lower_segments", [])),
            route_records=route_records,
            category="pump_lower_branch",
            basis=str(branch.get("basis", "")),
        )
        counts["pump_branches"] += 1

    for route in layout.get("tower_drop_routes", []):
        draw_segment_bundle(
            service,
            bundle_id=str(route["tower_tag"]),
            dn=int(route["dn"]),
            segments=list(route.get("segments", [])),
            route_records=route_records,
            category="tower_drop",
            basis=str(route.get("basis", "")),
        )
        counts["tower_drops"] += 1

    for route in layout.get("section_preview_routes", []):
        draw_section_preview_route(service, route=route, route_records=route_records)
        counts["section_preview_routes"] += 1

    for route in layout.get("auxiliary_routes", []):
        draw_segment_bundle(
            service,
            bundle_id=str(route["route_id"]),
            dn=int(route["dn"]),
            segments=list(route.get("segments", [])),
            route_records=route_records,
            category="auxiliary",
            basis=str(route.get("basis", "")),
        )
        counts["auxiliary_routes"] += 1

    for proxy in layout.get("support_proxies_v2", []):
        if draw_support_proxy(service, proxy):
            counts["support_proxies"] += 1

    counts["pipe_segments"] = sum(1 for item in route_records if item.get("record_type", "segment") == "segment")
    return route_records, counts


def build_model(
    service: CADService,
    list_info: dict[str, Any],
    dimension_info: dict[str, Any],
    section_info: dict[str, Any],
    section_route_connection_info: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int], dict[str, int], dict[str, Any]]:
    """Build the pipeline model from the refreshed shared JSON inputs."""

    _ = list_info
    dimension_entries = flatten_dimension_entries(dimension_info)
    equipment_counts, basin_geometry = build_equipment_model(service, dimension_info, section_info)
    route_records, pipeline_counts = draw_pipeline_layout_v2(
        service,
        dimension_info,
        section_info,
        section_route_connection_info,
        basin_geometry,
    )
    zoom_extents_with_retry(service.controller)
    return route_records, dimension_entries, equipment_counts, pipeline_counts, basin_geometry


def collect_used_sections(section_info: dict[str, Any]) -> set[str]:
    """Collect section names referenced by pipeline_profiles_v2."""

    used_sections: set[str] = set()
    profiles = section_info.get("pipeline_profiles_v2", {})
    for group_name in ("global_profiles", "section_only_profiles"):
        for profile in profiles.get(group_name, []):
            for section_name in profile.get("source_sections", []):
                used_sections.add(str(section_name))
    return used_sections


def build_closure_report(
    list_info: dict[str, Any],
    dimension_info: dict[str, Any],
    section_info: dict[str, Any],
    section_route_connection_info: dict[str, Any],
    route_records: list[dict[str, Any]],
    dimension_entries: list[dict[str, Any]],
    equipment_counts: dict[str, int],
    pipeline_counts: dict[str, int],
    basin_geometry: dict[str, Any],
) -> dict[str, Any]:
    """Build closure checks against the refreshed pipeline inputs."""

    layout = build_dynamic_pipeline_layout(dimension_info, section_info, section_route_connection_info, basin_geometry)
    expected_summary = {
        "headers": len(layout.get("global_headers", [])),
        "pump_branches": len(layout.get("pump_branch_routes", [])),
        "tower_drops": len(layout.get("tower_drop_routes", [])),
        "section_preview_routes": len(layout.get("section_preview_routes", [])),
        "auxiliary_routes": len(layout.get("auxiliary_routes", [])),
        "support_proxies": len(layout.get("support_proxies_v2", [])),
        "pipe_segments": sum(
            len(group.get("segments", [])) for group in layout.get("global_headers", [])
        )
        + sum(len(group.get("upper_segments", [])) + len(group.get("lower_segments", [])) for group in layout.get("pump_branch_routes", []))
        + sum(len(group.get("segments", [])) for group in layout.get("tower_drop_routes", []))
        + sum(len(group.get("segments", [])) for group in layout.get("section_preview_routes", []))
        + sum(len(group.get("segments", [])) for group in layout.get("auxiliary_routes", [])),
    }

    implemented_bundle_ids = {str(item["bundle_id"]) for item in route_records}
    bom_results = []
    for item in list_info.get("bom_items", []):
        item_no = int(item["item_no"])
        model_status = str(item.get("model_status", "")).strip().lower()
        if model_status == "layout_approximated":
            closure = "drawn_or_layout_implemented"
        elif model_status in {"not_explicitly_modeled", "section_only"}:
            closure = "explicit_exception"
        else:
            closure = "missing"
        bom_results.append(
            {
                "item_no": item_no,
                "name": item["name"],
                "model_status": model_status,
                "closure_result": closure,
                "reason": item.get("modeled_as"),
            }
        )

    dimension_results = [
        {
            "label": str(entry.get("label", "")),
            "result": "loaded_from_json",
            "scope": entry.get("_ledger_scope"),
            "scope_name": entry.get("_scope_name"),
            "dimension_type": entry.get("dimension_type"),
            "raw_text": entry.get("raw_text"),
        }
        for entry in dimension_entries
    ]

    used_sections = collect_used_sections(section_info)
    section_results = []
    for item in section_info.get("sections", []):
        section_name = str(item["section"])
        section_results.append(
            {
                "section": section_name,
                "result": "used" if section_name in used_sections else "loaded_not_directly_drawn",
                "purpose": item.get("purpose"),
            }
        )

    return {
        "topic": OUTPUT_TOPIC,
        "source_pdf": list_info.get("source_pdf") or dimension_info.get("source_pdf"),
        "equipment_context": {
            "drawn": equipment_counts,
            "basin_geometry": basin_geometry,
        },
        "pipeline_context": {
            "expected": expected_summary,
            "drawn": pipeline_counts,
            "implemented_bundle_ids": sorted(implemented_bundle_ids),
        },
        "bom_closure": bom_results,
        "dimension_closure": dimension_results,
        "section_closure": section_results,
        "summary": {
            "drawn_equipment_basins": equipment_counts.get("basins", 0),
            "drawn_partition_gratings": equipment_counts.get("partition_gratings", 0),
            "drawn_cooling_towers": equipment_counts.get("cooling_towers", 0),
            "drawn_pumps": equipment_counts.get("pumps", 0),
            "drawn_wall_sleeves": equipment_counts.get("wall_sleeves", 0),
            "drawn_headers": pipeline_counts.get("headers", 0),
            "drawn_pump_branches": pipeline_counts.get("pump_branches", 0),
            "drawn_tower_drops": pipeline_counts.get("tower_drops", 0),
            "drawn_section_preview_routes": pipeline_counts.get("section_preview_routes", 0),
            "drawn_auxiliary_routes": pipeline_counts.get("auxiliary_routes", 0),
            "drawn_support_proxies": pipeline_counts.get("support_proxies", 0),
            "drawn_pipe_segments": pipeline_counts.get("pipe_segments", 0),
            "drawn_or_layout_implemented_items": sum(1 for item in bom_results if item["closure_result"] == "drawn_or_layout_implemented"),
            "explicit_exception_items": sum(1 for item in bom_results if item["closure_result"] == "explicit_exception"),
            "missing_items": sum(1 for item in bom_results if item["closure_result"] == "missing"),
            "loaded_dimensions": len(dimension_results),
            "used_sections": sum(1 for item in section_results if item["result"] == "used"),
        },
    }


def capture_previews(service: CADService, version_dir: Path, version: int) -> list[str]:
    """Capture front, top and isometric previews for validation."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    model_center = (8750.0, 5000.0, 1200.0)
    view_specs = [
        ("front", model_center, 22000.0, 28000.0, f"{OUTPUT_TOPIC}_front_preview_v{version}.png"),
        ("top", model_center, 24000.0, 32000.0, f"{OUTPUT_TOPIC}_top_preview_v{version}.png"),
        ("swiso", model_center, 28000.0, 34000.0, f"{OUTPUT_TOPIC}_iso_preview_v{version}.png"),
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


def write_version_artifacts(
    version_dir: Path,
    version: int,
    source_pdf: Path | None,
    dwg_path: Path,
    preview_paths: list[str],
    closure_report: dict[str, Any],
    dimension_entries: list[dict[str, Any]],
    section_route_connection_info: dict[str, Any],
) -> None:
    """Archive script parameters, closure report and notes."""

    parameters_path = version_dir / f"{OUTPUT_TOPIC}_parameters_v{version}.json"
    closure_path = version_dir / f"{OUTPUT_TOPIC}_closure_report_v{version}.json"
    notes_path = version_dir / f"{OUTPUT_TOPIC}_notes_v{version}.txt"
    dimension_snapshot_path = version_dir / f"{OUTPUT_TOPIC}_all_dimensions_v{version}.json"
    parameters_path.write_text(
        json.dumps(
            {
                "topic": OUTPUT_TOPIC,
                "version": version,
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
                "shared_jsons": {
                    "list_info": str(LIST_INFO_PATH),
                    "dimension_info": str(DIMENSION_INFO_PATH),
                    "section_info": str(SECTION_INFO_PATH),
                    "section_route_connection_info": str(SECTION_ROUTE_CONNECTION_INFO_PATH),
                },
                "equipment_base_reference": str(EQUIPMENT_BASE_DWG_PATH),
                "source_mode": (
                    f"equipment geometry is rebuilt from the same shared JSONs used by equipment drawing v{EQUIPMENT_BASE_VERSION}; "
                    f"pipeline geometry is recompiled from v{EQUIPMENT_BASE_VERSION} equipment anchors + pipeline_layout_v2 plan offsets + pipeline_profiles_v2 elevations"
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    closure_path.write_text(json.dumps(closure_report, ensure_ascii=False, indent=2), encoding="utf-8")
    dimension_snapshot_path.write_text(json.dumps(dimension_entries, ensure_ascii=False, indent=2), encoding="utf-8")

    analysis_snapshot_paths: list[str] = []
    section_route_connection_index = build_section_route_connection_index(section_route_connection_info)
    for section_name, section_entry in section_route_connection_index.items():
        snapshot_lines = resolve_section_verbatim_analysis_lines(section_entry)
        if not snapshot_lines:
            continue
        safe_section_name = str(section_name).replace("/", "_").replace("\\", "_").strip() or "section"
        snapshot_path = version_dir / f"{OUTPUT_TOPIC}_{safe_section_name}_analysis_v{version}.txt"
        snapshot_path.write_text("\n".join(snapshot_lines), encoding="utf-8")
        analysis_snapshot_paths.append(str(snapshot_path))

    notes = [
        f"{OUTPUT_TOPIC} JSON 驱动 3D 配管说明 v{version}",
        "",
        f"DWG: {dwg_path}",
        f"设备布置图基准: {EQUIPMENT_BASE_DWG_PATH}",
        "本轮脚本直接读取:",
        f"- {LIST_INFO_PATH}",
        f"- {DIMENSION_INFO_PATH}",
        f"- {SECTION_INFO_PATH}",
        f"- {SECTION_ROUTE_CONNECTION_INFO_PATH}",
        "",
        "本轮主输入:",
        f"- 设备图 v{EQUIPMENT_BASE_VERSION} 锚点（通过共享 JSON 重建）",
        "- model_input.pipeline_layout_v2",
        "- pipeline_profiles_v2",
        "- section_route_connection_info 的剖面路由/连接拓扑",
        "",
        "闭环摘要:",
        f"- 设备底座池体数: {closure_report['summary']['drawn_equipment_basins']}",
        f"- 分隔壁格栅数: {closure_report['summary']['drawn_partition_gratings']}",
        f"- 冷却塔数量: {closure_report['summary']['drawn_cooling_towers']}",
        f"- 泵数量: {closure_report['summary']['drawn_pumps']}",
        f"- 刚性防水套管数: {closure_report['summary']['drawn_wall_sleeves']}",
        f"- 主管数量: {closure_report['summary']['drawn_headers']}",
        f"- 泵主支管组数: {closure_report['summary']['drawn_pump_branches']}",
        f"- 冷却塔下落支管组数: {closure_report['summary']['drawn_tower_drops']}",
        f"- 剖面试绘支路数: {closure_report['summary']['drawn_section_preview_routes']}",
        f"- 左侧辅助路线数: {closure_report['summary']['drawn_auxiliary_routes']}",
        f"- 支架代理数: {closure_report['summary']['drawn_support_proxies']}",
        f"- 管段总数: {closure_report['summary']['drawn_pipe_segments']}",
        f"- 已实现/已布局件号数: {closure_report['summary']['drawn_or_layout_implemented_items']}",
        f"- 明确例外件号数: {closure_report['summary']['explicit_exception_items']}",
        f"- 缺失件号数: {closure_report['summary']['missing_items']}",
        f"- 已读取尺寸条目总数: {closure_report['summary']['loaded_dimensions']}",
        f"- 直接控制已使用剖面数: {closure_report['summary']['used_sections']}",
    ]
    if analysis_snapshot_paths:
        notes.extend(
            [
                "",
                "本轮同步归档的剖面分析快照:",
                *[f"- {path}" for path in analysis_snapshot_paths],
            ]
        )
    if source_pdf is not None:
        notes.insert(2, f"源 PDF: {source_pdf}")
    notes_path.write_text("\n".join(notes), encoding="utf-8")

    if source_pdf is not None:
        copy_if_missing(source_pdf, SHARED_DIR / source_pdf.name)


def main() -> int:
    """Load refreshed shared JSONs, build the 3D pipeline model and archive output."""

    for path in (LIST_INFO_PATH, DIMENSION_INFO_PATH, SECTION_INFO_PATH, SECTION_ROUTE_CONNECTION_INFO_PATH):
        if not path.exists():
            raise FileNotFoundError(f"缺少输入文件: {path}")

    list_info = load_json(LIST_INFO_PATH)
    dimension_info = load_json(DIMENSION_INFO_PATH)
    section_info = load_json(SECTION_INFO_PATH)
    section_route_connection_info = load_json(SECTION_ROUTE_CONNECTION_INFO_PATH)
    source_pdf_raw = list_info.get("source_pdf") or dimension_info.get("source_pdf") or section_info.get("source_pdf")
    source_pdf = Path(source_pdf_raw) if source_pdf_raw else None

    version, version_dir = prepare_version_dir(OUTPUT_ROOT_DIR, minimum_version=PIPELINE_VERSION_FLOOR)
    dwg_path = version_dir / f"{OUTPUT_TOPIC}_3D_v{version}.dwg"

    service = CADService()
    create_fresh_document(service)
    route_records, dimension_entries, equipment_counts, pipeline_counts, basin_geometry = build_model(
        service,
        list_info,
        dimension_info,
        section_info,
        section_route_connection_info,
    )

    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    preview_paths = capture_previews(service, version_dir, version)
    closure_report = build_closure_report(
        list_info=list_info,
        dimension_info=dimension_info,
        section_info=section_info,
        section_route_connection_info=section_route_connection_info,
        route_records=route_records,
        dimension_entries=dimension_entries,
        equipment_counts=equipment_counts,
        pipeline_counts=pipeline_counts,
        basin_geometry=basin_geometry,
    )
    write_version_artifacts(
        version_dir=version_dir,
        version=version,
        source_pdf=source_pdf,
        dwg_path=dwg_path,
        preview_paths=preview_paths,
        closure_report=closure_report,
        dimension_entries=dimension_entries,
        section_route_connection_info=section_route_connection_info,
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
