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

from drawing_validation import PreviewArtifact, ValidationContext, merge_reports, run_validation
from pipeline_models import BomClosureEntry, DimensionLedgerEntry, RecognitionRegion, SectionDetail, ViewInterpretation
from screen_capture import ScreenCapture
from server import CADService

OUTPUT_TOPIC = "压缩空气储罐"
PDF_PATH = Path(r"f:\毕业论文\管道布置图\PDF\PDF\压缩空气储罐.pdf")
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
SHARED_DIR = OUTPUT_ROOT_DIR / "shared"

COLOR_WHITE = 7
COLOR_MAGENTA = 6
COLOR_YELLOW = 2
COLOR_CYAN = 4

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
    outer_diameter: float
    center_hole_diameter: float
    bolt_circle_diameter: float
    bolt_hole_diameter: float
    bolt_count: int
    thickness: float


@dataclass
class BlindAssembly:
    blind_thickness: float
    blind_outer_diameter: float
    bolt_circle_diameter: float
    bolt_hole_diameter: float
    bolt_count: int
    washer_inner_diameter: float
    washer_outer_diameter: float
    washer_thickness: float
    bolt_length: float
    shank_diameter: float
    nut_thickness: float
    nut_width: float
    show_fasteners: bool = False


@dataclass
class NozzleSpec:
    name: str
    connect_center: tuple[float, float, float]
    axis: str
    outer_diameter: float
    wall_thickness: float
    outward_length: float
    insert_depth: float
    flange: CustomFlange | None
    layer: str = "NOZZLE"
    color: int = COLOR_WHITE
    union_to_shell: bool = True
    display_base_offset: float = 0.0
    flange_seat_overlap: float = 6.0
    blind_assembly: BlindAssembly | None = None
    flange_axis: str | None = None
    position_basis: dict[str, Any] | None = None


@dataclass
class ItemClosure:
    item_no: int
    name: str
    quantity: int
    spec: str
    status: str
    note: str


@dataclass
class TankParameters:
    drawing_name: str
    shell_outer_diameter: float
    shell_straight_height: float
    shell_bottom_z: float
    support_height: float
    base_plate_thickness: float
    support_span: float
    overall_height: float
    source_page_image: str
    view_interpretation: str
    analysis_basis: str
    uncertainties: list[str]
    support_angles_deg: list[float]
    lug_angles_deg: list[float]
    lug_line_angle_deg: float
    recognition_regions: list[RecognitionRegion]
    view_interpretations: list[ViewInterpretation]
    dimension_ledger: list[DimensionLedgerEntry]
    bom_closure: list[BomClosureEntry]
    section_details: list[SectionDetail]
    nozzles: list[NozzleSpec]
    item_closure: list[ItemClosure]


def next_version_dir(output_root: Path) -> tuple[int, Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    max_version = 0
    for item in output_root.iterdir():
        if item.is_dir() and item.name.startswith("v") and item.name[1:].isdigit():
            max_version = max(max_version, int(item.name[1:]))
    version = max_version + 1
    version_dir = output_root / f"v{version}"
    version_dir.mkdir(parents=True, exist_ok=False)
    return version, version_dir


def copy_if_missing(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def render_source_page(destination: Path, dpi: int = 350) -> Path:
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


def create_fresh_document(service: CADService) -> None:
    if not service.start_cad():
        raise RuntimeError("无法启动或连接 CAD。")
    docs = getattr(service.controller.app, "Documents", None)
    if docs is None:
        raise RuntimeError("CAD 未暴露 Documents 集合。")
    docs.Add()
    time.sleep(3.5)
    service.controller.doc = service.controller.app.ActiveDocument
    service.drawing_state["entities"] = []


def apply_entity_style(controller: Any, entity: Any, layer: str, color: int) -> None:
    for _ in range(3):
        try:
            controller.create_layer(layer)
            entity.Layer = layer
            entity.Color = color
            return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(f"实体样式设置失败: {layer}")


def draw_box_with_retry(controller: Any, *, center: tuple[float, float, float], length: float, width: float, height: float, layer: str, color: int) -> Any:
    last_result = None
    for _ in range(4):
        last_result = controller.draw_box(center=center, length=length, width=width, height=height, layer=layer, color=color)
        if last_result is not None:
            return last_result
        time.sleep(0.8)
    return last_result


def draw_flange_with_retry(controller: Any, center: tuple[float, float, float], flange: CustomFlange) -> Any:
    last_result = None
    for _ in range(4):
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
        time.sleep(0.8)
    return last_result


def draw_blind_flange_with_retry(controller: Any, center: tuple[float, float, float], assembly: BlindAssembly) -> Any:
    last_result = None
    for _ in range(4):
        last_result = controller.draw_blind_flange(
            center,
            outer_diameter=assembly.blind_outer_diameter,
            bolt_circle_diameter=assembly.bolt_circle_diameter,
            bolt_hole_diameter=assembly.bolt_hole_diameter,
            bolt_count=assembly.bolt_count,
            thickness=assembly.blind_thickness,
        )
        if last_result is not None:
            return last_result
        time.sleep(0.8)
    return last_result


def draw_elliptical_head_with_retry(controller: Any, *, center: tuple[float, float, float], dn: float, axis: str, layer: str, color: int) -> Any:
    last_result = None
    for _ in range(4):
        try:
            last_result = controller.draw_elliptical_head(center, dn=dn, axis=axis, with_straight=True, layer=None, color=None)
            if last_result is not None:
                apply_entity_style(controller, last_result, layer, color)
                return last_result
        except Exception:
            pass
        time.sleep(0.8)
    return last_result


def draw_washer_with_retry(controller: Any, *, center: tuple[float, float, float], inner_diameter: float, outer_diameter: float, thickness: float, axis: str, layer: str, color: int) -> Any:
    last_result = None
    for _ in range(4):
        try:
            last_result = controller.draw_washer(
                center,
                inner_diameter,
                outer_diameter=outer_diameter,
                thickness=thickness,
                axis=axis,
                layer=layer,
                color=color,
            )
            if last_result is not None:
                return last_result
        except Exception:
            pass
        time.sleep(0.8)
    return last_result


def draw_bolt_with_retry(controller: Any, *, base_center: tuple[float, float, float], shank_diameter: float, length: float, axis: str, head_width: float, head_height: float, layer: str, color: int) -> Any:
    last_result = None
    for _ in range(4):
        try:
            last_result = controller.draw_bolt(
                base_center,
                shank_diameter,
                length,
                axis=axis,
                head_width=head_width,
                head_height=head_height,
                layer=layer,
                color=color,
            )
            if last_result is not None:
                return last_result
        except Exception:
            pass
        time.sleep(0.8)
    return last_result


def draw_hex_nut_with_retry(controller: Any, *, center: tuple[float, float, float], inner_diameter: float, thickness: float, width_across_flats: float, axis: str, layer: str, color: int) -> Any:
    last_result = None
    for _ in range(4):
        try:
            last_result = controller.draw_hex_nut(
                center,
                inner_diameter,
                thickness=thickness,
                width_across_flats=width_across_flats,
                axis=axis,
                layer=layer,
                color=color,
            )
            if last_result is not None:
                return last_result
        except Exception:
            pass
        time.sleep(0.8)
    return last_result


def entity_handle(entity: Any) -> str:
    for _ in range(5):
        try:
            return str(entity.Handle)
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("实体句柄读取失败。")


def boolean_union_with_retry(controller: Any, primary_handle: str, secondary_handle: str) -> None:
    for _ in range(4):
        try:
            if controller.boolean_union(primary_handle, secondary_handle):
                return
        except Exception:
            pass
        time.sleep(0.8)
    raise RuntimeError(f"布尔并集失败: {primary_handle}, {secondary_handle}")


def boolean_subtract_with_retry(controller: Any, primary_handle: str, tool_handle: str) -> None:
    for _ in range(4):
        try:
            if controller.boolean_subtract(primary_handle, tool_handle):
                return
        except Exception:
            pass
        time.sleep(0.8)
    raise RuntimeError(f"布尔差集失败: {primary_handle}, {tool_handle}")


def rotate_entity_with_retry(controller: Any, handle: str, base_point: tuple[float, float, float], angle: float, axis_direction: tuple[float, float, float]) -> None:
    for _ in range(4):
        try:
            if controller.rotate_entity(handle, base_point, angle, axis_direction=axis_direction):
                return
        except Exception:
            pass
        time.sleep(0.8)
    raise RuntimeError(f"旋转失败: {handle}")


def zoom_extents_with_retry(controller: Any) -> None:
    for _ in range(4):
        try:
            if controller.zoom_extents():
                return
        except Exception:
            pass
        time.sleep(0.8)
    raise RuntimeError("缩放范围失败。")


def set_named_view_with_retry(controller: Any, view_name: str, *, target: tuple[float, float, float], height: float, width: float) -> None:
    for _ in range(4):
        try:
            if controller.set_named_view(view_name, target=target, center=(0.0, 0.0), height=height, width=width):
                return
        except Exception:
            pass
        time.sleep(0.8)
    raise RuntimeError(f"视图切换失败: {view_name}")


def save_drawing_with_retry(controller: Any, path: str) -> bool:
    for _ in range(4):
        try:
            if controller.save_drawing(path):
                return True
        except Exception:
            pass
        time.sleep(1.2)
    return False


def tank_polar_point(radius: float, angle_deg: float, z: float) -> tuple[float, float, float]:
    angle_rad = math.radians(angle_deg)
    return (radius * math.sin(angle_rad), radius * math.cos(angle_rad), z)


def axis_offset(point: tuple[float, float, float], axis: str, distance: float) -> tuple[float, float, float]:
    dx, dy, dz = AXIS_VECTORS[axis]
    return (point[0] + dx * distance, point[1] + dy * distance, point[2] + dz * distance)


def build_recognition_regions() -> list[RecognitionRegion]:
    return [
        RecognitionRegion(
            region_id="main-view",
            region_type="main_view",
            page_number=1,
            source_view="front",
            label="主视图",
            description="读取筒体外径、总高、支腿高度、接管标高和上下封头关系。",
            extracted_fields=["shell_outer_diameter", "overall_height", "support_height", "nozzle_elevation"],
            metadata={"source": "manual-review"},
        ),
        RecognitionRegion(
            region_id="top-view",
            region_type="top_view",
            page_number=1,
            source_view="top",
            label="俯视图",
            description="读取 N1/N2/N3/N4/N5/P 的周向方位、支腿方位、吊耳和铭牌座位置。",
            extracted_fields=["nozzle_angle", "support_angle", "lug_angle", "nameplate_angle"],
            metadata={"source": "manual-review"},
        ),
        RecognitionRegion(
            region_id="bom-table",
            region_type="bom_table",
            page_number=1,
            label="件号表",
            description="读取编号件、数量、规格和建模闭环状态。",
            extracted_fields=["item_no", "quantity", "spec", "status"],
            metadata={"source": "manual-review"},
        ),
        RecognitionRegion(
            region_id="detail-view",
            region_type="detail_view",
            page_number=1,
            label="接管详图",
            description="读取法兰型式、口径、厚度和盲板总成信息。",
            extracted_fields=["flange_type", "bolt_circle", "flange_thickness", "blind_assembly"],
            metadata={"source": "manual-review"},
        ),
    ]


def build_view_interpretations() -> list[ViewInterpretation]:
    return [
        ViewInterpretation(
            view_id="front",
            source_view="主视图",
            canonical_role="front",
            confidence=0.95,
            evidence=["主视图标签", "主体高度与标高尺寸"],
            region_id="main-view",
            metadata={"source": "manual-review"},
        ),
        ViewInterpretation(
            view_id="top",
            source_view="俯视图",
            canonical_role="top",
            confidence=0.95,
            evidence=["俯视图标签", "口件角度与支腿方位"],
            region_id="top-view",
            metadata={"source": "manual-review"},
        ),
    ]


def build_dimension_ledger(
    shell_outer_diameter: float,
    shell_straight_height: float,
    shell_bottom_z: float,
    support_height: float,
    support_span: float,
    overall_height: float,
    lower_nozzle_elevation: float,
    upper_nozzle_elevation: float,
    top_head_nozzle_z: float,
) -> list[DimensionLedgerEntry]:
    return [
        DimensionLedgerEntry(
            dimension_id="D-OD",
            dimension_type="diameter",
            source_view="front",
            value=shell_outer_diameter,
            unit="mm",
            applies_to=["shell.outer_diameter"],
            used_by_model=True,
            confidence=0.95,
            verified=True,
            is_key=True,
            source_region="main-view",
            source_text="DN800",
            metadata={"category": "diameter"},
        ),
        DimensionLedgerEntry(
            dimension_id="D-SHELL-H",
            dimension_type="vertical",
            source_view="front",
            value=shell_straight_height,
            unit="mm",
            applies_to=["shell.straight_height"],
            used_by_model=True,
            confidence=0.95,
            verified=True,
            is_key=True,
            source_region="main-view",
            source_text="筒体直段 H=1800",
            metadata={"category": "height"},
        ),
        DimensionLedgerEntry(
            dimension_id="D-SUPPORT-H",
            dimension_type="vertical",
            source_view="front",
            value=support_height,
            unit="mm",
            applies_to=["support.height"],
            used_by_model=True,
            confidence=0.9,
            verified=True,
            is_key=True,
            source_region="main-view",
            source_text="支腿 H=830",
            metadata={"category": "height"},
        ),
        DimensionLedgerEntry(
            dimension_id="D-SUPPORT-SPAN",
            dimension_type="horizontal",
            source_view="front",
            value=support_span,
            unit="mm",
            applies_to=["support.span"],
            used_by_model=True,
            confidence=0.9,
            verified=True,
            is_key=True,
            source_region="main-view",
            source_text="支腿外跨 792",
            metadata={"category": "spacing"},
        ),
        DimensionLedgerEntry(
            dimension_id="D-OVERALL-H",
            dimension_type="vertical",
            source_view="front",
            value=overall_height,
            unit="mm",
            applies_to=["vessel.overall_height"],
            used_by_model=True,
            confidence=0.95,
            verified=True,
            is_key=True,
            source_region="main-view",
            source_text="总高 2956",
            metadata={"category": "overall"},
        ),
        DimensionLedgerEntry(
            dimension_id="D-N1-Z",
            dimension_type="vertical",
            source_view="front",
            value=lower_nozzle_elevation - shell_bottom_z,
            unit="mm",
            applies_to=["nozzle.N1.z", "nozzle.N5.z"],
            used_by_model=True,
            confidence=0.88,
            verified=True,
            is_key=True,
            source_region="main-view",
            source_text="N1/N5 标高复核",
            metadata={"category": "height"},
        ),
        DimensionLedgerEntry(
            dimension_id="D-N2-Z",
            dimension_type="vertical",
            source_view="front",
            value=upper_nozzle_elevation - shell_bottom_z,
            unit="mm",
            applies_to=["nozzle.N2.z"],
            used_by_model=True,
            confidence=0.88,
            verified=True,
            is_key=True,
            source_region="main-view",
            source_text="N2 标高复核",
            metadata={"category": "height"},
        ),
        DimensionLedgerEntry(
            dimension_id="D-N3-TOP",
            dimension_type="vertical",
            source_view="front",
            value=top_head_nozzle_z,
            unit="mm",
            applies_to=["nozzle.N3.z", "nozzle.N4.z", "nozzle.P.z"],
            used_by_model=True,
            confidence=0.88,
            verified=True,
            is_key=True,
            source_region="main-view",
            source_text="顶部口件标高 2856",
            metadata={"category": "height"},
        ),
    ]


def build_bom_closure(item_closure: list[ItemClosure]) -> list[BomClosureEntry]:
    decoded_specs = {
        2: {"outer_diameter": 45.0, "wall_thickness": 4.0},
        3: {"dn": 40, "pn": 16, "type": "WN", "seat_thickness": 16.0},
        5: {"outer_diameter": 57.0, "wall_thickness": 3.5},
        6: {"dn": 50, "pn": 16, "type": "WN", "seat_thickness": 18.0},
        8: {"outer_diameter": 32.0, "wall_thickness": 3.0},
        9: {"dn": 25, "pn": 16, "type": "WN", "seat_thickness": 16.0},
        10: {"dn": 25, "pn": 16, "type": "blind_flange"},
    }
    modeled_as = {
        1: "support_leg_assembly",
        2: "N5_nozzle",
        3: "N5_flange",
        4: "elliptical_heads",
        5: "N1_N2_N3_N4_nozzles",
        6: "N1_N2_N3_N4_flanges",
        7: "shell_body",
        8: "P_nozzle",
        9: "P_flange",
        10: "P_blind_flange",
        14: "lifting_lugs",
        15: "nameplate_seat",
    }
    bom_entries: list[BomClosureEntry] = []
    for item in item_closure:
        status = "completed" if item.status == "modeled" else item.status
        review_status = "verified" if item.status in {"modeled", "hidden_by_request"} else "pending"
        exception_reason = "" if item.status == "modeled" else item.note
        bom_entries.append(
            BomClosureEntry(
                item_no=item.item_no,
                name=item.name,
                quantity=item.quantity,
                spec=item.spec,
                material="按图纸件号表人工复核",
                standard="待补件号表标准字段",
                decoded_params=decoded_specs.get(item.item_no, {}),
                status=item.status,
                model_status=status,
                review_status=review_status,
                exception_reason=exception_reason,
                modeled_as=modeled_as.get(item.item_no, ""),
                note=item.note,
                metadata={"source_region": "bom-table"},
            )
        )
    return bom_entries


def build_section_details() -> list[SectionDetail]:
    return [
        SectionDetail(
            section_id="section-weld-a-b",
            section_type="weld_detail",
            title="A.B 接管焊接详图",
            source_view="front",
            region_id="detail-view",
            purpose="用于读取接管与壳体连接、法兰根部和局部焊接表达。",
            cut_line_description="局部详图，不是完整剖切视图，主要用于补充接管根部与焊接关系。",
            view_direction="按详图局部放大方向读取，不单独作为整机剖视方向依据。",
            removed_side="不适用",
            retained_side="不适用",
            projection_basis="依据主视图接管位置与局部详图标注共同判断。",
            related_items=["N1", "N2", "N3", "N4", "N5", "P"],
            extracted_fields=["insert_depth", "flange_fit_up", "weld_relation"],
            modeling_notes=[
                "3D 建模时优先用来确认接管向壳体内的吃入量。",
                "法兰与接管连接面应按详图关系贴合，避免留缝。",
            ],
            confidence=0.88,
            direction_confidence=0.7,
            metadata={"source": "manual-review"},
        ),
        SectionDetail(
            section_id="section-radial-nozzle",
            section_type="section_view",
            title="径向/非径向接管剖面关系",
            source_view="front",
            region_id="section-view",
            purpose="用于读取径向与非径向接管的剖面关系和局部连接方向。",
            cut_line_description="剖切线应回到主视图中的径向/非径向接管位置确认，重点看是否穿过接管轴线和壳体中心线。",
            view_direction="待根据 PDF 剖切箭头方向最终确认；当前按主视图剖切关系进行人工复核。",
            removed_side="待结合原图箭头确认",
            retained_side="待结合原图箭头确认",
            projection_basis="依据主视图剖切线、剖切箭头和接管在俯视图中的方位联合判断。",
            related_items=["N1", "N2", "N3", "N4", "N5"],
            extracted_fields=["axis_relation", "projection_length", "section_relation"],
            modeling_notes=[
                "3D 配管或接管建模时先读剖面关系，再决定实体朝向。",
                "如果箭头方向未完全确认，则不要把该剖面方向当作唯一依据，应回到原图复核。",
            ],
            confidence=0.85,
            direction_confidence=0.55,
            metadata={"source": "manual-review"},
        ),
    ]


def write_shared_recognition_jsons(params: TankParameters) -> dict[str, str]:
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    list_info_path = SHARED_DIR / f"{OUTPUT_TOPIC}_list_info.json"
    dimension_info_path = SHARED_DIR / f"{OUTPUT_TOPIC}_dimension_info.json"
    section_info_path = SHARED_DIR / f"{OUTPUT_TOPIC}_section_info.json"

    list_info_path.write_text(
        json.dumps(
            {
                "drawing_name": params.drawing_name,
                "source_page_image": params.source_page_image,
                "bom_closure": [entry.to_dict() for entry in params.bom_closure],
                "recognition_regions": [
                    region.to_dict() for region in params.recognition_regions if region.region_type == "bom_table"
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    dimension_info_path.write_text(
        json.dumps(
            {
                "drawing_name": params.drawing_name,
                "source_page_image": params.source_page_image,
                "dimension_ledger": [entry.to_dict() for entry in params.dimension_ledger],
                "view_interpretations": [entry.to_dict() for entry in params.view_interpretations],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    section_info_path.write_text(
        json.dumps(
            {
                "drawing_name": params.drawing_name,
                "source_page_image": params.source_page_image,
                "section_details": [entry.to_dict() for entry in params.section_details],
                "recognition_regions": [
                    region.to_dict()
                    for region in params.recognition_regions
                    if region.region_type in {"detail_view", "section_view"}
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "list_info_path": str(list_info_path),
        "dimension_info_path": str(dimension_info_path),
        "section_info_path": str(section_info_path),
    }


def build_validation_context(
    *,
    stage: str,
    params: TankParameters,
    preview_artifacts: list[PreviewArtifact] | None = None,
    dwg_path: Path | None = None,
    document_name: str | None = None,
) -> ValidationContext:
    return ValidationContext(
        stage=stage,
        topic=OUTPUT_TOPIC,
        params=params,
        script_path=str(CURRENT_FILE),
        dwg_path=str(dwg_path) if dwg_path is not None else None,
        document_name=document_name,
        dimension_ledger=[entry.to_dict() for entry in params.dimension_ledger],
        bom_closure=[entry.to_dict() for entry in params.bom_closure],
        recognition_summary={
            "view_interpretation": params.view_interpretation,
            "analysis_basis": params.analysis_basis,
            "regions": [region.to_dict() for region in params.recognition_regions],
        },
        preview_artifacts=list(preview_artifacts or []),
    )


def ensure_validation_pass(report: Any, stage_label: str) -> None:
    if report.has_errors:
        summary = report.summary()
        raise RuntimeError(f"{stage_label} 校验失败: error={summary.get('error', 0)}, warning={summary.get('warning', 0)}")


def orient_disc_like_entity(controller: Any, handle: str, center: tuple[float, float, float], axis: str) -> None:
    if axis == "x":
        rotate_entity_with_retry(controller, handle, center, 90.0, (0.0, 1.0, 0.0))
    elif axis == "-x":
        rotate_entity_with_retry(controller, handle, center, -90.0, (0.0, 1.0, 0.0))
    elif axis == "y":
        rotate_entity_with_retry(controller, handle, center, -90.0, (1.0, 0.0, 0.0))
    elif axis == "-y":
        rotate_entity_with_retry(controller, handle, center, 90.0, (1.0, 0.0, 0.0))
    elif axis == "-z":
        rotate_entity_with_retry(controller, handle, center, 180.0, (1.0, 0.0, 0.0))


def build_parameters(source_page_image: Path) -> TankParameters:
    dn50_flange = CustomFlange(165.0, 57.0, 125.0, 18.0, 4, 18.0)
    dn40_flange = CustomFlange(145.0, 45.0, 110.0, 18.0, 4, 16.0)
    dn25_flange = CustomFlange(115.0, 32.0, 85.0, 14.0, 4, 16.0)
    blind_assembly = BlindAssembly(
        blind_thickness=16.0,
        blind_outer_diameter=115.0,
        bolt_circle_diameter=85.0,
        bolt_hole_diameter=14.0,
        bolt_count=4,
        washer_inner_diameter=13.0,
        washer_outer_diameter=24.0,
        washer_thickness=2.5,
        bolt_length=50.0,
        shank_diameter=12.0,
        nut_thickness=10.0,
        nut_width=19.0,
    )

    shell_bottom_z = 830.0
    shell_straight_height = 1800.0
    shell_top_z = shell_bottom_z + shell_straight_height
    top_head_nozzle_z = 2856.0
    top_head_offset = top_head_nozzle_z - shell_top_z
    lower_nozzle_elevation = shell_bottom_z + 500.0
    upper_nozzle_elevation = shell_top_z - 200.0

    nozzles = [
        NozzleSpec(
            "N1",
            tank_polar_point(400.0, 180.0, lower_nozzle_elevation),
            "-y",
            57.0,
            3.5,
            100.0,
            40.0,
            dn50_flange,
            flange_axis="-y",
            position_basis={
                "height_reference": "主视图 N1 标高",
                "elevation": lower_nozzle_elevation,
                "plan_reference": "俯视图 180° 方位",
                "angular_reference": 180.0,
            },
        ),
        NozzleSpec(
            "N2",
            tank_polar_point(400.0, 90.0, upper_nozzle_elevation),
            "x",
            57.0,
            3.5,
            100.0,
            40.0,
            dn50_flange,
            union_to_shell=False,
            display_base_offset=0.0,
            flange_seat_overlap=6.0,
            flange_axis="x",
            position_basis={
                "height_reference": "主视图 N2 标高",
                "elevation": upper_nozzle_elevation,
                "plan_reference": "俯视图 90° 方位",
                "angular_reference": 90.0,
            },
        ),
        NozzleSpec(
            "N3",
            tank_polar_point(180.0, 0.0, shell_top_z + top_head_offset),
            "z",
            57.0,
            3.5,
            100.0,
            40.0,
            dn50_flange,
            flange_axis="z",
            position_basis={
                "height_reference": "主视图顶部口件标高",
                "elevation": shell_top_z + top_head_offset,
                "plan_reference": "俯视图 0° 方位",
                "angular_reference": 0.0,
                "radial_reference": 180.0,
            },
        ),
        NozzleSpec(
            "N4",
            (0.0, 0.0, shell_top_z + top_head_offset),
            "z",
            57.0,
            3.5,
            100.0,
            40.0,
            dn50_flange,
            flange_axis="z",
            position_basis={
                "height_reference": "主视图顶部中轴口标高",
                "elevation": shell_top_z + top_head_offset,
                "plan_reference": "俯视图中心轴",
                "angular_reference": 0.0,
            },
        ),
        NozzleSpec(
            "N5",
            (0.0, 0.0, shell_bottom_z - top_head_offset),
            "-z",
            45.0,
            4.0,
            100.0,
            35.0,
            dn40_flange,
            flange_axis="-z",
            position_basis={
                "height_reference": "主视图底部排污口标高",
                "elevation": shell_bottom_z - top_head_offset,
                "plan_reference": "与 N4 同轴",
                "angular_reference": 0.0,
            },
        ),
        NozzleSpec(
            "P",
            tank_polar_point(180.0, 90.0, shell_top_z + top_head_offset),
            "z",
            32.0,
            3.0,
            100.0,
            35.0,
            dn25_flange,
            blind_assembly=blind_assembly,
            flange_axis="z",
            position_basis={
                "height_reference": "主视图顶部口件标高",
                "elevation": shell_top_z + top_head_offset,
                "plan_reference": "俯视图 90° 方位",
                "angular_reference": 90.0,
                "radial_reference": 180.0,
            },
        ),
    ]

    item_closure = [
        ItemClosure(1, "支腿", 3, "A3-830-6", "modeled", "按俯视图明确可见的 0°/120°/240° 三组支腿建模。"),
        ItemClosure(2, "接管", 1, "φ45x4", "modeled", "作为 N5 底部接管建模。"),
        ItemClosure(3, "法兰", 1, "WN40(B)-16 RF(S=4)", "modeled", "作为 N5 配套法兰建模。"),
        ItemClosure(4, "椭圆封头", 2, "EHA800x6(5.3)", "modeled", "按上下椭圆封头建模。"),
        ItemClosure(5, "接管", 4, "φ57x3.5", "modeled", "作为 N1/N2/N3/N4 接管建模。"),
        ItemClosure(6, "法兰", 4, "WN50(B)-16 RF(S=3.5)", "modeled", "作为 N1/N2/N3/N4 配套法兰建模。"),
        ItemClosure(7, "筒体", 1, "DN800x6 H=1800", "modeled", "按主视图主体尺寸建模。"),
        ItemClosure(8, "接管", 1, "φ32x3", "modeled", "作为 P 口接管建模。"),
        ItemClosure(9, "法兰", 1, "WN25(B)-16 RF(S=3)", "modeled", "作为 P 口配套法兰建模。"),
        ItemClosure(10, "法兰盖", 1, "BL25-16 RF", "modeled", "作为 P 口盲板建模。"),
        ItemClosure(11, "垫圈", 1, "25-16 RF", "hidden_by_request", "按当前用户要求，本轮 3D 装配表达中不显示该组紧固件。"),
        ItemClosure(12, "螺栓", 4, "M12x50", "hidden_by_request", "按当前用户要求，本轮 3D 装配表达中不显示该组紧固件。"),
        ItemClosure(13, "螺母", 4, "M12", "hidden_by_request", "按当前用户要求，本轮 3D 装配表达中不显示该组紧固件。"),
        ItemClosure(14, "吊耳", 2, "TPP-2-6", "modeled", "按俯视图 315°/135° 两处吊耳做简化立片建模。"),
        ItemClosure(15, "铭牌、铭牌座", 1, "KH-001", "modeled", "按俯视图 180° 方向单处铭牌座简化建模。"),
    ]

    recognition_regions = build_recognition_regions()
    view_interpretations = build_view_interpretations()
    dimension_ledger = build_dimension_ledger(
        shell_outer_diameter=800.0,
        shell_straight_height=shell_straight_height,
        shell_bottom_z=shell_bottom_z,
        support_height=830.0,
        support_span=792.0,
        overall_height=2956.0,
        lower_nozzle_elevation=lower_nozzle_elevation,
        upper_nozzle_elevation=upper_nozzle_elevation,
        top_head_nozzle_z=top_head_nozzle_z,
    )
    bom_closure = build_bom_closure(item_closure)
    section_details = build_section_details()

    return TankParameters(
        drawing_name="压缩空气缓冲罐(V-3004)",
        shell_outer_diameter=800.0,
        shell_straight_height=shell_straight_height,
        shell_bottom_z=shell_bottom_z,
        support_height=830.0,
        base_plate_thickness=20.0,
        support_span=792.0,
        overall_height=2956.0,
        source_page_image=str(source_page_image),
        view_interpretation=(
            "该 PDF 的当前页由主视图、俯视图、右侧技术特性/接管表、件号表以及 A.B/径向/非径向接管焊接详图组成。"
            "3D 建模以主视图读取主体高度、封头与接管标高，以俯视图读取 N1/N2/N3/N4/N5/P 的周向方位，以及吊耳、铭牌座与支腿的平面位置。"
        ),
        analysis_basis=(
            "人工复核到的关键参数包括：DN800、筒体直段 H=1800、支腿 H=830、支腿外跨 792、"
            "N1/N2/N3/N4 为 DN50 法兰接管、N5 为 DN40 底部排污口、P 为 DN25 盲板口。"
            "俯视图还明确给出三只支腿位于 0°/120°/240°，N3 位于 0°、N2 位于 90°、N1 位于 180°，并用 N4(N5) 标示上下同轴。"
        ),
        uncertainties=[
            "吊耳与铭牌座按装配表达做几何简化，未复现制造级板厚倒角与焊缝细节。",
            "N2 为了保证俯视图中接管可见，本轮保留为外伸独立实体，不并入筒体，但接管起点仍以壳体外表面为基准。",
            "当前版本已整理关键尺寸台账，但尚未把所有 PDF 标注完整回写到 CAD 尺寸对象。",
        ],
        support_angles_deg=[0.0, 120.0, 240.0],
        lug_angles_deg=[135.0, 315.0],
        lug_line_angle_deg=315.0,
        recognition_regions=recognition_regions,
        view_interpretations=view_interpretations,
        dimension_ledger=dimension_ledger,
        bom_closure=bom_closure,
        section_details=section_details,
        nozzles=nozzles,
        item_closure=item_closure,
    )


def add_support_legs(service: CADService, params: TankParameters) -> None:
    controller = service.controller
    leg_radius = params.support_span / 2.0
    for angle_deg in params.support_angles_deg:
        x, y, _ = tank_polar_point(leg_radius, angle_deg, 0.0)
        plate = draw_box_with_retry(
            controller,
            center=(x, y, params.support_height / 2.0),
            length=16.0,
            width=90.0,
            height=params.support_height,
            layer="SUPPORT",
            color=COLOR_WHITE,
        )
        if plate is None:
            raise RuntimeError("支腿创建失败。")
        plate_handle = entity_handle(plate)
        rotate_entity_with_retry(controller, plate_handle, (x, y, params.support_height / 2.0), angle_deg, (0.0, 0.0, 1.0))

        top_plate = draw_box_with_retry(
            controller,
            center=(x, y, params.support_height - 6.0),
            length=120.0,
            width=70.0,
            height=12.0,
            layer="SUPPORT",
            color=COLOR_WHITE,
        )
        base_plate = draw_box_with_retry(
            controller,
            center=(x, y, params.base_plate_thickness / 2.0),
            length=140.0,
            width=70.0,
            height=params.base_plate_thickness,
            layer="SUPPORT",
            color=COLOR_WHITE,
        )
        if top_plate is None or base_plate is None:
            raise RuntimeError("支腿底座或顶座创建失败。")
        rotate_entity_with_retry(controller, entity_handle(top_plate), (x, y, params.support_height - 6.0), angle_deg, (0.0, 0.0, 1.0))
        rotate_entity_with_retry(controller, entity_handle(base_plate), (x, y, params.base_plate_thickness / 2.0), angle_deg, (0.0, 0.0, 1.0))


def add_lifting_lugs(service: CADService, params: TankParameters) -> None:
    controller = service.controller
    shell_top_z = params.shell_bottom_z + params.shell_straight_height
    for angle_deg in params.lug_angles_deg:
        x, y, _ = tank_polar_point(320.0, angle_deg, shell_top_z + 110.0)
        lug = draw_box_with_retry(
            controller,
            center=(x, y, shell_top_z + 130.0),
            length=160.0,
            width=24.0,
            height=70.0,
            layer="LUG",
            color=COLOR_WHITE,
        )
        if lug is None:
            raise RuntimeError("吊耳创建失败。")
        rotate_entity_with_retry(
            controller,
            entity_handle(lug),
            (x, y, shell_top_z + 130.0),
            params.lug_line_angle_deg,
            (0.0, 0.0, 1.0),
        )


def add_nameplate(service: CADService, params: TankParameters) -> None:
    controller = service.controller
    x, y, _ = tank_polar_point(410.0, 180.0, params.shell_bottom_z + 120.0)
    seat = draw_box_with_retry(
        controller,
        center=(x, y, params.shell_bottom_z + 120.0),
        length=120.0,
        width=20.0,
        height=90.0,
        layer="NAMEPLATE",
        color=COLOR_WHITE,
    )
    if seat is None:
        raise RuntimeError("铭牌座创建失败。")
    rotate_entity_with_retry(controller, entity_handle(seat), (x, y, params.shell_bottom_z + 120.0), 90.0, (0.0, 0.0, 1.0))


def draw_nozzle(service: CADService, shell_handle: str, nozzle: NozzleSpec) -> None:
    controller = service.controller
    radius = nozzle.outer_diameter / 2.0
    if nozzle.union_to_shell:
        total_length = nozzle.outward_length + nozzle.insert_depth
        base_point = axis_offset(nozzle.connect_center, nozzle.axis, -nozzle.insert_depth)
    else:
        total_length = nozzle.outward_length
        base_point = axis_offset(nozzle.connect_center, nozzle.axis, nozzle.display_base_offset)
    solid = controller._add_oriented_cylinder(base_point, radius, total_length, nozzle.axis)
    if solid is None:
        raise RuntimeError(f"接管创建失败: {nozzle.name}")
    apply_entity_style(controller, solid, nozzle.layer, nozzle.color)
    solid_handle = entity_handle(solid)
    if nozzle.union_to_shell:
        boolean_union_with_retry(controller, shell_handle, solid_handle)

    if nozzle.flange is None:
        return

    flange_base = axis_offset(
        base_point,
        nozzle.axis,
        max(total_length - nozzle.flange_seat_overlap, 0.0),
    )
    flange_entity = draw_flange_with_retry(controller, flange_base, nozzle.flange)
    if flange_entity is None:
        raise RuntimeError(f"法兰创建失败: {nozzle.name}")
    apply_entity_style(controller, flange_entity, "FLANGE", COLOR_WHITE)
    orient_disc_like_entity(controller, entity_handle(flange_entity), flange_base, nozzle.flange_axis or nozzle.axis)

    if nozzle.blind_assembly is not None:
        blind_axis = nozzle.flange_axis or nozzle.axis
        blind_base = axis_offset(flange_base, blind_axis, nozzle.flange.thickness)
        blind = draw_blind_flange_with_retry(controller, blind_base, nozzle.blind_assembly)
        if blind is None:
            raise RuntimeError("盲板创建失败。")
        apply_entity_style(controller, blind, "BLIND", COLOR_WHITE)
        orient_disc_like_entity(controller, entity_handle(blind), blind_base, blind_axis)
        if nozzle.blind_assembly.show_fasteners:
            add_blind_fasteners(service, blind_base, blind_axis, nozzle.blind_assembly)


def add_blind_fasteners(service: CADService, blind_base: tuple[float, float, float], axis: str, assembly: BlindAssembly) -> None:
    if axis != "z":
        return
    controller = service.controller
    bolt_radius = assembly.bolt_circle_diameter / 2.0
    for index in range(assembly.bolt_count):
        angle_deg = index * (360.0 / assembly.bolt_count)
        x, y, _ = tank_polar_point(bolt_radius, angle_deg, blind_base[2] + assembly.blind_thickness / 2.0)
        washer = draw_washer_with_retry(
            controller,
            center=(x, y, blind_base[2] + assembly.blind_thickness + assembly.washer_thickness / 2.0),
            inner_diameter=assembly.washer_inner_diameter,
            outer_diameter=assembly.washer_outer_diameter,
            thickness=assembly.washer_thickness,
            axis="z",
            layer="FASTENER",
            color=COLOR_WHITE,
        )
        bolt = draw_bolt_with_retry(
            controller,
            base_center=(x, y, blind_base[2] + assembly.blind_thickness),
            shank_diameter=assembly.shank_diameter,
            length=assembly.bolt_length,
            axis="z",
            head_width=19.0,
            head_height=8.0,
            layer="FASTENER",
            color=COLOR_WHITE,
        )
        nut = draw_hex_nut_with_retry(
            controller,
            center=(x, y, blind_base[2] + assembly.blind_thickness + assembly.washer_thickness + assembly.nut_thickness / 2.0),
            inner_diameter=assembly.shank_diameter,
            thickness=assembly.nut_thickness,
            width_across_flats=assembly.nut_width,
            axis="z",
            layer="FASTENER",
            color=COLOR_WHITE,
        )
        if washer is None or bolt is None or nut is None:
            raise RuntimeError("P 口紧固件创建失败。")


def build_model(service: CADService, params: TankParameters) -> None:
    shell = service.draw_cylinder(
        center=(0.0, 0.0, params.shell_bottom_z),
        radius=params.shell_outer_diameter / 2.0,
        height=params.shell_straight_height,
        layer="VESSEL",
        color=COLOR_WHITE,
    )
    if shell is None:
        raise RuntimeError("筒体创建失败。")
    shell_handle = entity_handle(shell)

    shell_top_z = params.shell_bottom_z + params.shell_straight_height
    top_head = draw_elliptical_head_with_retry(
        service.controller,
        center=(0.0, 0.0, shell_top_z),
        dn=params.shell_outer_diameter,
        axis="z",
        layer="VESSEL",
        color=COLOR_WHITE,
    )
    bottom_head = draw_elliptical_head_with_retry(
        service.controller,
        center=(0.0, 0.0, params.shell_bottom_z),
        dn=params.shell_outer_diameter,
        axis="-z",
        layer="VESSEL",
        color=COLOR_WHITE,
    )
    if top_head is None or bottom_head is None:
        raise RuntimeError("封头创建失败。")
    boolean_union_with_retry(service.controller, shell_handle, entity_handle(top_head))
    boolean_union_with_retry(service.controller, shell_handle, entity_handle(bottom_head))

    add_support_legs(service, params)
    add_lifting_lugs(service, params)
    add_nameplate(service, params)
    for nozzle in params.nozzles:
        draw_nozzle(service, shell_handle, nozzle)
    zoom_extents_with_retry(service.controller)


def capture_previews(service: CADService, version_dir: Path, version: int, params: TankParameters) -> list[PreviewArtifact]:
    capturer = ScreenCapture(cad_type=service.controller.cad_type, output_dir=str(version_dir))
    preferred_hwnd = service.controller.get_application_hwnd()
    document_hwnd = service.controller.get_document_hwnd()
    document_name = getattr(service.controller.doc, "Name", None)
    title_hint = getattr(service.controller.app, "Caption", None)
    target = (0.0, 0.0, params.shell_bottom_z + params.shell_straight_height / 2.0)
    view_specs = [
        ("left", 3400.0, 2200.0, f"{OUTPUT_TOPIC}_front_preview_v{version}.png"),
        ("top", 2800.0, 2800.0, f"{OUTPUT_TOPIC}_top_preview_v{version}.png"),
        ("swiso", 5600.0, 5600.0, f"{OUTPUT_TOPIC}_iso_preview_v{version}.png"),
    ]
    preview_artifacts: list[PreviewArtifact] = []
    for view_name, height, width, file_name in view_specs:
        set_named_view_with_retry(service.controller, view_name, target=target, height=height, width=width)
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
        preview_artifacts.append(
            PreviewArtifact(
                view_name=view_name,
                path=str(final_path),
                window_title=capture.get("window_title"),
                exists=final_path.exists(),
                metadata={
                    "captured_at": capture.get("captured_at"),
                    "frame_hash": capture.get("frame_hash"),
                    "window_hwnd": capture.get("window_hwnd"),
                },
            )
        )
    return preview_artifacts


def write_artifacts(
    version_dir: Path,
    version: int,
    params: TankParameters,
    dwg_path: Path,
    preview_artifacts: list[PreviewArtifact],
    validation_report: dict[str, Any],
    shared_json_paths: dict[str, str],
    shared_source_page_path: Path,
) -> None:
    parameters_path = version_dir / f"{OUTPUT_TOPIC}_parameters_v{version}.json"
    dimension_ledger_path = version_dir / f"{OUTPUT_TOPIC}_dimension_ledger_v{version}.json"
    bom_closure_path = version_dir / f"{OUTPUT_TOPIC}_bom_closure_v{version}.json"
    validation_report_path = version_dir / f"{OUTPUT_TOPIC}_validation_report_v{version}.json"
    recognition_summary_path = version_dir / f"{OUTPUT_TOPIC}_recognition_summary_v{version}.json"
    notes_path = version_dir / f"{OUTPUT_TOPIC}_notes_v{version}.txt"
    preview_paths = [artifact.path for artifact in preview_artifacts]
    with parameters_path.open("w", encoding="utf-8") as file_obj:
        json.dump(
            {
                "parameters": {
                    **asdict(params),
                    "nozzles": [asdict(item) for item in params.nozzles],
                    "item_closure": [asdict(item) for item in params.item_closure],
                },
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
                "preview_artifacts": [artifact.to_dict() for artifact in preview_artifacts],
                "validation_report": validation_report,
                "shared_json_paths": shared_json_paths,
                "shared_source_page_path": str(shared_source_page_path),
            },
            file_obj,
            ensure_ascii=False,
            indent=2,
        )
    dimension_ledger_path.write_text(
        json.dumps([entry.to_dict() for entry in params.dimension_ledger], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    bom_closure_path.write_text(
        json.dumps([entry.to_dict() for entry in params.bom_closure], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    validation_report_path.write_text(
        json.dumps(validation_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    recognition_summary_path.write_text(
        json.dumps(
            {
                "drawing_name": params.drawing_name,
                "source_page_image": params.source_page_image,
                "view_interpretation": params.view_interpretation,
                "analysis_basis": params.analysis_basis,
                "recognition_regions": [region.to_dict() for region in params.recognition_regions],
                "view_interpretations": [item.to_dict() for item in params.view_interpretations],
                "section_details": [item.to_dict() for item in params.section_details],
                "uncertainties": params.uncertainties,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    notes = [
        f"{OUTPUT_TOPIC} 参数化脚本建模说明 v{version}",
        "",
        f"源文件: {PDF_PATH}",
        f"DWG 输出: {dwg_path}",
        f"视图判定: {params.view_interpretation}",
        f"参数依据: {params.analysis_basis}",
        "",
        "编号件闭环:",
    ]
    notes.extend([f"- {item.item_no}. {item.name} / {item.spec} / {item.status} / {item.note}" for item in params.item_closure])
    notes.extend(
        [
            "",
            "归档脚本:",
                "",
            "关键尺寸台账:",
            *[
                f"- {entry.dimension_id} / {entry.value}{entry.unit} / 视图={entry.source_view} / 用途={','.join(entry.applies_to)}"
                for entry in params.dimension_ledger
            ],
            "",
            "不确定项:",
            *[f"- {entry}" for entry in params.uncertainties],
            "",
            "规则校验摘要:",
            f"- error={validation_report.get('summary', {}).get('error', 0)}",
            f"- warning={validation_report.get('summary', {}).get('warning', 0)}",
            f"- info={validation_report.get('summary', {}).get('info', 0)}",
            "",
            "shared 识图输入:",
            *[f"- {name}: {path}" for name, path in shared_json_paths.items()],
            "",
            "本轮说明:",
            "- 本轮从 PDF 重新识图后新建脚本，不参照旧压缩空气储罐脚本参数。",
            "- 已补结构化尺寸台账、编号件闭环表和规则校验报告。",
            "- 已在 shared 目录下写出列表信息、尺寸信息和剖面/详图信息 JSON，便于后续建模读取。",
            "- 已输出 front/top/iso 三张预览图用于视觉复核。",
        ]
    )
    notes_path.write_text("\n".join(notes), encoding="utf-8")

    copy_if_missing(PDF_PATH, SHARED_DIR / PDF_PATH.name)
    copy_if_missing(Path(params.source_page_image), SHARED_DIR / f"{OUTPUT_TOPIC}_source_page1.png")
    copy_if_missing(shared_source_page_path, SHARED_DIR / shared_source_page_path.name)


def main() -> int:
    if not PDF_PATH.exists():
        raise FileNotFoundError(f"未找到源 PDF: {PDF_PATH}")

    OUTPUT_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    version, version_dir = next_version_dir(OUTPUT_ROOT_DIR)
    dwg_path = version_dir / f"{OUTPUT_TOPIC}_3D_v{version}.dwg"
    shared_source_page_path = render_source_page(SHARED_DIR / f"{OUTPUT_TOPIC}_source_page1.png")
    params = build_parameters(shared_source_page_path)
    shared_json_paths = write_shared_recognition_jsons(params)
    pre_build_report = run_validation(build_validation_context(stage="pre_build", params=params))
    ensure_validation_pass(pre_build_report, "建模前")

    service = CADService()
    create_fresh_document(service)
    build_model(service, params)
    post_build_report = run_validation(build_validation_context(stage="post_build", params=params))
    ensure_validation_pass(post_build_report, "建模后")
    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    preview_artifacts = capture_previews(service, version_dir, version, params)
    post_preview_report = run_validation(
        build_validation_context(
            stage="post_preview",
            params=params,
            preview_artifacts=preview_artifacts,
            dwg_path=dwg_path,
            document_name=getattr(service.controller.doc, "Name", dwg_path.name),
        )
    )
    combined_report = merge_reports([pre_build_report, post_build_report, post_preview_report], stage="full_run")
    write_artifacts(
        version_dir,
        version,
        params,
        dwg_path,
        preview_artifacts,
        combined_report.to_dict(),
        shared_json_paths,
        shared_source_page_path,
    )
    ensure_validation_pass(post_preview_report, "预览校验")
    print(
        json.dumps(
            {
                "success": True,
                "version": version,
                "version_dir": str(version_dir),
                "dwg_path": str(dwg_path),
                "preview_paths": [artifact.path for artifact in preview_artifacts],
                "validation_report_path": str(version_dir / f"{OUTPUT_TOPIC}_validation_report_v{version}.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
