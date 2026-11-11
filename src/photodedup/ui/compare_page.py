"""对比页：左右并排、叠加、差异高亮三种视角，以及删除/撤销主流程。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QSlider, QVBoxLayout, QWidget,
)

from ..core.hashing import md5_file
from ..core.models import REL_LABEL
from ..core.scanner import human_size
from ..core.session import Session
from . import theme
from .widgets import (
    ALIGN_TOLERANCE, AdvicePanel, ImagePane, InfoCard, build_diff, build_overlay,
    load_qimage,
)

COMPOSITE_MAX = 1800

HINTS = ("Delete 删除推荐删除的那张　·　1 / 2 指定删左边或右边　·　"
         "空格 两张都留　·　Z 撤销　·　F 切换视角　·　Esc 结束")


class ComparePage(QWidget):
    requestFinish = Signal()
    requestToast = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session: Session | None = None
        self._img_cache: dict[str, QImage | None] = {}
        self._mode = "side"
        self.allow_unsure_delete = False

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(10)

        # ---- 顶部状态条 ----
        top = QHBoxLayout()
        top.setSpacing(12)
        self.lbl_progress = QLabel("—")
        self.lbl_progress.setObjectName("H2")
        top.addWidget(self.lbl_progress)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedWidth(220)
        self.bar.setFixedHeight(8)
        top.addWidget(self.bar)

        self.lbl_stats = QLabel("")
        self.lbl_stats.setObjectName("Dim")
        top.addWidget(self.lbl_stats)
        top.addStretch(1)

        self.lbl_group = QLabel("—")
        self.lbl_group.setObjectName("Dim")
        top.addWidget(self.lbl_group)

        top.addSpacing(8)
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(["并排对比", "叠加对比", "差异高亮"])
        self.cmb_mode.setFixedWidth(110)
        self.cmb_mode.currentIndexChanged.connect(self._on_mode_changed)
        top.addWidget(self.cmb_mode)

        self.btn_skip = QPushButton("整组跳过")
        self.btn_skip.clicked.connect(self._skip_group)
        top.addWidget(self.btn_skip)

        self.btn_finish = QPushButton("结束并出报告")
        self.btn_finish.clicked.connect(self.requestFinish.emit)
        top.addWidget(self.btn_finish)
        root.addLayout(top)

        # ---- 主体：两张图（单图模式下左栏横跨两列）----
        self.body = QGridLayout()
        self.body.setHorizontalSpacing(10)
        self.body.setVerticalSpacing(8)
        self.left = self._build_side()
        self.right = self._build_side()
        self.body.addWidget(self.left["box"], 0, 0)
        self.body.addWidget(self.right["box"], 0, 1)
        self.body.setColumnStretch(0, 1)
        self.body.setColumnStretch(1, 1)
        self.body.setRowStretch(0, 1)
        root.addLayout(self.body, 1)

        # ---- 叠加透明度 / 差异提示 ----
        self.extra = QWidget()
        extra_lay = QHBoxLayout(self.extra)
        extra_lay.setContentsMargins(2, 0, 2, 0)
        self.lbl_opacity = QLabel("叠加不透明度")
        self.lbl_opacity.setObjectName("Dim")
        extra_lay.addWidget(self.lbl_opacity)
        self.sld_opacity = QSlider(Qt.Orientation.Horizontal)
        self.sld_opacity.setRange(0, 100)
        self.sld_opacity.setValue(50)
        self.sld_opacity.setFixedWidth(200)
        self.sld_opacity.valueChanged.connect(lambda _: self._render_composite())
        extra_lay.addWidget(self.sld_opacity)
        self.lbl_diff = QLabel("")
        self.lbl_diff.setObjectName("Dim")
        extra_lay.addWidget(self.lbl_diff)
        extra_lay.addStretch(1)
        self.extra.hide()
        root.addWidget(self.extra)

        # ---- 推荐 ----
        self.advice = AdvicePanel()
        root.addWidget(self.advice)

        hint = QLabel(HINTS)
        hint.setObjectName("Dim")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(hint)

    # ---------- 构建 ----------
    def _build_side(self) -> dict:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        pane = ImagePane()
        pane.setMinimumHeight(240)
        info = InfoCard()
        lay.addWidget(pane, 1)
        lay.addWidget(info)
        return {"box": box, "pane": pane, "info": info}

    # ---------- 生命周期 ----------
    def bind(self, session: Session) -> None:
        self.session = session
        self._img_cache.clear()
        self.refresh()

    def _load(self, path: Path) -> QImage | None:
        key = str(path)
        if key not in self._img_cache:
            if len(self._img_cache) > 8:
                self._img_cache.clear()
            self._img_cache[key] = load_qimage(path) if path.exists() else None
        return self._img_cache[key]

    def refresh(self) -> None:
        if not self.session:
            return
        bout = self.session.current()
        if bout is None:
            self.requestFinish.emit()
            return

        a = self.session.result.files[bout.champion]
        b = self.session.result.files[bout.challenger]
        for info in (a, b):  # 只对正在看的这两张补算 MD5，避免全量扫描 I/O
            if not info.md5:
                try:
                    info.md5 = md5_file(info.path)
                except Exception:
                    pass
        rec = self.session.advice()
        pv = self.session.pair_verdict()

        self.left["pane"].set_image(self._load(a.path))
        self.right["pane"].set_image(self._load(b.path))
        self.left["info"].set_info(a, self.session.root, pv,
                                   "keep" if rec and rec.keep == a.index else "drop")
        self.right["info"].set_info(b, self.session.root, pv,
                                    "keep" if rec and rec.keep == b.index else "drop")

        if rec:
            self.left["pane"].set_badge("保留" if rec.keep == a.index else "删除",
                                        theme.KEEP if rec.keep == a.index else theme.DROP)
            self.right["pane"].set_badge("保留" if rec.keep == b.index else "删除",
                                         theme.KEEP if rec.keep == b.index else theme.DROP)
            self.advice.set_advice(rec, {a.index: a.name, b.index: b.name})

        done, total = self.session.progress()
        self.lbl_progress.setText(f"第 {min(done + 1, max(total, 1))} / {max(total, 1)} 对")
        self.bar.setMaximum(max(total, 1))
        self.bar.setValue(done)
        freed = sum(r.size for r in self.session.deleted)
        self.lbl_stats.setText(
            f"已删 {len(self.session.deleted)} 个 · 释放 {human_size(freed)}"
            f" · 完成组 {self.session.groups_done()} / {len(self.session.groups)}")
        g = self.session.result.groups[bout.group_id]
        self.lbl_group.setText(
            f"本组 {g.size} 张 · 还剩 {len(self.session.groups[bout.group_id].alive)} 张"
            f" · {REL_LABEL.get(g.rel, g.rel)}")

        self._render_composite()

    # ---------- 视角 ----------
    def cycle_mode(self) -> None:
        self.cmb_mode.setCurrentIndex((self.cmb_mode.currentIndex() + 1) % 3)

    def _on_mode_changed(self, idx: int) -> None:
        self._mode = ("side", "overlay", "diff")[idx]
        side = self._mode == "side"
        if side:
            self.body.addWidget(self.left["box"], 0, 0)
            self.body.addWidget(self.right["box"], 0, 1)
            self.right["box"].show()
        else:
            self.right["box"].hide()
            self.body.addWidget(self.left["box"], 0, 0, 1, 2)
        self.extra.setVisible(not side)
        self.lbl_opacity.setVisible(self._mode == "overlay")
        self.sld_opacity.setVisible(self._mode == "overlay")
        self._render_composite()

    def _render_composite(self) -> None:
        if self._mode == "side" or not self.session:
            return
        bout = self.session.current()
        if not bout:
            return
        a = self._load(self.session.result.files[bout.champion].path)
        b = self._load(self.session.result.files[bout.challenger].path)
        if a is None or b is None:
            return
        if max(a.width(), a.height()) > COMPOSITE_MAX:
            s = COMPOSITE_MAX / max(a.width(), a.height())
            from PySide6.QtCore import QSize
            a = a.scaled(QSize(int(a.width() * s), int(a.height() * s)),
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        if self._mode == "overlay":
            op = self.sld_opacity.value() / 100.0
            self.left["pane"].set_image(build_overlay(a, b, op))
            self.lbl_diff.setText("滑块拉到两端可分别看两张，中间是混合结果")
        else:
            img, ratio = build_diff(a, b)
            self.left["pane"].set_image(img)
            text = f"高亮处为差异（占对比区域 {ratio:.1f}%）"
            ar_a = a.width() / max(1, a.height())
            ar_b = b.width() / max(1, b.height())
            if abs(ar_a - ar_b) > ALIGN_TOLERANCE * max(ar_a, ar_b):
                text += " · 两图宽高比不同，仅对比重叠区域（未重叠处已调暗）"
            self.lbl_diff.setText(text)

    # ---------- 操作 ----------
    def delete_side(self, side: int) -> None:
        if not self.session:
            return
        bout = self.session.current()
        if not bout:
            return
        target = bout.champion if side == 0 else bout.challenger
        self.delete_index(target)

    def delete_recommended(self) -> None:
        if not self.session:
            return
        rec = self.session.advice()
        if not rec:
            return
        if not rec.confident and not self.allow_unsure_delete:
            # 把握不大时不替用户做决定，要求按 1/2 明确指定
            self.requestToast.emit(
                f"两张差别不大（{rec.confidence}），请自己看图后用 1 或 2 指定删哪张")
            return
        self.delete_index(rec.drop)

    def delete_index(self, idx: int) -> None:
        if not self.session:
            return
        try:
            rec = self.session.delete(idx)
        except Exception as e:  # DeleteError 等
            self.requestToast.emit(f"删除失败：{e}")
            return
        self._img_cache.pop(str(rec.path), None)
        self.requestToast.emit(f"已移出：{rec.path.name}")
        self.refresh()

    def keep_both(self) -> None:
        if not self.session:
            return
        self.session.keep_both()
        self.refresh()

    def undo(self) -> None:
        if not self.session:
            return
        try:
            rec = self.session.undo()
        except Exception as e:
            self.requestToast.emit(f"撤销失败：{e}")
            return
        if rec is None:
            self.requestToast.emit("没有可撤销的删除")
            return
        self._img_cache.clear()
        self.requestToast.emit(f"已还原：{rec.path.name}")
        self.refresh()

    def _skip_group(self) -> None:
        if not self.session:
            return
        self.session.skip_group()
        self.refresh()
