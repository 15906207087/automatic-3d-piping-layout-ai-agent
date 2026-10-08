import logging
import math
import time
import os
import json
from typing import Any, Dict, List, Optional, Tuple, Union

# 直接读取config.json文件
config_path = os.path.join(os.path.dirname(__file__), 'config.json')
with open(config_path, 'r', encoding='utf-8') as f:
    config = json.load(f)

try:
    import win32com.client
    # pythoncom是pywin32的一部分，不需要单独安装
    import pythoncom
except ImportError:
    logging.error("无法导入win32com.client或pythoncom，请确保已安装pywin32库")
    raise

logger = logging.getLogger('cad_controller')

class CADController:
    """CAD控制器类，负责与CAD应用程序交互"""

    ORTHOGRAPHIC_VIEW_ALIASES = {
        "front": "front",
        "frontview": "front",
        "front view": "front",
        "main": "front",
        "mainview": "front",
        "main view": "front",
        "elevation": "front",
        "前视图": "front",
        "主视图": "front",
        "三视图主视图": "front",
        "top": "top",
        "topview": "top",
        "top view": "top",
        "plan": "top",
        "planview": "top",
        "plan view": "top",
        "俯视图": "top",
        "顶视图": "top",
        "上视图": "top",
        "left": "left",
        "leftview": "left",
        "left view": "left",
        "leftside": "left",
        "left side": "left",
        "left elevation": "left",
        "左视图": "left",
        "左侧视图": "left",
        "right": "right",
        "rightview": "right",
        "right view": "right",
        "右视图": "right",
        "back": "back",
        "backview": "back",
        "back view": "back",
        "rear": "back",
        "rearview": "back",
        "rear view": "back",
        "后视图": "back",
        "bottom": "bottom",
        "bottomview": "bottom",
        "bottom view": "bottom",
        "底视图": "bottom",
        "下视图": "bottom",
    }
    
    def __init__(self):
        """初始化CAD控制器"""
        self.app = None
        self.doc = None
        self.entities = {}  # 存储已创建图形的实体引用，用于后续修改
        self.view_change_callback = None
        # 从配置文件加载参数
        self.startup_wait_time = config["cad"]["startup_wait_time"]
        self.command_delay = config["cad"]["command_delay"]
        # 获取CAD类型
        self.cad_type = config["cad"]["type"]
        # 有效的线宽值列表
        self.valid_lineweights = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211]
        logger.info("CAD控制器已初始化")
    
    def start_cad(self) -> bool:
        """启动CAD并创建或打开一个文档"""
        try:
            # 初始化COM
            pythoncom.CoInitialize()
            
            # 存储旧实例引用（如果有）以便后续清理
            old_app = None
            if self.app is not None:
                old_app = self.app
                self.app = None
                self.doc = None
            
            try:
                # 根据配置的CAD类型选择不同的应用程序标识符
                app_id = "AutoCAD.Application"
                app_name = "AutoCAD"
                
                if self.cad_type.lower() == "autocad":
                    app_id = "AutoCAD.Application"
                    app_name = "AutoCAD"
                elif self.cad_type.lower() == "gcad":
                    app_id = "GCAD.Application"
                    app_name = "浩辰CAD"
                elif self.cad_type.lower() == "gstarcad":
                    app_id = "GCAD.Application"
                    app_name = "浩辰CAD"
                elif self.cad_type.lower() == "zwcad":
                    app_id = "ZWCAD.Application"
                    app_name = "中望CAD"
                
                # 尝试连接到已运行的CAD实例（使用 dynamic 避免部分国产 CAD 的接口差异）
                logger.info(f"尝试连接现有{app_name}实例...")
                try:
                    self.app = win32com.client.GetActiveObject(app_id)
                    logger.info(f"成功连接到已运行的{app_name}实例")
                except Exception as e:
                    logger.info(f"未找到运行中的{app_name}实例，将尝试启动新实例: {str(e)}")
                    raise

                # 获取或创建文档（兼容 AutoCAD / 浩辰 / 中望，避免依赖 Documents.Count）
                self.doc = None
                for _ in range(3):
                    try:
                        self.doc = self.app.ActiveDocument
                        logger.info("获取活动文档...")
                        break
                    except Exception as ad_ex:
                        logger.debug(f"ActiveDocument 获取失败 (重试中): {ad_ex}")
                        time.sleep(1)
                if self.doc is None:
                    try:
                        docs = getattr(self.app, "Documents", None)
                        if docs is not None:
                            docs.Add()
                            logger.info("创建新文档...")
                            time.sleep(1)
                            for _ in range(5):
                                try:
                                    self.doc = self.app.ActiveDocument
                                    if self.doc is not None:
                                        break
                                except Exception:
                                    time.sleep(1)
                    except Exception as doc_ex:
                        logger.warning(f"通过 Documents.Add 创建文档失败: {doc_ex}")
                if self.doc is None:
                    raise RuntimeError("无法获取文档，请确保 CAD 已打开至少一张图纸")
                    
            except Exception as app_ex:
                # 连接或获取文档失败，尝试重试或启动新实例
                logger.info(f"连接/文档获取异常: {str(app_ex)}")
                app_id = "AutoCAD.Application"
                app_name = "AutoCAD"
                if self.cad_type.lower() == "autocad":
                    app_id, app_name = "AutoCAD.Application", "AutoCAD"
                elif self.cad_type.lower() in ("gcad", "gstarcad"):
                    app_id, app_name = "GCAD.Application", "浩辰CAD"
                elif self.cad_type.lower() == "zwcad":
                    app_id, app_name = "ZWCAD.Application", "中望CAD"

                # 若已有 app，先重试获取 ActiveDocument（部分 CAD 需等待）
                if self.app is not None:
                    for _ in range(3):
                        try:
                            self.doc = self.app.ActiveDocument
                            if self.doc is not None:
                                logger.info("已通过重试获取活动文档")
                                break
                        except Exception:
                            time.sleep(2)
                    if self.doc is None:
                        logger.info(f"正在启动新的{app_name}实例...")
                        self.app = win32com.client.DispatchEx(app_id)
                        self.app.Visible = True
                        time.sleep(self.startup_wait_time)
                        for _ in range(5):
                            try:
                                self.doc = self.app.ActiveDocument
                                break
                            except Exception:
                                time.sleep(2)
                else:
                    logger.info(f"正在启动{app_name}实例...")
                    self.app = win32com.client.DispatchEx(app_id)
                    self.app.Visible = True
                    time.sleep(self.startup_wait_time)
                    for _ in range(5):
                        try:
                            self.doc = self.app.ActiveDocument
                            break
                        except Exception:
                            time.sleep(2)
                if self.doc is None:
                    raise RuntimeError("无法获取活动文档，请确保 CAD 已打开至少一张图纸后重试")
            
            # 额外安全检查和等待
            time.sleep(2)  # 给CAD更多时间处理文档创建

            if self.doc is None:
                raise Exception("无法获取有效的Document对象")

            # 尝试读取文档属性以验证（部分国产 CAD 的 .Name 可能不可用，仅作日志）
            try:
                name = self.doc.Name
                logger.info(f"文档名称: {name}")
            except Exception as name_ex:
                logger.warning(f"无法读取文档名称（部分 CAD 版本可能不支持）: {name_ex}")
                # 不因 Name 失败而中断，doc 对象仍可能可用

            logger.info("CAD已成功启动和准备")
            return True
            
        except Exception as e:
            logger.error(f"启动CAD失败: {str(e)}")
            return False
        finally:
            # 清理旧实例
            if old_app is not None:
                try:
                    del old_app
                except:
                    pass
    
        
    def is_running(self) -> bool:
        """检查CAD是否正在运行"""
        return self.app is not None and self.doc is not None

    def set_view_change_callback(self, callback) -> None:
        """注册视图变化回调。"""
        self.view_change_callback = callback

    def get_document_name(self) -> Optional[str]:
        """返回当前文档名称。"""
        if not self.is_running():
            return None
        try:
            return str(self.doc.Name)
        except Exception:
            return None

    def get_application_caption(self) -> Optional[str]:
        """返回 CAD 应用窗口标题。"""
        if self.app is None:
            return None
        try:
            return str(self.app.Caption)
        except Exception:
            return None

    def get_application_hwnd(self) -> Optional[int]:
        """返回 CAD 主窗口句柄。"""
        if self.app is None:
            return None
        for attr_name in ("HWND", "Hwnd", "hWnd"):
            try:
                value = getattr(self.app, attr_name)
                if value:
                    return int(value)
            except Exception:
                continue
        return None

    def get_document_hwnd(self) -> Optional[int]:
        """返回当前文档窗口句柄。"""
        if not self.is_running():
            return None
        for attr_name in ("HWND", "Hwnd", "hWnd"):
            try:
                value = getattr(self.doc, attr_name)
                if value:
                    return int(value)
            except Exception:
                continue
        return None

    def get_window_context(self) -> Dict[str, Optional[str]]:
        """返回定位 CAD 窗口所需的上下文。"""
        return {
            "cad_type": self.cad_type,
            "document_name": self.get_document_name(),
            "application_caption": self.get_application_caption(),
            "application_hwnd": self.get_application_hwnd(),
            "document_hwnd": self.get_document_hwnd(),
        }

    def _is_retryable_com_error(self, exc: Exception) -> bool:
        """识别 AutoCAD 忙碌态下可重试的 COM 错误。"""
        if not hasattr(exc, "args") or not exc.args:
            return False
        return exc.args[0] in (-2147418111, -2147417846)

    def _call_with_retry(self, func, *args, retries: int = 20, delay: float = 0.3, **kwargs):
        """在 AutoCAD 临时拒绝调用时做短暂重试。"""
        last_error = None
        for attempt in range(retries):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                last_error = exc
                if not self._is_retryable_com_error(exc) or attempt == retries - 1:
                    raise
                time.sleep(delay)
                try:
                    pythoncom.PumpWaitingMessages()
                except Exception:
                    pass
        if last_error is not None:
            raise last_error
        return None

    def _get_model_space(self):
        """带重试获取 ModelSpace，避免启动后短时间内访问失败。"""
        if not self.is_running():
            return None
        try:
            return self._call_with_retry(lambda: self.doc.ModelSpace)
        except Exception:
            if self.app is not None:
                try:
                    self.doc = self.app.ActiveDocument
                    return self._call_with_retry(lambda: self.doc.ModelSpace)
                except Exception:
                    pass
            raise

    def _normalize_point3d(self, point: Tuple[float, ...]) -> Tuple[float, float, float]:
        """将输入点标准化为三维坐标。"""
        if point is None:
            return (0.0, 0.0, 0.0)
        if len(point) == 2:
            return (float(point[0]), float(point[1]), 0.0)
        return (float(point[0]), float(point[1]), float(point[2]))

    def _axis_dir(self, axis_signed_str: str, default: str = "z") -> Tuple[str, int]:
        """解析带正负号的轴向字符串。"""
        s = (axis_signed_str or default).lower().strip()
        if not s:
            s = default
        if s.startswith("-"):
            return s[1:], -1
        return s, 1

    def _flip_axis(self, axis_signed_str: str, default: str = "z") -> str:
        """返回相反方向的轴向字符串。"""
        s = (axis_signed_str or default).lower().strip()
        if not s:
            s = default
        return s[1:] if s.startswith("-") else f"-{s}"

    def _axis_unit(self, axis_signed_str: str, default: str = "z") -> Tuple[float, float, float]:
        """返回指定轴向的单位向量。"""
        axis_name, sign = self._axis_dir(axis_signed_str, default=default)
        if axis_name == "x":
            return (float(sign), 0.0, 0.0)
        if axis_name == "y":
            return (0.0, float(sign), 0.0)
        return (0.0, 0.0, float(sign))

    def _advance_point(self, point: Tuple[float, float, float], axis_signed_str: str, distance: float,
                       default: str = "z") -> Tuple[float, float, float]:
        """沿指定轴向平移点位。"""
        ux, uy, uz = self._axis_unit(axis_signed_str, default=default)
        return (
            float(point[0]) + ux * float(distance),
            float(point[1]) + uy * float(distance),
            float(point[2]) + uz * float(distance),
        )

    def _rotate_solid_from_z(self, solid: Any, rotate_center: Tuple[float, float, float], axis_signed_str: str, default: str = "z") -> None:
        """将沿 +Z 创建的实体旋转到指定轴向。"""
        axis_name, sign = self._axis_dir(axis_signed_str, default=default)
        if axis_name not in ("x", "y", "z"):
            return
        px, py, pz = self._normalize_point3d(rotate_center)
        pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz])
        if axis_name == "z":
            if sign < 0:
                pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1.0, py, pz])
                solid.Rotate3D(pt_center, pt_x, math.pi)
            return
        if axis_name == "x":
            pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
            angle = (math.pi / 2.0) if sign > 0 else (-math.pi / 2.0)
            solid.Rotate3D(pt_center, pt_y, angle)
            return
        pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1.0, py, pz])
        angle = (-math.pi / 2.0) if sign > 0 else (math.pi / 2.0)
        solid.Rotate3D(pt_center, pt_x, angle)

    def _rotate_solid_from_x(self, solid: Any, rotate_center: Tuple[float, float, float], axis_signed_str: str, default: str = "x") -> None:
        """将沿 +X 创建的实体旋转到指定轴向。"""
        axis_name, sign = self._axis_dir(axis_signed_str, default=default)
        if axis_name not in ("x", "y", "z"):
            return
        px, py, pz = self._normalize_point3d(rotate_center)
        pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz])
        if axis_name == "x":
            if sign < 0:
                pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
                solid.Rotate3D(pt_center, pt_y, math.pi)
            return
        if axis_name == "y":
            pt_z = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz + 1.0])
            angle = math.pi / 2.0 if sign > 0 else -math.pi / 2.0
            solid.Rotate3D(pt_center, pt_z, angle)
            return
        pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
        angle = math.pi / 2.0 if sign > 0 else -math.pi / 2.0
        solid.Rotate3D(pt_center, pt_y, angle)

    def _safe_delete_entity(self, entity: Any) -> None:
        """尽量删除临时实体，忽略清理异常。"""
        if entity is None:
            return
        try:
            entity.Delete()
        except Exception:
            pass

    def _add_oriented_cylinder(self, base_pt: Tuple[float, float, float], radius: float, height: float, axis_signed_str: str = "z") -> Any:
        """创建沿指定轴向的圆柱体，base_pt 为起始端面圆心。"""
        if radius <= 0 or height <= 0:
            return None
        base = self._normalize_point3d(base_pt)
        # ActiveX 的 AddCylinder 以实体几何中心为基准，而当前 helper 的对外语义是
        # “base_pt 为起始端面圆心”。因此这里需要先把底面中心换算到沿 +Z 创建时的
        # 实体几何中心，再围绕 base_pt 旋转到目标轴向，才能保证各调用方看到的仍是
        # 统一的“起始端面圆心 + 轴向长度”语义。
        creation_center = (base[0], base[1], base[2] + float(height) / 2.0)
        center_variant = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(creation_center))
        last_error = None
        for _ in range(4):
            try:
                model_space = self._get_model_space()
                if model_space is None:
                    time.sleep(0.4)
                    continue
                solid = self._call_with_retry(
                    model_space.AddCylinder,
                    center_variant,
                    float(radius),
                    float(height),
                )
                self._rotate_solid_from_z(solid, base, axis_signed_str)
                return solid
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)
        if last_error is not None:
            logger.error(f"_add_oriented_cylinder 失败: {last_error}")
        return None

    def _add_oriented_hollow_cylinder(
        self,
        base_pt: Tuple[float, float, float],
        outer_radius: float,
        height: float,
        axis_signed_str: str = "z",
        wall_thickness: Optional[float] = None,
        through_cut_extra: float = 1.0,
    ) -> Any:
        """创建沿指定轴向的空心圆管，base_pt 为起始端面圆心。"""
        if outer_radius <= 0 or height <= 0:
            return None
        if wall_thickness is None or wall_thickness <= 0:
            return self._add_oriented_cylinder(base_pt, outer_radius, height, axis_signed_str)
        inner_radius = float(outer_radius) - float(wall_thickness)
        if inner_radius <= 0:
            logger.warning("_add_oriented_hollow_cylinder: 壁厚 %.3f 过大，回退为实心圆柱", wall_thickness)
            return self._add_oriented_cylinder(base_pt, outer_radius, height, axis_signed_str)
        outer = self._add_oriented_cylinder(base_pt, outer_radius, height, axis_signed_str)
        if outer is None:
            return None
        inner_base = self._advance_point(
            self._normalize_point3d(base_pt),
            self._flip_axis(axis_signed_str),
            through_cut_extra,
            default=axis_signed_str.lstrip("-") or "z",
        )
        inner = self._add_oriented_cylinder(
            inner_base,
            inner_radius,
            float(height) + float(through_cut_extra) * 2.0,
            axis_signed_str,
        )
        if inner is None:
            self._safe_delete_entity(outer)
            return None
        if not self._solid_boolean_subtract(outer, inner):
            logger.warning("_add_oriented_hollow_cylinder: 内孔布尔减失败，可能仍为实心圆柱")
        self._safe_delete_entity(inner)
        return outer

    def _add_hex_prism(self, base_pt: Tuple[float, float, float], width_across_flats: float, height: float, axis_signed_str: str = "z") -> Any:
        """创建六角棱柱，base_pt 为一端面中心，width_across_flats 为对边宽。"""
        if width_across_flats <= 0 or height <= 0:
            return None
        model_space = self._get_model_space()
        if model_space is None:
            return None

        circum_radius = float(width_across_flats) / math.sqrt(3.0)
        points_2d = []
        for index in range(6):
            angle = math.radians(30.0 + index * 60.0)
            points_2d.append((circum_radius * math.cos(angle), circum_radius * math.sin(angle)))

        polyline = None
        region = None
        try:
            arr = []
            for px, py in points_2d:
                arr.extend([px, py])
            points_variant = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, arr)
            try:
                polyline = self._call_with_retry(model_space.AddLightWeightPolyline, points_variant)
            except Exception:
                arr3 = []
                for px, py in points_2d:
                    arr3.extend([px, py, 0.0])
                polyline = self._call_with_retry(
                    model_space.AddPolyline,
                    win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, arr3),
                )
            if polyline is None:
                return None
            polyline.Closed = True
            curves = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [polyline])
            regions = self._call_with_retry(model_space.AddRegion, curves)
            if regions is None or (hasattr(regions, "__len__") and len(regions) == 0):
                return None
            region = regions[0] if hasattr(regions, "__len__") else regions
            prism = self._call_with_retry(model_space.AddExtrudedSolid, region, float(height), 0.0)
            if prism is None:
                return None
            self._rotate_solid_from_z(prism, (0.0, 0.0, 0.0), axis_signed_str)
            base = self._normalize_point3d(base_pt)
            self._call_with_retry(
                prism.Move,
                win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 0.0, 0.0]),
                win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(base)),
            )
            return prism
        finally:
            self._safe_delete_entity(polyline)
            self._safe_delete_entity(region)

    def _notify_view_changed(self, source: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """通知外层当前视图可能已变化。"""
        if not callable(self.view_change_callback):
            return
        try:
            self.view_change_callback(source, payload or {})
        except Exception as exc:
            logger.warning(f"视图变化回调执行失败: {exc}")

    def _clear_selection(self) -> None:
        """尽量清除当前选中集，避免新建 3D 实体一直显示操控手柄。"""
        if not self.is_running():
            return
        for attr_name in ("ActiveSelectionSet", "PickfirstSelectionSet"):
            try:
                selection_set = getattr(self.doc, attr_name)
                self._call_with_retry(selection_set.Clear)
            except Exception:
                continue

    def _finalize_entity_display(self, entity: Any) -> None:
        """收尾实体显示状态，避免高亮或 gizmo 干扰视图核对。"""
        if entity is None:
            return
        try:
            self._call_with_retry(entity.Highlight, False)
        except Exception:
            pass
        self._clear_selection()

    def _get_entity_bounding_box(
        self, entity: Any
    ) -> Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float]]]:
        """读取实体包围盒。"""
        if entity is None:
            return None
        try:
            bbox = self._call_with_retry(entity.GetBoundingBox)
        except Exception as exc:
            logger.error(f"读取实体包围盒失败: {exc}")
            return None
        if not bbox or len(bbox) != 2:
            return None
        min_pt = tuple(float(value) for value in bbox[0])
        max_pt = tuple(float(value) for value in bbox[1])
        return min_pt, max_pt

    def fit_orthographic_view_to_entity(
        self,
        entity: Any,
        view_name: str,
        padding_ratio: float = 1.4,
        min_size: float = 50.0,
    ) -> bool:
        """按实体包围盒自动设置正投影视图范围。"""
        bbox = self._get_entity_bounding_box(entity)
        if bbox is None:
            return False

        min_pt, max_pt = bbox
        center_target = (
            (min_pt[0] + max_pt[0]) / 2.0,
            (min_pt[1] + max_pt[1]) / 2.0,
            (min_pt[2] + max_pt[2]) / 2.0,
        )
        x_span = max(max_pt[0] - min_pt[0], min_size)
        y_span = max(max_pt[1] - min_pt[1], min_size)
        z_span = max(max_pt[2] - min_pt[2], min_size)

        normalized_view_name = self._normalize_orthographic_view_name(view_name)
        projected_sizes = {
            "front": (x_span, z_span),
            "back": (x_span, z_span),
            "top": (x_span, y_span),
            "bottom": (x_span, y_span),
            "left": (y_span, z_span),
            "right": (y_span, z_span),
        }
        view_size = projected_sizes.get(normalized_view_name)
        if view_size is None:
            logger.error(f"不支持按包围盒适配的正投影视图: {view_name}")
            return False

        width = max(view_size[0] * max(1.0, padding_ratio), min_size)
        height = max(view_size[1] * max(1.0, padding_ratio), min_size)
        return self.set_orthographic_view(
            view_name,
            target=center_target,
            center=(0.0, 0.0),
            height=height,
            width=width,
        )
    
    def save_drawing(self, file_path: str) -> bool:
        """保存当前图纸到指定路径"""
        if not self.is_running():
            logger.error("CAD未运行，无法保存图纸")
            return False
            
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            # 保存文件
            self.doc.SaveAs(file_path)
            logger.info(f"图纸已保存到: {file_path}")
           
            return True
        except Exception as e:
            logger.error(f"保存图纸失败: {str(e)}")
            return False
    
    def refresh_view(self) -> None:
        """刷新CAD视图"""
        if self.is_running():
            try:
                self._call_with_retry(self.doc.Regen, 1)  # acAllViewports = 1
                self._notify_view_changed("refresh_view")
            except Exception as e:
                logger.error(f"刷新视图失败: {str(e)}")

    def send_command(self, cmd: str) -> bool:
        """
        向 CAD 发送命令字符串（如 STRETCH、ZOOM 等）。
        命令字符串需以空格或换行结尾以模拟回车。

        Args:
            cmd: 命令字符串，如 "_STRETCH "

        Returns:
            成功返回 True，失败返回 False
        """
        if not self.is_running():
            return False
        try:
            if not cmd.endswith(" ") and not cmd.endswith("\n"):
                cmd = cmd + " "
            self._call_with_retry(self.doc.SendCommand, cmd, retries=30, delay=0.4)
            time.sleep(self.command_delay)
            self._notify_view_changed("send_command", {"command": cmd.strip()[:200]})
            return True
        except Exception as e:
            logger.error(f"发送命令失败: {cmd[:50]}..., 错误: {str(e)}")
            return False

    def validate_lineweight(self, lineweight) -> int:
        """验证并返回有效的线宽值
        
        如果提供的线宽值不在有效值列表中，则返回默认值0
        
        Args:
            lineweight: 要验证的线宽值
            
        Returns:
            有效的线宽值
        """
        if lineweight is None:
            return None
            
        # 检查线宽是否在有效值列表中
        if lineweight in self.valid_lineweights:
            return lineweight
        else:
            logger.warning(f"线宽值 {lineweight} 无效，将使用默认值 0")
            return 0
    
    def draw_line(self, start_point: Tuple[float, float, float], 
                 end_point: Tuple[float, float, float], layer: str = None, color: int = None, lineweight=None) -> bool:
        """绘制直线"""
        if not self.is_running():
            return False
            
        try:
            # 确保点是三维的
            if len(start_point) == 2:
                start_point = (start_point[0], start_point[1], 0)
            if len(end_point) == 2:
                end_point = (end_point[0], end_point[1], 0)
      
            # 使用VARIANT包装坐标点数据
            start_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, 
                                               [start_point[0], start_point[1], start_point[2]])
            end_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, 
                                             [end_point[0], end_point[1], end_point[2]])
            
            # 添加直线
            line = self.doc.ModelSpace.AddLine(start_array, end_array)
            
            # 如果指定了图层，设置图层
            if layer:
                # 确保图层存在
                self.create_layer(layer)
                # 设置实体的图层
                line.Layer = layer
         
            # 如果指定了颜色，设置颜色
            if color is not None:
                line.Color = color

            if lineweight is not None:
                line.LineWeight = self.validate_lineweight(lineweight)
            
            # 刷新视图
            self.refresh_view()

            logger.debug(f"已绘制直线: 起点{start_point}, 终点{end_point}, 图层{layer if layer else '默认'}, 颜色{color if color is not None else '默认'}")
            return line
            
        except Exception as e:
            logger.error(f"绘制直线时出错: {str(e)}")
            return None
    
    def draw_circle(self, center: Tuple[float, float, float], 
                   radius: float, layer: str = None, color: int = None, lineweight=None) -> Any:
        """绘制圆"""
        if not self.is_running():
            return None
            
        try:
            # 确保点是三维的
            if len(center) == 2:
                center = (center[0], center[1], 0)
            
            # 使用VARIANT包装坐标点数据
            center_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, 
                                               [center[0], center[1], center[2]])
            
            # 添加圆
            circle = self.doc.ModelSpace.AddCircle(center_array, radius)
            
            # 如果指定了图层，设置图层
            if layer:
                # 确保图层存在
                self.create_layer(layer)
                # 设置实体的图层
                circle.Layer = layer
            
            # 如果指定了颜色，设置颜色
            if color is not None:
                circle.Color = color

            if lineweight is not None:
                circle.LineWeight = self.validate_lineweight(lineweight)
            
            # 刷新视图
            self.refresh_view()
            
            logger.debug(f"已绘制圆: 中心{center}, 半径{radius}, 图层{layer if layer else '默认'}, 颜色{color if color is not None else '默认'}")
            return circle
            
        except Exception as e:
            logger.error(f"绘制圆时出错: {str(e)}")
            return None
    
    def draw_sphere(self, center: Tuple[float, float, float], radius: float, layer: str = None, color: int = None) -> Any:
        """绘制球体（3D实体）"""
        if not self.is_running():
            return None
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0)
            center_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [center[0], center[1], center[2]])
            sphere = self.doc.ModelSpace.AddSphere(center_array, radius)
            if layer:
                self.create_layer(layer)
                sphere.Layer = layer
            if color is not None:
                sphere.Color = color
            self.refresh_view()
            logger.debug(f"已绘制球体: 中心{center}, 半径{radius}")
            return sphere
        except Exception as e:
            logger.error(f"绘制球体时出错: {str(e)}")
            return None
    
    def draw_box(self, center: Tuple[float, float, float], length: float, width: float, height: float, layer: str = None, color: int = None) -> Any:
        """绘制长方体/正方体（3D实体）。正方体时 length=width=height"""
        if not self.is_running():
            return None
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0)
            center_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [center[0], center[1], center[2]])
            model_space = self._get_model_space()
            if model_space is None:
                return None
            box = self._call_with_retry(model_space.AddBox, center_array, float(length), float(width), float(height))
            if layer:
                self.create_layer(layer)
                self._call_with_retry(lambda: setattr(box, "Layer", layer))
            if color is not None:
                self._call_with_retry(lambda: setattr(box, "Color", color))
            self.refresh_view()
            logger.debug(f"已绘制长方体: 中心{center}, 长{length} 宽{width} 高{height}")
            return box
        except Exception as e:
            logger.error(f"绘制长方体时出错: {str(e)}")
            return None

    def draw_cylinder(self, center: Tuple[float, float, float], radius: float, height: float,
                     layer: str = None, color: int = None) -> Any:
        """
        绘制圆柱体（3D实体，沿Z轴方向）
        
        Args:
            center: 底面圆心 (x, y, z)
            radius: 底面半径
            height: 高度（沿Z轴正方向）
            layer: 图层名称（可选）
            color: 颜色索引（可选）
            
        Returns:
            创建的 3DSolid 对象，失败返回 None
        """
        if not self.is_running():
            return None
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0)
            # AutoCAD ActiveX 的 AddCylinder 实际以实体几何中心为基准，
            # 这里对外仍保持“底面圆心 + 高度”的语义，因此先换算到实体中心。
            solid_center = (
                float(center[0]),
                float(center[1]),
                float(center[2]) + float(height) / 2.0,
            )
            center_array = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_R8,
                [solid_center[0], solid_center[1], solid_center[2]],
            )
            model_space = self._get_model_space()
            cylinder = self._call_with_retry(model_space.AddCylinder, center_array, radius, height)
            if layer:
                self.create_layer(layer)
                cylinder.Layer = layer
            if color is not None:
                cylinder.Color = color
            self.refresh_view()
            logger.debug(f"已绘制圆柱: 底面中心{center}, 实体中心{solid_center}, 半径{radius}, 高度{height}")
            return cylinder
        except Exception as e:
            logger.error(f"绘制圆柱时出错: {str(e)}")
            return None

    def draw_cone(self, center: Tuple[float, float, float], base_radius: float, height: float,
                 layer: str = None, color: int = None) -> Any:
        """
        绘制圆锥体（3D实体，沿Z轴方向）
        
        Args:
            center: 底面圆心 (x, y, z)
            base_radius: 底面半径
            height: 高度（沿Z轴正方向）
            layer: 图层名称（可选）
            color: 颜色索引（可选）
            
        Returns:
            创建的 3DSolid 对象，失败返回 None
        """
        if not self.is_running():
            return None
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0)
            center_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [center[0], center[1], center[2]])
            cone = self.doc.ModelSpace.AddCone(center_array, base_radius, height)
            if layer:
                self.create_layer(layer)
                cone.Layer = layer
            if color is not None:
                cone.Color = color
            self.refresh_view()
            logger.debug(f"已绘制圆锥: 底面中心{center}, 半径{base_radius}, 高度{height}")
            return cone
        except Exception as e:
            logger.error(f"绘制圆锥时出错: {str(e)}")
            return None

    def draw_torus(self, center: Tuple[float, float, float], major_radius: float, minor_radius: float,
                   layer: str = None, color: int = None) -> Any:
        """
        绘制圆环体（3D实体）
        
        Args:
            center: 中心点 (x, y, z)
            major_radius: 圆环主半径（从中心到管道中心的距离）
            minor_radius: 管道半径
            layer: 图层名称（可选）
            color: 颜色索引（可选）
            
        Returns:
            创建的 3DSolid 对象，失败返回 None
        """
        if not self.is_running():
            return None
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0)
            center_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [center[0], center[1], center[2]])
            torus = self.doc.ModelSpace.AddTorus(center_array, major_radius, minor_radius)
            if layer:
                try:
                    self.create_layer(layer)
                    torus.Layer = layer
                except Exception as style_error:
                    logger.warning(f"draw_pipe_elbow: 设置图层失败，继续保留弯头实体: {style_error}")
            if color is not None:
                try:
                    torus.Color = color
                except Exception as color_error:
                    logger.warning(f"draw_pipe_elbow: 设置颜色失败，继续保留弯头实体: {color_error}")
            self.refresh_view()
            logger.debug(f"已绘制圆环: 中心{center}, 主半径{major_radius}, 管道半径{minor_radius}")
            return torus
        except Exception as e:
            logger.error(f"绘制圆环时出错: {str(e)}")
            return None

    # -------------------------------------------------------------------------
    # 国标 GB/T 12459-2017 钢制对焊管件 - 弯头用公称直径 DN 与管子外径 OD（单位 mm）
    # R = 1.5D（长半径）、R = 1.0D（短半径）、R = 3D（3D 弯头）中 D 为管子外径 OD
    # 数据参考 GB/T 12459、GB/T 28708 及常用钢管外径系列
    # -------------------------------------------------------------------------
    ELBOW_DN_OD_GB12459 = {
        15: 21.3, 20: 26.9, 25: 33.7, 32: 42.4, 40: 48.3, 50: 60.3, 65: 76.1,
        80: 88.9, 100: 114.3, 125: 140, 150: 165.1, 200: 219.1, 250: 273,
        300: 323.9, 350: 355.6, 400: 406.4, 450: 457, 500: 508, 600: 610,
    }
    # 公称直径与壁厚联动表（国标默认壁厚，mm）。
    # 当未显式指定壁厚时，默认按国标常用 STD 系列取值；若当前口径没有补齐更精确的
    # STD 值，则回退到历史近似表，确保旧任务不被阻断。
    ELBOW_WALL_THICKNESS_GB = {
        15: 2.0, 20: 2.0, 25: 2.5, 32: 2.5, 40: 3.0, 50: 3.5, 65: 4.0, 80: 4.0,
        100: 4.5, 125: 5.0, 150: 5.5, 200: 6.5, 250: 8.0, 300: 8.5, 350: 9.5, 400: 10.0,
        450: 11.0, 500: 12.0, 600: 14.0,
    }

    # 依据 GB/T 12459 三通/对焊管件常用 STD 系列壁厚补齐的优先表。
    # 当前优先覆盖本项目已实际使用并可稳定核实的口径。
    FITTING_WALL_THICKNESS_GB12459_STD = {
        125: 6.55,
        250: 9.27,
        450: 9.53,
        500: 9.53,
    }

    def _resolve_default_fitting_wall_thickness_gb12459(self, dn: Optional[int]) -> Optional[float]:
        """按国标常用 STD 系列解析默认壁厚。"""
        if dn is None:
            return None
        dn_value = int(dn)
        if dn_value in self.FITTING_WALL_THICKNESS_GB12459_STD:
            return float(self.FITTING_WALL_THICKNESS_GB12459_STD[dn_value])
        if dn_value in self.ELBOW_WALL_THICKNESS_GB:
            return float(self.ELBOW_WALL_THICKNESS_GB[dn_value])
        return None

    def _infer_elbow_dn_from_outer_diameter(self, outer_diameter: float) -> Optional[int]:
        """按 GB/T 12459 外径表从 OD 反推最接近的弯头公称直径 DN。"""
        if outer_diameter is None or outer_diameter <= 0:
            return None

        best_dn = None
        best_od = None
        best_delta = float("inf")
        for candidate_dn, candidate_od in self.ELBOW_DN_OD_GB12459.items():
            delta = abs(float(candidate_od) - float(outer_diameter))
            if delta < best_delta:
                best_dn = int(candidate_dn)
                best_od = float(candidate_od)
                best_delta = delta

        if best_dn is None:
            return None

        tolerance = max(1.0, float(outer_diameter) * 0.03)
        if best_delta > tolerance:
            logger.warning(
                "draw_pipe_elbow: 外径 %.3f 未精确命中国标外径序列，按最近标准 DN%d (OD=%.3f) 取壁厚",
                outer_diameter,
                best_dn,
                best_od,
            )
        else:
            logger.info(
                "draw_pipe_elbow: 根据外径 %.3f 反推国标 DN%d (OD=%.3f)",
                outer_diameter,
                best_dn,
                best_od,
            )
        return best_dn

    # 国标 GB/T 12459-2017 三通中心至端面尺寸（Series A，单位 mm）。
    # 等径三通：C=M；异径三通：主管方向沿用等径 C，支管方向 M 按异径规格表读取。
    TEE_CENTER_TO_END_GB12459 = {
        15: 25.0,
        20: 29.0,
        25: 38.0,
        32: 48.0,
        40: 57.0,
        50: 64.0,
        65: 76.0,
        80: 86.0,
        100: 105.0,
        125: 124.0,
        150: 143.0,
        200: 178.0,
        250: 216.0,
        300: 254.0,
        350: 279.0,
        400: 305.0,
        450: 343.0,
        500: 381.0,
        600: 432.0,
    }

    REDUCING_TEE_BRANCH_CENTER_TO_END_GB12459 = {
        (125, 100): 117.0,
        (125, 90): 114.0,
        (125, 80): 111.0,
        (125, 65): 108.0,
        (125, 50): 105.0,
        (250, 200): 203.0,
        (250, 150): 194.0,
        (250, 125): 191.0,
        (250, 100): 184.0,
        (450, 400): 330.0,
        (450, 350): 330.0,
        (450, 300): 321.0,
        (450, 250): 308.0,
        (450, 200): 298.0,
        (500, 450): 368.0,
        (500, 400): 356.0,
        (500, 350): 356.0,
        (500, 300): 346.0,
        (500, 250): 333.0,
        (500, 200): 324.0,
    }

    def _resolve_tee_center_to_end_gb12459(
        self,
        run_dn: int,
        branch_dn: int,
        run_center_to_end: Optional[float] = None,
        branch_center_to_end: Optional[float] = None,
    ) -> Optional[Tuple[float, float]]:
        """Resolve tee C/M dimensions from GB/T 12459 or explicit overrides."""
        if run_dn not in self.TEE_CENTER_TO_END_GB12459 and run_center_to_end is None:
            logger.error(f"draw_tee: 未找到国标 DN{run_dn} 对应主管中心至端面尺寸 C")
            return None
        if branch_dn > run_dn:
            logger.error(f"draw_tee: branch_dn={branch_dn} 不能大于 dn={run_dn}")
            return None

        C = float(run_center_to_end) if run_center_to_end is not None else float(self.TEE_CENTER_TO_END_GB12459[run_dn])
        if branch_center_to_end is not None:
            M = float(branch_center_to_end)
        elif branch_dn == run_dn:
            M = C
        else:
            M = self.REDUCING_TEE_BRANCH_CENTER_TO_END_GB12459.get((run_dn, branch_dn))
            if M is None:
                logger.error(
                    "draw_tee: 未找到国标 DN%s×DN%s 异径三通的支管中心至端面尺寸 M；"
                    "如需绘制该非内置组合，请显式传入 run_center_to_end 与 branch_center_to_end",
                    run_dn,
                    branch_dn,
                )
                return None
            M = float(M)
        return C, M

    def draw_pipe_elbow(self, center: Tuple[float, float, float], pipe_radius: float = None,
                        dn: Optional[int] = None, bend_radius: float = None, elbow_type: str = "LR",
                        angle: float = 90, plane: str = "xy", wall_thickness: Optional[float] = None,
                        layer: str = None, color: int = None) -> Any:
        """
        绘制管道弯头（3D 实体，可空心）。依据国标 GB/T 12459-2017《钢制对焊管件 类型与参数》。

        弯曲半径 R 与管子外径 D（OD）的关系（国标规定）：
        - LR（长半径）：R = 1.5D，默认，代号如 90EL、45EL、180EL
        - SR（短半径）：R = 1.0D，代号如 90ES、180ES
        - 3D：R = 3D，代号如 90E3D、45E3D
        角度：45°、90°、180°。

        壁厚与公称直径联动：未显式指定 wall_thickness 时，优先使用 dn；若只给 pipe_radius，则按国标外径序列
        反推最近 DN，再从 ELBOW_WALL_THICKNESS_GB 取对应壁厚（如 DN15→2.0mm，DN50→3.5mm）绘制空心弯头。
        显式传入 wall_thickness 时以该值为准；显式传 0 时保持实心。

        Args:
            center: 弯头转角中心 (x, y, z)，两段管道轴线的交点
            pipe_radius: 管道半径（外径 OD 的一半），与 dn 二选一
            dn: 公称通径（mm），与 pipe_radius 二选一；指定时从 ELBOW_DN_OD_GB12459 查 OD
            bend_radius: 弯曲半径 R（可选）；未指定时按 elbow_type 计算
            elbow_type: "LR"=长半径(1.5D)，"SR"=短半径(1.0D)，"3D"=3倍外径(3D)，默认 "LR"
            angle: 弯头角度 45、90、180（度），默认 90
            plane: 弯头所在平面，当前支持 "xy"
            wall_thickness: 壁厚（mm）；不指定时按 dn 或 pipe_radius 反推的 DN 从国标表自动取；
                显式传 0 为实心
            layer: 图层名称（可选）
            color: 颜色索引（可选）

        Returns:
            创建的 3DSolid 弯头实体（实心或空心），失败返回 None
        """
        if not self.is_running():
            return None
        # 由 dn 或 pipe_radius 得到管道半径 r（外径之半）
        resolved_dn = int(dn) if dn is not None else None
        if pipe_radius is not None and pipe_radius > 0:
            r = float(pipe_radius)
        elif dn is not None and dn in self.ELBOW_DN_OD_GB12459:
            od = self.ELBOW_DN_OD_GB12459[int(dn)]
            r = od / 2.0
        else:
            if dn is not None:
                logger.error(f"draw_pipe_elbow: 未找到国标 DN{dn} 对应外径，请指定 pipe_radius 或使用表中 DN")
            else:
                logger.error("draw_pipe_elbow: 须指定 pipe_radius 或 dn")
            return None
        pipe_radius = r
        D = 2.0 * pipe_radius  # 管子外径 OD（国标中的 D）
        if resolved_dn is None:
            resolved_dn = self._infer_elbow_dn_from_outer_diameter(D)
        # 按 GB/T 12459 由 elbow_type 计算弯曲半径 R
        if bend_radius is None:
            et = (elbow_type or "LR").upper()
            if et == "SR":
                bend_radius = 1.0 * D   # 短半径：R = 1.0D
            elif et == "3D":
                bend_radius = 3.0 * D   # 3D 弯头：R = 3D
            else:
                bend_radius = 1.5 * D   # 长半径：R = 1.5D（默认）
        # 壁厚与 DN 联动：未显式指定时，若提供了 dn 则从国标表取对应壁厚（如 DN15→2.0，DN50→3.5）
        if wall_thickness is not None and wall_thickness > 0:
            wt = float(wall_thickness)
        elif resolved_dn is not None:
            resolved_wt = self._resolve_default_fitting_wall_thickness_gb12459(resolved_dn)
            wt = float(resolved_wt) if resolved_wt is not None else 0.0
        else:
            wt = 0.0
        if plane.lower() not in ("xy",):
            logger.warning(f"draw_pipe_elbow: plane={plane} 暂不支持，使用 xy")
            return self.draw_pipe_elbow(center, pipe_radius=pipe_radius, dn=dn, bend_radius=bend_radius,
                                        elbow_type=elbow_type, angle=angle, plane="xy", wall_thickness=wall_thickness,
                                        layer=layer, color=color)
        angle = float(angle)
        if angle not in (45, 90, 180):
            logger.warning(f"draw_pipe_elbow: angle={angle} 不在支持列表(45/90/180)，使用 90")
            angle = 90
        created_entities: list[Any] = []
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0)
            cx, cy, cz = center
            R, r = bend_radius, pipe_radius
            half_inf = max(R + r + 100, 10000.0)

            def _delete_solid(solid):
                try:
                    solid.Delete()
                except Exception:
                    pass

            def _track(solid):
                if solid is not None:
                    created_entities.append(solid)
                return solid

            def _make_elbow_segment(r_minor: float):
                """创建单侧圆环体并按 angle 切除，得到弯头弧段。r_minor 为圆环管道半径（外径之半或内径之半）。"""
                center_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, cz])
                seg = _track(self._call_with_retry(self._get_model_space().AddTorus, center_arr, R, r_minor))
                hi = max(R + r_minor + 100, 10000.0)
                if plane.lower() == "xy":
                    if angle == 180:
                        box_c = (cx, cy + hi / 2, cz)
                        box_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(box_c))
                        box = _track(self._call_with_retry(self._get_model_space().AddBox, box_center, 2 * hi, hi, 2 * hi))
                        if not self._solid_boolean_subtract(seg, box):
                            raise RuntimeError("elbow angle 180 first cut failed")
                        _delete_solid(box)
                    elif angle == 90:
                        box1_c = (cx - hi / 2, cy, cz)
                        box1_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(box1_c))
                        box1 = _track(self._call_with_retry(self._get_model_space().AddBox, box1_center, hi, 2 * hi, 2 * hi))
                        if not self._solid_boolean_subtract(seg, box1):
                            raise RuntimeError("elbow angle 90 first cut failed")
                        _delete_solid(box1)
                        box2_c = (cx, cy - hi / 2, cz)
                        box2_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(box2_c))
                        box2 = _track(self._call_with_retry(self._get_model_space().AddBox, box2_center, 2 * hi, hi, 2 * hi))
                        if not self._solid_boolean_subtract(seg, box2):
                            raise RuntimeError("elbow angle 90 second cut failed")
                        _delete_solid(box2)
                    else:
                        box1_c = (cx - hi / 2, cy, cz)
                        box1_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(box1_c))
                        box1 = _track(self._call_with_retry(self._get_model_space().AddBox, box1_center, hi, 2 * hi, 2 * hi))
                        if not self._solid_boolean_subtract(seg, box1):
                            raise RuntimeError("elbow angle 45 first cut failed")
                        _delete_solid(box1)
                        box2_c = (cx, cy - hi / 2, cz)
                        box2_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(box2_c))
                        box2 = _track(self._call_with_retry(self._get_model_space().AddBox, box2_center, 2 * hi, hi, 2 * hi))
                        if not self._solid_boolean_subtract(seg, box2):
                            raise RuntimeError("elbow angle 45 second cut failed")
                        _delete_solid(box2)
                        # 45° 弯头的端面必须由过弯曲中心的径向平面切出，
                        # 这样切面才会与管道中心线垂直，投影结果两端都会保持为圆。
                        diag_box_c = (cx, cy - hi / 2, cz)
                        diag_box_center = win32com.client.VARIANT(
                            pythoncom.VT_ARRAY | pythoncom.VT_R8,
                            list(diag_box_c),
                        )
                        diag_box = _track(self._call_with_retry(self._get_model_space().AddBox, diag_box_center, 2 * hi, hi, 2 * hi))
                        pt1 = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, cz])
                        pt2 = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, cz + 1])
                        self._call_with_retry(diag_box.Rotate3D, pt1, pt2, math.radians(45))
                        if not self._solid_boolean_subtract(seg, diag_box):
                            raise RuntimeError("elbow angle 45 diagonal cut failed")
                        _delete_solid(diag_box)
                return seg

            if wt > 0 and wt < r:
                # 空心弯头：外弧段减去内弧段，内孔可输送流体
                r_inner = r - wt
                outer_elbow = _make_elbow_segment(r)
                inner_elbow = _make_elbow_segment(r_inner)
                if not self._solid_boolean_subtract(outer_elbow, inner_elbow):
                    raise RuntimeError("elbow hollow bore subtract failed")
                _delete_solid(inner_elbow)
                torus = outer_elbow
            else:
                torus = _make_elbow_segment(r)

            if layer:
                self.create_layer(layer)
                torus.Layer = layer
            if color is not None:
                torus.Color = color
            self._finalize_entity_display(torus)
            self.refresh_view()
            logger.debug(
                f"已绘制{angle}°弯头: 中心{center}, 弯曲半径{R}, 管道半径{r}, 推断DN={resolved_dn}, 壁厚{wt}, 平面{plane}"
            )
            return torus
        except Exception as e:
            for entity in reversed(created_entities):
                _delete_solid(entity)
            logger.error(f"绘制{angle}°弯头时出错: {str(e)}")
            return None

    def draw_pipe_elbow_90(self, center: Tuple[float, float, float], pipe_radius: float = None,
                           dn: Optional[int] = None, bend_radius: float = None, elbow_type: str = "LR",
                           plane: str = "xy", wall_thickness: Optional[float] = None,
                           layer: str = None, color: int = None) -> Any:
        """
        绘制 90° 管道弯头（GB/T 12459，可指定壁厚做空心）。默认长半径 LR。
        """
        return self.draw_pipe_elbow(center, pipe_radius=pipe_radius, dn=dn, bend_radius=bend_radius,
                                    elbow_type=elbow_type, angle=90, plane=plane, wall_thickness=wall_thickness,
                                    layer=layer, color=color)

    def draw_reducer(
        self,
        center: Tuple[float, float, float],
        dn_large: int,
        dn_small: int,
        axis: str = "z",
        reducer_type: str = "concentric",
        length: Optional[float] = None,
        wall_thickness: Optional[float] = None,
        eccentric_direction: Optional[str] = None,
        eccentric_offset: Optional[float] = None,
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制异径管（同心/偏心，3D 实体）。依据国标 GB/T 12459-2017《钢制对焊管件》。

        当前接口保持 `draw_reducer` 名称以兼容既有代码，但中文语义统一视为“异径管”。
        其中 `reducer_type="concentric"` 表示同心异径管，`reducer_type="eccentric"` 表示偏心异径管。

        Args:
            center: 大端圆心 (x, y, z)，异径管沿轴正方向由大端到小端
            dn_large: 大端公称通径（mm）
            dn_small: 小端公称通径（mm），须小于 dn_large
            axis: 异径管轴向，默认 "z"（沿 Z 正方向）
            reducer_type: "concentric"/"eccentric"
            length: 结构长度（mm）；不指定时按国标习惯公式估算
            wall_thickness: 壁厚（mm）；不指定时按 dn_large 从国标表取
            eccentric_direction: 偏心方向（x/y/z 或 -x/-y/-z），仅偏心异径管时生效
            eccentric_offset: 偏心量（mm），仅偏心异径管时生效
            layer: 图层名称（可选）
            color: 颜色索引（可选）

        Returns:
            创建的 3DSolid 异径管实体，失败返回 None
        """
        return self._draw_pipe_reducer_impl(
            center=center,
            dn_large=dn_large,
            dn_small=dn_small,
            axis=axis,
            reducer_type=reducer_type,
            length=length,
            wall_thickness=wall_thickness,
            eccentric_direction=eccentric_direction,
            eccentric_offset=eccentric_offset,
            layer=layer,
            color=color,
        )

    def _draw_pipe_reducer_impl(
        self,
        center: Tuple[float, float, float],
        dn_large: int,
        dn_small: int,
        axis: str = "z",
        reducer_type: str = "concentric",
        length: Optional[float] = None,
        wall_thickness: Optional[float] = None,
        eccentric_direction: Optional[str] = None,
        eccentric_offset: Optional[float] = None,
        layer: str = None,
        color: int = None,
    ) -> Any:
        if not self.is_running():
            return None
        if dn_small >= dn_large:
            logger.error("draw_reducer: dn_small 须小于 dn_large")
            return None
        if dn_large not in self.ELBOW_DN_OD_GB12459 or dn_small not in self.ELBOW_DN_OD_GB12459:
            logger.error(f"draw_reducer: 未找到 DN{dn_large} 或 DN{dn_small} 对应外径")
            return None
        od_large = self.ELBOW_DN_OD_GB12459[dn_large]
        od_small = self.ELBOW_DN_OD_GB12459[dn_small]
        r_large = od_large / 2.0
        r_small = od_small / 2.0
        axis_signed = (axis or "z").lower().strip()
        axis_abs = axis_signed.lstrip("-")
        reducer_type_key = (reducer_type or "concentric").strip().lower().replace("-", "_").replace(" ", "_")
        if reducer_type_key in {"conc", "同心", "同心异径管"}:
            reducer_type_key = "concentric"
        elif reducer_type_key in {"ecc", "偏心", "偏心异径管"}:
            reducer_type_key = "eccentric"
        elif reducer_type_key not in {"concentric", "eccentric"}:
            logger.warning(f"draw_reducer: 未识别的 reducer_type={reducer_type}，已回退为 concentric")
            reducer_type_key = "concentric"
        length_value = float(length) if length is not None and length > 0 else max(50.0, (od_large - od_small) * 1.2)
        if wall_thickness is not None and wall_thickness > 0:
            wt = float(wall_thickness)
        else:
            large_default_wt = self._resolve_default_fitting_wall_thickness_gb12459(dn_large)
            small_default_wt = self._resolve_default_fitting_wall_thickness_gb12459(dn_small)
            if large_default_wt is not None:
                wt = float(large_default_wt)
            elif small_default_wt is not None:
                wt = float(small_default_wt)
            else:
                wt = 4.0

        try:
            if axis_abs not in ("x", "y", "z"):
                logger.error(f"draw_reducer: 不支持的轴向 {axis}")
                return None
            cx, cy, cz = self._normalize_point3d(center)

            def _local_to_global_from_z(local_vec: Tuple[float, float, float]) -> Tuple[float, float, float]:
                x_val, y_val, z_val = local_vec
                axis_name, sign = self._axis_dir(axis_signed, default="z")
                if axis_name == "z":
                    return (x_val, -y_val, -z_val) if sign < 0 else (x_val, y_val, z_val)
                if axis_name == "x":
                    return (z_val, y_val, -x_val) if sign > 0 else (-z_val, y_val, x_val)
                return (x_val, z_val, -y_val) if sign > 0 else (x_val, -z_val, y_val)

            def _resolve_eccentric_direction(direction_value: Optional[str]) -> Tuple[str, Tuple[float, float, float]]:
                desired = direction_value or ("y" if axis_abs == "x" else "x")
                desired_unit = self._axis_unit(desired, default="x")
                axis_unit = self._axis_unit(axis_signed, default="z")
                if abs(sum(desired_unit[i] * axis_unit[i] for i in range(3))) > 1e-6:
                    raise ValueError(f"偏心方向 {desired} 不能与轴向 {axis_signed} 平行")
                for local_vec in ((1.0, 0.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, -1.0, 0.0)):
                    mapped = _local_to_global_from_z(local_vec)
                    if all(abs(mapped[i] - desired_unit[i]) < 1e-6 for i in range(3)):
                        return desired, local_vec
                raise ValueError(f"无法解析偏心方向 {desired}")

            def _add_circle_raw(circle_center: Tuple[float, float, float], radius: float):
                center_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(circle_center))
                return self._call_with_retry(self.doc.ModelSpace.AddCircle, center_arr, float(radius))

            def _loft_between_circles(
                start_center: Tuple[float, float, float],
                start_radius: float,
                end_center: Tuple[float, float, float],
                end_radius: float,
            ):
                if start_radius <= 0 or end_radius <= 0:
                    return None
                model_space = self._get_model_space()
                if model_space is None:
                    return None
                count_before = model_space.Count
                circle_start = None
                circle_end = None
                try:
                    circle_start = _add_circle_raw(start_center, start_radius)
                    circle_end = _add_circle_raw(end_center, end_radius)
                    if circle_start is None or circle_end is None:
                        return None
                    cmd = (
                        f'(command "_.LOFT" (handent "{circle_start.Handle}") '
                        f'(handent "{circle_end.Handle}") "" "") '
                    )
                    if not self.send_command(cmd):
                        return None
                    time.sleep(max(0.8, self.command_delay))
                    count_after = model_space.Count
                    if count_after <= count_before:
                        logger.error("draw_reducer: LOFT 未生成新的异径管实体")
                        return None
                    return model_space.Item(count_after - 1)
                finally:
                    self._safe_delete_entity(circle_start)
                    self._safe_delete_entity(circle_end)

            outer_small_center = (0.0, 0.0, length_value)
            inner_small_center = (0.0, 0.0, length_value)
            eccentric_direction_key = None
            if reducer_type_key == "eccentric":
                eccentric_direction_key, local_offset_unit = _resolve_eccentric_direction(eccentric_direction)
                outer_offset_value = abs(float(eccentric_offset)) if eccentric_offset is not None else (r_large - r_small)
                outer_small_center = (
                    local_offset_unit[0] * outer_offset_value,
                    local_offset_unit[1] * outer_offset_value,
                    length_value,
                )

            r_inner_large = r_large - wt
            r_inner_small = r_small - wt
            if reducer_type_key == "eccentric" and r_inner_large > 0 and r_inner_small > 0:
                _, local_offset_unit = _resolve_eccentric_direction(eccentric_direction_key)
                inner_offset_value = abs(float(eccentric_offset)) if eccentric_offset is not None else (r_inner_large - r_inner_small)
                inner_small_center = (
                    local_offset_unit[0] * inner_offset_value,
                    local_offset_unit[1] * inner_offset_value,
                    length_value,
                )

            outer = _loft_between_circles((0.0, 0.0, 0.0), r_large, outer_small_center, r_small)
            if outer is None:
                logger.error("draw_reducer: 创建异径管外轮廓失败")
                return None

            self._rotate_solid_from_z(outer, (0.0, 0.0, 0.0), axis_signed)
            pt_move_from = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 0.0, 0.0])
            pt_move_to = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, cz])
            outer.Move(pt_move_from, pt_move_to)
            if wt > 0 and r_inner_small > 0 and r_inner_large > 0:
                inner = _loft_between_circles((0.0, 0.0, 0.0), r_inner_large, inner_small_center, r_inner_small)
                if inner is not None:
                    self._rotate_solid_from_z(inner, (0.0, 0.0, 0.0), axis_signed)
                    inner.Move(pt_move_from, pt_move_to)
                    if not self._solid_boolean_subtract(outer, inner):
                        logger.warning("draw_reducer: 内轮廓布尔减失败，异径管可能仍为封闭实体")
                    self._safe_delete_entity(inner)
            if layer:
                self.create_layer(layer)
                outer.Layer = layer
            if color is not None:
                outer.Color = color
            self.refresh_view()
            logger.debug(
                "已绘制异径管: 中心%s, DN%s→DN%s, type=%s, 轴向%s, 长度%s, 壁厚%s, 偏心方向=%s",
                center,
                dn_large,
                dn_small,
                reducer_type_key,
                axis_signed,
                length_value,
                wt,
                eccentric_direction_key,
            )
            return outer
        except Exception as e:
            logger.error(f"绘制异径管时出错: {str(e)}")
            return None

    def draw_tee(
        self,
        center: Tuple[float, float, float],
        dn: int,
        branch_dn: Optional[int] = None,
        run_axis: str = "x",
        branch_axis: str = "z",
        wall_thickness: Optional[float] = None,
        branch_wall_thickness: Optional[float] = None,
        run_center_to_end: Optional[float] = None,
        branch_center_to_end: Optional[float] = None,
        layer: str = None,
        color: int = None,
        reinforce: bool = False,
        reinforce_extra: Optional[float] = None,
        reinforce_run_ratio: float = 0.55,
        reinforce_branch_ratio: float = 0.70,
        transition_ratio: float = 0.0,
        junction_bulge: bool = False,
        junction_bulge_extra: Optional[float] = None,
    ) -> Any:
        """
        绘制三通（3D 实体，GB/T 12459-2017）。当 branch_dn 为空或等于 dn 时绘制等径三通；
        当 branch_dn 小于 dn 时绘制异径三通。默认按国标常用 STD 系列壁厚生成空心流道；
        仅当显式传入 0 时才保持实心。
        
        外形细节（可选增强，贴近工程常见“外鼓/补强+过渡”效果）：
        - reinforce=True：在主管/支管交界处加外补强（加强圈/加厚区），并用锥台过渡到主管/支管外径
        - junction_bulge=True：在三通交点叠加外鼓球面（小幅外鼓），用于模拟国标对焊三通常见的厚度过渡/圆滑交界
        
        默认朝向（对应工程图常见的标准三视图）：
        - 主管沿 X（水平），其端面圆位于 yz 平面
        - 支管沿 +Z（在 AutoCAD 前视图中表现为向上），其端面圆位于 xy 平面
        - 支管与主管在 center 处正交相交（支管在主管正中心）
        - 默认外形对应三通三视图；默认会自动生成主管两端与支管顶端的可过流开口

        尺寸：C 为主管中心至端面尺寸，M 为支管中心至端面尺寸。
        为了让默认三视图更贴近工程图表达，代码会在保留支管露出长度 M 的前提下，
        适度拉长主管显示长度，使主视图中的左右外露段更接近示意图常见比例。
        支管轴向可带负号表示反方向。为匹配工程图主视图，默认采用 "z"。

        Args:
            center: 三通交点 (x, y, z)，支管位于主管正中心
            dn: 主管公称通径（mm）
            branch_dn: 支管公称通径（mm）；不填或等于 dn 时为等径三通
            run_axis: 主管轴向 "x"/"y"/"z" 或带符号 "-x/-y/-z"，默认 "x"
            branch_axis: 支管轴向 "x"/"y"/"z"/"-x"/"-y"/"-z"，默认 "z"（在 AutoCAD 前视图中向上）
            wall_thickness: 主管壁厚（mm）；不填时按国标常用 STD 系列自动取值，传 0 时保持实心
            branch_wall_thickness: 支管壁厚（mm）；不填时按支管 DN 的国标常用 STD 系列自动取值，缺失时沿用主管壁厚
            run_center_to_end: 显式指定主管方向 C（mm）；仅在国标表未覆盖或需要手工修正时使用
            branch_center_to_end: 显式指定支管方向 M（mm）；仅在国标表未覆盖或需要手工修正时使用
            layer: 图层名称（可选）
            color: 颜色索引（可选）
            reinforce: 是否启用交界外补强（加强圈/加厚区）与锥台过渡；默认 False
            reinforce_extra: 外补强增加的外半径（mm）；不指定时按 DN/壁厚估算
            reinforce_run_ratio: 补强在主管方向的长度占 2C 的比例（0~1），默认 0.55（居中对称）
            reinforce_branch_ratio: 补强在支管方向的长度占 M 的比例（0~1），默认 0.70（从交点向支管延伸）
            transition_ratio: 过渡锥台长度占 OD 的比例（0~0.5），默认 0.0；对异径三通默认关闭外过渡，避免主管两侧出现额外凸起
            junction_bulge: 是否在交点叠加外鼓（用于模拟“圆角/厚度过渡”观感）；默认 False
            junction_bulge_extra: 外鼓增加的外半径（mm）；不指定时默认接近壁厚

        Returns:
            创建的 3DSolid 三通实体，失败返回 None
        """
        if not self.is_running():
            return None
        if dn not in self.ELBOW_DN_OD_GB12459:
            logger.error(f"draw_tee: 未找到国标 DN{dn} 对应外径")
            return None
        branch_dn_resolved = int(branch_dn) if branch_dn is not None else int(dn)
        if branch_dn_resolved not in self.ELBOW_DN_OD_GB12459:
            logger.error(f"draw_tee: 未找到国标支管 DN{branch_dn_resolved} 对应外径")
            return None
        resolved_center_to_end = self._resolve_tee_center_to_end_gb12459(
            run_dn=int(dn),
            branch_dn=branch_dn_resolved,
            run_center_to_end=run_center_to_end,
            branch_center_to_end=branch_center_to_end,
        )
        if resolved_center_to_end is None:
            return None
        C, M = resolved_center_to_end
        od_run = self.ELBOW_DN_OD_GB12459[int(dn)]
        od_branch = self.ELBOW_DN_OD_GB12459[branch_dn_resolved]
        R_outer_run = od_run / 2.0
        R_outer_branch = od_branch / 2.0
        if wall_thickness is not None and wall_thickness > 0:
            run_wt = float(wall_thickness)
        elif wall_thickness == 0:
            run_wt = 0.0
        else:
            resolved_run_wt = self._resolve_default_fitting_wall_thickness_gb12459(int(dn))
            run_wt = float(resolved_run_wt) if resolved_run_wt is not None else 0.0
        if branch_wall_thickness is not None and branch_wall_thickness > 0:
            branch_wt = float(branch_wall_thickness)
        elif branch_wall_thickness == 0:
            branch_wt = 0.0
        else:
            resolved_branch_wt = self._resolve_default_fitting_wall_thickness_gb12459(branch_dn_resolved)
            branch_wt = float(resolved_branch_wt) if resolved_branch_wt is not None else run_wt
        R_inner_run = max(0.0, R_outer_run - run_wt) if run_wt > 0 else 0.0
        R_inner_branch = max(0.0, R_outer_branch - branch_wt) if branch_wt > 0 else 0.0
        is_equal_tee = branch_dn_resolved == int(dn)

        run_axis_signed = (run_axis or "x").lower().strip()
        branch_axis_signed = (branch_axis or "z").lower().strip()
        run_axis_abs = run_axis_signed.lstrip("-")
        branch_axis_abs = branch_axis_signed.lstrip("-")
        if run_axis_abs == branch_axis_abs:
            logger.error("draw_tee: run_axis 与 branch_axis 不能为同一条轴")
            return None

        created_entities: list[Any] = []
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0.0)
            cx, cy, cz = center

            def _del(s):
                try:
                    s.Delete()
                except Exception:
                    pass

            _unit = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}

            def _axis_dir(axis_signed_str: str) -> Tuple[str, int]:
                s = (axis_signed_str or "z").lower().strip()
                if s.startswith("-"):
                    return s[1:], -1
                return s, 1

            def _rotate_solid_from_z(solid, base_pt: Tuple[float, float, float], axis_signed_str: str):
                """将沿 +Z 生成的实体绕 base_pt 旋转到指定轴向（含正负号）。"""
                ax, sign = _axis_dir(axis_signed_str)
                if ax not in ("x", "y", "z"):
                    return
                px, py, pz = base_pt
                pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz])
                if ax == "z":
                    if sign < 0:
                        pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1, py, pz])
                        solid.Rotate3D(pt_center, pt_x, math.pi)
                    return
                if ax == "x":
                    pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1, pz])
                    ang = (-math.pi / 2) if sign > 0 else (math.pi / 2)
                    solid.Rotate3D(pt_center, pt_y, ang)
                    return
                if ax == "y":
                    pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1, py, pz])
                    ang = (-math.pi / 2) if sign > 0 else (math.pi / 2)
                    solid.Rotate3D(pt_center, pt_x, ang)

            def _vec_mul(v, k: float):
                return (v[0] * k, v[1] * k, v[2] * k)

            def _vec_add(a, b):
                return (a[0] + b[0], a[1] + b[1], a[2] + b[2])

            def _vec_sub(a, b):
                return (a[0] - b[0], a[1] - b[1], a[2] - b[2])

            def _axis_unit(axis_signed_str: str):
                ax, sign = _axis_dir(axis_signed_str)
                u = _unit.get(ax, (0.0, 0.0, 1.0))
                return (u[0] * sign, u[1] * sign, u[2] * sign)

            def _cylinder(base_pt: Tuple[float, float, float], radius: float, height: float, axis_signed_str: str):
                """创建沿 axis_signed_str 的圆柱，base_pt 为起始端面圆心，height 为长度。"""
                axis_vec = _axis_unit(axis_signed_str)
                center_pt = _vec_add(base_pt, _vec_mul(axis_vec, height / 2.0))
                ox, oy, oz = center_pt
                cen = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [ox, oy, oz])
                model_space = self._get_model_space()
                cyl = self._call_with_retry(model_space.AddCylinder, cen, radius, height)
                _rotate_solid_from_z(cyl, center_pt, axis_signed_str)
                return cyl

            def _frustum(base_pt: Tuple[float, float, float], r_big: float, r_small: float, length_f: float, axis_signed_str: str):
                """圆台：大端半径 r_big 在 base_pt，小端半径 r_small 在 base_pt 沿 axis 方向 length_f 处。"""
                if r_big <= r_small or length_f <= 0:
                    return None
                H = length_f * r_big / (r_big - r_small)
                ox, oy, oz = base_pt
                base_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [ox, oy, oz])
                model_space = self._get_model_space()
                cone_big = self._call_with_retry(model_space.AddCone, base_arr, r_big, H)
                tip_local = (ox, oy, oz + length_f)
                tip_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [tip_local[0], tip_local[1], tip_local[2]])
                cone_small = self._call_with_retry(model_space.AddCone, tip_arr, r_small, H - length_f)
                self._solid_boolean_subtract(cone_big, cone_small)
                _del(cone_small)
                _rotate_solid_from_z(cone_big, base_pt, axis_signed_str)
                return cone_big

            branch_visible_len = M
            run_half_len = max(C, branch_visible_len + R_outer_run)
            run_len = 2.0 * run_half_len
            branch_transition_len = 0.0
            transition_ratio_value = max(0.0, min(0.45, float(transition_ratio)))
            if (not is_equal_tee) and R_outer_branch < R_outer_run and transition_ratio_value > 0:
                branch_transition_len = max(
                    (R_outer_run - R_outer_branch) * 1.2,
                    od_run * transition_ratio_value,
                )
                branch_transition_len = min(branch_transition_len, branch_visible_len * 0.6)
            if R_inner_run > 0 or R_inner_branch > 0:
                # 支管外壁在根部也要向主管内部吃进一小段，和内孔超切保持一致，
                # 否则空心三通在支管根部下侧会被切出可见缺口。
                branch_embed = min(max(1.0, run_wt, branch_wt), max(0.0, R_outer_run - 0.5))
            else:
                branch_embed = 0.0
            branch_len = branch_visible_len + R_outer_run - branch_transition_len
            run_dir = _axis_unit(run_axis_signed)
            branch_dir = _axis_unit(branch_axis_signed)
            run_base = _vec_sub((cx, cy, cz), _vec_mul(run_dir, run_half_len))
            branch_base = _vec_add(
                _vec_sub((cx, cy, cz), _vec_mul(branch_dir, branch_embed)),
                _vec_mul(branch_dir, branch_transition_len),
            )
            branch_total_len = max(branch_len + branch_embed, R_outer_run + branch_embed)

            run_outer = _cylinder(run_base, R_outer_run, run_len, run_axis_signed)
            if run_outer is None:
                return None
            if branch_transition_len > 0:
                branch_transition_outer = _frustum(
                    (cx, cy, cz),
                    R_outer_run,
                    R_outer_branch,
                    branch_transition_len,
                    branch_axis_signed,
                )
                if branch_transition_outer:
                    self._solid_boolean_union(run_outer, branch_transition_outer)
                    _del(branch_transition_outer)
            branch_outer = _cylinder(branch_base, R_outer_branch, branch_total_len, branch_axis_signed)
            if branch_outer is None:
                _del(run_outer)
                return None
            self._solid_boolean_union(run_outer, branch_outer)
            _del(branch_outer)

            if reinforce and reinforce_extra and reinforce_extra > 0:
                reinforce_radius = max(R_outer_run, R_outer_branch) + float(reinforce_extra)
                reinforce_run_len = max(od_run, run_len * max(0.15, min(0.9, reinforce_run_ratio)))
                reinforce_branch_len = max(od_branch * 0.8, branch_visible_len * max(0.15, min(0.9, reinforce_branch_ratio)))

                reinforce_run_base = _vec_sub((cx, cy, cz), _vec_mul(run_dir, reinforce_run_len / 2.0))
                reinforce_run = _cylinder(reinforce_run_base, reinforce_radius, reinforce_run_len, run_axis_signed)
                if reinforce_run:
                    self._solid_boolean_union(run_outer, reinforce_run)
                    _del(reinforce_run)

                reinforce_branch = _cylinder((cx, cy, cz), reinforce_radius, reinforce_branch_len, branch_axis_signed)
                if reinforce_branch:
                    self._solid_boolean_union(run_outer, reinforce_branch)
                    _del(reinforce_branch)

            if junction_bulge and junction_bulge_extra and junction_bulge_extra > 0:
                bulge_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, cz])
                model_space = self._get_model_space()
                bulge = self._call_with_retry(
                    model_space.AddSphere,
                    bulge_center,
                    max(R_outer_run, R_outer_branch) + float(junction_bulge_extra),
                )
                if bulge:
                    self._solid_boolean_union(run_outer, bulge)
                    _del(bulge)

            if R_inner_run > 0 or R_inner_branch > 0:
                bore_eps = max(1.0, run_wt, branch_wt)
            else:
                bore_eps = 0.0
            if R_inner_run > 0:
                run_inner_base = _vec_sub(run_base, _vec_mul(run_dir, bore_eps))
                run_inner = _cylinder(run_inner_base, R_inner_run, run_len + 2.0 * bore_eps, run_axis_signed)
                if run_inner:
                    self._solid_boolean_subtract(run_outer, run_inner)
                    _del(run_inner)
            if R_inner_branch > 0:
                branch_inner_base = _vec_sub((cx, cy, cz), _vec_mul(branch_dir, bore_eps))
                branch_inner = _cylinder(
                    branch_inner_base,
                    R_inner_branch,
                    branch_visible_len + R_outer_run + 2.0 * bore_eps,
                    branch_axis_signed,
                )
                if branch_inner:
                    self._solid_boolean_subtract(run_outer, branch_inner)
                    _del(branch_inner)

            if R_inner_run > 0 or R_inner_branch > 0:
                # 端口清口只需要覆盖端面附近，不能再用超长贯穿圆柱，
                # 否则支管顶口的清口会一路切穿到三通根部，造成外壳下侧出现假缺口。
                run_port_open_len = max(20.0, bore_eps * 3.0, run_wt * 2.0)
                branch_port_open_len = max(20.0, bore_eps * 3.0, branch_wt * 2.0)
                if R_inner_run > 0:
                    run_port_radius = max(0.1, R_inner_run - 0.5)
                    left_port_base = _vec_sub((cx, cy, cz), _vec_mul(run_dir, run_half_len + run_port_open_len))
                    left_port = _cylinder(left_port_base, run_port_radius, run_port_open_len * 2.0, run_axis_signed)
                    if left_port:
                        self._solid_boolean_subtract(run_outer, left_port)
                        _del(left_port)

                    right_port_base = _vec_sub(
                        _vec_add((cx, cy, cz), _vec_mul(run_dir, run_half_len)),
                        _vec_mul(run_dir, run_port_open_len),
                    )
                    right_port = _cylinder(right_port_base, run_port_radius, run_port_open_len * 2.0, run_axis_signed)
                    if right_port:
                        self._solid_boolean_subtract(run_outer, right_port)
                        _del(right_port)

                if R_inner_branch > 0:
                    branch_port_radius = max(0.1, R_inner_branch - 0.5)
                    branch_outlet_end = _vec_add(branch_base, _vec_mul(branch_dir, branch_total_len))
                    top_port_base = _vec_sub(branch_outlet_end, _vec_mul(branch_dir, branch_port_open_len))
                    top_port = _cylinder(top_port_base, branch_port_radius, branch_port_open_len * 2.0, branch_axis_signed)
                    if top_port:
                        self._solid_boolean_subtract(run_outer, top_port)
                        _del(top_port)

            if layer:
                self.create_layer(layer)
                run_outer.Layer = layer
            if color is not None:
                run_outer.Color = color
            self._finalize_entity_display(run_outer)
            self.refresh_view()
            logger.debug(
                "已绘制%s三通: 中心%s, DN%s×DN%s, 主管%s 支管%s, "
                "C_gb=%s C_display=%s M=%s, 主管壁厚%s 支管壁厚%s, merge_model=True",
                "等径" if is_equal_tee else "异径",
                center,
                dn,
                branch_dn_resolved,
                run_axis_signed,
                branch_axis_signed,
                C,
                run_half_len,
                M,
                run_wt,
                branch_wt,
            )
            return run_outer
        except Exception as e:
            logger.error(f"绘制三通时出错: {str(e)}")
            return None

    def draw_wedge(self, center: Tuple[float, float, float], length: float, width: float, height: float,
                   layer: str = None, color: int = None) -> Any:
        """
        绘制楔体（3D实体）
        
        Args:
            center: 底面角点 (x, y, z)
            length: 长度
            width: 宽度
            height: 高度
            layer: 图层名称（可选）
            color: 颜色索引（可选）
            
        Returns:
            创建的 3DSolid 对象，失败返回 None
        """
        if not self.is_running():
            return None
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0)
            center_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [center[0], center[1], center[2]])
            wedge = self.doc.ModelSpace.AddWedge(center_array, length, width, height)
            if layer:
                self.create_layer(layer)
                wedge.Layer = layer
            if color is not None:
                wedge.Color = color
            self.refresh_view()
            logger.debug(f"已绘制楔体: 中心{center}, 长{length} 宽{width} 高{height}")
            return wedge
        except Exception as e:
            logger.error(f"绘制楔体时出错: {str(e)}")
            return None

    # -------------------------------------------------------------------------
    # 国标 GB/T 9119-2010 板式平焊钢制管法兰 尺寸表（单位 mm）
    # 键: (DN, PN)，值: D=外径, K=螺栓孔中心圆直径, L=螺栓孔直径, n=螺栓孔数, C=法兰厚度, d=内径(中心孔)
    # 数据来源：GB/T 9119-2010 平面、突面板式平焊钢制管法兰
    # -------------------------------------------------------------------------
    FLANGE_GB9119 = {
        (10, 2.5): {"D": 75, "K": 50, "L": 11, "n": 4, "C": 12, "d": 18},
        (15, 2.5): {"D": 80, "K": 55, "L": 11, "n": 4, "C": 12, "d": 22},
        (20, 2.5): {"D": 90, "K": 65, "L": 11, "n": 4, "C": 15, "d": 27},
        (25, 2.5): {"D": 100, "K": 75, "L": 11, "n": 4, "C": 15, "d": 34},
        (32, 2.5): {"D": 120, "K": 90, "L": 14, "n": 4, "C": 16, "d": 43},
        (40, 2.5): {"D": 130, "K": 100, "L": 14, "n": 4, "C": 16, "d": 49},
        (50, 2.5): {"D": 140, "K": 110, "L": 14, "n": 4, "C": 16, "d": 61},
        (65, 2.5): {"D": 160, "K": 130, "L": 14, "n": 4, "C": 18, "d": 77},
        (80, 2.5): {"D": 190, "K": 150, "L": 18, "n": 4, "C": 18, "d": 90},
        (100, 2.5): {"D": 210, "K": 170, "L": 18, "n": 4, "C": 18, "d": 116},
        (10, 6): {"D": 90, "K": 60, "L": 14, "n": 4, "C": 14, "d": 18},
        (15, 6): {"D": 95, "K": 65, "L": 14, "n": 4, "C": 14, "d": 22},
        (20, 6): {"D": 105, "K": 75, "L": 14, "n": 4, "C": 16, "d": 27},
        (25, 6): {"D": 115, "K": 85, "L": 14, "n": 4, "C": 16, "d": 34},
        (32, 6): {"D": 140, "K": 100, "L": 18, "n": 4, "C": 18, "d": 43},
        (40, 6): {"D": 150, "K": 110, "L": 18, "n": 4, "C": 18, "d": 49},
        (50, 6): {"D": 165, "K": 125, "L": 18, "n": 4, "C": 20, "d": 61},
        (65, 6): {"D": 185, "K": 145, "L": 18, "n": 4, "C": 22, "d": 77},
        (80, 6): {"D": 200, "K": 160, "L": 18, "n": 8, "C": 22, "d": 90},
        (100, 6): {"D": 220, "K": 180, "L": 18, "n": 8, "C": 24, "d": 116},
        (125, 6): {"D": 250, "K": 210, "L": 18, "n": 8, "C": 26, "d": 143},
        (150, 6): {"D": 285, "K": 240, "L": 22, "n": 8, "C": 28, "d": 169},
        (200, 6): {"D": 340, "K": 295, "L": 22, "n": 12, "C": 30, "d": 221},
        (10, 10): {"D": 90, "K": 60, "L": 14, "n": 4, "C": 14, "d": 18},
        (20, 10): {"D": 105, "K": 75, "L": 14, "n": 4, "C": 16, "d": 27},
        (25, 10): {"D": 115, "K": 85, "L": 14, "n": 4, "C": 16, "d": 34},
        (32, 10): {"D": 140, "K": 100, "L": 18, "n": 4, "C": 18, "d": 43},
        (40, 10): {"D": 150, "K": 110, "L": 18, "n": 4, "C": 18, "d": 49},
        (50, 10): {"D": 165, "K": 125, "L": 18, "n": 4, "C": 20, "d": 61},
        (65, 10): {"D": 185, "K": 145, "L": 18, "n": 4, "C": 22, "d": 77},
        (80, 10): {"D": 200, "K": 160, "L": 18, "n": 8, "C": 22, "d": 90},
        (100, 10): {"D": 220, "K": 180, "L": 18, "n": 8, "C": 24, "d": 116},
        (125, 10): {"D": 250, "K": 210, "L": 18, "n": 8, "C": 26, "d": 143},
        (150, 10): {"D": 285, "K": 240, "L": 22, "n": 8, "C": 28, "d": 169},
        (200, 10): {"D": 340, "K": 295, "L": 22, "n": 12, "C": 30, "d": 221},
        (10, 16): {"D": 90, "K": 60, "L": 14, "n": 4, "C": 14, "d": 18},
        (15, 16): {"D": 95, "K": 65, "L": 14, "n": 4, "C": 14, "d": 22},
        (20, 16): {"D": 105, "K": 75, "L": 14, "n": 4, "C": 16, "d": 27},
        (25, 16): {"D": 115, "K": 85, "L": 14, "n": 4, "C": 16, "d": 34},
        (32, 16): {"D": 140, "K": 100, "L": 18, "n": 4, "C": 18, "d": 43},
        (40, 16): {"D": 150, "K": 110, "L": 18, "n": 4, "C": 18, "d": 49},
        (50, 16): {"D": 165, "K": 125, "L": 18, "n": 4, "C": 20, "d": 61},
        (65, 16): {"D": 185, "K": 145, "L": 18, "n": 4, "C": 22, "d": 77},
        (80, 16): {"D": 200, "K": 160, "L": 18, "n": 8, "C": 22, "d": 90},
        (100, 16): {"D": 220, "K": 180, "L": 18, "n": 8, "C": 24, "d": 116},
        (125, 16): {"D": 250, "K": 210, "L": 18, "n": 8, "C": 26, "d": 143},
        (150, 16): {"D": 285, "K": 240, "L": 22, "n": 8, "C": 28, "d": 169},
        (200, 16): {"D": 340, "K": 295, "L": 22, "n": 12, "C": 30, "d": 221},
        (250, 16): {"D": 405, "K": 355, "L": 26, "n": 12, "C": 32, "d": 273},
        (300, 16): {"D": 460, "K": 410, "L": 26, "n": 12, "C": 36, "d": 325},
    }

    # -------------------------------------------------------------------------
    # 人孔首版标准族参数表（单位 mm）
    # 当前先内置常用的 HG/T 21517 回转盖带颈平焊法兰人孔族，
    # 目的是让 draw_manhole() 具备 standard + dn + pn 的查表驱动能力。
    # 几何表达仍沿用当前已经验证的人孔层次：短颈、法兰圈、盲盖、圆滑 U 形把手。
    # -------------------------------------------------------------------------
    MANHOLE_HG21517 = {
        450: {
            "opening_diameter": 450.0,
            "neck_outer_diameter": 450.0,
            "neck_wall_thickness": 10.0,
            "neck_height": 150.0,
            "flange_outer_diameter": 580.0,
            "bolt_circle_diameter": 500.0,
            "bolt_hole_diameter": 22.0,
            "bolt_count": 16,
            "flange_thickness": 26.0,
            "cover_outer_diameter": 580.0,
            "cover_thickness": 24.0,
            "cover_gap": 10.0,
            "handle_count": 2,
            "handle_width": 180.0,
            "handle_height": 65.0,
            "handle_bar_diameter": 16.0,
            "add_lifting_lug": False,
        },
        500: {
            "opening_diameter": 500.0,
            "neck_outer_diameter": 500.0,
            "neck_wall_thickness": 10.0,
            "neck_height": 170.0,
            "flange_outer_diameter": 640.0,
            "bolt_circle_diameter": 560.0,
            "bolt_hole_diameter": 24.0,
            "bolt_count": 16,
            "flange_thickness": 28.0,
            "cover_outer_diameter": 640.0,
            "cover_thickness": 26.0,
            "cover_gap": 10.0,
            "handle_count": 2,
            "handle_width": 205.0,
            "handle_height": 70.0,
            "handle_bar_diameter": 18.0,
            "add_lifting_lug": False,
        },
        600: {
            "opening_diameter": 600.0,
            "neck_outer_diameter": 600.0,
            "neck_wall_thickness": 12.0,
            "neck_height": 185.0,
            "flange_outer_diameter": 760.0,
            "bolt_circle_diameter": 660.0,
            "bolt_hole_diameter": 26.0,
            "bolt_count": 20,
            "flange_thickness": 32.0,
            "cover_outer_diameter": 760.0,
            "cover_thickness": 30.0,
            "cover_gap": 12.0,
            "handle_count": 2,
            "handle_width": 230.0,
            "handle_height": 75.0,
            "handle_bar_diameter": 18.0,
            "add_lifting_lug": False,
        },
    }

    # -------------------------------------------------------------------------
    # 紧固件国标尺寸表（单位 mm）
    # GB/T 5782-2016、GB/T 5783-2016 六角头螺栓：d=螺纹公称直径, s=对边宽, k=头高
    # GB/T 6170.1-2016 1型六角螺母：d=螺纹公称直径, s=对边宽, m=厚度
    # GB/T 97.1-2002 平垫圈：d=适配螺纹公称直径, d1=内径, d2=外径, h=厚度
    # 当前内置常用规格，后续可按同结构继续扩展
    # -------------------------------------------------------------------------
    FASTENER_BOLT_GB5782 = {
        "M6": {"d": 6.0, "s": 10.0, "k": 4.0},
        "M8": {"d": 8.0, "s": 13.0, "k": 5.3},
        "M10": {"d": 10.0, "s": 16.0, "k": 6.4},
        "M12": {"d": 12.0, "s": 18.0, "k": 7.5},
        "M16": {"d": 16.0, "s": 24.0, "k": 10.0},
        "M20": {"d": 20.0, "s": 30.0, "k": 12.5},
        "M24": {"d": 24.0, "s": 36.0, "k": 15.0},
    }
    FASTENER_NUT_GB6170_1 = {
        "M6": {"d": 6.0, "s": 10.0, "m": 5.2},
        "M8": {"d": 8.0, "s": 13.0, "m": 6.8},
        "M10": {"d": 10.0, "s": 16.0, "m": 8.4},
        "M12": {"d": 12.0, "s": 18.0, "m": 10.8},
        "M16": {"d": 16.0, "s": 24.0, "m": 14.8},
        "M20": {"d": 20.0, "s": 30.0, "m": 18.0},
        "M24": {"d": 24.0, "s": 36.0, "m": 21.5},
    }
    FASTENER_WASHER_GB97_1 = {
        "M6": {"d": 6.0, "d1": 6.4, "d2": 12.0, "h": 1.6},
        "M8": {"d": 8.0, "d1": 8.4, "d2": 16.0, "h": 1.6},
        "M10": {"d": 10.0, "d1": 10.5, "d2": 20.0, "h": 2.0},
        "M12": {"d": 12.0, "d1": 13.0, "d2": 24.0, "h": 2.5},
        "M16": {"d": 16.0, "d1": 17.0, "d2": 30.0, "h": 3.0},
        "M20": {"d": 20.0, "d1": 21.0, "d2": 37.0, "h": 3.0},
        "M24": {"d": 24.0, "d1": 25.0, "d2": 44.0, "h": 4.0},
    }

    # -------------------------------------------------------------------------
    # 球阀结构长度（单位 mm），参考 GB/T 12221 金属阀门结构长度、GB/T 12237 钢制球阀
    # -------------------------------------------------------------------------
    VALVE_BALL_STRUCTURE_LENGTH = {
        15: 130, 20: 150, 25: 160, 32: 180, 40: 200, 50: 220, 65: 250, 80: 280,
        100: 300, 125: 350, 150: 400, 200: 500, 250: 600, 300: 700,
    }

    # -------------------------------------------------------------------------
    # 蝶阀结构长度（单位 mm），参考 GB/T 12221、GB/T 12238 通用阀门 法兰和对夹连接蝶阀
    # -------------------------------------------------------------------------
    VALVE_BUTTERFLY_STRUCTURE_LENGTH = {
        40: 43, 50: 43, 65: 46, 80: 50, 100: 56, 125: 64, 150: 70, 200: 71,
        250: 76, 300: 83, 350: 92, 400: 102, 500: 114, 600: 127,
    }

    # -------------------------------------------------------------------------
    # 截止阀结构长度（单位 mm），参考 GB/T 12221、GB/T 12235 钢制截止阀和升降式止回阀
    # -------------------------------------------------------------------------
    VALVE_GLOBE_STRUCTURE_LENGTH = {
        15: 130, 20: 150, 25: 160, 32: 180, 40: 200, 50: 230, 65: 290, 80: 310,
        100: 350, 125: 400, 150: 450, 200: 550, 250: 650, 300: 750,
    }

    # -------------------------------------------------------------------------
    # 旋启式止回阀结构长度（单位 mm），参考 GB/T 12221、GB/T 12236
    # 当前优先固化已核实的 DN250 PN16 系列长度；其他口径缺省走保守估算
    # -------------------------------------------------------------------------
    VALVE_CHECK_STRUCTURE_LENGTH = {
        250: 730,
    }

    # -------------------------------------------------------------------------
    # 一般压力表外形参数（单位 mm），参考 GB/T 1226 常用 Y-60 / Y-100 / Y-150 系列
    # center 语义为表盘中心，face_axis 语义为表盘法向
    # -------------------------------------------------------------------------
    PRESSURE_GAUGE_GB1226 = {
        60: {
            "case_outer_diameter": 60.0,
            "case_body_diameter": 56.0,
            "case_depth": 28.0,
            "thread_size": "M14x1.5",
            "thread_major_diameter": 14.0,
            "thread_length": 18.0,
            "socket_outer_diameter": 16.0,
            "socket_length": 10.0,
            "hex_width_across_flats": 19.0,
            "panel_flange_outer_diameter": 72.0,
            "panel_bolt_circle_diameter": 64.0,
            "panel_bolt_hole_diameter": 4.5,
            "panel_bolt_count": 3,
        },
        100: {
            "case_outer_diameter": 100.0,
            "case_body_diameter": 90.0,
            "case_depth": 42.0,
            "thread_size": "M20x1.5",
            "thread_major_diameter": 20.0,
            "thread_length": 20.0,
            "socket_outer_diameter": 24.0,
            "socket_length": 12.0,
            "hex_width_across_flats": 27.0,
            "panel_flange_outer_diameter": 118.0,
            "panel_bolt_circle_diameter": 108.0,
            "panel_bolt_hole_diameter": 6.0,
            "panel_bolt_count": 3,
        },
        150: {
            "case_outer_diameter": 150.0,
            "case_body_diameter": 138.0,
            "case_depth": 44.0,
            "thread_size": "M20x1.5",
            "thread_major_diameter": 20.0,
            "thread_length": 20.0,
            "socket_outer_diameter": 28.0,
            "socket_length": 14.0,
            "hex_width_across_flats": 30.0,
            "panel_flange_outer_diameter": 170.0,
            "panel_bolt_circle_diameter": 156.0,
            "panel_bolt_hole_diameter": 6.0,
            "panel_bolt_count": 3,
        },
    }

    # -------------------------------------------------------------------------
    # 刚性防水套管（单位 mm），参考 02S404 常用 A 型钢管穿墙套管
    # 参数直接摘录自用户提供的“刚性防水套管（A型）尺寸、重量表”截图：
    # D1/D2/D3/D4/δ/b/K/重量。截图未包含 L，默认绘制长度继续取 200，可由 length 覆盖。
    # 当前绘制几何优先使用 D3 + δ 反算套管内径；D2、K 和重量作为图表原始台账保留。
    # -------------------------------------------------------------------------
    RIGID_WATERPROOF_SLEEVE_02S404_A = {
        50: {
            "pipe_outer_diameter": 60.0,
            "table_d2_diameter": 80.0,
            "sleeve_outer_diameter": 114.0,
            "wing_ring_outer_diameter": 225.0,
            "sleeve_wall_thickness": 3.5,
            "wing_ring_thickness": 10.0,
            "table_k": 4,
            "reference_weight_kg": 4.49,
            "default_length": 200.0,
        },
        65: {
            "pipe_outer_diameter": 75.5,
            "table_d2_diameter": 95.0,
            "sleeve_outer_diameter": 121.0,
            "wing_ring_outer_diameter": 230.0,
            "sleeve_wall_thickness": 3.75,
            "wing_ring_thickness": 10.0,
            "table_k": 4,
            "reference_weight_kg": 4.66,
            "default_length": 200.0,
        },
        80: {
            "pipe_outer_diameter": 89.0,
            "table_d2_diameter": 110.0,
            "sleeve_outer_diameter": 140.0,
            "wing_ring_outer_diameter": 250.0,
            "sleeve_wall_thickness": 4.0,
            "wing_ring_thickness": 10.0,
            "table_k": 4,
            "reference_weight_kg": 5.33,
            "default_length": 200.0,
        },
        100: {
            "pipe_outer_diameter": 108.0,
            "table_d2_diameter": 130.0,
            "sleeve_outer_diameter": 159.0,
            "wing_ring_outer_diameter": 270.0,
            "sleeve_wall_thickness": 4.5,
            "wing_ring_thickness": 10.0,
            "table_k": 5,
            "reference_weight_kg": 6.36,
            "default_length": 200.0,
        },
        125: {
            "pipe_outer_diameter": 133.0,
            "table_d2_diameter": 155.0,
            "sleeve_outer_diameter": 180.0,
            "wing_ring_outer_diameter": 290.0,
            "sleeve_wall_thickness": 5.0,
            "wing_ring_thickness": 10.0,
            "table_k": 6,
            "reference_weight_kg": 8.33,
            "default_length": 200.0,
        },
        150: {
            "pipe_outer_diameter": 159.0,
            "table_d2_diameter": 180.0,
            "sleeve_outer_diameter": 219.0,
            "wing_ring_outer_diameter": 330.0,
            "sleeve_wall_thickness": 6.0,
            "wing_ring_thickness": 10.0,
            "table_k": 6,
            "reference_weight_kg": 10.06,
            "default_length": 200.0,
        },
        200: {
            "pipe_outer_diameter": 219.0,
            "table_d2_diameter": 240.0,
            "sleeve_outer_diameter": 273.0,
            "wing_ring_outer_diameter": 385.0,
            "sleeve_wall_thickness": 8.0,
            "wing_ring_thickness": 12.0,
            "table_k": 8,
            "reference_weight_kg": 15.90,
            "default_length": 200.0,
        },
        250: {
            "pipe_outer_diameter": 273.0,
            "table_d2_diameter": 295.0,
            "sleeve_outer_diameter": 325.0,
            "wing_ring_outer_diameter": 435.0,
            "sleeve_wall_thickness": 8.0,
            "wing_ring_thickness": 12.0,
            "table_k": 8,
            "reference_weight_kg": 18.68,
            "default_length": 200.0,
        },
        300: {
            "pipe_outer_diameter": 325.0,
            "table_d2_diameter": 345.0,
            "sleeve_outer_diameter": 377.0,
            "wing_ring_outer_diameter": 500.0,
            "sleeve_wall_thickness": 10.0,
            "wing_ring_thickness": 14.0,
            "table_k": 10,
            "reference_weight_kg": 27.40,
            "default_length": 200.0,
        },
        350: {
            "pipe_outer_diameter": 377.0,
            "table_d2_diameter": 400.0,
            "sleeve_outer_diameter": 426.0,
            "wing_ring_outer_diameter": 550.0,
            "sleeve_wall_thickness": 10.0,
            "wing_ring_thickness": 14.0,
            "table_k": 10,
            "reference_weight_kg": 30.95,
            "default_length": 200.0,
        },
        400: {
            "pipe_outer_diameter": 426.0,
            "table_d2_diameter": 445.0,
            "sleeve_outer_diameter": 480.0,
            "wing_ring_outer_diameter": 600.0,
            "sleeve_wall_thickness": 10.0,
            "wing_ring_thickness": 14.0,
            "table_k": 10,
            "reference_weight_kg": 34.35,
            "default_length": 200.0,
        },
        450: {
            "pipe_outer_diameter": 480.0,
            "table_d2_diameter": 500.0,
            "sleeve_outer_diameter": 530.0,
            "wing_ring_outer_diameter": 650.0,
            "sleeve_wall_thickness": 10.0,
            "wing_ring_thickness": 14.0,
            "table_k": 10,
            "reference_weight_kg": 37.85,
            "default_length": 200.0,
        },
    }

    # -------------------------------------------------------------------------
    # 钢制喇叭口（单位 mm），参考 02S403 吸水喇叭口系列
    # 公开资料可确认 DN×壁厚系列；当未取得完整 D/d/L 表时，按系列常见比例建模
    # -------------------------------------------------------------------------
    STEEL_BELLMOUTH_02S403 = {
        100: {"wall_thickness": 6.0},
        150: {"wall_thickness": 6.0},
        200: {"wall_thickness": 6.0},
        250: {"wall_thickness": 6.0},
        300: {"wall_thickness": 6.0},
        400: {"wall_thickness": 6.0},
        500: {"wall_thickness": 6.0},
        600: {"wall_thickness": 10.0},
        700: {"wall_thickness": 10.0},
        800: {"wall_thickness": 10.0},
        900: {"wall_thickness": 10.0},
        1000: {"wall_thickness": 10.0},
        1200: {"wall_thickness": 12.0},
        1400: {"wall_thickness": 12.0},
    }

    # -------------------------------------------------------------------------
    # 吸水喇叭管支架（单位 mm），参考 02S403 ZA/ZB/ZC/ZD 系列
    # 当前优先固化 ZB3 的公开规格 φ426×φ630；细部板厚和高度按工程稳定比例表达
    # -------------------------------------------------------------------------
    SUCTION_BELLMOUTH_SUPPORT_02S403 = {
        ("ZB3", "J", "B"): {
            "small_diameter": 426.0,
            "large_diameter": 630.0,
            "support_height": 220.0,
            "shell_thickness": 10.0,
            "ring_band_width": 18.0,
            "middle_ring_ratio": 0.55,
        },
    }

    # -------------------------------------------------------------------------
    # 双法兰限位伸缩接头（单位 mm），参考 GB/T 12465、VSSJA-2(B2F) / JSSJA-2 常用系列
    # 当前优先固化已核实的 DN250 PN1.0 关键尺寸；部分非公开外形细节采用工程比例表达
    # -------------------------------------------------------------------------
    DOUBLE_FLANGE_LIMIT_EXPANSION_JOINT_MODEL_ALIASES = {
        "jssja-2": "VSSJA-2(B2F)",
        "vssja-2": "VSSJA-2(B2F)",
        "b2f": "VSSJA-2(B2F)",
        "vssja-2(b2f)": "VSSJA-2(B2F)",
    }
    DOUBLE_FLANGE_LIMIT_EXPANSION_JOINT_GBT12465 = {
        ("VSSJA-2(B2F)", 250, 1.0): {
            "pipe_outer_diameter": 273.0,
            "overall_length": 395.0,
            "installation_length": 350.0,
            "center_barrel_length": 130.0,
            "expansion_allowance": 22.0,
            "flange_outer_diameter": 395.0,
            "flange_bolt_circle_diameter": 350.0,
            "flange_bolt_hole_diameter": 23.0,
            "flange_bolt_count": 12,
            "flange_thickness": 30.0,
            "shell_outer_diameter": 335.0,
            "gland_outer_diameter": 315.0,
            "gland_length": 44.0,
            "tie_rod_count": 4,
            "tie_rod_thread_size": "M20",
            "tie_rod_length_extra": 80.0,
            "nominal_bore": 250.0,
        },
        ("VSSJA-2(B2F)", 300, 1.0): {
            "pipe_outer_diameter": 325.0,
            "overall_length": 370.0,
            "installation_length": 320.0,
            "center_barrel_length": 140.0,
            "expansion_allowance": 65.0,
            "flange_outer_diameter": 445.0,
            "flange_bolt_circle_diameter": 400.0,
            "flange_bolt_hole_diameter": 23.0,
            "flange_bolt_count": 12,
            "flange_thickness": 30.0,
            "shell_outer_diameter": 385.0,
            "gland_outer_diameter": 365.0,
            "gland_length": 46.0,
            "tie_rod_count": 4,
            "tie_rod_thread_size": "M20",
            "tie_rod_length_extra": 90.0,
            "nominal_bore": 300.0,
        },
    }

    # -------------------------------------------------------------------------
    # 椭圆封头 直边高度（单位 mm），参考 GB/T 25198 压力容器封头
    # 公称直径 DN≤2000 直边高度 25，DN>2000 为 40
    # -------------------------------------------------------------------------
    ELLIPTICAL_HEAD_STRAIGHT_HEIGHT = 25  # DN≤2000
    ELLIPTICAL_HEAD_STRAIGHT_HEIGHT_LARGE = 40  # DN>2000

    def _get_flange_params(self, dn: int, pn: float) -> Optional[Dict[str, float]]:
        """从国标法兰表获取 DN、PN 对应尺寸，单位 mm。"""
        key = (int(dn), float(pn))
        if key not in self.FLANGE_GB9119:
            return None
        row = self.FLANGE_GB9119[key]
        return {
            "D": float(row["D"]), "K": float(row["K"]), "L": float(row["L"]),
            "n": int(row["n"]), "C": float(row["C"]), "d": float(row["d"]),
        }

    def _normalize_metric_thread_size(self, thread_size: Union[str, int, float, None]) -> Optional[str]:
        """将螺纹规格统一为 Mxx 形式，例如 16 -> M16，"m12" -> M12。"""
        if thread_size is None:
            return None
        if isinstance(thread_size, str):
            raw = thread_size.strip().upper().replace(" ", "")
            if not raw:
                return None
            if raw.startswith("M"):
                raw = raw[1:]
            try:
                value = float(raw)
            except ValueError:
                return None
        else:
            value = float(thread_size)
        if value <= 0:
            return None
        if abs(value - round(value)) < 1e-6:
            return f"M{int(round(value))}"
        return f"M{value:g}"

    def _normalize_standard_code(self, specification: Optional[str]) -> str:
        """标准号归一化，便于做别名兼容。"""
        return (specification or "").upper().replace(" ", "").replace("-", "").replace("_", "")

    def _get_bolt_standard_params(self, specification: Optional[str], thread_size: Union[str, int, float, None]) -> Optional[Dict[str, float]]:
        """按国标和螺纹规格获取六角头螺栓参数。"""
        size_key = self._normalize_metric_thread_size(thread_size)
        spec_key = self._normalize_standard_code(specification)
        if not size_key:
            return None
        if spec_key in ("GB/T5782", "GBT5782", "GB/T5783", "GBT5783"):
            row = self.FASTENER_BOLT_GB5782.get(size_key)
            if row is None:
                return None
            return {"thread_size": size_key, "d": float(row["d"]), "s": float(row["s"]), "k": float(row["k"])}
        return None

    def _get_nut_standard_params(self, specification: Optional[str], thread_size: Union[str, int, float, None]) -> Optional[Dict[str, float]]:
        """按国标和螺纹规格获取六角螺母参数。"""
        size_key = self._normalize_metric_thread_size(thread_size)
        spec_key = self._normalize_standard_code(specification)
        if not size_key:
            return None
        if spec_key in ("GB/T6170.1", "GBT6170.1", "GB/T61701", "GBT61701", "GB/T6170", "GBT6170"):
            row = self.FASTENER_NUT_GB6170_1.get(size_key)
            if row is None:
                return None
            return {"thread_size": size_key, "d": float(row["d"]), "s": float(row["s"]), "m": float(row["m"])}
        return None

    def _get_washer_standard_params(self, specification: Optional[str], thread_size: Union[str, int, float, None]) -> Optional[Dict[str, float]]:
        """按国标和螺纹规格获取平垫圈参数。"""
        size_key = self._normalize_metric_thread_size(thread_size)
        spec_key = self._normalize_standard_code(specification)
        if not size_key:
            return None
        if spec_key in ("GB/T97.1", "GBT97.1", "GB/T971", "GBT971"):
            row = self.FASTENER_WASHER_GB97_1.get(size_key)
            if row is None:
                return None
            return {
                "thread_size": size_key,
                "d": float(row["d"]),
                "d1": float(row["d1"]),
                "d2": float(row["d2"]),
                "h": float(row["h"]),
            }
        return None

    def _get_manhole_standard_params(self, specification: Optional[str], dn: Optional[int], pn: Optional[float]) -> Optional[Dict[str, float]]:
        """按人孔标准族获取首版参数表。"""

        spec_key = self._normalize_standard_code(specification)
        if spec_key not in ("HG/T21517", "HGT21517", "HG21517"):
            return None
        if dn is None:
            return None
        dn_value = int(dn)
        row = self.MANHOLE_HG21517.get(dn_value)
        if row is None:
            return None
        if pn is not None and float(pn) not in (0.6, 1.0, 1.6):
            return None
        resolved = dict(row)
        resolved["standard"] = "HG/T 21517"
        resolved["dn"] = dn_value
        resolved["pn"] = float(pn) if pn is not None else 1.0
        return resolved

    def _draw_compact_ball_valve(
        self,
        center: Tuple[float, float, float],
        dn: int,
        pn: float,
        axis: str = "x",
        layer: str = None,
        color: int = None,
        connection_type: str = "threaded",
    ) -> Any:
        """绘制更接近三视图样式的紧凑式球阀实体。"""
        if not self.is_running():
            return None
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0.0)
            cx, cy, cz = center
            dn = int(dn)
            od = float(self.ELBOW_DN_OD_GB12459.get(dn, max(float(dn) * 1.22, float(dn) + 8.0)))
            wall_thickness = float(self.ELBOW_WALL_THICKNESS_GB.get(dn, max(2.0, od * 0.075)))
            pipe_outer_radius = od / 2.0
            bore_radius = max(6.0, pipe_outer_radius - wall_thickness)
            flanged_face_to_face = float(self.VALVE_BALL_STRUCTURE_LENGTH.get(dn, 200 + dn))

            connection_key = (connection_type or "threaded").strip().lower().replace("-", "_").replace(" ", "_")
            if connection_key in {"socket", "socket_weld", "socket_welding", "sw"}:
                connection_key = "socket_welding"
            elif connection_key in {"butt", "butt_weld", "butt_welding", "bw"}:
                connection_key = "butt_welding"
            elif connection_key not in {"threaded", "socket_welding", "butt_welding"}:
                connection_key = "threaded"

            face_to_face_scale = {
                "threaded": 0.62,
                "socket_welding": 0.68,
                "butt_welding": 0.74,
            }.get(connection_key, 0.62)
            face_to_face = max(od * 3.2, flanged_face_to_face * face_to_face_scale)
            half = face_to_face / 2.0

            body_radius = max(pipe_outer_radius * 1.55, face_to_face * 0.24)
            body_barrel_len = max(od * 1.20, face_to_face * 0.30)
            side_chamber_len = max(od * 0.44, face_to_face * 0.16)
            side_tail_len = max(12.0, half - body_barrel_len / 2.0 - side_chamber_len)
            side_transition_len = max(8.0, side_tail_len * 0.45)
            side_pipe_len = max(8.0, side_tail_len - side_transition_len)
            end_port_radius = max(pipe_outer_radius * 1.22, bore_radius * 1.42)
            tail_radius = {
                "threaded": pipe_outer_radius * 1.08,
                "socket_welding": pipe_outer_radius * 1.04,
                "butt_welding": pipe_outer_radius * 0.96,
            }.get(connection_key, pipe_outer_radius * 1.08)

            seat_outer_radius = min(body_radius * 0.84, bore_radius + max(4.0, body_radius * 0.12))
            seat_thickness = max(4.0, body_barrel_len * 0.11)
            ball_radius = min(body_radius * 0.90, max(bore_radius * 1.18, body_radius * 0.70))
            split_plate_depth = max(6.0, body_radius * 0.24)
            body_bolt_radius = max(2.6, body_radius * 0.08)
            body_bolt_span = max(od * 1.10, body_barrel_len * 0.72)
            body_bolt_offset_y = body_radius * 0.70

            top_pad_width = max(body_radius * 1.80, od * 1.22)
            top_pad_depth = max(body_radius * 1.28, od * 0.90)
            top_pad_thickness = max(8.0, body_radius * 0.22)
            top_pad_center_z = cz + body_radius * 0.82
            bonnet_radius = max(bore_radius * 0.82, body_radius * 0.42)
            bonnet_height = max(18.0, body_radius * 0.48)
            gland_radius = bonnet_radius * 0.76
            gland_height = max(8.0, bonnet_height * 0.36)
            gland_plate_width = top_pad_width * 0.92
            gland_plate_depth = top_pad_depth * 0.86
            gland_plate_thickness = max(6.0, top_pad_thickness * 0.72)
            gland_bolt_radius = max(2.2, body_radius * 0.055)
            gland_bolt_offset_x = gland_plate_width * 0.34
            gland_bolt_offset_y = gland_plate_depth * 0.34

            stem_radius = max(4.0, bore_radius * 0.22)
            stem_bottom_z = cz - ball_radius * 0.10
            stem_top_z = (
                top_pad_center_z
                + top_pad_thickness / 2.0
                + bonnet_height
                + gland_height
                + gland_plate_thickness
                + max(14.0, body_radius * 0.18)
            )
            handle_hub_radius = max(6.0, stem_radius * 1.35)
            handle_length = max(90.0, body_radius * 2.60)
            handle_width = max(12.0, body_radius * 0.28)
            handle_thickness = max(6.0, handle_width * 0.56)
            handle_center_x = cx + handle_length * 0.34
            handle_z = stem_top_z - handle_thickness * 0.45

            thread_flat_size = max(tail_radius * 3.2, od * 1.04)
            thread_flat_len = max(10.0, side_transition_len * 0.58)

            def _del(solid):
                try:
                    solid.Delete()
                except Exception:
                    pass

            def _track(solid):
                if solid is not None:
                    created_entities.append(solid)
                return solid

            def _axis_dir(axis_signed_str: str) -> Tuple[str, int]:
                value = (axis_signed_str or "x").lower().strip()
                if value.startswith("-"):
                    return value[1:], -1
                return value, 1

            def _rotate_solid_from_z(solid, rotate_center: Tuple[float, float, float], axis_signed_str: str):
                axis_name, sign = _axis_dir(axis_signed_str)
                if axis_name not in ("x", "y", "z"):
                    return
                px, py, pz = rotate_center
                pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz])
                if axis_name == "z":
                    if sign < 0:
                        pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1.0, py, pz])
                        solid.Rotate3D(pt_center, pt_x, math.pi)
                    return
                if axis_name == "x":
                    pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
                    solid.Rotate3D(pt_center, pt_y, -math.pi / 2 if sign > 0 else math.pi / 2)
                    return
                pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1.0, py, pz])
                solid.Rotate3D(pt_center, pt_x, -math.pi / 2 if sign > 0 else math.pi / 2)

            def _rotate_solid_from_x(solid, rotate_center: Tuple[float, float, float], axis_signed_str: str):
                axis_name, sign = _axis_dir(axis_signed_str)
                if axis_name not in ("x", "y", "z"):
                    return
                px, py, pz = rotate_center
                pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz])
                if axis_name == "x":
                    if sign < 0:
                        pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
                        solid.Rotate3D(pt_center, pt_y, math.pi)
                    return
                if axis_name == "y":
                    pt_z = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz + 1.0])
                    solid.Rotate3D(pt_center, pt_z, math.pi / 2 if sign > 0 else -math.pi / 2)
                    return
                pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
                solid.Rotate3D(pt_center, pt_y, math.pi / 2 if sign > 0 else -math.pi / 2)

            def _cylinder(base_pt: Tuple[float, float, float], radius: float, height: float, axis_signed_str: str):
                axis_name, _ = _axis_dir(axis_signed_str)
                if radius <= 0 or height <= 0:
                    return None
                bx, by, bz = base_pt
                if axis_name == "z":
                    center_pt = (bx, by, bz + height / 2.0)
                elif axis_name == "y":
                    center_pt = (bx, by + height / 2.0, bz)
                else:
                    center_pt = (bx + height / 2.0, by, bz)
                center_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(center_pt))
                solid = self._call_with_retry(self._get_model_space().AddCylinder, center_arr, radius, height)
                _rotate_solid_from_z(solid, center_pt, axis_signed_str)
                return solid

            def _box(box_center: Tuple[float, float, float], lx: float, ly: float, lz: float):
                corner = [
                    box_center[0] - lx / 2.0,
                    box_center[1] - ly / 2.0,
                    box_center[2] - lz / 2.0,
                ]
                corner_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, corner)
                return self._get_model_space().AddBox(corner_arr, lx, ly, lz)

            def _union_into(target, solid):
                if solid is None:
                    return
                self._solid_boolean_union(target, solid)
                _del(solid)

            body_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, cz])
            body = self._call_with_retry(self._get_model_space().AddSphere, body_center, body_radius)
            if body is None:
                return None

            barrel = _cylinder((cx - body_barrel_len / 2.0, cy, cz), body_radius * 0.97, body_barrel_len, "x")
            _union_into(body, barrel)

            left_chamber = _cylinder((cx - body_barrel_len / 2.0 - side_chamber_len, cy, cz), end_port_radius, side_chamber_len, "x")
            _union_into(body, left_chamber)
            right_chamber = _cylinder((cx + body_barrel_len / 2.0, cy, cz), end_port_radius, side_chamber_len, "x")
            _union_into(body, right_chamber)

            left_transition = _cylinder((cx - half, cy, cz), end_port_radius, side_transition_len, "x")
            _union_into(body, left_transition)
            left_tail = _cylinder((cx - half + side_transition_len, cy, cz), tail_radius, side_pipe_len, "x")
            _union_into(body, left_tail)
            right_tail = _cylinder((cx + half - side_transition_len - side_pipe_len, cy, cz), tail_radius, side_pipe_len, "x")
            _union_into(body, right_tail)
            right_transition = _cylinder((cx + half - side_transition_len, cy, cz), end_port_radius, side_transition_len, "x")
            _union_into(body, right_transition)

            if connection_key == "threaded":
                left_hex = _box((cx - half + side_transition_len / 2.0, cy, cz), thread_flat_len, thread_flat_size, thread_flat_size * 0.88)
                _union_into(body, left_hex)
                right_hex = _box((cx + half - side_transition_len / 2.0, cy, cz), thread_flat_len, thread_flat_size, thread_flat_size * 0.88)
                _union_into(body, right_hex)
            elif connection_key == "socket_welding":
                left_band = _cylinder((cx - half + side_transition_len * 0.15, cy, cz), end_port_radius * 1.06, side_transition_len * 0.30, "x")
                _union_into(body, left_band)
                right_band = _cylinder((cx + half - side_transition_len * 0.45, cy, cz), end_port_radius * 1.06, side_transition_len * 0.30, "x")
                _union_into(body, right_band)
            else:
                left_neck = _cylinder((cx - half + side_transition_len * 0.35, cy, cz), tail_radius * 0.90, side_transition_len * 0.45, "x")
                _union_into(body, left_neck)
                right_neck = _cylinder((cx + half - side_transition_len * 0.80, cy, cz), tail_radius * 0.90, side_transition_len * 0.45, "x")
                _union_into(body, right_neck)

            full_bore = _cylinder((cx - half - 2.0, cy, cz), bore_radius, face_to_face + 4.0, "x")
            if full_bore is not None:
                self._solid_boolean_subtract(body, full_bore)
                _del(full_bore)

            if connection_key == "socket_welding":
                socket_counterbore_radius = min(end_port_radius * 0.86, bore_radius + wall_thickness * 0.80)
                socket_counterbore_len = max(10.0, side_pipe_len * 0.65)
                left_socket = _cylinder((cx - half - 1.0, cy, cz), socket_counterbore_radius, socket_counterbore_len, "x")
                if left_socket is not None:
                    self._solid_boolean_subtract(body, left_socket)
                    _del(left_socket)
                right_socket = _cylinder((cx + half - socket_counterbore_len + 1.0, cy, cz), socket_counterbore_radius, socket_counterbore_len, "x")
                if right_socket is not None:
                    self._solid_boolean_subtract(body, right_socket)
                    _del(right_socket)

            ball = self._call_with_retry(self._get_model_space().AddSphere, body_center, ball_radius)
            if ball is not None:
                ball_bore = _cylinder((cx - ball_radius - 2.0, cy, cz), bore_radius, ball_radius * 2.0 + 4.0, "x")
                if ball_bore is not None:
                    self._solid_boolean_subtract(ball, ball_bore)
                    _del(ball_bore)
                _union_into(body, ball)

            left_seat = _cylinder((cx - ball_radius * 0.82 - seat_thickness / 2.0, cy, cz), seat_outer_radius, seat_thickness, "x")
            if left_seat is not None:
                left_seat_hole = _cylinder((cx - ball_radius * 0.82 - seat_thickness / 2.0 - 1.0, cy, cz), bore_radius, seat_thickness + 2.0, "x")
                if left_seat_hole is not None:
                    self._solid_boolean_subtract(left_seat, left_seat_hole)
                    _del(left_seat_hole)
                _union_into(body, left_seat)

            right_seat = _cylinder((cx + ball_radius * 0.82 - seat_thickness / 2.0, cy, cz), seat_outer_radius, seat_thickness, "x")
            if right_seat is not None:
                right_seat_hole = _cylinder((cx + ball_radius * 0.82 - seat_thickness / 2.0 - 1.0, cy, cz), bore_radius, seat_thickness + 2.0, "x")
                if right_seat_hole is not None:
                    self._solid_boolean_subtract(right_seat, right_seat_hole)
                    _del(right_seat_hole)
                _union_into(body, right_seat)

            split_plate = _box((cx, cy, cz), split_plate_depth, body_radius * 1.84, body_radius * 1.60)
            _union_into(body, split_plate)

            for sign_x in (-1.0, 1.0):
                for sign_y in (-1.0, 1.0):
                    bolt_x = cx + sign_x * body_bolt_span / 2.0
                    bolt_y = cy + sign_y * body_bolt_offset_y
                    body_bolt = _cylinder((bolt_x, bolt_y - split_plate_depth * 0.55, cz), body_bolt_radius, split_plate_depth * 1.10, "y")
                    _union_into(body, body_bolt)

            top_pad = _box((cx, cy, top_pad_center_z), top_pad_width, top_pad_depth, top_pad_thickness)
            _union_into(body, top_pad)

            bonnet = _cylinder((cx, cy, top_pad_center_z + top_pad_thickness / 2.0), bonnet_radius, bonnet_height, "z")
            _union_into(body, bonnet)
            gland = _cylinder((cx, cy, top_pad_center_z + top_pad_thickness / 2.0 + bonnet_height), gland_radius, gland_height, "z")
            _union_into(body, gland)

            gland_plate_z = top_pad_center_z + top_pad_thickness / 2.0 + bonnet_height + gland_height + gland_plate_thickness / 2.0
            gland_plate = _box((cx, cy, gland_plate_z), gland_plate_width, gland_plate_depth, gland_plate_thickness)
            _union_into(body, gland_plate)

            for sign_x in (-1.0, 1.0):
                for sign_y in (-1.0, 1.0):
                    gbx = cx + sign_x * gland_bolt_offset_x
                    gby = cy + sign_y * gland_bolt_offset_y
                    gland_bolt = _cylinder(
                        (gbx, gby, gland_plate_z - gland_plate_thickness / 2.0),
                        gland_bolt_radius,
                        gland_plate_thickness + gland_height * 0.90,
                        "z",
                    )
                    _union_into(body, gland_bolt)

            stem = _cylinder((cx, cy, stem_bottom_z), stem_radius, stem_top_z - stem_bottom_z, "z")
            _union_into(body, stem)

            handle_hub = _cylinder((cx, cy, handle_z - handle_thickness * 0.36), handle_hub_radius, handle_thickness * 0.72, "z")
            _union_into(body, handle_hub)
            handle = _box((handle_center_x, cy, handle_z), handle_length, handle_width, handle_thickness)
            _union_into(body, handle)

            _rotate_solid_from_x(body, (cx, cy, cz), axis or "x")

            if layer:
                self.create_layer(layer)
                body.Layer = layer
            if color is not None:
                body.Color = color
            self._finalize_entity_display(body)
            self.refresh_view()
            logger.debug(
                "已绘制紧凑式球阀: 中心%s, DN%s PN%s, connection=%s, face_to_face=%s",
                center,
                dn,
                pn,
                connection_key,
                face_to_face,
            )
            return body
        except Exception as exc:
            logger.error(f"绘制紧凑式球阀时出错: {str(exc)}")
            return None

    def draw_ball_valve(
        self,
        center: Tuple[float, float, float],
        dn: int,
        pn: float,
        axis: str = "x",
        layer: str = None,
        color: int = None,
        connection_type: str = "flanged",
    ) -> Any:
        """
        绘制球阀（3D 实体，开启状态）。依据 GB/T 12237、GB/T 12221。

        - connection_type="flanged"：保持原有法兰球阀表达
        - connection_type="threaded" | "socket_welding" | "butt_welding"：
          生成更接近三视图样式的紧凑式上装球阀
        """
        if not self.is_running():
            return None
        connection_key = (connection_type or "flanged").strip().lower().replace("-", "_").replace(" ", "_")
        if connection_key in {"socket", "socket_weld", "socket_welding", "sw"}:
            connection_key = "socket_welding"
        elif connection_key in {"butt", "butt_weld", "butt_welding", "bw"}:
            connection_key = "butt_welding"
        elif connection_key not in {"flanged", "threaded", "socket_welding", "butt_welding"}:
            connection_key = "flanged"
        if connection_key != "flanged":
            return self._draw_compact_ball_valve(
                center,
                dn,
                pn,
                axis=axis,
                layer=layer,
                color=color,
                connection_type=connection_key,
            )
        fp = self._get_flange_params(dn, pn)
        if not fp:
            logger.error(f"draw_ball_valve: 未找到法兰规格 DN{dn} PN{pn}")
            return None
        L = self.VALVE_BALL_STRUCTURE_LENGTH.get(dn)
        if L is None:
            L = 200 + dn
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0.0)
            cx, cy, cz = center
            D, d = fp["D"], fp["d"]
            flange_thickness = max(8.0, min(float(fp["C"]), 24.0))
            flange_radius = D / 2.0
            bore_radius = d / 2.0
            flange_thickness = max(8.0, min(float(fp["C"]), 24.0))
            face_to_face = float(L)
            half = face_to_face / 2.0

            body_barrel_radius = min(flange_radius * 0.70, face_to_face * 0.22)
            body_barrel_len = max(bore_radius * 2.2, face_to_face * 0.34)
            side_neck_len = max(8.0, half - flange_thickness - body_barrel_len / 2.0)
            end_cap_radius = max(body_barrel_radius * 0.82, bore_radius * 1.10)
            ball_radius = min(body_barrel_radius * 0.92, max(bore_radius * 1.12, body_barrel_radius * 0.72))
            seat_outer_radius = min(ball_radius * 0.96, bore_radius + max(4.0, body_barrel_radius * 0.12))
            seat_thickness = max(4.0, body_barrel_len * 0.10)
            bolt_circle_radius = body_barrel_radius * 1.03
            bolt_radius = max(2.5, body_barrel_radius * 0.10)
            bolt_span = body_barrel_len * 0.58
            bonnet_radius = max(body_barrel_radius * 0.48, bore_radius * 0.55)
            bonnet_height = max(14.0, body_barrel_radius * 0.52)
            gland_radius = bonnet_radius * 0.72
            gland_height = max(6.0, bonnet_height * 0.34)
            gland_plate_size = max(gland_radius * 3.0, body_barrel_radius * 1.10)
            gland_plate_thickness = max(5.0, gland_height * 0.70)
            gland_bolt_radius = max(2.2, gland_radius * 0.18)
            gland_bolt_offset = gland_plate_size * 0.34
            stem_radius = max(3.0, bore_radius * 0.18)
            handle_block_len = max(90.0, body_barrel_radius * 2.8)
            handle_block_width = max(10.0, body_barrel_radius * 0.42)
            handle_block_height = max(6.0, handle_block_width * 0.55)
            handle_cap_radius = max(4.0, handle_block_height * 0.55)
            stem_bottom_z = cz - ball_radius * 0.12
            handle_z = cz + body_barrel_radius + bonnet_height + gland_height + handle_block_height * 0.10
            stem_top_z = cz + body_barrel_radius + bonnet_height + gland_height + handle_block_height * 1.25
            handle_center_x = cx + handle_block_len * 0.34
            body_plate_depth = max(6.0, body_barrel_radius * 0.26)

            def _del(s):
                try:
                    s.Delete()
                except Exception:
                    pass

            def _axis_dir(axis_signed_str: str) -> Tuple[str, int]:
                s = (axis_signed_str or "x").lower().strip()
                if s.startswith("-"):
                    return s[1:], -1
                return s, 1

            def _rotate_solid_from_z(solid, rotate_center: Tuple[float, float, float], axis_signed_str: str):
                ax, sign = _axis_dir(axis_signed_str)
                if ax not in ("x", "y", "z"):
                    return
                px, py, pz = rotate_center
                pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz])
                if ax == "z":
                    if sign < 0:
                        pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1.0, py, pz])
                        solid.Rotate3D(pt_center, pt_x, math.pi)
                    return
                if ax == "x":
                    pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
                    ang = (-math.pi / 2) if sign > 0 else (math.pi / 2)
                    solid.Rotate3D(pt_center, pt_y, ang)
                    return
                if ax == "y":
                    pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1.0, py, pz])
                    ang = (-math.pi / 2) if sign > 0 else (math.pi / 2)
                    solid.Rotate3D(pt_center, pt_x, ang)

            def _rotate_solid_from_x(solid, rotate_center: Tuple[float, float, float], axis_signed_str: str):
                ax, sign = _axis_dir(axis_signed_str)
                if ax not in ("x", "y", "z"):
                    return
                px, py, pz = rotate_center
                pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz])
                if ax == "x":
                    if sign < 0:
                        pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
                        solid.Rotate3D(pt_center, pt_y, math.pi)
                    return
                if ax == "y":
                    pt_z = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz + 1.0])
                    ang = math.pi / 2 if sign > 0 else -math.pi / 2
                    solid.Rotate3D(pt_center, pt_z, ang)
                    return
                if ax == "z":
                    pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
                    ang = math.pi / 2 if sign > 0 else -math.pi / 2
                    solid.Rotate3D(pt_center, pt_y, ang)

            def _cylinder(base_pt: Tuple[float, float, float], radius: float, height: float, axis_signed_str: str):
                axis_name, _ = _axis_dir(axis_signed_str)
                if radius <= 0 or height <= 0:
                    return None
                bx, by, bz = base_pt
                if axis_name == "z":
                    center_pt = (bx, by, bz + height / 2.0)
                elif axis_name == "y":
                    center_pt = (bx, by + height / 2.0, bz)
                else:
                    center_pt = (bx + height / 2.0, by, bz)
                center_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(center_pt))
                model_space = self._get_model_space()
                solid = self._call_with_retry(model_space.AddCylinder, center_arr, radius, height)
                _rotate_solid_from_z(solid, center_pt, axis_signed_str)
                return solid

            def _box(box_center: Tuple[float, float, float], lx: float, ly: float, lz: float):
                center_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(box_center))
                return self._get_model_space().AddBox(center_arr, lx, ly, lz)

            body_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, cz])
            body = self._call_with_retry(self._get_model_space().AddSphere, body_center, body_barrel_radius)
            if body is None:
                return None

            barrel = _cylinder((cx - body_barrel_len / 2.0, cy, cz), body_barrel_radius * 0.98, body_barrel_len, "x")
            if barrel is not None:
                self._solid_boolean_union(body, barrel)
                _del(barrel)

            left_neck = _cylinder((cx - body_barrel_len / 2.0 - side_neck_len, cy, cz), end_cap_radius, side_neck_len, "x")
            if left_neck is not None:
                self._solid_boolean_union(body, left_neck)
                _del(left_neck)
            right_neck = _cylinder((cx + body_barrel_len / 2.0, cy, cz), end_cap_radius, side_neck_len, "x")
            if right_neck is not None:
                self._solid_boolean_union(body, right_neck)
                _del(right_neck)

            flow_bore = _cylinder((cx - half - 2.0, cy, cz), bore_radius, face_to_face + 4.0, "x")
            if flow_bore is not None:
                self._solid_boolean_subtract(body, flow_bore)
                _del(flow_bore)

            ball = self._call_with_retry(self._get_model_space().AddSphere, body_center, ball_radius)
            if ball is not None:
                ball_bore = _cylinder((cx - ball_radius - 2.0, cy, cz), bore_radius, ball_radius * 2.0 + 4.0, "x")
                if ball_bore is not None:
                    self._solid_boolean_subtract(ball, ball_bore)
                    _del(ball_bore)
                self._solid_boolean_union(body, ball)
                _del(ball)

            left_seat = _cylinder((cx - seat_thickness / 2.0 - ball_radius * 0.82, cy, cz), seat_outer_radius, seat_thickness, "x")
            if left_seat is not None:
                left_seat_hole = _cylinder((cx - seat_thickness / 2.0 - ball_radius * 0.82 - 1.0, cy, cz), bore_radius, seat_thickness + 2.0, "x")
                if left_seat_hole is not None:
                    self._solid_boolean_subtract(left_seat, left_seat_hole)
                    _del(left_seat_hole)
                self._solid_boolean_union(body, left_seat)
                _del(left_seat)

            right_seat = _cylinder((cx + ball_radius * 0.82 - seat_thickness / 2.0, cy, cz), seat_outer_radius, seat_thickness, "x")
            if right_seat is not None:
                right_seat_hole = _cylinder((cx + ball_radius * 0.82 - seat_thickness / 2.0 - 1.0, cy, cz), bore_radius, seat_thickness + 2.0, "x")
                if right_seat_hole is not None:
                    self._solid_boolean_subtract(right_seat, right_seat_hole)
                    _del(right_seat_hole)
                self._solid_boolean_union(body, right_seat)
                _del(right_seat)

            split_plate = _box((cx, cy, cz), body_plate_depth, body_barrel_radius * 1.85, body_barrel_radius * 1.65)
            if split_plate is not None:
                self._solid_boolean_union(body, split_plate)
                _del(split_plate)

            for sign_x in (-1.0, 1.0):
                for sign_y in (-1.0, 1.0):
                    bx = cx + sign_x * bolt_span / 2.0
                    by = cy + sign_y * bolt_circle_radius * 0.74
                    bolt = _cylinder((bx, by - body_plate_depth * 0.55, cz), bolt_radius, body_plate_depth * 1.10, "y")
                    if bolt is not None:
                        self._solid_boolean_union(body, bolt)
                        _del(bolt)
                    nut_front = _cylinder((bx, by - body_plate_depth * 0.90, cz), bolt_radius * 1.35, body_plate_depth * 0.24, "y")
                    if nut_front is not None:
                        self._solid_boolean_union(body, nut_front)
                        _del(nut_front)
                    nut_back = _cylinder((bx, by + body_plate_depth * 0.66, cz), bolt_radius * 1.35, body_plate_depth * 0.24, "y")
                    if nut_back is not None:
                        self._solid_boolean_union(body, nut_back)
                        _del(nut_back)

            bonnet = _cylinder((cx, cy, cz + body_barrel_radius * 0.55), bonnet_radius, bonnet_height, "z")
            if bonnet is not None:
                self._solid_boolean_union(body, bonnet)
                _del(bonnet)

            gland = _cylinder((cx, cy, cz + body_barrel_radius * 0.55 + bonnet_height), gland_radius, gland_height, "z")
            if gland is not None:
                self._solid_boolean_union(body, gland)
                _del(gland)

            gland_plate_z = cz + body_barrel_radius * 0.55 + bonnet_height + gland_height
            gland_plate = _box((cx, cy, gland_plate_z), gland_plate_size, gland_plate_size, gland_plate_thickness)
            if gland_plate is not None:
                self._solid_boolean_union(body, gland_plate)
                _del(gland_plate)

            for sign_x in (-1.0, 1.0):
                for sign_y in (-1.0, 1.0):
                    gbx = cx + sign_x * gland_bolt_offset
                    gby = cy + sign_y * gland_bolt_offset
                    gland_bolt = _cylinder((gbx, gby, gland_plate_z - gland_plate_thickness * 0.10), gland_bolt_radius, gland_plate_thickness + gland_height * 0.80, "z")
                    if gland_bolt is not None:
                        self._solid_boolean_union(body, gland_bolt)
                        _del(gland_bolt)

            stem = _cylinder((cx, cy, stem_bottom_z), stem_radius, stem_top_z - stem_bottom_z, "z")
            if stem is not None:
                self._solid_boolean_union(body, stem)
                _del(stem)

            handle_hub = _cylinder((cx, cy, handle_z - handle_block_height * 0.35), handle_cap_radius * 1.2, handle_block_height * 0.7, "z")
            if handle_hub is not None:
                self._solid_boolean_union(body, handle_hub)
                _del(handle_hub)

            handle = _box((handle_center_x, cy, handle_z), handle_block_len, handle_block_width, handle_block_height)
            if handle is not None:
                self._solid_boolean_union(body, handle)
                _del(handle)

            flange1 = self._draw_standard_flange_from_outer_face(
                (cx - half, cy, cz),
                dn=dn,
                pn=pn,
                axis_signed="x",
                layer=layer,
                color=color,
            )
            if not flange1:
                _del(body)
                return None

            flange2 = self._draw_standard_flange_from_outer_face(
                (cx + half, cy, cz),
                dn=dn,
                pn=pn,
                axis_signed="-x",
                layer=layer,
                color=color,
            )
            if not flange2:
                _del(body)
                _del(flange1)
                return None

            self._solid_boolean_union(flange1, body)
            _del(body)
            self._solid_boolean_union(flange1, flange2)
            _del(flange2)

            _rotate_solid_from_x(flange1, (cx, cy, cz), axis or "x")

            if layer:
                self.create_layer(layer)
                flange1.Layer = layer
            if color is not None:
                flange1.Color = color
            self._finalize_entity_display(flange1)
            self.refresh_view()
            logger.debug(f"已绘制球阀: 中心{center}, DN{dn} PN{pn}, face_to_face={face_to_face}")
            return flange1
        except Exception as e:
            logger.error(f"绘制球阀时出错: {str(e)}")
            return None

    def draw_butterfly_valve(
        self,
        center: Tuple[float, float, float],
        dn: int,
        pn: float,
        axis: str = "z",
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制手柄式蝶阀（3D 实体）。依据 GB/T 12238、GB/T 12221，
        并结合常见工程三视图的外形表达生成更接近实物的短体蝶阀。

        默认基准模型：
        - 流道沿 +Y
        - 阀杆沿 +Z
        - 主视图（front，观察方向 -Y）可看到阀体圆环与竖向阀板
        - 左视图（left，观察方向 +X）可看到短体阀身、阀杆座与手柄
        """
        if not self.is_running():
            return None
        fp = self._get_flange_params(dn, pn)
        if not fp:
            logger.error(f"draw_butterfly_valve: 未找到法兰规格 DN{dn} PN{pn}")
            return None
        L = self.VALVE_BUTTERFLY_STRUCTURE_LENGTH.get(dn)
        if L is None:
            L = 50 + dn * 0.2
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0.0)
            cx, cy, cz = center
            D, d = fp["D"], fp["d"]
            # 图样中的 DN150 关键尺寸接近：阀体外径约 205、通径约 161、短体长度约 100、端面台阶约 8。
            # 这里用国标法兰内径 d 反推阀体与流道尺寸，使不同 DN 下仍保持相似外形比例。
            body_outer_diameter = min(D * 0.78, max(d * 1.21, d + 20.0))
            body_bore_diameter = max(10.0, d - max(8.0, round(d * 0.047)))
            body_length = max(float(L), round(d * 0.60))
            face_lip_thickness = max(6.0, min(10.0, round(d * 0.047)))
            body_waist_diameter = max(
                body_bore_diameter + max(14.0, round(d * 0.10)),
                body_outer_diameter * 0.74,
            )
            body_transition_length = min(
                max(face_lip_thickness * 0.90, body_length * 0.14),
                max(8.0, body_length * 0.24),
            )
            body_waist_length = max(body_length - 2.0 * body_transition_length, body_length * 0.46)
            stem_radius = max(6.0, body_bore_diameter * 0.045)
            bonnet_base_radius = max(stem_radius * 2.8, body_outer_diameter * 0.17)
            bonnet_base_height = max(10.0, face_lip_thickness * 1.35)
            neck_radius = max(stem_radius * 1.95, bonnet_base_radius * 0.58)
            neck_height = max(24.0, min(48.0, body_outer_diameter * 0.22))
            gland_radius = max(neck_radius * 1.18, body_outer_diameter * 0.12)
            gland_height = max(10.0, min(18.0, neck_height * 0.40))
            top_plate_radius = max(gland_radius * 0.76, stem_radius * 1.8)
            top_plate_thickness = max(8.0, min(12.0, face_lip_thickness * 1.15))
            disc_diameter = body_bore_diameter * 0.92
            disc_radius = disc_diameter / 2.0
            disc_thickness = max(10.0, body_length * 0.12)
            handle_length = max(135.0, body_outer_diameter * 0.96)
            handle_width = max(10.0, body_bore_diameter * 0.07)
            handle_height = max(6.0, handle_width * 0.65)
            handle_tail_length = handle_length * 0.26
            handle_hub_height = max(10.0, min(14.0, body_bore_diameter * 0.08))
            bolt_hole_count = 8 if dn >= 125 else 4
            bolt_hole_radius = max(4.0, min(4.5, (body_outer_diameter - body_bore_diameter) * 0.16))
            bolt_pitch_radius = (body_outer_diameter + body_bore_diameter) / 4.0
            flange_radius = body_outer_diameter / 2.0
            body_waist_radius = body_waist_diameter / 2.0
            body_bore_radius = body_bore_diameter / 2.0
            body_face_radius = body_waist_radius
            half = body_length / 2.0

            def _del(s):
                try:
                    s.Delete()
                except Exception:
                    pass

            def _axis_dir(axis_signed_str: str) -> Tuple[str, int]:
                s = (axis_signed_str or "y").lower().strip()
                if s.startswith("-"):
                    return s[1:], -1
                return s, 1

            def _rotate_solid_from_z(solid, rotate_center: Tuple[float, float, float], axis_signed_str: str):
                ax, sign = _axis_dir(axis_signed_str)
                if ax not in ("x", "y", "z"):
                    return
                px, py, pz = rotate_center
                pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz])
                if ax == "z":
                    if sign < 0:
                        pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1, py, pz])
                        solid.Rotate3D(pt_center, pt_x, math.pi)
                    return
                if ax == "x":
                    pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1, pz])
                    ang = (-math.pi / 2) if sign > 0 else (math.pi / 2)
                    solid.Rotate3D(pt_center, pt_y, ang)
                    return
                if ax == "y":
                    pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1, py, pz])
                    ang = (-math.pi / 2) if sign > 0 else (math.pi / 2)
                    solid.Rotate3D(pt_center, pt_x, ang)

            def _rotate_solid_from_y(solid, rotate_center: Tuple[float, float, float], axis_signed_str: str):
                ax, sign = _axis_dir(axis_signed_str)
                if ax not in ("x", "y", "z"):
                    return
                px, py, pz = rotate_center
                pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz])
                if ax == "x":
                    pt_z = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz + 1])
                    ang = -math.pi / 2 if sign > 0 else math.pi / 2
                    solid.Rotate3D(pt_center, pt_z, ang)
                    return
                if ax == "y":
                    if sign < 0:
                        pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1, py, pz])
                        solid.Rotate3D(pt_center, pt_x, math.pi)
                    return
                if ax == "z":
                    pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1, py, pz])
                    ang = math.pi / 2 if sign > 0 else -math.pi / 2
                    solid.Rotate3D(pt_center, pt_x, ang)

            def _cylinder(base_pt: Tuple[float, float, float], radius: float, height: float, axis_signed_str: str):
                axis_name, _ = _axis_dir(axis_signed_str)
                if radius <= 0 or height <= 0:
                    return None
                bx, by, bz = base_pt
                if axis_name == "z":
                    center_pt = (bx, by, bz + height / 2.0)
                elif axis_name == "y":
                    center_pt = (bx, by + height / 2.0, bz)
                else:
                    center_pt = (bx + height / 2.0, by, bz)
                center_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(center_pt))
                model_space = self._get_model_space()
                solid = self._call_with_retry(model_space.AddCylinder, center_arr, radius, height)
                _rotate_solid_from_z(solid, center_pt, axis_signed_str)
                return solid

            def _box(box_center: Tuple[float, float, float], lx: float, ly: float, lz: float):
                center_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(box_center))
                return self._get_model_space().AddBox(center_arr, lx, ly, lz)

            def _frustum(base_pt: Tuple[float, float, float], r_big: float, r_small: float, length_f: float, axis_signed_str: str):
                if r_big <= r_small or length_f <= 0:
                    return None
                full_height = length_f * r_big / (r_big - r_small)
                ox, oy, oz = base_pt
                base_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [ox, oy, oz])
                model_space = self._get_model_space()
                cone_big = self._call_with_retry(model_space.AddCone, base_arr, r_big, full_height)
                tip_local = (ox, oy, oz + length_f)
                tip_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [tip_local[0], tip_local[1], tip_local[2]])
                cone_small = self._call_with_retry(model_space.AddCone, tip_arr, r_small, full_height - length_f)
                self._solid_boolean_subtract(cone_big, cone_small)
                _del(cone_small)
                _rotate_solid_from_z(cone_big, base_pt, axis_signed_str)
                return cone_big

            def _cut_obround_slots(target_solid, slot_base_y_positions: Tuple[float, float], slot_cut_depth: float):
                slot_half_span = max(bolt_hole_radius * 1.6, (flange_radius - body_bore_radius) * 0.28)
                for i in range(bolt_hole_count):
                    angle = (2.0 * math.pi * i / bolt_hole_count) + (math.pi / bolt_hole_count)
                    radial_x = math.cos(angle)
                    radial_z = math.sin(angle)
                    tangent_x = -math.sin(angle)
                    tangent_z = math.cos(angle)
                    slot_center_x = cx + bolt_pitch_radius * radial_x
                    slot_center_z = cz + bolt_pitch_radius * radial_z
                    for slot_base_y in slot_base_y_positions:
                        slot_cyl_1 = _cylinder(
                            (
                                slot_center_x - slot_half_span * tangent_x,
                                slot_base_y,
                                slot_center_z - slot_half_span * tangent_z,
                            ),
                            bolt_hole_radius,
                            slot_cut_depth,
                            base_flow_axis,
                        )
                        if slot_cyl_1 is not None:
                            self._solid_boolean_subtract(target_solid, slot_cyl_1)
                            _del(slot_cyl_1)

                        slot_cyl_2 = _cylinder(
                            (
                                slot_center_x + slot_half_span * tangent_x,
                                slot_base_y,
                                slot_center_z + slot_half_span * tangent_z,
                            ),
                            bolt_hole_radius,
                            slot_cut_depth,
                            base_flow_axis,
                        )
                        if slot_cyl_2 is not None:
                            self._solid_boolean_subtract(target_solid, slot_cyl_2)
                            _del(slot_cyl_2)

                        slot_box = _box(
                            (slot_center_x, slot_base_y + slot_cut_depth / 2.0, slot_center_z),
                            bolt_hole_radius * 2.0,
                            slot_cut_depth,
                            slot_half_span * 2.0,
                        )
                        if slot_box is not None:
                            pt_slot = win32com.client.VARIANT(
                                pythoncom.VT_ARRAY | pythoncom.VT_R8,
                                [slot_center_x, slot_base_y + slot_cut_depth / 2.0, slot_center_z],
                            )
                            pt_slot_axis = win32com.client.VARIANT(
                                pythoncom.VT_ARRAY | pythoncom.VT_R8,
                                [slot_center_x, slot_base_y + slot_cut_depth / 2.0 + 1.0, slot_center_z],
                            )
                            slot_box.Rotate3D(pt_slot, pt_slot_axis, -angle)
                            self._solid_boolean_subtract(target_solid, slot_box)
                            _del(slot_box)

            base_flow_axis = "y"
            body = _cylinder(
                (cx, cy - body_waist_length / 2.0, cz),
                body_waist_radius,
                body_waist_length,
                base_flow_axis,
            )
            if body is None:
                return None

            if body_face_radius > body_waist_radius:
                left_transition = _frustum(
                    (cx, cy - half, cz),
                    body_face_radius,
                    body_waist_radius,
                    body_transition_length,
                    base_flow_axis,
                )
            else:
                left_transition = _cylinder(
                    (cx, cy - half, cz),
                    body_waist_radius,
                    body_transition_length,
                    base_flow_axis,
                )
            if left_transition is not None:
                self._solid_boolean_union(body, left_transition)
                _del(left_transition)

            if body_face_radius > body_waist_radius:
                right_transition = _frustum(
                    (cx, cy + half, cz),
                    body_face_radius,
                    body_waist_radius,
                    body_transition_length,
                    "-y",
                )
            else:
                right_transition = _cylinder(
                    (cx, cy + half - body_transition_length, cz),
                    body_waist_radius,
                    body_transition_length,
                    base_flow_axis,
                )
            if right_transition is not None:
                self._solid_boolean_union(body, right_transition)
                _del(right_transition)

            bore_eps = 2.0
            bore = _cylinder((cx, cy - half - bore_eps, cz), body_bore_radius, body_length + 2.0 * bore_eps, base_flow_axis)
            if bore is not None:
                self._solid_boolean_subtract(body, bore)
                _del(bore)

            flange_left = _cylinder((cx, cy - half - face_lip_thickness, cz), flange_radius, face_lip_thickness, base_flow_axis)
            if flange_left is not None:
                flange_left_bore = _cylinder((cx, cy - half - face_lip_thickness - 1.0, cz), body_bore_radius, face_lip_thickness + 2.0, base_flow_axis)
                if flange_left_bore is not None:
                    self._solid_boolean_subtract(flange_left, flange_left_bore)
                    _del(flange_left_bore)
                _cut_obround_slots(flange_left, (cy - half - face_lip_thickness - 1.0,), face_lip_thickness + 2.0)
                self._solid_boolean_union(body, flange_left)
                _del(flange_left)

            flange_right = _cylinder((cx, cy + half, cz), flange_radius, face_lip_thickness, base_flow_axis)
            if flange_right is not None:
                flange_right_bore = _cylinder((cx, cy + half - 1.0, cz), body_bore_radius, face_lip_thickness + 2.0, base_flow_axis)
                if flange_right_bore is not None:
                    self._solid_boolean_subtract(flange_right, flange_right_bore)
                    _del(flange_right_bore)
                _cut_obround_slots(flange_right, (cy + half - 1.0,), face_lip_thickness + 2.0)
                self._solid_boolean_union(body, flange_right)
                _del(flange_right)

            bonnet_base_z = cz + body_waist_radius
            bonnet_base = _cylinder((cx, cy, bonnet_base_z), bonnet_base_radius, bonnet_base_height, "z")
            if bonnet_base is not None:
                self._solid_boolean_union(body, bonnet_base)
                _del(bonnet_base)

            neck_z = bonnet_base_z + bonnet_base_height
            neck = _cylinder((cx, cy, neck_z), neck_radius, neck_height, "z")
            if neck is not None:
                self._solid_boolean_union(body, neck)
                _del(neck)

            gland_z = neck_z + neck_height
            gland = _cylinder((cx, cy, gland_z), gland_radius, gland_height, "z")
            if gland is not None:
                self._solid_boolean_union(body, gland)
                _del(gland)

            top_plate_z = gland_z + gland_height
            top_plate = _cylinder((cx, cy, top_plate_z), top_plate_radius, top_plate_thickness, "z")
            if top_plate is not None:
                self._solid_boolean_union(body, top_plate)
                _del(top_plate)

            stem_base_z = cz - disc_diameter * 0.45
            stem_height = top_plate_z + top_plate_thickness + handle_hub_height - 2.0 - stem_base_z
            stem = _cylinder((cx, cy, stem_base_z), stem_radius, stem_height, "z")
            if stem is not None:
                self._solid_boolean_union(body, stem)
                _del(stem)

            disc = _cylinder((cx, cy - disc_thickness / 2.0, cz), disc_radius, disc_thickness, base_flow_axis)
            if disc is not None:
                self._solid_boolean_union(body, disc)
                _del(disc)

            handle_hub_z = top_plate_z + top_plate_thickness
            handle_hub = _cylinder((cx, cy, handle_hub_z), stem_radius * 1.8, handle_hub_height, "z")
            if handle_hub is not None:
                self._solid_boolean_union(body, handle_hub)
                _del(handle_hub)

            handle_z = handle_hub_z + handle_hub_height * 0.55
            handle = _box((cx - handle_length * 0.42, cy, handle_z), handle_length, handle_width, handle_height)
            if handle is not None:
                pt_handle = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, handle_z])
                pt_handle_axis = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy + 1.0, handle_z])
                handle.Rotate3D(pt_handle, pt_handle_axis, math.radians(12.0))
                self._solid_boolean_union(body, handle)
                _del(handle)

            handle_tail_z = handle_hub_z + handle_hub_height * 0.25
            handle_tail = _box((cx + handle_tail_length * 0.55, cy, handle_tail_z), handle_tail_length, handle_width * 0.55, handle_height * 0.55)
            if handle_tail is not None:
                pt_tail = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, handle_tail_z])
                pt_tail_axis = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy + 1.0, handle_tail_z])
                handle_tail.Rotate3D(pt_tail, pt_tail_axis, math.radians(-5.0))
                self._solid_boolean_union(body, handle_tail)
                _del(handle_tail)

            _rotate_solid_from_y(body, (cx, cy, cz), axis or "y")

            if layer:
                self.create_layer(layer)
                body.Layer = layer
            if color is not None:
                body.Color = color
            self._finalize_entity_display(body)
            self.refresh_view()
            logger.debug(
                f"已绘制蝶阀: 中心{center}, DN{dn} PN{pn}, body_od={body_outer_diameter}, "
                f"body_id={body_bore_diameter}, body_len={body_length}"
            )
            return body
        except Exception as e:
            logger.error(f"绘制蝶阀时出错: {str(e)}")
            return None

    def draw_globe_valve(
        self,
        center: Tuple[float, float, float],
        dn: int,
        pn: float,
        axis: str = "x",
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制法兰截止阀（3D 实体）。依据 GB/T 12235、GB/T 12221，
        默认流道沿 X、阀杆沿 Z。优先保证阀体-侧管-法兰连续贴合与通流空心，
        再表达阀盖、支架与手轮外形；默认外观不挖穿阀体中腔。
        """
        if not self.is_running():
            return None
        fp = self._get_flange_params(dn, pn)
        if not fp:
            logger.error(f"draw_globe_valve: 未找到法兰规格 DN{dn} PN{pn}")
            return None
        L = self.VALVE_GLOBE_STRUCTURE_LENGTH.get(dn)
        if L is None:
            L = 200 + dn
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0.0)
            cx, cy, cz = center
            D, d = fp["D"], fp["d"]
            bore_radius = d / 2.0
            # 视觉法兰略小于国标外圈，但中心孔必须与侧管外径一致，避免“法兰悬空”。
            flange_scale = 0.82
            raw_bolt_circle_diameter = float(fp.get("K", D * 0.70))
            raw_bolt_hole_diameter = float(fp.get("L", max(10.0, d * 0.10)))
            flange_outer_diameter = min(
                D * flange_scale,
                max(d * 1.58, raw_bolt_circle_diameter * flange_scale + raw_bolt_hole_diameter * 1.40),
            )
            flange_bolt_circle_diameter = min(
                raw_bolt_circle_diameter * flange_scale,
                flange_outer_diameter - raw_bolt_hole_diameter * 1.55,
            )
            flange_bolt_circle_diameter = max(flange_outer_diameter * 0.58, flange_bolt_circle_diameter)
            flange_bolt_hole_diameter = min(
                raw_bolt_hole_diameter * 0.92,
                max(6.0, (flange_outer_diameter - flange_bolt_circle_diameter) * 0.82),
            )
            flange_bolt_count = int(fp.get("n", 4))
            flange_radius = flange_outer_diameter / 2.0
            flange_thickness = max(8.0, min(float(fp["C"]) * 0.92, 22.0))
            face_to_face = float(L)
            half = face_to_face / 2.0

            body_lower_radius = min(flange_radius * 0.78, face_to_face * 0.24)
            body_shell_center_z = cz
            # 侧管必须明显粗于流道孔，否则减流道会把接管整根削没，法兰就会悬空。
            side_pipe_outer_radius = max(body_lower_radius * 0.52, bore_radius * 0.62)
            flow_bore_radius = min(side_pipe_outer_radius * 0.72, bore_radius * 0.78)
            # 球体保持完整，不再裁顶；球体与阀盖圆盘之间用小圆柱衔接。
            sphere_top_z = body_shell_center_z + body_lower_radius
            # 侧法兰立放后，其最上缘约在 cz + 法兰外半径。
            side_flange_top_z = cz + flange_radius
            pipe_embed_into_body = max(2.0, side_pipe_outer_radius * 0.18)
            visible_pipe_span = max(4.0, half - flange_thickness - body_lower_radius)
            shortened_visible_pipe_span = max(4.0, visible_pipe_span * 0.50)
            flange_inset = visible_pipe_span - shortened_visible_pipe_span
            left_flange_outer_face_x = cx - half + flange_inset
            right_flange_outer_face_x = cx + half - flange_inset
            side_neck_len = flange_thickness + shortened_visible_pipe_span + pipe_embed_into_body

            bonnet_flange_radius = max(body_lower_radius * 0.56, side_pipe_outer_radius * 1.26)
            bonnet_flange_thickness = max(10.0, flange_thickness * 0.90)
            # 球体到阀盖圆盘的衔接圆柱：前视要能看出两道竖线轮廓，圆盘整体明显高于两侧法兰顶。
            body_connector_radius = max(bonnet_flange_radius * 0.62, body_lower_radius * 0.48)
            body_connector_clear_height = max(36.0, body_lower_radius * 0.48)
            bonnet_base_z = max(
                sphere_top_z + body_connector_clear_height,
                side_flange_top_z + max(48.0, flange_radius * 0.38),
            )
            body_connector_clear_height = bonnet_base_z - sphere_top_z
            connector_embed = max(5.0, body_connector_radius * 0.25)
            connector_base_z = sphere_top_z - connector_embed
            connector_draw_height = bonnet_base_z - connector_base_z

            # 六颗螺丝上方的小圆柱：弯支架下端就接在这里。
            bonnet_neck_radius = max(side_pipe_outer_radius * 0.54, body_lower_radius * 0.36)
            bonnet_neck_height = max(28.0, body_lower_radius * 0.42)
            packing_radius = bonnet_neck_radius * 0.78
            packing_height = max(8.0, bonnet_neck_height * 0.36)
            gland_radius = packing_radius * 0.86
            gland_height = max(6.0, packing_height * 0.55)
            stud_circle_radius = bonnet_flange_radius * 0.74
            stud_radius = max(2.6, bonnet_flange_radius * 0.08)
            stud_height = bonnet_flange_thickness * 1.9
            nut_radius = stud_radius * 1.45
            nut_height = max(4.5, bonnet_flange_thickness * 0.48)
            yoke_post_radius = max(3.5, bonnet_flange_radius * 0.11)
            # 手轮略放大，但不超过法兰外径，避免喧宾夺主。
            handwheel_radius = max(28.0, min(bonnet_flange_radius * 1.35, flange_radius * 0.92))
            handwheel_diameter = handwheel_radius * 2.0
            handwheel_tube_radius = max(3.2, handwheel_radius * 0.09)
            handwheel_hub_radius = max(6.5, yoke_post_radius * 1.35)
            stem_radius = max(3.0, flow_bore_radius * 0.22)
            pipe_outer_diameter = side_pipe_outer_radius * 2.0

            bonnet_neck_base_z = bonnet_base_z + bonnet_flange_thickness
            packing_base_z = bonnet_base_z + bonnet_flange_thickness + bonnet_neck_height * 0.30
            gland_base_z = bonnet_base_z + bonnet_flange_thickness + bonnet_neck_height + packing_height * 0.18
            # 手轮圆环下方的连接小圆柱：两侧光滑弯支架上端接到这里。
            yoke_bushing_radius = max(handwheel_hub_radius * 1.85, stem_radius * 4.0, yoke_post_radius * 3.2)
            yoke_bushing_height = max(36.0, handwheel_tube_radius * 6.5, bonnet_neck_height * 1.25)
            # 中间阀杆拉高，给两侧圆环状弯支架留出竖向空间。
            disc_top_z = bonnet_base_z + bonnet_flange_thickness
            yoke_clearance = max(210.0, body_lower_radius * 2.35)
            handwheel_z = disc_top_z + yoke_clearance
            yoke_bushing_base_z = handwheel_z - handwheel_tube_radius * 0.15 - yoke_bushing_height
            # 弯支架：下端明确接到“六螺钉圆盘上方的颈柱”外壁，上端接手轮下连接柱。
            # 落点取颈柱中上部，避免珠串沿圆盘顶面横扫看起来像落在法兰面上。
            arm_low_z = bonnet_neck_base_z + bonnet_neck_height * 0.62
            arm_up_z = yoke_bushing_base_z + yoke_bushing_height * 0.50
            # 阀杆只从阀盖附近升起，不挖穿阀体中腔。
            stem_bottom_z = bonnet_base_z + bonnet_flange_thickness * 0.20
            stem_top_z = handwheel_z + handwheel_tube_radius * 0.90

            def _del(s):
                try:
                    s.Delete()
                except Exception:
                    pass

            def _axis_dir(axis_signed_str: str) -> Tuple[str, int]:
                s = (axis_signed_str or "x").lower().strip()
                if s.startswith("-"):
                    return s[1:], -1
                return s, 1

            def _rotate_solid_from_z(solid, rotate_center: Tuple[float, float, float], axis_signed_str: str):
                ax, sign = _axis_dir(axis_signed_str)
                if ax not in ("x", "y", "z"):
                    return
                px, py, pz = rotate_center
                pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz])
                if ax == "z":
                    if sign < 0:
                        pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1.0, py, pz])
                        solid.Rotate3D(pt_center, pt_x, math.pi)
                    return
                if ax == "x":
                    pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
                    ang = (-math.pi / 2) if sign > 0 else (math.pi / 2)
                    solid.Rotate3D(pt_center, pt_y, ang)
                    return
                if ax == "y":
                    pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1.0, py, pz])
                    ang = (-math.pi / 2) if sign > 0 else (math.pi / 2)
                    solid.Rotate3D(pt_center, pt_x, ang)

            def _rotate_solid_from_x(solid, rotate_center: Tuple[float, float, float], axis_signed_str: str):
                ax, sign = _axis_dir(axis_signed_str)
                if ax not in ("x", "y", "z"):
                    return
                px, py, pz = rotate_center
                pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz])
                if ax == "x":
                    if sign < 0:
                        pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
                        solid.Rotate3D(pt_center, pt_y, math.pi)
                    return
                if ax == "y":
                    pt_z = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz + 1.0])
                    ang = math.pi / 2 if sign > 0 else -math.pi / 2
                    solid.Rotate3D(pt_center, pt_z, ang)
                    return
                if ax == "z":
                    pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
                    ang = math.pi / 2 if sign > 0 else -math.pi / 2
                    solid.Rotate3D(pt_center, pt_y, ang)

            def _cylinder(base_pt: Tuple[float, float, float], radius: float, height: float, axis_signed_str: str):
                axis_name, _ = _axis_dir(axis_signed_str)
                if radius <= 0 or height <= 0:
                    return None
                bx, by, bz = base_pt
                if axis_name == "z":
                    center_pt = (bx, by, bz + height / 2.0)
                elif axis_name == "y":
                    center_pt = (bx, by + height / 2.0, bz)
                else:
                    center_pt = (bx + height / 2.0, by, bz)
                center_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(center_pt))
                model_space = self._get_model_space()
                solid = self._call_with_retry(model_space.AddCylinder, center_arr, radius, height)
                _rotate_solid_from_z(solid, center_pt, axis_signed_str)
                return solid

            def _box(box_center: Tuple[float, float, float], lx: float, ly: float, lz: float):
                corner = [
                    box_center[0] - lx / 2.0,
                    box_center[1] - ly / 2.0,
                    box_center[2] - lz / 2.0,
                ]
                corner_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, corner)
                return self._get_model_space().AddBox(corner_arr, lx, ly, lz)

            def _union_into(target, solid):
                if solid is None:
                    return False
                result = self._solid_boolean_union(target, solid)
                if result is None:
                    logger.warning("draw_globe_valve: 布尔并体失败，保留原实体以免丢件")
                    return False
                _del(solid)
                return True

            def _union_pair(solid_a, solid_b):
                if solid_a is None:
                    return solid_b
                if solid_b is None:
                    return solid_a
                result = self._solid_boolean_union(solid_a, solid_b)
                if result is None:
                    logger.warning("draw_globe_valve: 支架段并体失败，保留两段实体")
                    return solid_a
                _del(solid_b)
                return solid_a

            def _draw_visual_flange_from_outer_face(
                outer_face_center: Tuple[float, float, float],
                axis_signed_str: str,
            ):
                flange = self.draw_flange(
                    self._normalize_point3d(outer_face_center),
                    outer_diameter=flange_outer_diameter,
                    center_hole_diameter=pipe_outer_diameter,
                    bolt_circle_diameter=flange_bolt_circle_diameter,
                    bolt_hole_diameter=flange_bolt_hole_diameter,
                    bolt_count=flange_bolt_count,
                    thickness=flange_thickness,
                    layer=layer,
                    color=color,
                )
                if flange is None:
                    return None
                _rotate_solid_from_z(flange, outer_face_center, axis_signed_str)
                return flange

            body_center = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_R8,
                [cx, cy, body_shell_center_z],
            )
            body = self._call_with_retry(self._get_model_space().AddSphere, body_center, body_lower_radius)
            if body is None:
                return None

            # 先并侧管，再减比侧管更细的流道，保证接管壁厚不被削没。
            left_neck = _cylinder((left_flange_outer_face_x, cy, cz), side_pipe_outer_radius, side_neck_len, "x")
            _union_into(body, left_neck)

            right_neck = _cylinder(
                (right_flange_outer_face_x - side_neck_len, cy, cz),
                side_pipe_outer_radius,
                side_neck_len,
                "x",
            )
            _union_into(body, right_neck)

            flow_bore = _cylinder(
                (left_flange_outer_face_x - 2.0, cy, cz),
                flow_bore_radius,
                right_flange_outer_face_x - left_flange_outer_face_x + 4.0,
                "x",
            )
            if flow_bore is not None:
                self._solid_boolean_subtract(body, flow_bore)
                _del(flow_bore)

            flange_left = _draw_visual_flange_from_outer_face((left_flange_outer_face_x, cy, cz), "x")
            if not flange_left:
                _del(body)
                return None

            flange_right = _draw_visual_flange_from_outer_face((right_flange_outer_face_x, cy, cz), "-x")
            if not flange_right:
                _del(body)
                _del(flange_left)
                return None

            # 球体顶面 -> 小圆柱 -> 带六颗螺钉的阀盖圆盘。
            body_connector = _cylinder(
                (cx, cy, connector_base_z),
                body_connector_radius,
                connector_draw_height,
                "z",
            )
            _union_into(body, body_connector)

            bonnet_flange = _cylinder((cx, cy, bonnet_base_z), bonnet_flange_radius, bonnet_flange_thickness, "z")
            _union_into(body, bonnet_flange)

            bonnet_neck = _cylinder(
                (cx, cy, bonnet_neck_base_z),
                bonnet_neck_radius,
                bonnet_neck_height,
                "z",
            )
            _union_into(body, bonnet_neck)

            packing = _cylinder((cx, cy, packing_base_z), packing_radius, packing_height, "z")
            _union_into(body, packing)

            gland = _cylinder((cx, cy, gland_base_z), gland_radius, gland_height, "z")
            _union_into(body, gland)

            # 两侧弯支架：右侧完整 C 为真值源，镜像到左侧。
            # 下端落到六螺钉圆盘上方颈柱外壁，上端落到手轮下连接柱外壁。
            arm_tube_radius = max(yoke_post_radius * 1.35, 5.0)
            arm_attach_low_r = bonnet_neck_radius * 0.99  # 中心略埋入颈柱外壁，接触更实
            arm_attach_up_r = yoke_bushing_radius * 0.98
            arm_mid_z = 0.5 * (arm_low_z + arm_up_z)
            arm_half_h = 0.5 * (arm_up_z - arm_low_z)
            arm_attach_r = 0.5 * (arm_attach_low_r + arm_attach_up_r)
            arm_major_r = math.sqrt(max(1.0, arm_attach_r ** 2 + arm_half_h ** 2))
            # 中段外凸，但下端必须收回颈柱，不能落到法兰螺钉圆上。
            arm_bow_r = max(arm_major_r, arm_attach_low_r + arm_half_h * 0.50)
            seg_count = 56
            neck_climb_frac = 0.24  # 前段先贴颈柱爬升，再外扩

            def _build_full_c_arm(sign_x: float):
                """下端接六螺钉圆盘上方颈柱，上端接手轮下连接柱。"""
                pts = []
                for i in range(seg_count + 1):
                    t = i / float(seg_count)
                    pz = arm_low_z + (arm_up_z - arm_low_z) * t
                    circle_r = math.sqrt(max(0.0, arm_major_r * arm_major_r - (pz - arm_mid_z) ** 2))
                    bow = arm_attach_r + (arm_bow_r - arm_attach_r) * math.sin(math.pi * t)
                    radial_open = max(circle_r, bow)
                    # 下端不要沿圆盘顶横扫：先沿颈柱外壁爬一段，再平滑外扩成 C。
                    if t <= neck_climb_frac:
                        climb = t / max(1e-6, neck_climb_frac)
                        radial = arm_attach_low_r + (radial_open - arm_attach_low_r) * (climb ** 2.2) * 0.18
                    else:
                        t2 = (t - neck_climb_frac) / max(1e-6, 1.0 - neck_climb_frac)
                        radial = arm_attach_low_r + (radial_open - arm_attach_low_r) * (0.18 + 0.82 * (t2 ** 0.85))
                    if t >= 0.88:
                        u = (t - 0.88) / 0.12
                        radial = radial * (1.0 - u) + arm_attach_up_r * u
                    pts.append((cx + sign_x * radial, cy, pz))
                # 端点强制落在目标圆柱外壁。
                pts[0] = (cx + sign_x * arm_attach_low_r, cy, arm_low_z)
                pts[-1] = (cx + sign_x * arm_attach_up_r, cy, arm_up_z)
                # 下端沿颈柱再叠几颗球，把“接在圆盘上方圆柱上”做实。
                for k in range(1, 5):
                    pz = arm_low_z + arm_tube_radius * 0.55 * k
                    if pz >= arm_up_z:
                        break
                    pts.insert(k, (cx + sign_x * arm_attach_low_r, cy, pz))

                arm_solid = None
                for px, py, pz in pts:
                    center_arr = win32com.client.VARIANT(
                        pythoncom.VT_ARRAY | pythoncom.VT_R8,
                        [px, py, pz],
                    )
                    sph = self._call_with_retry(
                        self._get_model_space().AddSphere,
                        center_arr,
                        float(arm_tube_radius),
                    )
                    arm_solid = _union_pair(arm_solid, sph)
                return arm_solid

            left_arm = None
            right_arm = None
            try:
                right_arm = _build_full_c_arm(1.0)
                if right_arm is None:
                    raise RuntimeError("right full C arm build returned None")
                stub = right_arm.Copy()
                p1 = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    [cx, cy - 1.0, arm_mid_z - 1.0],
                )
                p2 = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    [cx, cy + 1.0, arm_mid_z - 1.0],
                )
                p3 = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    [cx, cy, arm_mid_z + 1.0],
                )
                left_arm = stub.Mirror3D(p1, p2, p3)
                if left_arm is None:
                    raise RuntimeError("left arm Mirror3D from right failed")
                try:
                    if getattr(stub, "Handle", None) != getattr(left_arm, "Handle", None):
                        _del(stub)
                except Exception:
                    _del(stub)
                logger.info(
                    "draw_globe_valve: 右侧完整 C 已镜像到左侧；独立实体不并入阀体"
                )
            except Exception as arm_error:
                left_arm = None
                right_arm = None
                logger.warning(f"draw_globe_valve: 完整 C 形支架失败: {arm_error}")

            # 阀盖法兰用少量螺柱+螺母表达层次，避免线框过密。
            for ang_deg in (0.0, 60.0, 120.0, 180.0, 240.0, 300.0):
                ang = math.radians(ang_deg)
                sx = cx + stud_circle_radius * math.cos(ang)
                sy = cy + stud_circle_radius * math.sin(ang)
                stud = _cylinder(
                    (sx, sy, bonnet_base_z + bonnet_flange_thickness * 0.05),
                    stud_radius,
                    stud_height,
                    "z",
                )
                _union_into(body, stud)
                nut = _cylinder(
                    (sx, sy, bonnet_base_z + bonnet_flange_thickness * 0.95),
                    nut_radius,
                    nut_height,
                    "z",
                )
                _union_into(body, nut)

            stem = _cylinder((cx, cy, stem_bottom_z), stem_radius, max(1.0, stem_top_z - stem_bottom_z), "z")
            _union_into(body, stem)

            # 先落手轮下方连接小圆柱，再画手轮，避免上端附着点“看不见”。
            yoke_bushing = _cylinder(
                (cx, cy, yoke_bushing_base_z),
                yoke_bushing_radius,
                yoke_bushing_height,
                "z",
            )
            _union_into(body, yoke_bushing)

            hub_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, handwheel_z])
            torus = self._call_with_retry(
                self._get_model_space().AddTorus,
                hub_center,
                handwheel_radius,
                handwheel_tube_radius,
            )
            _union_into(body, torus)

            hub = _cylinder(
                (cx, cy, handwheel_z - handwheel_tube_radius * 1.05),
                handwheel_hub_radius,
                handwheel_tube_radius * 2.1,
                "z",
            )
            _union_into(body, hub)

            # 手轮三根辐条：互成 120°，从轮毂伸到轮缘。
            # 避开 0° 水平辐条，减少前视误看成手轮下横梁。
            spoke_radius = max(2.8, handwheel_tube_radius * 0.85)
            for ang_deg in (30.0, 150.0, 270.0):
                ang = math.radians(ang_deg)
                spoke = _cylinder(
                    (cx, cy, handwheel_z),
                    spoke_radius,
                    handwheel_radius * 0.92,
                    "x",
                )
                if spoke is not None:
                    try:
                        pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, handwheel_z])
                        pt_axis = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, handwheel_z + 1.0])
                        spoke.Rotate3D(pt_center, pt_axis, ang)
                        _union_into(body, spoke)
                    except Exception as spoke_error:
                        logger.warning(f"draw_globe_valve: 辐条旋转失败，跳过该辐条: {spoke_error}")
                        _del(spoke)

            self._solid_boolean_union(flange_left, body)
            _del(body)
            self._solid_boolean_union(flange_left, flange_right)
            _del(flange_right)

            _rotate_solid_from_x(flange_left, (cx, cy, cz), axis or "x")
            # 左右支架保持独立实体并同步旋转，不再并入总成。
            # 实测并入法兰/阀体后左侧 C 常被布尔吃成半截或细条。
            for arm_solid in (left_arm, right_arm):
                if arm_solid is None:
                    continue
                _rotate_solid_from_x(arm_solid, (cx, cy, cz), axis or "x")
                if layer:
                    try:
                        arm_solid.Layer = layer
                    except Exception:
                        pass
                if color is not None:
                    try:
                        arm_solid.Color = color
                    except Exception:
                        pass
                self._finalize_entity_display(arm_solid)

            if layer:
                try:
                    self.create_layer(layer)
                    flange_left.Layer = layer
                except Exception as style_error:
                    logger.warning(f"draw_globe_valve: 设置图层失败，继续保留截止阀实体: {style_error}")
            if color is not None:
                try:
                    flange_left.Color = color
                except Exception as color_error:
                    logger.warning(f"draw_globe_valve: 设置颜色失败，继续保留截止阀实体: {color_error}")
            self._finalize_entity_display(flange_left)
            self.refresh_view()
            logger.debug(f"已绘制截止阀: 中心{center}, DN{dn} PN{pn}, face_to_face={face_to_face}")
            return flange_left
        except Exception as e:
            logger.error(f"绘制截止阀时出错: {str(e)}")
            return None

    def draw_check_valve(
        self,
        center: Tuple[float, float, float],
        dn: int,
        pn: float = 16,
        axis: str = "x",
        layer: str = None,
        color: int = None,
        valve_type: str = "swing",
    ) -> Any:
        """
        绘制止回阀（3D 实体）。

        当前默认实现为法兰旋启式止回阀，标准依据：
        - GB/T 12236 钢制旋启式止回阀
        - GB/T 12221 金属阀门结构长度

        约定：
        - center 为阀门几何中心
        - axis 为流道轴向
        - valve_type 目前优先支持 swing/旋启式
        """
        if not self.is_running():
            return None
        valve_type_key = (valve_type or "swing").strip().lower().replace("-", "_").replace(" ", "_")
        if valve_type_key in {"旋启", "旋启式", "swing_check"}:
            valve_type_key = "swing"
        if valve_type_key != "swing":
            logger.error(f"draw_check_valve: 暂不支持止回阀型式 {valve_type}")
            return None

        fp = self._get_flange_params(dn, pn)
        if not fp:
            logger.error(f"draw_check_valve: 未找到法兰规格 DN{dn} PN{pn}")
            return None

        face_to_face = float(self.VALVE_CHECK_STRUCTURE_LENGTH.get(dn, max(220.0, dn * 1.9 + 250.0)))
        created_entities: list[Any] = []
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0.0)
            cx, cy, cz = center
            flange_radius = float(fp["D"]) / 2.0
            bore_radius = float(fp["d"]) / 2.0
            flange_thickness = max(10.0, min(float(fp["C"]), 30.0))
            half = face_to_face / 2.0

            chamber_radius = min(flange_radius * 0.82, face_to_face * 0.21)
            chamber_barrel_len = max(bore_radius * 2.3, chamber_radius * 1.35)
            neck_radius = max(bore_radius * 1.07, chamber_radius * 0.62)
            neck_len = max(16.0, half - flange_thickness - chamber_barrel_len / 2.0)
            seat_outer_radius = max(bore_radius * 1.12, neck_radius * 0.92)
            seat_length = max(12.0, chamber_radius * 0.18)

            cover_radius = max(chamber_radius * 0.56, bore_radius * 1.20)
            cover_height = max(24.0, chamber_radius * 0.66)
            cover_base_z = cz + chamber_radius * 0.18
            cover_cap_radius = cover_radius * 0.72

            hinge_boss_radius = max(10.0, cover_radius * 0.20)
            hinge_boss_length = max(chamber_radius * 0.95, cover_radius * 1.08)
            hinge_boss_center_x = cx - chamber_radius * 0.26
            hinge_boss_center_z = cover_base_z + cover_height * 0.78

            side_rib_thickness = max(10.0, chamber_radius * 0.16)
            side_rib_height = max(chamber_radius * 0.90, cover_height * 0.95)
            side_rib_depth = max(chamber_radius * 0.24, bore_radius * 0.48)

            disc_radius = max(bore_radius * 0.90, chamber_radius * 0.72)
            disc_thickness = max(12.0, chamber_radius * 0.18)
            disc_center = (cx - chamber_radius * 0.08, cy + chamber_radius * 0.08, cz + chamber_radius * 0.06)
            disc_open_angle = math.radians(20.0)

            hinge_pin_radius = max(5.0, hinge_boss_radius * 0.34)
            hinge_pin_span = chamber_radius * 1.18

            def _del(solid):
                try:
                    solid.Delete()
                except Exception:
                    pass

            def _track(solid):
                if solid is not None:
                    created_entities.append(solid)
                return solid

            def _axis_dir(axis_signed_str: str) -> Tuple[str, int]:
                s = (axis_signed_str or "x").lower().strip()
                if s.startswith("-"):
                    return s[1:], -1
                return s, 1

            def _rotate_solid_from_z(solid, rotate_center: Tuple[float, float, float], axis_signed_str: str):
                ax, sign = _axis_dir(axis_signed_str)
                if ax not in ("x", "y", "z"):
                    return
                px, py, pz = rotate_center
                pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz])
                if ax == "z":
                    if sign < 0:
                        pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1.0, py, pz])
                        self._call_with_retry(solid.Rotate3D, pt_center, pt_x, math.pi)
                    return
                if ax == "x":
                    pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
                    ang = (-math.pi / 2) if sign > 0 else (math.pi / 2)
                    self._call_with_retry(solid.Rotate3D, pt_center, pt_y, ang)
                    return
                pt_x = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px + 1.0, py, pz])
                ang = (-math.pi / 2) if sign > 0 else (math.pi / 2)
                self._call_with_retry(solid.Rotate3D, pt_center, pt_x, ang)

            def _rotate_solid_from_x(solid, rotate_center: Tuple[float, float, float], axis_signed_str: str):
                ax, sign = _axis_dir(axis_signed_str)
                if ax not in ("x", "y", "z"):
                    return
                px, py, pz = rotate_center
                pt_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz])
                if ax == "x":
                    if sign < 0:
                        pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
                        self._call_with_retry(solid.Rotate3D, pt_center, pt_y, math.pi)
                    return
                if ax == "y":
                    pt_z = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py, pz + 1.0])
                    ang = math.pi / 2 if sign > 0 else -math.pi / 2
                    self._call_with_retry(solid.Rotate3D, pt_center, pt_z, ang)
                    return
                pt_y = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [px, py + 1.0, pz])
                ang = math.pi / 2 if sign > 0 else -math.pi / 2
                self._call_with_retry(solid.Rotate3D, pt_center, pt_y, ang)

            def _cylinder(base_pt: Tuple[float, float, float], radius: float, height: float, axis_signed_str: str):
                axis_name, _ = _axis_dir(axis_signed_str)
                if radius <= 0 or height <= 0:
                    return None
                bx, by, bz = base_pt
                if axis_name == "z":
                    center_pt = (bx, by, bz + height / 2.0)
                elif axis_name == "y":
                    center_pt = (bx, by + height / 2.0, bz)
                else:
                    center_pt = (bx + height / 2.0, by, bz)
                center_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(center_pt))
                solid = None
                try:
                    solid = self._call_with_retry(self._get_model_space().AddCylinder, center_arr, radius, height)
                    _rotate_solid_from_z(solid, center_pt, axis_signed_str)
                    return _track(solid)
                except Exception:
                    _del(solid)
                    raise

            def _box(box_center: Tuple[float, float, float], lx: float, ly: float, lz: float):
                center_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(box_center))
                solid = None
                try:
                    solid = self._get_model_space().AddBox(center_arr, lx, ly, lz)
                    return _track(solid)
                except Exception:
                    _del(solid)
                    raise

            def _union_into(main_solid, addon_solid):
                if addon_solid is None:
                    return
                self._solid_boolean_union(main_solid, addon_solid)
                _del(addon_solid)

            body_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, cz])
            body = _track(self._call_with_retry(self._get_model_space().AddSphere, body_center, chamber_radius))
            if body is None:
                return None

            chamber_barrel = _cylinder((cx - chamber_barrel_len / 2.0, cy, cz), chamber_radius * 0.98, chamber_barrel_len, "x")
            _union_into(body, chamber_barrel)

            left_neck = _cylinder((cx - chamber_barrel_len / 2.0 - neck_len, cy, cz), neck_radius, neck_len, "x")
            _union_into(body, left_neck)
            right_neck = _cylinder((cx + chamber_barrel_len / 2.0, cy, cz), neck_radius, neck_len, "x")
            _union_into(body, right_neck)

            flow_bore = _cylinder((cx - half - 3.0, cy, cz), bore_radius, face_to_face + 6.0, "x")
            if flow_bore is not None:
                self._solid_boolean_subtract(body, flow_bore)
                _del(flow_bore)

            inlet_seat = _cylinder((cx - seat_length / 2.0 - chamber_radius * 0.30, cy, cz), seat_outer_radius, seat_length, "x")
            if inlet_seat is not None:
                inlet_seat_bore = _cylinder((cx - seat_length / 2.0 - chamber_radius * 0.30 - 1.0, cy, cz), bore_radius, seat_length + 2.0, "x")
                if inlet_seat_bore is not None:
                    self._solid_boolean_subtract(inlet_seat, inlet_seat_bore)
                    _del(inlet_seat_bore)
                _union_into(body, inlet_seat)

            outlet_seat = _cylinder((cx + chamber_radius * 0.20, cy, cz), seat_outer_radius * 0.96, seat_length * 0.90, "x")
            if outlet_seat is not None:
                outlet_seat_bore = _cylinder((cx + chamber_radius * 0.20 - 1.0, cy, cz), bore_radius, seat_length * 0.90 + 2.0, "x")
                if outlet_seat_bore is not None:
                    self._solid_boolean_subtract(outlet_seat, outlet_seat_bore)
                    _del(outlet_seat_bore)
                _union_into(body, outlet_seat)

            cover = _cylinder((cx, cy, cover_base_z), cover_radius, cover_height, "z")
            _union_into(body, cover)
            cover_cap_center = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_R8,
                [cx, cy, cover_base_z + cover_height + cover_cap_radius * 0.52],
            )
            cover_cap = _track(self._call_with_retry(self._get_model_space().AddSphere, cover_cap_center, cover_cap_radius))
            _union_into(body, cover_cap)

            hinge_boss = _cylinder(
                (hinge_boss_center_x - hinge_boss_length / 2.0, cy - hinge_boss_radius * 0.10, hinge_boss_center_z),
                hinge_boss_radius,
                hinge_boss_length,
                "x",
            )
            _union_into(body, hinge_boss)

            left_rib = _box(
                (cx - chamber_radius * 0.12, cy - cover_radius * 0.58, cover_base_z + side_rib_height / 2.0),
                side_rib_thickness,
                side_rib_depth,
                side_rib_height,
            )
            _union_into(body, left_rib)
            right_rib = _box(
                (cx - chamber_radius * 0.12, cy + cover_radius * 0.58, cover_base_z + side_rib_height / 2.0),
                side_rib_thickness,
                side_rib_depth,
                side_rib_height,
            )
            _union_into(body, right_rib)

            hinge_pin = _cylinder(
                (hinge_boss_center_x - hinge_pin_span / 2.0, cy, hinge_boss_center_z - hinge_pin_radius * 0.25),
                hinge_pin_radius,
                hinge_pin_span,
                "x",
            )
            _union_into(body, hinge_pin)

            disc = _cylinder(
                (disc_center[0], disc_center[1] - disc_thickness / 2.0, disc_center[2]),
                disc_radius,
                disc_thickness,
                "y",
            )
            if disc is not None:
                pt_disc = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [hinge_boss_center_x, cy, hinge_boss_center_z])
                pt_disc_axis = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    [hinge_boss_center_x + 1.0, cy, hinge_boss_center_z],
                )
                disc.Rotate3D(pt_disc, pt_disc_axis, -disc_open_angle)
                _union_into(body, disc)

            flange_left = _track(
                self._draw_standard_flange_from_outer_face(
                    (cx - half, cy, cz),
                    dn=dn,
                    pn=pn,
                    axis_signed="x",
                    layer=layer,
                    color=color,
                )
            )
            if not flange_left:
                _del(body)
                return None

            flange_right = _track(
                self._draw_standard_flange_from_outer_face(
                    (cx + half, cy, cz),
                    dn=dn,
                    pn=pn,
                    axis_signed="-x",
                    layer=layer,
                    color=color,
                )
            )
            if not flange_right:
                _del(body)
                _del(flange_left)
                return None

            self._solid_boolean_union(flange_left, body)
            _del(body)
            self._solid_boolean_union(flange_left, flange_right)
            _del(flange_right)

            _rotate_solid_from_x(flange_left, (cx, cy, cz), axis or "x")

            if layer:
                self.create_layer(layer)
                flange_left.Layer = layer
            if color is not None:
                flange_left.Color = color
            self._finalize_entity_display(flange_left)
            self.refresh_view()
            logger.debug(
                f"已绘制止回阀: 中心{center}, DN{dn} PN{pn}, valve_type={valve_type_key}, face_to_face={face_to_face}"
            )
            return flange_left
        except Exception as e:
            for entity in reversed(created_entities):
                _del(entity)
            logger.error(f"绘制止回阀时出错: {str(e)}")
            return None

    def draw_pressure_gauge(
        self,
        center: Tuple[float, float, float],
        nominal_diameter: int = 100,
        style: str = "radial",
        mount_type: str = "direct",
        face_axis: str = "x",
        thread_size: Optional[str] = None,
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制一般压力表（3D 实体）。参考 GB/T 1226 常用 Y-60 / Y-100 / Y-150 系列。

        约定：
        - center 为表盘中心点
        - face_axis 为表盘法向
        - style="radial" 为径向表，接头默认位于表壳下方
        - style="axial" 为轴向表，接头默认位于表壳背面中心
        - mount_type="panel" 会补一圈嵌装/凸装法兰
        """
        if not self.is_running():
            return None

        try:
            nominal = int(round(float(nominal_diameter)))
        except Exception:
            logger.error(f"draw_pressure_gauge: 非法表盘规格 {nominal_diameter}")
            return None

        gauge = self.PRESSURE_GAUGE_GB1226.get(nominal)
        if gauge is None:
            logger.error(f"draw_pressure_gauge: 暂不支持 Y-{nominal}，当前支持 {sorted(self.PRESSURE_GAUGE_GB1226)}")
            return None

        style_key = (style or "radial").strip().lower().replace("-", "_").replace(" ", "_")
        if style_key in {"bottom", "lower", "径向"}:
            style_key = "radial"
        elif style_key in {"back", "rear", "轴向"}:
            style_key = "axial"
        elif style_key not in {"radial", "axial"}:
            style_key = "radial"

        mount_key = (mount_type or "direct").strip().lower().replace("-", "_").replace(" ", "_")
        if mount_key in {"panel_mount", "flush", "flush_mount", "panel", "嵌装", "凸装"}:
            mount_key = "panel"
        elif mount_key not in {"direct", "panel"}:
            mount_key = "direct"

        cx, cy, cz = self._normalize_point3d(center)
        case_outer_radius = gauge["case_outer_diameter"] / 2.0
        case_body_radius = gauge["case_body_diameter"] / 2.0
        case_depth = gauge["case_depth"]
        bezel_depth = max(3.0, case_depth * 0.14)
        back_cap_depth = max(5.0, case_depth * 0.24)
        inner_cavity_radius = case_body_radius * 0.86
        inner_cavity_depth = max(6.0, case_depth - bezel_depth - back_cap_depth)
        dial_plate_depth = max(1.4, bezel_depth * 0.35)
        pointer_thickness_x = max(1.6, case_depth * 0.07)
        pointer_length = inner_cavity_radius * 0.74
        pointer_width = max(2.2, gauge["case_outer_diameter"] * 0.03)
        pointer_height = max(1.6, pointer_width * 0.55)
        hub_radius = max(3.0, gauge["case_outer_diameter"] * 0.045)
        hub_length = max(2.4, bezel_depth * 0.42)

        standard_thread_size = thread_size or gauge["thread_size"]
        thread_major_diameter = gauge["thread_major_diameter"]
        socket_outer_diameter = gauge["socket_outer_diameter"]
        socket_length = gauge["socket_length"]
        thread_length = gauge["thread_length"]
        hex_width = gauge["hex_width_across_flats"]
        hex_height = max(5.0, socket_length * 0.70)
        stem_blend_radius = max(socket_outer_diameter * 0.42, thread_major_diameter * 0.62)
        stem_blend_length = max(4.0, socket_length * 0.45)

        def _advance(point: Tuple[float, float, float], axis_signed_str: str, distance: float) -> Tuple[float, float, float]:
            ux, uy, uz = self._axis_unit(axis_signed_str, default="x")
            return (
                point[0] + ux * float(distance),
                point[1] + uy * float(distance),
                point[2] + uz * float(distance),
            )

        def _add_box(box_center: Tuple[float, float, float], lx: float, ly: float, lz: float) -> Any:
            center_variant = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(box_center))
            model_space = self._get_model_space()
            if model_space is None:
                return None
            return self._call_with_retry(model_space.AddBox, center_variant, float(lx), float(ly), float(lz))

        body = self._add_oriented_cylinder((cx - case_depth / 2.0, cy, cz), case_body_radius, case_depth, "x")
        if body is None:
            return None

        try:
            bezel = self._add_oriented_cylinder((cx + case_depth / 2.0 - bezel_depth, cy, cz), case_outer_radius, bezel_depth, "x")
            if bezel is not None:
                self._solid_boolean_union(body, bezel)
                self._safe_delete_entity(bezel)

            front_cavity_base = (cx + case_depth / 2.0 + 0.8, cy, cz)
            cavity = self._add_oriented_cylinder(front_cavity_base, inner_cavity_radius, inner_cavity_depth + 1.6, "-x")
            if cavity is not None:
                self._solid_boolean_subtract(body, cavity)
                self._safe_delete_entity(cavity)

            dial_plate_base = (cx + case_depth / 2.0 - bezel_depth - dial_plate_depth, cy, cz)
            dial_plate = self._add_oriented_cylinder(dial_plate_base, inner_cavity_radius * 0.97, dial_plate_depth, "x")
            if dial_plate is not None:
                self._solid_boolean_union(body, dial_plate)
                self._safe_delete_entity(dial_plate)

            hub_base = (cx + case_depth / 2.0 - bezel_depth - hub_length * 1.2, cy, cz)
            hub = self._add_oriented_cylinder(hub_base, hub_radius, hub_length, "x")
            if hub is not None:
                self._solid_boolean_union(body, hub)
                self._safe_delete_entity(hub)

            pointer_center = (
                cx + case_depth / 2.0 - bezel_depth - pointer_thickness_x * 0.7,
                cy + pointer_length * 0.28,
                cz,
            )
            pointer = _add_box(pointer_center, pointer_thickness_x, pointer_length, pointer_height)
            if pointer is not None:
                pointer_pivot = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    [cx, cy, cz],
                )
                pointer_axis = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    [cx + 1.0, cy, cz],
                )
                pointer.Rotate3D(pointer_pivot, pointer_axis, math.radians(18.0))
                self._solid_boolean_union(body, pointer)
                self._safe_delete_entity(pointer)

            secondary_pointer_center = (
                cx + case_depth / 2.0 - bezel_depth - pointer_thickness_x * 0.75,
                cy + pointer_length * 0.12,
                cz,
            )
            secondary_pointer = _add_box(
                secondary_pointer_center,
                pointer_thickness_x * 0.8,
                pointer_length * 0.42,
                pointer_height * 0.72,
            )
            if secondary_pointer is not None:
                secondary_pivot = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    [cx, cy, cz],
                )
                secondary_axis = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    [cx + 1.0, cy, cz],
                )
                secondary_pointer.Rotate3D(secondary_pivot, secondary_axis, math.radians(-36.0))
                self._solid_boolean_union(body, secondary_pointer)
                self._safe_delete_entity(secondary_pointer)

            if style_key == "axial":
                connector_axis = "-x"
                connector_base = (cx - case_depth / 2.0, cy, cz)
            else:
                connector_axis = "-z"
                connector_base = (cx, cy, cz - case_body_radius * 0.70)

            stem_blend = self._add_oriented_cylinder(connector_base, stem_blend_radius, stem_blend_length, connector_axis)
            if stem_blend is not None:
                self._solid_boolean_union(body, stem_blend)
                self._safe_delete_entity(stem_blend)

            socket_base = _advance(connector_base, connector_axis, stem_blend_length * 0.65)
            socket = self._add_oriented_cylinder(socket_base, socket_outer_diameter / 2.0, socket_length, connector_axis)
            if socket is not None:
                self._solid_boolean_union(body, socket)
                self._safe_delete_entity(socket)

            hex_base = _advance(socket_base, connector_axis, max(1.2, socket_length * 0.08))
            hex_prism = self._add_hex_prism(hex_base, hex_width, hex_height, connector_axis)
            if hex_prism is not None:
                self._solid_boolean_union(body, hex_prism)
                self._safe_delete_entity(hex_prism)

            thread_base = _advance(socket_base, connector_axis, socket_length)
            thread = self._add_oriented_cylinder(thread_base, thread_major_diameter / 2.0, thread_length, connector_axis)
            if thread is not None:
                self._solid_boolean_union(body, thread)
                self._safe_delete_entity(thread)

            if mount_key == "panel":
                flange_outer_radius = gauge["panel_flange_outer_diameter"] / 2.0
                flange_thickness = max(3.0, bezel_depth * 0.55)
                flange_base = (cx + case_depth / 2.0 - flange_thickness, cy, cz)
                panel_flange = self._add_oriented_cylinder(flange_base, flange_outer_radius, flange_thickness, "x")
                if panel_flange is not None:
                    flange_hole = self._add_oriented_cylinder(
                        (cx + case_depth / 2.0 - flange_thickness - 0.5, cy, cz),
                        case_outer_radius * 0.92,
                        flange_thickness + 1.0,
                        "x",
                    )
                    if flange_hole is not None:
                        self._solid_boolean_subtract(panel_flange, flange_hole)
                        self._safe_delete_entity(flange_hole)
                    bolt_circle_radius = gauge["panel_bolt_circle_diameter"] / 2.0
                    bolt_hole_radius = gauge["panel_bolt_hole_diameter"] / 2.0
                    bolt_count = int(gauge["panel_bolt_count"])
                    for idx in range(bolt_count):
                        angle = (2.0 * math.pi * idx / bolt_count) - (math.pi / 2.0)
                        bolt_center_y = cy + bolt_circle_radius * math.cos(angle)
                        bolt_center_z = cz + bolt_circle_radius * math.sin(angle)
                        bolt_hole = self._add_oriented_cylinder(
                            (cx + case_depth / 2.0 - flange_thickness - 0.5, bolt_center_y, bolt_center_z),
                            bolt_hole_radius,
                            flange_thickness + 1.0,
                            "x",
                        )
                        if bolt_hole is not None:
                            self._solid_boolean_subtract(panel_flange, bolt_hole)
                            self._safe_delete_entity(bolt_hole)
                    self._solid_boolean_union(body, panel_flange)
                    self._safe_delete_entity(panel_flange)

            self._rotate_solid_from_x(body, (cx, cy, cz), face_axis or "x")

            if layer:
                try:
                    self.create_layer(layer)
                    body.Layer = layer
                except Exception as layer_ex:
                    logger.warning(f"draw_pressure_gauge: 设置图层失败，保留当前图层: {layer_ex}")
            if color is not None:
                try:
                    body.Color = color
                except Exception as color_ex:
                    logger.warning(f"draw_pressure_gauge: 设置颜色失败，保留默认颜色: {color_ex}")

            self._finalize_entity_display(body)
            self.refresh_view()
            logger.debug(
                "已绘制压力表: 中心%s, Y-%s, style=%s, mount=%s, face_axis=%s, thread=%s",
                center,
                nominal,
                style_key,
                mount_key,
                face_axis,
                standard_thread_size,
            )
            return body
        except Exception as exc:
            self._safe_delete_entity(body)
            logger.error(f"绘制压力表时出错: {str(exc)}")
            return None

    def draw_rigid_waterproof_sleeve(
        self,
        center: Tuple[float, float, float],
        dn: int,
        axis: str = "x",
        sleeve_type: str = "A",
        length: Optional[float] = None,
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制刚性防水套管（3D 实体）。参考 02S404 A 型钢管穿墙套管。

        约定：
        - `center` 为套管几何中心
        - `axis` 为穿墙方向
        - 当前内置 02S404 A 型常用 DN50-DN450 标准参数
        - 尺寸表未给出 L，默认长度取 200，可通过 length 覆盖
        """
        if not self.is_running():
            return None
        sleeve_type_key = (sleeve_type or "A").strip().upper()
        if sleeve_type_key != "A":
            logger.error(f"draw_rigid_waterproof_sleeve: 当前仅支持 A 型，收到 {sleeve_type}")
            return None
        params = self.RIGID_WATERPROOF_SLEEVE_02S404_A.get(int(dn))
        if params is None:
            logger.error(f"draw_rigid_waterproof_sleeve: 当前仅内置 DN50-DN450 标准参数，收到 DN{dn}")
            return None

        sleeve_length = float(length) if length is not None and length > 0 else float(params["default_length"])
        pipe_outer_diameter = float(params.get("pipe_outer_diameter", 0.0) or 0.0)
        sleeve_outer_diameter = float(params["sleeve_outer_diameter"])
        sleeve_wall_thickness = float(params.get("sleeve_wall_thickness", 0.0) or 0.0)
        if sleeve_wall_thickness > 0:
            sleeve_inner_diameter = sleeve_outer_diameter - 2.0 * sleeve_wall_thickness
        else:
            sleeve_inner_diameter = float(params.get("sleeve_inner_diameter", 0.0) or 0.0)
        if sleeve_inner_diameter <= 0 or sleeve_inner_diameter >= sleeve_outer_diameter:
            logger.error(f"draw_rigid_waterproof_sleeve: DN{dn} 套管内外径参数不合法")
            return None
        if pipe_outer_diameter > 0 and sleeve_inner_diameter <= pipe_outer_diameter:
            logger.error(f"draw_rigid_waterproof_sleeve: DN{dn} 套管内径不大于穿管外径")
            return None
        sleeve_outer_radius = sleeve_outer_diameter / 2.0
        sleeve_inner_radius = sleeve_inner_diameter / 2.0
        wing_outer_radius = float(params["wing_ring_outer_diameter"]) / 2.0
        wing_thickness = float(params["wing_ring_thickness"])
        axis_unit = self._axis_unit(axis, default="x")
        cx, cy, cz = self._normalize_point3d(center)

        def _advance(point: Tuple[float, float, float], distance: float) -> Tuple[float, float, float]:
            return (
                point[0] + axis_unit[0] * distance,
                point[1] + axis_unit[1] * distance,
                point[2] + axis_unit[2] * distance,
            )

        try:
            body_base = _advance((cx, cy, cz), -sleeve_length / 2.0)
            body = self._add_oriented_cylinder(body_base, sleeve_outer_radius, sleeve_length, axis)
            if body is None:
                return None

            inner_cut = self._add_oriented_cylinder(
                _advance(body_base, -1.0),
                sleeve_inner_radius,
                sleeve_length + 2.0,
                axis,
            )
            if inner_cut is not None:
                self._solid_boolean_subtract(body, inner_cut)
                self._safe_delete_entity(inner_cut)

            wing_base = _advance((cx, cy, cz), -wing_thickness / 2.0)
            wing_ring = self._add_oriented_cylinder(wing_base, wing_outer_radius, wing_thickness, axis)
            if wing_ring is not None:
                wing_hole = self._add_oriented_cylinder(
                    _advance(wing_base, -0.5),
                    sleeve_outer_radius,
                    wing_thickness + 1.0,
                    axis,
                )
                if wing_hole is not None:
                    self._solid_boolean_subtract(wing_ring, wing_hole)
                    self._safe_delete_entity(wing_hole)
                self._solid_boolean_union(body, wing_ring)
                self._safe_delete_entity(wing_ring)

            if layer:
                self.create_layer(layer)
                body.Layer = layer
            if color is not None:
                body.Color = color
            self._finalize_entity_display(body)
            self.refresh_view()
            logger.debug(
                "已绘制刚性防水套管: 中心%s, DN%s, type=%s, axis=%s, length=%s",
                center,
                dn,
                sleeve_type_key,
                axis,
                sleeve_length,
            )
            return body
        except Exception as exc:
            logger.error(f"绘制刚性防水套管时出错: {str(exc)}")
            return None

    def draw_steel_bellmouth(
        self,
        center: Tuple[float, float, float],
        dn: int,
        axis: str = "x",
        flare_length: Optional[float] = None,
        throat_length: Optional[float] = None,
        mouth_outer_diameter: Optional[float] = None,
        wall_thickness: Optional[float] = None,
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制钢制喇叭口（3D 实体）。参考 02S403 吸水喇叭口系列。

        约定：
        - `center` 为小端（管道连接端）端面圆心
        - `axis` 为从小端指向喇叭口开口的方向
        - 当前公开资料可稳定确认 DN×壁厚系列；未取得完整表尺寸时按常见系列比例建模
        """
        if not self.is_running():
            return None
        if int(dn) not in self.ELBOW_DN_OD_GB12459:
            logger.error(f"draw_steel_bellmouth: 未找到 DN{dn} 对应外径")
            return None
        if axis.lstrip("-").lower() not in ("x", "y", "z"):
            logger.error(f"draw_steel_bellmouth: 不支持的轴向 {axis}")
            return None

        spec = self.STEEL_BELLMOUTH_02S403.get(int(dn), {})
        throat_outer_diameter = float(self.ELBOW_DN_OD_GB12459[int(dn)])
        throat_wall = float(wall_thickness) if wall_thickness is not None and wall_thickness > 0 else float(spec.get("wall_thickness", 6.0))
        throat_inner_diameter = throat_outer_diameter - 2.0 * throat_wall
        if throat_inner_diameter <= 0:
            logger.error("draw_steel_bellmouth: 壁厚过大，导致内径不合法")
            return None

        mouth_outer = float(mouth_outer_diameter) if mouth_outer_diameter is not None and mouth_outer_diameter > throat_outer_diameter else max(throat_outer_diameter * 1.65, throat_outer_diameter + 140.0)
        flare_len = float(flare_length) if flare_length is not None and flare_length > 0 else max(160.0, throat_outer_diameter * 0.9)
        throat_len = float(throat_length) if throat_length is not None and throat_length > 0 else max(80.0, throat_outer_diameter * 0.4)
        mouth_inner = mouth_outer - 2.0 * throat_wall
        if mouth_inner <= throat_inner_diameter:
            mouth_inner = throat_inner_diameter + 40.0
            mouth_outer = mouth_inner + 2.0 * throat_wall

        axis_unit = self._axis_unit(axis, default="x")
        cx, cy, cz = self._normalize_point3d(center)

        def _advance(point: Tuple[float, float, float], distance: float) -> Tuple[float, float, float]:
            return (
                point[0] + axis_unit[0] * distance,
                point[1] + axis_unit[1] * distance,
                point[2] + axis_unit[2] * distance,
            )

        def _add_circle_raw(circle_center: Tuple[float, float, float], radius: float):
            center_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(circle_center))
            return self._call_with_retry(self.doc.ModelSpace.AddCircle, center_arr, float(radius))

        def _loft_between_circles(start_radius: float, end_radius: float, loft_length: float):
            if start_radius <= 0 or end_radius <= 0 or loft_length <= 0:
                return None
            model_space = self._get_model_space()
            if model_space is None:
                return None
            count_before = model_space.Count
            circle_start = None
            circle_end = None
            try:
                circle_start = _add_circle_raw((0.0, 0.0, 0.0), start_radius)
                circle_end = _add_circle_raw((0.0, 0.0, loft_length), end_radius)
                cmd = (
                    f'(command "_.LOFT" (handent "{circle_start.Handle}") '
                    f'(handent "{circle_end.Handle}") "" "") '
                )
                if not self.send_command(cmd):
                    return None
                time.sleep(max(0.8, self.command_delay))
                count_after = model_space.Count
                if count_after <= count_before:
                    return None
                return model_space.Item(count_after - 1)
            finally:
                self._safe_delete_entity(circle_start)
                self._safe_delete_entity(circle_end)

        try:
            body = self._add_oriented_cylinder((cx, cy, cz), throat_outer_diameter / 2.0, throat_len, axis)
            if body is None:
                return None
            flare_outer = _loft_between_circles(throat_outer_diameter / 2.0, mouth_outer / 2.0, flare_len)
            if flare_outer is None:
                self._safe_delete_entity(body)
                logger.error("draw_steel_bellmouth: 创建外扩喇叭段失败")
                return None
            flare_start = _advance((cx, cy, cz), throat_len)
            self._rotate_solid_from_z(flare_outer, (0.0, 0.0, 0.0), axis)
            flare_outer.Move(
                win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 0.0, 0.0]),
                win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(flare_start)),
            )
            self._solid_boolean_union(body, flare_outer)
            self._safe_delete_entity(flare_outer)

            throat_inner = self._add_oriented_cylinder(
                _advance((cx, cy, cz), -1.0),
                throat_inner_diameter / 2.0,
                throat_len + 2.0,
                axis,
            )
            if throat_inner is not None:
                self._solid_boolean_subtract(body, throat_inner)
                self._safe_delete_entity(throat_inner)

            flare_inner = _loft_between_circles(throat_inner_diameter / 2.0, mouth_inner / 2.0, flare_len + 2.0)
            if flare_inner is not None:
                self._rotate_solid_from_z(flare_inner, (0.0, 0.0, 0.0), axis)
                flare_inner.Move(
                    win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 0.0, 0.0]),
                    win32com.client.VARIANT(
                        pythoncom.VT_ARRAY | pythoncom.VT_R8,
                        list(_advance(flare_start, -1.0)),
                    ),
                )
                self._solid_boolean_subtract(body, flare_inner)
                self._safe_delete_entity(flare_inner)

            if layer:
                self.create_layer(layer)
                body.Layer = layer
            if color is not None:
                body.Color = color
            self._finalize_entity_display(body)
            self.refresh_view()
            logger.debug(
                "已绘制钢制喇叭口: 中心%s, DN%s, axis=%s, throat_len=%s, flare_len=%s, mouth_od=%s, wt=%s",
                center,
                dn,
                axis,
                throat_len,
                flare_len,
                mouth_outer,
                throat_wall,
            )
            return body
        except Exception as exc:
            logger.error(f"绘制钢制喇叭口时出错: {str(exc)}")
            return None

    def draw_suction_bellmouth_support(
        self,
        center: Tuple[float, float, float],
        support_model: str = "ZB3",
        bellmouth_form: str = "J",
        support_type: str = "B",
        axis: str = "x",
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制吸水喇叭管支架（3D 实体）。参考 02S403 ZA/ZB/ZC/ZD 支架系列。

        约定：
        - `center` 为支架几何中心
        - `axis` 为支架与喇叭管同轴方向，从小端指向大端
        - 当前优先内置 ZB3 / J / B 的公开规格 φ426×φ630；细部板厚和锥段高度按工程稳定比例表达
        """
        if not self.is_running():
            return None
        if axis.lstrip("-").lower() not in ("x", "y", "z"):
            logger.error(f"draw_suction_bellmouth_support: 不支持的轴向 {axis}")
            return None

        key = (
            str(support_model or "ZB3").strip().upper(),
            str(bellmouth_form or "J").strip().upper(),
            str(support_type or "B").strip().upper(),
        )
        spec = self.SUCTION_BELLMOUTH_SUPPORT_02S403.get(key)
        if spec is None:
            logger.error(
                "draw_suction_bellmouth_support: 当前仅内置 ZB3/J/B 标准参数，收到 model=%s form=%s type=%s",
                support_model,
                bellmouth_form,
                support_type,
            )
            return None

        small_diameter = float(spec["small_diameter"])
        large_diameter = float(spec["large_diameter"])
        support_height = float(spec["support_height"])
        shell_thickness = float(spec["shell_thickness"])
        ring_band_width = float(spec["ring_band_width"])
        middle_ring_ratio = float(spec["middle_ring_ratio"])
        axis_unit = self._axis_unit(axis, default="x")
        cx, cy, cz = self._normalize_point3d(center)

        def _advance(point: Tuple[float, float, float], distance: float) -> Tuple[float, float, float]:
            return (
                point[0] + axis_unit[0] * distance,
                point[1] + axis_unit[1] * distance,
                point[2] + axis_unit[2] * distance,
            )

        def _add_circle_raw(circle_center: Tuple[float, float, float], radius: float):
            center_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(circle_center))
            return self._call_with_retry(self.doc.ModelSpace.AddCircle, center_arr, float(radius))

        def _loft_between_circles(start_radius: float, end_radius: float, loft_length: float):
            if start_radius <= 0 or end_radius <= 0 or loft_length <= 0:
                return None
            model_space = self._get_model_space()
            if model_space is None:
                return None
            count_before = model_space.Count
            circle_start = None
            circle_end = None
            try:
                circle_start = _add_circle_raw((0.0, 0.0, 0.0), start_radius)
                circle_end = _add_circle_raw((0.0, 0.0, loft_length), end_radius)
                cmd = (
                    f'(command "_.LOFT" (handent "{circle_start.Handle}") '
                    f'(handent "{circle_end.Handle}") "" "") '
                )
                if not self.send_command(cmd):
                    return None
                time.sleep(max(0.8, self.command_delay))
                count_after = model_space.Count
                if count_after <= count_before:
                    return None
                return model_space.Item(count_after - 1)
            finally:
                self._safe_delete_entity(circle_start)
                self._safe_delete_entity(circle_end)

        def _create_ring(axis_offset: float, outer_diameter: float, inner_diameter: float) -> Any:
            ring_base = _advance((cx, cy, cz), axis_offset - ring_band_width / 2.0)
            ring = self._add_oriented_cylinder(ring_base, outer_diameter / 2.0, ring_band_width, axis)
            if ring is None:
                return None
            hole = self._add_oriented_cylinder(
                _advance(ring_base, -1.0),
                inner_diameter / 2.0,
                ring_band_width + 2.0,
                axis,
            )
            if hole is not None:
                self._solid_boolean_subtract(ring, hole)
                self._safe_delete_entity(hole)
            return ring

        try:
            outer_shell = _loft_between_circles(
                (small_diameter + 2.0 * shell_thickness) / 2.0,
                (large_diameter + 2.0 * shell_thickness) / 2.0,
                support_height,
            )
            if outer_shell is None:
                logger.error("draw_suction_bellmouth_support: 创建外轮廓失败")
                return None
            inner_shell = _loft_between_circles(
                small_diameter / 2.0,
                large_diameter / 2.0,
                support_height + 2.0,
            )
            self._rotate_solid_from_z(outer_shell, (0.0, 0.0, 0.0), axis)
            outer_shell.Move(
                win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 0.0, 0.0]),
                win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    list(_advance((cx, cy, cz), -support_height / 2.0)),
                ),
            )
            if inner_shell is not None:
                self._rotate_solid_from_z(inner_shell, (0.0, 0.0, 0.0), axis)
                inner_shell.Move(
                    win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 0.0, 0.0]),
                    win32com.client.VARIANT(
                        pythoncom.VT_ARRAY | pythoncom.VT_R8,
                        list(_advance((cx, cy, cz), -support_height / 2.0 - 1.0)),
                    ),
                )
                self._solid_boolean_subtract(outer_shell, inner_shell)
                self._safe_delete_entity(inner_shell)

            top_ring = _create_ring(-support_height / 2.0 + ring_band_width / 2.0, small_diameter + 2.0 * shell_thickness, small_diameter)
            if top_ring is not None:
                self._solid_boolean_union(outer_shell, top_ring)
                self._safe_delete_entity(top_ring)

            bottom_ring = _create_ring(support_height / 2.0 - ring_band_width / 2.0, large_diameter + 2.0 * shell_thickness, large_diameter)
            if bottom_ring is not None:
                self._solid_boolean_union(outer_shell, bottom_ring)
                self._safe_delete_entity(bottom_ring)

            middle_diameter = small_diameter + (large_diameter - small_diameter) * middle_ring_ratio
            middle_ring = _create_ring(0.0, middle_diameter + 2.0 * shell_thickness, middle_diameter)
            if middle_ring is not None:
                self._solid_boolean_union(outer_shell, middle_ring)
                self._safe_delete_entity(middle_ring)

            if layer:
                self.create_layer(layer)
                outer_shell.Layer = layer
            if color is not None:
                try:
                    outer_shell.Color = color
                except Exception as color_ex:
                    logger.warning(f"draw_suction_bellmouth_support: 设置颜色失败，已保留默认颜色: {color_ex}")
            self._finalize_entity_display(outer_shell)
            self.refresh_view()
            logger.debug(
                "已绘制吸水喇叭管支架: center=%s, model=%s, form=%s, type=%s, axis=%s, d_small=%s, d_large=%s",
                center,
                key[0],
                key[1],
                key[2],
                axis,
                small_diameter,
                large_diameter,
            )
            return outer_shell
        except Exception as exc:
            logger.error(f"绘制吸水喇叭管支架时出错: {str(exc)}")
            return None

    def draw_double_flange_limit_expansion_joint(
        self,
        center: Tuple[float, float, float],
        dn: int,
        model: str = "JSSJA-2",
        pn: float = 1.0,
        axis: str = "x",
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制双法兰限位伸缩接头（3D 实体）。参考 GB/T 12465、VSSJA-2(B2F) / JSSJA-2。

        约定：
        - `center` 为整套接头几何中心
        - `axis` 为流道轴向
        - 当前优先内置 DN250 PN1.0 的关键查表尺寸；中部筒体/压盖等未公开细部按稳定工程比例表达
        """
        if not self.is_running():
            return None
        if axis.lstrip("-").lower() not in ("x", "y", "z"):
            logger.error(f"draw_double_flange_limit_expansion_joint: 不支持的轴向 {axis}")
            return None

        model_key = self.DOUBLE_FLANGE_LIMIT_EXPANSION_JOINT_MODEL_ALIASES.get(
            (model or "JSSJA-2").strip().lower(),
            (model or "JSSJA-2").strip(),
        )
        spec = self.DOUBLE_FLANGE_LIMIT_EXPANSION_JOINT_GBT12465.get((model_key, int(dn), float(pn)))
        if spec is None:
            logger.error(
                "draw_double_flange_limit_expansion_joint: 当前仅内置 %s DN250 PN1.0 标准参数，收到 model=%s DN%s PN%s",
                model_key,
                model,
                dn,
                pn,
            )
            return None

        axis_unit = self._axis_unit(axis, default="x")
        cx, cy, cz = self._normalize_point3d(center)
        flange_thickness = float(spec["flange_thickness"])
        overall_length = float(spec["overall_length"])
        pipe_outer_diameter = float(spec["pipe_outer_diameter"])
        shell_outer_diameter = float(spec["shell_outer_diameter"])
        gland_outer_diameter = float(spec["gland_outer_diameter"])
        center_barrel_length = float(spec["center_barrel_length"])
        gland_length = float(spec["gland_length"])
        flange_outer_diameter = float(spec["flange_outer_diameter"])
        bolt_circle_diameter = float(spec["flange_bolt_circle_diameter"])
        bolt_hole_diameter = float(spec["flange_bolt_hole_diameter"])
        bolt_count = int(spec["flange_bolt_count"])
        nominal_bore = float(spec.get("nominal_bore", dn))
        tie_rod_count = int(spec.get("tie_rod_count", 4))
        tie_rod_thread_size = str(spec.get("tie_rod_thread_size", "M20"))
        tie_rod_length_extra = float(spec.get("tie_rod_length_extra", 80.0))

        pipe_inner_diameter = max(nominal_bore, pipe_outer_diameter - 18.0)
        if pipe_inner_diameter >= pipe_outer_diameter:
            pipe_inner_diameter = pipe_outer_diameter - 8.0

        def _advance(point: Tuple[float, float, float], distance: float) -> Tuple[float, float, float]:
            return (
                point[0] + axis_unit[0] * distance,
                point[1] + axis_unit[1] * distance,
                point[2] + axis_unit[2] * distance,
            )

        def _local_point(axis_distance: float = 0.0, radial_u: float = 0.0, radial_v: float = 0.0) -> Tuple[float, float, float]:
            return (
                cx + axis_unit[0] * axis_distance + basis_u[0] * radial_u + basis_v[0] * radial_v,
                cy + axis_unit[1] * axis_distance + basis_u[1] * radial_u + basis_v[1] * radial_v,
                cz + axis_unit[2] * axis_distance + basis_u[2] * radial_u + basis_v[2] * radial_v,
            )

        axis_name = axis.lstrip("-").lower()
        if axis_name == "x":
            basis_u = (0.0, 1.0, 0.0)
            basis_v = (0.0, 0.0, 1.0)
        elif axis_name == "y":
            basis_u = (1.0, 0.0, 0.0)
            basis_v = (0.0, 0.0, 1.0)
        else:
            basis_u = (1.0, 0.0, 0.0)
            basis_v = (0.0, 1.0, 0.0)

        def _create_oriented_flange(base_point: Tuple[float, float, float]) -> Any:
            flange = self._add_oriented_cylinder(base_point, flange_outer_diameter / 2.0, flange_thickness, axis)
            if flange is None:
                return None

            # 为了确保与中间接管形成稳定并集，法兰中心孔略小于接管外径。
            center_hole = self._add_oriented_cylinder(
                _advance(base_point, -1.0),
                max(pipe_outer_diameter - 4.0, pipe_inner_diameter + 4.0) / 2.0,
                flange_thickness + 2.0,
                axis,
            )
            if center_hole is not None:
                self._solid_boolean_subtract(flange, center_hole)
                self._safe_delete_entity(center_hole)

            hole_radius = bolt_hole_diameter / 2.0
            bolt_radius = bolt_circle_diameter / 2.0
            for index in range(bolt_count):
                angle = 2.0 * math.pi * index / bolt_count
                hole_center = _local_point(
                    axis_distance=(base_point[0] - cx) if axis_name == "x" else ((base_point[1] - cy) if axis_name == "y" else (base_point[2] - cz)),
                    radial_u=bolt_radius * math.cos(angle),
                    radial_v=bolt_radius * math.sin(angle),
                )
                hole_base = _advance(hole_center, -1.0)
                hole = self._add_oriented_cylinder(hole_base, hole_radius, flange_thickness + 2.0, axis)
                if hole is not None:
                    self._solid_boolean_subtract(flange, hole)
                    self._safe_delete_entity(hole)
            return flange

        try:
            left_base = _advance((cx, cy, cz), -overall_length / 2.0)
            body = self._add_oriented_cylinder(left_base, pipe_outer_diameter / 2.0, overall_length, axis)
            if body is None:
                return None

            left_flange = _create_oriented_flange(left_base)
            if left_flange is not None:
                self._solid_boolean_union(body, left_flange)
                self._safe_delete_entity(left_flange)

            right_flange_base = _advance((cx, cy, cz), overall_length / 2.0 - flange_thickness)
            right_flange = _create_oriented_flange(right_flange_base)
            if right_flange is not None:
                self._solid_boolean_union(body, right_flange)
                self._safe_delete_entity(right_flange)

            center_shell_base = _advance((cx, cy, cz), -center_barrel_length / 2.0)
            center_shell = self._add_oriented_cylinder(
                center_shell_base,
                shell_outer_diameter / 2.0,
                center_barrel_length,
                axis,
            )
            if center_shell is not None:
                self._solid_boolean_union(body, center_shell)
                self._safe_delete_entity(center_shell)

            gland_axis_offset = center_barrel_length / 2.0 + gland_length * 0.15
            for direction in (-1.0, 1.0):
                gland_center_axis = direction * gland_axis_offset
                gland_base = _advance(
                    _local_point(axis_distance=gland_center_axis),
                    -gland_length / 2.0,
                )
                gland = self._add_oriented_cylinder(
                    gland_base,
                    gland_outer_diameter / 2.0,
                    gland_length,
                    axis,
                )
                if gland is not None:
                    self._solid_boolean_union(body, gland)
                    self._safe_delete_entity(gland)

            bore = self._add_oriented_cylinder(
                _advance(left_base, -1.0),
                pipe_inner_diameter / 2.0,
                overall_length + 2.0,
                axis,
            )
            if bore is not None:
                self._solid_boolean_subtract(body, bore)
                self._safe_delete_entity(bore)

            # 限位拉杆与螺母/垫圈保持为独立实体，便于保留穿杆视觉效果。
            rod_params = self.FASTENER_BOLT_GB5782.get(tie_rod_thread_size, self.FASTENER_BOLT_GB5782["M20"])
            nut_params = self.FASTENER_NUT_GB6170_1.get(tie_rod_thread_size, self.FASTENER_NUT_GB6170_1["M20"])
            washer_params = self.FASTENER_WASHER_GB97_1.get(tie_rod_thread_size, self.FASTENER_WASHER_GB97_1["M20"])
            tie_rod_length = overall_length + tie_rod_length_extra
            tie_rod_base = _advance((cx, cy, cz), -tie_rod_length / 2.0)
            rod_radius = bolt_circle_diameter / 2.0
            for index in range(tie_rod_count):
                angle = 2.0 * math.pi * index / tie_rod_count
                rod_origin = _local_point(
                    axis_distance=0.0,
                    radial_u=rod_radius * math.cos(angle),
                    radial_v=rod_radius * math.sin(angle),
                )
                rod = self._add_oriented_cylinder(
                    _advance(rod_origin, -tie_rod_length / 2.0),
                    float(rod_params["d"]) / 2.0,
                    tie_rod_length,
                    axis,
                )
                if rod is not None:
                    if layer:
                        try:
                            self.create_layer(layer)
                            rod.Layer = layer
                        except Exception:
                            pass
                    if color is not None:
                        try:
                            rod.Color = color
                        except Exception:
                            pass
                    self._finalize_entity_display(rod)

                left_washer = self.draw_washer(
                    center=_advance(rod_origin, -(overall_length / 2.0 + washer_params["h"] / 2.0 + 8.0)),
                    specification="GB/T 97.1",
                    thread_size=tie_rod_thread_size,
                    axis=axis,
                    layer=layer,
                    color=color,
                )
                left_nut = self.draw_hex_nut(
                    center=_advance(
                        rod_origin,
                        -(overall_length / 2.0 + washer_params["h"] + nut_params["m"] / 2.0 + 10.0),
                    ),
                    specification="GB/T 6170.1",
                    thread_size=tie_rod_thread_size,
                    axis=axis,
                    layer=layer,
                    color=color,
                )
                right_washer = self.draw_washer(
                    center=_advance(rod_origin, overall_length / 2.0 + washer_params["h"] / 2.0 + 8.0),
                    specification="GB/T 97.1",
                    thread_size=tie_rod_thread_size,
                    axis=axis,
                    layer=layer,
                    color=color,
                )
                right_nut = self.draw_hex_nut(
                    center=_advance(
                        rod_origin,
                        overall_length / 2.0 + washer_params["h"] + nut_params["m"] / 2.0 + 10.0,
                    ),
                    specification="GB/T 6170.1",
                    thread_size=tie_rod_thread_size,
                    axis=axis,
                    layer=layer,
                    color=color,
                )
                _ = (left_washer, left_nut, right_washer, right_nut)

            if layer:
                self.create_layer(layer)
                body.Layer = layer
            if color is not None:
                try:
                    body.Color = color
                except Exception as color_ex:
                    logger.warning(f"draw_double_flange_limit_expansion_joint: 设置颜色失败，已保留默认颜色: {color_ex}")
            self._finalize_entity_display(body)
            self.refresh_view()
            logger.debug(
                "已绘制双法兰限位伸缩接头: center=%s, model=%s, dn=%s, pn=%s, axis=%s, L=%s",
                center,
                model_key,
                dn,
                pn,
                axis,
                overall_length,
            )
            return body
        except Exception as exc:
            logger.error(f"绘制双法兰限位伸缩接头时出错: {str(exc)}")
            return None

    def draw_elliptical_head(
        self,
        center: Tuple[float, float, float],
        dn: float,
        axis: str = "z",
        with_straight: bool = False,
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制椭圆封头（3D 实体）。依据国标 GB/T 25198 压力容器封头。

        默认仅绘制椭圆曲面（2:1 椭圆，内高 = 内径/4），不包含直边段；需直边段时可设 with_straight=True。
        直边高度：DN≤2000 为 25mm，DN>2000 为 40mm。

        Args:
            center: 封头开口面圆心 (x, y, z)，封头向 axis 正方向凸出
            dn: 公称直径（mm），即封头内径
            axis: 凸出方向轴向，默认 "z"（封头向 +Z 凸出）
            with_straight: 是否带直边段，默认 False（仅椭圆曲面）
            layer: 图层名称（可选）
            color: 颜色索引（可选）

        Returns:
            创建的 3DSolid 椭圆封头实体，失败返回 None
        """
        if not self.is_running():
            return None
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0.0)
            cx, cy, cz = center
            Di = float(dn)
            if Di <= 0:
                logger.error("draw_elliptical_head: dn 须大于 0")
                return None

            h_straight = self.ELLIPTICAL_HEAD_STRAIGHT_HEIGHT_LARGE if Di > 2000 else self.ELLIPTICAL_HEAD_STRAIGHT_HEIGHT
            if not with_straight:
                h_straight = 0

            a = Di / 2.0
            b = Di / 4.0

            # 轮廓在局部 XY 平面构建：
            # - y=0 为封头开口面
            # - 直边段位于开口侧，而不是封头顶部
            # - 顶点总高度 = 椭圆高 b + 直边高 h_straight
            num_seg = 24
            profile_offset_y = float(h_straight)
            points_2d = [(0.0, 0.0), (a, 0.0)]
            if h_straight > 0:
                points_2d.append((a, profile_offset_y))
            start_index = 1 if h_straight > 0 else 0
            for i in range(start_index, num_seg + 1):
                t = (i / num_seg) * (math.pi / 2.0)
                points_2d.append((a * math.cos(t), profile_offset_y + b * math.sin(t)))
            points_2d.append((0.0, 0.0))

            arr = []
            for p in points_2d:
                arr.extend([p[0], p[1]])
            points_variant = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, arr)

            try:
                pline = self.doc.ModelSpace.AddLightWeightPolyline(points_variant)
            except Exception:
                arr3 = [coord for p in points_2d for coord in (p[0], p[1], 0.0)]
                pline = self.doc.ModelSpace.AddPolyline(win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, arr3))
            if pline is None:
                logger.error("draw_elliptical_head: 创建多段线失败")
                return None

            pline.Closed = True
            curves = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [pline])
            regions = self.doc.ModelSpace.AddRegion(curves)
            if regions is None or (hasattr(regions, "__len__") and len(regions) == 0):
                try:
                    pline.Delete()
                except Exception:
                    pass
                logger.error("draw_elliptical_head: 创建面域失败")
                return None
            region = regions[0] if hasattr(regions, "__len__") else regions

            pt = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 0.0, 0.0])
            vec = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 1.0, 0.0])
            head = self.doc.ModelSpace.AddRevolvedSolid(region, pt, vec, 2.0 * math.pi)
            try:
                pline.Delete()
            except Exception:
                pass
            try:
                region.Delete()
            except Exception:
                pass
            if head is None:
                logger.error("draw_elliptical_head: 旋转生成实体失败")
                return None

            pt_rot1 = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 0.0, 0.0])
            pt_rot2 = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [1.0, 0.0, 0.0])
            head.Rotate3D(pt_rot1, pt_rot2, math.radians(90))

            pt_move_from = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 0.0, 0.0])
            pt_move_to = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, cz])
            head.Move(pt_move_from, pt_move_to)
            self._rotate_solid_from_z(head, (cx, cy, cz), axis, default="z")

            if layer:
                self.create_layer(layer)
                head.Layer = layer
            if color is not None:
                head.Color = color
            self.refresh_view()
            logger.debug(f"已绘制椭圆封头: 中心{center}, DN{Di}, 轴向{axis}, 直边{h_straight}mm")
            return head
        except Exception as e:
            logger.error(f"绘制椭圆封头时出错: {str(e)}")
            return None

    def draw_flange(
        self,
        center: Tuple[float, float, float],
        specification: Optional[str] = None,
        dn: Optional[int] = None,
        pn: Optional[float] = None,
        outer_diameter: Optional[float] = None,
        center_hole_diameter: Optional[float] = None,
        bolt_circle_diameter: Optional[float] = None,
        bolt_hole_diameter: Optional[float] = None,
        bolt_count: Optional[int] = None,
        thickness: Optional[float] = None,
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制法兰盘（3D 实体，沿 Z 轴扁圆柱，带中心孔与均布螺栓孔）。

        支持国标 GB/T 9119-2010 板式平焊钢制管法兰：指定 specification="GB/T 9119" 及 dn、pn 时，
        从内置尺寸表查取外径、螺栓孔中心圆、螺栓孔直径与数量、厚度、内径。

        也可完全自定义尺寸（单位与 CAD 一致，通常为 mm）：outer_diameter、center_hole_diameter、
        bolt_circle_diameter、bolt_hole_diameter、bolt_count、thickness。

        Args:
            center: 法兰底面圆心 (x, y, z)，法兰沿 Z 轴正方向拉伸。
            specification: 标准代号，当前支持 "GB/T 9119"；不指定则使用自定义参数。
            dn: 公称通径（mm），与 specification 配合使用。
            pn: 公称压力（MPa），如 2.5、6、10、16；与 specification 配合使用。
            outer_diameter: 法兰外径（自定义时必填）。
            center_hole_diameter: 中心孔（内径）直径（自定义时必填）。
            bolt_circle_diameter: 螺栓孔中心圆直径（自定义时必填）。
            bolt_hole_diameter: 螺栓孔直径（自定义时必填）。
            bolt_count: 螺栓孔数量（自定义时必填）。
            thickness: 法兰厚度（自定义时必填；国标时由表查得）。
            layer: 图层名称（可选）。
            color: 颜色索引（可选）。

        Returns:
            创建的 3DSolid 法兰实体，失败返回 None。
        """
        if not self.is_running():
            return None

        # 国标查表或自定义参数（单位 mm）
        if (specification or "").strip().upper().replace(" ", "") in ("GB/T9119", "GBT9119"):
            if dn is None or pn is None:
                logger.error("draw_flange: 使用国标 GB/T 9119 时必须指定 dn 和 pn")
                return None
            key = (int(dn), float(pn))
            if key not in self.FLANGE_GB9119:
                logger.error(f"draw_flange: 未找到国标 GB/T 9119 规格 DN{dn} PN{pn}，请使用自定义参数或查阅标准")
                return None
            row = self.FLANGE_GB9119[key]
            outer_diameter = float(row["D"])
            center_hole_diameter = float(row["d"])
            bolt_circle_diameter = float(row["K"])
            bolt_hole_diameter = float(row["L"])
            bolt_count = int(row["n"])
            thickness = float(row["C"])

        if outer_diameter is None or center_hole_diameter is None or bolt_circle_diameter is None or bolt_hole_diameter is None or bolt_count is None or thickness is None:
            logger.error("draw_flange: 缺少必要尺寸（国标须指定 dn/pn，自定义须指定 outer_diameter/center_hole_diameter/bolt_circle_diameter/bolt_hole_diameter/bolt_count/thickness）")
            return None

        if outer_diameter <= 0 or thickness <= 0 or bolt_count < 2:
            logger.error("draw_flange: 外径、厚度须大于 0，螺栓孔数至少为 2")
            return None

        try:
            if len(center) == 2:
                center = (center[0], center[1], 0.0)
            cx, cy, cz = center

            # AutoCAD ActiveX 的 AddCylinder 以实体几何中心为基准。
            # 对外保持“底面圆心 + 厚度”的语义，因此先换算到实体中心。
            solid_center_z = cz + float(thickness) / 2.0
            center_array = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_R8,
                [cx, cy, solid_center_z],
            )
            outer_radius = outer_diameter / 2.0
            flange = self.doc.ModelSpace.AddCylinder(center_array, outer_radius, thickness)
            if flange is None:
                return None

            # 中心孔：圆柱布尔减，高度略大于法兰厚以免未切透
            hole_height = thickness + 2.0
            center_hole_radius = center_hole_diameter / 2.0
            if center_hole_radius > 0 and center_hole_radius < outer_radius:
                center_hole_center = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    [cx, cy, solid_center_z],
                )
                center_hole = self.doc.ModelSpace.AddCylinder(center_hole_center, center_hole_radius, hole_height)
                if center_hole:
                    self._solid_boolean_subtract(flange, center_hole)
                    try:
                        center_hole.Delete()
                    except Exception:
                        pass

            # 螺栓孔：均布在 bolt_circle_diameter 的圆上，第一个孔在 +X 方向
            bolt_radius = bolt_hole_diameter / 2.0
            bolt_circle_radius = bolt_circle_diameter / 2.0
            if bolt_count > 0 and bolt_radius > 0 and bolt_circle_radius >= outer_radius:
                logger.error("draw_flange: 螺栓孔中心圆半径应小于法兰外圆半径")
                try:
                    flange.Delete()
                except Exception:
                    pass
                return None
            for i in range(bolt_count):
                angle = 2.0 * math.pi * i / bolt_count
                bx = cx + bolt_circle_radius * math.cos(angle)
                by = cy + bolt_circle_radius * math.sin(angle)
                bolt_center = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    [bx, by, solid_center_z],
                )
                bolt_hole = self.doc.ModelSpace.AddCylinder(bolt_center, bolt_radius, hole_height)
                if bolt_hole:
                    self._solid_boolean_subtract(flange, bolt_hole)
                    try:
                        bolt_hole.Delete()
                    except Exception:
                        pass

            if layer:
                self.create_layer(layer)
                flange.Layer = layer
            if color is not None:
                flange.Color = color
            self.refresh_view()
            logger.debug(
                f"已绘制法兰: 中心{center}, 外径{outer_diameter}, 厚度{thickness}, 螺栓孔数{bolt_count}"
            )
            return flange
        except Exception as e:
            logger.error(f"绘制法兰时出错: {str(e)}")
            return None

    def _draw_standard_flange_from_outer_face(
        self,
        outer_face_center: Tuple[float, float, float],
        *,
        dn: int,
        pn: float,
        axis_signed: str = "x",
        layer: str = None,
        color: int = None,
    ) -> Any:
        """按“外侧端面圆心 + 轴向”语义绘制一只国标法兰。"""
        flange = self.draw_flange(
            self._normalize_point3d(outer_face_center),
            specification="GB/T 9119",
            dn=dn,
            pn=pn,
            layer=layer,
            color=color,
        )
        if flange is None:
            return None
        self._rotate_solid_from_z(flange, self._normalize_point3d(outer_face_center), axis_signed, default="x")
        return flange

    def draw_blind_flange(
        self,
        center: Tuple[float, float, float],
        specification: Optional[str] = None,
        dn: Optional[int] = None,
        pn: Optional[float] = None,
        outer_diameter: Optional[float] = None,
        bolt_circle_diameter: Optional[float] = None,
        bolt_hole_diameter: Optional[float] = None,
        bolt_count: Optional[int] = None,
        thickness: Optional[float] = None,
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制盲板（法兰盖，3D 实体）。依据国标 GB/T 9123 等，与同规格法兰外径、螺栓孔一致，但无中心孔，用于封堵管口。

        国标：指定 specification="GB/T 9119" 及 dn、pn 时，从 FLANGE_GB9119 取 D、K、L、n、C（盲板不设中心孔）。
        自定义：outer_diameter、bolt_circle_diameter、bolt_hole_diameter、bolt_count、thickness。

        Args:
            center: 盲板底面圆心 (x, y, z)，沿 Z 轴正方向为厚度
            specification: 标准代号，如 "GB/T 9119"
            dn: 公称通径（mm），与 specification 配合使用
            pn: 公称压力（MPa），与 specification 配合使用
            outer_diameter: 外径（自定义时必填）
            bolt_circle_diameter: 螺栓孔中心圆直径（自定义时必填）
            bolt_hole_diameter: 螺栓孔直径（自定义时必填）
            bolt_count: 螺栓孔数量（自定义时必填）
            thickness: 厚度（自定义时必填）
            layer: 图层名称（可选）
            color: 颜色索引（可选）

        Returns:
            创建的 3DSolid 盲板实体，失败返回 None
        """
        if not self.is_running():
            return None

        if (specification or "").strip().upper().replace(" ", "") in ("GB/T9119", "GBT9119"):
            if dn is None or pn is None:
                logger.error("draw_blind_flange: 使用国标 GB/T 9119 时必须指定 dn 和 pn")
                return None
            key = (int(dn), float(pn))
            if key not in self.FLANGE_GB9119:
                logger.error(f"draw_blind_flange: 未找到国标规格 DN{dn} PN{pn}")
                return None
            row = self.FLANGE_GB9119[key]
            outer_diameter = float(row["D"])
            bolt_circle_diameter = float(row["K"])
            bolt_hole_diameter = float(row["L"])
            bolt_count = int(row["n"])
            thickness = float(row["C"])

        if outer_diameter is None or bolt_circle_diameter is None or bolt_hole_diameter is None or bolt_count is None or thickness is None:
            logger.error("draw_blind_flange: 缺少必要尺寸（国标须指定 dn/pn，自定义须指定 outer_diameter/bolt_circle_diameter/bolt_hole_diameter/bolt_count/thickness）")
            return None

        if outer_diameter <= 0 or thickness <= 0 or bolt_count < 2:
            logger.error("draw_blind_flange: 外径、厚度须大于 0，螺栓孔数至少为 2")
            return None

        try:
            if len(center) == 2:
                center = (center[0], center[1], 0.0)
            cx, cy, cz = center
            solid_center_z = cz + float(thickness) / 2.0
            center_array = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_R8,
                [cx, cy, solid_center_z],
            )
            outer_radius = outer_diameter / 2.0
            blind = self.doc.ModelSpace.AddCylinder(center_array, outer_radius, thickness)
            if blind is None:
                return None

            hole_height = thickness + 2.0
            bolt_radius = bolt_hole_diameter / 2.0
            bolt_circle_radius = bolt_circle_diameter / 2.0
            if bolt_circle_radius >= outer_radius:
                logger.error("draw_blind_flange: 螺栓孔中心圆半径应小于外圆半径")
                try:
                    blind.Delete()
                except Exception:
                    pass
                return None
            for i in range(bolt_count):
                angle = 2.0 * math.pi * i / bolt_count
                bx = cx + bolt_circle_radius * math.cos(angle)
                by = cy + bolt_circle_radius * math.sin(angle)
                bolt_center = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    [bx, by, solid_center_z],
                )
                bolt_hole = self.doc.ModelSpace.AddCylinder(bolt_center, bolt_radius, hole_height)
                if bolt_hole:
                    self._solid_boolean_subtract(blind, bolt_hole)
                    try:
                        bolt_hole.Delete()
                    except Exception:
                        pass

            if layer:
                self.create_layer(layer)
                blind.Layer = layer
            if color is not None:
                blind.Color = color
            self.refresh_view()
            logger.debug(f"已绘制盲板: 中心{center}, 外径{outer_diameter}, 厚度{thickness}, 螺栓孔数{bolt_count}")
            return blind
        except Exception as e:
            logger.error(f"绘制盲板时出错: {str(e)}")
            return None

    def draw_manhole(
        self,
        center: Tuple[float, float, float],
        *,
        specification: Optional[str] = None,
        dn: Optional[int] = None,
        pn: Optional[float] = None,
        opening_diameter: Optional[float] = None,
        neck_outer_diameter: Optional[float] = None,
        neck_wall_thickness: float = 10.0,
        neck_height: float = 120.0,
        flange_outer_diameter: Optional[float] = None,
        bolt_circle_diameter: Optional[float] = None,
        bolt_hole_diameter: Optional[float] = None,
        bolt_count: Optional[int] = None,
        flange_thickness: Optional[float] = None,
        cover_outer_diameter: Optional[float] = None,
        cover_thickness: float = 20.0,
        cover_gap: float = 6.0,
        handle_count: int = 2,
        handle_width: float = 180.0,
        handle_height: float = 60.0,
        handle_bar_diameter: float = 16.0,
        add_lifting_lug: bool = False,
        lug_width: float = 56.0,
        lug_thickness: float = 20.0,
        lug_height: float = 110.0,
        lug_hole_diameter: float = 28.0,
        layer: str = None,
        color: int = None,
    ) -> Any:
        """绘制低矮圆形人孔总成。

        几何表达采用 `短颈 -> 法兰圈 -> 盲盖 -> 圆滑 U 形把手/吊耳` 的组合方式。
        当提供 `specification="HG/T 21517"` 且带 `dn/pn` 时，优先按首版人孔
        标准族参数表解析出整套人孔参数；当前先支持 `DN450 / DN500 / DN600`。
        当提供 `specification="GB/T 9119"` 以及 `dn/pn` 时，则仅将法兰圈与盖板的
        外径、孔圈、孔径、孔数、厚度按国标法兰表驱动；其余短颈、圆滑把手、吊耳
        仍保持参数化。

        `center` 语义为人孔开口中心，同时也是短颈底面圆心；整体沿 +Z 方向生成。
        """

        if not self.is_running():
            return None

        spec_key = self._normalize_standard_code(specification)
        manhole_standard_params = self._get_manhole_standard_params(specification, dn, pn)
        if manhole_standard_params is not None:
            opening_diameter = float(manhole_standard_params["opening_diameter"])
            neck_outer_diameter = float(manhole_standard_params["neck_outer_diameter"])
            neck_wall_thickness = float(manhole_standard_params["neck_wall_thickness"])
            neck_height = float(manhole_standard_params["neck_height"])
            flange_outer_diameter = float(manhole_standard_params["flange_outer_diameter"])
            bolt_circle_diameter = float(manhole_standard_params["bolt_circle_diameter"])
            bolt_hole_diameter = float(manhole_standard_params["bolt_hole_diameter"])
            bolt_count = int(manhole_standard_params["bolt_count"])
            flange_thickness = float(manhole_standard_params["flange_thickness"])
            cover_outer_diameter = float(manhole_standard_params["cover_outer_diameter"])
            cover_thickness = float(manhole_standard_params["cover_thickness"])
            cover_gap = float(manhole_standard_params["cover_gap"])
            handle_count = int(manhole_standard_params["handle_count"])
            handle_width = float(manhole_standard_params["handle_width"])
            handle_height = float(manhole_standard_params["handle_height"])
            handle_bar_diameter = float(manhole_standard_params["handle_bar_diameter"])
            add_lifting_lug = bool(manhole_standard_params["add_lifting_lug"])
        elif spec_key in ("GB/T9119", "GBT9119"):
            if dn is None or pn is None:
                logger.error("draw_manhole: 使用 GB/T 9119 时必须指定 dn 和 pn")
                return None
            key = (int(dn), float(pn))
            if key not in self.FLANGE_GB9119:
                logger.error(f"draw_manhole: 未找到 GB/T 9119 规格 DN{dn} PN{pn}")
                return None
            row = self.FLANGE_GB9119[key]
            flange_outer_diameter = float(row["D"])
            opening_diameter = float(opening_diameter or row["d"])
            bolt_circle_diameter = float(row["K"])
            bolt_hole_diameter = float(row["L"])
            bolt_count = int(row["n"])
            flange_thickness = float(row["C"])
        elif spec_key in ("HG/T21517", "HGT21517", "HG21517"):
            logger.error("draw_manhole: HG/T 21517 首版仅支持 DN450 / DN500 / DN600，且 PN 仅支持 0.6 / 1.0 / 1.6")
            return None

        if opening_diameter is None or float(opening_diameter) <= 0:
            logger.error("draw_manhole: opening_diameter 必须大于 0")
            return None
        if neck_outer_diameter is None:
            neck_outer_diameter = float(opening_diameter) + float(neck_wall_thickness) * 2.0
        if (
            flange_outer_diameter is None
            or bolt_circle_diameter is None
            or bolt_hole_diameter is None
            or bolt_count is None
            or flange_thickness is None
        ):
            logger.error("draw_manhole: 缺少法兰关键尺寸，请指定 dn/pn 或完整自定义法兰参数")
            return None
        if cover_outer_diameter is None:
            cover_outer_diameter = float(flange_outer_diameter)

        cx, cy, cz = self._normalize_point3d(center)
        neck = None
        flange = None
        blind_cover = None
        accessory_entities: list[Any] = []
        try:
            neck = self._add_oriented_hollow_cylinder(
                (cx, cy, cz),
                outer_radius=float(neck_outer_diameter) / 2.0,
                height=float(neck_height),
                wall_thickness=float(neck_wall_thickness),
                axis_signed_str="z",
            )
            if neck is None:
                logger.error("draw_manhole: 创建短颈失败")
                return None
            if layer:
                self.create_layer(layer)
                neck.Layer = layer
            if color is not None:
                neck.Color = color

            flange_base = (cx, cy, cz + float(neck_height))
            flange = self.draw_flange(
                flange_base,
                outer_diameter=float(flange_outer_diameter),
                center_hole_diameter=float(opening_diameter),
                bolt_circle_diameter=float(bolt_circle_diameter),
                bolt_hole_diameter=float(bolt_hole_diameter),
                bolt_count=int(bolt_count),
                thickness=float(flange_thickness),
                layer=layer,
                color=color,
            )
            if flange is None:
                logger.error("draw_manhole: 创建法兰圈失败")
                return None

            cover_base = (
                cx,
                cy,
                cz + float(neck_height) + float(flange_thickness) + float(cover_gap),
            )
            blind_cover = self.draw_blind_flange(
                cover_base,
                outer_diameter=float(cover_outer_diameter),
                bolt_circle_diameter=float(bolt_circle_diameter),
                bolt_hole_diameter=float(bolt_hole_diameter),
                bolt_count=int(bolt_count),
                thickness=float(cover_thickness),
                layer=layer,
                color=color,
            )
            if blind_cover is None:
                logger.error("draw_manhole: 创建盲盖失败")
                return None

            cover_top_z = cover_base[2] + float(cover_thickness)
            handle_radius = max(float(handle_bar_diameter) / 2.0, 2.0)
            if int(handle_count) > 0 and float(handle_width) > float(handle_bar_diameter):
                corner_radius = min(
                    float(handle_width) * 0.22,
                    float(handle_height) * 0.45,
                    (float(handle_width) / 2.0) - handle_radius * 1.1,
                )
                corner_radius = max(corner_radius, handle_radius * 1.6)
                top_bar_length = max(float(handle_width) - 2.0 * corner_radius, handle_radius * 2.0)
                vertical_leg_height = max(float(handle_height) - corner_radius, handle_radius * 1.2)
                elbow_center_z = cover_top_z + vertical_leg_height
                top_bar_z = elbow_center_z + corner_radius

                if int(handle_count) == 1:
                    handle_y_offsets = [0.0]
                else:
                    handle_y_offsets = [
                        -float(cover_outer_diameter) * 0.18,
                        float(cover_outer_diameter) * 0.18,
                    ][: int(handle_count)]
                for handle_y in handle_y_offsets:
                    left_leg_x = cx - float(handle_width) / 2.0
                    right_leg_x = cx + float(handle_width) / 2.0
                    left_elbow_center_x = left_leg_x + corner_radius
                    right_elbow_center_x = right_leg_x - corner_radius

                    left_leg = self._add_oriented_cylinder(
                        (left_leg_x, cy + handle_y, cover_top_z),
                        handle_radius,
                        vertical_leg_height,
                        "z",
                    )
                    right_leg = self._add_oriented_cylinder(
                        (right_leg_x, cy + handle_y, cover_top_z),
                        handle_radius,
                        vertical_leg_height,
                        "z",
                    )
                    top_bar = self._add_oriented_cylinder(
                        (
                            left_elbow_center_x,
                            cy + handle_y,
                            top_bar_z,
                        ),
                        handle_radius,
                        top_bar_length,
                        "x",
                    )
                    left_elbow = self.draw_pipe_elbow(
                        center=(left_elbow_center_x, cy + handle_y, elbow_center_z),
                        pipe_radius=handle_radius,
                        bend_radius=corner_radius,
                        angle=90,
                        wall_thickness=0.0,
                        layer=layer,
                        color=color,
                    )
                    right_elbow = self.draw_pipe_elbow(
                        center=(right_elbow_center_x, cy + handle_y, elbow_center_z),
                        pipe_radius=handle_radius,
                        bend_radius=corner_radius,
                        angle=90,
                        wall_thickness=0.0,
                        layer=layer,
                        color=color,
                    )

                    if left_elbow is not None:
                        pt_center = win32com.client.VARIANT(
                            pythoncom.VT_ARRAY | pythoncom.VT_R8,
                            [left_elbow_center_x, cy + handle_y, elbow_center_z],
                        )
                        pt_x = win32com.client.VARIANT(
                            pythoncom.VT_ARRAY | pythoncom.VT_R8,
                            [left_elbow_center_x + 1.0, cy + handle_y, elbow_center_z],
                        )
                        pt_z = win32com.client.VARIANT(
                            pythoncom.VT_ARRAY | pythoncom.VT_R8,
                            [left_elbow_center_x, cy + handle_y, elbow_center_z + 1.0],
                        )
                        self._call_with_retry(left_elbow.Rotate3D, pt_center, pt_x, math.pi / 2.0)
                        self._call_with_retry(left_elbow.Rotate3D, pt_center, pt_z, math.pi)

                    if right_elbow is not None:
                        pt_center = win32com.client.VARIANT(
                            pythoncom.VT_ARRAY | pythoncom.VT_R8,
                            [right_elbow_center_x, cy + handle_y, elbow_center_z],
                        )
                        pt_x = win32com.client.VARIANT(
                            pythoncom.VT_ARRAY | pythoncom.VT_R8,
                            [right_elbow_center_x + 1.0, cy + handle_y, elbow_center_z],
                        )
                        self._call_with_retry(right_elbow.Rotate3D, pt_center, pt_x, math.pi / 2.0)

                    for part in (left_leg, right_leg, top_bar, left_elbow, right_elbow):
                        if part is None:
                            continue
                        if layer:
                            part.Layer = layer
                        if color is not None:
                            part.Color = color
                        accessory_entities.append(part)

            if add_lifting_lug:
                lug_center = (
                    cx,
                    cy - float(cover_outer_diameter) * 0.10,
                    cover_top_z + float(lug_height) / 2.0,
                )
                lug = self.draw_box(
                    lug_center,
                    length=float(lug_width),
                    width=float(lug_thickness),
                    height=float(lug_height),
                    layer=layer,
                    color=color,
                )
                if lug is not None:
                    hole = self._add_oriented_cylinder(
                        (
                            lug_center[0],
                            lug_center[1] - float(lug_thickness) / 2.0 - 1.0,
                            cover_top_z + float(lug_height) * 0.62,
                        ),
                        radius=float(lug_hole_diameter) / 2.0,
                        height=float(lug_thickness) + 2.0,
                        axis_signed_str="y",
                    )
                    if hole is not None:
                        self._solid_boolean_subtract(lug, hole)
                        self._safe_delete_entity(hole)
                    accessory_entities.append(lug)

            self.refresh_view()
            logger.debug(
                "已绘制人孔总成: center=%s opening=%.3f neck_od=%.3f flange_od=%.3f",
                (cx, cy, cz),
                float(opening_diameter),
                float(neck_outer_diameter),
                float(flange_outer_diameter),
            )
            return blind_cover
        except Exception as exc:
            logger.error(f"绘制人孔总成时出错: {str(exc)}")
            self._safe_delete_entity(neck)
            self._safe_delete_entity(flange)
            self._safe_delete_entity(blind_cover)
            for entity in accessory_entities:
                self._safe_delete_entity(entity)
            return None

    def draw_bolt(
        self,
        base_center: Tuple[float, float, float],
        shank_diameter: Optional[float] = None,
        length: Optional[float] = None,
        axis: str = "z",
        head_width: Optional[float] = None,
        head_height: Optional[float] = None,
        specification: Optional[str] = None,
        thread_size: Optional[Union[str, int, float]] = None,
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制六角头螺栓/螺丝（3D 实体，简化表达，不生成真实螺纹）。

        支持两种模式：
        1. 国标查表：指定 specification="GB/T 5782" 或 "GB/T 5783" 以及 thread_size（如 "M16"）
        2. 自定义：直接指定 shank_diameter、head_width、head_height

        Args:
            base_center: 杆部起始端面圆心（即杆部与头部贴合面中心）
            shank_diameter: 杆径（mm），自定义时使用
            length: 杆部长度（mm，不含头部高度）
            axis: 杆部轴向，支持 x/y/z 或 -x/-y/-z，默认 z
            head_width: 六角头对边宽（mm），自定义时可指定；国标模式默认查表
            head_height: 六角头高度（mm），自定义时可指定；国标模式默认查表
            specification: 国标代号，如 "GB/T 5782"、"GB/T 5783"
            thread_size: 螺纹规格，如 "M16"
            layer: 图层名称（可选）
            color: 颜色索引（可选）

        Returns:
            创建的 3DSolid 螺栓实体，失败返回 None
        """
        if not self.is_running():
            return None
        try:
            base = self._normalize_point3d(base_center)
            standard_params = None
            if specification:
                standard_params = self._get_bolt_standard_params(specification, thread_size)
                if standard_params is None:
                    logger.error(f"draw_bolt: 未找到国标 {specification} 规格 {thread_size}")
                    return None
                shank_diameter = float(standard_params["d"])
                if head_width is None:
                    head_width = float(standard_params["s"])
                if head_height is None:
                    head_height = float(standard_params["k"])

            if shank_diameter is None or length is None:
                logger.error("draw_bolt: 缺少必要尺寸（国标须指定 specification/thread_size/length，自定义须指定 shank_diameter/length）")
                return None

            shaft_diameter = float(shank_diameter)
            shaft_length = float(length)
            if shaft_diameter <= 0 or shaft_length <= 0:
                logger.error("draw_bolt: 杆径和长度必须大于 0")
                return None

            head_width = float(head_width) if head_width is not None else shaft_diameter * 1.6
            head_height = float(head_height) if head_height is not None else max(shaft_diameter * 0.7, 2.0)
            if head_width <= shaft_diameter or head_height <= 0:
                logger.error("draw_bolt: 六角头对边宽需大于杆径，头高需大于 0")
                return None

            bolt = self._add_oriented_cylinder(base, shaft_diameter / 2.0, shaft_length, axis)
            if bolt is None:
                logger.error("draw_bolt: 杆部创建失败")
                return None

            head = self._add_hex_prism(base, head_width, head_height, self._flip_axis(axis))
            if head is None:
                self._safe_delete_entity(bolt)
                logger.error("draw_bolt: 六角头创建失败")
                return None

            if self._solid_boolean_union(bolt, head) is None:
                self._safe_delete_entity(bolt)
                self._safe_delete_entity(head)
                logger.error("draw_bolt: 螺栓头与杆部并集失败")
                return None
            self._safe_delete_entity(head)

            if layer:
                self.create_layer(layer)
                bolt.Layer = layer
            if color is not None:
                try:
                    bolt.Color = color
                except Exception as color_ex:
                    logger.warning(f"draw_bolt: 设置颜色失败，已保留实体默认颜色: {color_ex}")
            self.refresh_view()
            if standard_params is not None:
                logger.debug(
                    f"已按国标绘制螺栓: 标准={specification}, 规格={standard_params['thread_size']}, "
                    f"base={base_center}, d={shank_diameter}, s={head_width}, k={head_height}, L={length}, axis={axis}"
                )
            else:
                logger.debug(f"已绘制螺栓: base={base_center}, d={shank_diameter}, L={length}, axis={axis}")
            return bolt
        except Exception as e:
            logger.error(f"绘制螺栓时出错: {str(e)}")
            return None

    def draw_hex_nut(
        self,
        center: Tuple[float, float, float],
        inner_diameter: Optional[float] = None,
        thickness: Optional[float] = None,
        width_across_flats: Optional[float] = None,
        axis: str = "z",
        specification: Optional[str] = None,
        thread_size: Optional[Union[str, int, float]] = None,
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制六角螺母（3D 实体，简化表达，不生成真实螺纹）。

        支持两种模式：
        1. 国标查表：指定 specification="GB/T 6170.1"（或 GB/T 6170）和 thread_size（如 "M16"）
        2. 自定义：直接指定 inner_diameter、thickness、width_across_flats

        Args:
            center: 螺母中心点（实体几何中心）
            inner_diameter: 通孔直径（mm），自定义时使用；国标模式缺省取螺纹公称直径做简化表达
            thickness: 厚度（mm），自定义时可指定；国标模式默认查表
            width_across_flats: 六角对边宽（mm），自定义时可指定；国标模式默认查表
            axis: 螺母轴向，支持 x/y/z 或 -x/-y/-z，默认 z
            specification: 国标代号，如 "GB/T 6170.1"
            thread_size: 螺纹规格，如 "M16"
            layer: 图层名称（可选）
            color: 颜色索引（可选）

        Returns:
            创建的 3DSolid 螺母实体，失败返回 None
        """
        if not self.is_running():
            return None
        try:
            cx, cy, cz = self._normalize_point3d(center)
            standard_params = None
            if specification:
                standard_params = self._get_nut_standard_params(specification, thread_size)
                if standard_params is None:
                    logger.error(f"draw_hex_nut: 未找到国标 {specification} 规格 {thread_size}")
                    return None
                if inner_diameter is None:
                    inner_diameter = float(standard_params["d"])
                if width_across_flats is None:
                    width_across_flats = float(standard_params["s"])
                if thickness is None:
                    thickness = float(standard_params["m"])

            if inner_diameter is None:
                logger.error("draw_hex_nut: 缺少必要尺寸（国标须指定 specification/thread_size，自定义须指定 inner_diameter）")
                return None

            hole_diameter = float(inner_diameter)
            nut_thickness = float(thickness) if thickness is not None else max(hole_diameter * 0.8, 2.0)
            nut_width = float(width_across_flats) if width_across_flats is not None else hole_diameter * 1.6
            if hole_diameter <= 0 or nut_thickness <= 0 or nut_width <= hole_diameter:
                logger.error("draw_hex_nut: 通孔、厚度必须大于 0，且对边宽需大于通孔直径")
                return None

            axis_vec = self._axis_unit(axis)
            nut_base = (
                cx - axis_vec[0] * nut_thickness / 2.0,
                cy - axis_vec[1] * nut_thickness / 2.0,
                cz - axis_vec[2] * nut_thickness / 2.0,
            )
            nut = self._add_hex_prism(nut_base, nut_width, nut_thickness, axis)
            if nut is None:
                logger.error("draw_hex_nut: 螺母主体创建失败")
                return None

            # 孔刀体前后都超出实体端面，避免布尔减与端面共面时残留薄盖。
            hole_margin = max(1.0, nut_thickness * 0.2)
            hole_base = (
                cx - axis_vec[0] * (nut_thickness / 2.0 + hole_margin),
                cy - axis_vec[1] * (nut_thickness / 2.0 + hole_margin),
                cz - axis_vec[2] * (nut_thickness / 2.0 + hole_margin),
            )
            hole = self._add_oriented_cylinder(
                hole_base,
                hole_diameter / 2.0,
                nut_thickness + hole_margin * 2.0,
                axis,
            )
            if hole is not None:
                self._solid_boolean_subtract(nut, hole)
                self._safe_delete_entity(hole)

            if layer:
                self.create_layer(layer)
                nut.Layer = layer
            if color is not None:
                try:
                    nut.Color = color
                except Exception as color_ex:
                    logger.warning(f"draw_hex_nut: 设置颜色失败，已保留实体默认颜色: {color_ex}")
            self.refresh_view()
            if standard_params is not None:
                logger.debug(
                    f"已按国标绘制六角螺母: 标准={specification}, 规格={standard_params['thread_size']}, "
                    f"center={center}, d={inner_diameter}, t={nut_thickness}, s={nut_width}"
                )
            else:
                logger.debug(f"已绘制六角螺母: center={center}, d={inner_diameter}, t={nut_thickness}, s={nut_width}")
            return nut
        except Exception as e:
            logger.error(f"绘制六角螺母时出错: {str(e)}")
            return None

    def draw_washer(
        self,
        center: Tuple[float, float, float],
        inner_diameter: Optional[float] = None,
        outer_diameter: Optional[float] = None,
        thickness: Optional[float] = None,
        axis: str = "z",
        specification: Optional[str] = None,
        thread_size: Optional[Union[str, int, float]] = None,
        layer: str = None,
        color: int = None,
    ) -> Any:
        """
        绘制垫圈（3D 实体）。

        支持两种模式：
        1. 国标查表：指定 specification="GB/T 97.1" 和 thread_size（如 "M16"）
        2. 自定义：直接指定 inner_diameter、outer_diameter、thickness

        Args:
            center: 垫圈中心点（实体几何中心）
            inner_diameter: 内孔直径（mm），自定义时使用；国标模式默认查表
            outer_diameter: 外径（mm），自定义时可指定；国标模式默认查表
            thickness: 厚度（mm），自定义时可指定；国标模式默认查表
            axis: 垫圈轴向，支持 x/y/z 或 -x/-y/-z，默认 z
            specification: 国标代号，如 "GB/T 97.1"
            thread_size: 螺纹规格，如 "M16"
            layer: 图层名称（可选）
            color: 颜色索引（可选）

        Returns:
            创建的 3DSolid 垫圈实体，失败返回 None
        """
        if not self.is_running():
            return None
        try:
            cx, cy, cz = self._normalize_point3d(center)
            standard_params = None
            if specification:
                standard_params = self._get_washer_standard_params(specification, thread_size)
                if standard_params is None:
                    logger.error(f"draw_washer: 未找到国标 {specification} 规格 {thread_size}")
                    return None
                if inner_diameter is None:
                    inner_diameter = float(standard_params["d1"])
                if outer_diameter is None:
                    outer_diameter = float(standard_params["d2"])
                if thickness is None:
                    thickness = float(standard_params["h"])

            if inner_diameter is None:
                logger.error("draw_washer: 缺少必要尺寸（国标须指定 specification/thread_size，自定义须指定 inner_diameter）")
                return None

            hole_diameter = float(inner_diameter)
            ring_outer_diameter = float(outer_diameter) if outer_diameter is not None else hole_diameter * 2.0
            ring_thickness = float(thickness) if thickness is not None else max(hole_diameter * 0.15, 1.0)
            if hole_diameter <= 0 or ring_outer_diameter <= hole_diameter or ring_thickness <= 0:
                logger.error("draw_washer: 内孔、厚度必须大于 0，且外径需大于内孔直径")
                return None

            axis_vec = self._axis_unit(axis)
            body_base = (
                cx - axis_vec[0] * ring_thickness / 2.0,
                cy - axis_vec[1] * ring_thickness / 2.0,
                cz - axis_vec[2] * ring_thickness / 2.0,
            )
            washer = self._add_oriented_cylinder(body_base, ring_outer_diameter / 2.0, ring_thickness, axis)
            if washer is None:
                logger.error("draw_washer: 垫圈主体创建失败")
                return None

            # 与螺母同理，孔刀体增加穿透余量，确保形成真正贯通孔。
            hole_margin = max(1.0, ring_thickness * 0.5)
            hole_base = (
                cx - axis_vec[0] * (ring_thickness / 2.0 + hole_margin),
                cy - axis_vec[1] * (ring_thickness / 2.0 + hole_margin),
                cz - axis_vec[2] * (ring_thickness / 2.0 + hole_margin),
            )
            hole = self._add_oriented_cylinder(
                hole_base,
                hole_diameter / 2.0,
                ring_thickness + hole_margin * 2.0,
                axis,
            )
            if hole is not None:
                self._solid_boolean_subtract(washer, hole)
                self._safe_delete_entity(hole)

            if layer:
                self.create_layer(layer)
                washer.Layer = layer
            if color is not None:
                try:
                    washer.Color = color
                except Exception as color_ex:
                    logger.warning(f"draw_washer: 设置颜色失败，已保留实体默认颜色: {color_ex}")
            self.refresh_view()
            if standard_params is not None:
                logger.debug(
                    f"已按国标绘制垫圈: 标准={specification}, 规格={standard_params['thread_size']}, "
                    f"center={center}, di={inner_diameter}, do={ring_outer_diameter}, t={ring_thickness}"
                )
            else:
                logger.debug(f"已绘制垫圈: center={center}, di={inner_diameter}, do={ring_outer_diameter}, t={ring_thickness}")
            return washer
        except Exception as e:
            logger.error(f"绘制垫圈时出错: {str(e)}")
            return None

    def draw_spline(self, points: List[Tuple[float, float, float]], layer: str = None,
                    color: int = None, lineweight=None) -> Any:
        """
        绘制样条曲线
        
        Args:
            points: 控制点坐标列表 [(x, y, z), ...]
            layer: 图层名称（可选）
            color: 颜色索引（可选）
            lineweight: 线宽（可选）
            
        Returns:
            创建的 Spline 对象，失败返回 None
        """
        if not self.is_running():
            return None
        try:
            if not points or len(points) < 2:
                logger.error("样条曲线至少需要2个控制点")
                return None
            flat = []
            for p in points:
                pt = list(p) if len(p) >= 3 else [p[0], p[1], 0]
                flat.extend(pt)
            points_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, flat)
            # AddSpline(FitPoints, StartTangent, EndTangent) - 使用空数组表示无切线约束
            start_tangent = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0, 0, 0])
            end_tangent = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0, 0, 0])
            spline = self.doc.ModelSpace.AddSpline(points_array, start_tangent, end_tangent)
            if layer:
                self.create_layer(layer)
                spline.Layer = layer
            if color is not None:
                spline.Color = color
            if lineweight is not None:
                spline.LineWeight = self.validate_lineweight(lineweight)
            self.refresh_view()
            logger.debug(f"已绘制样条曲线: 控制点数{len(points)}")
            return spline
        except Exception as e:
            logger.error(f"绘制样条曲线时出错: {str(e)}")
            return None

    def draw_mtext(self, position: Tuple[float, float, float], width: float, text: str,
                   layer: str = None, color: int = None) -> Any:
        """
        添加多行文字
        
        Args:
            position: 插入点坐标 (x, y, z)
            width: 文本框宽度
            text: 文字内容
            layer: 图层名称（可选）
            color: 颜色索引（可选）
            
        Returns:
            创建的 MText 对象，失败返回 None
        """
        if not self.is_running():
            return None
        try:
            if len(position) == 2:
                position = (position[0], position[1], 0)
            pos_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [position[0], position[1], position[2]])
            mtext = self.doc.ModelSpace.AddMText(pos_array, width, text)
            if layer:
                self.create_layer(layer)
                mtext.Layer = layer
            if color is not None:
                mtext.Color = color
            self.refresh_view()
            logger.debug(f"已添加多行文字: 位置{position}, 宽度{width}")
            return mtext
        except Exception as e:
            logger.error(f"添加多行文字时出错: {str(e)}")
            return None

    # AutoCAD AcBooleanType 常量（用于 3DSolid/Region 布尔运算）
    # 标准 AutoCAD COM: acUnion=0, acIntersection=1, acSubtraction=2
    # 浩辰/中望 CAD 可能使用: Union=1, Intersection=2, Subtraction=3（可在 config.json 中配置）
    @property
    def AC_BOOLEAN_UNION(self) -> int:
        return config.get("cad", {}).get("boolean_union", 1)

    @property
    def AC_BOOLEAN_INTERSECTION(self) -> int:
        return config.get("cad", {}).get("boolean_intersection", 2)

    @property
    def AC_BOOLEAN_SUBTRACTION(self) -> int:
        return config.get("cad", {}).get("boolean_subtraction", 3)

    def _solid_boolean_subtract(self, main_solid: Any, tool_solid: Any) -> bool:
        """对 3D 实体做布尔减：main_solid 保留，减去 tool_solid 与之相交部分。main_solid 会被原地修改。"""
        try:
            main_solid.Boolean(self.AC_BOOLEAN_SUBTRACTION, tool_solid)
            return True
        except Exception as e:
            logger.error(f"布尔减运算失败: {str(e)}")
            return False

    def _solid_boolean_union(self, solid_a: Any, solid_b: Any) -> Any:
        """对两个 3D 实体做布尔并集。solid_a 会被原地修改并返回，solid_b 可被删除。"""
        try:
            solid_a.Boolean(self.AC_BOOLEAN_UNION, solid_b)
            return solid_a
        except Exception as e:
            logger.error(f"布尔并集失败: {str(e)}")
            return None

    def _solid_boolean_intersect(self, solid_a: Any, solid_b: Any) -> Any:
        """对两个 3D 实体做布尔交集。solid_a 会被原地修改并返回交集结果，solid_b 可被删除。"""
        try:
            solid_a.Boolean(self.AC_BOOLEAN_INTERSECTION, solid_b)
            return solid_a
        except Exception as e:
            logger.error(f"布尔交集失败: {str(e)}")
            return None

    def _get_entity_by_handle(self, handle: str) -> Any:
        """通过 Handle 获取图纸中的实体对象"""
        if not self.is_running():
            logger.error("_get_entity_by_handle: CAD 未运行")
            return None
        try:
            # Handle 可能是字符串或数字，统一转为字符串
            handle_str = str(handle).strip() if handle is not None else ""
            if not handle_str:
                logger.error("_get_entity_by_handle: Handle 为空")
                return None
            obj = self.doc.HandleToObject(handle_str)
            if obj is None:
                logger.error(f"_get_entity_by_handle: HandleToObject 返回 None, handle={handle_str}")
            return obj
        except Exception as e:
            logger.error(f"通过 Handle 获取实体失败: handle={handle}, 错误: {str(e)}")
            return None

    def boolean_union(self, handle_a: str, handle_b: str) -> bool:
        """
        对两个 3D 实体做布尔并集。
        
        Args:
            handle_a: 实体 A 的 Handle（并集结果保留在 A 上）
            handle_b: 实体 B 的 Handle（运算后 B 会被删除）
            
        Returns:
            成功返回 True，失败返回 False
        """
        if not self.is_running():
            return False
        solid_a = self._get_entity_by_handle(handle_a)
        solid_b = self._get_entity_by_handle(handle_b)
        if solid_a is None or solid_b is None:
            return False
        try:
            self._solid_boolean_union(solid_a, solid_b)
            try:
                solid_b.Delete()
            except Exception:
                pass
            self.refresh_view()
            logger.debug(f"布尔并集成功: {handle_a} ∪ {handle_b}")
            return True
        except Exception as e:
            logger.error(f"布尔并集失败: {str(e)}")
            return False

    def boolean_intersect(self, handle_a: str, handle_b: str) -> bool:
        """
        对两个 3D 实体做布尔交集。
        
        Args:
            handle_a: 实体 A 的 Handle（交集结果保留在 A 上）
            handle_b: 实体 B 的 Handle（运算后 B 会被删除）
            
        Returns:
            成功返回 True，失败返回 False
        """
        if not self.is_running():
            return False
        solid_a = self._get_entity_by_handle(handle_a)
        solid_b = self._get_entity_by_handle(handle_b)
        if solid_a is None or solid_b is None:
            return False
        try:
            self._solid_boolean_intersect(solid_a, solid_b)
            try:
                solid_b.Delete()
            except Exception:
                pass
            self.refresh_view()
            logger.debug(f"布尔交集成功: {handle_a} ∩ {handle_b}")
            return True
        except Exception as e:
            logger.error(f"布尔交集失败: {str(e)}")
            return False

    def boolean_subtract(self, handle_main: str, handle_tool: str) -> bool:
        """
        对两个 3D 实体做布尔差集。从 main 中减去 tool 与之相交的部分。
        
        Args:
            handle_main: 主实体 Handle（保留，被减去部分）
            handle_tool: 减除实体 Handle（运算后会被删除）
            
        Returns:
            成功返回 True，失败返回 False
        """
        if not self.is_running():
            logger.error("boolean_subtract: CAD 未运行")
            return False
        main_solid = self._get_entity_by_handle(handle_main)
        tool_solid = self._get_entity_by_handle(handle_tool)
        if main_solid is None:
            logger.error(f"boolean_subtract: 无法获取主实体 handle_main={handle_main}")
            return False
        if tool_solid is None:
            logger.error(f"boolean_subtract: 无法获取减除实体 handle_tool={handle_tool}")
            return False
        try:
            self._solid_boolean_subtract(main_solid, tool_solid)
            try:
                tool_solid.Delete()
            except Exception:
                pass
            self.refresh_view()
            logger.debug(f"布尔差集成功: {handle_main} - {handle_tool}")
            return True
        except Exception as e:
            logger.error(f"布尔差集失败: {str(e)}")
            return False

    def rotate_entity(self, handle: str, base_point: Tuple[float, ...], angle: float,
                     axis_point2: Tuple[float, ...] = None,
                     axis_direction: Tuple[float, float, float] = None) -> bool:
        """
        旋转 2D 或 3D 实体。

        支持两种 3D 旋转方式：
        1. 沿某一条线旋转：提供 axis_point2，旋转轴为 base_point 到 axis_point2 的直线
        2. 沿某一点旋转：提供 axis_direction [x,y,z]，旋转轴为过 base_point 且沿该方向的直线；
           若不提供 axis_point2 和 axis_direction，则绕过 base_point 的 Z 轴旋转（2D，在 XY 平面内）

        Args:
            handle: 实体 Handle
            base_point: 旋转轴上的点 [x, y] 或 [x, y, z]
            angle: 旋转角度（度）
            axis_point2: 旋转轴第二点 [x, y, z]，与 axis_direction 二选一，定义“沿一条线”
            axis_direction: 旋转轴方向向量 [x, y, z]，与 axis_point2 二选一，定义“沿某一点+方向”

        Returns:
            成功返回 True，失败返回 False
        """
        if not self.is_running():
            return False
        obj = self._get_entity_by_handle(handle)
        if obj is None:
            return False
        try:
            angle_rad = math.radians(float(angle))
            bp = list(base_point) if base_point else [0, 0, 0]
            if len(bp) == 2:
                bp.append(0)
            pt1 = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, bp)

            if axis_point2 is not None and len(axis_point2) >= 3:
                pt2 = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(axis_point2)[:3])
                obj.Rotate3D(pt1, pt2, angle_rad)
            elif axis_direction is not None and len(axis_direction) >= 3:
                dx, dy, dz = axis_direction[0], axis_direction[1], axis_direction[2]
                pt2 = win32com.client.VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8,
                    [bp[0] + dx, bp[1] + dy, bp[2] + dz]
                )
                obj.Rotate3D(pt1, pt2, angle_rad)
            else:
                obj.Rotate(pt1, angle_rad)

            self.refresh_view()
            logger.debug(f"已旋转实体 handle={handle}, 角度={angle}°")
            return True
        except Exception as e:
            logger.error(f"旋转实体失败: handle={handle}, 错误: {str(e)}")
            return False

    def extrude_entity(self, handle: str, height: float = None, taper_angle: float = 0,
                      path_handle: str = None, layer: str = None, color: int = None) -> Optional[str]:
        """
        3D 拉伸（EXTRUDE）：将 2D 封闭图形沿指定方向拉伸为 3D 实体。
        支持两种模式：1) 沿 Z 轴指定高度拉伸；2) 沿路径曲线拉伸。

        Args:
            handle: 2D 实体 Handle（支持 Circle、Region、封闭 LWPolyline、Ellipse）
            height: 拉伸高度（沿 Z 轴正方向），与 path_handle 二选一
            taper_angle: 锥角（度），-90 到 90，默认 0
            path_handle: 路径曲线 Handle（Line、Arc、Spline、Polyline 等），与 height 二选一
            layer: 图层名称（可选）
            color: 颜色索引（可选）

        Returns:
            新创建的 3D 实体 Handle，失败返回 None
        """
        if not self.is_running():
            return None
        obj = self._get_entity_by_handle(handle)
        if obj is None:
            return None
        if height is None and path_handle is None:
            logger.error("extrude_entity: 需提供 height 或 path_handle")
            return None
        try:
            taper_rad = math.radians(float(taper_angle))
            obj_name = getattr(obj, "ObjectName", "") or ""
            region = None
            if "Region" in obj_name:
                region = obj
            else:
                curves = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [obj])
                regions = self.doc.ModelSpace.AddRegion(curves)
                if regions is None or (hasattr(regions, "__len__") and len(regions) == 0):
                    logger.error(f"extrude_entity: 无法从实体创建面域，可能非封闭图形 handle={handle}")
                    return None
                region = regions[0] if hasattr(regions, "__len__") else regions

            if path_handle:
                path_obj = self._get_entity_by_handle(path_handle)
                if path_obj is None:
                    logger.error(f"extrude_entity: 无法获取路径实体 handle={path_handle}")
                    return None
                extruded = self.doc.ModelSpace.AddExtrudedSolidAlongPath(region, path_obj)
            else:
                extruded = self.doc.ModelSpace.AddExtrudedSolid(region, float(height), taper_rad)

            if layer:
                self.create_layer(layer)
                extruded.Layer = layer
            if color is not None:
                extruded.Color = color
            # 拉伸完成后删除原先的 2D 图形（AddRegion/AddExtrudedSolid 可能已消耗，尝试删除）
            try:
                obj.Delete()
            except Exception:
                pass
            self.refresh_view()
            result_handle = str(extruded.Handle)
            mode = f"沿路径({path_handle})" if path_handle else f"高度={height}"
            logger.debug(f"已拉伸实体 handle={handle} -> 新实体 handle={result_handle}, {mode}, 已删除原2D图形")
            return result_handle
        except Exception as e:
            logger.error(f"拉伸实体失败: handle={handle}, 错误: {str(e)}")
            return None

    def revolve_entity(self, handle: str, axis_point: Tuple[float, ...], axis_direction: Tuple[float, ...],
                      angle: float = 360, layer: str = None, color: int = None) -> Optional[str]:
        """
        将 2D 封闭图形绕轴旋转生成 3D 实体（REVOLVE 命令）。
        默认旋转 360° 一周。

        Args:
            handle: 2D 实体 Handle（支持 Circle、Region、封闭 LWPolyline、Ellipse）
            axis_point: 旋转轴上一点 [x, y, z]
            axis_direction: 旋转轴方向向量 [x, y, z]，如 [1,0,0] 绕 X 轴、[0,1,0] 绕 Y 轴、[0,0,1] 绕 Z 轴
            angle: 旋转角度（度），默认 360 为一周
            layer: 图层名称（可选）
            color: 颜色索引（可选）

        Returns:
            新创建的 3D 实体 Handle，失败返回 None
        """
        if not self.is_running():
            return None
        obj = self._get_entity_by_handle(handle)
        if obj is None:
            return None
        try:
            angle_rad = math.radians(float(angle))
            ap = list(axis_point)[:3] if axis_point else [0, 0, 0]
            if len(ap) == 2:
                ap.append(0)
            ad = list(axis_direction)[:3] if axis_direction else [0, 0, 1]
            if len(ad) == 2:
                ad.append(1)
            obj_name = getattr(obj, "ObjectName", "") or ""
            region = None
            if "Region" in obj_name:
                region = obj
            else:
                curves = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [obj])
                regions = self.doc.ModelSpace.AddRegion(curves)
                if regions is None or (hasattr(regions, "__len__") and len(regions) == 0):
                    logger.error(f"revolve_entity: 无法从实体创建面域，可能非封闭图形 handle={handle}")
                    return None
                region = regions[0] if hasattr(regions, "__len__") else regions

            pt = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, ap)
            vec = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, ad)
            revolved = self.doc.ModelSpace.AddRevolvedSolid(region, pt, vec, angle_rad)

            if layer:
                self.create_layer(layer)
                revolved.Layer = layer
            if color is not None:
                revolved.Color = color
            try:
                obj.Delete()
            except Exception:
                pass
            self.refresh_view()
            result_handle = str(revolved.Handle)
            logger.debug(f"已旋转实体 handle={handle} -> 新实体 handle={result_handle}, 角度={angle}°")
            return result_handle
        except Exception as e:
            logger.error(f"旋转生成实体失败: handle={handle}, 错误: {str(e)}")
            return None

    def draw_hemisphere(self, center: Tuple[float, float, float], radius: float,
                        flat_normal: Tuple[float, float, float] = (0, 1, 0),
                        layer: str = None, color: int = None) -> Any:
        """在指定位置绘制半球：先绘制球，再绘制一个足够大的长方体表示半空间，用布尔差集（球减长方体）得到半球。
        flat_normal 为平面法向，保留法向一侧的半球。例如 flat_normal=(0,1,0) 表示平面为 y=center[1]，保留 y 正方向一侧。"""
        if not self.is_running():
            return None
        try:
            if len(center) == 2:
                center = (center[0], center[1], 0)
            cx, cy, cz = center
            nx, ny, nz = flat_normal

            # 步骤 1：绘制球体
            center_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [cx, cy, cz])
            sphere = self.doc.ModelSpace.AddSphere(center_array, radius)

            # 步骤 2：绘制一个足够大的长方体（视为无限大半空间），用于切掉球体在 -flat_normal 一侧的部分
            half_infinite = max(radius * 100, 10000.0)
            if abs(ny) >= 0.9:
                # 平面为 y=cy，保留 y 正侧则减去 y<cy 的盒子
                box_min = (cx - half_infinite, cy - half_infinite, cz - half_infinite)
                box_max = (cx + half_infinite, cy, cz + half_infinite)
            elif abs(nx) >= 0.9:
                box_min = (cx, cy - half_infinite, cz - half_infinite) if nx > 0 else (cx - half_infinite, cy - half_infinite, cz - half_infinite)
                box_max = (cx + half_infinite, cy + half_infinite, cz + half_infinite) if nx > 0 else (cx, cy + half_infinite, cz + half_infinite)
            else:
                box_min = (cx - half_infinite, cy - half_infinite, cz)
                box_max = (cx + half_infinite, cy + half_infinite, cz + half_infinite) if nz > 0 else (cx + half_infinite, cy + half_infinite, cz)
            box_cx = (box_min[0] + box_max[0]) / 2
            box_cy = (box_min[1] + box_max[1]) / 2
            box_cz = (box_min[2] + box_max[2]) / 2
            lx = abs(box_max[0] - box_min[0])
            ly = abs(box_max[1] - box_min[1])
            lz = abs(box_max[2] - box_min[2])
            box_center = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [box_cx, box_cy, box_cz])
            box = self.doc.ModelSpace.AddBox(box_center, lx, ly, lz)

            # 步骤 3：布尔差集（球减长方体），得到半球
            self._solid_boolean_subtract(sphere, box)
            try:
                box.Delete()
            except Exception:
                pass

            if layer:
                self.create_layer(layer)
                sphere.Layer = layer
            if color is not None:
                sphere.Color = color
            self.refresh_view()
            logger.debug(f"已绘制半球: 中心{center}, 半径{radius}, 平面法向{flat_normal}")
            return sphere
        except Exception as e:
            logger.error(f"绘制半球时出错: {str(e)}")
            return None

    def draw_cylinder_along_y(self, base_center: Tuple[float, float, float], radius: float, height_y: float,
                              layer: str = None, color: int = None) -> Any:
        """在 base_center 处创建底面圆（位于 xz 平面，即 y=const），沿 WCS 的 +Y 方向拉伸 height_y，得到圆柱体。"""
        if not self.is_running():
            return None
        saved_ucs = None
        try:
            if len(base_center) == 2:
                base_center = (base_center[0], base_center[1], 0)
            ox, oy, oz = base_center
            # UCS：原点 base_center，Z 轴为 WCS +Y，以便 AddExtrudedSolid 的“高度”沿 Y
            # 取 X=WCS X，Y=WCS -Z，则 Z = X×Y = (0,1,0)
            origin = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [ox, oy, oz])
            x_axis_pt = (ox + 1, oy, oz)
            y_axis_pt = (ox, oy, oz - 1)
            x_axis = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(x_axis_pt))
            y_axis = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(y_axis_pt))
            saved_ucs = self.doc.ActiveUCS
            try:
                new_ucs = self.doc.UserCoordinateSystems.Add(origin, x_axis, y_axis, "TempYExtrude")
                self.doc.ActiveUCS = new_ucs
            except Exception as ucs_ex:
                logger.warning(f"设置 UCS 失败，尝试不切换 UCS: {ucs_ex}")
                # 若本 CAD 不支持或接口不同，可退化为在 XY 平面画圆并沿 Z 拉伸，再旋转（此处从略）
                return None
            try:
                center_ucs = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0, 0, 0])
                circle = self.doc.ModelSpace.AddCircle(center_ucs, radius)
                curves = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [circle])
                regions = self.doc.ModelSpace.AddRegion(curves)
                if regions is None or (hasattr(regions, '__len__') and len(regions) == 0):
                    raise RuntimeError("AddRegion 未生成区域")
                region = regions[0] if hasattr(regions, '__len__') else regions
                extruded = self.doc.ModelSpace.AddExtrudedSolid(region, height_y, 0)
                if layer:
                    self.create_layer(layer)
                    extruded.Layer = layer
                if color is not None:
                    extruded.Color = color
                self.doc.ActiveUCS = saved_ucs
                self.refresh_view()
                logger.debug(f"已绘制沿 Y 圆柱: 底面中心{base_center}, 半径{radius}, 高度{height_y}")
                return extruded
            finally:
                try:
                    self.doc.ActiveUCS = saved_ucs
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"绘制沿 Y 圆柱时出错: {str(e)}")
            if saved_ucs is not None:
                try:
                    self.doc.ActiveUCS = saved_ucs
                except Exception:
                    pass
            return None

    def draw_hemisphere_extruded_y(self, center: Tuple[float, float, float], radius: float, extrude_height: float,
                                   layer: str = None, color: int = None) -> Any:
        """在 center 处绘制半径为 radius 的半球（平面为 y=center[1]，保留 y 正方向），
        再沿 +Y 方向拉伸 extrude_height（即与半球底面同心的圆柱体与半球做并集）。"""
        if not self.is_running():
            return None
        try:
            hemisphere = self.draw_hemisphere(center, radius, flat_normal=(0, 1, 0), layer=layer, color=color)
            if not hemisphere:
                return None
            cylinder = self.draw_cylinder_along_y(center, radius, extrude_height, layer=layer, color=color)
            if not cylinder:
                logger.warning("圆柱未创建，仅保留半球。")
                return hemisphere
            result = self._solid_boolean_union(hemisphere, cylinder)
            if result:
                try:
                    cylinder.Delete()
                except Exception:
                    pass
            self.refresh_view()
            logger.debug(f"已绘制半球并沿 Y 拉伸: 中心{center}, 半径{radius}, 拉伸{extrude_height}")
            return result
        except Exception as e:
            logger.error(f"绘制半球并拉伸时出错: {str(e)}")
            return None

    def draw_arc(self, center: Tuple[float, float, float], 
                radius: float, start_angle: float, end_angle: float, layer: str = None, color: int = None, lineweight=None) -> Any:
        """绘制圆弧"""
        if not self.is_running():
            return None
            
        try:
            # 确保点是三维的
            if len(center) == 2:
                center = (center[0], center[1], 0)
                
            # 将角度转换为弧度
            start_rad = math.radians(start_angle)
            end_rad = math.radians(end_angle)
            
            # 使用VARIANT包装坐标点数据
            center_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, 
                                               [center[0], center[1], center[2]])
            
            # 添加圆弧
            arc = self.doc.ModelSpace.AddArc(center_array, radius, start_rad, end_rad)
            
            # 如果指定了图层，设置图层
            if layer:
                # 确保图层存在
                self.create_layer(layer)
                # 设置实体的图层
                arc.Layer = layer
            
            # 如果指定了颜色，设置颜色
            if color is not None:
                arc.Color = color

            if lineweight is not None:
                arc.LineWeight = self.validate_lineweight(lineweight)
            
            # 刷新视图
            self.refresh_view()
            
            logger.debug(f"已绘制圆弧: 中心{center}, 半径{radius}, 起始角度{start_angle}, 结束角度{end_angle}, 图层{layer if layer else '默认'}, 颜色{color if color is not None else '默认'}")
            return arc
        except Exception as e:
            logger.error(f"绘制圆弧失败: {str(e)}")
            return None
 
    def draw_ellipse(self, center: Tuple[float, float, float], 
                    major_axis: float, minor_axis: float, rotation: float = 0, 
                    layer: str = None, color: int = None, lineweight=None) -> Any:
        """绘制椭圆"""
        if not self.is_running():
            return None
            
        try:
            # 确保点是三维的
            if len(center) == 2:
                center = (center[0], center[1], 0)
            
            if rotation is None:
                rotation = 0

            # 将旋转角度转换为弧度
            rotation_rad = math.radians(rotation)
            
            # 使用VARIANT包装坐标点数据
            center_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, 
                                               [center[0], center[1], center[2]])
            
            # 计算椭圆的主轴向量
            major_x = major_axis * math.cos(rotation_rad)
            major_y = major_axis * math.sin(rotation_rad)
            major_vector = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, 
                                               [major_x, major_y, 0])
            
            # 添加椭圆
            ellipse = self.doc.ModelSpace.AddEllipse(center_array, major_vector, minor_axis / major_axis)
            
            # 如果指定了图层，设置图层
            if layer:
                # 确保图层存在
                self.create_layer(layer)
                # 设置实体的图层
                ellipse.Layer = layer
            
            # 如果指定了颜色，设置颜色
            if color is not None:
                ellipse.Color = color

            if lineweight is not None:
                ellipse.LineWeight = self.validate_lineweight(lineweight)
            
            # 刷新视图
            self.refresh_view()
            
            logger.debug(f"已绘制椭圆: 中心{center}, 长轴{major_axis}, 短轴{minor_axis}, 旋转角度{rotation}, 图层{layer if layer else '默认'}, 颜色{color if color is not None else '默认'}")
            return ellipse
        except Exception as e:
            logger.error(f"绘制椭圆失败: {str(e)}")
            return None
    
    def draw_polyline(self, points: List[Tuple[float, float, float]], closed: bool = False, layer: str = None, color: int = None, lineweight=None) -> Any:
        """绘制多段线"""
        if not self.is_running():
            return None
            
        try:
            # 确保所有点都是三维的
            processed_points = []
            for point in points:
                if len(point) == 2:
                    processed_points.append((point[0], point[1], 0))
                else:
                    processed_points.append(point)
            
            # 创建点数组
            point_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, 
                                                [coord for point in processed_points for coord in point])
            
            # 添加多段线
            polyline = self.doc.ModelSpace.AddPolyline(point_array)
            
            # 如果需要闭合
            if closed and len(processed_points) > 2:
                polyline.Closed = True
            
            # 如果指定了图层，设置图层
            if layer:
                # 确保图层存在
                self.create_layer(layer)
                # 设置实体的图层
                polyline.Layer = layer
            
            # 如果指定了颜色，设置颜色
            if color is not None:
                polyline.Color = color

            if lineweight is not None:
                polyline.LineWeight = self.validate_lineweight(lineweight)
            
            # 刷新视图
            self.refresh_view()

            logger.debug(f"已绘制多段线: {len(points)}个点, {'闭合' if closed else '不闭合'}, 图层{layer if layer else '默认'}, 颜色{color if color is not None else '默认'}")
            return polyline
        except Exception as e:
            logger.error(f"绘制多段线时出错: {str(e)}")
            return None
    
    def draw_rectangle(self, corner1: Tuple[float, float, float], 
                      corner2: Tuple[float, float, float], layer: str = None, color: int = None, lineweight=None) -> Any:
        """绘制矩形"""
        if not self.is_running():
            return None
            
        try:
            # 确保点是三维的
            if len(corner1) == 2:
                corner1 = (corner1[0], corner1[1], 0)
            if len(corner2) == 2:
                corner2 = (corner2[0], corner2[1], 0)
                
            # 计算矩形的四个角点
            x1, y1, z1 = corner1
            x2, y2, z2 = corner2
            
            # 创建矩形的四个点
            points = [
                (x1, y1, z1),
                (x2, y1, z1),
                (x2, y2, z1),
                (x1, y2, z1),
                (x1, y1, z1)  # 闭合矩形
            ]
            
            # 使用多段线绘制矩形
            return self.draw_polyline(points, True, layer, color, lineweight)
        except Exception as e:
            logger.error(f"绘制矩形时出错: {str(e)}")
            return None
    
    def draw_text(self, position: Tuple[float, float, float], 
                 text: str, height: float = 2.5, rotation: float = 0, layer: str = None, color: int = None) -> Any:
        """添加文本"""
        if not self.is_running():
            return None
            
        try:
            # 确保点是三维的
            if len(position) == 2:
                position = (position[0], position[1], 0)
            
            # 使用VARIANT包装坐标点数据
            position_array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, 
                                                 [position[0], position[1], position[2]])
                
            # 添加文本
            text_obj = self.doc.ModelSpace.AddText(text, position_array, height)
            
            # 设置旋转角度
            if rotation != 0:
                text_obj.Rotation = math.radians(rotation)
            
            # 如果指定了图层，设置图层
            if layer:
                # 确保图层存在
                self.create_layer(layer)
                # 设置实体的图层
                text_obj.Layer = layer
            
            # 如果指定了颜色，设置颜色
            if color is not None:
                text_obj.Color = color
            
            # 刷新视图
            self.refresh_view()
                                
            logger.debug(f"已添加文本: '{text}', 位置{position}, 高度{height}, 旋转{rotation}度, 图层{layer if layer else '默认'}, 颜色{color if color is not None else '默认'}")
            return text_obj
        except Exception as e:
            logger.error(f"添加文本时出错: {str(e)}")
            return None
    
    def draw_hatch(self, points: List[Tuple[float, float, float]], 
                  pattern_name: str = "SOLID", scale: float = 1.0, layer: str = None, color: int = None) -> Any:
        """绘制填充图案
        
        Args:
            points: 填充边界的点集，每个点为二维或三维坐标元组
            pattern_name: 填充图案名称，默认为"SOLID"(实体填充)
            scale: 填充图案比例，默认为1.0
            layer: 图层名称，如果为None则使用当前图层
            color: 颜色索引，如果为None则使用默认颜色
            
        Returns:
            成功返回填充对象，失败返回None
        """
        if not self.is_running():
            return None
            
        try:
            # 确保所有点都是有效的
            if not points or len(points) < 3:
                logger.error("创建填充失败: 至少需要3个点来定义填充边界")
                return None
                
            # 创建闭合多段线作为边界
            closed_polyline = self.draw_polyline(points, closed=True, layer=layer)
            if not closed_polyline:
                logger.error("创建填充失败: 无法创建边界多段线")
                return None
                
            # 创建填充对象 (0表示正常填充，True表示关联边界)
            hatch = self.doc.ModelSpace.AddHatch(0, pattern_name, True)
                
            # 添加外部边界循环
            # 使用VARIANT包装对象数组
            object_ids = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [closed_polyline])
            hatch.AppendOuterLoop(object_ids)
                
            # 设置填充图案比例
            hatch.PatternScale = scale
                
            # 如果指定了图层，设置图层
            if layer:
                # 确保图层存在
                self.create_layer(layer)
                # 设置实体的图层
                hatch.Layer = layer
                
            # 如果指定了颜色，设置颜色
            if color is not None:
                hatch.Color = color
                                
            # 更新填充 (计算填充区域)
            hatch.Evaluate()
            
            # 刷新视图
            self.refresh_view()
                
            logger.debug(f"已创建填充: 图案 {pattern_name}, 比例 {scale}, 图层{layer if layer else '默认'}, 颜色{color if color is not None else '默认'}")
            return hatch
        except Exception as e:
            logger.error(f"创建填充时出错: {str(e)}")
            return None
    
    def zoom_extents(self) -> bool:
        """缩放视图以显示所有对象"""
        if not self.is_running():
            return False
            
        try:
            self._call_with_retry(self.app.ZoomExtents)
            logger.info("已缩放视图以显示所有对象")
            self._notify_view_changed("zoom_extents")
            return True
        except Exception as e:
            logger.error(f"缩放视图时出错: {str(e)}")
            return False

    def set_view_direction(
        self,
        direction: Tuple[float, float, float],
        target: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        center: Tuple[float, float] = (0.0, 0.0),
        height: float = 400.0,
        width: float = 600.0,
    ) -> bool:
        """直接通过 COM 设置视口方向，避免触发 VPOINT 弹窗。"""
        if not self.is_running():
            return False

        try:
            viewport = self._call_with_retry(lambda: self.doc.ActiveViewport)
            coord_variant = pythoncom.VT_ARRAY | pythoncom.VT_R8
            viewport.Direction = win32com.client.VARIANT(coord_variant, list(direction))
            viewport.Target = win32com.client.VARIANT(coord_variant, list(target))
            viewport.Center = win32com.client.VARIANT(coord_variant, list(center))
            viewport.Height = float(height)
            viewport.Width = float(width)
            self._call_with_retry(setattr, self.doc, "ActiveViewport", viewport)
            self._call_with_retry(self.doc.Regen, 1)
            self._notify_view_changed(
                "set_view_direction",
                {
                    "direction": list(direction),
                    "target": list(target),
                    "center": list(center),
                    "height": height,
                    "width": width,
                },
            )
            return True
        except Exception as e:
            logger.error(f"设置视口方向失败: {str(e)}")
            return False

    def set_named_view(
        self,
        view_name: str,
        target: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        center: Tuple[float, float] = (0.0, 0.0),
        height: float = 400.0,
        width: float = 600.0,
    ) -> bool:
        """设置常用命名视图，支持正投影视图与等轴测视图。"""
        directions = {
            "front": (0.0, -1.0, 0.0),
            "top": (0.0, 0.0, 1.0),
            "right": (1.0, 0.0, 0.0),
            "back": (0.0, 1.0, 0.0),
            "bottom": (0.0, 0.0, -1.0),
            "left": (-1.0, 0.0, 0.0),
            "swiso": (-1.0, -1.0, 1.0),
            "seiso": (1.0, -1.0, 1.0),
            "neiso": (1.0, 1.0, 1.0),
            "nwiso": (-1.0, 1.0, 1.0),
        }
        normalized_view_name = self._normalize_named_view_name(view_name)
        direction = directions.get(normalized_view_name)
        if direction is None:
            logger.error(f"不支持的命名视图: {view_name}")
            return False
        return self.set_view_direction(direction, target=target, center=center, height=height, width=width)

    def set_orthographic_view(
        self,
        view_name: str,
        target: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        center: Tuple[float, float] = (0.0, 0.0),
        height: float = 400.0,
        width: float = 600.0,
    ) -> bool:
        """设置常用正投影视图。

        三视图约定：
        - 前视图 = 主视图 = front
        - 俯视图 = top
        - 左视图 = left

        视图方向采用 CAD 常见标准：
        - front: 观察方向 -Y，投影面 XZ
        - top: 观察方向 +Z，投影面 XY
        - left: 观察方向 +X，投影面 YZ
        """
        directions = {
            "front": (0.0, -1.0, 0.0),
            "top": (0.0, 0.0, 1.0),
            "right": (1.0, 0.0, 0.0),
            "back": (0.0, 1.0, 0.0),
            "bottom": (0.0, 0.0, -1.0),
            "left": (-1.0, 0.0, 0.0),
        }
        normalized_view_name = self._normalize_orthographic_view_name(view_name)
        direction = directions.get(normalized_view_name)
        if direction is None:
            logger.error(f"不支持的正投影视图: {view_name}")
            return False
        return self.set_view_direction(direction, target=target, center=center, height=height, width=width)

    def _normalize_orthographic_view_name(self, view_name: str) -> Optional[str]:
        """将中英文视图别名规范化为标准正投影视图名称。"""
        raw_name = str(view_name or "").strip().lower()
        if not raw_name:
            return None
        normalized_name = raw_name.replace("-", " ").replace("_", " ")
        compact_name = normalized_name.replace(" ", "")
        return (
            self.ORTHOGRAPHIC_VIEW_ALIASES.get(normalized_name)
            or self.ORTHOGRAPHIC_VIEW_ALIASES.get(compact_name)
        )

    def _normalize_named_view_name(self, view_name: str) -> Optional[str]:
        """将命名视图别名规范化为标准视图名称。"""
        orthographic_name = self._normalize_orthographic_view_name(view_name)
        if orthographic_name:
            return orthographic_name

        raw_name = str(view_name or "").strip().lower()
        if not raw_name:
            return None
        normalized_name = raw_name.replace("-", " ").replace("_", " ")
        compact_name = normalized_name.replace(" ", "")
        isometric_aliases = {
            "swiso": "swiso",
            "southwest isometric": "swiso",
            "southwestisometric": "swiso",
            "西南等轴测": "swiso",
            "西南等轴": "swiso",
            "seiso": "seiso",
            "southeast isometric": "seiso",
            "southeastisometric": "seiso",
            "东南等轴测": "seiso",
            "东南等轴": "seiso",
            "neiso": "neiso",
            "northeast isometric": "neiso",
            "northeastisometric": "neiso",
            "东北等轴测": "neiso",
            "东北等轴": "neiso",
            "nwiso": "nwiso",
            "northwest isometric": "nwiso",
            "northwestisometric": "nwiso",
            "西北等轴测": "nwiso",
            "西北等轴": "nwiso",
            "isometric": "swiso",
            "等轴测": "swiso",
        }
        return (
            isometric_aliases.get(normalized_name)
            or isometric_aliases.get(compact_name)
        )
    
    def close(self) -> None:
        """关闭CAD控制器"""
        try:
            # 释放COM资源
            if self.app is not None:
                del self.app
            pythoncom.CoUninitialize()
        except:
            pass

    
    def create_layer(self, layer_name: str) -> bool:    # , color: Union[int, Tuple[int, int, int]] = 7
        """创建新图层
        
        Args:
            layer_name: 图层名称
            color: 颜色值，可以是CAD颜色索引(int)或RGB颜色值(tuple)
            
        Returns:
            操作是否成功
        """
        if not self.is_running():
            return False
        
        try:
            layers = self._call_with_retry(lambda: self.doc.Layers)
            # 检查图层是否已存在
            layer_count = int(self._call_with_retry(lambda: layers.Count))
            for i in range(layer_count):
                existing_layer = self._call_with_retry(layers.Item, i)
                if existing_layer.Name == layer_name:
                    # 图层已存在，激活它
                    self._call_with_retry(lambda: setattr(self.doc, "ActiveLayer", existing_layer))
                    return True
                
            # 创建新图层
            new_layer = self._call_with_retry(layers.Add, layer_name)
            
            # 图层不设置颜色，设置里面的实体颜色
            # # 设置颜色
            # if isinstance(color, int):
            #     # 使用颜色索引
            #     new_layer.Color = color
            # elif isinstance(color, tuple) and len(color) == 3:
            #     # 使用RGB值
            #     r, g, b = color
            #     # 设置TrueColor
            #     new_layer.TrueColor = self._create_true_color(r, g, b)
            
            # 设置为当前图层
            self._call_with_retry(lambda: setattr(self.doc, "ActiveLayer", new_layer))
            logger.info(f"已创建新图层: {layer_name}")  #, 颜色: {color}
            return True
        except Exception as e:
            logger.error(f"创建图层时出错: {str(e)}")
            return False

    def _normalize_3d_point(
        self,
        point: Optional[Union[Tuple[float, float, float], List[float]]],
    ) -> Optional[Tuple[float, float, float]]:
        """将输入点统一规范为三维坐标。"""
        if point is None:
            return None
        if len(point) == 2:
            return (float(point[0]), float(point[1]), 0.0)
        if len(point) >= 3:
            return (float(point[0]), float(point[1]), float(point[2]))
        raise ValueError(f"无效坐标点: {point}")

    def _point_to_variant(self, point: Tuple[float, float, float]) -> Any:
        """将三维点转换为 AutoCAD COM 所需的 VARIANT 数组。"""
        return win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8,
            [point[0], point[1], point[2]],
        )

    def _project_point_to_view_plane(
        self,
        point: Optional[Union[Tuple[float, float, float], List[float]]],
        source_view: Optional[str] = None,
    ) -> Optional[Tuple[float, float, float]]:
        """将视图局部坐标投影到对应的 WCS 正投影平面。"""
        normalized_point = self._normalize_3d_point(point)
        if normalized_point is None:
            return None

        normalized_view = self._normalize_orthographic_view_name(source_view) if source_view else None
        if normalized_view is None:
            return normalized_point

        local_u, local_v, local_depth = normalized_point
        if normalized_view == "front":
            return (local_u, local_depth, local_v)
        if normalized_view == "top":
            return (local_u, local_v, local_depth)
        if normalized_view == "left":
            return (local_depth, local_u, local_v)
        if normalized_view == "right":
            return (-local_depth, local_u, local_v)
        if normalized_view == "back":
            return (local_u, -local_depth, local_v)
        if normalized_view == "bottom":
            return (local_u, local_v, -local_depth)
        return normalized_point

    def _make_local_dimension_point(
        self,
        point: Optional[Tuple[float, float, float]],
        normalized_view: Optional[str],
    ) -> Optional[Tuple[float, float, float]]:
        """把视图局部坐标转换为创建尺寸时使用的局部二维坐标。"""
        if point is None:
            return None
        if normalized_view == "front":
            return (point[0], point[1], 0.0)
        return point

    def _resolve_view_depth(
        self,
        normalized_view: Optional[str],
        *points: Optional[Tuple[float, float, float]],
    ) -> float:
        """提取视图局部坐标中的统一深度值。"""
        if normalized_view != "front":
            return 0.0
        depths = [float(point[2]) for point in points if point is not None]
        if not depths:
            return 0.0
        return sum(depths) / len(depths)

    def _move_entity_by_vector(self, entity: Any, vector: Tuple[float, float, float]) -> None:
        """按给定位移向量移动实体。"""
        if entity is None:
            return
        from_point = self._point_to_variant((0.0, 0.0, 0.0))
        to_point = self._point_to_variant(vector)
        entity.Move(from_point, to_point)

    def _orient_dimension_entity_for_view(
        self,
        dimension: Any,
        normalized_view: Optional[str],
        depth_offset: float = 0.0,
    ) -> None:
        """将局部二维尺寸对象整体旋转/平移到目标正投影视图平面。"""
        if dimension is None or not normalized_view:
            return

        if normalized_view == "front":
            axis_start = self._point_to_variant((0.0, 0.0, 0.0))
            axis_end = self._point_to_variant((1.0, 0.0, 0.0))
            dimension.Rotate3D(axis_start, axis_end, math.pi / 2.0)
            if abs(depth_offset) > 1e-6:
                self._move_entity_by_vector(dimension, (0.0, depth_offset, 0.0))

    def _resolve_dimension_text_position_in_view(
        self,
        start_point: Optional[Union[Tuple[float, float, float], List[float]]] = None,
        end_point: Optional[Union[Tuple[float, float, float], List[float]]] = None,
        text_position: Optional[Union[Tuple[float, float, float], List[float]]] = None,
        source_view: Optional[str] = None,
    ) -> Optional[Tuple[float, float, float]]:
        """先在视图局部坐标中求默认文字位置，再投影到目标视图平面。"""
        normalized_text_position = self._normalize_3d_point(text_position)
        if normalized_text_position is not None:
            return self._project_point_to_view_plane(normalized_text_position, source_view)

        normalized_start = self._normalize_3d_point(start_point)
        normalized_end = self._normalize_3d_point(end_point)
        if normalized_start is None or normalized_end is None:
            return None

        local_text_position = (
            (normalized_start[0] + normalized_end[0]) / 2.0,
            (normalized_start[1] + normalized_end[1]) / 2.0 + 5.0,
            (normalized_start[2] + normalized_end[2]) / 2.0,
        )
        return self._project_point_to_view_plane(local_text_position, source_view)

    def _resolve_dimension_text_position(
        self,
        start_point: Optional[Tuple[float, float, float]] = None,
        end_point: Optional[Tuple[float, float, float]] = None,
        text_position: Optional[Union[Tuple[float, float, float], List[float]]] = None,
        center: Optional[Tuple[float, float, float]] = None,
        chord_point: Optional[Tuple[float, float, float]] = None,
        leader_length: Optional[float] = None,
    ) -> Optional[Tuple[float, float, float]]:
        """为尺寸标注生成默认文字位置。"""
        normalized_text_position = self._normalize_3d_point(text_position)
        if normalized_text_position is not None:
            return normalized_text_position

        if start_point is not None and end_point is not None:
            mid_x = (start_point[0] + end_point[0]) / 2.0
            mid_y = (start_point[1] + end_point[1]) / 2.0
            mid_z = (start_point[2] + end_point[2]) / 2.0
            return (mid_x, mid_y + 5.0, mid_z)

        if center is not None and chord_point is not None:
            effective_leader = float(leader_length if leader_length is not None else 5.0)
            direction_x = chord_point[0] - center[0]
            direction_y = chord_point[1] - center[1]
            direction_z = chord_point[2] - center[2]
            magnitude = math.sqrt(direction_x ** 2 + direction_y ** 2 + direction_z ** 2)
            if magnitude <= 1e-6:
                return (chord_point[0], chord_point[1] + effective_leader, chord_point[2])
            scale = effective_leader / magnitude
            return (
                chord_point[0] + direction_x * scale,
                chord_point[1] + direction_y * scale,
                chord_point[2] + direction_z * scale,
            )

        return None

    def _apply_dimension_style(
        self,
        dimension: Any,
        textheight: Optional[float],
        layer: Optional[str],
        color: Optional[int],
    ) -> None:
        """统一应用尺寸实体的通用样式。"""
        if textheight is not None:
            dimension.TextHeight = textheight

        if layer:
            self.create_layer(layer)
            dimension.Layer = layer

        if color is not None:
            dimension.Color = color

    def add_dimension(
        self,
        start_point: Optional[Union[Tuple[float, float, float], List[float]]] = None,
        end_point: Optional[Union[Tuple[float, float, float], List[float]]] = None,
        text_position: Optional[Union[Tuple[float, float, float], List[float]]] = None,
        textheight: float = 5,
        layer: str = None,
        color: int = None,
        dimension_type: str = "aligned",
        center: Optional[Union[Tuple[float, float, float], List[float]]] = None,
        chord_point: Optional[Union[Tuple[float, float, float], List[float]]] = None,
        far_chord_point: Optional[Union[Tuple[float, float, float], List[float]]] = None,
        leader_length: Optional[float] = None,
        source_view: Optional[str] = None,
    ) -> Any:
        """添加常用尺寸标注。"""
        if not self.is_running():
            return None

        try:
            normalized_type = (dimension_type or "aligned").lower()
            normalized_view = self._normalize_orthographic_view_name(source_view) if source_view else None
            raw_start_point = self._normalize_3d_point(start_point)
            raw_end_point = self._normalize_3d_point(end_point)
            raw_center = self._normalize_3d_point(center)
            raw_chord_point = self._normalize_3d_point(chord_point)
            raw_far_chord_point = self._normalize_3d_point(far_chord_point)
            raw_text_position = self._normalize_3d_point(text_position)

            use_entity_view_transform = normalized_view == "front"

            if use_entity_view_transform:
                start_point = self._make_local_dimension_point(raw_start_point, normalized_view)
                end_point = self._make_local_dimension_point(raw_end_point, normalized_view)
                center = self._make_local_dimension_point(raw_center, normalized_view)
                chord_point = self._make_local_dimension_point(raw_chord_point, normalized_view)
                far_chord_point = self._make_local_dimension_point(raw_far_chord_point, normalized_view)
                depth_offset = self._resolve_view_depth(
                    normalized_view,
                    raw_start_point,
                    raw_end_point,
                    raw_center,
                    raw_chord_point,
                    raw_far_chord_point,
                    raw_text_position,
                )
            else:
                depth_offset = 0.0
                start_point = self._project_point_to_view_plane(raw_start_point, source_view)
                end_point = self._project_point_to_view_plane(raw_end_point, source_view)
                center = self._project_point_to_view_plane(raw_center, source_view)
                chord_point = self._project_point_to_view_plane(raw_chord_point, source_view)
                far_chord_point = self._project_point_to_view_plane(raw_far_chord_point, source_view)

            if normalized_type in {"aligned", "horizontal", "vertical"}:
                if start_point is None or end_point is None:
                    raise ValueError("线性标注必须提供 start_point 和 end_point")
                if use_entity_view_transform:
                    text_position = self._make_local_dimension_point(raw_text_position, normalized_view)
                    if text_position is None:
                        text_position = self._resolve_dimension_text_position(
                            start_point=start_point,
                            end_point=end_point,
                            text_position=None,
                        )
                else:
                    text_position = self._resolve_dimension_text_position_in_view(
                        start_point=raw_start_point,
                        end_point=raw_end_point,
                        text_position=text_position,
                        source_view=source_view,
                    ) or self._resolve_dimension_text_position(
                        start_point=start_point,
                        end_point=end_point,
                        text_position=text_position,
                    )
                if text_position is None:
                    raise ValueError("线性标注缺少 text_position")

                start_array = self._point_to_variant(start_point)
                end_array = self._point_to_variant(end_point)
                text_pos_array = self._point_to_variant(text_position)

                if normalized_type == "aligned":
                    dimension = self.doc.ModelSpace.AddDimAligned(start_array, end_array, text_pos_array)
                else:
                    rotation = 0.0 if normalized_type == "horizontal" else (math.pi / 2.0)
                    dimension = self.doc.ModelSpace.AddDimRotated(
                        start_array,
                        end_array,
                        text_pos_array,
                        rotation,
                    )
            elif normalized_type == "diameter":
                chord_point = chord_point or start_point
                far_chord_point = far_chord_point or end_point
                if chord_point is None or far_chord_point is None:
                    raise ValueError("直径标注必须提供 chord_point/far_chord_point 或 start_point/end_point")
                effective_leader = float(leader_length if leader_length is not None else 5.0)
                dimension = self.doc.ModelSpace.AddDimDiametric(
                    self._point_to_variant(chord_point),
                    self._point_to_variant(far_chord_point),
                    effective_leader,
                )
            elif normalized_type == "radius":
                center = center or start_point
                chord_point = chord_point or end_point
                if center is None or chord_point is None:
                    raise ValueError("半径标注必须提供 center 和 chord_point，或复用 start_point/end_point")
                effective_leader = float(leader_length if leader_length is not None else 5.0)
                dimension = self.doc.ModelSpace.AddDimRadial(
                    self._point_to_variant(center),
                    self._point_to_variant(chord_point),
                    effective_leader,
                )
            else:
                raise ValueError(f"暂不支持的标注类型: {dimension_type}")

            if use_entity_view_transform:
                self._orient_dimension_entity_for_view(dimension, normalized_view, depth_offset=depth_offset)

            self._apply_dimension_style(dimension, textheight, layer, color)
            self.refresh_view()
            logger.info(
                "已添加%s标注，图层%s，目标视图%s",
                normalized_type,
                layer if layer else "默认",
                source_view if source_view else "当前WCS",
            )
            return dimension
        except Exception as e:
            logger.error(f"添加标注时出错: {str(e)}")
            return None

