"""可复用的界面控件：看图控件、信息卡、推荐面板、叠加/差异合成。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
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


def build_overlay(a: QImage, b: QImage, opacity: float = 0.5) -> QImage:
    """把 B 以给定不透明度叠在 A 上，用来发现细微位移/改动。"""
    size = a.size()
    ca = a.convertToFormat(QImage.Format.Format_RGB888)
    cb = b.convertToFormat(QImage.Format.Format_RGB888).scaled(
        size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    out = QImage(size, QImage.Format.Format_RGB888)
    out.fill(QColor(0, 0, 0))
    p = QPainter(out)
    p.drawImage(0, 0, ca)
    p.setOpacity(opacity)
    p.drawImage(0, 0, cb)
    p.end()
    return out


def build_diff(a: QImage, b: QImage, gain: float = 5.0) -> tuple[QImage, float]:
    """差异高亮：把两图的绝对差放大后画成热力图，返回 (图, 差异像素占比)。"""
    size = a.size()
    ca = a.convertToFormat(QImage.Format.Format_RGB888)
    cb = b.convertToFormat(QImage.Format.Format_RGB888).scaled(
        size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)

    aa = qimage_to_rgb(ca).astype(np.int16)
    bb = qimage_to_rgb(cb).astype(np.int16)
    d = np.abs(aa - bb).max(axis=2).astype(np.float32)

    # 只保留结构差异，忽略编码噪声
    hot = np.clip((d - 12.0) * gain, 0, 255).astype(np.uint8)
    ratio = float((d > 12).mean()) * 100.0

    base = (aa * 0.25 + 40).astype(np.uint8)
    out = np.dstack([base, base, base])
    mask = hot > 0
    out[..., 0] = np.where(mask, 255, out[..., 0])
    out[..., 1] = np.where(mask, np.clip(120 - hot, 0, 255), out[..., 1])
    out[..., 2] = np.where(mask, np.clip(120 - hot, 0, 255), out[..., 2])
    return rgb_to_qimage(out), ratio


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
