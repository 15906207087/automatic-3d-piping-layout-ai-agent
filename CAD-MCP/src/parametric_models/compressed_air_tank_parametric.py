from __future__ import annotations

import json
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


CURRENT_FILE = Path(__file__).resolve()
SRC_DIR = CURRENT_FILE.parents[1]
PROJECT_DIR = CURRENT_FILE.parents[2]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from screen_capture import ScreenCapture
from server import CADService


OUTPUT_TOPIC = "压缩空气储罐"
CASE_FOLDER_NAME = "压缩空气储罐555"
PDF_PATH = PROJECT_DIR / "output" / CASE_FOLDER_NAME / "shared" / "压缩空气储罐.pdf"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / CASE_FOLDER_NAME
SHARED_DIR = OUTPUT_ROOT_DIR / "shared"

COLOR_WHITE = 7
COLOR_YELLOW = 2
COLOR_CYAN = 4
COLOR_MAGENTA = 6
COLOR_GREEN = 3
COLOR_RED = 1

AXIS_VECTORS: dict[str, tuple[float, float, float]] = {
    "x": (1.0, 0.0, 0.0),
    "-x": (-1.0, 0.0, 0.0),
    "y": (0.0, 1.0, 0.0),
    "-y": (0.0, -1.0, 0.0),
    "z": (0.0, 0.0, 1.0),
    "-z": (0.0, 0.0, -1.0),
}


@dataclass
class CustomFlange:
    """Custom flange dimensions in millimeters."""

    outer_diameter: float
    center_hole_diameter: float
    bolt_circle_diameter: float
    bolt_hole_diameter: float
    bolt_count: int
    thickness: float


@dataclass
class BlindFlangeSpec:
    """Blind flange dimensions used for sealed nozzle ends."""

    outer_diameter: float
    bolt_circle_diameter: float
    bolt_hole_diameter: float
    bolt_count: int
    thickness: float


@dataclass
class NozzleSpec:
    """Parametric nozzle definition."""

    name: str
    connect_center: tuple[float, float, float]
    axis: str
    outer_diameter: float
    wall_thickness: float
    outward_length: float
    insert_depth: float
    flange: CustomFlange | None = None
    blind_flange: BlindFlangeSpec | None = None
    layer: str = "NOZZLE"
    color: int = COLOR_CYAN
    union_to_shell: bool = True
    flange_seat_overlap: float = 6.0


@dataclass
class LugSpec:
    """Approximate lifting lug placement derived from the top view."""

    center: tuple[float, float, float]
    length: float
    width: float
    height: float
    rotation_deg: float


@dataclass
class ChecklistEntry:
    """Structured closure record for each numbered item in the source BOM."""

    item_no: int
    name: str
    quantity: int
    specification: str
    standard: str
    material: str
    status: str
    modeled_as: str
    note: str


@dataclass
class TankParameters:
    """Top-level parameter set used by the script model."""

    drawing_name: str
    diameter: float
    shell_thickness: float
    head_thickness: float
    shell_height: float
    shell_bottom_z: float
    support_height: float
    support_span: float
    total_height: float
    source_page_image: str
    parse_summary: str
    view_interpretation: str
    analysis_basis: str
    nozzles: list[NozzleSpec]
    lugs: list[LugSpec]
    numbered_items: list[ChecklistEntry]


def elliptical_head_surface_offset(diameter: float, radial_offset: float, straight_height: float = 25.0) -> float:
    """Return head height above the opening plane at a given radial offset."""
    radius = diameter / 2.0
    crown_height = diameter / 4.0
    clamped_offset = max(0.0, min(abs(radial_offset), radius))
    ellipse_component = crown_height * ((1.0 - (clamped_offset / radius) ** 2) ** 0.5)
    return straight_height + ellipse_component


def bottom_head_surface_z(opening_plane_z: float, diameter: float, radial_offset: float, straight_height: float = 25.0) -> float:
    """Return the lower head outer surface Z at a given radial offset."""
    return opening_plane_z - elliptical_head_surface_offset(diameter, radial_offset, straight_height=straight_height)


def next_version_dir(output_root: Path) -> tuple[int, Path]:
    """Return the next version folder like v1/v2 under the case topic root."""
    output_root.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(r"^v(\d+)$", re.IGNORECASE)
    max_version = 0
    for item in output_root.iterdir():
        if not item.is_dir():
            continue
        match = pattern.match(item.name)
        if match:
            max_version = max(max_version, int(match.group(1)))
    version = max_version + 1
    version_dir = output_root / f"v{version}"
    version_dir.mkdir(parents=True, exist_ok=False)
    return version, version_dir


def copy_if_missing(source: Path, destination: Path) -> None:
    """Copy source to destination if the destination does not already exist."""
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def render_source_page(destination: Path, dpi: int = 220) -> Path:
    """Render the first PDF page locally for stable archival and review."""
    if destination.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fitz
    except ModuleNotFoundError:
        parser_candidates = sorted((PROJECT_DIR / "output" / "parser").glob(f"{OUTPUT_TOPIC}_page_1_*.png"))
        if not parser_candidates:
            raise
        shutil.copy2(parser_candidates[-1], destination)
        return destination

    document = fitz.open(PDF_PATH)
    try:
        page = document.load_page(0)
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        pix.save(str(destination))
    finally:
        document.close()
    return destination


def build_manual_parse_result(source_page_image: Path) -> dict[str, Any]:
    """Return a manual recognition record backed by the reviewed PDF page."""
    return {
        "success": True,
        "summary": (
            "本轮直接基于 PDF 人工复核建立参数，不依赖 mock 识图结果。"
            "已确认该图为立式压缩空气缓冲罐装配图，包含主视图、俯视图、局部焊接详图和 BOM。"
        ),
        "semantic_model": {
            "document": {"image_path": str(source_page_image)},
            "dimensions": [
                {"label": "筒体外径", "dimension_type": "diameter", "value": 800.0},
                {"label": "筒体直段高度", "dimension_type": "vertical", "value": 1800.0},
                {"label": "设备总高", "dimension_type": "vertical", "value": 2966.0},
                {"label": "支腿高度", "dimension_type": "vertical", "value": 830.0},
                {"label": "支腿跨距", "dimension_type": "horizontal", "value": 792.0},
                {"label": "侧口标高", "dimension_type": "vertical", "value": 500.0},
                {"label": "封头直边", "dimension_type": "vertical", "value": 25.0},
                {"label": "封头外高", "dimension_type": "vertical", "value": 225.0},
                {"label": "口件外伸", "dimension_type": "horizontal", "value": 100.0},
            ],
            "graph": {
                "nodes": [
                    {"label": "N1", "elevation": 500.0, "azimuth_deg": 180.0},
                    {"label": "N2", "elevation": 1800.0, "azimuth_deg": 90.0},
                    {"label": "N3", "elevation": 2241.0, "azimuth_deg": 0.0},
                    {"label": "N4", "elevation": 2241.0, "azimuth_deg": None},
                    {"label": "P", "elevation": 2241.0, "azimuth_deg": 90.0},
                    {"label": "N5", "elevation": 0.0, "azimuth_deg": None},
                ],
                "segments": [],
                "fittings": [],
            },
        },
    }


def pick_dimension_value(dimensions: list[dict[str, Any]], *, dimension_type: str, value: float) -> float:
    """Pick a parsed dimension value close to the expected target."""
    best_match: float | None = None
    best_delta: float | None = None
    for item in dimensions:
        if item.get("dimension_type") != dimension_type:
            continue
        item_value = item.get("value")
        if item_value is None:
            continue
        delta = abs(float(item_value) - value)
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_match = float(item_value)
    return best_match if best_match is not None else value


def build_parameters(parse_result: dict[str, Any]) -> TankParameters:
    """Build a manual parameter set validated against the PDF front and top views."""
    semantic_model = parse_result["semantic_model"]
    dimensions = semantic_model.get("dimensions", [])
    diameter = pick_dimension_value(dimensions, dimension_type="diameter", value=800.0)
    shell_height = 1800.0
    total_height = 2966.0
    support_height = 830.0
    support_span = 792.0
    shell_bottom_z = 225.0
    shell_top_z = shell_bottom_z + shell_height
    shell_thickness = 6.0
    head_thickness = 5.3
    nozzle_projection = 100.0
    top_nozzle_radial_offset = 125.0
    top_nozzle_z = shell_top_z + elliptical_head_surface_offset(diameter, top_nozzle_radial_offset)

    dn50_flange = CustomFlange(
        outer_diameter=165.0,
        center_hole_diameter=57.0,
        bolt_circle_diameter=125.0,
        bolt_hole_diameter=18.0,
        bolt_count=4,
        thickness=18.0,
    )
    dn40_flange = CustomFlange(
        outer_diameter=145.0,
        center_hole_diameter=45.0,
        bolt_circle_diameter=110.0,
        bolt_hole_diameter=18.0,
        bolt_count=4,
        thickness=16.0,
    )
    dn25_flange = CustomFlange(
        outer_diameter=115.0,
        center_hole_diameter=32.0,
        bolt_circle_diameter=85.0,
        bolt_hole_diameter=14.0,
        bolt_count=4,
        thickness=16.0,
    )
    blind_flange_25 = BlindFlangeSpec(
        outer_diameter=115.0,
        bolt_circle_diameter=85.0,
        bolt_hole_diameter=14.0,
        bolt_count=4,
        thickness=16.0,
    )

    nozzles = [
        NozzleSpec(
            name="N1",
            connect_center=(0.0, -diameter / 2.0, 500.0),
            axis="-y",
            outer_diameter=57.0,
            wall_thickness=3.5,
            outward_length=nozzle_projection,
            insert_depth=35.0,
            flange=dn50_flange,
        ),
        NozzleSpec(
            name="N2",
            connect_center=(diameter / 2.0, 0.0, shell_top_z - 200.0),
            axis="x",
            outer_diameter=57.0,
            wall_thickness=3.5,
            outward_length=nozzle_projection,
            insert_depth=35.0,
            flange=dn50_flange,
        ),
        NozzleSpec(
            name="N3",
            connect_center=(0.0, top_nozzle_radial_offset, top_nozzle_z),
            axis="z",
            outer_diameter=57.0,
            wall_thickness=3.5,
            outward_length=nozzle_projection,
            insert_depth=30.0,
            flange=dn50_flange,
        ),
        NozzleSpec(
            name="N4",
            connect_center=(0.0, 0.0, shell_top_z + elliptical_head_surface_offset(diameter, 0.0)),
            axis="z",
            outer_diameter=57.0,
            wall_thickness=3.5,
            outward_length=nozzle_projection,
            insert_depth=30.0,
            flange=dn50_flange,
        ),
        NozzleSpec(
            name="P",
            connect_center=(top_nozzle_radial_offset, 0.0, top_nozzle_z),
            axis="z",
            outer_diameter=32.0,
            wall_thickness=3.0,
            outward_length=nozzle_projection,
            insert_depth=24.0,
            flange=dn25_flange,
            blind_flange=blind_flange_25,
        ),
        NozzleSpec(
            name="N5",
            connect_center=(0.0, 0.0, 0.0),
            axis="-z",
            outer_diameter=45.0,
            wall_thickness=4.0,
            outward_length=nozzle_projection,
            insert_depth=25.0,
            flange=dn40_flange,
        ),
    ]

    lugs = [
        LugSpec(center=(-230.0, 230.0, shell_top_z + 55.0), length=155.0, width=18.0, height=90.0, rotation_deg=45.0),
        LugSpec(center=(230.0, -230.0, shell_top_z + 55.0), length=155.0, width=18.0, height=90.0, rotation_deg=45.0),
    ]

    numbered_items = [
        ChecklistEntry(1, "支腿", 4, "A3-830-6", "JB/T 4712.2-2007", "Q235-A/Q345R", "modeled", "2组镜像支腿总成，对应4件腿板", "按设备级 3D 简化建模，保留支腿高度 830 和跨距 792。"),
        ChecklistEntry(2, "接管", 1, "φ45x4", "", "20", "modeled", "N5 排污口接管", "按底部排污口空心接管建模。"),
        ChecklistEntry(3, "法兰", 1, "WN40(B)-16 RF(S=4)", "HG/T20592-2009", "16MnII", "modeled", "N5 排污口法兰", "按 DN40 法兰建模。"),
        ChecklistEntry(4, "椭圆封头", 2, "EHA800x6(5.3)", "GB/T25198-2010", "Q345R", "modeled", "上下椭圆封头", "按 2:1 椭圆封头近似生成。"),
        ChecklistEntry(5, "接管", 4, "φ57x3.5", "", "20", "modeled", "N1/N2/N3/N4 接管", "按 4 个 DN50 接管建模。"),
        ChecklistEntry(6, "法兰", 4, "WN50(B)-16 RF(S=3.5)", "HG/T20592-2009", "16MnII", "modeled", "N1/N2/N3/N4 法兰", "按 4 个 DN50 法兰建模。"),
        ChecklistEntry(7, "筒体", 1, "DN800x6 H=1800", "", "Q345R", "modeled", "主体筒体", "按外径 φ800、直段高度 1800 建模。"),
        ChecklistEntry(8, "接管", 1, "φ32x3", "", "20", "modeled", "P 压力表口接管", "按顶部小口空心接管建模。"),
        ChecklistEntry(9, "法兰", 1, "WN25(B)-16 RF(S=3)", "HG/T20592-2009", "20II", "modeled", "P 口法兰", "按 DN25 法兰建模。"),
        ChecklistEntry(10, "法兰盖", 1, "BL25-16 RF", "HG/T20592-2009", "20II", "modeled", "P 口盲板", "按盲法兰盖建模。"),
        ChecklistEntry(11, "垫圈", 1, "25-16 RF", "HG/T20606-2009", "PTFE", "documented_not_modeled", "P 口密封垫", "装配级 3D 中未单独拉出薄垫片实体，但在闭环记录中保留。"),
        ChecklistEntry(12, "螺栓", 4, "M12x50", "GB/T5782-2000", "8.8级", "documented_not_modeled", "P 口紧固件", "为保证本轮设备级交付效率，未逐颗建模。"),
        ChecklistEntry(13, "螺母", 4, "M12", "GB/T6170-2000", "8级", "documented_not_modeled", "P 口紧固件", "与螺栓同属装配紧固件，未逐颗建模。"),
        ChecklistEntry(14, "耳环", 2, "TPP-2-6", "HG/T21574-2008", "组合件", "modeled", "上封头两只吊耳近似实体", "结合俯视图 135°/315° 方位进行近似建模。"),
        ChecklistEntry(15, "铭牌、铭牌座", 1, "KH-001", "", "组合件", "documented_not_modeled", "铭牌附件", "图中未给出足够三维轮廓与精确安装位置，本轮记录但不单独建模。"),
    ]

    return TankParameters(
        drawing_name=OUTPUT_TOPIC,
        diameter=diameter,
        shell_thickness=shell_thickness,
        head_thickness=head_thickness,
        shell_height=shell_height,
        shell_bottom_z=shell_bottom_z,
        support_height=support_height,
        support_span=support_span,
        total_height=total_height,
        source_page_image=semantic_model["document"]["image_path"],
        parse_summary=parse_result["summary"],
        view_interpretation="该 PDF 属于立式容器装配图：主视图用于读取主体高度、封头和支腿；俯视图用于判定 N1/N2/N3/P 的周向方位，N4 与 N5 在俯视图中发生重影。",
        analysis_basis=(
            "依据主视图读取 φ800、H=1800、总高≈2966、支腿高 830、跨距 792、侧口标高 500 和各口件 100 外伸量；"
            "依据俯视图读取 N2 位于 90°、N1 位于 180°、N3 位于 0°、P 位于 90°偏心顶部，"
            "并结合 BOM 确认真实件号仅有 N1/N2/N3/N4/N5/P，不存在额外 N6。"
        ),
        nozzles=nozzles,
        lugs=lugs,
        numbered_items=numbered_items,
    )


def axis_offset(point: tuple[float, float, float], axis: str, distance: float) -> tuple[float, float, float]:
    """Offset a point along a signed axis."""
    dx, dy, dz = AXIS_VECTORS[axis]
    return (point[0] + dx * distance, point[1] + dy * distance, point[2] + dz * distance)


def orient_flange(controller: Any, handle: str, center: tuple[float, float, float], axis: str) -> None:
    """Rotate a flange from +Z to the target axis."""
    if axis == "x":
        controller.rotate_entity(handle, center, 90.0, axis_direction=(0.0, 1.0, 0.0))
    elif axis == "-x":
        controller.rotate_entity(handle, center, -90.0, axis_direction=(0.0, 1.0, 0.0))
    elif axis == "y":
        controller.rotate_entity(handle, center, 90.0, axis_direction=(1.0, 0.0, 0.0))
    elif axis == "-y":
        controller.rotate_entity(handle, center, -90.0, axis_direction=(1.0, 0.0, 0.0))
    elif axis == "-z":
        controller.rotate_entity(handle, center, 180.0, axis_direction=(1.0, 0.0, 0.0))


def create_fresh_document(service: CADService) -> None:
    """Create a fresh drawing document for this scripted build."""
    if not service.start_cad():
        raise RuntimeError("无法启动或连接 CAD。")
    docs = getattr(service.controller.app, "Documents", None)
    if docs is None:
        raise RuntimeError("CAD 未暴露 Documents 集合，无法创建新文档。")
    docs.Add()
    time.sleep(2.0)
    service.controller.doc = service.controller.app.ActiveDocument
    service.drawing_state["entities"] = []


def apply_entity_style(controller: Any, entity: Any, layer: str, color: int) -> None:
    """Apply layer and color to a created entity."""
    last_error: Exception | None = None
    for _ in range(3):
        try:
            controller.create_layer(layer)
            entity.Layer = layer
            entity.Color = color
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.4)
    if last_error is not None:
        raise last_error


def draw_box_with_retry(
    controller: Any,
    *,
    center: tuple[float, float, float],
    length: float,
    width: float,
    height: float,
    layer: str,
    color: int,
    retries: int = 3,
    wait_seconds: float = 0.8,
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


def draw_flange_with_retry(
    controller: Any,
    center: tuple[float, float, float],
    flange: CustomFlange,
    retries: int = 3,
    wait_seconds: float = 0.8,
) -> Any:
    """Retry flange creation because early COM calls may be refused by CAD."""
    last_result = None
    for _ in range(retries):
        last_result = controller.draw_flange(
            center,
            outer_diameter=flange.outer_diameter,
            center_hole_diameter=flange.center_hole_diameter,
            bolt_circle_diameter=flange.bolt_circle_diameter,
            bolt_hole_diameter=flange.bolt_hole_diameter,
            bolt_count=flange.bolt_count,
            thickness=flange.thickness,
        )
        if last_result is not None:
            return last_result
        time.sleep(wait_seconds)
    return last_result


def draw_blind_flange_with_retry(
    service: CADService,
    center: tuple[float, float, float],
    blind_flange: BlindFlangeSpec,
    retries: int = 4,
    wait_seconds: float = 0.8,
) -> Any:
    """Retry blind flange creation because sealed nozzle ends are COM-sensitive."""
    last_result = None
    for _ in range(retries):
        last_result = service.draw_blind_flange(
            center,
            outer_diameter=blind_flange.outer_diameter,
            bolt_circle_diameter=blind_flange.bolt_circle_diameter,
            bolt_hole_diameter=blind_flange.bolt_hole_diameter,
            bolt_count=blind_flange.bolt_count,
            thickness=blind_flange.thickness,
            layer="BLIND_FLANGE",
            color=COLOR_RED,
        )
        if last_result is not None:
            return last_result
        time.sleep(wait_seconds)
    return last_result


def draw_cylinder_with_retry(
    controller: Any,
    *,
    center: tuple[float, float, float],
    radius: float,
    height: float,
    layer: str,
    color: int,
    retries: int = 4,
    wait_seconds: float = 0.6,
) -> Any:
    """Create a cylinder with retries and apply style after the solid exists."""
    last_result = None
    for _ in range(retries):
        last_result = controller.draw_cylinder(center=center, radius=radius, height=height)
        if last_result is not None:
            apply_entity_style(controller, last_result, layer, color)
            return last_result
        time.sleep(wait_seconds)
    return last_result


def entity_handle(entity: Any, retries: int = 5, wait_seconds: float = 0.5) -> str:
    """Read COM entity handle with retries because AutoCAD may still be busy."""
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            return str(entity.Handle)
        except Exception as exc:
            last_error = exc
            time.sleep(wait_seconds)
    if last_error is not None:
        raise last_error
    raise RuntimeError("实体句柄读取失败。")


def boolean_union_with_retry(
    controller: Any,
    primary_handle: str,
    secondary_handle: str,
    retries: int = 4,
    wait_seconds: float = 0.8,
) -> None:
    """Retry boolean union because CAD can reject early COM calls."""
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            if controller.boolean_union(primary_handle, secondary_handle):
                return
        except Exception as exc:
            last_error = exc
        time.sleep(wait_seconds)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"布尔并集失败: {primary_handle}, {secondary_handle}")


def boolean_subtract_with_retry(
    controller: Any,
    primary_handle: str,
    secondary_handle: str,
    retries: int = 4,
    wait_seconds: float = 0.8,
) -> None:
    """Retry boolean subtraction for hollow pipe generation."""
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            if controller.boolean_subtract(primary_handle, secondary_handle):
                return
        except Exception as exc:
            last_error = exc
        time.sleep(wait_seconds)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"布尔减失败: {primary_handle}, {secondary_handle}")


def draw_pipe_with_retry(
    controller: Any,
    *,
    center: tuple[float, float, float],
    outer_diameter: float,
    wall_thickness: float,
    height: float,
    layer: str,
    color: int,
) -> Any:
    """Create a hollow pipe using outer and inner cylinders."""
    outer_solid = draw_cylinder_with_retry(
        controller,
        center=center,
        radius=outer_diameter / 2.0,
        height=height,
        layer=layer,
        color=color,
    )
    if outer_solid is None:
        return None
    inner_radius = outer_diameter / 2.0 - wall_thickness
    if inner_radius <= 0:
        return outer_solid
    inner_solid = draw_cylinder_with_retry(
        controller,
        center=(center[0], center[1], center[2] - 1.0),
        radius=inner_radius,
        height=height + 2.0,
        layer=layer,
        color=color,
    )
    if inner_solid is None:
        raise RuntimeError("接管内孔创建失败。")
    boolean_subtract_with_retry(controller, entity_handle(outer_solid), entity_handle(inner_solid))
    return outer_solid


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


def add_dimension_with_retry(controller: Any, retries: int = 4, wait_seconds: float = 0.6, **kwargs: Any) -> Any:
    """Retry dimensions because CAD frequently rejects consecutive annotations."""
    last_result = None
    for _ in range(retries):
        last_result = controller.add_dimension(**kwargs)
        if last_result is not None:
            return last_result
        time.sleep(wait_seconds)
    return last_result


def draw_nozzle(service: CADService, shell_handle: str, nozzle: NozzleSpec) -> None:
    """Draw a nozzle as a parametric solid and merge it into the vessel shell."""
    controller = service.controller
    radius = nozzle.outer_diameter / 2.0
    total_length = nozzle.outward_length + nozzle.insert_depth
    base_point = axis_offset(nozzle.connect_center, nozzle.axis, -nozzle.insert_depth)
    outer_solid = controller._add_oriented_cylinder(base_point, radius, total_length, nozzle.axis)
    if outer_solid is None:
        raise RuntimeError(f"口件 {nozzle.name} 创建失败。")
    apply_entity_style(controller, outer_solid, nozzle.layer, nozzle.color)
    solid_handle = entity_handle(outer_solid)
    inner_radius = radius - nozzle.wall_thickness
    if inner_radius > 0:
        inner_solid = controller._add_oriented_cylinder(base_point, inner_radius, total_length + 2.0, nozzle.axis)
        if inner_solid is None:
            raise RuntimeError(f"口件 {nozzle.name} 内孔创建失败。")
        apply_entity_style(controller, inner_solid, nozzle.layer, nozzle.color)
        boolean_subtract_with_retry(controller, solid_handle, entity_handle(inner_solid))
    if nozzle.union_to_shell:
        boolean_union_with_retry(controller, shell_handle, solid_handle)

    if nozzle.flange is None:
        return

    flange_base = axis_offset(
        nozzle.connect_center,
        nozzle.axis,
        max(nozzle.outward_length - nozzle.flange_seat_overlap, 0.0),
    )
    flange_entity = draw_flange_with_retry(controller, flange_base, nozzle.flange)
    if flange_entity is None:
        raise RuntimeError(f"口件 {nozzle.name} 的法兰创建失败。")
    flange_handle = entity_handle(flange_entity)
    orient_flange(controller, flange_handle, flange_base, nozzle.axis)
    apply_entity_style(controller, flange_entity, "FLANGE", COLOR_MAGENTA)

    if nozzle.blind_flange is not None:
        blind_center = axis_offset(
            flange_base,
            nozzle.axis,
            nozzle.flange.thickness / 2.0 + nozzle.blind_flange.thickness / 2.0,
        )
        blind_entity = draw_blind_flange_with_retry(service, blind_center, nozzle.blind_flange)
        if blind_entity is None:
            raise RuntimeError(f"口件 {nozzle.name} 的盲板创建失败。")
        orient_flange(controller, entity_handle(blind_entity), blind_center, nozzle.axis)
        apply_entity_style(controller, blind_entity, "BLIND_FLANGE", COLOR_RED)


def add_supports(service: CADService, params: TankParameters) -> None:
    """Add a simplified pair of support legs and base plates."""
    controller = service.controller
    leg_center_x = 300.0
    leg_width_x = 110.0
    leg_width_y = 180.0
    base_plate_width_x = 180.0
    base_plate_width_y = 220.0
    base_plate_thickness = 18.0
    connector_length_x = 70.0
    connector_height_z = 120.0
    connector_width_y = 90.0
    plate_center_z = -params.support_height - base_plate_thickness / 2.0
    leg_center_z = -params.support_height / 2.0

    for sign in (-1.0, 1.0):
        leg = draw_box_with_retry(
            controller,
            center=(sign * leg_center_x, 0.0, leg_center_z),
            length=leg_width_x,
            width=leg_width_y,
            height=params.support_height,
            layer="SUPPORT",
            color=COLOR_YELLOW,
        )
        if leg is None:
            raise RuntimeError("支腿实体创建失败。")
        plate = draw_box_with_retry(
            controller,
            center=(sign * leg_center_x, 0.0, plate_center_z),
            length=base_plate_width_x,
            width=base_plate_width_y,
            height=base_plate_thickness,
            layer="SUPPORT",
            color=COLOR_YELLOW,
        )
        if plate is None:
            raise RuntimeError("底板实体创建失败。")
        connector = draw_box_with_retry(
            controller,
            center=(sign * (leg_center_x - connector_length_x / 2.0), 0.0, connector_height_z / 2.0),
            length=connector_length_x,
            width=connector_width_y,
            height=connector_height_z,
            layer="SUPPORT",
            color=COLOR_YELLOW,
        )
        if connector is None:
            raise RuntimeError("支腿连接块创建失败。")


def add_lugs(service: CADService, params: TankParameters) -> None:
    """Add two approximate lifting lugs from the reviewed top-view positions."""
    controller = service.controller
    for lug in params.lugs:
        lug_entity = draw_box_with_retry(
            controller,
            center=lug.center,
            length=lug.length,
            width=lug.width,
            height=lug.height,
            layer="LUG",
            color=COLOR_WHITE,
        )
        if lug_entity is None:
            raise RuntimeError("吊耳实体创建失败。")
        controller.rotate_entity(entity_handle(lug_entity), lug.center, lug.rotation_deg, axis_direction=(0.0, 0.0, 1.0))


def add_key_dimensions(service: CADService, params: TankParameters) -> None:
    """Reproduce the key source dimensions in the front view."""
    shell_top_z = params.shell_bottom_z + params.shell_height
    add_dimension_with_retry(
        service.controller,
        start_point=(-params.diameter / 2.0, 0.0, 1600.0),
        end_point=(params.diameter / 2.0, 0.0, 1600.0),
        center=(0.0, 0.0, 1600.0),
        chord_point=(-params.diameter / 2.0, 0.0, 1600.0),
        far_chord_point=(params.diameter / 2.0, 0.0, 1600.0),
        textheight=30.0,
        layer="DIM",
        color=COLOR_GREEN,
        dimension_type="diameter",
        source_view="front",
    )
    linear_dimensions = [
        ((0.0, 0.0, 0.0), (0.0, 0.0, params.total_height), (-560.0, 0.0, params.total_height / 2.0)),
        ((0.0, 0.0, params.shell_bottom_z), (0.0, 0.0, shell_top_z), (-470.0, 0.0, (params.shell_bottom_z + shell_top_z) / 2.0)),
        ((-params.diameter / 2.0 - 40.0, 0.0, 0.0), (-params.diameter / 2.0 - 40.0, 0.0, params.shell_bottom_z), (-650.0, 0.0, params.shell_bottom_z / 2.0)),
        ((0.0, 0.0, 0.0), (0.0, 0.0, 500.0), (-420.0, 0.0, 250.0)),
        ((0.0, 0.0, 0.0), (0.0, 0.0, params.support_height), (520.0, 0.0, params.support_height / 2.0)),
        ((-params.support_span / 2.0, 0.0, -80.0), (params.support_span / 2.0, 0.0, -80.0), (0.0, 0.0, -120.0)),
        ((params.diameter / 2.0, 0.0, 500.0), (params.diameter / 2.0 + 100.0, 0.0, 500.0), (params.diameter / 2.0 + 130.0, 0.0, 540.0)),
    ]
    for start_point, end_point, text_point in linear_dimensions:
        add_dimension_with_retry(
            service.controller,
            start_point=start_point,
            end_point=end_point,
            text_position=text_point,
            textheight=30.0,
            layer="DIM",
            color=COLOR_GREEN,
            dimension_type="vertical" if start_point[2] != end_point[2] and start_point[0] == end_point[0] else "horizontal",
            source_view="front",
        )


def build_model(service: CADService, params: TankParameters) -> None:
    """Build the vessel model using structured parameters instead of interactive steps."""
    shell = service.draw_cylinder(
        center=(0.0, 0.0, params.shell_bottom_z),
        radius=params.diameter / 2.0,
        height=params.shell_height,
        layer="VESSEL",
        color=COLOR_WHITE,
    )
    if shell is None:
        raise RuntimeError("筒体创建失败。")
    shell_handle = entity_handle(shell)

    shell_top_z = params.shell_bottom_z + params.shell_height
    top_head = service.draw_elliptical_head(
        center=(0.0, 0.0, shell_top_z),
        dn=params.diameter,
        axis="z",
        with_straight=True,
        layer="VESSEL",
        color=COLOR_WHITE,
    )
    bottom_head = service.draw_elliptical_head(
        center=(0.0, 0.0, params.shell_bottom_z),
        dn=params.diameter,
        axis="-z",
        with_straight=True,
        layer="VESSEL",
        color=COLOR_WHITE,
    )
    if top_head is None or bottom_head is None:
        raise RuntimeError("封头创建失败。")
    boolean_union_with_retry(service.controller, shell_handle, entity_handle(top_head))
    boolean_union_with_retry(service.controller, shell_handle, entity_handle(bottom_head))

    add_supports(service, params)
    add_lugs(service, params)
    for nozzle in params.nozzles:
        draw_nozzle(service, shell_handle, nozzle)
    add_key_dimensions(service, params)
    zoom_extents_with_retry(service.controller)


def capture_previews(service: CADService, version_dir: Path, version: int, params: TankParameters) -> list[str]:
    """Capture front, top and isometric previews for visual verification."""
    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    shell_top_z = params.shell_bottom_z + params.shell_height
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    front_target = (0.0, 0.0, (params.shell_bottom_z + shell_top_z) / 2.0)
    top_target = (0.0, 0.0, (params.shell_bottom_z + shell_top_z) / 2.0)
    iso_target = (0.0, 0.0, 900.0)
    view_specs = [
        ("front", front_target, 4200.0, 3200.0, f"{OUTPUT_TOPIC}_front_preview_v{version}.png"),
        ("top", top_target, 2600.0, 2600.0, f"{OUTPUT_TOPIC}_top_preview_v{version}.png"),
        ("swiso", iso_target, 5600.0, 5600.0, f"{OUTPUT_TOPIC}_iso_preview_v{version}.png"),
    ]
    saved_paths: list[str] = []
    for view_name, target, height, width, file_name in view_specs:
        if not set_named_view_with_retry(service.controller, view_name, target=target, height=height, width=width):
            raise RuntimeError(f"预览视图切换失败: {view_name}")
        capture = capturer.capture_cad_window(
            document_name=document_name,
            title_hint=title_hint,
            preferred_hwnd=preferred_hwnd,
            document_hwnd=document_hwnd,
        )
        final_path = version_dir / file_name
        shutil.copy2(capture["image_path"], final_path)
        temp_capture_path = Path(capture["image_path"])
        if temp_capture_path.exists() and temp_capture_path.resolve() != final_path.resolve():
            temp_capture_path.unlink()
        saved_paths.append(str(final_path))
    return saved_paths


def write_artifacts(
    version_dir: Path,
    version: int,
    params: TankParameters,
    parse_result: dict[str, Any],
    dwg_path: Path,
    preview_paths: list[str],
    shared_source_page_path: Path,
) -> None:
    """Persist structured analysis and notes alongside the DWG."""
    parameters_path = version_dir / f"{OUTPUT_TOPIC}_parameters_v{version}.json"
    notes_path = version_dir / f"{OUTPUT_TOPIC}_notes_v{version}.txt"

    with parameters_path.open("w", encoding="utf-8") as file_obj:
        json.dump(
            {
                "parameters": {
                    **asdict(params),
                    "nozzles": [asdict(item) for item in params.nozzles],
                    "lugs": [asdict(item) for item in params.lugs],
                    "numbered_items": [asdict(item) for item in params.numbered_items],
                },
                "parse_result": parse_result,
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
                "shared_source_page_path": str(shared_source_page_path),
            },
            file_obj,
            ensure_ascii=False,
            indent=2,
        )

    notes = [
        f"{OUTPUT_TOPIC} 参数化脚本建模说明 v{version}",
        "",
        f"源文件: {PDF_PATH}",
        f"DWG 输出: {dwg_path}",
        "本轮采用参数化 Python 脚本建模，不采用纯交互式逐步建模。",
        f"视图判定: {params.view_interpretation}",
        f"识图摘要: {params.parse_summary}",
        f"参数依据: {params.analysis_basis}",
        "",
        "本轮脚本参数重点:",
        f"- 筒体直径: {params.diameter}",
        f"- 筒体厚度: {params.shell_thickness}",
        f"- 封头厚度: {params.head_thickness}",
        f"- 筒体直段高度: {params.shell_height}",
        f"- 总高: {params.total_height}",
        f"- 支腿高度: {params.support_height}",
        f"- 支腿跨距: {params.support_span}",
        f"- 口件数量: {len(params.nozzles)}",
        f"- 吊耳数量: {len(params.lugs)}",
        "",
        "归档脚本:",
        "",
        "件号闭环:",
        *[
            f"- {item.item_no}. {item.name} | 数量={item.quantity} | 状态={item.status} | 映射={item.modeled_as} | 说明={item.note}"
            for item in params.numbered_items
        ],
        "",
        "说明:",
        "- 本轮 3D 模型已补充关键尺寸标注，用于与 PDF 主视图复核。",
        "- P 口已补齐法兰盖；垫片、螺栓、螺母和铭牌保留在闭环清单中，但未逐件细化实体。",
        "- 已输出主视、俯视、等轴测预览图，用于本轮视觉复核。",
    ]
    notes_path.write_text("\n".join(notes), encoding="utf-8")

    copy_if_missing(PDF_PATH, SHARED_DIR / PDF_PATH.name)
    copy_if_missing(shared_source_page_path, SHARED_DIR / shared_source_page_path.name)


def main() -> int:
    """Entry point for scripted modeling of the compressed air tank."""
    if not PDF_PATH.exists():
        raise FileNotFoundError(f"未找到源 PDF: {PDF_PATH}")

    OUTPUT_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    version, version_dir = next_version_dir(OUTPUT_ROOT_DIR)
    dwg_path = version_dir / f"{OUTPUT_TOPIC}_3D_v{version}.dwg"
    shared_source_page_path = render_source_page(SHARED_DIR / f"{OUTPUT_TOPIC}_source_page1.png")
    parse_result = build_manual_parse_result(shared_source_page_path)
    params = build_parameters(parse_result)

    service = CADService()
    create_fresh_document(service)
    build_model(service, params)

    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    preview_paths = capture_previews(service, version_dir, version, params)
    write_artifacts(
        version_dir,
        version,
        params,
        parse_result,
        dwg_path,
        preview_paths,
        shared_source_page_path,
    )
    print(
        json.dumps(
            {
                "success": True,
                "dwg_path": str(dwg_path),
                "version": version,
                "version_dir": str(version_dir),
                "preview_paths": preview_paths,
                "shared_source_page_path": str(shared_source_page_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
