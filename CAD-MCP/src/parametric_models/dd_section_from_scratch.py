from __future__ import annotations

import argparse
import itertools
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

from cad_controller import CADController
from pipeline_connection_helper import (
    _validate_run_specs,
    PipeConnectionRunSpec,
    RotationInstruction,
    build_orthogonal_elbow_connection,
    resolve_elbow_bend_radius,
)
from screen_capture import ScreenCapture


TOPIC = "D-D剖面重做"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / TOPIC
SHARED_DIR = OUTPUT_ROOT_DIR / "shared"
WHITE = 7


@dataclass
class NumberedItem:
    """Store one numbered item interpreted directly from the source section image."""

    item_no: int
    name: str
    quantity: int
    specification: str
    modeling_status: str
    note: str


@dataclass
class SectionRecognition:
    """Store the from-scratch recognition result of the D-D section image."""

    section: str
    view_type: str
    view_direction: str
    scope: str
    branch_dn: int
    elevations_m: dict[str, str]
    linear_dimensions_mm: dict[str, float]
    stable_conclusions: list[str]
    assumptions: list[str]
    numbered_items: list[NumberedItem]


@dataclass
class ModelParameters:
    """Store the executable parameters derived from the new recognition."""

    branch_dn: int
    visible_horizontal_reference_x_mm: float
    upper_corner: list[float]
    lower_corner: list[float]
    riser_top: list[float]
    sleeve_center: list[float]
    equipment_stub_end: list[float]
    equipment_stub_axis: str
    equipment_stub_length_mm: float
    upper_elbow_rotation: dict[str, Any]
    lower_elbow_rotation: dict[str, Any]
    notes: list[str]


@dataclass
class GeometrySnapshot:
    """Store the actual connection geometry used for drawing and verification."""

    od_mm: float
    wall_thickness_mm: float
    bend_radius_mm: float
    upper_vertical_requested_tangent: list[float]
    upper_horizontal_requested_tangent: list[float]
    lower_horizontal_requested_tangent: list[float]
    lower_vertical_requested_tangent: list[float]
    upper_vertical_length_mm: float
    lower_vertical_validation_length_mm: float
    view_center: list[float]
    view_width_mm: float
    view_height_mm: float


def build_from_scratch_recognition() -> tuple[SectionRecognition, ModelParameters]:
    """Reconstruct the D-D section using only the source image semantics."""

    numbered_items = [
        NumberedItem(
            item_no=21,
            name="90度弯头",
            quantity=2,
            specification="DN200，默认按长半径 LR 解释",
            modeling_status="will_model",
            note="上部一只负责立管转为水平，下部一只负责接向设备侧接口。",
        ),
        NumberedItem(
            item_no=28,
            name="制孔口/穿孔构件语义",
            quantity=1,
            specification="DN200",
            modeling_status="cannot_model_yet",
            note="当前通用 CAD 库无独立标准件原语，本轮保留为显式例外，不用代理几何替代正式件。",
        ),
        NumberedItem(
            item_no=31,
            name="刚性防水套管",
            quantity=1,
            specification="DN200，A 型",
            modeling_status="will_model",
            note="作为水平管穿越结构位置的直接连接件绘制。",
        ),
    ]
    recognition = SectionRecognition(
        section="D-D",
        view_type="剖面图",
        view_direction="沿局部管线剖切面观察，右侧为设备接口侧，左侧为池内立管侧。",
        scope="只绘制 DN200 管道走向及其直接连接件，不绘制右侧设备本体。",
        branch_dn=200,
        elevations_m={
            "riser_top": "+1.650",
            "horizontal_center": "+0.400",
            "platform": "+1.900",
            "pool_upper_control": "+1.600",
            "grade": "±0.000",
            "pool_bottom": "-1.400",
        },
        linear_dimensions_mm={
            "first_to_second_elbow_distance": 1650.0,
            "composite_basis": 1650.0,
        },
        stable_conclusions=[
            "图中主体是一条 DN200 局部支管。",
            "上部竖向管段自 +1.650 下落，到 +0.400 处完成第一次转向。",
            "局部链上可稳定识别两只 21 号弯头。",
            "31 号件是水平段上的刚性防水套管。",
            "28 号件表达的是穿越位置上的制孔口/穿孔构件语义。",
        ],
        assumptions=[
            "根据用户进一步更正，第一只弯头与第二只弯头的理论转角点间距按 650+1000=1650 处理，横管长度由该间距扣除两端弯头让口得到。",
            "由于设备不绘制，右侧不再保留接口短接；但下弯头仍保留，并按剖面内向下转折语义校验其第二端口朝向。",
        ],
        numbered_items=numbered_items,
    )
    model = ModelParameters(
        branch_dn=200,
        visible_horizontal_reference_x_mm=1650.0,
        upper_corner=[0.0, 0.0, 400.0],
        lower_corner=[1650.0, 0.0, 400.0],
        riser_top=[0.0, 0.0, 1650.0],
        sleeve_center=[650.0, 0.0, 400.0],
        equipment_stub_end=[1650.0, 0.0, 71.35],
        equipment_stub_axis="-z",
        equipment_stub_length_mm=328.65,
        upper_elbow_rotation={"angle_deg": 90.0, "axis_direction": [1.0, 0.0, 0.0]},
        lower_elbow_rotation={"angle_deg": 90.0, "axis_direction": [0.0, 0.0, 1.0]},
        notes=[
            "本轮完全不读取旧的循环水剖面路由代码或旧出图参数。",
            "本轮只依赖源图稳定可见尺寸和通用 CAD 控制器。",
            "本轮按用户最新更正，把两只弯头之间距离固定解释为 1650，并取消右侧接口短接实体。",
        ],
    )
    return recognition, model


def prepare_version_dir(output_root: Path) -> tuple[int, Path]:
    """Create the next version directory for this brand-new task."""

    output_root.mkdir(parents=True, exist_ok=True)
    version = 1
    while (output_root / f"v{version}").exists():
        version += 1
    version_dir = output_root / f"v{version}"
    version_dir.mkdir(parents=True, exist_ok=False)
    return version, version_dir


def archive_source_image(source_image: Path) -> Path:
    """Copy the user-provided source image into the shared folder of this new task."""

    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    destination = SHARED_DIR / source_image.name
    if not destination.exists():
        shutil.copy2(source_image, destination)
    return destination


def write_shared_analysis(
    *,
    source_image: Path,
    recognition: SectionRecognition,
    model: ModelParameters,
) -> Path:
    """Write the from-scratch recognition result into the new topic shared folder."""

    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "topic": TOPIC,
        "source_image": str(source_image),
        "recognition": {
            **asdict(recognition),
            "numbered_items": [asdict(item) for item in recognition.numbered_items],
        },
        "executable_model": asdict(model),
    }
    analysis_path = SHARED_DIR / "D-D_剖面_from_scratch_analysis.json"
    analysis_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return analysis_path


def ensure_clean_document(controller: CADController) -> None:
    """Start CAD and create a fresh drawing document for this task."""

    if not controller.start_cad():
        raise RuntimeError("无法启动或连接 CAD。")
    docs = getattr(controller.app, "Documents", None)
    if docs is None:
        raise RuntimeError("CAD 未暴露 Documents 集合。")
    docs.Add()
    time.sleep(1.5)
    controller.doc = controller.app.ActiveDocument


def resolve_pipe_profile(controller: CADController, dn: int) -> tuple[float, float]:
    """Resolve outer diameter and wall thickness for one DN pipe."""

    od = float(controller.ELBOW_DN_OD_GB12459[int(dn)])
    wall_thickness = float(controller._resolve_default_fitting_wall_thickness_gb12459(int(dn)))
    return od, wall_thickness


def classify_axis(start: list[float], end: list[float]) -> tuple[str, float]:
    """Resolve one axis-aligned direction string and segment length."""

    dx = float(end[0] - start[0])
    dy = float(end[1] - start[1])
    dz = float(end[2] - start[2])
    non_zero = [(abs(dx), "x", dx), (abs(dy), "y", dy), (abs(dz), "z", dz)]
    non_zero = [item for item in non_zero if item[0] > 1e-6]
    if len(non_zero) != 1:
        raise RuntimeError(f"当前新脚本仅支持正交直管，收到 start={start}, end={end}")
    _, axis_name, signed_length = non_zero[0]
    axis = axis_name if signed_length > 0 else f"-{axis_name}"
    return axis, abs(float(signed_length))


def apply_entity_style(controller: CADController, entity: Any, *, layer: str, color: int) -> None:
    """Apply layer and color to one created entity."""

    if entity is None:
        return
    controller.create_layer(layer)
    controller._call_with_retry(lambda: setattr(entity, "Layer", layer))
    controller._call_with_retry(lambda: setattr(entity, "Color", color))
    controller.refresh_view()


def draw_hollow_pipe(
    controller: CADController,
    *,
    start: list[float],
    end: list[float],
    dn: int,
    layer: str,
    color: int,
) -> Any:
    """Draw one hollow straight pipe segment using the generic CAD controller."""

    axis, length = classify_axis(start, end)
    od, wall_thickness = resolve_pipe_profile(controller, dn)
    outer_radius = od / 2.0
    inner_radius = max(outer_radius - wall_thickness, 0.0)
    outer = controller._add_oriented_cylinder(tuple(start), outer_radius, length, axis)
    if outer is None:
        raise RuntimeError(f"直管绘制失败: start={start}, end={end}")
    if inner_radius > 1e-6:
        inner_start = controller._advance_point(tuple(start), axis, -1.0)
        inner = controller._add_oriented_cylinder(inner_start, inner_radius, length + 2.0, axis)
        if inner is not None:
            controller._solid_boolean_subtract(outer, inner)
            controller._safe_delete_entity(inner)
    apply_entity_style(controller, outer, layer=layer, color=color)
    return outer


def rotate_entity(
    controller: CADController,
    *,
    entity: Any,
    base_point: list[float],
    angle_deg: float,
    axis_direction: list[float],
) -> None:
    """Rotate one CAD entity around an axis direction."""

    handle = getattr(entity, "Handle", None)
    if not handle:
        raise RuntimeError("实体缺少 Handle，无法旋转。")
    if not controller.rotate_entity(
        handle=str(handle),
        base_point=tuple(base_point),
        angle=float(angle_deg),
        axis_direction=tuple(axis_direction),
    ):
        raise RuntimeError(f"实体旋转失败: handle={handle}")


def draw_elbow(
    controller: CADController,
    *,
    corner: list[float],
    dn: int,
    layer: str,
    color: int,
    rotation: dict[str, Any],
) -> Any:
    """Draw one DN elbow and rotate it to the requested orientation."""

    last_error: Exception | None = None
    elbow = None
    for _ in range(4):
        try:
            elbow = controller.draw_pipe_elbow(
                center=tuple(corner),
                dn=int(dn),
                elbow_type="LR",
                angle=90,
                plane="xy",
                layer=layer,
                color=color,
            )
            if elbow is not None:
                break
        except Exception as exc:
            last_error = exc
        time.sleep(0.6)
    if elbow is None:
        if last_error is not None:
            raise RuntimeError(f"弯头绘制失败: center={corner}, error={last_error}")
        raise RuntimeError(f"弯头绘制失败: center={corner}")
    rotate_entity(
        controller,
        entity=elbow,
        base_point=corner,
        angle_deg=float(rotation["angle_deg"]),
        axis_direction=[float(value) for value in rotation["axis_direction"]],
    )
    return elbow


def iter_rotation_instruction_candidates() -> list[list[RotationInstruction]]:
    """Return a compact candidate set for orthogonal elbow orientation search."""

    base_candidates = [
        ("x", 90.0),
        ("x", -90.0),
        ("x", 180.0),
        ("y", 90.0),
        ("y", -90.0),
        ("y", 180.0),
        ("z", 90.0),
        ("z", -90.0),
        ("z", 180.0),
    ]
    sequences: list[list[RotationInstruction]] = [[]]
    for axis_name, angle_deg in base_candidates:
        axis_direction = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}[axis_name]
        sequences.append(
            [
                RotationInstruction(
                    angle_deg=angle_deg,
                    axis_direction=axis_direction,
                    axis_name=axis_name,
                )
            ]
        )
    for (axis_name_a, angle_deg_a), (axis_name_b, angle_deg_b) in itertools.product(base_candidates, repeat=2):
        axis_direction_a = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}[axis_name_a]
        axis_direction_b = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}[axis_name_b]
        sequences.append(
            [
                RotationInstruction(angle_deg=angle_deg_a, axis_direction=axis_direction_a, axis_name=axis_name_a),
                RotationInstruction(angle_deg=angle_deg_b, axis_direction=axis_direction_b, axis_name=axis_name_b),
            ]
        )
    return sequences


def resolve_elbow_geometry(
    controller: CADController,
    *,
    dn: int,
    corner_point: list[float],
    run_specs: list[PipeConnectionRunSpec],
    layer: str,
    color: int,
    draw_runs: bool,
) -> dict[str, Any]:
    """Search one valid orthogonal elbow orientation from desired run specs."""

    bend_radius = resolve_elbow_bend_radius(controller, dn, "LR")
    last_error: Exception | None = None
    best_rotation_sequence: list[RotationInstruction] | None = None
    best_shift_mm: float | None = None
    for rotation_sequence in iter_rotation_instruction_candidates():
        try:
            validated_runs, _ = _validate_run_specs(
                elbow_center=tuple(corner_point),
                bend_radius=bend_radius,
                run_specs=run_specs,
                elbow_rotations=rotation_sequence,
            )
            total_shift_mm = sum(float(run["center_distance_mm"]) for run in validated_runs)
            if best_shift_mm is None or total_shift_mm < best_shift_mm:
                best_shift_mm = total_shift_mm
                best_rotation_sequence = rotation_sequence
        except Exception as exc:
            last_error = exc
            continue
    if best_rotation_sequence is None:
        raise RuntimeError(f"未能为 corner={corner_point} 解析到符合端口链规则的弯头朝向: {last_error}")
    if best_shift_mm is None or best_shift_mm > 10.0:
        raise RuntimeError(
            f"corner={corner_point} 未找到足够贴合的弯头朝向，最小 snap_shift={best_shift_mm} mm。"
        )
    return build_orthogonal_elbow_connection(
        controller,
        dn=dn,
        elbow_center=tuple(corner_point),
        elbow_type="LR",
        run_specs=run_specs,
        elbow_rotations=best_rotation_sequence,
        layer=layer,
        color=color,
        draw_runs=draw_runs,
    )


def build_geometry(
    controller: CADController,
    *,
    model: ModelParameters,
) -> GeometrySnapshot:
    """Build the requested connection skeleton before helper port snapping."""

    od, wall_thickness = resolve_pipe_profile(controller, model.branch_dn)
    bend_radius = resolve_elbow_bend_radius(controller, model.branch_dn, "LR")
    upper_vertical_requested_tangent = [
        model.upper_corner[0],
        model.upper_corner[1],
        model.upper_corner[2] + bend_radius,
    ]
    upper_horizontal_requested_tangent = [
        model.upper_corner[0] + bend_radius,
        model.upper_corner[1],
        model.upper_corner[2],
    ]
    lower_horizontal_requested_tangent = [
        model.lower_corner[0] - bend_radius,
        model.lower_corner[1],
        model.lower_corner[2],
    ]
    lower_vertical_requested_tangent = [
        model.lower_corner[0],
        model.lower_corner[1],
        model.lower_corner[2] - bend_radius,
    ]
    upper_vertical_length = model.riser_top[2] - upper_vertical_requested_tangent[2]
    lower_vertical_validation_length = max(300.0, bend_radius)
    all_points = [
        model.riser_top,
        model.upper_corner,
        model.lower_corner,
        upper_vertical_requested_tangent,
        upper_horizontal_requested_tangent,
        lower_horizontal_requested_tangent,
        lower_vertical_requested_tangent,
        model.sleeve_center,
    ]
    xs = [point[0] for point in all_points]
    zs = [point[2] for point in all_points]
    view_center = [
        (min(xs) + max(xs)) / 2.0,
        0.0,
        (min(zs) + max(zs)) / 2.0,
    ]
    view_width = max(2600.0, (max(xs) - min(xs)) * 2.0)
    view_height = max(2600.0, (max(zs) - min(zs)) * 2.2)
    if upper_vertical_length <= 1e-6:
        raise RuntimeError("上部立管在按弯头端口让口后长度不足。")
    if lower_horizontal_requested_tangent[0] <= upper_horizontal_requested_tangent[0]:
        raise RuntimeError("按当前从图识读的稳定尺寸，水平直管净长不足；请补充第二只弯头定位信息。")
    return GeometrySnapshot(
        od_mm=od,
        wall_thickness_mm=wall_thickness,
        bend_radius_mm=bend_radius,
        upper_vertical_requested_tangent=upper_vertical_requested_tangent,
        upper_horizontal_requested_tangent=upper_horizontal_requested_tangent,
        lower_horizontal_requested_tangent=lower_horizontal_requested_tangent,
        lower_vertical_requested_tangent=lower_vertical_requested_tangent,
        upper_vertical_length_mm=upper_vertical_length,
        lower_vertical_validation_length_mm=lower_vertical_validation_length,
        view_center=view_center,
        view_width_mm=view_width,
        view_height_mm=view_height,
    )


def draw_model(
    controller: CADController,
    *,
    model: ModelParameters,
    geometry: GeometrySnapshot,
) -> list[dict[str, Any]]:
    """Draw the rebuilt D-D model and return a flat record list."""

    records: list[dict[str, Any]] = []
    pipe_layer = f"PIPE_DN{model.branch_dn}"
    fitting_layer = f"PIPE_FITTING_DN{model.branch_dn}"
    horizontal_run_length = float(
        geometry.lower_horizontal_requested_tangent[0] - geometry.upper_horizontal_requested_tangent[0]
    )

    upper_elbow_geometry = build_orthogonal_elbow_connection(
        controller,
        dn=model.branch_dn,
        elbow_center=tuple(model.upper_corner),
        elbow_type="LR",
        run_specs=[
            PipeConnectionRunSpec(
                run_id="upper_vertical",
                tangent_point_mm=tuple(geometry.upper_vertical_requested_tangent),
                axis_away_from_fitting="-z",
                length_mm=float(geometry.upper_vertical_length_mm),
                connection_end="top_end",
                flow_role="pipe_1",
            ),
            PipeConnectionRunSpec(
                run_id="upper_horizontal",
                tangent_point_mm=tuple(geometry.upper_horizontal_requested_tangent),
                axis_away_from_fitting="-x",
                length_mm=horizontal_run_length,
                connection_end="right_end",
                flow_role="pipe_2",
            ),
        ],
        elbow_rotations=[
            RotationInstruction(angle_deg=-90.0, axis_direction=(1.0, 0.0, 0.0), axis_name="x"),
            RotationInstruction(angle_deg=90.0, axis_direction=(0.0, 1.0, 0.0), axis_name="y"),
        ],
        layer=fitting_layer,
        color=WHITE,
        draw_runs=False,
    )
    upper_runs_by_id = {item["run_id"]: item for item in upper_elbow_geometry["runs"]}

    lower_elbow_geometry = build_orthogonal_elbow_connection(
        controller,
        dn=model.branch_dn,
        elbow_center=tuple(model.lower_corner),
        elbow_type="LR",
        run_specs=[
            PipeConnectionRunSpec(
                run_id="lower_horizontal",
                tangent_point_mm=tuple(geometry.lower_horizontal_requested_tangent),
                axis_away_from_fitting="+x",
                length_mm=horizontal_run_length,
                connection_end="left_end",
                flow_role="pipe_1",
            ),
            PipeConnectionRunSpec(
                run_id="lower_vertical_validation",
                tangent_point_mm=tuple(geometry.lower_vertical_requested_tangent),
                axis_away_from_fitting="+z",
                length_mm=float(geometry.lower_vertical_validation_length_mm),
                connection_end="bottom_end",
                flow_role="pipe_2",
            ),
        ],
        elbow_rotations=[
            RotationInstruction(angle_deg=-90.0, axis_direction=(0.0, 1.0, 0.0), axis_name="y"),
            RotationInstruction(angle_deg=-90.0, axis_direction=(0.0, 0.0, 1.0), axis_name="z"),
        ],
        layer=fitting_layer,
        color=WHITE,
        draw_runs=False,
    )
    lower_runs_by_id = {item["run_id"]: item for item in lower_elbow_geometry["runs"]}

    upper_vertical_start = upper_runs_by_id["upper_vertical"]["tangent_point_mm"]
    upper_vertical_end = model.riser_top
    vertical_pipe = draw_hollow_pipe(
        controller,
        start=upper_vertical_start,
        end=upper_vertical_end,
        dn=model.branch_dn,
        layer=pipe_layer,
        color=WHITE,
    )
    records.append({"kind": "pipe", "name": "vertical_riser", "handle": getattr(vertical_pipe, "Handle", None)})

    horizontal_start = upper_runs_by_id["upper_horizontal"]["tangent_point_mm"]
    horizontal_end = lower_runs_by_id["lower_horizontal"]["tangent_point_mm"]
    horizontal_pipe = draw_hollow_pipe(
        controller,
        start=horizontal_start,
        end=horizontal_end,
        dn=model.branch_dn,
        layer=pipe_layer,
        color=WHITE,
    )
    records.append({"kind": "pipe", "name": "horizontal_run", "handle": getattr(horizontal_pipe, "Handle", None)})
    records.append(
        {
            "kind": "fitting",
            "item_no": 21,
            "name": "upper_elbow",
            "ports": upper_elbow_geometry["elbow_ports"],
            "runs": upper_elbow_geometry["runs"],
            "rotations": upper_elbow_geometry["elbow_rotations"],
        }
    )
    records.append(
        {
            "kind": "fitting",
            "item_no": 21,
            "name": "lower_elbow",
            "ports": lower_elbow_geometry["elbow_ports"],
            "runs": lower_elbow_geometry["runs"],
            "rotations": lower_elbow_geometry["elbow_rotations"],
        }
    )

    sleeve = controller.draw_rigid_waterproof_sleeve(
        center=tuple(model.sleeve_center),
        dn=model.branch_dn,
        axis="x",
        sleeve_type="A",
        layer=fitting_layer,
        color=WHITE,
    )
    if sleeve is None:
        raise RuntimeError("刚性防水套管绘制失败。")
    records.append({"kind": "fitting", "item_no": 31, "name": "rigid_waterproof_sleeve", "handle": getattr(sleeve, "Handle", None)})
    return records


def capture_named_views(
    controller: CADController,
    *,
    geometry: GeometrySnapshot,
    version_dir: Path,
    output_stem: str,
    version: int,
) -> list[str]:
    """Capture front, top, and iso views for verification."""

    capturer = ScreenCapture(cad_type=controller.cad_type, output_dir=str(version_dir))
    captured_paths: list[str] = []
    view_specs = [
        ("front", geometry.view_center, geometry.view_height_mm, geometry.view_width_mm),
        ("top", geometry.view_center, geometry.view_width_mm, geometry.view_width_mm),
        ("swiso", geometry.view_center, geometry.view_height_mm * 1.4, geometry.view_width_mm * 1.4),
    ]
    document_name = getattr(controller.doc, "Name", None)
    title_hint = getattr(controller.app, "Caption", None)
    preferred_hwnd = controller.get_application_hwnd()
    document_hwnd = controller.get_document_hwnd()
    for view_name, target, height, width in view_specs:
        if not controller.set_named_view(view_name, target=tuple(target), height=height, width=width):
            raise RuntimeError(f"视图切换失败: {view_name}")
        time.sleep(0.8)
        capture = capturer.capture_cad_window(
            document_name=document_name,
            title_hint=title_hint,
            preferred_hwnd=preferred_hwnd,
            document_hwnd=document_hwnd,
        )
        final_path = version_dir / f"{output_stem}_{view_name}_preview_v{version}.png"
        shutil.copy2(capture["image_path"], final_path)
        captured_paths.append(str(final_path))
    return captured_paths


def write_run_artifacts(
    *,
    version_dir: Path,
    version: int,
    output_stem: str,
    source_image: Path,
    analysis_path: Path,
    recognition: SectionRecognition,
    model: ModelParameters,
    geometry: GeometrySnapshot,
    records: list[dict[str, Any]],
    dwg_path: Path,
    preview_paths: list[str],
) -> None:
    """Write parameters and notes for this brand-new D-D task."""

    parameters_path = version_dir / f"{output_stem}_parameters_v{version}.json"
    notes_path = version_dir / f"{output_stem}_notes_v{version}.txt"
    parameters_payload = {
        "topic": TOPIC,
        "source_image": str(source_image),
        "analysis_path": str(analysis_path),
        "recognition": {
            **asdict(recognition),
            "numbered_items": [asdict(item) for item in recognition.numbered_items],
        },
        "model": asdict(model),
        "geometry": asdict(geometry),
        "records": records,
    }
    parameters_path.write_text(json.dumps(parameters_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    notes = [
        f"{TOPIC} v{version}",
        "",
        "任务边界:",
        "- 完全按本轮源图重新识别，不读取旧的循环水剖面脚本或旧出图结果。",
        "- 仅绘制 DN200 管道与直接连接件，不绘制右侧设备本体。",
        "",
        "稳定识读结果:",
        *[f"- {item}" for item in recognition.stable_conclusions],
        "",
        "执行假设:",
        *[f"- {item}" for item in recognition.assumptions],
        "",
        "编号件闭环:",
        *[
            f"- item {item.item_no}: {item.name} / {item.specification} / {item.modeling_status} / {item.note}"
            for item in recognition.numbered_items
        ],
        "",
        f"DWG: {dwg_path}",
        f"分析 JSON: {analysis_path}",
        "预览图:",
        *[f"- {path}" for path in preview_paths],
    ]
    notes_path.write_text("\n".join(notes), encoding="utf-8")


def run(
    *,
    source_image: Path,
    label: str,
) -> dict[str, Any]:
    """Execute the new D-D section task from scratch."""

    recognition, model = build_from_scratch_recognition()
    archived_source_image = archive_source_image(source_image)
    analysis_path = write_shared_analysis(
        source_image=archived_source_image,
        recognition=recognition,
        model=model,
    )
    version, version_dir = prepare_version_dir(OUTPUT_ROOT_DIR)
    output_stem = label.strip() or "D-D_剖面重做"
    dwg_path = version_dir / f"{output_stem}_3D_v{version}.dwg"

    controller = CADController()
    ensure_clean_document(controller)
    geometry = build_geometry(controller, model=model)
    records = draw_model(controller, model=model, geometry=geometry)
    controller.zoom_extents()
    if not controller.save_drawing(str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")
    preview_paths = capture_named_views(
        controller,
        geometry=geometry,
        version_dir=version_dir,
        output_stem=output_stem,
        version=version,
    )
    write_run_artifacts(
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        source_image=archived_source_image,
        analysis_path=analysis_path,
        recognition=recognition,
        model=model,
        geometry=geometry,
        records=records,
        dwg_path=dwg_path,
        preview_paths=preview_paths,
    )
    return {
        "version": version,
        "version_dir": str(version_dir),
        "dwg_path": str(dwg_path),
        "analysis_path": str(analysis_path),
        "preview_paths": preview_paths,
    }


def main() -> int:
    """Parse CLI arguments and run the standalone D-D task."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--source-image", required=True)
    parser.add_argument("--label", default="D-D_剖面重做")
    args = parser.parse_args()
    result = run(source_image=Path(args.source_image), label=str(args.label))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
