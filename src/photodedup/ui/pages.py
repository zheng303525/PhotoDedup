"""三个辅助页面：开始、扫描进度、结束总结。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QProgressBar, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ..core.scanner import human_size
from ..core.session import MODE_STAGING, MODE_TRASH
from . import theme


class StartPage(QWidget):
    startRequested = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(60, 50, 60, 50)
        lay.setSpacing(16)
        lay.addStretch(1)

        title = QLabel("图片去重工坊")
        title.setObjectName("H1")
        lay.addWidget(title)

        sub = QLabel("选一个目录，递归扫描所有子文件夹里的图片，逐个对比确认重复项，保留最好的那一份。")
        sub.setObjectName("Dim")
        sub.setWordWrap(True)
        lay.addWidget(sub)
        lay.addSpacing(10)

        card = QFrame()
        card.setObjectName("Card")
        grid = QGridLayout(card)
        grid.setContentsMargins(20, 18, 20, 18)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(12)

        r = 0
        lbl_dir = QLabel("目录")
        lbl_dir.setObjectName("FieldName")
        self.ed_dir = QLineEdit()
        self.ed_dir.setPlaceholderText("例如 D:\\OneDrive\\Pictures（也可以直接把文件夹拖进来）")
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.ed_dir, 1)
        row.addWidget(btn_browse)
        grid.addWidget(lbl_dir, r, 0)
        grid.addLayout(row, r, 1, 1, 3)
        r += 1

        lbl_thr = QLabel("判重阈值")
        lbl_thr.setObjectName("FieldName")
        self.spn_threshold = QSpinBox()
        self.spn_threshold.setRange(0, 16)
        self.spn_threshold.setValue(8)
        self.spn_threshold.setFixedWidth(80)
        thr_hint = QLabel("感知哈希允许的汉明距离，默认 8；调大能抓到更多改动，也更容易误判")
        thr_hint.setObjectName("Dim")
        thr_hint.setWordWrap(True)
        grid.addWidget(lbl_thr, r, 0)
        grid.addWidget(self.spn_threshold, r, 1)
        grid.addWidget(thr_hint, r, 2, 1, 2)
        r += 1

        lbl_del = QLabel("删除方式")
        lbl_del.setObjectName("FieldName")
        self.cmb_delete = QComboBox()
        self.cmb_delete.addItem("系统回收站（默认）", MODE_TRASH)
        self.cmb_delete.addItem("移到待删文件夹（100% 可撤销）", MODE_STAGING)
        self.cmb_delete.setFixedWidth(230)
        del_hint = QLabel("回收站方式跟资源管理器一致，撤销时程序自动去回收站捞回来；"
                          "待删文件夹方式不动回收站，适合先攒着核一遍再清空")
        del_hint.setObjectName("Dim")
        del_hint.setWordWrap(True)
        grid.addWidget(lbl_del, r, 0)
        grid.addWidget(self.cmb_delete, r, 1)
        grid.addWidget(del_hint, r, 2, 1, 2)
        r += 1

        self.chk_near = QCheckBox("检测感知近重（缩放、重压缩、调色、水印后的同一张）")
        self.chk_near.setChecked(True)
        self.chk_dihedral = QCheckBox("同时检测旋转 90° / 镜像的版本（慢一些）")
        self.chk_dihedral.setChecked(True)
        self.chk_unsure = QCheckBox("两张差别不大时，也允许直接按 Delete 删除（默认要求手动指定）")
        self.chk_unsure.setChecked(False)
        grid.addWidget(self.chk_near, r, 1, 1, 3)
        r += 1
        grid.addWidget(self.chk_dihedral, r, 1, 1, 3)
        r += 1
        grid.addWidget(self.chk_unsure, r, 1, 1, 3)

        lay.addWidget(card)
        lay.addSpacing(6)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.btn_start = QPushButton("开始扫描")
        self.btn_start.setObjectName("Primary")
        self.btn_start.setFixedHeight(40)
        self.btn_start.setMinimumWidth(160)
        self.btn_start.clicked.connect(self._start)
        actions.addWidget(self.btn_start)
        lay.addLayout(actions)

        tip = QLabel("扫描只读，不会动任何文件。删除是你在对比页逐个按键决定的，"
                     "程序不会自作主张批量删。")
        tip.setObjectName("Dim")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tip.setWordWrap(True)
        lay.addWidget(tip)
        lay.addStretch(2)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择要扫描的目录", self.ed_dir.text() or "")
        if d:
            self.ed_dir.setText(d)

    def _start(self) -> None:
        path = self.ed_dir.text().strip().strip('"')
        if not path:
            self.ed_dir.setFocus()
            return
        p = Path(path)
        if not p.is_dir():
            self.ed_dir.setStyleSheet(f"border: 1px solid {theme.DROP};")
            return
        self.ed_dir.setStyleSheet("")
        self.startRequested.emit({
            "root": p,
            "threshold": self.spn_threshold.value(),
            "detect_near": self.chk_near.isChecked(),
            "dihedral": self.chk_dihedral.isChecked(),
            "mode": self.cmb_delete.currentData(),
            "allow_unsure": self.chk_unsure.isChecked(),
        })

    # 支持把文件夹拖进来
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_dir():
                self.ed_dir.setText(str(p))
                break


class ProgressPage(QWidget):
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(90, 0, 90, 0)
        lay.addStretch(2)

        self.lbl_title = QLabel("准备扫描…")
        self.lbl_title.setObjectName("H1")
        lay.addWidget(self.lbl_title)
        lay.addSpacing(14)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        lay.addSpacing(10)

        self.lbl_detail = QLabel("")
        self.lbl_detail.setObjectName("Dim")
        lay.addWidget(self.lbl_detail)
        lay.addSpacing(18)

        row = QHBoxLayout()
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.cancelRequested.emit)
        row.addWidget(self.btn_cancel)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(3)

    def update_progress(self, stage: str, done: int, total: int, text: str) -> None:
        self.lbl_title.setText(stage)
        if total > 0:
            self.bar.setValue(int(done / total * 1000))
        fm = QFontMetrics(self.lbl_detail.font())
        self.lbl_detail.setText(
            fm.elidedText(f"{done} / {total}　{text}", Qt.TextElideMode.ElideMiddle, 700))


class FinishPage(QWidget):
    openReport = Signal()
    openStaging = Signal()
    cleanupStaging = Signal()
    restart = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(90, 0, 90, 0)
        lay.addStretch(2)

        self.lbl_title = QLabel("处理完毕")
        self.lbl_title.setObjectName("H1")
        lay.addWidget(self.lbl_title)
        lay.addSpacing(10)

        self.lbl_body = QLabel("")
        self.lbl_body.setWordWrap(True)
        self.lbl_body.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.lbl_body)
        lay.addSpacing(18)

        row = QHBoxLayout()
        self.btn_open_report = QPushButton("打开报告")
        self.btn_open_report.clicked.connect(self.openReport.emit)
        self.btn_open_staging = QPushButton("打开待删文件夹")
        self.btn_open_staging.clicked.connect(self.openStaging.emit)
        self.btn_cleanup = QPushButton("清空待删文件夹")
        self.btn_cleanup.setObjectName("Danger")
        self.btn_cleanup.clicked.connect(self.cleanupStaging.emit)
        self.btn_restart = QPushButton("扫描另一个目录")
        self.btn_restart.setObjectName("Primary")
        self.btn_restart.clicked.connect(self.restart.emit)
        for b in (self.btn_open_report, self.btn_open_staging, self.btn_cleanup):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.btn_restart)
        lay.addLayout(row)
        lay.addStretch(3)

    def set_summary(self, deleted: int, freed: int, groups: int, images: int,
                    mode: str, elapsed: float, staging: Path | None) -> None:
        if deleted == 0:
            body = (f"扫描了 <b>{images}</b> 张图片，找到 <b>{groups}</b> 组重复。"
                    f"本次没有删除任何文件，扫描耗时 {elapsed:.1f} 秒。")
        else:
            body = (f"扫描了 <b>{images}</b> 张图片，找到 <b>{groups}</b> 组重复。<br>"
                    f"本次删除 <b>{deleted}</b> 个文件，释放 <b>{human_size(freed)}</b>，"
                    f"扫描耗时 {elapsed:.1f} 秒。<br><br>"
                    + ("删除的文件在待删文件夹里，确认没问题再清空：<br>"
                       f"<span style='color:{theme.TEXT_DIM}'>{staging}</span>"
                       if mode == "staging" else
                       "删除的文件已进系统回收站，想找回按 <b>Z</b> 撤销，"
                       "或直接去回收站右键「还原」。"))
        self.lbl_body.setText(body)
        self.btn_cleanup.setVisible(mode == "staging" and deleted > 0)
        self.btn_open_staging.setVisible(mode == "staging" and deleted > 0)
        self.btn_open_report.setVisible(deleted > 0)
