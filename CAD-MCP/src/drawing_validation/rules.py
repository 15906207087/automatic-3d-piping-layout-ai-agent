from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List

from drawing_validation.context import ValidationContext
from drawing_validation.report import ValidationIssue, ValidationReport

ValidationRule = Callable[[ValidationContext], List[ValidationIssue]]


def rule_key_dimensions_present(context: ValidationContext) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    if context.stage not in {"pre_build", "post_build"}:
        return issues

    key_dimensions = [
        entry
        for entry in context.dimension_ledger
        if bool(entry.get("is_key")) or entry.get("metadata", {}).get("category") in {"overall", "height", "diameter", "spacing"}
    ]
    if not key_dimensions:
        issues.append(
            ValidationIssue(
                rule_id="RULE_KEY_DIMENSIONS_PRESENT",
                severity="error",
                category="dimension_ledger",
                message="缺少关键尺寸台账，当前任务不应直接进入正式建模。",
                path="dimension_ledger",
            )
        )
        return issues

    for index, entry in enumerate(key_dimensions):
        if entry.get("value") is None:
            issues.append(
                ValidationIssue(
                    rule_id="RULE_KEY_DIMENSIONS_PRESENT",
                    severity="warning",
                    category="dimension_ledger",
                    message=f"关键尺寸 {entry.get('dimension_id') or index + 1} 缺少数值。",
                    path=f"dimension_ledger.{index}.value",
                )
            )
        if not entry.get("source_view"):
            issues.append(
                ValidationIssue(
                    rule_id="RULE_KEY_DIMENSIONS_PRESENT",
                    severity="warning",
                    category="dimension_ledger",
                    message=f"关键尺寸 {entry.get('dimension_id') or index + 1} 缺少来源视图。",
                    path=f"dimension_ledger.{index}.source_view",
                )
            )
    return issues


def rule_all_bom_items_closed(context: ValidationContext) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    if context.stage not in {"pre_build", "post_build"}:
        return issues
    if not context.bom_closure:
        issues.append(
            ValidationIssue(
                rule_id="RULE_ALL_BOM_ITEMS_CLOSED",
                severity="error",
                category="bom_closure",
                message="未形成编号件闭环表，无法确认是否漏件少件。",
                path="bom_closure",
            )
        )
        return issues

    acceptable = {"modeled", "hidden_by_request", "documented_not_modeled", "explicit_exception"}
    for index, entry in enumerate(context.bom_closure):
        status = str(entry.get("status") or entry.get("model_status") or "").strip()
        if status not in acceptable:
            issues.append(
                ValidationIssue(
                    rule_id="RULE_ALL_BOM_ITEMS_CLOSED",
                    severity="error",
                    category="bom_closure",
                    message=f"编号件 {entry.get('item_no') or index + 1} 尚未完成闭环。",
                    path=f"bom_closure.{index}.status",
                    metadata={"status": status},
                )
            )
        if int(entry.get("quantity") or 0) <= 0:
            issues.append(
                ValidationIssue(
                    rule_id="RULE_ALL_BOM_ITEMS_CLOSED",
                    severity="warning",
                    category="bom_closure",
                    message=f"编号件 {entry.get('item_no') or index + 1} 数量无效。",
                    path=f"bom_closure.{index}.quantity",
                )
            )
    return issues


def rule_nozzle_has_position_basis(context: ValidationContext) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    if context.stage not in {"pre_build", "post_build"}:
        return issues

    for index, nozzle in enumerate(context.iter_nozzles()):
        basis = getattr(nozzle, "position_basis", None) or {}
        if not isinstance(basis, dict) or not basis:
            issues.append(
                ValidationIssue(
                    rule_id="RULE_NOZZLE_HAS_POSITION_BASIS",
                    severity="error",
                    category="nozzle_basis",
                    message=f"接管 {getattr(nozzle, 'name', index + 1)} 缺少定位依据。",
                    path=f"nozzles.{index}.position_basis",
                )
            )
            continue
        if not basis.get("elevation") and not basis.get("height_reference"):
            issues.append(
                ValidationIssue(
                    rule_id="RULE_NOZZLE_HAS_POSITION_BASIS",
                    severity="warning",
                    category="nozzle_basis",
                    message=f"接管 {getattr(nozzle, 'name', index + 1)} 缺少标高依据。",
                    path=f"nozzles.{index}.position_basis.elevation",
                )
            )
        if not basis.get("plan_reference") and not basis.get("angular_reference"):
            issues.append(
                ValidationIssue(
                    rule_id="RULE_NOZZLE_HAS_POSITION_BASIS",
                    severity="warning",
                    category="nozzle_basis",
                    message=f"接管 {getattr(nozzle, 'name', index + 1)} 缺少平面方位依据。",
                    path=f"nozzles.{index}.position_basis.plan_reference",
                )
            )
    return issues


def rule_flange_no_gap(context: ValidationContext) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    if context.stage not in {"pre_build", "post_build"}:
        return issues

    for index, nozzle in enumerate(context.iter_nozzles()):
        flange = getattr(nozzle, "flange", None)
        if flange is None:
            continue
        overlap = float(getattr(nozzle, "flange_seat_overlap", 0.0) or 0.0)
        outward_length = float(getattr(nozzle, "outward_length", 0.0) or 0.0)
        if overlap <= 0.0:
            issues.append(
                ValidationIssue(
                    rule_id="RULE_FLANGE_NO_GAP",
                    severity="error",
                    category="flange_fit",
                    message=f"接管 {getattr(nozzle, 'name', index + 1)} 的法兰吃入量必须大于 0，避免法兰与接管留缝。",
                    path=f"nozzles.{index}.flange_seat_overlap",
                )
            )
        elif overlap > outward_length:
            issues.append(
                ValidationIssue(
                    rule_id="RULE_FLANGE_NO_GAP",
                    severity="error",
                    category="flange_fit",
                    message=f"接管 {getattr(nozzle, 'name', index + 1)} 的法兰吃入量超过了接管外伸长度。",
                    path=f"nozzles.{index}.flange_seat_overlap",
                )
            )
    return issues


def rule_nozzle_axis_matches_flange_normal(context: ValidationContext) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    if context.stage not in {"pre_build", "post_build"}:
        return issues

    for index, nozzle in enumerate(context.iter_nozzles()):
        flange = getattr(nozzle, "flange", None)
        if flange is None:
            continue
        axis = str(getattr(nozzle, "axis", ""))
        flange_axis = str(getattr(nozzle, "flange_axis", axis))
        if axis != flange_axis:
            issues.append(
                ValidationIssue(
                    rule_id="RULE_NOZZLE_AXIS_MATCHES_FLANGE_NORMAL",
                    severity="error",
                    category="flange_orientation",
                    message=f"接管 {getattr(nozzle, 'name', index + 1)} 的轴向与法兰法向不一致。",
                    path=f"nozzles.{index}.flange_axis",
                    metadata={"axis": axis, "flange_axis": flange_axis},
                )
            )
    return issues


def rule_required_previews_exist(context: ValidationContext) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    if context.stage != "post_preview":
        return issues
    if not context.preview_artifacts:
        issues.append(
            ValidationIssue(
                rule_id="RULE_REQUIRED_PREVIEWS_EXIST",
                severity="error",
                category="preview",
                message="未生成任何校验预览图。",
                path="preview_artifacts",
            )
        )
        return issues

    for index, artifact in enumerate(context.preview_artifacts):
        preview_path = Path(artifact.path)
        if not preview_path.exists():
            issues.append(
                ValidationIssue(
                    rule_id="RULE_REQUIRED_PREVIEWS_EXIST",
                    severity="error",
                    category="preview",
                    message=f"{artifact.view_name} 视图预览图不存在。",
                    path=f"preview_artifacts.{index}.path",
                )
            )
    return issues


def rule_captured_window_is_current_dwg(context: ValidationContext) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    if context.stage != "post_preview":
        return issues

    expected_name = (context.document_name or Path(context.dwg_path or "").name or "").lower()
    if not expected_name:
        return issues

    for index, artifact in enumerate(context.preview_artifacts):
        title = (artifact.window_title or "").lower()
        if title and expected_name not in title:
            issues.append(
                ValidationIssue(
                    rule_id="RULE_CAPTURED_WINDOW_IS_CURRENT_DWG",
                    severity="warning",
                    category="preview",
                    message=f"{artifact.view_name} 预览截图窗口标题与当前 DWG 不匹配。",
                    path=f"preview_artifacts.{index}.window_title",
                    metadata={"window_title": artifact.window_title, "expected_name": expected_name},
                )
            )
    return issues


RULE_REGISTRY: Dict[str, ValidationRule] = {
    "RULE_KEY_DIMENSIONS_PRESENT": rule_key_dimensions_present,
    "RULE_ALL_BOM_ITEMS_CLOSED": rule_all_bom_items_closed,
    "RULE_NOZZLE_HAS_POSITION_BASIS": rule_nozzle_has_position_basis,
    "RULE_FLANGE_NO_GAP": rule_flange_no_gap,
    "RULE_NOZZLE_AXIS_MATCHES_FLANGE_NORMAL": rule_nozzle_axis_matches_flange_normal,
    "RULE_REQUIRED_PREVIEWS_EXIST": rule_required_previews_exist,
    "RULE_CAPTURED_WINDOW_IS_CURRENT_DWG": rule_captured_window_is_current_dwg,
}

DEFAULT_RULES_BY_STAGE: Dict[str, List[str]] = {
    "pre_build": [
        "RULE_KEY_DIMENSIONS_PRESENT",
        "RULE_ALL_BOM_ITEMS_CLOSED",
        "RULE_NOZZLE_HAS_POSITION_BASIS",
        "RULE_FLANGE_NO_GAP",
        "RULE_NOZZLE_AXIS_MATCHES_FLANGE_NORMAL",
    ],
    "post_build": [
        "RULE_KEY_DIMENSIONS_PRESENT",
        "RULE_ALL_BOM_ITEMS_CLOSED",
        "RULE_NOZZLE_HAS_POSITION_BASIS",
        "RULE_FLANGE_NO_GAP",
        "RULE_NOZZLE_AXIS_MATCHES_FLANGE_NORMAL",
    ],
    "post_preview": [
        "RULE_REQUIRED_PREVIEWS_EXIST",
        "RULE_CAPTURED_WINDOW_IS_CURRENT_DWG",
    ],
}


def run_validation(
    context: ValidationContext,
    *,
    rule_ids: List[str] | None = None,
) -> ValidationReport:
    selected_rule_ids = list(rule_ids or DEFAULT_RULES_BY_STAGE.get(context.stage, []))
    report = ValidationReport(
        stage=context.stage,
        metadata={"topic": context.topic, "rule_ids": selected_rule_ids},
    )
    for rule_id in selected_rule_ids:
        rule = RULE_REGISTRY.get(rule_id)
        if rule is None:
            report.issues.append(
                ValidationIssue(
                    rule_id=rule_id,
                    severity="warning",
                    category="validation_framework",
                    message=f"规则 {rule_id} 未注册，已跳过。",
                )
            )
            continue
        report.extend(rule(context))
    return report
