"""可复用的界面控件：看图控件、信息卡、推荐面板、叠加/差异合成。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from ..core.models import REL_LABEL, ImageInfo, PairVerdict
from ..core.recommend import Recommendation
from ..core.scanner import human_size
from . import theme

MAX_DECODE_SIDE = 4500


def load_qimage(path: Path | str, max_side: int = MAX_DECODE_SIDE) -> QImage | None:
    """按 EXIF 方向摆正后读取；超大图按比例缩小，避免内存爆掉。"""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    if not reader.canRead():
        return None
    sz = reader.size()
    if sz.isValid() and max(sz.width(), sz.height()) > max_side:
        s = max_side / float(max(sz.width(), sz.height()))
        reader.setScaledSize(QSize(max(1, int(sz.width() * s)),
                                   max(1, int(sz.height() * s))))
    img = reader.read()
    return None if img.isNull() else img


def qimage_to_rgb(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    w, h = img.width(), img.height()
    ptr = img.bits()
    arr = np.frombuffer(ptr, np.uint8).reshape((h, img.bytesPerLine()))
    return arr[:, : w * 3].reshape(h, w, 3).copy()


def rgb_to_qimage(arr: np.ndarray) -> QImage:
    h, w, _ = arr.shape
    buf = np.ascontiguousarray(arr, dtype=np.uint8).tobytes()
    return QImage(buf, w, h, w * 3, QImage.Format.Format_RGB888).copy()


ALIGN_TOLERANCE = 0.02  # 宽高比相对差 ≤2% 视为同比例


def _align_pair(a: QImage, b: QImage) -> tuple[QImage, QRect]:
    """把 B 与 A 对齐，返回 (B 的对齐版本, B 在 A 坐标系里的放置矩形)。

    宽高比一致（±2%）：B 缩放到 A 的尺寸，覆盖全帧——同比例缩放，无变形。
    宽高比不同（裁剪版）：B 按 1:1 居中放置，不再拉伸到 A 的尺寸——
    旧版用 IgnoreAspectRatio 硬拉伸，裁剪版会全图错位、差异占比失真。
    同尺度裁剪（裁剪后未缩放，最常见）在此对齐是精确的；缩放过的裁剪版
    无法无特征对齐，差异只统计重叠区域，未重叠处调暗。
    """
    iw, ih = a.width(), a.height()
    bw, bh = b.width(), b.height()
    if iw <= 0 or ih <= 0 or bw <= 0 or bh <= 0:
        scaled = b.scaled(a.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
        return scaled, QRect(0, 0, max(iw, 1), max(ih, 1))
    ar_a, ar_b = iw / ih, bw / bh
    if abs(ar_a - ar_b) <= ALIGN_TOLERANCE * max(ar_a, ar_b):
        scaled = b.scaled(a.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
        return scaled, QRect(0, 0, iw, ih)
    x = (iw - bw) // 2
    y = (ih - bh) // 2
    return b, QRect(x, y, bw, bh)


def build_overlay(a: QImage, b: QImage, opacity: float = 0.5) -> QImage:
    """把 B 以给定不透明度叠在 A 上，用来发现细微位移/改动。B 先等比对齐。"""
    cb, rect = _align_pair(a, b)
    out = QImage(a.size(), QImage.Format.Format_RGB888)
    out.fill(QColor(0, 0, 0))
    p = QPainter(out)
    p.drawImage(0, 0, a.convertToFormat(QImage.Format.Format_RGB888))
    p.setOpacity(opacity)
    p.drawImage(rect.topLeft(), cb)
    p.end()
    return out


def build_diff(a: QImage, b: QImage, gain: float = 5.0) -> tuple[QImage, float]:
    """差异高亮：把两图的绝对差放大后画成热力图，返回 (图, 差异像素占比)。

    占比按「对比区域」计算：宽高比一致时为全帧，裁剪版为重叠区域；
    未重叠处画成暗边、不参与占比。
    """
    cb, rect = _align_pair(a, b)
    ca = a.convertToFormat(QImage.Format.Format_RGB888)
    aa = qimage_to_rgb(ca).astype(np.int16)
    ih, iw = aa.shape[:2]

    # B 的放置区域与画面的交集
    x0, y0 = max(0, rect.left()), max(0, rect.top())
    x1, y1 = min(iw, rect.left() + rect.width()), min(ih, rect.top() + rect.height())
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        # 退化（极端尺寸差完全错开）：退回整帧缩放对比，避免输出无意义的全暗图
        cb = cb.scaled(a.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
        rect = QRect(0, 0, iw, ih)
        x0, y0, x1, y1 = 0, 0, iw, ih

    bb_full = qimage_to_rgb(cb.convertToFormat(QImage.Format.Format_RGB888)).astype(np.int16)
    bb = np.zeros_like(aa)
    sy0 = max(0, y0 - rect.top())
    sx0 = max(0, x0 - rect.left())
    sy1 = min(bb_full.shape[0], sy0 + (y1 - y0))
    sx1 = min(bb_full.shape[1], sx0 + (x1 - x0))
    bb[y0:y0 + (sy1 - sy0), x0:x0 + (sx1 - sx0)] = bb_full[sy0:sy1, sx0:sx1]

    d = np.abs(aa - bb).max(axis=2).astype(np.float32)
    compared = np.zeros((ih, iw), dtype=bool)
    compared[y0:y1, x0:x1] = True

    # 只保留结构差异，忽略编码噪声；未重叠处不画热区
    hot = np.clip((d - 12.0) * gain, 0, 255).astype(np.uint8)
    hot[~compared] = 0
    ratio = float((d[compared] > 12).mean()) * 100.0

    base = (aa * 0.25 + 40).astype(np.uint8)
    dim = (base[~compared].astype(np.float32) * 0.35).astype(np.uint8)
    base[~compared] = dim
    mask = hot > 0
    # 120-hot 必须先升 int16：uint8 直接减会下溢（120-255 → 121），热区该是纯红会变粉
    cool = np.clip(120 - hot.astype(np.int16), 0, 255).astype(np.uint8)
    base[..., 0] = np.where(mask, 255, base[..., 0])
    base[..., 1] = np.where(mask, cool, base[..., 1])
    base[..., 2] = np.where(mask, cool, base[..., 2])
    return rgb_to_qimage(base), ratio


class ImagePane(QWidget):
    """单张图片查看：滚轮缩放（以光标为中心）、拖动平移、双击复位。

    视图状态用「归一化中心 + 相对适配的缩放倍数」表达，这样两张不同尺寸的图
    也能同步——同样倍数意味着同样比例的取景范围。
    """

    viewChanged = Signal(float, float, float)

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._zoom = 1.0
        self._cx = 0.5
        self._cy = 0.5
        self._silent = False
        self._drag_from: QPoint | None = None
        self._title = title
        self._badge = ""
        self._badge_color = theme.TEXT_DIM
        self._hint = ""
        self.setMinimumSize(220, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    # ---------- 对外 ----------
    def set_image(self, image: QImage | None, hint: str = "") -> None:
        self._image = image
        self._hint = hint
        self.reset_view()

    def set_badge(self, text: str, color: str) -> None:
        self._badge = text
        self._badge_color = color
        self.update()

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._cx = 0.5
        self._cy = 0.5
        self.update()
        self._emit_view()

    def view_state(self) -> tuple[float, float, float]:
        return self._zoom, self._cx, self._cy

    def set_view(self, zoom: float, cx: float, cy: float) -> None:
        self._silent = True
        try:
            self._zoom = max(0.05, min(40.0, zoom))
            self._cx, self._cy = cx, cy
            self.update()
        finally:
            self._silent = False

    def zoom_by(self, factor: float) -> None:
        self._zoom = max(0.05, min(40.0, self._zoom * factor))
        self.update()
        self._emit_view()

    # ---------- 内部 ----------
    def _emit_view(self) -> None:
        if not self._silent:
            self.viewChanged.emit(self._zoom, self._cx, self._cy)

    def _fit_scale(self) -> float:
        if not self._image or self._image.isNull():
            return 1.0
        iw, ih = self._image.width(), self._image.height()
        if iw <= 0 or ih <= 0:
            return 1.0
        return min(self.width() / iw, self.height() / ih)

    def _draw_rect(self) -> QRectF:
        iw, ih = self._image.width(), self._image.height()  # type: ignore[union-attr]
        s = self._fit_scale() * self._zoom
        dw, dh = iw * s, ih * s
        x = self.width() / 2.0 - self._cx * dw
        y = self.height() / 2.0 - self._cy * dh
        return QRectF(x, y, dw, dh)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0f1013"))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        if self._image is None or self._image.isNull():
            p.setPen(QColor(theme.TEXT_DIM))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       self._hint or "无法读取图片")
            p.end()
            return

        target = self._draw_rect()
        p.drawImage(target, self._image)

        if self._badge:
            font = p.font()
            font.setPointSize(11)
            font.setBold(True)
            p.setFont(font)
            metrics = p.fontMetrics()
            tw = metrics.horizontalAdvance(self._badge) + 20
            th = 26
            bx, by = 12.0, 12.0
            p.setPen(Qt.PenStyle.NoPen)
            bgc = QColor(self._badge_color)
            bgc.setAlpha(230)
            p.setBrush(bgc)
            p.drawRoundedRect(QRectF(bx, by, tw, th), 6, 6)
            p.setPen(QColor("#121316"))
            p.drawText(QRectF(bx, by, tw, th), Qt.AlignmentFlag.AlignCenter, self._badge)

        if self._title:
            p.setPen(QColor(theme.TEXT_DIM))
            p.drawText(10, self.height() - 10, self._title)
        p.end()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self._image or self._image.isNull():
            return
        delta = event.angleDelta().y()
        if not delta:
            return
        factor = 1.0 + (0.18 if delta > 0 else -0.15)
        new_zoom = max(0.05, min(40.0, self._zoom * factor))
        if abs(new_zoom - self._zoom) < 1e-6:
            return

        pos = event.position()
        rect = self._draw_rect()
        old_s = self._fit_scale() * self._zoom
        new_s = self._fit_scale() * new_zoom
        iw, ih = self._image.width(), self._image.height()

        # 光标下的图像坐标保持不变
        ix = (pos.x() - rect.left()) / max(1e-9, old_s)
        iy = (pos.y() - rect.top()) / max(1e-9, old_s)
        self._zoom = new_zoom
        dw, dh = iw * new_s, ih * new_s
        self._cx = (self.width() / 2.0 - (pos.x() - ix * new_s)) / max(1e-9, dw)
        self._cy = (self.height() / 2.0 - (pos.y() - iy * new_s)) / max(1e-9, dh)
        self.update()
        self._emit_view()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_from = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.reset_view()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is None or not self._image:
            return
        cur = event.position().toPoint()
        dx = cur.x() - self._drag_from.x()
        dy = cur.y() - self._drag_from.y()
        self._drag_from = cur
        s = self._fit_scale() * self._zoom
        iw, ih = self._image.width(), self._image.height()
        if iw and ih:
            self._cx -= dx / (iw * s)
            self._cy -= dy / (ih * s)
            self.update()
            self._emit_view()


class InfoCard(QFrame):
    """一张图的关键信息。字段固定，值可选中复制。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setMaximumHeight(210)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)

        self.head = QLabel("—")
        self.head.setObjectName("H2")
        self.head.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.head)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(3)
        self.grid.setColumnStretch(1, 1)
        self.grid.setColumnStretch(3, 1)
        lay.addLayout(self.grid)

        self._rows: dict[str, QLabel] = {}
        pairs = [("分辨率", "体积"), ("格式", "拍摄"), ("相机", "修改时间"), ("MD5", None)]
        row = 0
        for left, right in pairs:
            self._field(left, row, 0)
            if right:
                self._field(right, row, 2)
            row += 1
        self._field("判据", row, 0, colspan=3)
        row += 1
        self._field("位置", row, 0, colspan=3)

    def _field(self, key: str, row: int, col: int, colspan: int = 1) -> QLabel:
        name = QLabel(key)
        name.setObjectName("FieldName")
        val = QLabel("—")
        val.setObjectName("FieldValue")
        val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        val.setWordWrap(key == "位置")
        self.grid.addWidget(name, row, col)
        self.grid.addWidget(val, row, col + 1, 1, colspan)
        self._rows[key] = val
        return val

    def set_info(self, info: ImageInfo, root: Path,
                 verdict: PairVerdict | None, role: str) -> None:
        color = {"keep": theme.KEEP, "drop": theme.DROP}.get(role, theme.TEXT)
        self.head.setText(f"{info.name}")
        self.head.setStyleSheet(f"color: {color};")
        self.head.setToolTip(str(info.path))

        self._rows["分辨率"].setText(f"{info.width}×{info.height}（{info.pixels / 1e6:.1f} MP）")
        self._rows["体积"].setText(human_size(info.size))
        fmt = (info.fmt or info.ext.upper())
        if info.jpeg_quality:
            fmt += f" · 质量≈{info.jpeg_quality}"
        self._rows["格式"].setText(fmt)
        self._rows["拍摄"].setText(info.shot_time or "无")
        cam = f"{info.make} {info.model}".strip()
        if info.gps_zero:
            cam = (cam + " · GPS 被清零").strip(" ·")
        self._rows["相机"].setText(cam or "无 EXIF")
        self._rows["修改时间"].setText(
            datetime.fromtimestamp(info.mtime).strftime("%Y-%m-%d %H:%M"))
        self._rows["MD5"].setText(info.md5[:10] if info.md5 else "未计算")
        if verdict:
            bits = [REL_LABEL.get(verdict.rel, verdict.rel)]
            bits.append(f"pHash {verdict.pd}/64")
            if verdict.inliers:
                bits.append(f"内点 {verdict.inliers}（{verdict.ratio:.2f}）")
            if verdict.diff_ratio:
                bits.append(f"差异 {verdict.diff_ratio * 100:.1f}%")
            if verdict.blocks:
                bits.append(f"改动 {verdict.blocks} 处")
            self._rows["判据"].setText(" · ".join(bits))
        else:
            self._rows["判据"].setText("—")
        try:
            rel = info.path.relative_to(root)
        except ValueError:
            rel = info.path
        self._rows["位置"].setText(str(rel.parent) if str(rel.parent) != "." else "（根目录）")

    def clear(self) -> None:
        self.head.setText("—")
        for v in self._rows.values():
            v.setText("—")


class AdvicePanel(QFrame):
    """推荐结论 + 理由 + 警告。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)

        self.title = QLabel("—")
        self.title.setObjectName("H2")
        self.title.setWordWrap(True)
        lay.addWidget(self.title)

        self.reasons = QLabel("")
        self.reasons.setWordWrap(True)
        lay.addWidget(self.reasons)

        self.warns = QLabel("")
        self.warns.setWordWrap(True)
        self.warns.setStyleSheet(f"color: {theme.WARN};")
        lay.addWidget(self.warns)
        lay.addStretch(1)

    def set_advice(self, rec: Recommendation, names: dict[int, str]) -> None:
        """names: {图片索引: 显示名}。"""
        keep_name = names.get(rec.keep, "?")
        drop_name = names.get(rec.drop, "?")
        color = {"确定": theme.KEEP, "较高": theme.KEEP,
                 "可参考": theme.TEXT, "接近": theme.WARN}.get(rec.confidence, theme.TEXT)
        self.title.setText(
            f'推荐保留 <span style="color:{theme.KEEP}">{keep_name}</span>'
            f'　｜　删除 <span style="color:{theme.DROP}">{drop_name}</span>'
            f'　<span style="color:{color};font-size:12px">· {rec.confidence}</span>')
        self.title.setTextFormat(Qt.TextFormat.RichText)

        bullets = "".join(f"· {r}<br>" for r in rec.reasons)
        self.reasons.setText(bullets)
        self.reasons.setTextFormat(Qt.TextFormat.RichText)

        if rec.warnings:
            self.warns.setText("".join(f"⚠ {w}<br>" for w in rec.warnings))
            self.warns.setTextFormat(Qt.TextFormat.RichText)
            self.warns.show()
        else:
            self.warns.hide()
