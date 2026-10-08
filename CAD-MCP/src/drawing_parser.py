from __future__ import annotations

import io
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pipeline_models import (
    BomClosureEntry,
    DimensionLedgerEntry,
    DrawingDocument,
    PipelineSemanticModel,
    RecognitionRegion,
    ReviewIssue,
    SectionDetail,
    ViewInterpretation,
    pipeline_model_from_payload,
)
from vision_provider import create_vision_provider

logger = logging.getLogger("drawing_parser")

try:
    import fitz  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    fitz = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover - optional dependency
    Image = None


PARSER_SYSTEM_PROMPT = """
你是工程图识别助手，目标是把单页工程图、轴测配管图、P&ID 或设备布置图整理为结构化 JSON。

请只输出 JSON，禁止输出解释文字。返回结构如下：
{
  "summary": "一句话概括识别结果",
  "drawing_type": "isometric|pid|layout|unknown",
  "confidence": 0.0,
  "view_semantics": {
    "is_three_view": false,
    "mapping_rule": "前视图=主视图，俯视图=俯视图，左视图=左视图",
    "detected_views": [
      {
        "view_label": "前视图",
        "canonical_view": "front",
        "surface_label": "前面",
        "confidence": 0.0
      }
    ]
  },
  "recognition_regions": [
    {
      "region_id": "RG1",
      "region_type": "main_view|top_view|bom_table|detail_view|title_block|unknown",
      "page_number": 1,
      "source_view": "front",
      "label": "主视图",
      "description": "该区域负责主体轮廓和主要高度尺寸",
      "bbox": [0, 0, 100, 100],
      "extracted_fields": ["overall_height", "diameter"],
      "confidence": 0.0
    }
  ],
  "view_interpretations": [
    {
      "view_id": "V1",
      "source_view": "前视图",
      "canonical_role": "front",
      "confidence": 0.0,
      "evidence": ["视图标题", "投影关系"],
      "region_id": "RG1"
    }
  ],
  "nodes": [
    {
      "node_id": "N1",
      "point": [0, 0, 0],
      "label": "可选节点标签",
      "elevation": 0
    }
  ],
  "segments": [
    {
      "segment_id": "S1",
      "start_node": "N1",
      "end_node": "N2",
      "start_point": [0, 0, 0],
      "end_point": [1000, 0, 0],
      "route_points": [],
      "dn": 100,
      "outer_diameter": 114.3,
      "wall_thickness": 4.0,
      "layer": "PIPE"
    }
  ],
  "fittings": [
    {
      "fitting_id": "F1",
      "kind": "elbow|tee|reducer|flange|blind_flange|ball_valve|butterfly_valve|globe_valve",
      "center": [1000, 0, 0],
      "dn": 100,
      "connection_nodes": ["N1", "N2"],
      "orientation": {
        "plane": "xy",
        "angle": 90,
        "axis": "x",
        "run_axis": "x",
        "branch_axis": "z"
      },
      "spec": {
        "dn_large": 150,
        "dn_small": 100,
        "pn": 16
      }
    }
  ],
  "dimensions": [
    {
      "dimension_id": "D1",
      "dimension_type": "aligned|horizontal|vertical|diameter|radius",
      "start_point": [0, 0, 0],
      "end_point": [1000, 0, 0],
      "text_position": [500, 120, 0],
      "center": [0, 0, 0],
      "chord_point": [500, 0, 0],
      "far_chord_point": [500, 0, 0],
      "leader_length": 80,
      "textheight": 30,
      "source_view": "front",
      "orientation": "horizontal",
      "value": 1000
    }
  ],
  "dimension_ledger": [
    {
      "dimension_id": "D1",
      "dimension_type": "aligned|horizontal|vertical|diameter|radius",
      "source_view": "front",
      "value": 1000,
      "unit": "mm",
      "applies_to": ["shell.height"],
      "used_by_model": false,
      "confidence": 0.0,
      "verified": false,
      "is_key": true,
      "source_region": "RG1",
      "source_text": "总高 1000"
    }
  ],
  "bom_closure": [
    {
      "item_no": 1,
      "name": "法兰",
      "quantity": 1,
      "spec": "DN50",
      "material": "Q235",
      "standard": "HG/T20592",
      "decoded_params": {},
      "status": "pending",
      "model_status": "pending",
      "review_status": "pending",
      "exception_reason": "",
      "modeled_as": "",
      "note": ""
    }
  ],
  "section_details": [
    {
      "section_id": "SEC1",
      "section_type": "section_view|detail_view|weld_detail|nozzle_detail",
      "title": "A-A 剖面",
      "source_view": "front",
      "region_id": "RG1",
      "purpose": "说明接管、法兰、焊接和剖面关系",
      "cut_line_description": "剖切线穿过筒体中心线和接管轴线",
      "view_direction": "从右向左看",
      "removed_side": "观察者前侧移除",
      "retained_side": "后侧实体保留",
      "projection_basis": "依据主视图 A-A 剖切线与箭头方向判断",
      "related_items": ["N1", "法兰"],
      "extracted_fields": ["projection_length", "flange_type", "weld_type"],
      "modeling_notes": ["用于 3D 配管或接管建模时读取"],
      "direction_confidence": 0.0
    }
  ],
  "review_issues": [
    {
      "issue_id": "R1",
      "severity": "warning|critical|info",
      "category": "missing_field|low_confidence|manual_check",
      "message": "需要人工确认的内容",
      "field_path": "graph.segments.0.dn",
      "suggested_value": 100
    }
  ]
}

要求：
1. 首版优先面向轴测配管图，尽量给出毫米级相对坐标。
2. 如果绝对坐标无法确认，可输出局部相对坐标，但必须在 review_issues 中说明。
3. 对不确定的管径、标高、阀门类型、连接方向，必须生成 review_issues。
4. 如果图纸包含清晰尺寸标注，请同步输出 dimensions 数组；做不到时在 review_issues 中说明。
5. 除非图中明确要求或用户明确指定其他视图，dimensions 中所有尺寸标注默认写到主视图，因此 source_view 默认填 "front"。
6. 如果图纸不是轴测图，也请尽可能提取逻辑结构，但在 review_issues 中注明限制。
7. 未识别到的数据请返回空数组，不要虚构。
8. 如果输入是三视图或包含正投影视图，请固定采用以下约定：前视图 = 主视图，俯视图 = 俯视图，左视图 = 左视图。
9. 当图中出现前视图、俯视图、左视图标注时，必须按该约定判断对应面，不能擅自交换或重命名。
10. 如果输入是设备装配图或 PDF 页面，请尽量把页面拆成主视图、俯视图、件号表、局部详图或剖面图等 recognition_regions，并把可读的尺寸写入 dimension_ledger、件号表写入 bom_closure、剖面/详图信息写入 section_details。
11. 对 section_details 中的剖面图或局部详图，尽量补充剖切线说明、观察方向、移除侧、保留侧、投影判断依据和方向置信度；如果方向不确定，也要显式写出待确认。
""".strip()

DOCUMENT_VISION_SYSTEM_PROMPT = """
你是工程图和文档视觉分析助手。输入可能是由 PDF 渲染出的高清页面图片，也可能是原始图片。

请只输出 JSON，禁止输出解释文字。返回结构如下：
{
  "summary": "一句话总结页面主要内容和结论",
  "findings": [
    {
      "label": "观察点名称",
      "detail": "具体观察内容",
      "severity": "info|warning|critical"
    }
  ],
  "suggested_action": "建议下一步动作",
  "confidence": 0.0
}

要求：
1. 优先识别工程图、表格、标注、尺寸、标题栏、技术要求、阀门/管线/设备符号等关键信息。
2. 如果文字较密集，请尽量概括重点，不要编造看不清的细节。
3. 如果图片是 PDF 页面渲染结果，请把它当作原始页面进行分析，而不是普通截图。
4. `findings` 为空时返回 []。
5. `confidence` 范围是 0 到 1。
""".strip()


class DrawingParser:
    """Parse drawing documents into a structured piping semantic model."""

    IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}

    def __init__(self, vision_config: Optional[Dict[str, Any]] = None, output_dir: Optional[str] = None):
        self.vision_config = vision_config or {}
        self.provider = create_vision_provider(self.vision_config)
        base_output = output_dir or os.path.join(os.path.dirname(__file__), "..", "output", "parser")
        self.output_dir = os.path.abspath(base_output)
        os.makedirs(self.output_dir, exist_ok=True)

    def parse_document(
        self,
        file_path: str,
        drawing_type: str = "isometric",
        page_number: int = 1,
        recognized_data: Optional[Any] = None,
        focus_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Parse a PDF or image into a structured semantic model."""
        document, image_bytes, mime_type = self._prepare_document(file_path, drawing_type, page_number)

        if recognized_data is not None:
            payload = self._coerce_payload(recognized_data)
            recognition_metadata = {
                "provider": "manual-input",
                "source": "recognized_data",
            }
        else:
            prompt = self._build_prompt(document, focus_hint)
            provider_result = self.provider.analyze_image(
                image_bytes=image_bytes,
                prompt=prompt,
                system_prompt=PARSER_SYSTEM_PROMPT,
                mime_type=mime_type,
            )
            payload = self._coerce_payload(provider_result)
            recognition_metadata = {
                "provider": provider_result.get("provider"),
                "model": provider_result.get("model"),
                "raw_response_text": provider_result.get("raw_response_text"),
            }

        payload.setdefault("source_path", document.source_path)
        payload.setdefault("source_type", document.source_type)
        payload.setdefault("drawing_type", drawing_type or document.drawing_type)
        payload.setdefault("page_number", document.page_number)
        payload.setdefault("page_count", document.page_count)
        payload.setdefault("image_path", document.image_path)
        payload.setdefault("view_semantics", self._default_view_semantics())
        payload["recognition_regions"] = self._normalize_recognition_regions(
            payload.get("recognition_regions"),
            document=document,
        )
        payload["view_interpretations"] = self._normalize_view_interpretations(
            payload.get("view_interpretations"),
            payload.get("view_semantics"),
            payload["recognition_regions"],
        )
        payload["dimension_ledger"] = self._normalize_dimension_ledger(
            payload.get("dimension_ledger"),
            payload.get("dimensions"),
            payload["recognition_regions"],
        )
        payload["bom_closure"] = self._normalize_bom_closure(payload.get("bom_closure"))
        payload["section_details"] = self._normalize_section_details(
            payload.get("section_details"),
            payload["recognition_regions"],
            payload["view_interpretations"],
        )

        model = pipeline_model_from_payload(payload)
        model.document = document
        model.recognition_metadata.update(recognition_metadata)
        model.recognition_metadata.setdefault(
            "region_split_strategy",
            "provider-output-or-parser-skeleton",
        )
        self._ensure_review_issues(model)

        return {
            "success": True,
            "summary": model.summary or "图纸解析完成。",
            "document": model.document.to_dict(),
            "semantic_model": model.to_dict(),
            "view_semantics": dict(model.view_semantics),
            "recognition_regions": [region.to_dict() for region in model.recognition_regions],
            "view_interpretations": [item.to_dict() for item in model.view_interpretations],
            "dimension_ledger": [item.to_dict() for item in model.dimension_ledger],
            "bom_closure": [item.to_dict() for item in model.bom_closure],
            "section_details": [item.to_dict() for item in model.section_details],
            "review_issues": [issue.to_dict() for issue in model.review_issues],
            "requires_review": bool(model.unresolved_issues),
        }

    def analyze_document_visual(
        self,
        file_path: str,
        page_number: int = 1,
        analyze_all_pages: bool = False,
        max_pages: int = 5,
        dpi: int = 300,
        image_format: str = "png",
        prompt: Optional[str] = None,
        focus_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """将 PDF 或图片转换为高清图片后交给视觉模型分析。"""
        rendered_pages = self._render_document_pages(
            file_path=file_path,
            page_number=page_number,
            analyze_all_pages=analyze_all_pages,
            max_pages=max_pages,
            dpi=dpi,
            image_format=image_format,
        )

        page_results: List[Dict[str, Any]] = []
        merged_findings: List[Dict[str, Any]] = []
        provider_name: Optional[str] = None
        model_name: Optional[str] = None

        for rendered_page in rendered_pages:
            page_document = rendered_page["document"]
            provider_result = self.provider.analyze_image(
                image_bytes=rendered_page["image_bytes"],
                prompt=self._build_document_visual_prompt(
                    document=page_document,
                    prompt=prompt,
                    total_pages=len(rendered_pages),
                ),
                focus_hint=focus_hint,
                system_prompt=DOCUMENT_VISION_SYSTEM_PROMPT,
                mime_type=rendered_page["mime_type"],
            )
            provider_name = provider_name or provider_result.get("provider")
            model_name = model_name or provider_result.get("model")

            page_result = {
                "page_number": page_document.page_number,
                "page_count": page_document.page_count,
                "image_path": page_document.image_path,
                "image_format": rendered_page["image_format"],
                "mime_type": rendered_page["mime_type"],
                "dpi": rendered_page["dpi"],
                "summary": provider_result.get("summary", ""),
                "findings": provider_result.get("findings", []),
                "suggested_action": provider_result.get("suggested_action", ""),
                "confidence": provider_result.get("confidence"),
                "provider": provider_result.get("provider"),
                "model": provider_result.get("model"),
                "raw_response_text": provider_result.get("raw_response_text"),
            }
            page_results.append(page_result)

            for finding in page_result["findings"]:
                if not isinstance(finding, dict):
                    continue
                merged_finding = dict(finding)
                merged_finding["page_number"] = page_document.page_number
                merged_findings.append(merged_finding)

        return {
            "success": True,
            "summary": self._build_visual_analysis_summary(page_results),
            "source_path": str(Path(file_path)),
            "source_type": rendered_pages[0]["document"].source_type,
            "page_count": rendered_pages[0]["document"].page_count,
            "analyzed_pages": len(page_results),
            "provider": provider_name,
            "model": model_name,
            "rendered_images": [
                {
                    "page_number": rendered_page["document"].page_number,
                    "page_count": rendered_page["document"].page_count,
                    "image_path": rendered_page["document"].image_path,
                    "image_format": rendered_page["image_format"],
                    "mime_type": rendered_page["mime_type"],
                    "dpi": rendered_page["dpi"],
                }
                for rendered_page in rendered_pages
            ],
            "page_results": page_results,
            "findings": merged_findings,
        }

    def _prepare_document(
        self, file_path: str, drawing_type: str, page_number: int
    ) -> Tuple[DrawingDocument, bytes, str]:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"未找到图纸文件: {file_path}")

        suffix = path.suffix.lower()
        if suffix == ".pdf":
            image_path, image_bytes, page_count = self._extract_pdf_page(path, page_number)
            document = DrawingDocument(
                source_path=str(path),
                source_type="pdf",
                drawing_type=drawing_type,
                page_number=page_number,
                page_count=page_count,
                image_path=image_path,
                metadata={"rendered_from_pdf": True},
            )
            return document, image_bytes, "image/png"

        if suffix not in self.IMAGE_SUFFIXES:
            raise ValueError(f"暂不支持的图纸格式: {suffix}")

        image_bytes = path.read_bytes()
        document = DrawingDocument(
            source_path=str(path),
            source_type="image",
            drawing_type=drawing_type,
            page_number=1,
            page_count=1,
            image_path=str(path),
            metadata={},
        )
        return document, image_bytes, self._mime_type_from_suffix(suffix)

    def _render_document_pages(
        self,
        file_path: str,
        page_number: int,
        analyze_all_pages: bool,
        max_pages: int,
        dpi: int,
        image_format: str,
    ) -> List[Dict[str, Any]]:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"未找到图纸文件: {file_path}")

        suffix = path.suffix.lower()
        normalized_format = self._normalize_image_format(image_format)
        normalized_dpi = max(int(dpi or 300), 72)

        if suffix == ".pdf":
            return self._render_pdf_pages(
                path=path,
                page_number=page_number,
                analyze_all_pages=analyze_all_pages,
                max_pages=max_pages,
                dpi=normalized_dpi,
                image_format=normalized_format,
            )

        if suffix not in self.IMAGE_SUFFIXES:
            raise ValueError(f"暂不支持的图纸格式: {suffix}")

        return [self._render_image_file(path=path, image_format=normalized_format, dpi=normalized_dpi)]

    def _normalize_image_format(self, image_format: str | None) -> str:
        """规范化渲染图片格式，仅允许 png/jpg。"""
        normalized = str(image_format or "png").strip().lower()
        if normalized == "jpeg":
            normalized = "jpg"
        if normalized not in {"png", "jpg"}:
            raise ValueError("image_format 仅支持 png 或 jpg")
        return normalized

    def _render_pdf_pages(
        self,
        path: Path,
        page_number: int,
        analyze_all_pages: bool,
        max_pages: int,
        dpi: int,
        image_format: str,
    ) -> List[Dict[str, Any]]:
        if fitz is None:
            raise RuntimeError("解析 PDF 需要安装 PyMuPDF（fitz）。")

        rendered_pages: List[Dict[str, Any]] = []
        document = fitz.open(path)
        try:
            page_count = int(document.page_count)
            if page_count <= 0:
                raise ValueError("PDF 不包含可渲染页面。")

            if analyze_all_pages:
                if max_pages <= 0:
                    raise ValueError("max_pages 必须大于 0。")
                page_numbers = list(range(1, min(page_count, int(max_pages)) + 1))
            else:
                if page_number < 1 or page_number > page_count:
                    raise ValueError(f"页码超出范围，可选范围为 1 到 {page_count}")
                page_numbers = [page_number]

            for current_page in page_numbers:
                page = document.load_page(current_page - 1)
                pixmap = page.get_pixmap(dpi=dpi, alpha=False)
                image_bytes = self._pixmap_to_bytes(pixmap, image_format)
                image_path = self._save_rendered_page(
                    source_path=path,
                    page_number=current_page,
                    image_bytes=image_bytes,
                    image_format=image_format,
                )
                rendered_pages.append(
                    {
                        "document": DrawingDocument(
                            source_path=str(path),
                            source_type="pdf",
                            drawing_type="unknown",
                            page_number=current_page,
                            page_count=page_count,
                            image_path=image_path,
                            metadata={
                                "rendered_from_pdf": True,
                                "render_dpi": dpi,
                                "image_format": image_format,
                            },
                        ),
                        "image_bytes": image_bytes,
                        "image_format": image_format,
                        "mime_type": self._mime_type_from_format(image_format),
                        "dpi": dpi,
                    }
                )
        finally:
            document.close()

        return rendered_pages

    def _pixmap_to_bytes(self, pixmap: Any, image_format: str) -> bytes:
        """Convert a PyMuPDF pixmap into encoded image bytes."""
        normalized_format = self._normalize_image_format(image_format)
        output_format = "png" if normalized_format == "png" else "jpeg"
        return pixmap.tobytes(output_format)

    def _save_rendered_page(
        self,
        *,
        source_path: Path,
        page_number: int,
        image_bytes: bytes,
        image_format: str,
    ) -> str:
        """Persist one rendered page image under the parser output directory."""
        normalized_format = self._normalize_image_format(image_format)
        image_name = (
            f"{source_path.stem}_page_{page_number}_{uuid.uuid4().hex[:8]}.{normalized_format}"
        )
        image_path = Path(self.output_dir) / image_name
        image_path.write_bytes(image_bytes)
        return str(image_path)

    def _render_image_file(self, path: Path, image_format: str, dpi: int) -> Dict[str, Any]:
        """Normalize an image file into the same structure used for rendered PDF pages."""
        normalized_format = self._normalize_image_format(image_format)
        source_bytes = path.read_bytes()
        if Image is None:
            image_bytes = source_bytes
            saved_path = str(path)
        else:
            image = Image.open(io.BytesIO(source_bytes))
            buffer = io.BytesIO()
            save_format = "PNG" if normalized_format == "png" else "JPEG"
            image.save(buffer, format=save_format, dpi=(dpi, dpi))
            image_bytes = buffer.getvalue()
            saved_path = self._save_rendered_page(
                source_path=path,
                page_number=1,
                image_bytes=image_bytes,
                image_format=normalized_format,
            )
        return {
            "document": DrawingDocument(
                source_path=str(path),
                source_type="image",
                drawing_type="unknown",
                page_number=1,
                page_count=1,
                image_path=saved_path,
                metadata={
                    "rendered_from_image": True,
                    "render_dpi": dpi,
                    "image_format": normalized_format,
                },
            ),
            "image_bytes": image_bytes,
            "image_format": normalized_format,
            "mime_type": self._mime_type_from_format(normalized_format),
            "dpi": dpi,
        }

    def _mime_type_from_format(self, image_format: str) -> str:
        """Map normalized image format to MIME type."""
        normalized_format = self._normalize_image_format(image_format)
        return "image/png" if normalized_format == "png" else "image/jpeg"

    def _mime_type_from_suffix(self, suffix: str) -> str:
        """Map a file suffix to MIME type."""
        normalized_suffix = str(suffix or "").strip().lower()
        if normalized_suffix == ".png":
            return "image/png"
        if normalized_suffix in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if normalized_suffix == ".bmp":
            return "image/bmp"
        if normalized_suffix == ".gif":
            return "image/gif"
        if normalized_suffix == ".webp":
            return "image/webp"
        return "application/octet-stream"

    def _extract_pdf_page(self, path: Path, page_number: int) -> Tuple[str, bytes, int]:
        if fitz is None:
            raise RuntimeError("解析 PDF 需要安装 PyMuPDF（fitz）。")

        document = fitz.open(path)
        try:
            page_count = int(document.page_count)
            if page_number < 1 or page_number > page_count:
                raise ValueError(f"页码超出范围，可选范围为 1 到 {page_count}")
            page = document.load_page(page_number - 1)
            pixmap = page.get_pixmap(dpi=200, alpha=False)
            image_bytes = pixmap.tobytes("png")
        finally:
            document.close()

        image_name = f"{path.stem}_page_{page_number}_{uuid.uuid4().hex[:8]}.png"
        image_path = os.path.join(self.output_dir, image_name)
        with open(image_path, "wb") as file_obj:
            file_obj.write(image_bytes)
        return image_path, image_bytes, page_count

    def _build_prompt(self, document: DrawingDocument, focus_hint: Optional[str]) -> str:
        prompt = (
            f"请识别这张 {document.drawing_type} 工程图，优先输出可用于 3D 配管建模的结构化数据。"
            " 重点提取节点、直管段、弯头、三通、异径管、法兰、阀门、管径、标高、走向、关键尺寸标注以及任何需要人工确认的项。"
        )
        if focus_hint:
            prompt = f"{prompt}\n附加关注点：{focus_hint}"
        return prompt

    def _build_document_visual_prompt(
        self,
        *,
        document: DrawingDocument,
        prompt: Optional[str],
        total_pages: int,
    ) -> str:
        """Build a concise prompt for rendered engineering-document pages."""
        base_prompt = (
            f"请分析第 {document.page_number} 页工程图页面（共 {total_pages} 页），"
            "优先识别图纸类型、主要视图、关键尺寸、标高、DN、设备名称、管线走向和表格信息。"
        )
        if prompt:
            return f"{base_prompt}\n附加要求：{prompt}"
        return base_prompt

    def _build_visual_analysis_summary(self, page_results: List[Dict[str, Any]]) -> str:
        """Collapse per-page visual results into a one-line summary."""
        if not page_results:
            return "未生成任何页面分析结果。"
        summaries = []
        for item in page_results:
            page_no = item.get("page_number")
            page_summary = str(item.get("summary", "")).strip()
            if page_summary:
                summaries.append(f"第{page_no}页：{page_summary}")
        if summaries:
            return "；".join(summaries)
        return f"已完成 {len(page_results)} 页工程图视觉分析。"

    def _coerce_payload(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                return json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError("recognized_data 不是合法 JSON 字符串。") from exc
        raise ValueError("recognized_data 必须是 JSON 对象或 JSON 字符串。")

    def _default_view_semantics(self) -> Dict[str, Any]:
        """返回三视图面语义默认结构。"""
        return {
            "is_three_view": False,
            "mapping_rule": "前视图=主视图，俯视图=俯视图，左视图=左视图",
            "detected_views": [],
        }

    def _build_region_split_skeleton(self, document: DrawingDocument) -> List[RecognitionRegion]:
        region_specs = [
            (
                "main-view",
                "main_view",
                "front",
                "主视图",
                "优先读取主体轮廓、总高、关键标高和主要直径。",
                ["overall_height", "major_diameter", "elevation"],
            ),
            (
                "top-view",
                "top_view",
                "top",
                "俯视图",
                "优先读取周向方位、中心圆和口件夹角关系。",
                ["angular_position", "radial_offset", "center_circle"],
            ),
            (
                "bom-table",
                "bom_table",
                None,
                "明细表",
                "优先读取编号件、数量、规格、材料和标准号。",
                ["item_no", "quantity", "spec", "material", "standard"],
            ),
            (
                "detail-view",
                "detail_view",
                None,
                "局部详图",
                "优先读取法兰型式、孔距、厚度、伸出量和局部补充尺寸。",
                ["flange_type", "bolt_circle", "thickness", "projection_length"],
            ),
            (
                "section-view",
                "section_view",
                "front",
                "剖面图",
                "优先读取剖面关系、接管插入深度、法兰贴合关系和焊接详图。",
                ["insert_depth", "fit_up", "weld_type", "section_relation"],
            ),
        ]
        return [
            RecognitionRegion(
                region_id=region_id,
                region_type=region_type,
                page_number=document.page_number,
                source_view=source_view,
                label=label,
                description=description,
                extracted_fields=fields,
                metadata={"parser_generated": True},
            )
            for region_id, region_type, source_view, label, description, fields in region_specs
        ]

    def _normalize_recognition_regions(
        self,
        payload: Any,
        *,
        document: DrawingDocument,
    ) -> List[Dict[str, Any]]:
        if isinstance(payload, list) and payload:
            return [RecognitionRegion.from_dict(item).to_dict() for item in payload if isinstance(item, dict)]
        return [region.to_dict() for region in self._build_region_split_skeleton(document)]

    def _normalize_view_interpretations(
        self,
        payload: Any,
        view_semantics: Any,
        recognition_regions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if isinstance(payload, list) and payload:
            return [ViewInterpretation.from_dict(item).to_dict() for item in payload if isinstance(item, dict)]

        region_by_view: Dict[str, str] = {}
        for region in recognition_regions:
            if not isinstance(region, dict):
                continue
            source_view = region.get("source_view")
            region_id = region.get("region_id")
            if source_view and region_id and source_view not in region_by_view:
                region_by_view[str(source_view)] = str(region_id)

        normalized: List[Dict[str, Any]] = []
        detected_views = []
        if isinstance(view_semantics, dict):
            detected_views = view_semantics.get("detected_views") or []
        for index, item in enumerate(detected_views):
            if not isinstance(item, dict):
                continue
            canonical_view = str(item.get("canonical_view", ""))
            normalized.append(
                ViewInterpretation(
                    view_id=f"view-{index + 1}",
                    source_view=str(item.get("view_label", canonical_view or f"view-{index + 1}")),
                    canonical_role=canonical_view,
                    confidence=float(item["confidence"]) if item.get("confidence") is not None else None,
                    evidence=[str(item.get("surface_label", "")), str(view_semantics.get("mapping_rule", ""))],
                    region_id=region_by_view.get(canonical_view),
                    metadata={"parser_generated": True},
                ).to_dict()
            )

        if normalized:
            return normalized

        return [
            ViewInterpretation(
                view_id="view-front",
                source_view="主视图",
                canonical_role="front",
                confidence=None,
                evidence=["parser_default"],
                region_id=region_by_view.get("front", "main-view"),
                metadata={"parser_generated": True},
            ).to_dict()
        ]

    def _normalize_dimension_ledger(
        self,
        payload: Any,
        dimensions_payload: Any,
        recognition_regions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if isinstance(payload, list) and payload:
            return [DimensionLedgerEntry.from_dict(item).to_dict() for item in payload if isinstance(item, dict)]

        front_region_id = None
        for region in recognition_regions:
            if isinstance(region, dict) and region.get("source_view") == "front":
                front_region_id = region.get("region_id")
                break
        entries: List[Dict[str, Any]] = []
        if isinstance(dimensions_payload, list):
            for item in dimensions_payload:
                if not isinstance(item, dict):
                    continue
                dimension = DimensionLedgerEntry.from_dimension_spec(
                    pipeline_model_from_payload({"document": {}, "dimensions": [item]}).dimensions[0],
                    confidence=None,
                    verified=False,
                    is_key=True,
                    source_region=front_region_id,
                )
                entries.append(dimension.to_dict())
        return entries

    def _normalize_bom_closure(self, payload: Any) -> List[Dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        return [BomClosureEntry.from_dict(item).to_dict() for item in payload if isinstance(item, dict)]

    def _normalize_section_details(
        self,
        payload: Any,
        recognition_regions: List[Dict[str, Any]],
        view_interpretations: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if isinstance(payload, list) and payload:
            return [SectionDetail.from_dict(item).to_dict() for item in payload if isinstance(item, dict)]

        interpretation_by_role = {}
        for item in view_interpretations:
            if not isinstance(item, dict):
                continue
            role = item.get("canonical_role")
            if role and role not in interpretation_by_role:
                interpretation_by_role[str(role)] = item

        details: List[Dict[str, Any]] = []
        for region in recognition_regions:
            if not isinstance(region, dict):
                continue
            region_type = str(region.get("region_type", ""))
            if region_type not in {"detail_view", "section_view"}:
                continue
            source_view = region.get("source_view")
            role_hint = interpretation_by_role.get(str(source_view))
            notes = []
            if region_type == "section_view":
                notes.append("优先用于剖面关系、插入深度和壳体内外连接面的 3D 建模。")
            else:
                notes.append("优先用于法兰、接管、人孔和局部焊接详图的 3D 建模。")
            if role_hint:
                notes.append(f"对应视图语义: {role_hint.get('source_view')}")
            cut_line_description = ""
            view_direction = ""
            removed_side = ""
            retained_side = ""
            projection_basis = ""
            direction_confidence = None
            if region_type == "section_view":
                title = str(region.get("label") or "剖面图")
                source_view_text = str(source_view or "原始视图")
                cut_line_description = f"{title} 的剖切线需回到 {source_view_text} 对应原视图中确认具体位置。"
                view_direction = "待根据剖切箭头方向确认"
                removed_side = "待确认"
                retained_side = "待确认"
                projection_basis = "优先依据剖切线所在原视图、剖切箭头方向和投影关系判断"
                direction_confidence = 0.0
                notes.append("先确认剖切线所在视图和箭头方向，再决定 3D 中的朝向与连接方向。")
            details.append(
                SectionDetail(
                    section_id=str(region.get("region_id") or f"section-{len(details) + 1}"),
                    section_type=region_type,
                    title=str(region.get("label") or "局部详图"),
                    source_view=str(source_view) if source_view else None,
                    region_id=str(region.get("region_id")) if region.get("region_id") else None,
                    purpose=str(region.get("description") or ""),
                    cut_line_description=cut_line_description,
                    view_direction=view_direction,
                    removed_side=removed_side,
                    retained_side=retained_side,
                    projection_basis=projection_basis,
                    related_items=[str(item) for item in region.get("extracted_fields", [])],
                    extracted_fields=[str(item) for item in region.get("extracted_fields", [])],
                    modeling_notes=notes,
                    confidence=region.get("confidence"),
                    direction_confidence=direction_confidence,
                    metadata={"parser_generated": True},
                ).to_dict()
            )
        return details

    def _ensure_review_issues(self, model: PipelineSemanticModel) -> None:
        issues = list(model.review_issues)
        if model.document.drawing_type != "isometric":
            issues.append(
                ReviewIssue(
                    issue_id=f"review-{len(issues) + 1}",
                    severity="warning",
                    category="manual_check",
                    message="当前图纸并非轴测图，生成 3D 配管前需要人工补充几何和标高信息。",
                    field_path="document.drawing_type",
                )
            )

        if not model.graph.segments:
            issues.append(
                ReviewIssue(
                    issue_id=f"review-{len(issues) + 1}",
                    severity="critical",
                    category="missing_field",
                    message="未识别到任何直管段，无法生成 3D 配管。",
                    field_path="graph.segments",
                )
            )

        for index, segment in enumerate(model.graph.segments):
            if segment.dn is None and segment.outer_diameter is None:
                issues.append(
                    ReviewIssue(
                        issue_id=f"review-{len(issues) + 1}",
                        severity="warning",
                        category="missing_field",
                        message=f"管段 {segment.segment_id or index + 1} 缺少管径信息。",
                        field_path=f"graph.segments.{index}.dn",
                    )
                )
            if segment.start_point is None and not segment.start_node:
                issues.append(
                    ReviewIssue(
                        issue_id=f"review-{len(issues) + 1}",
                        severity="critical",
                        category="missing_field",
                        message=f"管段 {segment.segment_id or index + 1} 缺少起点信息。",
                        field_path=f"graph.segments.{index}.start_point",
                    )
                )
            if segment.end_point is None and not segment.end_node:
                issues.append(
                    ReviewIssue(
                        issue_id=f"review-{len(issues) + 1}",
                        severity="critical",
                        category="missing_field",
                        message=f"管段 {segment.segment_id or index + 1} 缺少终点信息。",
                        field_path=f"graph.segments.{index}.end_point",
                    )
                )

        for index, dimension in enumerate(model.dimensions):
            normalized_type = (dimension.dimension_type or "aligned").lower()
            if normalized_type in {"aligned", "horizontal", "vertical"}:
                if dimension.start_point is None:
                    issues.append(
                        ReviewIssue(
                            issue_id=f"review-{len(issues) + 1}",
                            severity="warning",
                            category="missing_field",
                            message=f"尺寸标注 {dimension.dimension_id or index + 1} 缺少起点信息。",
                            field_path=f"dimensions.{index}.start_point",
                        )
                    )
                if dimension.end_point is None:
                    issues.append(
                        ReviewIssue(
                            issue_id=f"review-{len(issues) + 1}",
                            severity="warning",
                            category="missing_field",
                            message=f"尺寸标注 {dimension.dimension_id or index + 1} 缺少终点信息。",
                            field_path=f"dimensions.{index}.end_point",
                        )
                    )
            elif normalized_type == "diameter":
                if dimension.chord_point is None or dimension.far_chord_point is None:
                    issues.append(
                        ReviewIssue(
                            issue_id=f"review-{len(issues) + 1}",
                            severity="warning",
                            category="missing_field",
                            message=f"直径标注 {dimension.dimension_id or index + 1} 缺少弦点信息。",
                            field_path=f"dimensions.{index}.chord_point",
                        )
                    )
            elif normalized_type == "radius":
                if dimension.center is None or dimension.chord_point is None:
                    issues.append(
                        ReviewIssue(
                            issue_id=f"review-{len(issues) + 1}",
                            severity="warning",
                            category="missing_field",
                            message=f"半径标注 {dimension.dimension_id or index + 1} 缺少圆心或弦点信息。",
                            field_path=f"dimensions.{index}.center",
                        )
                    )

        if not model.recognition_regions:
            issues.append(
                ReviewIssue(
                    issue_id=f"review-{len(issues) + 1}",
                    severity="warning",
                    category="manual_check",
                    message="当前识图结果未形成分区识图骨架，建议补充主视图、俯视图、明细表和局部详图区域。",
                    field_path="recognition_regions",
                )
            )

        if not model.dimension_ledger and model.document.drawing_type != "isometric":
            issues.append(
                ReviewIssue(
                    issue_id=f"review-{len(issues) + 1}",
                    severity="warning",
                    category="missing_field",
                    message="当前图纸未沉淀结构化尺寸台账，后续建模前应补充关键尺寸来源。",
                    field_path="dimension_ledger",
                )
            )

        if model.recognition_regions and not model.bom_closure:
            region_types = {region.region_type for region in model.recognition_regions}
            if "bom_table" in region_types:
                issues.append(
                    ReviewIssue(
                        issue_id=f"review-{len(issues) + 1}",
                        severity="warning",
                        category="missing_field",
                        message="识图已包含明细表区域，但尚未形成编号件闭环表。",
                        field_path="bom_closure",
                    )
                )

        if model.recognition_regions and not model.section_details:
            region_types = {region.region_type for region in model.recognition_regions}
            if "detail_view" in region_types or "section_view" in region_types:
                issues.append(
                    ReviewIssue(
                        issue_id=f"review-{len(issues) + 1}",
                        severity="warning",
                        category="missing_field",
                        message="识图已包含局部详图或剖面图区域，但尚未形成剖面/详图信息表。",
                        field_path="section_details",
                    )
                )

        if issues:
            model.review_issues = issues
            model.review_status = "pending"
        else:
            model.review_status = "approved"
