from __future__ import annotations

import json
import math
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

from pipeline_connection_helper import _add_directional_hollow_cylinder
from screen_capture import ScreenCapture
from server import CADService
from parametric_models.compressed_air_tank_parametric import (
    COLOR_WHITE,
    CustomFlange,
    apply_entity_style,
    boolean_union_with_retry,
    copy_if_missing,
    create_fresh_document,
    draw_box_with_retry,
    draw_flange_with_retry,
    entity_handle,
    next_version_dir,
    orient_flange,
    save_drawing_with_retry,
    set_named_view_with_retry,
    zoom_extents_with_retry,
)

OUTPUT_TOPIC = "5m3原料水罐"
PDF_PATH = Path(r"f:\毕业论文\管道布置图\PDF\PDF\5m3原料水罐.pdf")
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
SHARED_DIR = OUTPUT_ROOT_DIR / "shared"


@dataclass
class NozzleSpec:
    """Parametric nozzle definition for the raw water tank."""

    name: str
    kind: str
    angle_deg: float | None
    elevation: float
    outer_diameter: float
    wall_thickness: float | None
    outward_length: float
    insert_depth: float
    flange: CustomFlange | None = None
    radial_offset: float = 0.0
    layer: str = "NOZZLE"
    color: int = COLOR_WHITE
    union_to_shell: bool = True
    flange_seat_overlap: float = 6.0


@dataclass
class TankParameters:
    """Top-level parameters extracted from the drawing."""

    drawing_name: str
    diameter: float
    shell_height: float
    top_plate_thickness: float
    overall_height: float
    bottom_ring_outer_diameter: float
    bottom_ring_mid_diameter: float
    bottom_ring_inner_diameter: float
    manhole_diameter: float
    shell_thickness: float
    bottom_slope_rise: float
    source_page_image: str
    view_interpretation: str
    analysis_basis: str
    nozzles: list[NozzleSpec]


def render_source_page(destination: Path, dpi: int = 320) -> Path:
    """Render the first PDF page locally for archival and review."""

    import fitz

    if destination.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open(PDF_PATH)
    try:
        page = document.load_page(0)
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        pix.save(str(destination))
    finally:
        document.close()
    return destination


def ensure_drawable_document(service: CADService) -> None:
    """Attach to a clean drawable CAD document for this scripted build."""

    if not service.start_cad():
        raise RuntimeError("无法启动或连接 CAD。")
    docs = getattr(service.controller.app, "Documents", None)
    last_error: Exception | None = None

    def _doc_is_clean_and_drawable(doc: Any) -> bool:
        service.controller.doc = doc
        model_space = service.controller._get_model_space()
        count = None
        try:
            count = int(getattr(model_space, "Count", -1))
        except Exception:
            count = None
        return model_space is not None and (count in (0, None))

    previous_active = getattr(service.controller.app, "ActiveDocument", None)
    previous_count = None
    if docs is not None:
        try:
            previous_count = int(getattr(docs, "Count", 0))
        except Exception:
            previous_count = None

        try:
            add_method = getattr(docs, "Add", None)
            if callable(add_method):
                add_method()
                time.sleep(4.0)
        except Exception as exc:
            last_error = exc

        for _ in range(16):
            try:
                active_doc = getattr(service.controller.app, "ActiveDocument", None)
                if active_doc is not None and active_doc is not previous_active:
                    if _doc_is_clean_and_drawable(active_doc):
                        return
                if previous_count is not None:
                    current_count = int(getattr(docs, "Count", 0))
                    if current_count > previous_count:
                        for index in list(range(current_count)) + list(range(1, current_count + 1)):
                            try:
                                candidate = docs.Item(index)
                                if candidate is None or candidate is previous_active:
                                    continue
                                if _doc_is_clean_and_drawable(candidate):
                                    return
                            except Exception as exc:
                                last_error = exc
            except Exception as exc:
                last_error = exc
            time.sleep(0.8)

    if previous_active is not None:
        try:
            send_command = getattr(previous_active, "SendCommand", None)
            if callable(send_command):
                send_command("_.QNEW\n")
        except Exception as exc:
            last_error = exc

        for _ in range(20):
            try:
                active_doc = getattr(service.controller.app, "ActiveDocument", None)
                if active_doc is not None and active_doc is not previous_active:
                    if _doc_is_clean_and_drawable(active_doc):
                        return
            except Exception as exc:
                last_error = exc
            time.sleep(1.0)

    raise RuntimeError(f"无法获取干净可绘制 CAD 文档: {last_error}")


def tank_polar_point(radius: float, angle_deg: float, z: float) -> tuple[float, float, float]:
    """Convert drawing azimuth into WCS coordinates with 0 deg at the tank top."""

    radians_value = math.radians(angle_deg)
    return (
        radius * math.sin(radians_value),
        radius * math.cos(radians_value),
        z,
    )


def polar_unit_vector(angle_deg: float) -> tuple[float, float, float]:
    """Return the horizontal radial unit vector for one drawing azimuth."""

    radians_value = math.radians(angle_deg)
    return (
        math.sin(radians_value),
        math.cos(radians_value),
        0.0,
    )


def build_parameters(source_page_image: Path) -> TankParameters:
    """Return manually confirmed parameters from the PDF drawing."""

    dn25_flange = CustomFlange(
        outer_diameter=115.0,
        center_hole_diameter=32.0,
        bolt_circle_diameter=85.0,
        bolt_hole_diameter=14.0,
        bolt_count=4,
        thickness=16.0,
    )
    dn32_flange = CustomFlange(
        outer_diameter=140.0,
        center_hole_diameter=38.0,
        bolt_circle_diameter=100.0,
        bolt_hole_diameter=18.0,
        bolt_count=4,
        thickness=16.0,
    )
    dn40_flange = CustomFlange(
        outer_diameter=150.0,
        center_hole_diameter=45.0,
        bolt_circle_diameter=110.0,
        bolt_hole_diameter=18.0,
        bolt_count=4,
        thickness=16.0,
    )
    dn50_flange = CustomFlange(
        outer_diameter=165.0,
        center_hole_diameter=57.0,
        bolt_circle_diameter=125.0,
        bolt_hole_diameter=18.0,
        bolt_count=4,
        thickness=18.0,
    )
    dn80_flange = CustomFlange(
        outer_diameter=200.0,
        center_hole_diameter=89.0,
        bolt_circle_diameter=160.0,
        bolt_hole_diameter=18.0,
        bolt_count=8,
        thickness=20.0,
    )
    manhole_flange = CustomFlange(
        outer_diameter=560.0,
        center_hole_diameter=450.0,
        bolt_circle_diameter=510.0,
        bolt_hole_diameter=18.0,
        bolt_count=16,
        thickness=24.0,
    )

    return TankParameters(
        drawing_name="原料水罐 DN1600 VN=5m3",
        diameter=1600.0,
        shell_height=2700.0,
        top_plate_thickness=4.0,
        overall_height=2927.0,
        bottom_ring_outer_diameter=1700.0,
        bottom_ring_mid_diameter=1650.0,
        bottom_ring_inner_diameter=1550.0,
        manhole_diameter=450.0,
        shell_thickness=4.0,
        bottom_slope_rise=18.0,
        source_page_image=str(source_page_image),
        view_interpretation=(
            "该 PDF 由主视图、俯视图、A-A 剖视和底部局部详图组成。"
            "主视图负责主体高度、顶盖与侧壁接管标高；俯视图负责 N1~N6、L1、L2、M 的方位角；"
            "A-A 剖视用于确认底部 L50x50x6 加强网格的布置。"
        ),
        analysis_basis=(
            "人工复核得到的关键尺寸包括：筒体直径 φ1600、直壁段高度 2700、总高 2927、"
            "底部圈板直径 φ1700/φ1650/φ1550、顶部人孔 φ450，"
            "以及右下侧口中心标高 200、中部侧口中心标高 1200。"
            "俯视图还明确给出 N1/N2/N3/L2 的中心位于 φ1200 圆上，"
            "M 的中心距罐体中心 500。"
            "右下角件号表给出接管尺寸：N1/N5/N6 为 φ45x3，N2 为 φ38x3，"
            "N3 为 φ32x3，N4/L1 为 φ57x3.5，L2 为 φ89x4；"
            "件号 9 人孔按 DN450 总成处理。"
            "俯视图可见人孔总成外轮廓小于 φ600，因此人孔法兰外径按不超过该包络收敛。"
        ),
        nozzles=[
            NozzleSpec(
                name="N1",
                kind="top_elbowed",
                angle_deg=270.0,
                elevation=2704.0,
                radial_offset=600.0,
                outer_diameter=45.0,
                wall_thickness=3.0,
                outward_length=120.0,
                insert_depth=40.0,
                flange=dn40_flange,
                union_to_shell=False,
            ),
            NozzleSpec(
                name="N2",
                kind="top_vertical",
                angle_deg=0.0,
                elevation=2704.0,
                radial_offset=600.0,
                outer_diameter=38.0,
                wall_thickness=3.0,
                outward_length=120.0,
                insert_depth=40.0,
                flange=dn32_flange,
            ),
            NozzleSpec(
                name="N3",
                kind="top_vertical",
                angle_deg=90.0,
                elevation=2704.0,
                radial_offset=600.0,
                outer_diameter=32.0,
                wall_thickness=3.0,
                outward_length=120.0,
                insert_depth=35.0,
                flange=dn25_flange,
            ),
            NozzleSpec(
                name="N4",
                kind="shell_radial",
                angle_deg=90.0,
                elevation=200.0,
                radial_offset=800.0,
                outer_diameter=57.0,
                wall_thickness=3.5,
                outward_length=120.0,
                insert_depth=35.0,
                flange=dn50_flange,
                union_to_shell=False,
            ),
            NozzleSpec(
                name="N5",
                kind="shell_radial",
                angle_deg=315.0,
                elevation=2400.0,
                radial_offset=800.0,
                outer_diameter=45.0,
                wall_thickness=3.0,
                outward_length=140.0,
                insert_depth=35.0,
                flange=dn40_flange,
                union_to_shell=False,
            ),
            NozzleSpec(
                name="N6",
                kind="shell_radial",
                angle_deg=270.0,
                elevation=87.0,
                radial_offset=800.0,
                outer_diameter=45.0,
                wall_thickness=3.0,
                outward_length=120.0,
                insert_depth=35.0,
                flange=dn40_flange,
                union_to_shell=False,
            ),
            NozzleSpec(
                name="L1",
                kind="shell_radial",
                angle_deg=135.0,
                elevation=240.0,
                radial_offset=800.0,
                outer_diameter=57.0,
                wall_thickness=3.5,
                outward_length=120.0,
                insert_depth=35.0,
                flange=dn50_flange,
                union_to_shell=False,
            ),
            NozzleSpec(
                name="L2",
                kind="top_vertical",
                angle_deg=135.0,
                elevation=2704.0,
                radial_offset=600.0,
                outer_diameter=89.0,
                wall_thickness=4.0,
                outward_length=120.0,
                insert_depth=40.0,
                flange=dn80_flange,
            ),
            NozzleSpec(
                name="M",
                kind="top_manhole",
                angle_deg=180.0,
                elevation=2704.0,
                radial_offset=500.0,
                outer_diameter=450.0,
                wall_thickness=None,
                outward_length=120.0,
                insert_depth=60.0,
                flange=None,
            ),
        ],
    )


def draw_cylinder_with_retry(
    controller: Any,
    *,
    center: tuple[float, float, float],
    radius: float,
    height: float,
    layer: str,
    color: int,
    retries: int = 4,
    wait_seconds: float = 0.8,
) -> Any:
    """Create a cylinder first, then apply style after CAD finishes the solid."""

    last_result = None
    for _ in range(retries):
        last_result = controller.draw_cylinder(center=center, radius=radius, height=height)
        if last_result is not None:
            apply_entity_style(controller, last_result, layer, color)
            return last_result
        time.sleep(wait_seconds)
    return last_result


def add_dimension_with_retry(
    controller: Any,
    *,
    retries: int = 4,
    wait_seconds: float = 0.6,
    **kwargs: Any,
) -> Any:
    """Retry dimensions because CAD often rejects consecutive annotation calls."""

    last_result = None
    for _ in range(retries):
        last_result = controller.add_dimension(**kwargs)
        if last_result is not None:
            return last_result
        time.sleep(wait_seconds)
    return last_result


def rotate_entity_with_retry(
    controller: Any,
    handle: str,
    base_point: tuple[float, float, float],
    angle: float,
    *,
    axis_direction: tuple[float, float, float] | None = None,
    retries: int = 5,
    wait_seconds: float = 0.6,
) -> None:
    """Retry 3D rotation and fail fast if the rotated entity cannot be positioned reliably."""

    for _ in range(retries):
        if controller.rotate_entity(handle, base_point, angle, axis_direction=axis_direction):
            return
        time.sleep(wait_seconds)
    raise RuntimeError(f"实体旋转失败: handle={handle}, angle={angle}")


def create_flange_with_retry(
    controller: Any,
    center: tuple[float, float, float],
    flange: CustomFlange,
    *,
    retries: int = 6,
    wait_seconds: float = 0.8,
) -> Any:
    """Create a flange with extra retries because COM failures are common on repeated solids."""

    last_entity = None
    for _ in range(retries):
        last_entity = draw_flange_with_retry(controller, center, flange)
        if last_entity is not None:
            return last_entity
        time.sleep(wait_seconds)
    return last_entity


def boolean_subtract_with_retry(
    controller: Any,
    main_handle: str,
    tool_handle: str,
    retries: int = 4,
    wait_seconds: float = 0.8,
) -> None:
    """Retry boolean subtraction for hollow nozzle generation."""

    for _ in range(retries):
        if controller.boolean_subtract(main_handle, tool_handle):
            return
        time.sleep(wait_seconds)
    raise RuntimeError(f"布尔减失败: {main_handle} - {tool_handle}")


def draw_pipe_with_retry(
    controller: Any,
    *,
    center: tuple[float, float, float],
    outer_diameter: float,
    wall_thickness: float | None,
    height: float,
    layer: str,
    color: int,
) -> Any:
    """Create a hollow pipe when wall thickness is available."""

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
    if wall_thickness is None or wall_thickness <= 0:
        return outer_solid

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


def draw_directional_pipe_with_retry(
    controller: Any,
    *,
    start_point: tuple[float, float, float],
    direction_vector: tuple[float, float, float],
    outer_diameter: float,
    wall_thickness: float | None,
    length: float,
    layer: str,
    color: int,
) -> Any:
    """Create one hollow pipe from a start point along an arbitrary direction."""

    last_error: Exception | None = None
    for _ in range(4):
        try:
            entity = _add_directional_hollow_cylinder(
                controller,
                start_point=start_point,
                direction_vector=direction_vector,
                outer_radius=outer_diameter / 2.0,
                length=length,
                wall_thickness=float(wall_thickness or 0.0),
            )
            if entity is not None:
                apply_entity_style(controller, entity, layer, color)
                return entity
        except Exception as exc:
            last_error = exc
        time.sleep(0.6)
    if last_error is not None:
        raise RuntimeError(f"任意方向管道创建失败: {last_error}") from last_error
    return None


def orient_flange_with_retry(
    controller: Any,
    handle: str,
    center: tuple[float, float, float],
    axis: str,
) -> None:
    """Rotate a flange from +Z to the requested axis with retries."""

    if axis == "x":
        rotate_entity_with_retry(controller, handle, center, 90.0, axis_direction=(0.0, 1.0, 0.0))
    elif axis == "-x":
        rotate_entity_with_retry(controller, handle, center, -90.0, axis_direction=(0.0, 1.0, 0.0))
    elif axis == "y":
        rotate_entity_with_retry(controller, handle, center, 90.0, axis_direction=(1.0, 0.0, 0.0))
    elif axis == "-y":
        rotate_entity_with_retry(controller, handle, center, -90.0, axis_direction=(1.0, 0.0, 0.0))
    elif axis == "-z":
        rotate_entity_with_retry(controller, handle, center, 180.0, axis_direction=(1.0, 0.0, 0.0))


def draw_top_plate(service: CADService, params: TankParameters, shell_handle: str) -> None:
    """Draw and merge the flat top plate."""

    top_plate = draw_cylinder_with_retry(
        service.controller,
        center=(0.0, 0.0, params.shell_height),
        radius=params.diameter / 2.0,
        height=params.top_plate_thickness,
        layer="VESSEL",
        color=COLOR_WHITE,
    )
    if top_plate is None:
        raise RuntimeError("顶盖创建失败。")
    boolean_union_with_retry(service.controller, shell_handle, entity_handle(top_plate))


def draw_bottom_ring(service: CADService, diameter: float, z: float, height: float, shell_handle: str) -> None:
    """Draw one concentric bottom ring and merge it into the shell."""

    ring = draw_cylinder_with_retry(
        service.controller,
        center=(0.0, 0.0, z),
        radius=diameter / 2.0,
        height=height,
        layer="BOTTOM",
        color=COLOR_WHITE,
    )
    if ring is None:
        raise RuntimeError(f"底部圈板创建失败: {diameter}")
    boolean_union_with_retry(service.controller, shell_handle, entity_handle(ring))


def draw_bottom_reinforcement(service: CADService) -> None:
    """Add the L50x50x6 style reinforcement grid visible in section A-A."""

    grid_z = 60.0
    bar_height = 50.0
    bar_width = 50.0
    long_span = 1480.0
    short_span = 1120.0
    for x_value in (-375.0, 0.0, 375.0):
        bar = draw_box_with_retry(
            service.controller,
            center=(x_value, 0.0, grid_z),
            length=bar_width,
            width=long_span,
            height=bar_height,
            layer="INTERNAL",
            color=COLOR_WHITE,
        )
        if bar is None:
            raise RuntimeError("底部竖向加强创建失败。")
    for y_value in (-375.0, 0.0, 375.0):
        bar = draw_box_with_retry(
            service.controller,
            center=(0.0, y_value, grid_z),
            length=short_span,
            width=bar_width,
            height=bar_height,
            layer="INTERNAL",
            color=COLOR_WHITE,
        )
        if bar is None:
            raise RuntimeError("底部横向加强创建失败。")


def draw_top_lugs(service: CADService, params: TankParameters) -> None:
    """Add the two lifting lugs shown in the top view."""

    lug_radius = params.diameter / 2.0 + 35.0
    lug_z = params.shell_height + 55.0
    for angle_deg in (45.0, 225.0):
        center = tank_polar_point(lug_radius, angle_deg, lug_z)
        lug = draw_box_with_retry(
            service.controller,
            center=center,
            length=120.0,
            width=12.0,
            height=110.0,
            layer="ACCESSORY",
            color=COLOR_WHITE,
        )
        if lug is None:
            raise RuntimeError("吊耳创建失败。")
        handle = entity_handle(lug)
        rotate_entity_with_retry(service.controller, handle, center, 90.0 - angle_deg, axis_direction=(0.0, 0.0, 1.0))


def draw_vertical_nozzle(service: CADService, shell_handle: str, nozzle: NozzleSpec) -> None:
    """Draw a vertical nozzle on the roof."""

    if nozzle.angle_deg is None:
        raise RuntimeError(f"{nozzle.name} 缺少方位角。")
    controller = service.controller
    center = tank_polar_point(nozzle.radial_offset, nozzle.angle_deg, nozzle.elevation)
    solid = draw_pipe_with_retry(
        controller,
        center=(center[0], center[1], nozzle.elevation - nozzle.insert_depth),
        outer_diameter=nozzle.outer_diameter,
        wall_thickness=nozzle.wall_thickness,
        height=nozzle.outward_length + nozzle.insert_depth,
        layer=nozzle.layer,
        color=nozzle.color,
    )
    if solid is None:
        raise RuntimeError(f"顶部口件创建失败: {nozzle.name}")
    if nozzle.union_to_shell:
        boolean_union_with_retry(controller, shell_handle, entity_handle(solid))
    if nozzle.flange is None:
        return
    flange_base = (center[0], center[1], nozzle.elevation + max(nozzle.outward_length - nozzle.flange_seat_overlap, 0.0))
    flange_entity = create_flange_with_retry(controller, flange_base, nozzle.flange)
    if flange_entity is None:
        raise RuntimeError(f"顶部口件法兰创建失败: {nozzle.name}")
    apply_entity_style(controller, flange_entity, "FLANGE", COLOR_WHITE)


def draw_top_elbowed_nozzle(service: CADService, shell_handle: str, nozzle: NozzleSpec, shell_radius: float) -> None:
    """Draw the N1-style roof nozzle with a visible elbow under the flange."""

    if nozzle.angle_deg is None:
        raise RuntimeError(f"{nozzle.name} 缺少方位角。")
    controller = service.controller
    root_half = math.sqrt(0.5)
    bend_radius = nozzle.outer_diameter * 1.5
    roof_center = tank_polar_point(nozzle.radial_offset, nozzle.angle_deg, nozzle.elevation)
    roof_z = nozzle.elevation
    # 用户已确认当前弯头连接关系可接受，本轮只缩短第二根接管，
    # 避免它继续过长地下探穿出水罐。
    lower_run_projected_drop = 90.0

    # 第一根接管必须一直延伸到罐顶；在此基础上，允许用局部空间落位
    # 去收紧弯头与第二根接管的连接，而不是再缩短第一根接管。
    upper_vertical_tangent = roof_center
    elbow_center = (
        upper_vertical_tangent[0] - bend_radius,
        roof_center[1],
        upper_vertical_tangent[2],
    )
    lower_run_tangent = (
        elbow_center[0] + bend_radius * root_half,
        elbow_center[1],
        elbow_center[2] - bend_radius * root_half,
    )
    flange_base = (
        roof_center[0],
        roof_center[1],
        nozzle.elevation + max(nozzle.outward_length - nozzle.flange_seat_overlap, 0.0),
    )
    upper_vertical_length = flange_base[2] - upper_vertical_tangent[2]
    if upper_vertical_length <= 0:
        raise RuntimeError(f"顶部弯接管立管长度非法: {nozzle.name}")

    lower_run_length = lower_run_projected_drop / root_half
    if lower_run_length <= 0:
        raise RuntimeError(f"顶部弯接管下部接管长度非法: {nozzle.name}")

    lower_run = draw_directional_pipe_with_retry(
        controller,
        start_point=lower_run_tangent,
        direction_vector=(-root_half, 0.0, -root_half),
        outer_diameter=nozzle.outer_diameter,
        wall_thickness=nozzle.wall_thickness,
        length=lower_run_length,
        layer=nozzle.layer,
        color=nozzle.color,
    )
    if lower_run is None:
        raise RuntimeError(f"顶部弯接管下部接管创建失败: {nozzle.name}")

    upper_vertical = draw_directional_pipe_with_retry(
        controller,
        start_point=upper_vertical_tangent,
        direction_vector=(0.0, 0.0, 1.0),
        outer_diameter=nozzle.outer_diameter,
        wall_thickness=nozzle.wall_thickness,
        length=upper_vertical_length,
        layer=nozzle.layer,
        color=nozzle.color,
    )
    if upper_vertical is None:
        raise RuntimeError(f"顶部弯接管上部竖管创建失败: {nozzle.name}")
    if nozzle.union_to_shell:
        boolean_union_with_retry(controller, shell_handle, entity_handle(lower_run))

    elbow = controller.draw_pipe_elbow(
        center=elbow_center,
        pipe_radius=nozzle.outer_diameter / 2.0,
        elbow_type="LR",
        angle=45,
        plane="xy",
        wall_thickness=nozzle.wall_thickness,
        layer=nozzle.layer,
        color=nozzle.color,
    )
    if elbow is None:
        raise RuntimeError(f"顶部弯接管弯头创建失败: {nozzle.name}")
    elbow_handle = entity_handle(elbow)
    rotate_entity_with_retry(controller, elbow_handle, elbow_center, 90.0, axis_direction=(0.0, 1.0, 0.0))
    rotate_entity_with_retry(
        controller,
        elbow_handle,
        elbow_center,
        nozzle.angle_deg,
        axis_direction=(0.0, 0.0, 1.0),
    )
    flange_entity = create_flange_with_retry(controller, flange_base, nozzle.flange)
    if flange_entity is None:
        raise RuntimeError(f"顶部弯接管法兰创建失败: {nozzle.name}")
    apply_entity_style(controller, flange_entity, "FLANGE", COLOR_WHITE)


def draw_top_manhole(service: CADService, shell_handle: str, nozzle: NozzleSpec) -> None:
    """Draw the roof manhole using the standard manhole family helper."""

    if nozzle.angle_deg is None:
        raise RuntimeError(f"{nozzle.name} 缺少方位角。")
    controller = service.controller
    center = tank_polar_point(nozzle.radial_offset, nozzle.angle_deg, nozzle.elevation - nozzle.insert_depth)
    entity = controller.draw_manhole(
        center=center,
        specification="HG/T 21517",
        dn=int(round(nozzle.outer_diameter)),
        pn=1.0,
        layer=nozzle.layer,
        color=nozzle.color,
    )
    if entity is None:
        raise RuntimeError(f"顶部人孔创建失败: {nozzle.name}")


def draw_radial_nozzle(service: CADService, shell_handle: str, nozzle: NozzleSpec, shell_radius: float) -> None:
    """Draw a radial nozzle by creating it on +X and rotating it to the drawing azimuth."""

    if nozzle.angle_deg is None:
        raise RuntimeError(f"{nozzle.name} 缺少方位角。")
    controller = service.controller
    radial_base = nozzle.radial_offset if nozzle.radial_offset > 0 else shell_radius
    insert_depth = nozzle.insert_depth if nozzle.union_to_shell else 0.0
    total_length = nozzle.outward_length + insert_depth
    base_point = (radial_base - insert_depth, 0.0, nozzle.elevation)
    solid = draw_pipe_with_retry(
        controller,
        center=base_point,
        outer_diameter=nozzle.outer_diameter,
        wall_thickness=nozzle.wall_thickness,
        height=total_length,
        layer=nozzle.layer,
        color=nozzle.color,
    )
    if solid is None:
        raise RuntimeError(f"侧向口件创建失败: {nozzle.name}")
    solid_handle = entity_handle(solid)
    rotate_entity_with_retry(controller, solid_handle, base_point, 90.0, axis_direction=(0.0, 1.0, 0.0))
    rotate_entity_with_retry(controller, solid_handle, (0.0, 0.0, nozzle.elevation), 90.0 - nozzle.angle_deg, axis_direction=(0.0, 0.0, 1.0))
    if nozzle.union_to_shell:
        boolean_union_with_retry(controller, shell_handle, solid_handle)

    if nozzle.flange is None:
        return
    flange_base = (
        radial_base + max(nozzle.outward_length - nozzle.flange_seat_overlap, 0.0),
        0.0,
        nozzle.elevation,
    )
    flange_entity = create_flange_with_retry(controller, flange_base, nozzle.flange)
    if flange_entity is None:
        raise RuntimeError(f"侧向口件法兰创建失败: {nozzle.name}")
    flange_handle = entity_handle(flange_entity)
    orient_flange_with_retry(controller, flange_handle, flange_base, "x")
    rotate_entity_with_retry(controller, flange_handle, (0.0, 0.0, nozzle.elevation), 90.0 - nozzle.angle_deg, axis_direction=(0.0, 0.0, 1.0))
    apply_entity_style(controller, flange_entity, "FLANGE", COLOR_WHITE)


def add_dimensions(service: CADService, params: TankParameters) -> None:
    """Reproduce key source dimensions in the front view."""

    add_dimension_with_retry(
        service.controller,
        start_point=(-params.diameter / 2.0, 0.0, params.shell_height / 2.0),
        end_point=(params.diameter / 2.0, 0.0, params.shell_height / 2.0),
        center=(0.0, 0.0, params.shell_height / 2.0),
        chord_point=(-params.diameter / 2.0, 0.0, params.shell_height / 2.0),
        far_chord_point=(params.diameter / 2.0, 0.0, params.shell_height / 2.0),
        textheight=28.0,
        dimension_type="diameter",
        source_view="front",
    )
    for offset_index, diameter in enumerate(
        [
            params.bottom_ring_outer_diameter,
            params.bottom_ring_mid_diameter,
            params.bottom_ring_inner_diameter,
            params.manhole_diameter,
        ]
    ):
        z_value = -120.0 - offset_index * 45.0 if diameter != params.manhole_diameter else params.shell_height + 75.0
        add_dimension_with_retry(
            service.controller,
            start_point=(-diameter / 2.0, 0.0, z_value),
            end_point=(diameter / 2.0, 0.0, z_value),
            center=(0.0, 0.0, z_value),
            chord_point=(-diameter / 2.0, 0.0, z_value),
            far_chord_point=(diameter / 2.0, 0.0, z_value),
            textheight=24.0,
            dimension_type="diameter",
            source_view="front",
        )

    linear_dimensions = [
        ((0.0, 0.0, 0.0), (0.0, 0.0, params.overall_height), (-980.0, 0.0, params.overall_height / 2.0)),
        ((0.0, 0.0, 0.0), (0.0, 0.0, params.shell_height), (-860.0, 0.0, params.shell_height / 2.0)),
        ((params.diameter / 2.0, 0.0, 0.0), (params.diameter / 2.0, 0.0, 1200.0), (980.0, 0.0, 600.0)),
        ((params.diameter / 2.0, 0.0, 0.0), (params.diameter / 2.0, 0.0, 200.0), (880.0, 0.0, 100.0)),
    ]
    for start_point, end_point, text_point in linear_dimensions:
        add_dimension_with_retry(
            service.controller,
            start_point=start_point,
            end_point=end_point,
            text_position=text_point,
            textheight=24.0,
            dimension_type="vertical" if start_point[0] == end_point[0] else "horizontal",
            source_view="front",
        )


def build_model(service: CADService, params: TankParameters) -> None:
    """Build the raw water tank model from structured parameters."""

    shell = draw_cylinder_with_retry(
        service.controller,
        center=(0.0, 0.0, 0.0),
        radius=params.diameter / 2.0,
        height=params.shell_height,
        layer="VESSEL",
        color=COLOR_WHITE,
    )
    if shell is None:
        raise RuntimeError("筒体创建失败。")
    shell_handle = entity_handle(shell)

    draw_top_plate(service, params, shell_handle)
    draw_bottom_ring(service, params.bottom_ring_outer_diameter, -8.0, 8.0, shell_handle)
    draw_bottom_ring(service, params.bottom_ring_mid_diameter, -4.0, 4.0, shell_handle)
    draw_bottom_ring(service, params.bottom_ring_inner_diameter, 0.0, 4.0, shell_handle)
    draw_top_lugs(service, params)

    for nozzle in params.nozzles:
        if nozzle.kind == "top_vertical":
            draw_vertical_nozzle(service, shell_handle, nozzle)
        elif nozzle.kind == "top_manhole":
            draw_top_manhole(service, shell_handle, nozzle)
        elif nozzle.kind == "top_elbowed":
            draw_top_elbowed_nozzle(service, shell_handle, nozzle, shell_radius=params.diameter / 2.0)
        else:
            draw_radial_nozzle(service, shell_handle, nozzle, shell_radius=params.diameter / 2.0)

    zoom_extents_with_retry(service.controller)


def capture_previews(service: CADService, version_dir: Path, version: int, params: TankParameters) -> list[str]:
    """Capture front, top and isometric previews for visual verification."""

    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    front_target = (0.0, 0.0, params.shell_height / 2.0 - 120.0)
    top_target = (0.0, 0.0, params.shell_height / 2.0)
    iso_target = (0.0, 0.0, params.shell_height / 2.0 - 120.0)
    n1_local_target = tank_polar_point(600.0, 270.0, 2704.0)
    m_local_target = tank_polar_point(500.0, 180.0, 2704.0 + 80.0)
    view_specs = [
        ("front", front_target, 5600.0, 4200.0, f"{OUTPUT_TOPIC}_front_preview_v{version}.png"),
        ("top", top_target, 3000.0, 3000.0, f"{OUTPUT_TOPIC}_top_preview_v{version}.png"),
        ("swiso", iso_target, 7000.0, 7000.0, f"{OUTPUT_TOPIC}_iso_preview_v{version}.png"),
        ("front", n1_local_target, 900.0, 1200.0, f"{OUTPUT_TOPIC}_n1_local_front_preview_v{version}.png"),
        ("swiso", n1_local_target, 1400.0, 1800.0, f"{OUTPUT_TOPIC}_n1_local_iso_preview_v{version}.png"),
        ("front", m_local_target, 950.0, 1200.0, f"{OUTPUT_TOPIC}_m_local_front_preview_v{version}.png"),
        ("swiso", m_local_target, 1400.0, 1800.0, f"{OUTPUT_TOPIC}_m_local_iso_preview_v{version}.png"),
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
    dwg_path: Path,
    preview_paths: list[str],
    shared_source_page_path: Path,
) -> None:
    """Persist parameters and notes alongside the DWG."""

    parameters_path = version_dir / f"{OUTPUT_TOPIC}_parameters_v{version}.json"
    notes_path = version_dir / f"{OUTPUT_TOPIC}_notes_v{version}.txt"

    with parameters_path.open("w", encoding="utf-8") as file_obj:
        json.dump(
            {
                "parameters": {
                    **asdict(params),
                    "nozzles": [asdict(item) for item in params.nozzles],
                },
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
        f"视图判定: {params.view_interpretation}",
        f"参数依据: {params.analysis_basis}",
        "",
        "归档脚本:",
        "",
        "本轮说明:",
        "- 建模主体按主视图 DN1600、直壁段 2700、总高 2927 落图。",
        "- 口件方位按俯视图 0/90/180/270/315/135 度方位布置。",
        "- A-A 剖视里的底部 L50x50x6 加强网格以简化实心条方式表达。",
        "- 底部仅表达为分级圈板与内部加强，未复现薄板级别的小坡度细节。",
        "- 按当前用户要求，本轮默认不添加尺寸标注。",
        "- 已输出 front/top/iso 以及 N1 局部预览图用于复核。",
    ]
    notes_path.write_text("\n".join(notes), encoding="utf-8")

    if PDF_PATH.exists():
        copy_if_missing(PDF_PATH, SHARED_DIR / PDF_PATH.name)
    copy_if_missing(Path(params.source_page_image), SHARED_DIR / f"{OUTPUT_TOPIC}_parser_source_page1.png")
    copy_if_missing(shared_source_page_path, SHARED_DIR / shared_source_page_path.name)


def main() -> int:
    """Entry point for the raw water tank scripted model."""

    OUTPUT_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    version, version_dir = next_version_dir(OUTPUT_ROOT_DIR)
    dwg_path = version_dir / f"{OUTPUT_TOPIC}_3D_v{version}.dwg"
    shared_source_page_path = SHARED_DIR / f"{OUTPUT_TOPIC}_source_page1.png"
    if PDF_PATH.exists():
        shared_source_page_path = render_source_page(shared_source_page_path)
    elif not shared_source_page_path.exists():
        raise FileNotFoundError(f"未找到源 PDF，也未找到已归档源页: {PDF_PATH}")
    params = build_parameters(shared_source_page_path)

    service = CADService()
    ensure_drawable_document(service)
    build_model(service, params)

    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    preview_paths = capture_previews(service, version_dir, version, params)
    write_artifacts(
        version_dir,
        version,
        params,
        dwg_path,
        preview_paths,
        shared_source_page_path,
    )
    print(
        json.dumps(
            {
                "success": True,
                "version": version,
                "version_dir": str(version_dir),
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
