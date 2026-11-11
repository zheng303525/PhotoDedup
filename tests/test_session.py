"""会话测试：擂台制逐个删除、组内保底、撤销、报告。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from photodedup.core.pipeline import Pipeline, ScanOptions
from photodedup.core.session import MODE_STAGING, DeleteError, Session


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

    assert len(session.deleted) == 8, f"9 张一组应该删掉 8 张，实际 {len(session.deleted)}"

    gid = 0
    alive = [m for m in session.groups[gid].alive]
    assert len(alive) == 1, "每组必须留一张"
    leftover = session.result.files[alive[0]].path
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
