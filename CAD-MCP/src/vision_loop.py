import copy
import logging
import threading
import time
from typing import Any, Dict, Optional

from screen_capture import ScreenCapture
from vision_provider import create_vision_provider

logger = logging.getLogger("vision_loop")


class VisionLoop:
    """最小视觉闭环：窗口截图、视觉分析、状态缓存与低频轮询。"""

    def __init__(self, cad_controller: Any, vision_config: Optional[Dict[str, Any]] = None):
        self.cad_controller = cad_controller
        self.vision_config = vision_config or {}
        self.poll_interval_ms = int(self.vision_config.get("poll_interval_ms", 2000))
        self.diff_threshold = float(self.vision_config.get("diff_threshold", 0.0))
        self.enabled = bool(self.vision_config.get("enabled", False))
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker_thread: Optional[threading.Thread] = None
        self.last_frame_hash: Optional[str] = None
        self.last_view_hint: Dict[str, Any] = {}
        self.screen_capture = ScreenCapture(
            cad_type=self.vision_config.get("cad_type", "AUTOCAD"),
            output_dir=self.vision_config.get("capture_output_dir"),
        )
        self.provider = create_vision_provider(self.vision_config)
        self.latest_feedback = self._build_feedback(
            status="idle",
            summary="视觉模块已初始化，等待首次截图分析。",
            suggested_action="可调用 analyze_current_view 主动发起一次视觉分析。",
        )

    def get_latest_feedback(self) -> Dict[str, Any]:
        """返回最近一次视觉反馈。"""
        with self.lock:
            payload = copy.deepcopy(self.latest_feedback)
            payload["loop_enabled"] = self.enabled
            return payload

    def set_enabled(self, enabled: bool, reason: Optional[str] = None) -> Dict[str, Any]:
        """启用或停用低频视觉轮询。"""
        with self.lock:
            self.enabled = enabled
            self.latest_feedback = self._build_feedback(
                status="idle",
                captured_at=self.latest_feedback.get("captured_at"),
                window_title=self.latest_feedback.get("window_title"),
                frame_hash=self.latest_feedback.get("frame_hash"),
                summary="视觉轮询已启用，后台将按间隔自动分析 CAD 窗口。"
                if enabled
                else "视觉轮询已停用，不会再自动分析新画面。",
                findings=self.latest_feedback.get("findings", []),
                suggested_action="可等待自动更新，或手动调用 analyze_current_view。"
                if enabled
                else "如需恢复自动分析，请再次启用 set_vision_loop_enabled。",
                confidence=self.latest_feedback.get("confidence"),
                view_semantics=self.latest_feedback.get("view_semantics"),
                provider=self.latest_feedback.get("provider"),
                model=self.latest_feedback.get("model"),
                latency_ms=self.latest_feedback.get("latency_ms"),
                loop_enabled=enabled,
                control_reason=reason,
                image_path=self.latest_feedback.get("image_path"),
            )

        if enabled:
            self._ensure_worker_started()

        return self.get_latest_feedback()

    def notify_view_changed(self, source: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """记录 CAD 视图变更提示，供下一轮分析参考。"""
        with self.lock:
            self.last_view_hint = {
                "source": source,
                "payload": payload or {},
                "updated_at": self._utc_now_iso(),
            }

    def analyze_current_view(
        self,
        prompt: Optional[str] = None,
        focus_hint: Optional[str] = None,
        force: bool = False,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """同步执行一次截图与视觉分析。"""
        started_at = time.perf_counter()
        effective_force = force or bool(prompt) or bool(focus_hint)

        try:
            if not self.cad_controller.is_running():
                started = self.cad_controller.start_cad()
                if not started:
                    raise RuntimeError("CAD 尚未启动，无法截图。")

            window_context = self._get_window_context()
            capture_result = self.screen_capture.capture_cad_window(
                document_name=window_context.get("document_name"),
                title_hint=window_context.get("title_hint"),
                preferred_hwnd=window_context.get("application_hwnd"),
                document_hwnd=window_context.get("document_hwnd"),
            )

            if (
                not effective_force
                and self.last_frame_hash
                and capture_result["frame_hash"] == self.last_frame_hash
                and self.latest_feedback.get("status") == "success"
            ):
                reused_feedback = self.get_latest_feedback()
                reused_feedback["summary"] = "画面与上一帧一致，复用最近一次视觉分析结果。"
                reused_feedback["captured_at"] = capture_result["captured_at"]
                reused_feedback["frame_hash"] = capture_result["frame_hash"]
                reused_feedback["window_title"] = capture_result["window_title"]
                reused_feedback["image_path"] = capture_result["image_path"]
                reused_feedback["job_id"] = job_id
                with self.lock:
                    self.latest_feedback = copy.deepcopy(reused_feedback)
                return reused_feedback

            provider_result = self.provider.analyze_image(
                image_bytes=capture_result["image_bytes"],
                prompt=prompt,
                focus_hint=focus_hint,
            )
            latency_ms = round((time.perf_counter() - started_at) * 1000, 2)

            feedback = self._build_feedback(
                status="success",
                captured_at=capture_result["captured_at"],
                window_title=capture_result["window_title"],
                frame_hash=capture_result["frame_hash"],
                summary=provider_result.get("summary", "视觉分析已完成。"),
                findings=provider_result.get("findings", []),
                suggested_action=provider_result.get("suggested_action", ""),
                confidence=provider_result.get("confidence"),
                view_semantics=provider_result.get("view_semantics"),
                provider=provider_result.get("provider"),
                model=provider_result.get("model"),
                latency_ms=latency_ms,
                job_id=job_id,
                loop_enabled=self.enabled,
                image_path=capture_result["image_path"],
                bounds=capture_result.get("bounds"),
                view_hint=copy.deepcopy(self.last_view_hint),
                raw_response_text=provider_result.get("raw_response_text"),
            )
            with self.lock:
                self.latest_feedback = feedback
                self.last_frame_hash = capture_result["frame_hash"]
            return copy.deepcopy(feedback)
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
            error_feedback = self._build_feedback(
                status="error",
                summary="视觉分析失败。",
                findings=[
                    {
                        "label": "vision-error",
                        "detail": str(exc),
                        "severity": "warning",
                    }
                ],
                suggested_action="请检查 CAD 窗口是否可见、截图权限以及视觉 API 配置。",
                confidence=None,
                provider=self.latest_feedback.get("provider"),
                model=self.latest_feedback.get("model"),
                latency_ms=latency_ms,
                job_id=job_id,
                loop_enabled=self.enabled,
                error_message=str(exc),
            )
            with self.lock:
                self.latest_feedback = error_feedback
            logger.exception("视觉分析失败")
            return copy.deepcopy(error_feedback)

    def close(self) -> None:
        """停止后台轮询。"""
        self.stop_event.set()
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=1.0)

    def _ensure_worker_started(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return

        self.stop_event.clear()
        self.worker_thread = threading.Thread(
            target=self._worker_loop,
            name="cad-vision-loop",
            daemon=True,
        )
        self.worker_thread.start()

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.enabled:
                self.analyze_current_view(force=False)
            sleep_seconds = max(self.poll_interval_ms / 1000.0, 0.5)
            self.stop_event.wait(timeout=sleep_seconds)

    def _get_window_context(self) -> Dict[str, Optional[str]]:
        document_name = None
        title_hint = None
        try:
            document_name = self.cad_controller.get_document_name()
        except Exception:
            logger.debug("获取 CAD 文档名称失败。", exc_info=True)

        try:
            title_hint = self.cad_controller.get_application_caption()
        except Exception:
            logger.debug("获取 CAD 应用标题失败。", exc_info=True)

        return {
            "document_name": document_name,
            "title_hint": title_hint,
            "application_hwnd": self.cad_controller.get_application_hwnd(),
            "document_hwnd": self.cad_controller.get_document_hwnd(),
        }

    def _build_feedback(
        self,
        *,
        status: str,
        captured_at: Optional[str] = None,
        window_title: Optional[str] = None,
        frame_hash: Optional[str] = None,
        summary: str,
        findings: Optional[list[Dict[str, Any]]] = None,
        suggested_action: str,
        confidence: Optional[float] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        latency_ms: Optional[float] = None,
        job_id: Optional[str] = None,
        loop_enabled: bool = False,
        **extra: Any,
    ) -> Dict[str, Any]:
        payload = {
            "status": status,
            "captured_at": captured_at,
            "window_title": window_title,
            "frame_hash": frame_hash,
            "summary": summary,
            "findings": findings or [],
            "suggested_action": suggested_action,
            "confidence": confidence,
            "provider": provider,
            "model": model,
            "latency_ms": latency_ms,
            "job_id": job_id,
            "loop_enabled": loop_enabled,
        }
        payload.update(extra)
        return payload

    def _utc_now_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
