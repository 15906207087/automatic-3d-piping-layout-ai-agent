import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("vision_provider")


DEFAULT_ANALYSIS_PROMPT = """
你是 CAD 视觉分析助手。请基于给定的 CAD 窗口截图输出 JSON，禁止输出 JSON 之外的其他文字。

返回结构：
{
  "summary": "一句话总结当前画面",
  "findings": [
    {
      "label": "对象/风险/线索名称",
      "detail": "具体观察",
      "severity": "info|warning|critical"
    }
  ],
  "suggested_action": "建议下一步动作",
  "confidence": 0.0,
  "view_semantics": {
    "is_three_view": false,
    "mapping_rule": "前视图=主视图，俯视图=top view，左视图=left view",
    "detected_views": [
      {
        "view_label": "前视图",
        "canonical_view": "front",
        "surface_label": "前面",
        "confidence": 0.0
      }
    ]
  }
}

要求：
1. 如果无法确定细节，也要诚实说明不确定性。
2. `findings` 为空时返回 []。
3. `confidence` 范围是 0 到 1。
4. 遇到“三视图”或工程图视图语义时，固定采用以下约定：前视图 = 主视图，俯视图 = top view，左视图 = left view。
5. 如果画面中同时出现前视图、俯视图、左视图，必须按上述约定识别对应面，不能自行改判为其他面。
""".strip()


def _default_view_semantics() -> Dict[str, Any]:
    """返回统一的视图语义默认结构。"""
    return {
        "is_three_view": False,
        "mapping_rule": "前视图=主视图，俯视图=top view，左视图=left view",
        "detected_views": [],
    }


def _normalize_view_semantics(payload: Any) -> Dict[str, Any]:
    """规范化视觉结果中的视图语义结构。"""
    normalized = _default_view_semantics()
    if not isinstance(payload, dict):
        return normalized

    detected_views = payload.get("detected_views")
    normalized_detected_views = []
    if isinstance(detected_views, list):
        for item in detected_views:
            if not isinstance(item, dict):
                continue
            normalized_detected_views.append(
                {
                    "view_label": str(item.get("view_label", "")),
                    "canonical_view": str(item.get("canonical_view", "")),
                    "surface_label": str(item.get("surface_label", "")),
                    "confidence": item.get("confidence"),
                }
            )

    normalized["is_three_view"] = bool(payload.get("is_three_view", normalized["is_three_view"]))
    normalized["mapping_rule"] = str(payload.get("mapping_rule", normalized["mapping_rule"]))
    normalized["detected_views"] = normalized_detected_views
    return normalized


class VisionProvider:
    """视觉提供方抽象基类。"""

    def analyze_image(
        self,
        image_bytes: bytes,
        prompt: Optional[str] = None,
        focus_hint: Optional[str] = None,
        system_prompt: Optional[str] = None,
        mime_type: str = "image/png",
    ) -> Dict[str, Any]:
        raise NotImplementedError


class OpenAICompatibleVisionProvider(VisionProvider):
    """兼容 OpenAI Chat Completions 格式的视觉提供方。"""

    def __init__(self, config: Dict[str, Any]):
        self.provider_name = str(config.get("provider", "qwen")).lower()
        default_model = "qwen-vl-max-latest"
        default_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        default_api_key_env = "DASHSCOPE_API_KEY"
        if self.provider_name == "openai":
            default_model = "gpt-5.4"
            default_base_url = "https://api.openai.com/v1/chat/completions"
            default_api_key_env = "OPENAI_API_KEY"
        elif self.provider_name == "openrouter":
            default_model = "openai/gpt-5.4"
            default_base_url = "https://openrouter.ai/api/v1/chat/completions"
            default_api_key_env = "OPENROUTER_API_KEY"

        self.model = config.get("model", default_model)
        self.base_url = config.get("base_url", default_base_url)
        self.api_key_env = config.get("api_key_env", default_api_key_env)
        self.timeout_sec = int(config.get("timeout_sec", 60))
        self.app_name = config.get("app_name", "CAD-MCP")
        self.site_url = config.get("site_url")

    def analyze_image(
        self,
        image_bytes: bytes,
        prompt: Optional[str] = None,
        focus_hint: Optional[str] = None,
        system_prompt: Optional[str] = None,
        mime_type: str = "image/png",
    ) -> Dict[str, Any]:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"未找到视觉 API Key，请设置环境变量 `{self.api_key_env}`。")

        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        normalized_mime_type = mime_type.strip().lower() if mime_type else "image/png"
        user_prompt = prompt.strip() if prompt else "请分析当前 CAD 画面。"
        if focus_hint:
            user_prompt = f"{user_prompt}\n关注提示：{focus_hint}"

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": [{"type": "text", "text": (system_prompt or DEFAULT_ANALYSIS_PROMPT)}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{normalized_mime_type};base64,{image_base64}"},
                        },
                    ],
                },
            ],
            "temperature": 0.1,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        if self.provider_name == "openrouter":
            headers["HTTP-Referer"] = self.site_url or "https://cursor.local"
            headers["X-Title"] = self.app_name

        request = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"视觉请求失败: HTTP {exc.code} {error_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"视觉请求失败: {exc.reason}") from exc

        message_content = self._extract_message_content(response_data)
        parsed_payload = self._parse_json_payload(message_content)
        parsed_payload["provider"] = self.provider_name
        parsed_payload["model"] = self.model
        parsed_payload["raw_response_text"] = message_content
        return parsed_payload

    def _extract_message_content(self, response_data: Dict[str, Any]) -> str:
        try:
            content = response_data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise RuntimeError(f"视觉模型返回结构异常: {response_data}") from exc

        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(part for part in parts if part).strip()

        if isinstance(content, str):
            return content.strip()

        raise RuntimeError(f"无法解析视觉模型返回内容: {content}")

    def _parse_json_payload(self, content: str) -> Dict[str, Any]:
        stripped = content.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
            stripped = re.sub(r"```$", "", stripped).strip()

        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
            if not match:
                logger.warning("视觉模型未返回 JSON，降级为文本摘要。")
                return {
                    "summary": stripped or "视觉模型未返回可解析内容。",
                    "findings": [],
                    "suggested_action": "请检查提示词或模型输出格式。",
                    "confidence": None,
                    "view_semantics": _default_view_semantics(),
                }
            payload = json.loads(match.group(0))

        normalized_payload = {
            "summary": payload.get("summary", "视觉分析已完成，但模型未提供 summary。"),
            "findings": payload.get("findings", []),
            "suggested_action": payload.get("suggested_action", ""),
            "confidence": payload.get("confidence"),
            "view_semantics": _normalize_view_semantics(payload.get("view_semantics")),
        }
        normalized_payload.update(payload)
        normalized_payload["view_semantics"] = _normalize_view_semantics(normalized_payload.get("view_semantics"))
        return normalized_payload


class MockVisionProvider(VisionProvider):
    """本地占位 provider，便于未配置云端接口时调通链路。"""

    def analyze_image(
        self,
        image_bytes: bytes,
        prompt: Optional[str] = None,
        focus_hint: Optional[str] = None,
        system_prompt: Optional[str] = None,
        mime_type: str = "image/png",
    ) -> Dict[str, Any]:
        _ = image_bytes
        _ = system_prompt
        _ = mime_type
        summary = "已成功截取 CAD 窗口，但当前使用 mock provider，未做真实视觉识别。"
        if prompt:
            summary = f"{summary} 请求提示：{prompt}"
        if focus_hint:
            summary = f"{summary} 关注提示：{focus_hint}"

        return {
            "summary": summary,
            "findings": [
                {
                    "label": "mock-provider",
                    "detail": "截图链路已打通，等待切换到真实视觉模型。",
                    "severity": "info",
                }
            ],
            "suggested_action": "请在 config.json 中配置真实视觉 provider 与 API Key 环境变量。",
            "confidence": 0.1,
            "view_semantics": _default_view_semantics(),
            "provider": "mock",
            "model": "mock-vision",
            "raw_response_text": summary,
        }


def create_vision_provider(config: Optional[Dict[str, Any]]) -> VisionProvider:
    """按配置创建视觉 provider。"""
    config = config or {}
    provider_name = str(config.get("provider", "mock")).lower()
    if provider_name in {"qwen", "qwen-compatible", "dashscope", "openai", "openrouter"}:
        return OpenAICompatibleVisionProvider(config)
    if provider_name == "mock":
        return MockVisionProvider()
    raise ValueError(f"不支持的视觉 provider: {provider_name}")
