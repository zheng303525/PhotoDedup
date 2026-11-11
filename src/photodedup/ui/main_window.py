"""主窗口：页面切换、扫描线程、全局快捷键。"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QStackedWidget

from .. import APP_NAME
from ..core.pipeline import Pipeline, ScanOptions, ScanResult
from ..core.session import MODE_STAGING, Session
from .compare_page import ComparePage
from .pages import FinishPage, ProgressPage, StartPage


def cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / "PhotoDedup" / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


class ScanWorker(QObject):
    progress = Signal(str, int, int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, opt: ScanOptions) -> None:
        super().__init__()
        self.opt = opt
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    @Slot()
    def run(self) -> None:
        try:
            pipe = Pipeline(self.opt, cache_dir(),
                            progress=lambda *a: self.progress.emit(*a),
                            cancel=lambda: self._cancel)
            res = pipe.run()
            pipe.close()
            self.finished.emit(res)
        except Exception as e:  # pragma: no cover - 兜底
            self.failed.emit(f"{type(e).__name__}: {e}")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1360, 880)
        self.setMinimumSize(980, 680)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.start_page = StartPage()
        self.progress_page = ProgressPage()
        self.compare_page = ComparePage()
        self.finish_page = FinishPage()
        for w in (self.start_page, self.progress_page, self.compare_page, self.finish_page):
            self.stack.addWidget(w)

        self.session: Session | None = None
        self.result: ScanResult | None = None
        self.opt: ScanOptions | None = None
        self._thread: QThread | None = None
        self._worker: ScanWorker | None = None
        self.report_path: Path | None = None

        self.start_page.startRequested.connect(self.start_scan)
        self.progress_page.cancelRequested.connect(self.cancel_scan)
        self.compare_page.requestFinish.connect(self.finish)
        self.compare_page.requestToast.connect(self.toast)
        self.finish_page.openReport.connect(self.open_report)
        self.finish_page.openStaging.connect(self.open_staging)
        self.finish_page.cleanupStaging.connect(self.cleanup_staging)
        self.finish_page.restart.connect(self.back_to_start)

        self.statusBar().showMessage("选一个目录开始")
        self._build_shortcuts()
        self.stack.setCurrentWidget(self.start_page)

    # ---------------- 快捷键 ----------------
    def _build_shortcuts(self) -> None:
        self._shortcuts: list[QShortcut] = []

        def add(seq: str, fn) -> QShortcut:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(fn)
            sc.setEnabled(False)
            self._shortcuts.append(sc)
            return sc

        self.sc_delete = add("Delete", self._on_delete)
        self.sc_one = add("1", lambda: self._on_delete_side(0))
        self.sc_two = add("2", lambda: self._on_delete_side(1))
        self.sc_space = add("Space", self._on_keep_both)
        self.sc_undo = add("Ctrl+Z", self._on_undo)
        self.sc_z = add("Z", self._on_undo)
        self.sc_f = add("F", self._on_cycle_mode)
        self.sc_plus = add("+", lambda: self._zoom(1.25))
        self.sc_minus = add("-", lambda: self._zoom(0.8))
        self.sc_zero = add("0", self._reset_view)

    def _set_shortcuts_enabled(self, on: bool) -> None:
        for sc in self._shortcuts:
            sc.setEnabled(on)

    def _compare_active(self) -> bool:
        return self.stack.currentWidget() is self.compare_page and self.session is not None

    def _on_delete(self) -> None:
        if self._compare_active():
            self.compare_page.delete_recommended()

    def _on_delete_side(self, side: int) -> None:
        if self._compare_active():
            self.compare_page.delete_side(side)

    def _on_keep_both(self) -> None:
        if self._compare_active():
            self.compare_page.keep_both()

    def _on_undo(self) -> None:
        if self._compare_active():
            self.compare_page.undo()

    def _on_cycle_mode(self) -> None:
        if self._compare_active():
            self.compare_page.cycle_mode()

    def _zoom(self, factor: float) -> None:
        if self._compare_active():
            self.compare_page.left["pane"].zoom_by(factor)
            self.compare_page.right["pane"].set_view(
                *self.compare_page.left["pane"].view_state())

    def _reset_view(self) -> None:
        if self._compare_active():
            self.compare_page.left["pane"].reset_view()
            self.compare_page.right["pane"].reset_view()

    # ---------------- 扫描 ----------------
    def start_scan(self, params: dict) -> None:
        root = Path(params["root"])
        self.opt = ScanOptions(
            root=root,
            threshold=int(params.get("threshold", 8)),
            dihedral=bool(params.get("dihedral", True)),
            detect_near=bool(params.get("detect_near", True)),
        )
        self._delete_mode = params.get("mode", MODE_STAGING)
        self.compare_page.allow_unsure_delete = bool(params.get("allow_unsure", False))

        self.stack.setCurrentWidget(self.progress_page)
        self._set_shortcuts_enabled(False)
        self.progress_page.lbl_title.setText("扫描目录…")
        self.progress_page.bar.setValue(0)
        self.progress_page.lbl_detail.setText(str(root))

        self._thread = QThread(self)
        self._worker = ScanWorker(self.opt)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress_page.update_progress)
        self._worker.finished.connect(self._on_scan_finished)
        self._worker.failed.connect(self._on_scan_failed)
        self._thread.start()
        self.statusBar().showMessage("正在扫描…")

    def cancel_scan(self) -> None:
        if self._worker:
            self._worker.cancel()
            self.progress_page.lbl_title.setText("正在取消…")

    def _teardown_thread(self) -> None:
        if self._thread:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
        self._worker = None

    @Slot(object)
    def _on_scan_finished(self, result: ScanResult) -> None:
        self._teardown_thread()
        self.result = result

        if result.stats.cancelled:
            self.toast("扫描已取消")
            self.back_to_start()
            return
        if not result.groups:
            self.toast("没有找到重复的图片")
            self.session = Session(result, self.opt, mode=self._delete_mode)
            self._show_finish()
            return

        self.session = Session(result, self.opt, mode=self._delete_mode)
        self.compare_page.bind(self.session)
        self.stack.setCurrentWidget(self.compare_page)
        self._set_shortcuts_enabled(True)
        self.compare_page.setFocus()
        self.statusBar().showMessage(
            f"找到 {result.stats.groups} 组重复，共 {result.stats.duplicated} 个副本"
            f"　·　扫描耗时 {result.stats.elapsed:.1f} 秒")

    @Slot(str)
    def _on_scan_failed(self, msg: str) -> None:
        self._teardown_thread()
        QMessageBox.critical(self, "扫描失败", msg)
        self.back_to_start()

    # ---------------- 结束 ----------------
    def finish(self) -> None:
        if not self.session:
            return
        self._show_finish()

    def _show_finish(self) -> None:
        self._set_shortcuts_enabled(False)
        s = self.session
        assert s is not None
        freed = sum(r.size for r in s.deleted)
        if s.deleted:
            try:
                md, _csv = s.write_report(s.root)
                self.report_path = md
            except Exception as e:
                self.toast(f"报告写入失败：{e}")
        self.finish_page.set_summary(
            deleted=len(s.deleted), freed=freed,
            groups=s.result.stats.groups, images=s.result.stats.images,
            mode=s.mode, elapsed=s.result.stats.elapsed,
            staging=s.staging_root)
        if not s.result.groups:
            self.finish_page.lbl_title.setText("没有找到重复")
            self.finish_page.lbl_body.setText(
                f"扫描了 <b>{s.result.stats.images}</b> 张图片，没有发现重复。<br>"
                "如果确信有重复但没抓到，可以把判重阈值调大一点再扫一次"
                "（代价是可能多出需要你看一眼的候选）。")
        self.stack.setCurrentWidget(self.finish_page)

    def open_report(self) -> None:
        if self.report_path and self.report_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.report_path)))
        else:
            self.toast("还没有生成报告（本次没有删除任何文件）")

    def open_staging(self) -> None:
        if self.session and self.session.staging_root.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.session.staging_root)))
        else:
            self.toast("待删文件夹是空的")

    def cleanup_staging(self) -> None:
        if not self.session:
            return
        n = sum(1 for p in self.session.staging_root.rglob("*") if p.is_file())
        if n == 0:
            self.toast("待删文件夹已经是空的")
            return
        ret = QMessageBox.question(
            self, "确认清空",
            f"将永久删除待删文件夹里的 {n} 个文件，这一步不可恢复。\n\n"
            f"{self.session.staging_root}\n\n确定吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        freed = self.session.cleanup_staging()
        self.toast(f"已清空，释放 {freed / 1024 / 1024:.1f} MB")
        self.finish_page.set_summary(
            deleted=len(self.session.deleted), freed=0,
            groups=self.session.result.stats.groups,
            images=self.session.result.stats.images,
            mode=self.session.mode, elapsed=self.session.result.stats.elapsed,
            staging=self.session.staging_root)

    def back_to_start(self) -> None:
        self.session = None
        self.result = None
        self.stack.setCurrentWidget(self.start_page)
        self._set_shortcuts_enabled(False)
        self.statusBar().showMessage("选一个目录开始")

    def toast(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 6000)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.session and self.session.deleted and self.session.mode == MODE_STAGING:
            ret = QMessageBox.question(
                self, "还有待删文件",
                f"待删文件夹里还有 {len(self.session.deleted)} 个文件：\n"
                f"{self.session.staging_root}\n\n"
                "现在退出的话它们会留在那里，你可以之后再手动清空。要继续退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._teardown_thread()
        event.accept()
