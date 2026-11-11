"""会话测试：擂台制逐个删除、组内保底、撤销、报告。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from photodedup.core.pipeline import Pipeline, ScanOptions
from photodedup.core.session import (
    MODE_STAGING, MODE_TRASH, DeleteError, Session, restore_from_trash,
)


@pytest.fixture()
def session(sample_dir: Path, tmp_path: Path) -> Session:
    work = tmp_path / "photos"
    shutil.copytree(sample_dir, work)
    opt = ScanOptions(root=work, threshold=8, dihedral=True)
    pipe = Pipeline(opt, tmp_path / "cache")
    res = pipe.run()
    pipe.close()
    return Session(res, opt, mode=MODE_STAGING, staging_root=tmp_path / "staging")


def test_tournament_leaves_one_per_group(session: Session):
    guard = 0
    while session.current() is not None:
        guard += 1
        assert guard < 100, "队列没有收敛"
        rec = session.advice()
        assert rec is not None
        session.delete(rec.drop)

    assert session.current() is None, "队列没有收敛"

    expected = sum(max(0, g.size - 1) for g in session.result.groups)
    assert len(session.deleted) == expected, \
        f"每组应删到只剩一张，共 {expected} 次，实际 {len(session.deleted)}"

    for gid, st in session.groups.items():
        assert len(st.alive) == 1, f"组 {gid} 还剩 {len(st.alive)} 张，必须留一张"
        leftover = session.result.files[st.alive[0]].path
        assert leftover.exists(), "保留的那张不能被动"


def test_deleted_files_move_to_staging(session: Session):
    rec = session.advice()
    session.delete(rec.drop)
    moved = session.deleted[0]
    assert not moved.path.exists()
    assert moved.staged is not None and moved.staged.exists()
    assert session.staging_root in moved.staged.parents


def test_cannot_delete_last_one(session: Session):
    while session.current() is not None:
        rec = session.advice()
        session.delete(rec.drop)
    with pytest.raises(DeleteError):
        session.delete(session.result.groups[0].members[0])


def test_undo_restores_file(session: Session):
    rec = session.advice()
    session.delete(rec.drop)
    target = session.result.files[rec.drop].path
    assert not target.exists()
    session.undo()
    assert target.exists(), "撤销后文件必须回到原位置"


def test_group_refuses_delete_after_external_change(session: Session):
    rec = session.advice()
    target = session.result.files[rec.drop].path
    data = target.read_bytes()
    target.write_bytes(data + b"x" * 100)  # 外部改动，大小变化
    with pytest.raises(DeleteError):
        session.delete(rec.drop)
    assert target.exists()


def test_report_written(session: Session):
    rec = session.advice()
    session.delete(rec.drop)
    md, csv = session.write_report(session.result.root)
    assert md.exists() and csv.exists()
    text = md.read_text(encoding="utf-8")
    assert "去重报告" in text and "已删除的文件" in text


# ---------------- 默认模式：系统回收站 ----------------

def test_default_mode_is_trash():
    """默认走系统回收站（跟资源管理器一致），暂存区是可选项。"""
    import inspect
    sig = inspect.signature(Session.__init__)
    assert sig.parameters["mode"].default == MODE_TRASH


@pytest.fixture()
def trash_session(sample_dir: Path, tmp_path: Path) -> Session:
    work = tmp_path / "photos"
    shutil.copytree(sample_dir, work)
    opt = ScanOptions(root=work, threshold=8, dihedral=True)
    pipe = Pipeline(opt, tmp_path / "cache")
    res = pipe.run()
    pipe.close()
    return Session(res, opt, mode=MODE_TRASH)


@pytest.mark.skipif(os.name != "nt", reason="回收站还原依赖 Windows Shell")
def test_undo_pulls_file_back_from_recycle_bin(trash_session: Session):
    """回收站模式下撤销必须真的把文件捞回来（这条曾经是坏的：
    旧实现拿 Item.Path 比原路径，而回收站项的 Path 指向 $Recycle.Bin，永远不相等）。"""
    rec = trash_session.advice()
    target = trash_session.result.files[rec.drop].path
    size = target.stat().st_size
    trash_session.delete(rec.drop)
    assert not target.exists(), "删除后原位置不该还有文件"
    trash_session.undo()
    assert target.exists(), "撤销后文件必须从回收站回到原位置"
    assert target.stat().st_size == size


@pytest.mark.skipif(os.name != "nt", reason="依赖 Windows Shell")
def test_restore_from_trash_reports_failure(tmp_path: Path):
    """找不到就明确返回 False，不假装成功。"""
    ghost = tmp_path / "根本没被删过.jpg"
    ghost.write_bytes(b"x")
    assert restore_from_trash(ghost) is False
