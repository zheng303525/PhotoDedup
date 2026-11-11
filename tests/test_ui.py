"""UI 测试（pytest-qt，offscreen 无头）：对比页删除/撤销主流程、快捷键、叠加/差异合成。

覆盖此前为零的 UI 层。修复回归：
- 叠加/差异旧版把 B 拉伸到 A 的尺寸（IgnoreAspectRatio），裁剪版全图错位、
  差异占比失真；新版宽高比不同时 1:1 居中，差异只统计重叠区域。
- 提示条承诺「Esc 结束」但此前没有绑定。
"""
from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QImage

from photodedup.core.pipeline import Pipeline, ScanOptions
from photodedup.core.session import MODE_STAGING, Session
from photodedup.ui.compare_page import ComparePage
from photodedup.ui.main_window import MainWindow
from photodedup.ui.widgets import build_diff, build_overlay


@pytest.fixture()
def session(sample_dir: Path, tmp_path: Path) -> Session:
    work = tmp_path / "photos"
    shutil.copytree(sample_dir, work)
    opt = ScanOptions(root=work, threshold=8, dihedral=True)
    pipe = Pipeline(opt, tmp_path / "cache")
    res = pipe.run()
    pipe.close()
    return Session(res, opt, mode=MODE_STAGING, staging_root=tmp_path / "staging")


def _page(qtbot, session: Session) -> ComparePage:
    page = ComparePage()
    qtbot.addWidget(page)
    page.bind(session)
    return page


# ---------------- 叠加/差异合成（纯图像函数） ----------------

def _solid(w: int, h: int, color: tuple[int, int, int]) -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB888)
    img.fill(QColor(*color))
    return img


def _striped(w: int, h: int) -> QImage:
    """竖条纹图案：错位时逐像素差异很大，对齐时差异为 0。"""
    img = QImage(w, h, QImage.Format.Format_RGB888)
    p = QPainter(img)
    for x in range(0, w, 10):
        p.fillRect(x, 0, 5, h, QColor(200, 30, 30))
        p.fillRect(x + 5, 0, 5, h, QColor(30, 30, 200))
    p.end()
    return img


def test_overlay_same_size_blends(qtbot):
    """同尺寸（宽高比一致）：B 缩放到 A 的尺寸混合——与旧行为一致。"""
    a = _solid(100, 100, (200, 30, 30))
    b = _solid(100, 100, (30, 200, 30))
    out = build_overlay(a, b, 0.5)
    c = out.pixelColor(50, 50)
    assert abs(c.red() - 115) < 6 and abs(c.green() - 115) < 6, str(c)


def test_overlay_cropped_not_distorted(qtbot):
    """修复点：宽高比不同（裁剪版）时 B 不再被拉伸变形。

    A=100x100 红，B=50x100 绿（1:2 竖条）。旧版把 B 拉伸成 100x100，
    整个画面都被 B 污染；新版 B 1:1 居中，只覆盖 x∈[25,75) 的竖条。
    """
    a = _solid(100, 100, (200, 30, 30))
    b = _solid(50, 100, (30, 200, 30))
    out = build_overlay(a, b, 0.5)
    left = out.pixelColor(10, 50)
    assert left.red() > 180 and left.green() < 60, \
        f"B 之外的区域不该被污染（旧版拉伸会污染）：{left}"
    mid = out.pixelColor(50, 50)
    assert mid.green() > 90, f"B 覆盖区域应是混合色：{mid}"


def test_diff_cropped_version_aligns_pixel_perfect(qtbot):
    """修复点的核心回归：同尺度裁剪版在差异视角下应对齐得像素级精确。

    B = A 中央裁剪（同尺度、未缩放）。新版把 B 1:1 放回裁剪原点，
    重叠区域差异应为 0；旧版拉伸 B 导致全图错位，差异大量虚报。
    """
    a = _striped(100, 100)
    b = a.copy(25, 10, 50, 80)  # 中央裁剪，同尺度
    img, ratio = build_diff(a, b)
    assert ratio == 0.0, f"同尺度裁剪版应对齐精确（重叠区差异为 0），实际 {ratio}%"


def test_diff_cropped_letterbox_dimmed_and_excluded(qtbot):
    """宽高比不同时：未重叠处调暗、不画热区、不参与占比。"""
    a = _solid(100, 100, (200, 30, 30))
    b = _solid(50, 100, (30, 200, 30))
    img, ratio = build_diff(a, b)
    assert ratio == 100.0, f"重叠区域（B 的竖条）与 A 完全不同，实际 {ratio}%"
    hot_px = img.pixelColor(50, 50)
    assert hot_px.red() == 255, f"重叠区的差异处应高亮：{hot_px}"
    for x in (10, 90):
        px = img.pixelColor(x, 50)
        assert px.red() < 60, f"未重叠处应调暗且无热区：{px}"


def test_diff_same_size_full_frame_ratio(qtbot):
    a = _solid(100, 100, (200, 30, 30))
    b = _solid(100, 100, (30, 200, 30))
    img, ratio = build_diff(a, b)
    assert ratio == 100.0


# ---------------- 对比页主流程 ----------------

def test_compare_page_bind_and_refresh(qtbot, session: Session):
    page = _page(qtbot, session)
    bout = session.current()
    assert bout is not None
    assert "第 1 /" in page.lbl_progress.text()
    img = page.left["pane"]._image
    assert img is not None and not img.isNull()
    assert page.advice.title.text() != "—"


def test_delete_side_moves_file_and_updates_state(qtbot, session: Session):
    page = _page(qtbot, session)
    bout = session.current()
    target = session.result.files[bout.challenger].path
    n_before = len(session.deleted)
    with qtbot.waitSignal(page.requestToast, timeout=10000):
        page.delete_side(1)  # 删右边（挑战者）
    assert len(session.deleted) == n_before + 1
    assert not target.exists(), "删除后原位置不该有文件"
    rec = session.deleted[-1]
    assert rec.staged is not None and rec.staged.exists(), "staging 模式应移入暂存区"


def test_delete_recommended_refuses_when_unsure(qtbot, session: Session):
    """把握不大时不替用户做决定，要求按 1/2 明确指定。"""
    page = _page(qtbot, session)
    rec = session.advice()
    assert rec is not None
    unsure = dataclasses.replace(rec, confidence="可参考")  # 非「确定/较高」→ confident=False
    page.session.advice = lambda: unsure  # type: ignore[method-assign]
    n_before = len(session.deleted)
    with qtbot.waitSignal(page.requestToast, timeout=10000):
        page.delete_recommended()
    assert len(session.deleted) == n_before, "把握不大时不应替用户删除"


def test_undo_restores_file(qtbot, session: Session):
    page = _page(qtbot, session)
    bout = session.current()
    target = session.result.files[bout.challenger].path
    page.delete_side(1)
    assert not target.exists()
    page.undo()
    assert target.exists(), "撤销后文件必须回到原位置"


def test_mode_switch_hides_right_pane(qtbot, session: Session):
    page = _page(qtbot, session)
    page.cycle_mode()
    assert page._mode == "overlay"
    assert page.right["box"].isHidden(), "叠加模式下右侧应隐藏"


# ---------------- 主窗口快捷键 ----------------

def _window(qtbot, session: Session) -> MainWindow:
    win = MainWindow()
    qtbot.addWidget(win)
    win.session = session
    win.compare_page.bind(session)
    win.stack.setCurrentWidget(win.compare_page)
    win._set_shortcuts_enabled(True)
    win.show()
    win.activateWindow()
    win.compare_page.setFocus()
    return win


def test_main_window_shortcuts_wired_and_gated(qtbot, session: Session):
    """快捷键的接线与启用门控。

    QShortcutMap 的按键分发依赖窗口系统的焦点/活动窗口机制，offscreen 平台
    不触发（已实测：shortcut enabled 但 QTest.keyClick 不激活）。因此用信号
    发射验证「快捷键→处理器」的接线，用断言验证「仅对比页启用」的门控。
    """
    win = _window(qtbot, session)
    assert win.sc_f.isEnabled(), "对比页上快捷键应启用"

    win.back_to_start()
    assert not win.sc_f.isEnabled(), "离开对比页后快捷键应禁用"

    win.session = session
    win.compare_page.bind(session)
    win.stack.setCurrentWidget(win.compare_page)
    win._set_shortcuts_enabled(True)

    win.sc_f.activated.emit()  # F 切换视角
    assert win.compare_page._mode == "overlay", "F 的接线应切换到叠加视角"

    bout = session.current()
    win.sc_space.activated.emit()  # 空格两张都留
    assert bout.challenger in session.groups[bout.group_id].kept, "空格的接线应保留两张"

    bout2 = session.current()
    target = session.result.files[bout2.challenger].path
    win.sc_two.activated.emit()  # 2 删右边
    assert not target.exists(), "2 的接线应删除右边那张"

    win.sc_z.activated.emit()  # Z 撤销
    assert target.exists(), "Z 的接线应撤销删除"


def test_esc_shortcut_wired_to_finish(qtbot, session: Session):
    """修复回归：提示条承诺「Esc 结束」，此前没有绑定——现在 sc_esc 接到 finish。"""
    win = _window(qtbot, session)
    win.sc_esc.activated.emit()
    assert win.stack.currentWidget() is win.finish_page, "Esc 的接线应结束并出报告"
