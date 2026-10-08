import hashlib
import logging
import os
import subprocess
import tempfile
import time
from ctypes import windll
from typing import Any, Dict, List, Optional

import win32con
import win32gui
import win32ui
from PIL import Image

logger = logging.getLogger("screen_capture")


CAD_WINDOW_KEYWORDS = {
    "AUTOCAD": ["autocad"],
    "GCAD": ["浩辰", "gstarcad", "gcad"],
    "GSTARCAD": ["浩辰", "gstarcad", "gcad"],
    "ZWCAD": ["中望", "zwcad"],
}


class ScreenCapture:
    """负责定位 CAD 主窗口并截取窗口画面。"""

    def __init__(self, cad_type: str = "AUTOCAD", output_dir: Optional[str] = None):
        self.cad_type = (cad_type or "AUTOCAD").upper()
        self.output_dir = output_dir or os.path.join(tempfile.gettempdir(), "cad_mcp_vision")
        os.makedirs(self.output_dir, exist_ok=True)

    def locate_cad_window(
        self,
        document_name: Optional[str] = None,
        title_hint: Optional[str] = None,
        preferred_hwnd: Optional[int] = None,
        document_hwnd: Optional[int] = None,
    ) -> Dict[str, Any]:
        """按标题关键字和前台优先级定位 CAD 窗口。"""
        direct_window = self._resolve_preferred_window(preferred_hwnd, document_hwnd)
        if direct_window is not None:
            return direct_window

        keywords = CAD_WINDOW_KEYWORDS.get(self.cad_type, [self.cad_type.lower()])
        foreground_hwnd = win32gui.GetForegroundWindow()
        candidates: List[Dict[str, Any]] = []

        def _enum_windows(hwnd: int, _: Any) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return

            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return

            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            except Exception:
                return

            width = right - left
            height = bottom - top
            if width < 300 or height < 200:
                return

            score = self._score_window(
                title=title,
                keywords=keywords,
                document_name=document_name,
                title_hint=title_hint,
                is_foreground=hwnd == foreground_hwnd,
            )
            if score <= 0:
                return

            candidates.append(
                {
                    "hwnd": hwnd,
                    "title": title,
                    "score": score,
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                    "width": width,
                    "height": height,
                    "class_name": win32gui.GetClassName(hwnd),
                    "selection_strategy": "title_match",
                }
            )

        win32gui.EnumWindows(_enum_windows, None)
        if not candidates:
            raise RuntimeError("未找到可见的 CAD 窗口，请先确保 CAD 已打开且窗口未最小化。")

        candidates.sort(key=lambda item: (item["score"], item["width"] * item["height"]), reverse=True)
        return candidates[0]

    def capture_cad_window(
        self,
        document_name: Optional[str] = None,
        title_hint: Optional[str] = None,
        preferred_hwnd: Optional[int] = None,
        document_hwnd: Optional[int] = None,
    ) -> Dict[str, Any]:
        """截取 CAD 主窗口并保存为 PNG。"""
        window = self.locate_cad_window(
            document_name=document_name,
            title_hint=title_hint,
            preferred_hwnd=preferred_hwnd,
            document_hwnd=document_hwnd,
        )
        hwnd = window["hwnd"]

        try:
            win32gui.ShowWindow(hwnd, 9)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.2)
        except Exception:
            logger.debug("置顶 CAD 窗口失败，继续尝试截图。", exc_info=True)

        try:
            self._move_cursor_out_of_model_area(window)
            time.sleep(0.3)
        except Exception:
            logger.debug("截图前移动鼠标失败，继续尝试截图。", exc_info=True)

        file_name = f"cad_capture_{int(time.time() * 1000)}.png"
        output_path = os.path.join(self.output_dir, file_name)
        captured = False
        try:
            captured = self._capture_window_via_printwindow(hwnd, window["width"], window["height"], output_path)
        except Exception:
            logger.debug("PrintWindow 截图失败，将回退到屏幕区域截图。", exc_info=True)
        if not captured:
            self._capture_region_via_powershell(
                left=window["left"],
                top=window["top"],
                width=window["width"],
                height=window["height"],
                output_path=output_path,
            )

        with open(output_path, "rb") as file_obj:
            image_bytes = file_obj.read()

        return {
            "image_path": output_path,
            "image_bytes": image_bytes,
            "frame_hash": hashlib.sha256(image_bytes).hexdigest(),
            "window_title": window["title"],
            "window_class": window.get("class_name"),
            "window_hwnd": window["hwnd"],
            "captured_at": self._utc_now_iso(),
            "bounds": {
                "left": window["left"],
                "top": window["top"],
                "right": window["right"],
                "bottom": window["bottom"],
                "width": window["width"],
                "height": window["height"],
            },
            "mime_type": "image/png",
        }

    def _move_cursor_out_of_model_area(self, window: Dict[str, Any]) -> None:
        target_x = max(0, int(window["left"]) + 120)
        target_y = max(0, int(window["top"]) + 90)
        windll.user32.SetCursorPos(target_x, target_y)

    def _resolve_preferred_window(
        self,
        preferred_hwnd: Optional[int],
        document_hwnd: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        for candidate_hwnd in (preferred_hwnd, document_hwnd):
            if not candidate_hwnd:
                continue

            try:
                hwnd = int(candidate_hwnd)
                if not win32gui.IsWindow(hwnd):
                    continue

                root_hwnd = win32gui.GetAncestor(hwnd, 2)
                target_hwnd = root_hwnd or hwnd
                if not win32gui.IsWindowVisible(target_hwnd):
                    try:
                        win32gui.ShowWindow(target_hwnd, 9)
                        time.sleep(0.2)
                    except Exception:
                        continue

                left, top, right, bottom = win32gui.GetWindowRect(target_hwnd)
                width = right - left
                height = bottom - top
                class_name = win32gui.GetClassName(target_hwnd)
                if width < 300 or height < 200:
                    try:
                        win32gui.ShowWindow(target_hwnd, 9)
                        time.sleep(0.2)
                        left, top, right, bottom = win32gui.GetWindowRect(target_hwnd)
                        width = right - left
                        height = bottom - top
                    except Exception:
                        pass
                if width < 300 or height < 200:
                    continue
                if class_name == "AcTaskBarWin":
                    continue

                return {
                    "hwnd": target_hwnd,
                    "title": win32gui.GetWindowText(target_hwnd).strip(),
                    "score": 9999,
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                    "width": width,
                    "height": height,
                    "class_name": class_name,
                    "selection_strategy": "preferred_hwnd",
                }
            except Exception:
                logger.debug("通过首选句柄定位 CAD 窗口失败。", exc_info=True)

        return None

    def _score_window(
        self,
        title: str,
        keywords: List[str],
        document_name: Optional[str],
        title_hint: Optional[str],
        is_foreground: bool,
    ) -> int:
        normalized_title = title.lower()
        score = 0

        for keyword in keywords:
            if keyword.lower() in normalized_title:
                score += 80

        if document_name:
            doc_name = document_name.lower()
            if doc_name in normalized_title:
                score += 60

        if title_hint and title_hint.lower() in normalized_title:
            score += 40

        if is_foreground:
            score += 20

        return score

    def _capture_region_via_powershell(
        self,
        left: int,
        top: int,
        width: int,
        height: int,
        output_path: str,
    ) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("截图区域无效，宽高必须大于 0。")

        escaped_path = output_path.replace("'", "''")
        script = f"""
Add-Type -AssemblyName System.Drawing
$bitmap = New-Object System.Drawing.Bitmap({width}, {height})
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen({left}, {top}, 0, 0, $bitmap.Size)
$bitmap.Save('{escaped_path}', [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()
"""
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"PowerShell 截图失败: {completed.stderr.strip() or completed.stdout.strip() or '未知错误'}"
            )

    def _capture_window_via_printwindow(
        self,
        hwnd: int,
        width: int,
        height: int,
        output_path: str,
    ) -> bool:
        """Try to capture the actual window contents even when the window is occluded."""

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        if not hwnd_dc:
            return False

        mfc_dc = None
        save_dc = None
        bitmap = None
        try:
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
            save_dc.SelectObject(bitmap)

            result = win32gui.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
            if result != 1:
                return False

            bitmap_info = bitmap.GetInfo()
            bitmap_bits = bitmap.GetBitmapBits(True)
            image = Image.frombuffer(
                "RGB",
                (bitmap_info["bmWidth"], bitmap_info["bmHeight"]),
                bitmap_bits,
                "raw",
                "BGRX",
                0,
                1,
            )
            image.save(output_path)
            return True
        finally:
            if bitmap is not None:
                win32gui.DeleteObject(bitmap.GetHandle())
            if save_dc is not None:
                save_dc.DeleteDC()
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)

    def _utc_now_iso(self) -> str:
        """返回 UTC ISO 8601 字符串。"""
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
