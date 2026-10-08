import os
import sys
import json
import logging
import re
import shutil
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from pydantic import AnyUrl
from typing import Any, Dict, List
import sys

try:
    from mcp.server.models import InitializationOptions
    import mcp.types as types
    from mcp.server import NotificationOptions, Server
    import mcp.server.stdio
    MCP_RUNTIME_AVAILABLE = True
except ImportError:
    MCP_RUNTIME_AVAILABLE = False

    class _McpPlaceholder:
        def __init__(self, *args, **kwargs):
            self.args = args
            for key, value in kwargs.items():
                setattr(self, key, value)

    class _McpTypesModule:
        Resource = _McpPlaceholder
        Tool = _McpPlaceholder
        TextContent = _McpPlaceholder
        ImageContent = _McpPlaceholder
        EmbeddedResource = _McpPlaceholder
        Prompt = _McpPlaceholder
        GetPromptResult = _McpPlaceholder
        PromptMessage = _McpPlaceholder

    class InitializationOptions(_McpPlaceholder):
        pass

    class NotificationOptions(_McpPlaceholder):
        pass

    class Server:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def _decorator(self):
            def wrapper(func):
                return func

            return wrapper

        def list_resources(self):
            return self._decorator()

        def read_resource(self):
            return self._decorator()

        def list_tools(self):
            return self._decorator()

        def call_tool(self):
            return self._decorator()

        def list_prompts(self):
            return self._decorator()

        def get_prompt(self):
            return self._decorator()

        async def run(self, *args, **kwargs):
            raise RuntimeError("当前环境未安装 mcp，无法启动 MCP 服务模式。")

    class _DummyStdioServer:
        async def __aenter__(self):
            return None, None

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _DummyStdioModule:
        @staticmethod
        def stdio_server():
            return _DummyStdioServer()

    class _DummyServerModule:
        stdio = _DummyStdioModule()

    class _DummyMcpModule:
        server = _DummyServerModule()

    types = _McpTypesModule()
    mcp = _DummyMcpModule()

sys.dont_write_bytecode = True

# 使用绝对导入

from nlp_processor import NLPProcessor

from cad_controller import CADController
from drawing_parser import DrawingParser
from global_drawing_experience import build_pre_draw_brief, build_save_strategy, build_validation_rule_plan
from pipeline_builder import PipelineBuilder
from pipeline_models import DimensionSpec, PipelineSemanticModel
from vision_loop import VisionLoop


# 配置Windows环境下的UTF-8编码
if sys.platform == "win32" and os.environ.get('PYTHONIOENCODING') is None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('cad_mcp.log', encoding='utf-8')
    ]
)


logger = logging.getLogger('mcp_cad_server')
logger.info("启动 CAD MCP 服务器")


PROMPT_TEMPLATE = """
助手的目标是演示如何使用CAD MCP服务。通过这个服务，您可以直接在对话中控制CAD软件，创建和修改图形。

<cad-mcp>

提示：
这个服务器提供了一个名为"cad-assistant"的预设提示，帮助用户通过自然语言指令控制CAD。用户可以要求绘制各种形状、修改图形元素或保存图纸。

资源：
此服务器提供"drawing://current"资源，表示当前的CAD图纸状态。
此服务器还提供"vision://latest"资源，表示最近一次视觉反馈状态；当存在主动分析任务时，还可读取"vision://jobs/<job_id>"。

工具：

此服务器提供以下基本绘图工具：
"draw_line": 在CAD中绘制直线
"draw_circle": 在CAD中绘制圆
"draw_arc": 在CAD中绘制弧
"draw_ellipse": 在CAD中绘制椭圆
"draw_polyline": 在CAD中绘制多段线
"draw_rectangle": 在CAD中绘制矩形
"draw_text": 在CAD中添加文本
"draw_mtext": 在CAD中添加多行文字
"draw_hatch": 在CAD中绘制填充
"draw_spline": 在CAD中绘制样条曲线
"draw_sphere": 在CAD中绘制球体（3D）
"draw_box": 在CAD中绘制长方体/正方体（3D）
"draw_cylinder": 在CAD中绘制圆柱体（3D，沿Z轴）
"draw_cylinder_along_y": 在CAD中绘制沿Y轴圆柱体（3D）
"draw_cone": 在CAD中绘制圆锥体（3D）
"draw_torus": 在CAD中绘制圆环体（3D）
"draw_pipe_elbow": 在CAD中绘制管道弯头（45°/90°/180°，LR/SR，默认90°长半径，XY平面）
"draw_reducer": 在CAD中绘制异径管（同心/偏心，国标GB/T 12459，dn_large/dn_small）
"draw_tee": 在CAD中绘制三通（3D，国标GB/T 12459-2017；branch_dn 为空时为等径三通，branch_dn 小于 dn 时为异径三通）
"draw_wedge": 在CAD中绘制楔体（3D）
"draw_flange": 在CAD中绘制法兰盘（3D，国标GB/T 9119或自定义尺寸，带中心孔与螺栓孔）
"draw_blind_flange": 在CAD中绘制盲板/法兰盖（3D，国标，无中心孔，用于封堵管口）
"draw_bolt": 在CAD中绘制六角头螺栓/螺丝（3D，可按 GB/T 5782、GB/T 5783 国标查表）
"draw_hex_nut": 在CAD中绘制六角螺母（3D，可按 GB/T 6170.1 国标查表）
"draw_washer": 在CAD中绘制平垫圈（3D，可按 GB/T 97.1 国标查表）
"draw_ball_valve": 在CAD中绘制球阀（3D，国标GB/T 12237，需dn、pn）
"draw_butterfly_valve": 在CAD中绘制蝶阀（3D，国标GB/T 12238，需dn、pn）
"draw_globe_valve": 在CAD中绘制截止阀（3D，国标GB/T 12235，需dn、pn）
"draw_check_valve": 在CAD中绘制止回阀（3D，国标GB/T 12236，默认PN16）
"draw_elliptical_head": 在CAD中绘制椭圆封头（3D，国标GB/T 25198，需dn，默认仅椭圆曲面无直边）
"draw_hemisphere": 在CAD中绘制半球（3D）
"draw_hemisphere_extruded_y": 在CAD中绘制半球并沿Y轴拉伸（3D）
"zoom_extents": 缩放到显示所有对象
"boolean_union": 对两个3D实体做布尔并集（需提供Handle）
"boolean_intersect": 对两个3D实体做布尔交集（需提供Handle）
"boolean_subtract": 对两个3D实体做布尔差集（需提供Handle）
"rotate_entity": 旋转2D/3D实体（需提供Handle、base_point、angle；沿某一点用axis_direction，沿某条线用axis_point2）
"extrude_entity": 3D拉伸：将2D封闭图形沿Z轴或沿路径拉伸为3D实体（Handle+height 或 Handle+path_handle）
"revolve_entity": 将2D封闭图形绕轴旋转一周生成3D实体（Handle+axis_point+axis_direction，默认360°）
"save_drawing": 保存当前图纸
"add_dimension": 在CAD中添加尺寸标注
"apply_dimensions": 在CAD中批量添加尺寸标注
"process_command": 处理自然语言命令并转换为CAD操作
"analyze_document_visual": 将 PDF 或图片先转换为高清 PNG/JPG，再交给视觉模型分析
"get_latest_vision_feedback": 获取最近一次视觉反馈 JSON
"analyze_current_view": 主动触发一次当前 CAD 视图分析
"set_vision_loop_enabled": 启用或停用视觉轮询回路

</cad-mcp>

请以友好的方式开始演示，例如："嗨！今天我将向您展示如何使用CAD MCP服务。通过这个服务，您可以直接在我们的对话中控制CAD软件，无需手动操作界面。让我们开始吧！"
"""

# 后期再做
# "erase_entity": 删除指定的实体
# "move_entity": 移动指定的实体
# "scale_entity": 缩放指定的实体

VISION_SCHEMA_VERSION = "1.1.0"
VISION_LATEST_RESOURCE_URI = "vision://latest"
VISION_JOB_RESOURCE_PREFIX = "vision://jobs/"
VISION_STATUS_ENUM = ["idle", "analyzing", "success", "error"]

VIEW_SEMANTICS_SCHEMA = {
    "type": "object",
    "description": "三视图和当前画面对应面的语义信息。",
    "properties": {
        "is_three_view": {
            "type": "boolean",
            "description": "当前结果是否识别为三视图或包含三视图语义"
        },
        "mapping_rule": {
            "type": "string",
            "description": "三视图固定映射规则说明"
        },
        "detected_views": {
            "type": "array",
            "description": "识别到的视图及其对应面",
            "items": {
                "type": "object",
                "properties": {
                    "view_label": {"type": "string", "description": "图中视图标签，如前视图、主视图"},
                    "canonical_view": {"type": "string", "description": "标准视图名，如 front、top、left"},
                    "surface_label": {"type": "string", "description": "该视图对应的中文面语义，如前面、上面、左面"},
                    "confidence": {"type": ["number", "null"], "description": "该视图判断的置信度"}
                },
                "required": ["view_label", "canonical_view", "surface_label"],
                "additionalProperties": True
            }
        }
    },
    "required": ["is_three_view", "mapping_rule", "detected_views"],
    "additionalProperties": True
}

VISION_FEEDBACK_SCHEMA = {
    "type": "object",
    "description": "视觉反馈统一 JSON 返回结构。tool 与 resource 均返回该结构，必要时附加 contract 元数据。",
    "properties": {
        "status": {
            "type": "string",
            "enum": VISION_STATUS_ENUM,
            "description": "视觉状态：idle=空闲，analyzing=分析中，success=成功，error=失败"
        },
        "captured_at": {
            "type": ["string", "null"],
            "description": "截图时间，ISO 8601 UTC 字符串；尚未截图时为 null"
        },
        "window_title": {
            "type": ["string", "null"],
            "description": "被分析的 CAD 窗口标题"
        },
        "frame_hash": {
            "type": ["string", "null"],
            "description": "截图帧哈希，用于去重和追踪"
        },
        "summary": {
            "type": "string",
            "description": "面向上层代理的简要总结"
        },
        "findings": {
            "type": "array",
            "description": "结构化发现列表，允许后续 sidecar 按需补充字段",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "发现标签"},
                    "detail": {"type": "string", "description": "发现详情"},
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "critical"],
                        "description": "发现严重程度"
                    },
                    "bbox": {
                        "type": ["array", "null"],
                        "description": "可选区域框 [x1, y1, x2, y2]",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4
                    }
                },
                "required": ["label", "detail"],
                "additionalProperties": True
            }
        },
        "suggested_action": {
            "type": "string",
            "description": "建议的下一步动作或提示"
        },
        "confidence": {
            "type": ["number", "null"],
            "description": "模型置信度，范围通常为 0 到 1"
        },
        "view_semantics": deepcopy(VIEW_SEMANTICS_SCHEMA),
        "provider": {
            "type": ["string", "null"],
            "description": "视觉提供方，例如 qwen"
        },
        "model": {
            "type": ["string", "null"],
            "description": "视觉模型名称"
        },
        "latency_ms": {
            "type": ["number", "null"],
            "description": "视觉分析耗时（毫秒）"
        },
        "schema_version": {
            "type": "string",
            "description": "视觉反馈契约版本"
        },
        "resource_uri": {
            "type": "string",
            "description": "当前结果对应的 MCP resource URI"
        },
        "job_id": {
            "type": ["string", "null"],
            "description": "主动分析任务 ID；非任务型反馈可为 null"
        },
        "loop_enabled": {
            "type": "boolean",
            "description": "视觉轮询是否启用"
        }
    },
    "required": [
        "status",
        "captured_at",
        "window_title",
        "frame_hash",
        "summary",
        "findings",
        "suggested_action",
        "confidence",
        "view_semantics",
        "provider",
        "model",
        "latency_ms",
        "schema_version",
        "resource_uri",
        "job_id",
        "loop_enabled"
    ],
    "additionalProperties": True
}

GET_LATEST_VISION_FEEDBACK_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "include_schema": {
            "type": "boolean",
            "description": "是否在返回中附带完整视觉契约元数据，默认 false"
        }
    }
}

ANALYZE_CURRENT_VIEW_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "对本次视觉分析的补充说明，例如“检查当前视图里是否缺少尺寸标注”"
        },
        "focus_hint": {
            "type": "string",
            "description": "可选关注区域或视角提示，例如“右上角标题栏”或“主视图中心区域”"
        },
        "force": {
            "type": "boolean",
            "description": "是否忽略帧差与节流策略，强制发起一次分析，默认 false"
        },
        "include_schema": {
            "type": "boolean",
            "description": "是否在返回中附带完整视觉契约元数据，默认 false"
        }
    }
}

SET_VISION_LOOP_ENABLED_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "enabled": {
            "type": "boolean",
            "description": "是否启用视觉轮询回路"
        },
        "reason": {
            "type": "string",
            "description": "可选说明，便于记录启停原因"
        },
        "include_schema": {
            "type": "boolean",
            "description": "是否在返回中附带完整视觉契约元数据，默认 false"
        }
    },
    "required": ["enabled"]
}


def _utc_now_iso() -> str:
    """返回 UTC ISO 8601 时间字符串。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _build_vision_feedback(
    *,
    status: str = "idle",
    captured_at: str | None = None,
    window_title: str | None = None,
    frame_hash: str | None = None,
    summary: str = "视觉反馈尚未初始化。",
    findings: list[dict[str, Any]] | None = None,
    suggested_action: str = "接入 Vision Sidecar 后即可写入真实视觉结果。",
    confidence: float | None = None,
    view_semantics: Dict[str, Any] | None = None,
    provider: str | None = None,
    model: str | None = None,
    latency_ms: float | None = None,
    resource_uri: str = VISION_LATEST_RESOURCE_URI,
    job_id: str | None = None,
    loop_enabled: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """构造统一的视觉反馈 JSON。"""
    payload = {
        "status": status,
        "captured_at": captured_at,
        "window_title": window_title,
        "frame_hash": frame_hash,
        "summary": summary,
        "findings": findings or [],
        "suggested_action": suggested_action,
        "confidence": confidence,
        "view_semantics": deepcopy(view_semantics)
        if isinstance(view_semantics, dict)
        else {
            "is_three_view": False,
            "mapping_rule": "前视图=主视图，俯视图=top view，左视图=left view",
            "detected_views": [],
        },
        "provider": provider,
        "model": model,
        "latency_ms": latency_ms,
        "schema_version": VISION_SCHEMA_VERSION,
        "resource_uri": resource_uri,
        "job_id": job_id,
        "loop_enabled": loop_enabled,
    }
    payload.update(extra)
    return payload


def _build_vision_contract_metadata() -> dict[str, Any]:
    """返回视觉 MCP 契约说明，便于上层代理动态发现输入与输出结构。"""
    return {
        "schema_version": VISION_SCHEMA_VERSION,
        "tools": [
            {
                "name": "get_latest_vision_feedback",
                "input_schema": deepcopy(GET_LATEST_VISION_FEEDBACK_INPUT_SCHEMA),
                "returns": "VISION_FEEDBACK_SCHEMA"
            },
            {
                "name": "analyze_current_view",
                "input_schema": deepcopy(ANALYZE_CURRENT_VIEW_INPUT_SCHEMA),
                "returns": "VISION_FEEDBACK_SCHEMA"
            },
            {
                "name": "set_vision_loop_enabled",
                "input_schema": deepcopy(SET_VISION_LOOP_ENABLED_INPUT_SCHEMA),
                "returns": "VISION_FEEDBACK_SCHEMA"
            }
        ],
        "resources": [
            {
                "name": "vision://latest",
                "description": "最近一次视觉反馈"
            },
            {
                "name": "vision://jobs/<job_id>",
                "description": "指定主动分析任务的视觉反馈结果"
            }
        ],
        "response_schema": deepcopy(VISION_FEEDBACK_SCHEMA)
    }

class Config:

    def __init__(self):
        # 直接读取config.json文件
        config_path = os.path.join(os.path.dirname(__file__), 'config.json')
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
        logger.info("配置文件加载成功")

    @property
    def server_name(self) -> str:
        return self.config['server']['name']

    @property
    def server_version(self) -> str:
        return self.config['server']['version']

    @property
    def vision_config(self) -> Dict[str, Any]:
        return self.config.get('vision', {})

class CADService:
    def __init__(self, vision_config: Dict[str, Any] | None = None):
        """初始化CAD服务"""
        self.controller = CADController()
        self.nlp_processor = NLPProcessor()
        self.workspace_output_dir = Path(__file__).resolve().parents[2] / "output"
        self.default_dwg_filename = "cad_drawing.dwg"
        self.default_folder_name = "未命名图纸"
        self.current_drawing_subject: str | None = None
        self.last_saved_path: str | None = None
        self.drawing_state = {
            "entities": [],
            "current_layer": "0",
            "last_command": "",
            "last_result": "",
            "drawing_subject": None,
            "last_pre_draw_brief": None,
            "last_validation_summary": None,
        }
        self.vision_loop = VisionLoop(
            cad_controller=self.controller,
            vision_config={
                **(vision_config or {}),
                "cad_type": self.controller.cad_type,
            },
        )
        self.controller.set_view_change_callback(self._handle_view_change)
        self.vision_loop_enabled = self.vision_loop.enabled
        parser_output_dir = Path(__file__).resolve().parent.parent / "output" / "parser"
        self.drawing_parser = DrawingParser(
            vision_config=vision_config,
            output_dir=str(parser_output_dir),
        )
        self.pipeline_builder = PipelineBuilder(self)
        initial_feedback = self.vision_loop.get_latest_feedback()
        initial_feedback["summary"] = "视觉模块已初始化，等待首次截图分析。"
        initial_feedback["suggested_action"] = "可调用 analyze_current_view 触发一次真实截图与视觉分析。"
        self.vision_state = _build_vision_feedback(**initial_feedback)
        self.vision_jobs: Dict[str, Dict[str, Any]] = {}
        self.drawing_state["last_pipeline_model"] = None
        logger.info("CAD服务已初始化")

    def _sanitize_output_name(self, value: Any, fallback: str) -> str:
        """清洗文件夹名或文件名，避免出现 Windows 非法字符。"""
        text = str(value or "").strip()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
        text = text.strip(" .")
        return (text or fallback)[:80]

    def _is_generic_subject(self, value: str | None) -> bool:
        """判断候选主题是否过于泛化，不适合做文件夹名。"""
        if not value:
            return True

        normalized = value.strip()
        generic_values = {
            "",
            "绘图",
            "图纸",
            "当前图纸",
            "未命名图纸",
            "cad图纸",
            "cad drawing",
            "drawing",
            "save",
        }
        return normalized.lower() in generic_values

    def _normalize_subject_candidate(self, value: Any) -> str | None:
        """把候选主题清洗为适合目录命名的内容描述。"""
        if value is None:
            return None

        text = str(value).strip()
        if not text:
            return None

        cleanup_patterns = [
            r"^(请|帮我|麻烦|我要|我想|需要|将|把)",
            r"^(绘制|画|创建|生成|做一个|做个|建一个|建个)",
            r"^(一个|一台|一套|一张|一份)",
            r"^根据结构化配管模型",
            r"^应用配管模型人工确认结果",
            r"^解析图纸",
            r"^视觉分析文档",
            r"^保存图纸到",
        ]
        for pattern in cleanup_patterns:
            text = re.sub(pattern, "", text).strip()

        text = re.sub(r"(，|,)?\s*(并)?\s*保存.*$", "", text).strip()
        text = re.sub(r"\s+", " ", text).strip(" ：:-_,，。")
        text = self._sanitize_output_name(text, self.default_folder_name)

        if self._is_generic_subject(text):
            return None
        return text

    def _update_current_drawing_subject(self, subject: Any) -> None:
        """记录当前绘图主题，供保存 DWG 时生成目录名。"""
        normalized = self._normalize_subject_candidate(subject)
        if normalized:
            self.current_drawing_subject = normalized
            self.drawing_state["drawing_subject"] = normalized

    def _resolve_pre_draw_subject(self, subject: Any | None = None) -> str:
        """解析绘前摘要使用的主题名。"""
        normalized = self._normalize_subject_candidate(subject)
        if normalized:
            return normalized

        fallback_candidates = [
            self.current_drawing_subject,
            self._derive_subject_from_pipeline_model(),
            self.drawing_state.get("drawing_subject"),
        ]
        for candidate in fallback_candidates:
            normalized = self._normalize_subject_candidate(candidate)
            if normalized:
                return normalized
        return self.default_folder_name

    def _refresh_pre_draw_brief(self, subject: Any | None = None, trigger: str | None = None) -> Dict[str, Any]:
        """生成并缓存最新的绘前检查摘要。"""
        drawing_subject = self._resolve_pre_draw_subject(subject)
        brief = build_pre_draw_brief(drawing_subject)
        brief["drawing_subject"] = drawing_subject
        brief["trigger"] = trigger
        brief["recommended_save_names"] = build_save_strategy(drawing_subject)
        self.drawing_state["last_pre_draw_brief"] = deepcopy(brief)
        return deepcopy(brief)

    def _attach_pre_draw_brief(
        self,
        payload: Dict[str, Any],
        subject: Any | None = None,
        trigger: str | None = None,
    ) -> Dict[str, Any]:
        """在返回结果中附带绘前检查摘要。"""
        payload["pre_draw_brief"] = self._refresh_pre_draw_brief(subject=subject, trigger=trigger)
        return payload

    def _build_pipeline_validation_summary(self, model: PipelineSemanticModel) -> Dict[str, Any]:
        issue_counts = {"critical": 0, "warning": 0, "info": 0}
        for issue in model.review_issues:
            issue_counts[issue.severity] = issue_counts.get(issue.severity, 0) + 1
        return {
            "review_status": model.review_status,
            "requires_review": bool(model.unresolved_issues),
            "issue_counts": issue_counts,
            "dimension_ledger_count": len(model.dimension_ledger),
            "bom_closure_count": len(model.bom_closure),
            "section_detail_count": len(model.section_details),
            "recognition_region_count": len(model.recognition_regions),
            "view_interpretation_count": len(model.view_interpretations),
            "recommended_rule_plan": build_validation_rule_plan(
                model.summary or model.document.metadata.get("title") or self.default_folder_name
            ),
        }

    def _attach_validation_summary(
        self,
        payload: Dict[str, Any],
        model: PipelineSemanticModel,
    ) -> Dict[str, Any]:
        summary = self._build_pipeline_validation_summary(model)
        payload["validation_summary"] = summary
        self.drawing_state["last_validation_summary"] = deepcopy(summary)
        return payload

    def _derive_subject_from_pipeline_model(self) -> str | None:
        """优先从识图后的结构化模型中提取图纸主题。"""
        payload = self.drawing_state.get("last_pipeline_model")
        if not isinstance(payload, dict):
            return None

        candidates = [
            payload.get("recognition_metadata", {}).get("title"),
            payload.get("document", {}).get("metadata", {}).get("title"),
            payload.get("document", {}).get("metadata", {}).get("equipment_name"),
            payload.get("document", {}).get("metadata", {}).get("drawing_name"),
            payload.get("summary"),
        ]
        source_path = payload.get("document", {}).get("source_path")
        if source_path:
            candidates.append(Path(str(source_path)).stem)

        for candidate in candidates:
            normalized = self._normalize_subject_candidate(candidate)
            if normalized:
                return normalized
        return None

    def _derive_output_folder_name(self, file_path: str | None) -> str:
        """根据内容推断 DWG 保存目录名。"""
        candidates = [
            self.current_drawing_subject,
            self._derive_subject_from_pipeline_model(),
            self.drawing_state.get("last_command"),
            Path(file_path).stem if file_path else None,
        ]
        for candidate in candidates:
            normalized = self._normalize_subject_candidate(candidate)
            if normalized:
                return normalized
        return self.default_folder_name

    def _should_use_subject_filename(self, requested_stem: str | None) -> bool:
        """判断是否应该自动改用内容主题作为文件名。"""
        if not requested_stem:
            return True

        normalized = self._normalize_subject_candidate(requested_stem)
        if not normalized:
            return True

        generic_stems = {
            "cad_drawing",
            "drawing",
            "output",
            "result",
            "image",
            "photo",
            "document",
            "file",
            "图纸",
            "图片",
            "照片",
            "文档",
            "文件",
            "未命名图纸",
        }
        return requested_stem.strip().lower() in generic_stems

    def _derive_output_base_name(self, file_path: str | None, suffix: str) -> str:
        """根据内容主题推断输出文件名主体。"""
        requested_stem = Path(file_path).stem if file_path else ""
        folder_name = self._derive_output_folder_name(file_path)

        if self._should_use_subject_filename(requested_stem):
            return self._sanitize_output_name(folder_name, suffix)
        return self._sanitize_output_name(requested_stem, suffix)

    def _resolve_dwg_output_path(self, file_path: str | None) -> str:
        """将图纸保存到 CAD/output/<内容名称>/ 文件夹。"""
        folder_name = self._derive_output_folder_name(file_path)
        output_dir = self.workspace_output_dir / folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        base_name = self._derive_output_base_name(file_path, self.default_dwg_filename.removesuffix(".dwg"))
        filename = f"{base_name}.dwg"
        return str((output_dir / filename).resolve())

    def _resolve_content_output_dir(self, file_path: str | None = None) -> Path:
        """根据当前内容主题解析统一输出目录。"""
        folder_name = self._derive_output_folder_name(file_path)
        output_dir = self.workspace_output_dir / folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _resolve_content_shared_dir(self, file_path: str | None = None) -> Path:
        """返回当前内容主题下的 shared 目录。"""
        shared_dir = self._resolve_content_output_dir(file_path) / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)
        return shared_dir

    def _write_shared_parse_json(
        self,
        file_path: str,
        model: PipelineSemanticModel,
    ) -> Dict[str, str]:
        """将列表、尺寸和剖面/详图信息拆分写入 shared 目录。"""
        shared_dir = self._resolve_content_shared_dir(file_path)
        base_name = self._derive_output_base_name(file_path, Path(file_path).stem or "source")

        list_info_path = shared_dir / f"{base_name}_list_info.json"
        dimension_info_path = shared_dir / f"{base_name}_dimension_info.json"
        section_info_path = shared_dir / f"{base_name}_section_info.json"

        list_info_path.write_text(
            json.dumps(
                {
                    "source_path": model.document.source_path,
                    "summary": model.summary,
                    "bom_closure": [entry.to_dict() for entry in model.bom_closure],
                    "recognition_regions": [
                        region.to_dict() for region in model.recognition_regions if region.region_type == "bom_table"
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
                    "source_path": model.document.source_path,
                    "summary": model.summary,
                    "dimension_ledger": [entry.to_dict() for entry in model.dimension_ledger],
                    "view_interpretations": [entry.to_dict() for entry in model.view_interpretations],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        section_info_path.write_text(
            json.dumps(
                {
                    "source_path": model.document.source_path,
                    "summary": model.summary,
                    "section_details": [entry.to_dict() for entry in model.section_details],
                    "recognition_regions": [
                        region.to_dict()
                        for region in model.recognition_regions
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

    def _archive_source_document(self, file_path: str) -> str | None:
        """将 PDF 或图片复制到对应内容目录，便于和 DWG 一起归档。"""
        source_path = Path(file_path).expanduser().resolve()
        if not source_path.exists() or not source_path.is_file():
            logger.warning(f"源文件不存在，跳过归档: {file_path}")
            return None

        output_dir = self._resolve_content_output_dir(str(source_path))
        base_name = self._derive_output_base_name(str(source_path), source_path.stem or "source")
        suffix = source_path.suffix or ""
        target_path = output_dir / f"{base_name}{suffix}"

        if source_path == target_path:
            return str(target_path)

        shutil.copy2(source_path, target_path)
        return str(target_path)

    def parse_drawing_document(
        self,
        file_path: str,
        drawing_type: str = "isometric",
        page_number: int = 1,
        recognized_data: Any | None = None,
        review_overrides: Dict[str, Any] | None = None,
        focus_hint: str | None = None,
    ) -> Dict[str, Any]:
        """解析 PDF 或图片图纸并生成结构化语义模型。"""
        result = self.drawing_parser.parse_document(
            file_path=file_path,
            drawing_type=drawing_type,
            page_number=page_number,
            recognized_data=recognized_data,
            focus_hint=focus_hint,
        )
        semantic_model = PipelineSemanticModel.from_dict(result["semantic_model"])
        if review_overrides:
            semantic_model.apply_review_resolutions(review_overrides)
            result["semantic_model"] = semantic_model.to_dict()
            result["review_issues"] = [issue.to_dict() for issue in semantic_model.review_issues]
            result["requires_review"] = bool(semantic_model.unresolved_issues)

        self.drawing_state["last_pipeline_model"] = result["semantic_model"]
        self._update_current_drawing_subject(
            semantic_model.summary
            or semantic_model.document.metadata.get("title")
            or Path(file_path).stem
        )
        archived_source_path = self._archive_source_document(file_path)
        if archived_source_path:
            result["archived_source_path"] = archived_source_path
            result["document"]["archived_source_path"] = archived_source_path
            result["semantic_model"].setdefault("document", {})["archived_source_path"] = archived_source_path
        result["shared_json_paths"] = self._write_shared_parse_json(file_path, semantic_model)
        self.drawing_state["last_command"] = f"解析图纸 {file_path}"
        self.drawing_state["last_result"] = "成功"
        result = self._attach_validation_summary(result, semantic_model)
        return self._attach_pre_draw_brief(result, subject=semantic_model.summary or Path(file_path).stem, trigger="parse_drawing_document")

    def apply_pipeline_review(
        self,
        semantic_model: Dict[str, Any],
        resolutions: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """对识别后的语义模型应用人工确认结果。"""
        model = PipelineSemanticModel.from_dict(semantic_model)
        model.apply_review_resolutions(resolutions or {})
        result = {
            "success": True,
            "summary": "已应用人工确认结果。",
            "semantic_model": model.to_dict(),
            "review_issues": [issue.to_dict() for issue in model.review_issues],
            "requires_review": bool(model.unresolved_issues),
            "review_status": model.review_status,
        }
        self.drawing_state["last_pipeline_model"] = result["semantic_model"]
        self._update_current_drawing_subject(model.summary)
        source_path = model.document.source_path or self.last_saved_path or ""
        if source_path:
            result["shared_json_paths"] = self._write_shared_parse_json(source_path, model)
        self.drawing_state["last_command"] = "应用配管模型人工确认结果"
        self.drawing_state["last_result"] = "成功"
        return self._attach_validation_summary(result, model)

    def analyze_document_visual(
        self,
        file_path: str,
        page_number: int = 1,
        analyze_all_pages: bool = False,
        max_pages: int = 5,
        dpi: int = 300,
        image_format: str = "png",
        prompt: str | None = None,
        focus_hint: str | None = None,
    ) -> Dict[str, Any]:
        """将 PDF 或图片转换为高清图片并交给视觉模型分析。"""
        result = self.drawing_parser.analyze_document_visual(
            file_path=file_path,
            page_number=page_number,
            analyze_all_pages=analyze_all_pages,
            max_pages=max_pages,
            dpi=dpi,
            image_format=image_format,
            prompt=prompt,
            focus_hint=focus_hint,
        )
        self._update_current_drawing_subject(result.get("summary") or Path(file_path).stem)
        archived_source_path = self._archive_source_document(file_path)
        if archived_source_path:
            result["archived_source_path"] = archived_source_path
        self.drawing_state["last_command"] = f"视觉分析文档 {file_path}"
        self.drawing_state["last_result"] = "成功"
        return self._attach_pre_draw_brief(
            result,
            subject=result.get("summary") or Path(file_path).stem,
            trigger="analyze_document_visual",
        )

    def draw_pipeline_from_json(
        self,
        semantic_model: Dict[str, Any],
        require_review: bool = True,
        zoom_after_draw: bool = True,
    ) -> Dict[str, Any]:
        """将结构化管网 JSON 编排成现有 CAD 原语绘图调用。"""
        model = PipelineSemanticModel.from_dict(semantic_model)
        result = self.pipeline_builder.build(
            model,
            require_review=require_review,
            zoom_extents=zoom_after_draw,
        )
        self.drawing_state["last_pipeline_model"] = model.to_dict()
        self._update_current_drawing_subject(model.summary)
        self.drawing_state["last_command"] = "根据结构化配管模型绘制 3D 配管"
        self.drawing_state["last_result"] = "成功" if result.get("success") else "失败"
        return self._attach_pre_draw_brief(
            result,
            subject=model.summary,
            trigger="draw_pipeline_from_json",
        )

    def _handle_view_change(self, source: str, payload: Dict[str, Any] | None = None) -> None:
        """接收 CAD 视图变化提示并转发给视觉模块。"""
        self.vision_loop.notify_view_changed(source, payload or {})
    
    def start_cad(self):
        """启动CAD"""
        result = self.controller.start_cad()
        if result:
            self._refresh_pre_draw_brief(trigger="start_cad")
        return result
    
    def draw_line(self, start_point, end_point, layer=None, color=None, lineweight=None):
        """绘制直线"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 使用当前图层或指定图层
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_line(start_point, end_point, current_layer, color, lineweight)
        if result:
            entity_info = {"type": "line", "start": start_point, "end": end_point, "layer": current_layer, "color": color, "lineweight": lineweight}
            if hasattr(result, 'Handle'):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制直线从{start_point}到{end_point}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        
        return result
    
    def draw_circle(self, center, radius, layer=None, color=None, lineweight=None):
        """绘制圆"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 使用当前图层或指定图层
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_circle(center, radius, current_layer, color, lineweight)
        if result:
            entity_info = {"type": "circle", "center": center, "radius": radius, "layer": current_layer, "color": color, "lineweight": lineweight}
            if hasattr(result, 'Handle'):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制圆，中心点{center}，半径{radius}，图层{current_layer}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        
        return result
    
    def draw_arc(self, center, radius, start_angle, end_angle, layer=None, color=None, lineweight=None):
        """绘制弧"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 使用当前图层或指定图层
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_arc(center, radius, start_angle, end_angle, current_layer, color, lineweight)

        if result:
            self.drawing_state["entities"].append({
                "type": "arc",
                "center": center,
                "radius": radius,
                "start_angle": start_angle,
                "end_angle": end_angle,
                "layer": current_layer,
                "color": color,
                "lineweight": lineweight
            })
            self.drawing_state["last_command"] = f"绘制弧，中心点{center}，半径{radius}，起始角度{start_angle}，结束角度{end_angle}，图层{current_layer}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        
        return result
    
    def draw_ellipse(self, center, major_axis, minor_axis, rotation=0, layer=None, color=None, lineweight=None):
        """绘制椭圆"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 使用当前图层或指定图层
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_ellipse(center, major_axis, minor_axis, rotation, current_layer, color, lineweight)

        if result:
            self.drawing_state["entities"].append({
                "type": "ellipse",
                "center": center,
                "major_axis": major_axis,
                "minor_axis": minor_axis,
                "rotation": rotation,
                "layer": current_layer,
                "color": color,
                "lineweight": lineweight
            })
            self.drawing_state["last_command"] = f"绘制椭圆，中心点{center}，长轴{major_axis}，短轴{minor_axis}，旋转角度{rotation}，图层{current_layer}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        
        return result
    
    def draw_polyline(self, points, closed=False, layer=None, color=None, lineweight=None):
        """绘制多段线"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 使用当前图层或指定图层
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_polyline(points, closed, current_layer, color, lineweight)
        if result:
            entity_info = {"type": "polyline", "points": points, "closed": closed, "layer": current_layer, "color": color, "lineweight": lineweight}
            if hasattr(result, 'Handle'):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制多段线，点集{points}，{'闭合' if closed else '不闭合'}，图层{current_layer}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        
        return result
    
    def draw_rectangle(self, corner1, corner2, layer=None, color=None, lineweight=None):
        """绘制矩形"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 使用当前图层或指定图层
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_rectangle(corner1, corner2, current_layer, color, lineweight)
        if result:
            entity_info = {"type": "rectangle", "corner1": corner1, "corner2": corner2, "layer": current_layer, "color": color, "lineweight": lineweight}
            if hasattr(result, 'Handle'):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制矩形，对角点{corner1}和{corner2}，图层{current_layer}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        
        return result
    
    def draw_text(self, position, text, height=2.5, rotation=0, layer=None, color=None):
        """添加文本"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 使用当前图层或指定图层
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_text(position, text, height, rotation, current_layer, color)
        if result:
            self.drawing_state["entities"].append({
                "type": "text",
                "position": position,
                "text": text,
                "height": height,
                "rotation": rotation,
                "layer": current_layer,
                "color": color
            })
            self.drawing_state["last_command"] = f"添加文本'{text}'，位置{position}，高度{height}，旋转{rotation}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        
        return result
    
    def draw_hatch(self, points, pattern_name="SOLID", scale=1.0, layer=None, color=None):
        """绘制填充"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 使用当前图层或指定图层
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.draw_hatch(points, pattern_name, scale, current_layer, color)
        if result:
            self.drawing_state["entities"].append({
                "type": "hatch",
                "points": points,
                "pattern_name": pattern_name,
                "scale": scale,
                "layer": current_layer,
                "color": color
            })
            self.drawing_state["last_command"] = f"绘制填充，点集{points}，图案{pattern_name}，比例{scale}，图层{current_layer}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        
        return result
    
    def add_dimension(
        self,
        start_point=None,
        end_point=None,
        text_position=None,
        textheight=5,
        layer=None,
        color=None,
        dimension_type="aligned",
        center=None,
        chord_point=None,
        far_chord_point=None,
        leader_length=None,
        source_view="front",
    ):
        """添加尺寸标注。"""
        if not self.controller.is_running():
            self.start_cad()
        
        # 使用当前图层或指定图层
        current_layer = layer or self.drawing_state["current_layer"]
        
        result = self.controller.add_dimension(
            start_point=start_point,
            end_point=end_point,
            text_position=text_position,
            textheight=textheight,
            layer=current_layer,
            color=color,
            dimension_type=dimension_type,
            center=center,
            chord_point=chord_point,
            far_chord_point=far_chord_point,
            leader_length=leader_length,
            source_view=source_view,
        )
        if result:
            self.drawing_state["entities"].append({
                "type": "dimension",
                "dimension_type": dimension_type,
                "start": start_point,
                "end": end_point,
                "text_position": text_position,
                "textheight": textheight,
                "center": center,
                "chord_point": chord_point,
                "far_chord_point": far_chord_point,
                "leader_length": leader_length,
                "source_view": source_view or "front",
                "layer": current_layer,
                "color": color
            })
            self.drawing_state["last_command"] = f"在主视图添加{dimension_type}标注"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        
        return result

    def apply_dimensions(self, dimensions: List[Dict[str, Any]] | List[DimensionSpec]) -> Dict[str, Any]:
        """批量添加尺寸标注。"""
        created_entities: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []

        for index, item in enumerate(dimensions or []):
            spec = item if isinstance(item, DimensionSpec) else DimensionSpec.from_dict(item)
            result = self.add_dimension(
                start_point=spec.start_point,
                end_point=spec.end_point,
                text_position=spec.text_position,
                textheight=spec.textheight or 5,
                layer=spec.layer,
                color=spec.color,
                dimension_type=spec.dimension_type,
                center=spec.center,
                chord_point=spec.chord_point,
                far_chord_point=spec.far_chord_point,
                leader_length=spec.leader_length,
                source_view=spec.source_view or "front",
            )
            entity_payload = {
                "dimension_id": spec.dimension_id or f"D{index + 1}",
                "dimension_type": spec.dimension_type,
                "source_view": spec.source_view or "front",
                "handle": str(result.Handle) if result is not None and hasattr(result, "Handle") else None,
            }
            if result:
                created_entities.append(entity_payload)
            else:
                failures.append(entity_payload)

        summary = f"已添加 {len(created_entities)} 个尺寸标注"
        if failures:
            summary = f"{summary}，失败 {len(failures)} 个"

        return {
            "success": not failures,
            "summary": summary,
            "created_count": len(created_entities),
            "failed_count": len(failures),
            "entities": created_entities,
            "failures": failures,
        }

    def draw_sphere(self, center, radius, layer=None, color=None):
        """绘制球体（3D实体）"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_sphere(center, radius, current_layer, color)
        if result:
            entity_info = {
                "type": "sphere",
                "center": center,
                "radius": radius,
                "layer": current_layer,
                "color": color
            }
            if hasattr(result, 'Handle'):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制球体，中心{center}，半径{radius}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_box(self, center, length, width, height, layer=None, color=None):
        """绘制长方体/正方体（3D实体）"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_box(center, length, width, height, current_layer, color)
        if result:
            entity_info = {
                "type": "box",
                "center": center,
                "length": length,
                "width": width,
                "height": height,
                "layer": current_layer,
                "color": color
            }
            if hasattr(result, 'Handle'):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制长方体，中心{center}，长{length}宽{width}高{height}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_hemisphere(self, center, radius, flat_normal=(0, 1, 0), layer=None, color=None):
        """绘制半球（3D实体）"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_hemisphere(center, radius, flat_normal, current_layer, color)
        if result:
            entity_info = {"type": "hemisphere", "center": center, "radius": radius, "flat_normal": flat_normal, "layer": current_layer, "color": color}
            if hasattr(result, 'Handle'):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制半球，中心{center}，半径{radius}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_cylinder_along_y(self, base_center, radius, height_y, layer=None, color=None):
        """绘制沿Y轴方向的圆柱体（3D实体）"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_cylinder_along_y(base_center, radius, height_y, current_layer, color)
        if result:
            entity_info = {"type": "cylinder_y", "base_center": base_center, "radius": radius, "height_y": height_y, "layer": current_layer, "color": color}
            if hasattr(result, 'Handle'):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制沿Y轴圆柱，底面中心{base_center}，半径{radius}，高度{height_y}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_hemisphere_extruded_y(self, center, radius, extrude_height, layer=None, color=None):
        """绘制半球并沿Y轴拉伸（3D实体）"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_hemisphere_extruded_y(center, radius, extrude_height, current_layer, color)
        if result:
            entity_info = {"type": "hemisphere_extruded_y", "center": center, "radius": radius, "extrude_height": extrude_height, "layer": current_layer, "color": color}
            if hasattr(result, 'Handle'):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制半球并沿Y拉伸，中心{center}，半径{radius}，拉伸{extrude_height}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_cylinder(self, center, radius, height, layer=None, color=None):
        """绘制圆柱体（3D实体，沿Z轴）"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_cylinder(center, radius, height, current_layer, color)
        if result:
            entity_info = {"type": "cylinder", "center": center, "radius": radius, "height": height, "layer": current_layer, "color": color}
            try:
                entity_info["handle"] = str(result.Handle)
            except Exception:
                pass
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制圆柱，底面中心{center}，半径{radius}，高度{height}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_cone(self, center, base_radius, height, layer=None, color=None):
        """绘制圆锥体（3D实体）"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_cone(center, base_radius, height, current_layer, color)
        if result:
            entity_info = {"type": "cone", "center": center, "base_radius": base_radius, "height": height, "layer": current_layer, "color": color}
            if hasattr(result, 'Handle'):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制圆锥，底面中心{center}，半径{base_radius}，高度{height}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_torus(self, center, major_radius, minor_radius, layer=None, color=None):
        """绘制圆环体（3D实体）"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_torus(center, major_radius, minor_radius, current_layer, color)
        if result:
            entity_info = {"type": "torus", "center": center, "major_radius": major_radius, "minor_radius": minor_radius, "layer": current_layer, "color": color}
            if hasattr(result, 'Handle'):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制圆环，中心{center}，主半径{major_radius}，管道半径{minor_radius}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_pipe_elbow(self, center, pipe_radius=None, dn=None, bend_radius=None, elbow_type="LR", angle=90, plane="xy", wall_thickness=None, layer=None, color=None):
        """绘制管道弯头（国标 GB/T 12459）。支持 45°/90°/180°；LR/SR/3D。可指定壁厚做空心。须指定 pipe_radius 或 dn。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_pipe_elbow(center, pipe_radius=pipe_radius, dn=dn, bend_radius=bend_radius,
                                                  elbow_type=elbow_type, angle=angle, plane=plane,
                                                  wall_thickness=wall_thickness, layer=current_layer, color=color)
        if result:
            entity_info = {"type": "pipe_elbow", "center": center, "pipe_radius": pipe_radius, "dn": dn,
                          "bend_radius": bend_radius, "elbow_type": elbow_type, "angle": angle, "plane": plane,
                          "wall_thickness": wall_thickness, "layer": current_layer, "color": color}
            if hasattr(result, 'Handle'):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制{angle}°{elbow_type}弯头，中心{center}" + (f" DN{dn}" if dn else f" 半径{pipe_radius}")
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_pipe_elbow_90(self, center, pipe_radius=None, dn=None, bend_radius=None, elbow_type="LR", plane="xy", wall_thickness=None, layer=None, color=None):
        """绘制90°管道弯头（GB/T 12459，可指定壁厚做空心）。须指定 pipe_radius 或 dn。"""
        return self.draw_pipe_elbow(center, pipe_radius=pipe_radius, dn=dn, bend_radius=bend_radius, elbow_type=elbow_type, angle=90, plane=plane, wall_thickness=wall_thickness, layer=layer, color=color)

    def draw_reducer(
        self,
        center,
        dn_large,
        dn_small,
        axis="z",
        reducer_type="concentric",
        length=None,
        wall_thickness=None,
        eccentric_direction=None,
        eccentric_offset=None,
        layer=None,
        color=None,
    ):
        """绘制异径管（国标 GB/T 12459）。支持同心异径管与偏心异径管。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_reducer(
            center,
            dn_large,
            dn_small,
            axis=axis,
            reducer_type=reducer_type,
            length=length,
            wall_thickness=wall_thickness,
            eccentric_direction=eccentric_direction,
            eccentric_offset=eccentric_offset,
            layer=current_layer,
            color=color,
        )
        if result:
            entity_info = {
                "type": "reducer",
                "center": center,
                "dn_large": dn_large,
                "dn_small": dn_small,
                "axis": axis,
                "reducer_type": reducer_type,
                "length": length,
                "wall_thickness": wall_thickness,
                "eccentric_direction": eccentric_direction,
                "eccentric_offset": eccentric_offset,
                "layer": current_layer,
                "color": color,
            }
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            fitting_name = "偏心异径管" if str(reducer_type).lower().startswith("ecc") else "同心异径管"
            self.drawing_state["last_command"] = f"绘制{fitting_name} DN{dn_large}→DN{dn_small}，中心{center}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_tee(
        self,
        center,
        dn,
        branch_dn=None,
        run_axis="x",
        branch_axis="z",
        wall_thickness=None,
        branch_wall_thickness=None,
        run_center_to_end=None,
        branch_center_to_end=None,
        layer=None,
        color=None,
        reinforce=False,
        reinforce_extra=None,
        reinforce_run_ratio=0.55,
        reinforce_branch_ratio=0.70,
        transition_ratio=0.0,
        junction_bulge=False,
        junction_bulge_extra=None,
    ):
        """绘制三通（国标 GB/T 12459-2017）。默认按国标常用 STD 系列壁厚生成空心流道。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_tee(
            center,
            dn,
            branch_dn=branch_dn,
            run_axis=run_axis,
            branch_axis=branch_axis,
            wall_thickness=wall_thickness,
            branch_wall_thickness=branch_wall_thickness,
            run_center_to_end=run_center_to_end,
            branch_center_to_end=branch_center_to_end,
            layer=current_layer,
            color=color,
            reinforce=reinforce, reinforce_extra=reinforce_extra,
            reinforce_run_ratio=reinforce_run_ratio, reinforce_branch_ratio=reinforce_branch_ratio,
            transition_ratio=transition_ratio, junction_bulge=junction_bulge, junction_bulge_extra=junction_bulge_extra,
        )
        if result:
            tee_type = "tee" if branch_dn in (None, dn) else "reducing_tee"
            entity_info = {
                "type": tee_type,
                "center": center,
                "dn": dn,
                "branch_dn": branch_dn,
                "run_axis": run_axis,
                "branch_axis": branch_axis,
                "wall_thickness": wall_thickness,
                "branch_wall_thickness": branch_wall_thickness,
                "run_center_to_end": run_center_to_end,
                "branch_center_to_end": branch_center_to_end,
                "layer": current_layer,
                "color": color,
            }
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            command_label = f"绘制等径三通 DN{dn}" if branch_dn in (None, dn) else f"绘制异径三通 DN{dn}x{branch_dn}"
            self.drawing_state["last_command"] = f"{command_label}，中心{center}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_wedge(self, center, length, width, height, layer=None, color=None):
        """绘制楔体（3D实体）"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_wedge(center, length, width, height, current_layer, color)
        if result:
            entity_info = {"type": "wedge", "center": center, "length": length, "width": width, "height": height, "layer": current_layer, "color": color}
            if hasattr(result, 'Handle'):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制楔体，中心{center}，长{length}宽{width}高{height}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_flange(
        self,
        center,
        specification=None,
        dn=None,
        pn=None,
        outer_diameter=None,
        center_hole_diameter=None,
        bolt_circle_diameter=None,
        bolt_hole_diameter=None,
        bolt_count=None,
        thickness=None,
        layer=None,
        color=None,
    ):
        """绘制法兰盘（3D）。支持国标 GB/T 9119（指定 specification、dn、pn）或自定义尺寸（单位 mm）。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_flange(
            center,
            specification=specification,
            dn=dn,
            pn=pn,
            outer_diameter=outer_diameter,
            center_hole_diameter=center_hole_diameter,
            bolt_circle_diameter=bolt_circle_diameter,
            bolt_hole_diameter=bolt_hole_diameter,
            bolt_count=bolt_count,
            thickness=thickness,
            layer=current_layer,
            color=color,
        )
        if result:
            entity_info = {
                "type": "flange",
                "center": center,
                "specification": specification,
                "dn": dn,
                "pn": pn,
                "outer_diameter": outer_diameter,
                "center_hole_diameter": center_hole_diameter,
                "bolt_circle_diameter": bolt_circle_diameter,
                "bolt_hole_diameter": bolt_hole_diameter,
                "bolt_count": bolt_count,
                "thickness": thickness,
                "layer": current_layer,
                "color": color,
            }
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = (
                f"绘制法兰，中心{center}"
                + (f" DN{dn} PN{pn}" if dn is not None and pn is not None else " 自定义尺寸")
            )
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_blind_flange(self, center, specification=None, dn=None, pn=None, outer_diameter=None,
                          bolt_circle_diameter=None, bolt_hole_diameter=None, bolt_count=None,
                          thickness=None, layer=None, color=None):
        """绘制盲板（法兰盖，国标 GB/T 9123 等，无中心孔）。须指定国标 dn/pn 或自定义尺寸。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_blind_flange(
            center, specification=specification, dn=dn, pn=pn,
            outer_diameter=outer_diameter, bolt_circle_diameter=bolt_circle_diameter,
            bolt_hole_diameter=bolt_hole_diameter, bolt_count=bolt_count, thickness=thickness,
            layer=current_layer, color=color,
        )
        if result:
            entity_info = {"type": "blind_flange", "center": center, "specification": specification,
                           "dn": dn, "pn": pn, "outer_diameter": outer_diameter,
                           "bolt_circle_diameter": bolt_circle_diameter, "bolt_hole_diameter": bolt_hole_diameter,
                           "bolt_count": bolt_count, "thickness": thickness,
                           "layer": current_layer, "color": color}
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制盲板，中心{center}" + (f" DN{dn} PN{pn}" if dn and pn else " 自定义尺寸")
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_manhole(
        self,
        center,
        specification=None,
        dn=None,
        pn=None,
        opening_diameter=None,
        neck_outer_diameter=None,
        neck_wall_thickness=10.0,
        neck_height=120.0,
        flange_outer_diameter=None,
        bolt_circle_diameter=None,
        bolt_hole_diameter=None,
        bolt_count=None,
        flange_thickness=None,
        cover_outer_diameter=None,
        cover_thickness=20.0,
        cover_gap=6.0,
        handle_count=2,
        handle_width=180.0,
        handle_height=60.0,
        handle_bar_diameter=16.0,
        add_lifting_lug=False,
        lug_width=56.0,
        lug_thickness=20.0,
        lug_height=110.0,
        lug_hole_diameter=28.0,
        layer=None,
        color=None,
    ):
        """绘制低矮圆形人孔总成。默认关闭中间吊耳，只保留圆滑把手。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_manhole(
            center,
            specification=specification,
            dn=dn,
            pn=pn,
            opening_diameter=opening_diameter,
            neck_outer_diameter=neck_outer_diameter,
            neck_wall_thickness=neck_wall_thickness,
            neck_height=neck_height,
            flange_outer_diameter=flange_outer_diameter,
            bolt_circle_diameter=bolt_circle_diameter,
            bolt_hole_diameter=bolt_hole_diameter,
            bolt_count=bolt_count,
            flange_thickness=flange_thickness,
            cover_outer_diameter=cover_outer_diameter,
            cover_thickness=cover_thickness,
            cover_gap=cover_gap,
            handle_count=handle_count,
            handle_width=handle_width,
            handle_height=handle_height,
            handle_bar_diameter=handle_bar_diameter,
            add_lifting_lug=add_lifting_lug,
            lug_width=lug_width,
            lug_thickness=lug_thickness,
            lug_height=lug_height,
            lug_hole_diameter=lug_hole_diameter,
            layer=current_layer,
            color=color,
        )
        if result:
            entity_info = {
                "type": "manhole",
                "center": center,
                "specification": specification,
                "dn": dn,
                "pn": pn,
                "opening_diameter": opening_diameter,
                "neck_outer_diameter": neck_outer_diameter,
                "neck_wall_thickness": neck_wall_thickness,
                "neck_height": neck_height,
                "flange_outer_diameter": flange_outer_diameter,
                "bolt_circle_diameter": bolt_circle_diameter,
                "bolt_hole_diameter": bolt_hole_diameter,
                "bolt_count": bolt_count,
                "flange_thickness": flange_thickness,
                "cover_outer_diameter": cover_outer_diameter,
                "cover_thickness": cover_thickness,
                "cover_gap": cover_gap,
                "handle_count": handle_count,
                "layer": current_layer,
                "color": color,
            }
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制人孔，中心{center}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_bolt(self, base_center, shank_diameter=None, length=None, axis="z", head_width=None, head_height=None,
                  specification=None, thread_size=None, layer=None, color=None):
        """绘制六角头螺栓/螺丝（3D）。支持国标 GB/T 5782/5783 查表或自定义尺寸。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_bolt(
            base_center,
            shank_diameter,
            length,
            axis=axis,
            head_width=head_width,
            head_height=head_height,
            specification=specification,
            thread_size=thread_size,
            layer=current_layer,
            color=color,
        )
        if result:
            entity_info = {
                "type": "bolt",
                "base_center": base_center,
                "specification": specification,
                "thread_size": thread_size,
                "shank_diameter": shank_diameter,
                "length": length,
                "axis": axis,
                "head_width": head_width,
                "head_height": head_height,
                "layer": current_layer,
                "color": color,
            }
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            if specification and thread_size:
                self.drawing_state["last_command"] = f"按国标绘制螺栓 {specification} {thread_size}，基点{base_center}，L={length}"
            else:
                self.drawing_state["last_command"] = f"绘制螺栓，基点{base_center}，d={shank_diameter}，L={length}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_hex_nut(self, center, inner_diameter=None, thickness=None, width_across_flats=None, axis="z",
                     specification=None, thread_size=None, layer=None, color=None):
        """绘制六角螺母（3D）。支持国标 GB/T 6170.1 查表或自定义尺寸。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_hex_nut(
            center,
            inner_diameter,
            thickness=thickness,
            width_across_flats=width_across_flats,
            axis=axis,
            specification=specification,
            thread_size=thread_size,
            layer=current_layer,
            color=color,
        )
        if result:
            entity_info = {
                "type": "hex_nut",
                "center": center,
                "specification": specification,
                "thread_size": thread_size,
                "inner_diameter": inner_diameter,
                "thickness": thickness,
                "width_across_flats": width_across_flats,
                "axis": axis,
                "layer": current_layer,
                "color": color,
            }
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            if specification and thread_size:
                self.drawing_state["last_command"] = f"按国标绘制螺母 {specification} {thread_size}，中心{center}"
            else:
                self.drawing_state["last_command"] = f"绘制螺母，中心{center}，d={inner_diameter}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_washer(self, center, inner_diameter=None, outer_diameter=None, thickness=None, axis="z",
                    specification=None, thread_size=None, layer=None, color=None):
        """绘制平垫圈（3D）。支持国标 GB/T 97.1 查表或自定义尺寸。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_washer(
            center,
            inner_diameter,
            outer_diameter=outer_diameter,
            thickness=thickness,
            axis=axis,
            specification=specification,
            thread_size=thread_size,
            layer=current_layer,
            color=color,
        )
        if result:
            entity_info = {
                "type": "washer",
                "center": center,
                "specification": specification,
                "thread_size": thread_size,
                "inner_diameter": inner_diameter,
                "outer_diameter": outer_diameter,
                "thickness": thickness,
                "axis": axis,
                "layer": current_layer,
                "color": color,
            }
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            if specification and thread_size:
                self.drawing_state["last_command"] = f"按国标绘制垫圈 {specification} {thread_size}，中心{center}"
            else:
                self.drawing_state["last_command"] = f"绘制垫圈，中心{center}，d={inner_diameter}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_ball_valve(self, center, dn, pn, axis="x", layer=None, color=None, connection_type="flanged"):
        """绘制球阀（3D）。支持法兰式与紧凑式螺纹/焊接端。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_ball_valve(
            center,
            dn,
            pn,
            axis=axis,
            layer=current_layer,
            color=color,
            connection_type=connection_type,
        )
        if result:
            entity_info = {
                "type": "ball_valve",
                "center": center,
                "dn": dn,
                "pn": pn,
                "axis": axis,
                "connection_type": connection_type,
                "layer": current_layer,
                "color": color,
            }
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制球阀 DN{dn} PN{pn}，中心{center}，连接型式 {connection_type}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_butterfly_valve(self, center, dn, pn, axis="y", layer=None, color=None):
        """绘制蝶阀（3D）。国标 GB/T 12238，默认按工程三视图友好的姿态生成：流道沿 Y，阀杆沿 Z。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_butterfly_valve(center, dn, pn, axis=axis, layer=current_layer, color=color)
        if result:
            entity_info = {"type": "butterfly_valve", "center": center, "dn": dn, "pn": pn, "layer": current_layer, "color": color}
            try:
                entity_info["handle"] = str(result.Handle)
            except Exception:
                pass
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制蝶阀 DN{dn} PN{pn}，中心{center}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_globe_valve(self, center, dn, pn, axis="x", layer=None, color=None):
        """绘制截止阀（3D）。国标 GB/T 12235，默认流道沿 X、阀杆沿 Z。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_globe_valve(center, dn, pn, axis=axis, layer=current_layer, color=color)
        if result:
            entity_info = {"type": "globe_valve", "center": center, "dn": dn, "pn": pn, "layer": current_layer, "color": color}
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制截止阀 DN{dn} PN{pn}，中心{center}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_check_valve(self, center, dn, pn=16, axis="x", layer=None, color=None, valve_type="swing"):
        """绘制止回阀（3D）。国标 GB/T 12236，当前默认旋启式，默认流道沿 X。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        effective_color = 7 if color is None else color
        result = self.controller.draw_check_valve(
            center,
            dn,
            pn,
            axis=axis,
            layer=current_layer,
            color=effective_color,
            valve_type=valve_type,
        )
        if result:
            entity_info = {
                "type": "check_valve",
                "center": center,
                "dn": dn,
                "pn": pn,
                "axis": axis,
                "valve_type": valve_type,
                "layer": current_layer,
                "color": effective_color,
            }
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制止回阀 DN{dn} PN{pn}，中心{center}，型式 {valve_type}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_pressure_gauge(
        self,
        center,
        nominal_diameter=100,
        style="radial",
        mount_type="direct",
        face_axis="x",
        thread_size=None,
        layer=None,
        color=None,
    ):
        """绘制一般压力表（3D）。参考 GB/T 1226，默认输出白色实体。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        effective_color = 7 if color is None else color
        result = self.controller.draw_pressure_gauge(
            center,
            nominal_diameter=nominal_diameter,
            style=style,
            mount_type=mount_type,
            face_axis=face_axis,
            thread_size=thread_size,
            layer=current_layer,
            color=effective_color,
        )
        if result:
            entity_info = {
                "type": "pressure_gauge",
                "center": center,
                "nominal_diameter": nominal_diameter,
                "style": style,
                "mount_type": mount_type,
                "face_axis": face_axis,
                "thread_size": thread_size,
                "layer": current_layer,
                "color": effective_color,
            }
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = (
                f"绘制压力表 Y-{nominal_diameter}，中心{center}，型式 {style}，安装 {mount_type}，法向 {face_axis}"
            )
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_rigid_waterproof_sleeve(
        self,
        center,
        dn,
        axis="x",
        sleeve_type="A",
        length=None,
        layer=None,
        color=None,
    ):
        """绘制刚性防水套管（3D）。参考 02S404，当前支持 A 型常用 DN50-DN450。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        effective_color = 7 if color is None else color
        result = self.controller.draw_rigid_waterproof_sleeve(
            center,
            dn,
            axis=axis,
            sleeve_type=sleeve_type,
            length=length,
            layer=current_layer,
            color=effective_color,
        )
        if result:
            entity_info = {
                "type": "rigid_waterproof_sleeve",
                "center": center,
                "dn": dn,
                "axis": axis,
                "sleeve_type": sleeve_type,
                "length": length,
                "layer": current_layer,
                "color": effective_color,
            }
            try:
                handle = getattr(result, "Handle", None)
            except Exception:
                handle = None
            if handle is not None:
                entity_info["handle"] = str(handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制刚性防水套管 DN{dn}，中心{center}，型式 {sleeve_type}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_steel_bellmouth(
        self,
        center,
        dn,
        axis="x",
        flare_length=None,
        throat_length=None,
        mouth_outer_diameter=None,
        wall_thickness=None,
        layer=None,
        color=None,
    ):
        """绘制钢制喇叭口（3D）。参考 02S403 吸水喇叭口系列。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        effective_color = 7 if color is None else color
        result = self.controller.draw_steel_bellmouth(
            center,
            dn,
            axis=axis,
            flare_length=flare_length,
            throat_length=throat_length,
            mouth_outer_diameter=mouth_outer_diameter,
            wall_thickness=wall_thickness,
            layer=current_layer,
            color=effective_color,
        )
        if result:
            entity_info = {
                "type": "steel_bellmouth",
                "center": center,
                "dn": dn,
                "axis": axis,
                "flare_length": flare_length,
                "throat_length": throat_length,
                "mouth_outer_diameter": mouth_outer_diameter,
                "wall_thickness": wall_thickness,
                "layer": current_layer,
                "color": effective_color,
            }
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制钢制喇叭口 DN{dn}，连接端中心{center}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_double_flange_limit_expansion_joint(
        self,
        center,
        dn,
        model="JSSJA-2",
        pn=1.0,
        axis="x",
        layer=None,
        color=None,
    ):
        """绘制双法兰限位伸缩接头（3D）。参考 GB/T 12465，兼容 JSSJA-2 / VSSJA-2(B2F) 命名。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        effective_color = 7 if color is None else color
        result = self.controller.draw_double_flange_limit_expansion_joint(
            center=center,
            dn=dn,
            model=model,
            pn=pn,
            axis=axis,
            layer=current_layer,
            color=effective_color,
        )
        if result:
            entity_info = {
                "type": "double_flange_limit_expansion_joint",
                "center": center,
                "dn": dn,
                "model": model,
                "pn": pn,
                "axis": axis,
                "layer": current_layer,
                "color": effective_color,
            }
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制双法兰限位伸缩接头 {model} DN{dn} PN{pn}，中心{center}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_suction_bellmouth_support(
        self,
        center,
        support_model="ZB3",
        bellmouth_form="J",
        support_type="B",
        axis="x",
        layer=None,
        color=None,
    ):
        """绘制吸水喇叭管支架（3D）。参考 02S403，当前优先支持 ZB3/J/B。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        effective_color = 7 if color is None else color
        result = self.controller.draw_suction_bellmouth_support(
            center=center,
            support_model=support_model,
            bellmouth_form=bellmouth_form,
            support_type=support_type,
            axis=axis,
            layer=current_layer,
            color=effective_color,
        )
        if result:
            entity_info = {
                "type": "suction_bellmouth_support",
                "center": center,
                "support_model": support_model,
                "bellmouth_form": bellmouth_form,
                "support_type": support_type,
                "axis": axis,
                "layer": current_layer,
                "color": effective_color,
            }
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = (
                f"绘制吸水喇叭管支架 {support_model} {bellmouth_form}型 {support_type}型，中心{center}"
            )
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_elliptical_head(self, center, dn, axis="z", with_straight=False, layer=None, color=None):
        """绘制椭圆封头（3D）。国标 GB/T 25198，需 dn。"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_elliptical_head(center, dn, axis=axis, with_straight=with_straight, layer=current_layer, color=color)
        if result:
            entity_info = {"type": "elliptical_head", "center": center, "dn": dn, "layer": current_layer, "color": color}
            if hasattr(result, "Handle"):
                entity_info["handle"] = str(result.Handle)
            self.drawing_state["entities"].append(entity_info)
            self.drawing_state["last_command"] = f"绘制椭圆封头 DN{dn}，中心{center}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_spline(self, points, layer=None, color=None, lineweight=None):
        """绘制样条曲线"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_spline(points, current_layer, color, lineweight)
        if result:
            self.drawing_state["entities"].append({
                "type": "spline",
                "points": points,
                "layer": current_layer,
                "color": color,
                "lineweight": lineweight
            })
            self.drawing_state["last_command"] = f"绘制样条曲线，控制点数{len(points)}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def draw_mtext(self, position, width, text, layer=None, color=None):
        """添加多行文字"""
        if not self.controller.is_running():
            self.start_cad()
        current_layer = layer or self.drawing_state["current_layer"]
        result = self.controller.draw_mtext(position, width, text, current_layer, color)
        if result:
            self.drawing_state["entities"].append({
                "type": "mtext",
                "position": position,
                "width": width,
                "text": text,
                "layer": current_layer,
                "color": color
            })
            self.drawing_state["last_command"] = f"添加多行文字，位置{position}，宽度{width}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def zoom_extents(self):
        """缩放到显示所有对象"""
        if not self.controller.is_running():
            return False
        result = self.controller.zoom_extents()
        if result:
            self.drawing_state["last_command"] = "缩放到范围"
            self.drawing_state["last_result"] = "成功"
        return result

    def set_named_view(self, view_name, target=None, center=None, height=400.0, width=600.0, zoom_after_change=True):
        """设置命名视图，可用于正投影视图与等轴测视图。"""
        if not self.controller.is_running():
            self.start_cad()
        result = self.controller.set_named_view(
            view_name,
            target=tuple(target) if target else (0.0, 0.0, 0.0),
            center=tuple(center) if center else (0.0, 0.0),
            height=height,
            width=width,
        )
        if result and zoom_after_change:
            self.controller.zoom_extents()
        if result:
            self.drawing_state["last_command"] = f"设置命名视图: {view_name}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def set_orthographic_view(self, view_name, target=None, center=None, height=400.0, width=600.0):
        """设置常用正投影视图。"""
        if not self.controller.is_running():
            self.start_cad()
        result = self.controller.set_orthographic_view(
            view_name,
            target=tuple(target) if target else (0.0, 0.0, 0.0),
            center=tuple(center) if center else (0.0, 0.0),
            height=height,
            width=width,
        )
        if result:
            self.drawing_state["last_command"] = f"设置正投影视图: {view_name}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def boolean_union(self, handle_a: str, handle_b: str) -> bool:
        """对两个 3D 实体做布尔并集"""
        if not self.controller.is_running():
            self.start_cad()
        result = self.controller.boolean_union(handle_a, handle_b)
        if result:
            self.drawing_state["last_command"] = f"布尔并集: {handle_a} ∪ {handle_b}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def boolean_intersect(self, handle_a: str, handle_b: str) -> bool:
        """对两个 3D 实体做布尔交集"""
        if not self.controller.is_running():
            self.start_cad()
        result = self.controller.boolean_intersect(handle_a, handle_b)
        if result:
            self.drawing_state["last_command"] = f"布尔交集: {handle_a} ∩ {handle_b}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def boolean_subtract(self, handle_main: str, handle_tool: str) -> bool:
        """对两个 3D 实体做布尔差集（从 main 中减去 tool）"""
        if not self.controller.is_running():
            self.start_cad()
        result = self.controller.boolean_subtract(handle_main, handle_tool)
        if result:
            self.drawing_state["last_command"] = f"布尔差集: {handle_main} - {handle_tool}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def rotate_entity(self, handle: str, base_point, angle: float, axis_point2=None, axis_direction=None) -> bool:
        """旋转 2D/3D 实体。沿某一点：提供 axis_direction；沿某一条线：提供 axis_point2"""
        if not self.controller.is_running():
            self.start_cad()
        result = self.controller.rotate_entity(handle, base_point, angle, axis_point2, axis_direction)
        if result:
            self.drawing_state["last_command"] = f"旋转实体 handle={handle}, 角度={angle}°"
            self.drawing_state["last_result"] = "成功"
        else:
            self.drawing_state["last_result"] = "失败"
        return result

    def extrude_entity(self, handle: str, height: float = None, taper_angle: float = 0, path_handle: str = None, layer=None, color=None):
        """3D 拉伸：将 2D 封闭图形沿 Z 轴或沿路径拉伸为 3D 实体。height 与 path_handle 二选一"""
        if not self.controller.is_running():
            self.start_cad()
        color_rgb = self.nlp_processor.extract_color_from_command(color) if color else None
        if color_rgb is not None:
            color = color_rgb
        result_handle = self.controller.extrude_entity(handle, height, taper_angle, path_handle, layer, color)
        if result_handle:
            self.drawing_state["entities"].append({
                "type": "extruded_solid",
                "source_handle": handle,
                "handle": result_handle,
                "height": height,
                "path_handle": path_handle,
                "taper_angle": taper_angle,
                "layer": layer,
                "color": color
            })
            desc = f"沿路径({path_handle})" if path_handle else f"高度={height}"
            self.drawing_state["last_command"] = f"3D拉伸实体 handle={handle}, {desc}"
            self.drawing_state["last_result"] = "成功"
            return result_handle
        self.drawing_state["last_result"] = "失败"
        return None

    def revolve_entity(self, handle: str, axis_point, axis_direction, angle: float = 360, layer=None, color=None):
        """将 2D 封闭图形绕轴旋转生成 3D 实体，默认旋转 360° 一周"""
        if not self.controller.is_running():
            self.start_cad()
        color_rgb = self.nlp_processor.extract_color_from_command(color) if color else None
        if color_rgb is not None:
            color = color_rgb
        result_handle = self.controller.revolve_entity(handle, axis_point, axis_direction, angle, layer, color)
        if result_handle:
            self.drawing_state["entities"].append({
                "type": "revolved_solid",
                "source_handle": handle,
                "handle": result_handle,
                "axis_point": axis_point,
                "axis_direction": axis_direction,
                "angle": angle,
                "layer": layer,
                "color": color
            })
            self.drawing_state["last_command"] = f"旋转生成实体 handle={handle}, 角度={angle}°"
            self.drawing_state["last_result"] = "成功"
            return result_handle
        self.drawing_state["last_result"] = "失败"
        return None

    def save_drawing(self, file_path):
        """保存图纸"""
        if not self.controller.is_running():
            self.last_saved_path = None
            return False

        resolved_path = self._resolve_dwg_output_path(file_path)
        result = self.controller.save_drawing(resolved_path)
        if result:
            self.last_saved_path = resolved_path
            self.drawing_state["last_command"] = f"保存图纸到{resolved_path}"
            self.drawing_state["last_result"] = "成功"
        else:
            self.last_saved_path = None
            self.drawing_state["last_result"] = "失败"

        return result
    
    def process_command(self, command: str) -> Dict[str, Any]:
        """处理自然语言命令"""
        if not self.controller.is_running():
            self.start_cad()
        # 使用NLP处理器解析命令
        parsed_command = self.nlp_processor.process_command(command)
        command_type = parsed_command.get("type")
        non_drawing_commands = {"save", "zoom_extents", "set_view", "create_layer", "change_layer", "unknown"}
        should_attach_pre_draw = command_type not in non_drawing_commands
        if should_attach_pre_draw:
            self._update_current_drawing_subject(command)

        def finalize(payload: Dict[str, Any]) -> Dict[str, Any]:
            if not should_attach_pre_draw:
                return payload
            return self._attach_pre_draw_brief(
                payload,
                subject=command,
                trigger=f"process_command:{command_type}",
            )

        try:
            # 基本绘图命令处理
            if command_type == "draw_line":
                start_point = parsed_command.get("start_point")
                end_point = parsed_command.get("end_point")
                # 获取颜色参数
                color = parsed_command.get("color")
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                # 获取线宽参数
                lineweight = parsed_command.get("lineweight")
                result = self.draw_line(start_point, end_point, None, color, lineweight)
                return finalize({
                    "success": result is not None,
                    "message": "直线已绘制" if result else "绘制直线失败",
                    "entity_id": result.Handle if result else None
                })

            elif command_type == "draw_circle":
                center = parsed_command.get("center")
                radius = parsed_command.get("radius")
                # 获取颜色参数
                color = parsed_command.get("color")
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                # 获取线宽参数
                lineweight = parsed_command.get("lineweight")
                result = self.draw_circle(center, radius, None, color, lineweight)
                return finalize({
                    "success": result is not None,
                    "message": "圆已绘制" if result else "绘制圆失败",
                    "entity_id": result.Handle if result else None
                })

            elif command_type == "draw_arc":
                center = parsed_command.get("center")
                radius = parsed_command.get("radius")
                start_angle = parsed_command.get("start_angle")
                end_angle = parsed_command.get("end_angle")
                # 确保所有必要参数都存在且有效
                if center is None or radius is None or start_angle is None or end_angle is None:
                    return finalize({
                        "success": False,
                        "message": "绘制圆弧失败：缺少必要参数",
                        "error": "缺少必要参数：中心点、半径、起始角度或结束角度"
                    })
                # 获取颜色参数
                color = parsed_command.get("color")
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                # 获取线宽参数
                lineweight = parsed_command.get("lineweight")
                result = self.draw_arc(center, radius, start_angle, end_angle, None, color, lineweight)

                return finalize({
                    "success": result is not None,
                    "message": "圆弧已绘制" if result else "绘制圆弧失败",
                    "entity_id": result.Handle if result else None
                })
                
            elif command_type == "draw_ellipse":
                center = parsed_command.get("center")
                major_axis = parsed_command.get("major_axis")
                minor_axis = parsed_command.get("minor_axis")
                rotation = parsed_command.get("rotation", 0)  # 默认旋转角度为0
                # 确保所有必要参数都存在且有效
                if center is None or major_axis is None or minor_axis is None:
                    return finalize({
                        "success": False,
                        "message": "绘制椭圆失败：缺少必要参数",
                        "error": "缺少必要参数：中心点、长轴或短轴"
                    })
                # 获取颜色参数
                color = parsed_command.get("color")
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                # 获取线宽参数
                lineweight = parsed_command.get("lineweight")
                result = self.draw_ellipse(center, major_axis, minor_axis, rotation, None, color, lineweight)

                return finalize({
                    "success": result is not None,
                    "message": "椭圆已绘制" if result else "绘制椭圆失败",
                    "entity_id": result.Handle if result else None
                })

            elif command_type == "draw_rectangle":
                corner1 = parsed_command.get("corner1")
                corner2 = parsed_command.get("corner2")
                # 获取颜色参数
                color = parsed_command.get("color")
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                # 获取线宽参数
                lineweight = parsed_command.get("lineweight")
                result = self.draw_rectangle(corner1, corner2, None, color, lineweight)

                return finalize({
                    "success": result is not None,
                    "message": "矩形已绘制" if result else "绘制矩形失败",
                    "entity_id": result.Handle if result else None
                })

            elif command_type == "draw_text":
                position = parsed_command.get("position")
                text = parsed_command.get("text")
                height = parsed_command.get("height", 2.5)
                rotation = parsed_command.get("rotation", 0)
                # 获取颜色参数
                color = parsed_command.get("color")
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                result = self.draw_text(position, text, height, rotation, None, color)

                return finalize({
                    "success": result is not None,
                    "message": "文本已添加" if result else "添加文本失败",
                    "entity_id": result.Handle if result else None
                })

            elif command_type == "draw_hatch":
                points = parsed_command.get("points")
                pattern_name = parsed_command.get("pattern_name", "SOLID")
                scale = parsed_command.get("scale", 1.0)
                # 确保所有必要参数都存在且有效
                if points is None or len(points) < 3:
                    return finalize({
                        "success": False,
                        "message": "绘制填充失败：缺少必要参数或点数不足",
                        "error": "填充边界至少需要3个点"
                    })
                # 获取颜色参数，可能是索引或RGB值
                color = parsed_command.get("color")  # 获取颜色参数
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = self.nlp_processor.extract_color_from_command(command)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                result = self.draw_hatch(points, pattern_name, scale, None, color)

                return finalize({
                    "success": result is not None,
                    "message": "填充已绘制" if result else "绘制填充失败",
                    "entity_id": result.Handle if result else None
                })

            # 处理标注
            elif command_type == "add_dimension":
                dimension_type = parsed_command.get("dimension_type", "aligned")
                start_point = parsed_command.get("start_point")
                end_point = parsed_command.get("end_point")
                text_position = parsed_command.get("text_position")
                textheight = parsed_command.get("textheight", 5)
                center = parsed_command.get("center")
                chord_point = parsed_command.get("chord_point")
                far_chord_point = parsed_command.get("far_chord_point")
                leader_length = parsed_command.get("leader_length")
                color = parsed_command.get("color")
                result = self.add_dimension(
                    start_point=start_point,
                    end_point=end_point,
                    text_position=text_position,
                    textheight=textheight,
                    color=color,
                    dimension_type=dimension_type,
                    center=center,
                    chord_point=chord_point,
                    far_chord_point=far_chord_point,
                    leader_length=leader_length,
                )
                return finalize({
                    "success": result is not None,
                    "message": f"{dimension_type}标注已添加" if result else "添加标注失败",
                    "entity_id": result.Handle if result else None
                })

            elif command_type == "save":
                file_path = parsed_command.get("file_path")
                result = self.save_drawing(file_path)
                final_file_path = self.last_saved_path or self._resolve_dwg_output_path(file_path)

                return {
                    "success": result,
                    "message": f"图纸已保存到 {final_file_path}" if result else f"保存图纸到 {final_file_path} 失败"
                }

            elif command_type == "zoom_extents":
                result = self.zoom_extents()
                return {
                    "success": result,
                    "message": "已缩放到全部对象" if result else "缩放到全部对象失败"
                }

            elif command_type == "set_view":
                view_name = parsed_command.get("view_name")
                zoom_after_change = parsed_command.get("zoom_extents", True)
                result = self.set_named_view(view_name, zoom_after_change=zoom_after_change)
                return {
                    "success": result,
                    "message": f"已切换到视图 {view_name}" if result else f"切换到视图 {view_name} 失败"
                }


            # 处理图层操作
            elif command_type == "create_layer":
                layer_name = parsed_command.get("layer_name")
                color = parsed_command.get("color", 7)  # 默认白色
                result = self.controller.create_layer(layer_name)  # , color
                return {
                    "success": result,
                    "message": f"图层 {layer_name} 已创建" if result else f"创建图层 {layer_name} 失败"
                }

            return finalize({
                "success": False,
                "message": parsed_command.get("error", "无法识别的命令类型"),
                "parsed_command": parsed_command,
            })
            
        except Exception as e:
            return finalize({
                "success": False,
                "message": f"处理命令时出错: {str(e)}"
            })

    def get_latest_vision_feedback(self, include_schema: bool = False) -> Dict[str, Any]:
        """获取最近一次视觉反馈。"""
        self.vision_loop_enabled = self.vision_loop.enabled
        self.vision_state = _build_vision_feedback(
            **self.vision_loop.get_latest_feedback(),
            resource_uri=VISION_LATEST_RESOURCE_URI,
        )
        payload = deepcopy(self.vision_state)
        if include_schema:
            payload["contract"] = _build_vision_contract_metadata()
        return payload

    def analyze_current_view(
        self,
        prompt: str | None = None,
        focus_hint: str | None = None,
        force: bool = False,
        include_schema: bool = False
    ) -> Dict[str, Any]:
        """同步执行一次当前 CAD 视图分析并返回统一 JSON。"""
        job_id = str(uuid.uuid4())
        requested_at = _utc_now_iso()
        job_resource_uri = f"{VISION_JOB_RESOURCE_PREFIX}{job_id}"

        job_feedback = _build_vision_feedback(
            **self.vision_loop.analyze_current_view(
                prompt=prompt,
                focus_hint=focus_hint,
                force=force,
                job_id=job_id,
            ),
            resource_uri=job_resource_uri,
            requested_at=requested_at,
            request_prompt=prompt,
            focus_hint=focus_hint,
            force_refresh=force,
        )
        self.vision_jobs[job_id] = deepcopy(job_feedback)

        latest_feedback = deepcopy(job_feedback)
        latest_feedback["resource_uri"] = VISION_LATEST_RESOURCE_URI
        self.vision_state = _build_vision_feedback(**latest_feedback)

        if include_schema:
            self.vision_state["contract"] = _build_vision_contract_metadata()

        return deepcopy(self.vision_state)

    def set_vision_loop_enabled(
        self,
        enabled: bool,
        reason: str | None = None,
        include_schema: bool = False
    ) -> Dict[str, Any]:
        """启用或停用视觉轮询回路。"""
        self.vision_state = _build_vision_feedback(
            **self.vision_loop.set_enabled(enabled=enabled, reason=reason),
            resource_uri=VISION_LATEST_RESOURCE_URI,
            updated_at=_utc_now_iso(),
            control_reason=reason,
        )
        self.vision_loop_enabled = self.vision_loop.enabled
        if include_schema:
            self.vision_state["contract"] = _build_vision_contract_metadata()
        return deepcopy(self.vision_state)

    def get_vision_job(self, job_id: str, include_schema: bool = False) -> Dict[str, Any]:
        """读取指定视觉任务。"""
        if job_id not in self.vision_jobs:
            raise ValueError(f"未知的视觉任务: {job_id}")
        payload = deepcopy(self.vision_jobs[job_id])
        payload["loop_enabled"] = self.vision_loop_enabled
        if include_schema:
            payload["contract"] = _build_vision_contract_metadata()
        return payload

async def main():
    """主入口函数"""
    logger.info("启动 CAD MCP 服务器")

    # 加载配置
    config = Config()
    cad_service = CADService(vision_config=config.vision_config)
    server = Server(config.server_name)

    # 注册处理程序
    logger.debug("注册处理程序")

    @server.list_resources()
    async def handle_list_resources() -> list[types.Resource]:
        logger.debug("处理 list_resources 请求")
        resources = [
            types.Resource(
                uri=AnyUrl("drawing://current"),
                name="当前CAD图纸",
                description="当前CAD图纸的状态",
                mimeType="application/json",
            ),
            types.Resource(
                uri=AnyUrl(VISION_LATEST_RESOURCE_URI),
                name="最新视觉反馈",
                description="统一视觉反馈 JSON，可用于读取最近一次视觉状态与分析结果",
                mimeType="application/json",
            )
        ]
        for job_id in cad_service.vision_jobs:
            resources.append(
                types.Resource(
                    uri=AnyUrl(f"{VISION_JOB_RESOURCE_PREFIX}{job_id}"),
                    name=f"视觉分析任务 {job_id}",
                    description="主动视觉分析任务的统一 JSON 结果",
                    mimeType="application/json",
                )
            )
        return resources

    @server.read_resource()
    async def handle_read_resource(uri: AnyUrl) -> str:
        logger.debug(f"处理 read_resource 请求，URI: {uri}")
        if uri.scheme == "drawing":
            path = str(uri).replace("drawing://", "")
            if not path or path != "current":
                logger.error(f"未知的资源路径: {path}")
                raise ValueError(f"未知的资源路径: {path}")
            return json.dumps(cad_service.drawing_state, ensure_ascii=False)

        if uri.scheme == "vision":
            path = str(uri).replace("vision://", "")
            if path == "latest":
                return json.dumps(cad_service.get_latest_vision_feedback(), ensure_ascii=False)
            if path.startswith("jobs/"):
                job_id = path.split("/", 1)[1]
                return json.dumps(cad_service.get_vision_job(job_id), ensure_ascii=False)
            logger.error(f"未知的视觉资源路径: {path}")
            raise ValueError(f"未知的视觉资源路径: {path}")

        logger.error(f"不支持的 URI 协议: {uri.scheme}")
        raise ValueError(f"不支持的 URI 协议: {uri.scheme}")


    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        """列出可用工具"""
        return [
            types.Tool(
                name="draw_line",
                description="在CAD中绘制直线",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "start_point": {
                            "type": "array",
                            "description": "起点坐标 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "end_point": {
                            "type": "array",
                            "description": "终点坐标 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"},
                        "lineweight": {"type": "number", "description": "线宽（可选）"}
                    },
                    "required": ["start_point", "end_point"],
                },
            ),

            types.Tool(
                name="draw_circle",
                description="在CAD中绘制圆",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {
                            "type": "array",
                            "description": "圆心坐标 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "radius": {"type": "number", "description": "圆的半径"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"},
                        "lineweight": {"type": "number", "description": "线宽（可选）"}
                    },
                    "required": ["center", "radius"],
                },
            ),

            types.Tool(
                name="draw_arc",
                description="在CAD中绘制弧",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {
                            "type": "array",
                            "description": "圆心坐标 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "radius": {"type": "number", "description": "弧的半径"},
                        "start_angle": {"type": "number", "description": "起始角度（度）"},
                        "end_angle": {"type": "number", "description": "结束角度（度）"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"},
                        "lineweight": {"type": "number", "description": "线宽（可选）"}
                    },
                    "required": ["center", "radius", "start_angle", "end_angle"],
                },
            ),

            types.Tool(
                name="draw_ellipse",
                description="在CAD中绘制椭圆",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {
                            "type": "array",
                            "description": "椭圆中心坐标 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "major_axis": {"type": "number", "description": "长轴长度"},
                        "minor_axis": {"type": "number", "description": "短轴长度"},
                        "rotation": {"type": "number", "description": "旋转角度（度）（可选）"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"},
                        "lineweight": {"type": "number", "description": "线宽（可选）"}
                    },
                    "required": ["center", "major_axis", "minor_axis"],
                },
            ),

            types.Tool(
                name="draw_polyline",
                description="在CAD中绘制多段线",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "points": {
                            "type": "array",
                            "description": "点集 [[x1, y1, z1], [x2, y2, z2], ...]",
                            "items": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2,
                                "maxItems": 3
                            },
                            "minItems": 2
                        },
                        "closed": {"type": "boolean", "description": "是否闭合"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"},
                        "lineweight": {"type": "number", "description": "线宽（可选）"}
                    },
                    "required": ["points"],
                },
            ),

            types.Tool(
                name="draw_rectangle",
                description="在CAD中绘制矩形",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "corner1": {
                            "type": "array",
                            "description": "第一个角点坐标 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "corner2": {
                            "type": "array",
                            "description": "第二个角点坐标 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"},
                        "lineweight": {"type": "number", "description": "线宽（可选）"}
                    },
                    "required": ["corner1", "corner2"],
                },
            ),

            types.Tool(
                name="draw_sphere",
                description="在CAD中绘制球体（3D实体）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "球心坐标 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "radius": {"type": "number", "description": "球体半径"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "radius"],
                },
            ),
            types.Tool(
                name="draw_box",
                description="在CAD中绘制长方体/正方体（3D实体）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "中心坐标 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "length": {"type": "number", "description": "长度"},
                        "width": {"type": "number", "description": "宽度"},
                        "height": {"type": "number", "description": "高度"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "length", "width", "height"],
                },
            ),
            types.Tool(
                name="draw_cylinder",
                description="在CAD中绘制圆柱体（3D实体，沿Z轴）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "底面圆心坐标 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "radius": {"type": "number", "description": "底面半径"},
                        "height": {"type": "number", "description": "高度"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "radius", "height"],
                },
            ),
            types.Tool(
                name="draw_cylinder_along_y",
                description="在CAD中绘制沿Y轴方向的圆柱体（3D实体）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "base_center": {"type": "array", "description": "底面圆心坐标 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "radius": {"type": "number", "description": "底面半径"},
                        "height_y": {"type": "number", "description": "沿Y轴高度"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["base_center", "radius", "height_y"],
                },
            ),
            types.Tool(
                name="draw_cone",
                description="在CAD中绘制圆锥体（3D实体）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "底面圆心坐标 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "base_radius": {"type": "number", "description": "底面半径"},
                        "height": {"type": "number", "description": "高度"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "base_radius", "height"],
                },
            ),
            types.Tool(
                name="draw_torus",
                description="在CAD中绘制圆环体（3D实体）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "中心坐标 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "major_radius": {"type": "number", "description": "圆环主半径"},
                        "minor_radius": {"type": "number", "description": "管道半径"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "major_radius", "minor_radius"],
                },
            ),
            types.Tool(
                name="draw_pipe_elbow",
                description="在CAD中绘制管道弯头（国标GB/T 12459-2017）。支持45°/90°/180°；LR/SR/3D。公称直径与壁厚联动：指定 dn 时自动取对应壁厚（如 DN15→2.0mm，DN50→3.5mm）绘制空心弯头；可显式传 wall_thickness 覆盖。须指定 pipe_radius 或 dn。默认90°、LR。XY平面。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "弯头转角中心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "pipe_radius": {"type": "number", "description": "管道半径（外径之半）；与 dn 二选一"},
                        "dn": {"type": "integer", "description": "公称通径（mm），国标查外径；与 pipe_radius 二选一"},
                        "bend_radius": {"type": "number", "description": "弯曲半径 R（可选）；未指定时按 elbow_type：LR=1.5D，SR=1.0D，3D=3D"},
                        "elbow_type": {"type": "string", "description": "弯头类型：LR、SR、3D，默认 LR"},
                        "angle": {"type": "number", "description": "弯头角度 45/90/180（度），默认 90"},
                        "plane": {"type": "string", "description": "平面，默认 xy"},
                        "wall_thickness": {"type": "number", "description": "壁厚（mm）；不指定时按 dn 自动取国标值（如 DN15→2.0，DN50→3.5）；传0为实心"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center"],
                },
            ),
            types.Tool(
                name="draw_reducer",
                description="在CAD中绘制异径管（同心/偏心，国标GB/T 12459-2017）。大端dn_large、小端dn_small（mm）；可通过 reducer_type 选择 concentric(同心异径管) 或 eccentric(偏心异径管)。中心为大端圆心，默认沿Z轴正方向由大端到小端。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "大端圆心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "dn_large": {"type": "integer", "description": "大端公称通径（mm）"},
                        "dn_small": {"type": "integer", "description": "小端公称通径（mm），须小于 dn_large"},
                        "axis": {"type": "string", "description": "异径管轴向，默认 z"},
                        "reducer_type": {"type": "string", "description": "异径管类型：concentric(同心) 或 eccentric(偏心)，默认 concentric"},
                        "length": {"type": "number", "description": "结构长度（mm），可选"},
                        "wall_thickness": {"type": "number", "description": "壁厚（mm），可选；不指定时按大端DN取"},
                        "eccentric_direction": {"type": "string", "description": "偏心方向 x/y/z 或 -x/-y/-z；仅偏心异径管时生效"},
                        "eccentric_offset": {"type": "number", "description": "偏心量（mm）；仅偏心异径管时生效，不指定时默认按单边齐平自动计算"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "dn_large", "dn_small"],
                },
            ),
            types.Tool(
                name="draw_tee",
                description="在CAD中绘制三通（3D，国标GB/T 12459-2017）。须指定中心与主管 dn；当 branch_dn 为空或等于 dn 时绘制等径三通，当 branch_dn 小于 dn 时绘制异径三通。默认主管沿X（水平）、支管沿+Z（在 AutoCAD 前视图中向上），支管位于主管正中心；未显式指定壁厚时，默认按国标常用 STD 系列取值并生成空心流道。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "三通交点 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "dn": {"type": "integer", "description": "主管公称通径（mm）"},
                        "branch_dn": {"type": "integer", "description": "支管公称通径（mm）；不填或等于 dn 时为等径三通"},
                        "run_axis": {"type": "string", "description": "主管轴向 x/y/z，默认 x（水平）"},
                        "branch_axis": {"type": "string", "description": "支管轴向 x/y/z 或 -x/-y/-z，默认 z（在 AutoCAD 前视图中向上）"},
                        "wall_thickness": {"type": "number", "description": "主管壁厚（mm），可选；不填时按国标常用 STD 系列自动取值，传0时保持实心"},
                        "branch_wall_thickness": {"type": "number", "description": "支管壁厚（mm），可选；不填时按支管 DN 的国标常用 STD 系列自动取值，传0时保持实心"},
                        "run_center_to_end": {"type": "number", "description": "主管方向 C（mm），可选；用于非内置国标组合或人工修正"},
                        "branch_center_to_end": {"type": "number", "description": "支管方向 M（mm），可选；用于非内置国标组合或人工修正"},
                        "reinforce": {"type": "boolean", "description": "是否启用交界外补强（加强圈/厚度过渡），默认 false"},
                        "reinforce_extra": {"type": "number", "description": "补强额外外半径（mm），可选；不填则按DN/壁厚估算"},
                        "reinforce_run_ratio": {"type": "number", "description": "补强在主管方向长度占2C的比例(0~1)，默认0.55"},
                        "reinforce_branch_ratio": {"type": "number", "description": "补强在支管方向长度占M的比例(0~1)，默认0.70"},
                        "transition_ratio": {"type": "number", "description": "过渡锥台长度占OD的比例(0~0.5)，默认0.0；异径三通默认关闭该外过渡以避免主管两侧额外凸起"},
                        "junction_bulge": {"type": "boolean", "description": "是否在交点叠加外鼓用于圆滑过渡观感，默认 false"},
                        "junction_bulge_extra": {"type": "number", "description": "外鼓额外外半径（mm），可选；不填则按壁厚估算"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "dn"],
                },
            ),
            types.Tool(
                name="draw_wedge",
                description="在CAD中绘制楔体（3D实体）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "底面角点坐标 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "length": {"type": "number", "description": "长度"},
                        "width": {"type": "number", "description": "宽度"},
                        "height": {"type": "number", "description": "高度"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "length", "width", "height"],
                },
            ),
            types.Tool(
                name="draw_flange",
                description="在CAD中绘制法兰盘（3D）。国标GB/T 9119-2010板式平焊法兰：指定specification为GB/T 9119、dn(公称通径mm)、pn(公称压力MPa)从内置表查尺寸。或自定义：outer_diameter、center_hole_diameter、bolt_circle_diameter、bolt_hole_diameter、bolt_count、thickness（单位mm）。法兰底面圆心为center，沿Z轴正方向为厚度。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "法兰底面圆心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "specification": {"type": "string", "description": "标准代号，如 GB/T 9119；不填则使用下面自定义尺寸"},
                        "dn": {"type": "integer", "description": "公称通径（mm），与 specification 配合使用"},
                        "pn": {"type": "number", "description": "公称压力（MPa），如 2.5、6、10、16；与 specification 配合使用"},
                        "outer_diameter": {"type": "number", "description": "法兰外径（mm），自定义时必填"},
                        "center_hole_diameter": {"type": "number", "description": "中心孔直径（mm），自定义时必填"},
                        "bolt_circle_diameter": {"type": "number", "description": "螺栓孔中心圆直径（mm），自定义时必填"},
                        "bolt_hole_diameter": {"type": "number", "description": "螺栓孔直径（mm），自定义时必填"},
                        "bolt_count": {"type": "integer", "description": "螺栓孔数量，自定义时必填"},
                        "thickness": {"type": "number", "description": "法兰厚度（mm），自定义时必填"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center"],
                },
            ),
            types.Tool(
                name="draw_blind_flange",
                description="在CAD中绘制盲板/法兰盖（3D，国标GB/T 9123等）。与同规格法兰外径、螺栓孔一致，无中心孔，用于封堵管口。指定specification为GB/T 9119、dn、pn从表查尺寸；或自定义outer_diameter、bolt_circle_diameter、bolt_hole_diameter、bolt_count、thickness。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "盲板底面圆心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "specification": {"type": "string", "description": "标准代号，如 GB/T 9119"},
                        "dn": {"type": "integer", "description": "公称通径（mm），与 specification 配合使用"},
                        "pn": {"type": "number", "description": "公称压力（MPa），与 specification 配合使用"},
                        "outer_diameter": {"type": "number", "description": "外径（mm），自定义时必填"},
                        "bolt_circle_diameter": {"type": "number", "description": "螺栓孔中心圆直径（mm），自定义时必填"},
                        "bolt_hole_diameter": {"type": "number", "description": "螺栓孔直径（mm），自定义时必填"},
                        "bolt_count": {"type": "integer", "description": "螺栓孔数量，自定义时必填"},
                        "thickness": {"type": "number", "description": "厚度（mm），自定义时必填"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center"],
                },
            ),
            types.Tool(
                name="draw_bolt",
                description="在CAD中绘制六角头螺栓/螺丝（3D，简化表达，不生成真实螺纹）。支持国标模式：specification=GB/T 5782 或 GB/T 5783，thread_size=M6/M8/M10/M12/M16/M20/M24，length 为杆部长；也支持自定义尺寸模式。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "base_center": {"type": "array", "description": "杆部起始端面圆心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "specification": {"type": "string", "description": "国标代号，如 GB/T 5782、GB/T 5783；不填则使用自定义尺寸"},
                        "thread_size": {"type": "string", "description": "螺纹规格，如 M16；与 specification 配合使用"},
                        "shank_diameter": {"type": "number", "description": "杆径（mm），自定义时使用"},
                        "length": {"type": "number", "description": "杆部长度（mm，不含头高）；国标和自定义模式都需要"},
                        "axis": {"type": "string", "description": "杆部轴向 x/y/z 或 -x/-y/-z，默认 z"},
                        "head_width": {"type": "number", "description": "六角头对边宽（mm），可选；国标模式不填则查表"},
                        "head_height": {"type": "number", "description": "六角头高度（mm），可选；国标模式不填则查表"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["base_center", "length"],
                },
            ),
            types.Tool(
                name="draw_hex_nut",
                description="在CAD中绘制六角螺母（3D，简化表达，不生成真实螺纹）。支持国标模式：specification=GB/T 6170.1（或 GB/T 6170），thread_size=M6/M8/M10/M12/M16/M20/M24；也支持自定义尺寸模式。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "螺母中心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "specification": {"type": "string", "description": "国标代号，如 GB/T 6170.1；不填则使用自定义尺寸"},
                        "thread_size": {"type": "string", "description": "螺纹规格，如 M16；与 specification 配合使用"},
                        "inner_diameter": {"type": "number", "description": "通孔直径（mm），自定义时使用"},
                        "thickness": {"type": "number", "description": "厚度（mm），可选；国标模式不填则查表"},
                        "width_across_flats": {"type": "number", "description": "六角对边宽（mm），可选；国标模式不填则查表"},
                        "axis": {"type": "string", "description": "螺母轴向 x/y/z 或 -x/-y/-z，默认 z"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center"],
                },
            ),
            types.Tool(
                name="draw_washer",
                description="在CAD中绘制平垫圈（3D）。支持国标模式：specification=GB/T 97.1，thread_size=M6/M8/M10/M12/M16/M20/M24；也支持自定义尺寸模式。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "垫圈中心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "specification": {"type": "string", "description": "国标代号，如 GB/T 97.1；不填则使用自定义尺寸"},
                        "thread_size": {"type": "string", "description": "螺纹规格，如 M16；与 specification 配合使用"},
                        "inner_diameter": {"type": "number", "description": "内孔直径（mm），自定义时使用"},
                        "outer_diameter": {"type": "number", "description": "外径（mm），可选；国标模式不填则查表"},
                        "thickness": {"type": "number", "description": "厚度（mm），可选；国标模式不填则查表"},
                        "axis": {"type": "string", "description": "垫圈轴向 x/y/z 或 -x/-y/-z，默认 z"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center"],
                },
            ),
            types.Tool(
                name="draw_ball_valve",
                description="在CAD中绘制球阀（3D）。国标GB/T 12237钢制球阀、GB/T 12221结构长度。默认 connection_type=flanged 生成法兰球阀；也支持 threaded、socket_welding、butt_welding 生成更接近三视图样式的紧凑式球阀。默认流道沿X、阀杆沿Z。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "阀门中心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "dn": {"type": "integer", "description": "公称通径（mm）"},
                        "pn": {"type": "number", "description": "公称压力（MPa），如 2.5、6、10、16"},
                        "axis": {"type": "string", "description": "流道轴向，默认 x"},
                        "connection_type": {"type": "string", "description": "连接型式：flanged、threaded、socket_welding、butt_welding；默认 flanged"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "dn", "pn"],
                },
            ),
            types.Tool(
                name="draw_butterfly_valve",
                description="在CAD中绘制蝶阀（3D）。国标GB/T 12238法兰和对夹连接蝶阀、GB/T 12221结构长度。默认按工程三视图友好的姿态生成：流道沿Y、阀杆沿Z，外形更接近手柄式短体蝶阀。中心为阀门中心。需指定dn(公称通径mm)、pn(公称压力MPa)。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "阀门中心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "dn": {"type": "integer", "description": "公称通径（mm）"},
                        "pn": {"type": "number", "description": "公称压力（MPa），如 2.5、6、10、16"},
                        "axis": {"type": "string", "description": "流道轴向，默认 y"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "dn", "pn"],
                },
            ),
            types.Tool(
                name="draw_globe_valve",
                description="在CAD中绘制截止阀（3D）。国标GB/T 12235钢制截止阀和升降式止回阀、GB/T 12221结构长度。默认流道沿X、阀杆沿Z，外形更接近工程图中的法兰截止阀。中心为阀门中心。需指定dn(公称通径mm)、pn(公称压力MPa)。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "阀门中心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "dn": {"type": "integer", "description": "公称通径（mm）"},
                        "pn": {"type": "number", "description": "公称压力（MPa），如 2.5、6、10、16"},
                        "axis": {"type": "string", "description": "流道轴向，默认 x"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "dn", "pn"],
                },
            ),
            types.Tool(
                name="draw_check_valve",
                description="在CAD中绘制止回阀（3D）。国标GB/T 12236钢制旋启式止回阀、GB/T 12221结构长度。当前默认输出法兰旋启式止回阀，默认 pn=16、axis=x、valve_type=swing，未指定颜色时默认白色。中心为阀门中心。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "阀门中心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "dn": {"type": "integer", "description": "公称通径（mm）"},
                        "pn": {"type": "number", "description": "公称压力，默认 16"},
                        "axis": {"type": "string", "description": "流道轴向，默认 x"},
                        "valve_type": {"type": "string", "description": "止回阀型式，当前优先支持 swing（旋启式）"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选），默认白色"}
                    },
                    "required": ["center", "dn"],
                },
            ),
            types.Tool(
                name="draw_pressure_gauge",
                description="在CAD中绘制一般压力表（3D）。参考 GB/T 1226 常用 Y-60 / Y-100 / Y-150 系列，支持径向(radial)/轴向(axial)和直装(direct)/盘装(panel)。center 为表盘中心，face_axis 为表盘法向；未指定颜色时默认白色。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "表盘中心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "nominal_diameter": {"type": "integer", "description": "表盘公称直径，支持 60 / 100 / 150，默认 100"},
                        "style": {"type": "string", "description": "安装型式：radial(径向) 或 axial(轴向)，默认 radial"},
                        "mount_type": {"type": "string", "description": "安装方式：direct(直装) 或 panel(盘装/凸装)，默认 direct"},
                        "face_axis": {"type": "string", "description": "表盘法向 x/y/z 或 -x/-y/-z，默认 x"},
                        "thread_size": {"type": "string", "description": "接头螺纹，可选；默认按规格自动选 M14x1.5 或 M20x1.5"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选），默认白色"}
                    },
                    "required": ["center"],
                },
            ),
            types.Tool(
                name="draw_rigid_waterproof_sleeve",
                description="在CAD中绘制刚性防水套管（3D）。参考 02S404 A 型钢管穿墙套管。center 为套管几何中心，axis 为穿墙方向，当前内置 DN50-DN450 常用标准参数；未指定颜色时默认白色。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "套管几何中心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "dn": {"type": "integer", "description": "公称通径（mm），支持 DN50-DN450 常用 A 型系列"},
                        "axis": {"type": "string", "description": "穿墙方向 x/y/z 或 -x/-y/-z，默认 x"},
                        "sleeve_type": {"type": "string", "description": "套管型式，当前支持 A，默认 A"},
                        "length": {"type": "number", "description": "套管长度（mm），不填则取标准默认值"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选），默认白色"}
                    },
                    "required": ["center", "dn"],
                },
            ),
            types.Tool(
                name="draw_steel_bellmouth",
                description="在CAD中绘制钢制喇叭口（3D）。参考 02S403 吸水喇叭口系列。center 为小端连接端面圆心，axis 为从小端指向喇叭开口方向；默认按 DN 对应壁厚系列和常见喇叭扩口比例生成，未指定颜色时默认白色。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "小端连接端面圆心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "dn": {"type": "integer", "description": "公称通径（mm）"},
                        "axis": {"type": "string", "description": "喇叭口轴向 x/y/z 或 -x/-y/-z，默认 x"},
                        "flare_length": {"type": "number", "description": "扩口段长度（mm），可选"},
                        "throat_length": {"type": "number", "description": "小端直管段长度（mm），可选"},
                        "mouth_outer_diameter": {"type": "number", "description": "喇叭口大口端外径（mm），可选"},
                        "wall_thickness": {"type": "number", "description": "壁厚（mm），可选；不指定时按 DN 系列自动取值"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选），默认白色"}
                    },
                    "required": ["center", "dn"],
                },
            ),
            types.Tool(
                name="draw_double_flange_limit_expansion_joint",
                description="在CAD中绘制双法兰限位伸缩接头（3D）。参考 GB/T 12465，兼容 JSSJA-2 / VSSJA-2(B2F) 命名。center 为接头几何中心，axis 为流道轴向；当前优先支持已核实的 DN250 PN1.0 标准参数，未指定颜色时默认白色。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "接头几何中心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "dn": {"type": "integer", "description": "公称通径（mm），当前优先支持 250"},
                        "model": {"type": "string", "description": "型号名称，支持 JSSJA-2 / VSSJA-2 / B2F，默认 JSSJA-2"},
                        "pn": {"type": "number", "description": "公称压力（MPa），默认 1.0"},
                        "axis": {"type": "string", "description": "流道轴向 x/y/z 或 -x/-y/-z，默认 x"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选），默认白色"}
                    },
                    "required": ["center", "dn"],
                },
            ),
            types.Tool(
                name="draw_suction_bellmouth_support",
                description="在CAD中绘制吸水喇叭管支架（3D）。参考 02S403 ZA/ZB/ZC/ZD 系列。center 为支架几何中心，axis 为支架与喇叭管同轴方向；当前优先支持 ZB3 的 J 吸水喇叭管支架(B型)公开规格，未指定颜色时默认白色。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "支架几何中心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "support_model": {"type": "string", "description": "支架型号，如 ZB3，默认 ZB3"},
                        "bellmouth_form": {"type": "string", "description": "喇叭管形式代号，如 J，默认 J"},
                        "support_type": {"type": "string", "description": "支架型式，如 B，默认 B"},
                        "axis": {"type": "string", "description": "支架轴向 x/y/z 或 -x/-y/-z，默认 x"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选），默认白色"}
                    },
                    "required": ["center"],
                },
            ),
            types.Tool(
                name="draw_elliptical_head",
                description="在CAD中绘制椭圆封头（3D）。国标GB/T 25198压力容器封头，2:1椭圆，可带直边。中心为开口面圆心，封头向axis正方向凸出。需指定dn(公称直径/内径mm)。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "开口面圆心 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "dn": {"type": "number", "description": "公称直径（mm），封头内径"},
                        "axis": {"type": "string", "description": "凸出方向轴向，默认 z"},
                        "with_straight": {"type": "boolean", "description": "是否带直边段，默认 false（仅椭圆曲面）"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "dn"],
                },
            ),
            types.Tool(
                name="draw_hemisphere",
                description="在CAD中绘制半球（3D实体）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "球心坐标 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "radius": {"type": "number", "description": "半径"},
                        "flat_normal": {"type": "array", "description": "平面法向 [x, y, z]，默认(0,1,0)表示保留y正方向", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "radius"],
                },
            ),
            types.Tool(
                name="draw_hemisphere_extruded_y",
                description="在CAD中绘制半球并沿Y轴拉伸（3D实体）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "center": {"type": "array", "description": "球心坐标 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "radius": {"type": "number", "description": "半径"},
                        "extrude_height": {"type": "number", "description": "沿Y轴拉伸高度"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["center", "radius", "extrude_height"],
                },
            ),
            types.Tool(
                name="draw_spline",
                description="在CAD中绘制样条曲线",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "points": {
                            "type": "array",
                            "description": "控制点坐标列表 [[x1,y1,z1], [x2,y2,z2], ...]",
                            "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                            "minItems": 2
                        },
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"},
                        "lineweight": {"type": "number", "description": "线宽（可选）"}
                    },
                    "required": ["points"],
                },
            ),
            types.Tool(
                name="draw_mtext",
                description="在CAD中添加多行文字",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "position": {"type": "array", "description": "插入点坐标 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "width": {"type": "number", "description": "文本框宽度"},
                        "text": {"type": "string", "description": "文字内容"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["position", "width", "text"],
                },
            ),
            types.Tool(
                name="zoom_extents",
                description="缩放到显示所有对象",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="set_named_view",
                description="切换到命名视图。支持 front/top/left/right/back/bottom 以及 swiso/seiso/neiso/nwiso，也支持中文如主视图、俯视图、左视图、西南等轴测。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "view_name": {"type": "string", "description": "目标视图名称，如 front、top、left、swiso、主视图、俯视图、西南等轴测"},
                        "target": {"type": "array", "description": "视图目标点 [x, y, z]（可选）", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                        "center": {"type": "array", "description": "视口中心 [x, y]（可选）", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                        "height": {"type": "number", "description": "视口高度（可选）"},
                        "width": {"type": "number", "description": "视口宽度（可选）"},
                        "zoom_after_change": {"type": "boolean", "description": "切换后是否自动缩放到全部对象，默认 true"}
                    },
                    "required": ["view_name"],
                },
            ),

            types.Tool(
                name="boolean_union",
                description="对两个3D实体做布尔并集。需要提供实体的Handle（可通过 drawing://current 资源查看已绘制实体的handle）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "handle_a": {"type": "string", "description": "实体A的Handle"},
                        "handle_b": {"type": "string", "description": "实体B的Handle"}
                    },
                    "required": ["handle_a", "handle_b"],
                },
            ),
            types.Tool(
                name="boolean_intersect",
                description="对两个3D实体做布尔交集。需要提供实体的Handle",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "handle_a": {"type": "string", "description": "实体A的Handle"},
                        "handle_b": {"type": "string", "description": "实体B的Handle"}
                    },
                    "required": ["handle_a", "handle_b"],
                },
            ),
            types.Tool(
                name="boolean_subtract",
                description="对两个3D实体做布尔差集。从 main 中减去 tool 与之相交的部分",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "handle_main": {"type": "string", "description": "主实体Handle（保留）"},
                        "handle_tool": {"type": "string", "description": "减除实体Handle（将被删除）"}
                    },
                    "required": ["handle_main", "handle_tool"],
                },
            ),
            types.Tool(
                name="rotate_entity",
                description="旋转 2D/3D 实体。沿某一点旋转：提供 base_point 和 axis_direction（过该点的轴方向）；沿某一条线旋转：提供 base_point 和 axis_point2（轴上的两点）",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "handle": {"type": "string", "description": "实体 Handle"},
                        "base_point": {"type": "array", "description": "旋转轴上的点 [x, y] 或 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "angle": {"type": "number", "description": "旋转角度（度）"},
                        "axis_point2": {"type": "array", "description": "沿一条线旋转时，旋转轴第二点 [x, y, z]", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                        "axis_direction": {"type": "array", "description": "沿某一点旋转时，旋转轴方向向量 [x, y, z]，如 [1,0,0] 绕 X 轴、[0,1,0] 绕 Y 轴、[0,0,1] 绕 Z 轴", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
                    },
                    "required": ["handle", "base_point", "angle"],
                },
            ),
            types.Tool(
                name="extrude_entity",
                description="3D 拉伸（EXTRUDE）：将 2D 封闭图形沿 Z 轴或沿路径拉伸为 3D 实体。提供 height 为沿 Z 拉伸；提供 path_handle 为沿路径拉伸",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "handle": {"type": "string", "description": "2D 实体 Handle"},
                        "height": {"type": "number", "description": "拉伸高度（沿 Z 轴正方向），与 path_handle 二选一"},
                        "path_handle": {"type": "string", "description": "路径曲线 Handle（Line、Arc、Spline、Polyline），与 height 二选一"},
                        "taper_angle": {"type": "number", "description": "锥角（度），-90 到 90，默认 0（仅沿 Z 拉伸时有效）"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["handle"],
                },
            ),
            types.Tool(
                name="revolve_entity",
                description="将 2D 封闭图形绕轴旋转生成 3D 实体（REVOLVE）。默认旋转 360° 一周",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "handle": {"type": "string", "description": "2D 实体 Handle"},
                        "axis_point": {"type": "array", "description": "旋转轴上一点 [x, y, z]", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "axis_direction": {"type": "array", "description": "旋转轴方向向量 [x, y, z]，如 [1,0,0] 绕 X 轴、[0,1,0] 绕 Y 轴、[0,0,1] 绕 Z 轴", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                        "angle": {"type": "number", "description": "旋转角度（度），默认 360 为一周"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["handle", "axis_point", "axis_direction"],
                },
            ),
            types.Tool(
                name="draw_text",
                description="在CAD中添加文本",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "position": {
                            "type": "array",
                            "description": "插入点坐标 [x, y, z]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        },
                        "text": {"type": "string", "description": "文本内容"},
                        "height": {"type": "number", "description": "文本高度"},
                        "rotation": {"type": "number", "description": "旋转角度（度）"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["position", "text"],
                },
            ),
           
            types.Tool(
                name="draw_hatch",
                description="在CAD中绘制填充",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "points": {
                            "type": "array",
                            "description": "填充边界点集 [[x1, y1, z1], [x2, y2, z2], ...]",
                            "items": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2,
                                "maxItems": 3
                            },
                            "minItems": 3
                        },
                        "pattern_name": {"type": "string", "description": "填充图案名称（可选，默认为SOLID）"},
                        "scale": {"type": "number", "description": "填充图案比例（可选，默认为1.0）"},
                        "layer": {"type": "string", "description": "图层名称（可选）"},
                        "color": {"type": "string", "description": "颜色名称（可选）"}
                    },
                    "required": ["points"],
                },
            ),

            types.Tool(
                name="add_dimension",
                description="在CAD中添加尺寸标注，支持对齐、水平、垂直、直径、半径",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "dimension_type": {
                            "type": "string",
                            "description": "标注类型，可选 aligned、horizontal、vertical、diameter、radius",
                        },
                        "start_point": {"type": "array", "description": "线性标注起点坐标 [x, y, z]；半径标注时可复用为圆心；直径标注时可复用为第一圆周点", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "end_point": {"type": "array", "description": "线性标注终点坐标 [x, y, z]；半径标注时可复用为圆周点；直径标注时可复用为第二圆周点", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "text_position": {"type": "array", "description": "文本位置坐标 [x, y, z]，可选", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "center": {"type": "array", "description": "半径标注圆心 [x, y, z]，可选", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "chord_point": {"type": "array", "description": "半径/直径标注的圆周点 [x, y, z]，可选", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "far_chord_point": {"type": "array", "description": "直径标注的另一圆周点 [x, y, z]，可选", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                        "leader_length": {"type": "number", "description": "半径/直径标注引线长度，可选"},
                        "source_view": {"type": "string", "description": "目标视图，默认 front（主视图）"},
                        "textheight": {"type": "number", "description": "标注文本高度，可选"},
                        "layer": {"type": "string", "description": "图层名称，可选"},
                        "color": {"type": "string", "description": "颜色名称，可选"}
                    },
                    "required": [],
                },
            ),

            types.Tool(
                name="apply_dimensions",
                description="批量在CAD中添加尺寸标注",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "dimensions": {
                            "type": "array",
                            "description": "尺寸标注对象列表",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "dimension_id": {"type": "string"},
                                    "dimension_type": {"type": "string"},
                                    "start_point": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                                    "end_point": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                                    "text_position": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                                    "center": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                                    "chord_point": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                                    "far_chord_point": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                                    "leader_length": {"type": "number"},
                                    "textheight": {"type": "number"},
                                    "layer": {"type": "string"},
                                    "color": {"type": "string"},
                                    "source_view": {"type": "string", "description": "目标视图，默认 front（主视图）"},
                                    "orientation": {"type": "string"},
                                    "value": {"type": "number"}
                                }
                            }
                        }
                    },
                    "required": ["dimensions"],
                },
            ),

            types.Tool(
                name="save_drawing",
                description="保存当前图纸",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "保存文件名或路径，最终会统一保存到 CAD/output/<内容名称>/ 目录"}
                    },
                    "required": ["file_path"],
                },
            ),

            types.Tool(
                name="process_command",
                description="处理自然语言命令并转换为CAD操作",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "自然语言命令"}
                    },
                    "required": ["command"],
                },
            ),
            types.Tool(
                name="parse_drawing_document",
                description="识别 PDF 或图片图纸，并输出结构化配管语义模型与待确认项。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "图纸文件路径，支持图片和单页 PDF"},
                        "drawing_type": {"type": "string", "description": "图纸类型：isometric、pid、layout，默认 isometric"},
                        "page_number": {"type": "integer", "description": "PDF 页码，默认 1"},
                        "recognized_data": {
                            "description": "可选：外部已识别出的结构化 JSON，可直接跳过视觉识别",
                            "anyOf": [{"type": "object"}, {"type": "string"}]
                        },
                        "review_overrides": {
                            "type": "object",
                            "description": "可选：在解析完成后立即应用人工确认值，键为字段路径",
                            "additionalProperties": True
                        },
                        "focus_hint": {"type": "string", "description": "可选：额外关注点，例如“重点看管径和阀门类型”"}
                    },
                    "required": ["file_path"],
                },
            ),
            types.Tool(
                name="analyze_document_visual",
                description="将 PDF 或图片先转换为高清 PNG/JPG，再交给视觉模型分析，返回渲染图片路径和逐页分析结果。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "文件路径，支持 PDF 和图片"},
                        "page_number": {"type": "integer", "description": "分析单页 PDF 时的页码，默认 1"},
                        "analyze_all_pages": {"type": "boolean", "description": "是否分析 PDF 的多页内容，默认 false"},
                        "max_pages": {"type": "integer", "description": "多页分析时最多处理多少页，默认 5"},
                        "dpi": {"type": "integer", "description": "PDF 渲染 DPI，默认 300"},
                        "image_format": {"type": "string", "description": "渲染图片格式：png 或 jpg，默认 png"},
                        "prompt": {"type": "string", "description": "可选：覆盖默认视觉分析提示词"},
                        "focus_hint": {"type": "string", "description": "可选：额外关注点，例如“重点看技术要求和尺寸表”"},
                    },
                    "required": ["file_path"],
                },
            ),
            types.Tool(
                name="apply_pipeline_review",
                description="对结构化配管语义模型应用人工确认结果，更新 review_status。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "semantic_model": {"type": "object", "description": "parse_drawing_document 返回的 semantic_model"},
                        "resolutions": {
                            "type": "object",
                            "description": "人工确认结果，键为字段路径，例如 graph.segments.0.dn",
                            "additionalProperties": True
                        }
                    },
                    "required": ["semantic_model", "resolutions"],
                },
            ),
            types.Tool(
                name="draw_pipeline_from_json",
                description="将已确认的结构化配管语义模型转换为 CAD-MCP 的 3D 绘图调用。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "semantic_model": {"type": "object", "description": "结构化配管语义模型"},
                        "require_review": {"type": "boolean", "description": "是否要求无未确认问题才能绘制，默认 true"},
                        "zoom_after_draw": {"type": "boolean", "description": "绘制完成后是否执行 zoom_extents，默认 true"}
                    },
                    "required": ["semantic_model"],
                },
            ),
            types.Tool(
                name="get_latest_vision_feedback",
                description="获取最近一次视觉反馈。返回统一 JSON 结构，包含状态、摘要、发现列表、建议动作以及 provider/model/latency 等字段。",
                inputSchema=GET_LATEST_VISION_FEEDBACK_INPUT_SCHEMA,
            ),
            types.Tool(
                name="analyze_current_view",
                description="主动触发一次当前 CAD 视图分析。返回统一 JSON 结构，并附带 job_id 以便后续读取 vision://jobs/<job_id>。",
                inputSchema=ANALYZE_CURRENT_VIEW_INPUT_SCHEMA,
            ),
            types.Tool(
                name="set_vision_loop_enabled",
                description="启用或停用视觉轮询回路。返回统一 JSON 结构，并反映最新 loop_enabled 状态。",
                inputSchema=SET_VISION_LOOP_ENABLED_INPUT_SCHEMA,
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """处理工具执行请求"""
        try:
            arguments = arguments or {}

            if name == "draw_line":
                start_point = arguments.get("start_point")
                end_point = arguments.get("end_point")
                layer = arguments.get("layer")
                color = arguments.get("color") 
                lineweight = arguments.get("lineweight")
                
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                
                if not start_point or not end_point:
                    raise ValueError("缺少起点或终点坐标")
                
                
                result = cad_service.draw_line(start_point, end_point, layer, color, lineweight)
                return [types.TextContent(type="text", text=str(result))]

            elif name == "draw_circle":
                center = arguments.get("center")
                radius = arguments.get("radius")
                layer = arguments.get("layer")
                color = arguments.get("color")
                lineweight = arguments.get("lineweight")
                
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                
                if not center or radius is None:
                    raise ValueError("缺少圆心坐标或半径")
                
                
                result = cad_service.draw_circle(center, radius, layer, color, lineweight)
                msg = f"圆已绘制，handle={result.Handle}" if result and hasattr(result, 'Handle') else (str(result) if result else "绘制失败")
                return [types.TextContent(type="text", text=msg)]

            elif name == "draw_arc":
                center = arguments.get("center")
                radius = arguments.get("radius")
                start_angle = arguments.get("start_angle")
                end_angle = arguments.get("end_angle")
                layer = arguments.get("layer")
                color = arguments.get("color")
                lineweight = arguments.get("lineweight")
                
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                
                # 详细检查每个参数并提供具体的错误信息
                error_msgs = []
                if not center:
                    error_msgs.append("中心点坐标")
                if radius is None:
                    error_msgs.append("半径")
                if start_angle is None:
                    error_msgs.append("起始角度")
                if end_angle is None:
                    error_msgs.append("结束角度")
                
                if error_msgs:
                    raise ValueError(f"缺少必要参数: {', '.join(error_msgs)}")                
                
                result = cad_service.draw_arc(center, radius, start_angle, end_angle, layer, color, lineweight)
                return [types.TextContent(type="text", text=str(result))]

            elif name == "draw_ellipse":
                center = arguments.get("center")
                major_axis = arguments.get("major_axis")
                minor_axis = arguments.get("minor_axis")
                rotation = arguments.get("rotation")
                layer = arguments.get("layer")
                color = arguments.get("color")
                lineweight = arguments.get("lineweight")
                
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                
                # 详细检查每个参数并提供具体的错误信息
                error_msgs = []
                if not center:
                    error_msgs.append("中心点坐标")
                if major_axis is None:
                    error_msgs.append("长轴")
                if minor_axis is None:
                    error_msgs.append("短轴")
                
                if error_msgs:
                    raise ValueError(f"缺少必要参数: {', '.join(error_msgs)}")
                
                result = cad_service.draw_ellipse(center, major_axis, minor_axis, rotation, layer, color, lineweight)
                return [types.TextContent(type="text", text=str(result))]

            elif name == "draw_polyline":
                points = arguments.get("points")
                closed = arguments.get("closed", False)
                layer = arguments.get("layer")
                color = arguments.get("color")
                lineweight = arguments.get("lineweight")
                
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                
                if not points or len(points) < 2:
                    raise ValueError("缺少点集或点数不足")                
                
                result = cad_service.draw_polyline(points, closed, layer, color, lineweight)
                msg = f"多段线已绘制，handle={result.Handle}" if result and hasattr(result, 'Handle') else (str(result) if result else "绘制失败")
                return [types.TextContent(type="text", text=msg)]

            elif name == "draw_rectangle":
                corner1 = arguments.get("corner1")
                corner2 = arguments.get("corner2")
                layer = arguments.get("layer")
                color = arguments.get("color")
                lineweight = arguments.get("lineweight")
                
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                
                if not corner1 or not corner2:
                    raise ValueError("缺少角点坐标")                
                
                result = cad_service.draw_rectangle(corner1, corner2, layer, color, lineweight)
                msg = f"矩形已绘制，handle={result.Handle}" if result and hasattr(result, 'Handle') else (str(result) if result else "绘制失败")
                return [types.TextContent(type="text", text=msg)]

            elif name == "draw_sphere":
                center = arguments.get("center")
                radius = arguments.get("radius")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or radius is None:
                    raise ValueError("缺少球心坐标或半径")
                result = cad_service.draw_sphere(center, radius, layer, color)
                return [types.TextContent(type="text", text="球体已绘制" if result else "绘制球体失败")]

            elif name == "draw_box":
                center = arguments.get("center")
                length = arguments.get("length")
                width = arguments.get("width")
                height = arguments.get("height")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or length is None or width is None or height is None:
                    raise ValueError("缺少中心坐标或长宽高")
                result = cad_service.draw_box(center, length, width, height, layer, color)
                return [types.TextContent(type="text", text="长方体已绘制" if result else "绘制长方体失败")]

            elif name == "draw_cylinder":
                center = arguments.get("center")
                radius = arguments.get("radius")
                height = arguments.get("height")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or radius is None or height is None:
                    raise ValueError("缺少底面圆心、半径或高度")
                result = cad_service.draw_cylinder(center, radius, height, layer, color)
                return [types.TextContent(type="text", text="圆柱已绘制" if result else "绘制圆柱失败")]

            elif name == "draw_cylinder_along_y":
                base_center = arguments.get("base_center")
                radius = arguments.get("radius")
                height_y = arguments.get("height_y")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not base_center or radius is None or height_y is None:
                    raise ValueError("缺少底面圆心、半径或高度")
                result = cad_service.draw_cylinder_along_y(base_center, radius, height_y, layer, color)
                return [types.TextContent(type="text", text="沿Y轴圆柱已绘制" if result else "绘制失败")]

            elif name == "draw_cone":
                center = arguments.get("center")
                base_radius = arguments.get("base_radius")
                height = arguments.get("height")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or base_radius is None or height is None:
                    raise ValueError("缺少底面圆心、半径或高度")
                result = cad_service.draw_cone(center, base_radius, height, layer, color)
                return [types.TextContent(type="text", text="圆锥已绘制" if result else "绘制圆锥失败")]

            elif name == "draw_torus":
                center = arguments.get("center")
                major_radius = arguments.get("major_radius")
                minor_radius = arguments.get("minor_radius")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or major_radius is None or minor_radius is None:
                    raise ValueError("缺少中心、主半径或管道半径")
                result = cad_service.draw_torus(center, major_radius, minor_radius, layer, color)
                return [types.TextContent(type="text", text="圆环已绘制" if result else "绘制圆环失败")]

            elif name == "draw_pipe_elbow":
                center = arguments.get("center")
                pipe_radius = arguments.get("pipe_radius")
                dn = arguments.get("dn")
                bend_radius = arguments.get("bend_radius")
                elbow_type = arguments.get("elbow_type", "LR")
                angle = arguments.get("angle", 90)
                plane = arguments.get("plane", "xy")
                wall_thickness = arguments.get("wall_thickness")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center:
                    raise ValueError("缺少弯头中心")
                if pipe_radius is None and dn is None:
                    raise ValueError("须指定 pipe_radius 或 dn（国标公称通径）")
                result = cad_service.draw_pipe_elbow(center, pipe_radius=pipe_radius, dn=dn, bend_radius=bend_radius,
                                                     elbow_type=elbow_type, angle=angle, plane=plane,
                                                     wall_thickness=wall_thickness, layer=layer, color=color)
                return [types.TextContent(type="text", text=f"{angle}°弯头已绘制" if result else "绘制弯头失败")]

            elif name == "draw_reducer":
                center = arguments.get("center")
                dn_large = arguments.get("dn_large")
                dn_small = arguments.get("dn_small")
                axis = arguments.get("axis", "z")
                reducer_type = arguments.get("reducer_type", "concentric")
                length = arguments.get("length")
                wall_thickness = arguments.get("wall_thickness")
                eccentric_direction = arguments.get("eccentric_direction")
                eccentric_offset = arguments.get("eccentric_offset")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or dn_large is None or dn_small is None:
                    raise ValueError("缺少中心或 dn_large 或 dn_small")
                if dn_small >= dn_large:
                    raise ValueError("dn_small 须小于 dn_large")
                result = cad_service.draw_reducer(
                    center,
                    dn_large,
                    dn_small,
                    axis=axis,
                    reducer_type=reducer_type,
                    length=length,
                    wall_thickness=wall_thickness,
                    eccentric_direction=eccentric_direction,
                    eccentric_offset=eccentric_offset,
                    layer=layer,
                    color=color,
                )
                fitting_name = "偏心异径管" if str(reducer_type).lower().startswith("ecc") else "同心异径管"
                return [types.TextContent(type="text", text=f"{fitting_name}已绘制" if result else f"绘制{fitting_name}失败")]

            elif name == "draw_tee":
                center = arguments.get("center")
                dn = arguments.get("dn")
                branch_dn = arguments.get("branch_dn")
                run_axis = arguments.get("run_axis", "x")
                branch_axis = arguments.get("branch_axis", "z")
                wall_thickness = arguments.get("wall_thickness")
                branch_wall_thickness = arguments.get("branch_wall_thickness")
                run_center_to_end = arguments.get("run_center_to_end")
                branch_center_to_end = arguments.get("branch_center_to_end")
                reinforce = arguments.get("reinforce", True)
                reinforce_extra = arguments.get("reinforce_extra")
                reinforce_run_ratio = arguments.get("reinforce_run_ratio", 0.55)
                reinforce_branch_ratio = arguments.get("reinforce_branch_ratio", 0.70)
                transition_ratio = arguments.get("transition_ratio", 0.0)
                junction_bulge = arguments.get("junction_bulge", True)
                junction_bulge_extra = arguments.get("junction_bulge_extra")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or dn is None:
                    raise ValueError("缺少中心或 dn")
                result = cad_service.draw_tee(
                    center,
                    dn,
                    branch_dn=branch_dn,
                    run_axis=run_axis,
                    branch_axis=branch_axis,
                    wall_thickness=wall_thickness,
                    branch_wall_thickness=branch_wall_thickness,
                    run_center_to_end=run_center_to_end,
                    branch_center_to_end=branch_center_to_end,
                    layer=layer,
                    color=color,
                    reinforce=reinforce,
                    reinforce_extra=reinforce_extra,
                    reinforce_run_ratio=reinforce_run_ratio,
                    reinforce_branch_ratio=reinforce_branch_ratio,
                    transition_ratio=transition_ratio,
                    junction_bulge=junction_bulge,
                    junction_bulge_extra=junction_bulge_extra,
                )
                return [types.TextContent(type="text", text="三通已绘制" if result else "绘制三通失败")]

            elif name == "draw_wedge":
                center = arguments.get("center")
                length = arguments.get("length")
                width = arguments.get("width")
                height = arguments.get("height")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or length is None or width is None or height is None:
                    raise ValueError("缺少中心坐标或长宽高")
                result = cad_service.draw_wedge(center, length, width, height, layer, color)
                return [types.TextContent(type="text", text="楔体已绘制" if result else "绘制楔体失败")]

            elif name == "draw_flange":
                center = arguments.get("center")
                specification = arguments.get("specification")
                dn = arguments.get("dn")
                pn = arguments.get("pn")
                outer_diameter = arguments.get("outer_diameter")
                center_hole_diameter = arguments.get("center_hole_diameter")
                bolt_circle_diameter = arguments.get("bolt_circle_diameter")
                bolt_hole_diameter = arguments.get("bolt_hole_diameter")
                bolt_count = arguments.get("bolt_count")
                thickness = arguments.get("thickness")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center:
                    raise ValueError("缺少法兰中心坐标")
                result = cad_service.draw_flange(
                    center,
                    specification=specification,
                    dn=dn,
                    pn=pn,
                    outer_diameter=outer_diameter,
                    center_hole_diameter=center_hole_diameter,
                    bolt_circle_diameter=bolt_circle_diameter,
                    bolt_hole_diameter=bolt_hole_diameter,
                    bolt_count=bolt_count,
                    thickness=thickness,
                    layer=layer,
                    color=color,
                )
                return [types.TextContent(type="text", text="法兰已绘制" if result else "绘制法兰失败")]

            elif name == "draw_blind_flange":
                center = arguments.get("center")
                specification = arguments.get("specification")
                dn = arguments.get("dn")
                pn = arguments.get("pn")
                outer_diameter = arguments.get("outer_diameter")
                bolt_circle_diameter = arguments.get("bolt_circle_diameter")
                bolt_hole_diameter = arguments.get("bolt_hole_diameter")
                bolt_count = arguments.get("bolt_count")
                thickness = arguments.get("thickness")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center:
                    raise ValueError("缺少盲板中心坐标")
                result = cad_service.draw_blind_flange(
                    center, specification=specification, dn=dn, pn=pn,
                    outer_diameter=outer_diameter, bolt_circle_diameter=bolt_circle_diameter,
                    bolt_hole_diameter=bolt_hole_diameter, bolt_count=bolt_count, thickness=thickness,
                    layer=layer, color=color,
                )
                return [types.TextContent(type="text", text="盲板已绘制" if result else "绘制盲板失败")]

            elif name == "draw_manhole":
                center = arguments.get("center")
                specification = arguments.get("specification")
                dn = arguments.get("dn")
                pn = arguments.get("pn")
                opening_diameter = arguments.get("opening_diameter")
                neck_outer_diameter = arguments.get("neck_outer_diameter")
                neck_wall_thickness = arguments.get("neck_wall_thickness", 10.0)
                neck_height = arguments.get("neck_height", 120.0)
                flange_outer_diameter = arguments.get("flange_outer_diameter")
                bolt_circle_diameter = arguments.get("bolt_circle_diameter")
                bolt_hole_diameter = arguments.get("bolt_hole_diameter")
                bolt_count = arguments.get("bolt_count")
                flange_thickness = arguments.get("flange_thickness")
                cover_outer_diameter = arguments.get("cover_outer_diameter")
                cover_thickness = arguments.get("cover_thickness", 20.0)
                cover_gap = arguments.get("cover_gap", 6.0)
                handle_count = arguments.get("handle_count", 2)
                handle_width = arguments.get("handle_width", 180.0)
                handle_height = arguments.get("handle_height", 60.0)
                handle_bar_diameter = arguments.get("handle_bar_diameter", 16.0)
                add_lifting_lug = arguments.get("add_lifting_lug", True)
                lug_width = arguments.get("lug_width", 56.0)
                lug_thickness = arguments.get("lug_thickness", 20.0)
                lug_height = arguments.get("lug_height", 110.0)
                lug_hole_diameter = arguments.get("lug_hole_diameter", 28.0)
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center:
                    raise ValueError("缺少人孔中心坐标")
                result = cad_service.draw_manhole(
                    center,
                    specification=specification,
                    dn=dn,
                    pn=pn,
                    opening_diameter=opening_diameter,
                    neck_outer_diameter=neck_outer_diameter,
                    neck_wall_thickness=neck_wall_thickness,
                    neck_height=neck_height,
                    flange_outer_diameter=flange_outer_diameter,
                    bolt_circle_diameter=bolt_circle_diameter,
                    bolt_hole_diameter=bolt_hole_diameter,
                    bolt_count=bolt_count,
                    flange_thickness=flange_thickness,
                    cover_outer_diameter=cover_outer_diameter,
                    cover_thickness=cover_thickness,
                    cover_gap=cover_gap,
                    handle_count=handle_count,
                    handle_width=handle_width,
                    handle_height=handle_height,
                    handle_bar_diameter=handle_bar_diameter,
                    add_lifting_lug=add_lifting_lug,
                    lug_width=lug_width,
                    lug_thickness=lug_thickness,
                    lug_height=lug_height,
                    lug_hole_diameter=lug_hole_diameter,
                    layer=layer,
                    color=color,
                )
                return [types.TextContent(type="text", text="人孔已绘制" if result else "绘制人孔失败")]

            elif name == "draw_bolt":
                base_center = arguments.get("base_center")
                specification = arguments.get("specification")
                thread_size = arguments.get("thread_size")
                shank_diameter = arguments.get("shank_diameter")
                length = arguments.get("length")
                axis = arguments.get("axis", "z")
                head_width = arguments.get("head_width")
                head_height = arguments.get("head_height")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not base_center or length is None:
                    raise ValueError("缺少 base_center 或 length")
                if not specification and shank_diameter is None:
                    raise ValueError("自定义模式缺少 shank_diameter；国标模式请提供 specification 和 thread_size")
                result = cad_service.draw_bolt(
                    base_center,
                    shank_diameter,
                    length,
                    axis=axis,
                    head_width=head_width,
                    head_height=head_height,
                    specification=specification,
                    thread_size=thread_size,
                    layer=layer,
                    color=color,
                )
                return [types.TextContent(type="text", text="螺栓已绘制" if result else "绘制螺栓失败")]

            elif name == "draw_hex_nut":
                center = arguments.get("center")
                specification = arguments.get("specification")
                thread_size = arguments.get("thread_size")
                inner_diameter = arguments.get("inner_diameter")
                thickness = arguments.get("thickness")
                width_across_flats = arguments.get("width_across_flats")
                axis = arguments.get("axis", "z")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center:
                    raise ValueError("缺少 center")
                if not specification and inner_diameter is None:
                    raise ValueError("自定义模式缺少 inner_diameter；国标模式请提供 specification 和 thread_size")
                result = cad_service.draw_hex_nut(
                    center,
                    inner_diameter,
                    thickness=thickness,
                    width_across_flats=width_across_flats,
                    axis=axis,
                    specification=specification,
                    thread_size=thread_size,
                    layer=layer,
                    color=color,
                )
                return [types.TextContent(type="text", text="螺母已绘制" if result else "绘制螺母失败")]

            elif name == "draw_washer":
                center = arguments.get("center")
                specification = arguments.get("specification")
                thread_size = arguments.get("thread_size")
                inner_diameter = arguments.get("inner_diameter")
                outer_diameter = arguments.get("outer_diameter")
                thickness = arguments.get("thickness")
                axis = arguments.get("axis", "z")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center:
                    raise ValueError("缺少 center")
                if not specification and inner_diameter is None:
                    raise ValueError("自定义模式缺少 inner_diameter；国标模式请提供 specification 和 thread_size")
                result = cad_service.draw_washer(
                    center,
                    inner_diameter,
                    outer_diameter=outer_diameter,
                    thickness=thickness,
                    axis=axis,
                    specification=specification,
                    thread_size=thread_size,
                    layer=layer,
                    color=color,
                )
                return [types.TextContent(type="text", text="垫圈已绘制" if result else "绘制垫圈失败")]

            elif name == "draw_ball_valve":
                center = arguments.get("center")
                dn = arguments.get("dn")
                pn = arguments.get("pn")
                axis = arguments.get("axis", "x")
                connection_type = arguments.get("connection_type", "flanged")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or dn is None or pn is None:
                    raise ValueError("缺少中心、dn 或 pn")
                result = cad_service.draw_ball_valve(
                    center,
                    dn,
                    pn,
                    axis=axis,
                    layer=layer,
                    color=color,
                    connection_type=connection_type,
                )
                return [types.TextContent(type="text", text="球阀已绘制" if result else "绘制球阀失败")]

            elif name == "draw_butterfly_valve":
                center = arguments.get("center")
                dn = arguments.get("dn")
                pn = arguments.get("pn")
                axis = arguments.get("axis", "z")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or dn is None or pn is None:
                    raise ValueError("缺少中心、dn 或 pn")
                result = cad_service.draw_butterfly_valve(center, dn, pn, axis=axis, layer=layer, color=color)
                return [types.TextContent(type="text", text="蝶阀已绘制" if result else "绘制蝶阀失败")]

            elif name == "draw_globe_valve":
                center = arguments.get("center")
                dn = arguments.get("dn")
                pn = arguments.get("pn")
                axis = arguments.get("axis", "z")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or dn is None or pn is None:
                    raise ValueError("缺少中心、dn 或 pn")
                result = cad_service.draw_globe_valve(center, dn, pn, axis=axis, layer=layer, color=color)
                return [types.TextContent(type="text", text="截止阀已绘制" if result else "绘制截止阀失败")]

            elif name == "draw_check_valve":
                center = arguments.get("center")
                dn = arguments.get("dn")
                pn = arguments.get("pn", 16)
                axis = arguments.get("axis", "x")
                valve_type = arguments.get("valve_type", "swing")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or dn is None:
                    raise ValueError("缺少中心或 dn")
                result = cad_service.draw_check_valve(
                    center,
                    dn,
                    pn,
                    axis=axis,
                    layer=layer,
                    color=color,
                    valve_type=valve_type,
                )
                return [types.TextContent(type="text", text="止回阀已绘制" if result else "绘制止回阀失败")]

            elif name == "draw_pressure_gauge":
                center = arguments.get("center")
                nominal_diameter = arguments.get("nominal_diameter", 100)
                style = arguments.get("style", "radial")
                mount_type = arguments.get("mount_type", "direct")
                face_axis = arguments.get("face_axis", "x")
                thread_size = arguments.get("thread_size")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center:
                    raise ValueError("缺少表盘中心 center")
                result = cad_service.draw_pressure_gauge(
                    center,
                    nominal_diameter=nominal_diameter,
                    style=style,
                    mount_type=mount_type,
                    face_axis=face_axis,
                    thread_size=thread_size,
                    layer=layer,
                    color=color,
                )
                return [types.TextContent(type="text", text="压力表已绘制" if result else "绘制压力表失败")]

            elif name == "draw_rigid_waterproof_sleeve":
                center = arguments.get("center")
                dn = arguments.get("dn")
                axis = arguments.get("axis", "x")
                sleeve_type = arguments.get("sleeve_type", "A")
                length = arguments.get("length")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or dn is None:
                    raise ValueError("缺少 center 或 dn")
                result = cad_service.draw_rigid_waterproof_sleeve(
                    center=center,
                    dn=int(dn),
                    axis=axis,
                    sleeve_type=sleeve_type,
                    length=length,
                    layer=layer,
                    color=color,
                )
                return [types.TextContent(type="text", text="刚性防水套管已绘制" if result else "绘制刚性防水套管失败")]

            elif name == "draw_steel_bellmouth":
                center = arguments.get("center")
                dn = arguments.get("dn")
                axis = arguments.get("axis", "x")
                flare_length = arguments.get("flare_length")
                throat_length = arguments.get("throat_length")
                mouth_outer_diameter = arguments.get("mouth_outer_diameter")
                wall_thickness = arguments.get("wall_thickness")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or dn is None:
                    raise ValueError("缺少 center 或 dn")
                result = cad_service.draw_steel_bellmouth(
                    center=center,
                    dn=int(dn),
                    axis=axis,
                    flare_length=flare_length,
                    throat_length=throat_length,
                    mouth_outer_diameter=mouth_outer_diameter,
                    wall_thickness=wall_thickness,
                    layer=layer,
                    color=color,
                )
                return [types.TextContent(type="text", text="钢制喇叭口已绘制" if result else "绘制钢制喇叭口失败")]

            elif name == "draw_double_flange_limit_expansion_joint":
                center = arguments.get("center")
                dn = arguments.get("dn")
                model = arguments.get("model", "JSSJA-2")
                pn = arguments.get("pn", 1.0)
                axis = arguments.get("axis", "x")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or dn is None:
                    raise ValueError("缺少 center 或 dn")
                result = cad_service.draw_double_flange_limit_expansion_joint(
                    center=center,
                    dn=int(dn),
                    model=model,
                    pn=float(pn),
                    axis=axis,
                    layer=layer,
                    color=color,
                )
                return [types.TextContent(type="text", text="双法兰限位伸缩接头已绘制" if result else "绘制双法兰限位伸缩接头失败")]

            elif name == "draw_suction_bellmouth_support":
                center = arguments.get("center")
                support_model = arguments.get("support_model", "ZB3")
                bellmouth_form = arguments.get("bellmouth_form", "J")
                support_type = arguments.get("support_type", "B")
                axis = arguments.get("axis", "x")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center:
                    raise ValueError("缺少 center")
                result = cad_service.draw_suction_bellmouth_support(
                    center=center,
                    support_model=support_model,
                    bellmouth_form=bellmouth_form,
                    support_type=support_type,
                    axis=axis,
                    layer=layer,
                    color=color,
                )
                return [types.TextContent(type="text", text="吸水喇叭管支架已绘制" if result else "绘制吸水喇叭管支架失败")]

            elif name == "draw_elliptical_head":
                center = arguments.get("center")
                dn = arguments.get("dn")
                axis = arguments.get("axis", "z")
                with_straight = arguments.get("with_straight", False)
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or dn is None:
                    raise ValueError("缺少中心或 dn")
                result = cad_service.draw_elliptical_head(center, dn, axis=axis, with_straight=with_straight, layer=layer, color=color)
                return [types.TextContent(type="text", text="椭圆封头已绘制" if result else "绘制椭圆封头失败")]

            elif name == "draw_hemisphere":
                center = arguments.get("center")
                radius = arguments.get("radius")
                flat_normal = arguments.get("flat_normal", [0, 1, 0])
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or radius is None:
                    raise ValueError("缺少球心坐标或半径")
                result = cad_service.draw_hemisphere(center, radius, tuple(flat_normal) if flat_normal else (0, 1, 0), layer, color)
                return [types.TextContent(type="text", text="半球已绘制" if result else "绘制半球失败")]

            elif name == "draw_hemisphere_extruded_y":
                center = arguments.get("center")
                radius = arguments.get("radius")
                extrude_height = arguments.get("extrude_height")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not center or radius is None or extrude_height is None:
                    raise ValueError("缺少球心、半径或拉伸高度")
                result = cad_service.draw_hemisphere_extruded_y(center, radius, extrude_height, layer, color)
                return [types.TextContent(type="text", text="半球拉伸已绘制" if result else "绘制失败")]

            elif name == "draw_spline":
                points = arguments.get("points")
                layer = arguments.get("layer")
                color = arguments.get("color")
                lineweight = arguments.get("lineweight")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not points or len(points) < 2:
                    raise ValueError("样条曲线至少需要2个控制点")
                result = cad_service.draw_spline(points, layer, color, lineweight)
                return [types.TextContent(type="text", text="样条曲线已绘制" if result else "绘制样条曲线失败")]

            elif name == "draw_mtext":
                position = arguments.get("position")
                width = arguments.get("width")
                text = arguments.get("text")
                layer = arguments.get("layer")
                color = arguments.get("color")
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color) if color else None
                if color_rgb is not None:
                    color = color_rgb
                if not position or width is None or not text:
                    raise ValueError("缺少插入点、宽度或文字内容")
                result = cad_service.draw_mtext(position, width, text, layer, color)
                return [types.TextContent(type="text", text="多行文字已添加" if result else "添加多行文字失败")]

            elif name == "zoom_extents":
                result = cad_service.zoom_extents()
                return [types.TextContent(type="text", text="已缩放到显示所有对象" if result else "缩放失败")]

            elif name == "set_named_view":
                view_name = arguments.get("view_name")
                target = arguments.get("target")
                center = arguments.get("center")
                height = float(arguments.get("height", 400.0) or 400.0)
                width = float(arguments.get("width", 600.0) or 600.0)
                zoom_after_change = bool(arguments.get("zoom_after_change", True))
                if not view_name:
                    raise ValueError("缺少 view_name")
                result = cad_service.set_named_view(
                    view_name=view_name,
                    target=target,
                    center=center,
                    height=height,
                    width=width,
                    zoom_after_change=zoom_after_change,
                )
                return [types.TextContent(type="text", text="视图切换成功" if result else "视图切换失败")]

            elif name == "boolean_union":
                handle_a = arguments.get("handle_a")
                handle_b = arguments.get("handle_b")
                if not handle_a or not handle_b:
                    raise ValueError("缺少 handle_a 或 handle_b")
                result = cad_service.boolean_union(handle_a, handle_b)
                return [types.TextContent(type="text", text="布尔并集成功" if result else "布尔并集失败")]

            elif name == "boolean_intersect":
                handle_a = arguments.get("handle_a")
                handle_b = arguments.get("handle_b")
                if not handle_a or not handle_b:
                    raise ValueError("缺少 handle_a 或 handle_b")
                result = cad_service.boolean_intersect(handle_a, handle_b)
                return [types.TextContent(type="text", text="布尔交集成功" if result else "布尔交集失败")]

            elif name == "boolean_subtract":
                handle_main = arguments.get("handle_main")
                handle_tool = arguments.get("handle_tool")
                if not handle_main or not handle_tool:
                    raise ValueError("缺少 handle_main 或 handle_tool")
                result = cad_service.boolean_subtract(handle_main, handle_tool)
                return [types.TextContent(type="text", text="布尔差集成功" if result else "布尔差集失败")]

            elif name == "rotate_entity":
                handle = arguments.get("handle")
                base_point = arguments.get("base_point")
                angle = arguments.get("angle")
                axis_point2 = arguments.get("axis_point2")
                axis_direction = arguments.get("axis_direction")
                if not handle or base_point is None or angle is None:
                    raise ValueError("缺少 handle、base_point 或 angle")
                result = cad_service.rotate_entity(handle, base_point, angle, axis_point2, axis_direction)
                return [types.TextContent(type="text", text=f"实体已旋转 {angle}°" if result else "旋转失败")]

            elif name == "extrude_entity":
                handle = arguments.get("handle")
                height = arguments.get("height")
                path_handle = arguments.get("path_handle")
                taper_angle = arguments.get("taper_angle", 0)
                layer = arguments.get("layer")
                color = arguments.get("color")
                if not handle:
                    raise ValueError("缺少 handle")
                if height is None and not path_handle:
                    raise ValueError("需提供 height 或 path_handle")
                result_handle = cad_service.extrude_entity(handle, height, taper_angle, path_handle, layer, color)
                return [types.TextContent(type="text", text=f"3D拉伸成功，新实体 handle={result_handle}" if result_handle else "拉伸失败")]

            elif name == "revolve_entity":
                handle = arguments.get("handle")
                axis_point = arguments.get("axis_point")
                axis_direction = arguments.get("axis_direction")
                angle = arguments.get("angle", 360)
                layer = arguments.get("layer")
                color = arguments.get("color")
                if not handle or not axis_point or not axis_direction:
                    raise ValueError("缺少 handle、axis_point 或 axis_direction")
                result_handle = cad_service.revolve_entity(handle, axis_point, axis_direction, angle, layer, color)
                return [types.TextContent(type="text", text=f"旋转生成实体成功，handle={result_handle}" if result_handle else "旋转生成实体失败")]

            elif name == "draw_text":
                position = arguments.get("position")
                text = arguments.get("text")
                height = arguments.get("height", 2.5)
                rotation = arguments.get("rotation", 0)
                layer = arguments.get("layer")
                color = arguments.get("color")
                
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                
                if not position or not text:
                    raise ValueError("缺少插入点坐标或文本内容")
                
                result = cad_service.draw_text(position, text, height, rotation, layer, color)
                return [types.TextContent(type="text", text=str(result))]


            elif name == "draw_hatch":
                points = arguments.get("points")
                pattern_name = arguments.get("pattern_name", "SOLID")
                scale = arguments.get("scale", 1.0)
                layer = arguments.get("layer")
                color = arguments.get("color")
                
                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色
                
                if not points or len(points) < 3:
                    raise ValueError("缺少点集或点数不足，填充边界至少需要3个点")
                
                result = cad_service.draw_hatch(points, pattern_name, scale, layer, color)
                return [types.TextContent(type="text", text=str(result))]

            elif name == "save_drawing":
                file_path = arguments.get("file_path")
                
                if not file_path:
                    raise ValueError("缺少保存路径")
                
                result = cad_service.save_drawing(file_path)
                final_file_path = cad_service.last_saved_path or cad_service._resolve_dwg_output_path(file_path)
                return [types.TextContent(
                    type="text",
                    text=json.dumps({
                        "success": result,
                        "file_path": final_file_path
                    }, ensure_ascii=False)
                )]

            elif name == "add_dimension":
                dimension_type = arguments.get("dimension_type", "aligned")
                start_point = arguments.get("start_point")
                end_point = arguments.get("end_point")
                text_position = arguments.get("text_position")
                source_view = arguments.get("source_view") or "front"
                center = arguments.get("center")
                chord_point = arguments.get("chord_point")
                far_chord_point = arguments.get("far_chord_point")
                leader_length = arguments.get("leader_length")
                layer = arguments.get("layer")
                color = arguments.get("color")
                textheight = arguments.get("textheight")                

                # 尝试从命令中提取颜色名称并转换为RGB值
                color_rgb = cad_service.nlp_processor.extract_color_from_command(color)
                if color_rgb is not None:
                    color = color_rgb  # 优先使用从命令中提取的颜色                

                # 详细检查每个参数并提供具体的错误信息
                error_msgs = []
                if dimension_type in {"aligned", "horizontal", "vertical"}:
                    if not start_point:
                        error_msgs.append("起点坐标")
                    if not end_point:
                        error_msgs.append("终点坐标")
                elif dimension_type == "diameter":
                    if not (chord_point or start_point):
                        error_msgs.append("圆周点1")
                    if not (far_chord_point or end_point):
                        error_msgs.append("圆周点2")
                elif dimension_type == "radius":
                    if not (center or start_point):
                        error_msgs.append("圆心")
                    if not (chord_point or end_point):
                        error_msgs.append("圆周点")                

                if error_msgs:
                    raise ValueError(f"缺少必要参数: {', '.join(error_msgs)}")                

                result = cad_service.add_dimension(
                    start_point=start_point,
                    end_point=end_point,
                    text_position=text_position,
                    textheight=textheight,
                    layer=layer,
                    color=color,
                    dimension_type=dimension_type,
                    center=center,
                    chord_point=chord_point,
                    far_chord_point=far_chord_point,
                    leader_length=leader_length,
                    source_view=source_view,
                )
                return [types.TextContent(type="text", text=str(result))]

            elif name == "apply_dimensions":
                dimensions = arguments.get("dimensions")
                if not isinstance(dimensions, list):
                    raise ValueError("dimensions 必须是数组")
                result = cad_service.apply_dimensions(dimensions)
                return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

            elif name == "process_command":
                command = arguments.get("command")                

                if not command:
                    raise ValueError("缺少命令")                

                result = cad_service.process_command(command)
                return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            elif name == "parse_drawing_document":
                file_path = arguments.get("file_path")
                drawing_type = arguments.get("drawing_type", "isometric")
                page_number = int(arguments.get("page_number", 1) or 1)
                recognized_data = arguments.get("recognized_data")
                review_overrides = arguments.get("review_overrides")
                focus_hint = arguments.get("focus_hint")
                if not file_path:
                    raise ValueError("缺少 file_path")
                result = cad_service.parse_drawing_document(
                    file_path=file_path,
                    drawing_type=drawing_type,
                    page_number=page_number,
                    recognized_data=recognized_data,
                    review_overrides=review_overrides,
                    focus_hint=focus_hint,
                )
                return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            elif name == "analyze_document_visual":
                file_path = arguments.get("file_path")
                page_number = int(arguments.get("page_number", 1) or 1)
                analyze_all_pages = bool(arguments.get("analyze_all_pages", False))
                max_pages = int(arguments.get("max_pages", 5) or 5)
                dpi = int(arguments.get("dpi", 300) or 300)
                image_format = str(arguments.get("image_format", "png") or "png")
                prompt = arguments.get("prompt")
                focus_hint = arguments.get("focus_hint")
                if not file_path:
                    raise ValueError("缺少 file_path")
                result = cad_service.analyze_document_visual(
                    file_path=file_path,
                    page_number=page_number,
                    analyze_all_pages=analyze_all_pages,
                    max_pages=max_pages,
                    dpi=dpi,
                    image_format=image_format,
                    prompt=prompt,
                    focus_hint=focus_hint,
                )
                return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            elif name == "apply_pipeline_review":
                semantic_model = arguments.get("semantic_model")
                resolutions = arguments.get("resolutions")
                if not semantic_model:
                    raise ValueError("缺少 semantic_model")
                if not isinstance(resolutions, dict):
                    raise ValueError("resolutions 必须是对象")
                result = cad_service.apply_pipeline_review(semantic_model, resolutions)
                return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            elif name == "draw_pipeline_from_json":
                semantic_model = arguments.get("semantic_model")
                require_review = bool(arguments.get("require_review", True))
                zoom_after_draw = bool(arguments.get("zoom_after_draw", True))
                if not semantic_model:
                    raise ValueError("缺少 semantic_model")
                result = cad_service.draw_pipeline_from_json(
                    semantic_model=semantic_model,
                    require_review=require_review,
                    zoom_after_draw=zoom_after_draw,
                )
                return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            elif name == "get_latest_vision_feedback":
                include_schema = bool(arguments.get("include_schema", False))
                result = cad_service.get_latest_vision_feedback(include_schema=include_schema)
                return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            elif name == "analyze_current_view":
                prompt = arguments.get("prompt")
                focus_hint = arguments.get("focus_hint")
                force = bool(arguments.get("force", False))
                include_schema = bool(arguments.get("include_schema", False))
                result = cad_service.analyze_current_view(
                    prompt=prompt,
                    focus_hint=focus_hint,
                    force=force,
                    include_schema=include_schema
                )
                return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            elif name == "set_vision_loop_enabled":
                if "enabled" not in arguments:
                    raise ValueError("缺少 enabled")
                enabled = arguments.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled 必须是布尔值")
                reason = arguments.get("reason")
                include_schema = bool(arguments.get("include_schema", False))
                result = cad_service.set_vision_loop_enabled(
                    enabled=enabled,
                    reason=reason,
                    include_schema=include_schema
                )
                return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            else:
                raise ValueError(f"未知工具: {name}")

        except Exception as e:
            return [types.TextContent(type="text", text=f"错误: {str(e)}")]

    @server.list_prompts()
    async def handle_list_prompts() -> list[types.Prompt]:
        logger.debug("处理 list_prompts 请求")
        return [
            types.Prompt(
                name="cad-assistant",
                description="一个用于通过自然语言控制CAD的助手",
                arguments=[],
            )
        ]

    @server.get_prompt()
    async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
        logger.debug(f"处理 get_prompt 请求，名称: {name}，参数: {arguments}")
        if name != "cad-assistant":
            logger.error(f"未知的提示: {name}")
            raise ValueError(f"未知的提示: {name}")

        prompt = PROMPT_TEMPLATE
        logger.debug("生成提示模板")

        return types.GetPromptResult(
            description="CAD助手提示模板",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=prompt.strip()),
                )
            ],
        )

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        logger.info("服务器正在使用 stdio 传输运行")
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=config.server_name,
                server_version=config.server_version,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())