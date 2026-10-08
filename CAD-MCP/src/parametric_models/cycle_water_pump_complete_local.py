from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

CURRENT_FILE = Path(__file__).resolve()
SRC_DIR = CURRENT_FILE.parents[1]
PROJECT_DIR = CURRENT_FILE.parents[2]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from parametric_models.cycle_water_equipment_from_json import (
    CADService,
    draw_solid_box,
    save_drawing_with_retry,
    zoom_extents_with_retry,
    WHITE,
)
from parametric_models import cycle_water_dd_rebuilt as dd_rebuilt
from parametric_models import cycle_water_pipeline_from_json as pipeline

OUTPUT_TOPIC = "循环水_泵局部验证"
OUTPUT_ROOT_DIR = PROJECT_DIR / "output" / OUTPUT_TOPIC
SHARED_DIR = PROJECT_DIR / "output" / "循环水" / "shared"
REFERENCE_IMAGE = SHARED_DIR / "循环水_pump_vertical_inline_reference.png"


def build_pump_local_spec() -> dict[str, Any]:
    """Build one standalone vertical inline (ISG-style) pump validation spec.

    Visual reference: user-provided vertical pipeline pump photo
    (motor on top, horizontal inline flanges, pedestal + base).

    Catalog proxy ISG300-250A (public datasheet):
    - L≈1200 face-to-face, H≈1195, h≈300, base ≈450x350 / 400x300
    - Sample ports often both DN300; local uses DN300 / DN250 to mate items 25/26
    """

    face_to_face = 1200.0
    port_center_z = 300.0
    return {
        "topic": OUTPUT_TOPIC,
        "reference_model": "ISG300-250A vertical inline centrifugal pump (catalog + photo proxy)",
        "reference_image": str(REFERENCE_IMAGE),
        "reference_notes": [
            "用户给定实体图：立式管道泵，电机在上、蜗壳居中、进出口水平共线、下部支座落底板。",
            "公开样本 ISG300-250A：面距 L≈1200、总高 H≈1195、口高 h≈300。",
            "两端仍接 A-A 件25(DN300x250) 与件26(DN250x150)：吸入口 DN300 接 25 大端，排出口 DN250 接 26 大端。",
            "仅单泵局部验证，不改 A-A 剖面本体。",
        ],
        "pump": {
            "tag": "P-0204",
            "style": "vertical_inline",
            "face_to_face_mm": face_to_face,
            "overall_height_mm": 1195.0,
            "suction_dn": 300,
            "discharge_dn": 250,
            "port_center_z_mm": port_center_z,
            "volute_outer_diameter_mm": 620.0,
            "volute_height_mm": 360.0,
            "pedestal": {
                "bottom_diameter_mm": 280.0,
                "top_diameter_mm": 220.0,
                "height_mm": 160.0,
            },
            "motor": {
                "diameter_mm": 420.0,
                "height_mm": 520.0,
                "fin_count": 8,
                "fin_thickness_mm": 12.0,
                "fin_radial_mm": 22.0,
                "top_cap_height_mm": 40.0,
                "junction_box": {
                    "length_mm": 180.0,
                    "width_mm": 140.0,
                    "height_mm": 160.0,
                    "offset_y_mm": 250.0,
                },
            },
            "adapter_flange": {
                "diameter_mm": 480.0,
                "thickness_mm": 30.0,
            },
            "base": {
                "length_mm": 520.0,
                "width_mm": 420.0,
                "thickness_mm": 40.0,
            },
        },
        "item_25": {
            "item_no": 25,
            "name": "钢制同心异径管",
            "specification": "DN300x250",
            "dn_large": 300,
            "dn_small": 250,
            "length_mm": 300.0,
            "role": "接泵吸入口 DN300；大端贴泵口，小端朝外(-x)",
        },
        "item_26": {
            "item_no": 26,
            "name": "钢制异径管",
            "specification": "DN250x150",
            "dn_large": 250,
            "dn_small": 150,
            "length_mm": 250.0,
            "role": "接泵排出口 DN250；大端贴泵口，小端朝外(+x)",
        },
    }


def call_with_retry(label: str, builder: Any, retries: int = 3) -> Any:
    """Retry one CAD primitive builder."""

    last_error: Exception | None = None
    for _ in range(retries):
        try:
            result = builder()
            if result is not None:
                return result
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if last_error is not None:
        raise RuntimeError(f"{label} 失败: {last_error}") from last_error
    raise RuntimeError(f"{label} 失败: 返回空实体")


def _paint(entity: Any, layer: str) -> None:
    """Apply layer/color when the COM entity allows it."""

    if entity is None:
        return
    try:
        entity.Layer = layer
        entity.Color = WHITE
    except Exception:
        pass


def pump_body_spec_from_shared(spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the vertical-inline pump body dict used by local and world draws."""

    if spec is None:
        spec = build_pump_local_spec()
    return dict(spec["pump"])


def resolve_aa_pump_connection_placements(dimension_info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Resolve world pump placements that mate A-A item 25/26 pump-facing ends.

    A-A geometry (shared route):
    - item 25 DN300x250: large@11550 -> small@11850; pump faces DN250 @ y=11850, z=+0.535
    - item 26 DN250x150: drawn large@13100 axis=-y -> small@12850; pump faces DN150 @ y=12850, z=+0.510
    - pump sits in the 1000mm gap; X follows each A-A upper_anchor_x (not foundation center)
    """

    model_input = dimension_info.get("model_input", {})
    template = model_input.get("equipment_centers", {}).get("vertical_inline_pump_template", {})
    suction_y = float(template.get("item_25_pump_face_y_mm", 11850.0))
    discharge_y = float(template.get("item_26_pump_face_y_mm", 12850.0))
    suction_z = float(template.get("item_25_pump_face_elevation_mm", 535.0))
    discharge_z = float(template.get("item_26_pump_face_elevation_mm", 510.0))
    suction_dn = int(template.get("suction_dn", 250))
    discharge_dn = int(template.get("discharge_dn", 150))
    center_y = (suction_y + discharge_y) / 2.0
    face_to_face = abs(discharge_y - suction_y)

    route_candidates: list[Any] = []
    pipeline_layout = model_input.get("pipeline_layout_v2", {})
    if isinstance(pipeline_layout, dict):
        route_candidates.extend(pipeline_layout.get("section_preview_routes", []))
    route_candidates.extend(model_input.get("section_preview_routes", []))

    placements: dict[str, dict[str, Any]] = {}
    for route in route_candidates:
        if not isinstance(route, dict):
            continue
        if str(route.get("section", "")).strip().upper() != "A-A":
            continue
        if not bool(route.get("enabled", True)):
            continue
        pump_tag = str(route.get("pump_tag", "")).strip()
        if not pump_tag:
            continue
        upper_x = route.get("upper_anchor_x_mm")
        if upper_x is None:
            continue
        placements[pump_tag] = {
            "tag": pump_tag,
            "origin_xy": [float(upper_x), center_y],
            "flow_axis": "y",
            "face_to_face_mm": face_to_face,
            "suction_port_mm": [float(upper_x), suction_y, suction_z],
            "discharge_port_mm": [float(upper_x), discharge_y, discharge_z],
            "suction_port_z_mm": suction_z,
            "discharge_port_z_mm": discharge_z,
            "suction_dn": suction_dn,
            "discharge_dn": discharge_dn,
            "mates": {
                "item_25": {
                    "item_no": 25,
                    "pump_face_end": "small",
                    "dn": 250,
                    "y_mm": suction_y,
                    "elevation_mm": suction_z,
                },
                "item_26": {
                    "item_no": 26,
                    "pump_face_end": "small",
                    "dn": 150,
                    "y_mm": discharge_y,
                    "elevation_mm": discharge_z,
                },
            },
            "upper_anchor_x_mm": float(upper_x),
            "upper_anchor_reference": route.get("upper_anchor_reference"),
            "route_id": route.get("route_id"),
        }
    return placements


def draw_vertical_inline_pump(
    service: CADService,
    *,
    origin_xy: tuple[float, float],
    base_top_z: float,
    port_center_z: float,
    flow_axis: str = "x",
    face_to_face_mm: float | None = None,
    pump: dict[str, Any] | None = None,
    include_base_plate: bool = True,
    include_reducers: bool = False,
    include_stubs: bool = False,
    item_25: dict[str, Any] | None = None,
    item_26: dict[str, Any] | None = None,
    tag: str = "",
    suction_port_z: float | None = None,
    discharge_port_z: float | None = None,
) -> dict[str, Any]:
    """Draw one ISG-style vertical inline pump at a world/local origin.

    flow_axis:
    - "x": suction -x, discharge +x (local validation default)
    - "y": suction -y, discharge +y (A-A / plant row default)
    """

    controller = service.controller
    local_spec = build_pump_local_spec()
    pump = dict(pump or local_spec["pump"])
    item_25 = dict(item_25 or local_spec["item_25"])
    item_26 = dict(item_26 or local_spec["item_26"])

    axis = (flow_axis or "x").strip().lower()
    if axis not in {"x", "y"}:
        raise ValueError(f"不支持的 flow_axis={flow_axis}")

    ox, oy = float(origin_xy[0]), float(origin_xy[1])
    face_to_face = float(face_to_face_mm if face_to_face_mm is not None else pump["face_to_face_mm"])
    suction_z = float(suction_port_z if suction_port_z is not None else port_center_z)
    discharge_z = float(discharge_port_z if discharge_port_z is not None else port_center_z)
    port_z = (suction_z + discharge_z) / 2.0
    half = face_to_face / 2.0
    suction_dn = int(pump["suction_dn"])
    discharge_dn = int(pump["discharge_dn"])

    if axis == "x":
        suction_pt = (ox - half, oy, suction_z)
        discharge_pt = (ox + half, oy, discharge_z)
        suction_axis = "-x"
        discharge_axis = "x"
    else:
        suction_pt = (ox, oy - half, suction_z)
        discharge_pt = (ox, oy + half, discharge_z)
        suction_axis = "-y"
        discharge_axis = "y"

    if include_base_plate:
        base = pump["base"]
        base_thickness = float(base["thickness_mm"])
        # Base length follows flow axis; width is transverse.
        if axis == "x":
            length, width = float(base["length_mm"]), float(base["width_mm"])
        else:
            length, width = float(base["width_mm"]), float(base["length_mm"])
        draw_solid_box(
            service,
            center=(ox, oy, float(base_top_z) - base_thickness / 2.0),
            length=length,
            width=width,
            height=base_thickness,
            layer="PUMP",
        )

    pedestal = pump["pedestal"]
    pedestal_height = float(pedestal["height_mm"])
    pedestal_bottom_r = float(pedestal["bottom_diameter_mm"]) / 2.0
    pedestal_top_r = float(pedestal["top_diameter_mm"]) / 2.0
    ped_lower = call_with_retry(
        f"{tag} pedestal lower".strip(),
        lambda: controller._add_oriented_cylinder(
            (ox, oy, float(base_top_z)),
            pedestal_bottom_r,
            pedestal_height * 0.55,
            "z",
        ),
    )
    _paint(ped_lower, "PUMP")
    ped_upper = call_with_retry(
        f"{tag} pedestal upper".strip(),
        lambda: controller._add_oriented_cylinder(
            (ox, oy, float(base_top_z) + pedestal_height * 0.45),
            pedestal_top_r,
            pedestal_height * 0.55,
            "z",
        ),
    )
    _paint(ped_upper, "PUMP")

    volute_od = float(pump["volute_outer_diameter_mm"])
    volute_r = volute_od / 2.0
    volute_h = float(pump["volute_height_mm"])
    volute_bottom_z = port_z - volute_h / 2.0
    volute = call_with_retry(
        f"{tag} volute".strip(),
        lambda: controller._add_oriented_cylinder((ox, oy, volute_bottom_z), volute_r, volute_h, "z"),
    )
    _paint(volute, "PUMP")
    volute_belt = call_with_retry(
        f"{tag} volute belt".strip(),
        lambda: controller._add_oriented_cylinder(
            (ox, oy, port_z - volute_h * 0.18),
            volute_r * 1.08,
            volute_h * 0.36,
            "z",
        ),
    )
    _paint(volute_belt, "PUMP")

    suction_boss_len = max(120.0, half - volute_r + 40.0)
    discharge_boss_len = max(110.0, half - volute_r + 30.0)
    if axis == "x":
        suction_boss = call_with_retry(
            f"{tag} suction boss".strip(),
            lambda: controller._add_oriented_hollow_cylinder(
                (suction_pt[0], suction_pt[1], suction_pt[2]),
                pipeline.resolve_pipe_outer_radius_mm(suction_dn) + 24.0,
                suction_boss_len,
                "x",
                wall_thickness=14.0,
            ),
        )
        discharge_boss = call_with_retry(
            f"{tag} discharge boss".strip(),
            lambda: controller._add_oriented_hollow_cylinder(
                (discharge_pt[0] - discharge_boss_len, discharge_pt[1], discharge_pt[2]),
                pipeline.resolve_pipe_outer_radius_mm(discharge_dn) + 20.0,
                discharge_boss_len,
                "x",
                wall_thickness=12.0,
            ),
        )
        flange_t = 22.0
        suction_ring = call_with_retry(
            f"{tag} suction flange".strip(),
            lambda: controller._add_oriented_cylinder(
                (suction_pt[0] - flange_t, suction_pt[1], suction_pt[2]),
                pipeline.resolve_pipe_outer_radius_mm(suction_dn) + 48.0,
                flange_t,
                "x",
            ),
        )
        discharge_ring = call_with_retry(
            f"{tag} discharge flange".strip(),
            lambda: controller._add_oriented_cylinder(
                (discharge_pt[0], discharge_pt[1], discharge_pt[2]),
                pipeline.resolve_pipe_outer_radius_mm(discharge_dn) + 42.0,
                flange_t,
                "x",
            ),
        )
    else:
        suction_boss = call_with_retry(
            f"{tag} suction boss".strip(),
            lambda: controller._add_oriented_hollow_cylinder(
                (suction_pt[0], suction_pt[1], suction_pt[2]),
                pipeline.resolve_pipe_outer_radius_mm(suction_dn) + 24.0,
                suction_boss_len,
                "y",
                wall_thickness=14.0,
            ),
        )
        discharge_boss = call_with_retry(
            f"{tag} discharge boss".strip(),
            lambda: controller._add_oriented_hollow_cylinder(
                (discharge_pt[0], discharge_pt[1] - discharge_boss_len, discharge_pt[2]),
                pipeline.resolve_pipe_outer_radius_mm(discharge_dn) + 20.0,
                discharge_boss_len,
                "y",
                wall_thickness=12.0,
            ),
        )
        flange_t = 22.0
        suction_ring = call_with_retry(
            f"{tag} suction flange".strip(),
            lambda: controller._add_oriented_cylinder(
                (suction_pt[0], suction_pt[1] - flange_t, suction_pt[2]),
                pipeline.resolve_pipe_outer_radius_mm(suction_dn) + 48.0,
                flange_t,
                "y",
            ),
        )
        discharge_ring = call_with_retry(
            f"{tag} discharge flange".strip(),
            lambda: controller._add_oriented_cylinder(
                (discharge_pt[0], discharge_pt[1], discharge_pt[2]),
                pipeline.resolve_pipe_outer_radius_mm(discharge_dn) + 42.0,
                flange_t,
                "y",
            ),
        )
    _paint(suction_boss, "PUMP")
    _paint(discharge_boss, "PUMP")
    _paint(suction_ring, "PUMP")
    _paint(discharge_ring, "PUMP")

    adapter = pump["adapter_flange"]
    adapter_d = float(adapter["diameter_mm"])
    adapter_t = float(adapter["thickness_mm"])
    adapter_z = volute_bottom_z + volute_h
    adapter_ent = call_with_retry(
        f"{tag} adapter".strip(),
        lambda: controller._add_oriented_cylinder((ox, oy, adapter_z), adapter_d / 2.0, adapter_t, "z"),
    )
    _paint(adapter_ent, "PUMP")

    motor = pump["motor"]
    motor_r = float(motor["diameter_mm"]) / 2.0
    motor_h = float(motor["height_mm"])
    motor_z0 = adapter_z + adapter_t
    motor_body = call_with_retry(
        f"{tag} motor".strip(),
        lambda: controller._add_oriented_cylinder((ox, oy, motor_z0), motor_r, motor_h, "z"),
    )
    _paint(motor_body, "PUMP_MOTOR")

    fin_t = float(motor["fin_thickness_mm"])
    fin_radial = float(motor["fin_radial_mm"])
    fin_h = motor_h * 0.82
    fin_z_mid = motor_z0 + motor_h * 0.09 + fin_h / 2.0
    fin_ray = motor_r + fin_radial / 2.0
    for dx, dy, length, width in (
        (fin_ray, 0.0, fin_radial, fin_t),
        (-fin_ray, 0.0, fin_radial, fin_t),
        (0.0, fin_ray, fin_t, fin_radial),
        (0.0, -fin_ray, fin_t, fin_radial),
        (fin_ray * 0.72, fin_ray * 0.72, fin_radial * 0.75, fin_t),
        (-fin_ray * 0.72, fin_ray * 0.72, fin_radial * 0.75, fin_t),
        (fin_ray * 0.72, -fin_ray * 0.72, fin_radial * 0.75, fin_t),
        (-fin_ray * 0.72, -fin_ray * 0.72, fin_radial * 0.75, fin_t),
    ):
        draw_solid_box(
            service,
            center=(ox + dx, oy + dy, fin_z_mid),
            length=length,
            width=width,
            height=fin_h,
            layer="PUMP_MOTOR",
        )

    top_cap_h = float(motor["top_cap_height_mm"])
    top_cap = call_with_retry(
        f"{tag} top cap".strip(),
        lambda: controller._add_oriented_cylinder(
            (ox, oy, motor_z0 + motor_h),
            motor_r * 0.98,
            top_cap_h,
            "z",
        ),
    )
    _paint(top_cap, "PUMP_MOTOR")

    jbox = motor["junction_box"]
    # Keep junction box on +x side of the motor so it stays readable for both flow axes.
    draw_solid_box(
        service,
        center=(ox + float(jbox["offset_y_mm"]), oy, motor_z0 + motor_h * 0.45),
        length=float(jbox["width_mm"]),
        width=float(jbox["length_mm"]),
        height=float(jbox["height_mm"]),
        layer="PUMP_MOTOR",
    )

    handles: dict[str, Any] = {
        "suction_ring": getattr(suction_ring, "Handle", None),
        "discharge_ring": getattr(discharge_ring, "Handle", None),
        "reducer_25": None,
        "reducer_26": None,
    }
    item_25_small = list(suction_pt)
    item_26_small = list(discharge_pt)
    if include_reducers:
        reducer_25 = call_with_retry(
            f"{tag} item 25".strip(),
            lambda: controller.draw_reducer(
                center=suction_pt,
                dn_large=int(item_25["dn_large"]),
                dn_small=int(item_25["dn_small"]),
                axis=suction_axis,
                reducer_type="concentric",
                length=float(item_25["length_mm"]),
                layer="PIPE_FITTING",
                color=WHITE,
            ),
        )
        reducer_26 = call_with_retry(
            f"{tag} item 26".strip(),
            lambda: controller.draw_reducer(
                center=discharge_pt,
                dn_large=int(item_26["dn_large"]),
                dn_small=int(item_26["dn_small"]),
                axis=discharge_axis,
                reducer_type="concentric",
                length=float(item_26["length_mm"]),
                layer="PIPE_FITTING",
                color=WHITE,
            ),
        )
        handles["reducer_25"] = getattr(reducer_25, "Handle", None)
        handles["reducer_26"] = getattr(reducer_26, "Handle", None)
        if axis == "x":
            item_25_small = [suction_pt[0] - float(item_25["length_mm"]), suction_pt[1], port_z]
            item_26_small = [discharge_pt[0] + float(item_26["length_mm"]), discharge_pt[1], port_z]
        else:
            item_25_small = [suction_pt[0], suction_pt[1] - float(item_25["length_mm"]), port_z]
            item_26_small = [discharge_pt[0], discharge_pt[1] + float(item_26["length_mm"]), port_z]

        if include_stubs:
            if axis == "x":
                stub_25_end = [item_25_small[0] - 300.0, item_25_small[1], port_z]
                stub_26_end = [item_26_small[0] + 300.0, item_26_small[1], port_z]
            else:
                stub_25_end = [item_25_small[0], item_25_small[1] - 300.0, port_z]
                stub_26_end = [item_26_small[0], item_26_small[1] + 300.0, port_z]
            pipeline.draw_pipe_segment(service, start=stub_25_end, end=item_25_small, dn=int(item_25["dn_small"]))
            pipeline.draw_pipe_segment(service, start=item_26_small, end=stub_26_end, dn=int(item_26["dn_small"]))

    overall_top_z = motor_z0 + motor_h + top_cap_h
    return {
        "tag": tag,
        "style": "vertical_inline",
        "origin_xy": [ox, oy],
        "base_top_z_mm": float(base_top_z),
        "flow_axis": axis,
        "face_to_face_mm": face_to_face,
        "suction_port_mm": list(suction_pt),
        "discharge_port_mm": list(discharge_pt),
        "item_25_large_end_mm": list(suction_pt),
        "item_25_small_end_mm": item_25_small,
        "item_26_large_end_mm": list(discharge_pt),
        "item_26_small_end_mm": item_26_small,
        "overall_top_z_mm": overall_top_z,
        "handles": handles,
    }


def draw_complete_pump_local(service: CADService, spec: dict[str, Any]) -> dict[str, Any]:
    """Draw one local validation pump with item 25/26 on both ports."""

    return draw_vertical_inline_pump(
        service,
        origin_xy=(0.0, 0.0),
        base_top_z=0.0,
        port_center_z=float(spec["pump"]["port_center_z_mm"]),
        flow_axis="x",
        face_to_face_mm=float(spec["pump"]["face_to_face_mm"]),
        pump=spec["pump"],
        include_base_plate=True,
        include_reducers=True,
        include_stubs=True,
        item_25=spec["item_25"],
        item_26=spec["item_26"],
        tag=str(spec["pump"].get("tag", "")),
    )


def estimate_view(records: dict[str, Any], spec: dict[str, Any]) -> tuple[tuple[float, float, float], float, float]:
    """Estimate a local camera frame around the vertical pump assembly."""

    xs = [
        float(records["item_25_small_end_mm"][0]) - 300.0,
        float(records["item_26_small_end_mm"][0]) + 300.0,
    ]
    ys = [
        -float(spec["pump"]["base"]["width_mm"]) / 2.0 - 80.0,
        float(spec["pump"]["motor"]["junction_box"]["offset_y_mm"])
        + float(spec["pump"]["motor"]["junction_box"]["width_mm"]) / 2.0
        + 80.0,
    ]
    zs = [-60.0, float(records["overall_top_z_mm"]) + 40.0]
    center = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0, (min(zs) + max(zs)) / 2.0)
    height = max(max(zs) - min(zs), 1600.0) * 1.55
    width = max(max(xs) - min(xs), max(ys) - min(ys), 2600.0) * 1.2
    return center, height, width


def main() -> int:
    """Draw one standalone vertical inline pump with A-A items 25 and 26."""

    spec = build_pump_local_spec()
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    shared_path = SHARED_DIR / "循环水_pump_complete_local_info.json"
    shared_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    version, version_dir = pipeline.prepare_version_dir(OUTPUT_ROOT_DIR, minimum_version=1)
    output_stem = f"{OUTPUT_TOPIC}_p0204_complete"
    dwg_path = version_dir / f"{output_stem}_3D_v{version}.dwg"

    if REFERENCE_IMAGE.exists():
        shutil.copy2(REFERENCE_IMAGE, version_dir / REFERENCE_IMAGE.name)

    service = CADService()
    try:
        dd_rebuilt.create_validation_document(service)
    except RuntimeError:
        if not service.start_cad():
            raise
        active_doc = getattr(service.controller.app, "ActiveDocument", None)
        if active_doc is None:
            raise
        service.controller.doc = active_doc

    records = draw_complete_pump_local(service, spec)
    zoom_extents_with_retry(service.controller)
    if not save_drawing_with_retry(service.controller, str(dwg_path)):
        raise RuntimeError(f"DWG 保存失败: {dwg_path}")

    model_center, height, width = estimate_view(records, spec)
    preview_paths = dd_rebuilt.capture_previews(
        service,
        version_dir=version_dir,
        version=version,
        output_stem=output_stem,
        model_center=model_center,
        height=height,
        width=width,
    )

    parameters_path = version_dir / f"{output_stem}_parameters_v{version}.json"
    notes_path = version_dir / f"{output_stem}_notes_v{version}.txt"
    payload = {
        "spec": spec,
        "records": records,
        "dwg_path": str(dwg_path),
        "preview_paths": preview_paths,
        "shared_path": str(shared_path),
        "unchanged_sections": ["A-A"],
        "connection_semantics": {
            "item_25": "pump suction DN300 <-> reducer large end; small end outward",
            "item_26": "pump discharge DN250 <-> reducer large end; small end outward",
            "aa_merge_note": "A-A 本体本轮未改；并入整图时只做泵位整体挂接，不改 25/26 既有骨架。",
        },
    }
    parameters_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    notes_path.write_text(
        "\n".join(
            [
                f"{OUTPUT_TOPIC} 单泵完整验证 v{version}",
                "",
                f"DWG: {dwg_path}",
                "范围: 仅单泵局部（立式电机+蜗壳+支座+底板+件25+件26），不改 A-A 剖面。",
                "外形依据: 用户给定立式管道泵实体图 + ISG300-250A 尺寸代理。",
                "",
                "连接语义:",
                "- 泵吸入口 DN300 <-> 件25 DN300x250 大端（小端朝外）",
                "- 泵排出口 DN250 <-> 件26 DN250x150 大端（小端朝外）",
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
                "success": True,
                "version": version,
                "version_dir": str(version_dir),
                "dwg_path": str(dwg_path),
                "preview_paths": preview_paths,
                "parameters_path": str(parameters_path),
                "shared_path": str(shared_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
