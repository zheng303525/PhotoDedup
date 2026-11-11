"""会话测试：擂台制逐个删除、组内保底、撤销、报告。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from photodedup.core.pipeline import Pipeline, ScanOptions
from photodedup.core.session import (
    MODE_STAGING, MODE_TRASH, DeleteError, Session, clear_saved_progress,
    restore_from_trash, saved_progress,
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


def test_report_includes_low_confidence_groups(session: Session):
    """报告里有低置信候选明细（决策可追溯）。"""
    rec = session.advice()
    session.delete(rec.drop)
    md, _csv = session.write_report(session.result.root)
    text = md.read_text(encoding="utf-8")
    assert "低置信候选" in text, "报告应包含低置信候选章节"


# ---------------- 审阅会话持久化 ----------------

def test_progress_saved_and_resumable(sample_dir: Path, tmp_path: Path):
    """审阅进度存盘 + 恢复：删除过的文件自然对齐（已移出不在重扫里），「都留」回放。"""
    work = tmp_path / "photos"
    shutil.copytree(sample_dir, work)
    opt = ScanOptions(root=work, threshold=8, dihedral=True)
    progress = tmp_path / "progress"

    pipe = Pipeline(opt, tmp_path / "cache")
    res1 = pipe.run()
    pipe.close()
    s1 = Session(res1, opt, mode=MODE_STAGING, staging_root=tmp_path / "staging",
                 progress_dir=progress)
    rec = s1.advice()
    del_rec = s1.delete(rec.drop)
    bout = s1.current()
    ia, ib = bout.champion, bout.challenger
    s1.keep_both()

    saved = saved_progress(work, progress)
    assert saved is not None, "决定后进度文件应存在"
    assert saved["processed"] == 2, f"应记录 2 个决定，实际 {saved['processed']}"

    # 第二次审阅（重扫同一目录）：删除过的文件不在结果里（已移出），「都留」回放
    pipe2 = Pipeline(opt, tmp_path / "cache")
    res2 = pipe2.run()
    pipe2.close()
    assert del_rec.path not in {i.path for i in res2.files}, "删除过的文件不该出现在重扫里"
    s2 = Session(res2, opt, mode=MODE_STAGING, staging_root=tmp_path / "staging2",
                 progress_dir=progress)
    n = s2.apply_resume(saved)
    assert n == 1, f"「都留」应回放 1 个决定，实际 {n}"
    st = next(s for s in s2.groups.values() if ia in s.ranked and ib in s.ranked)
    assert {ia, ib} <= st.kept, "都留的两张应标记为保留"


def test_skip_group_resumes_as_done(sample_dir: Path, tmp_path: Path):
    """整组跳过的决定恢复后，该组应标记为已完成。"""
    work = tmp_path / "photos"
    shutil.copytree(sample_dir, work)
    opt = ScanOptions(root=work, threshold=8, dihedral=True)
    progress = tmp_path / "progress"

    pipe = Pipeline(opt, tmp_path / "cache")
    res1 = pipe.run()
    pipe.close()
    s1 = Session(res1, opt, mode=MODE_STAGING, staging_root=tmp_path / "staging",
                 progress_dir=progress)
    bout = s1.current()
    member_paths = {res1.files[m].path for m in res1.groups[bout.group_id].members}
    s1.skip_group()

    pipe2 = Pipeline(opt, tmp_path / "cache")
    res2 = pipe2.run()
    pipe2.close()
    s2 = Session(res2, opt, mode=MODE_STAGING, staging_root=tmp_path / "staging2",
                 progress_dir=progress)
    n = s2.apply_resume(saved_progress(work, progress))
    assert n >= 1, "跳过决定应被回放"
    st = next(s for s in s2.groups.values()
              if {res2.files[m].path for m in s.ranked} & member_paths)
    assert st.done, "跳过的组恢复后应标记为已完成"


def test_clear_saved_progress(sample_dir: Path, tmp_path: Path):
    """审阅结束后清掉进度文件：文件存在 = 上次没走完（意外关闭）。"""
    from photodedup.core.session import _progress_path

    work = tmp_path / "photos"
    shutil.copytree(sample_dir, work)
    progress = tmp_path / "progress"
    assert saved_progress(work, progress) is None

    p = _progress_path(work, progress)
    import json as _json

    p.write_text(_json.dumps({"root": str(work),
                              "decisions": [{"action": "keep_both"}]}),
                 encoding="utf-8")
    assert saved_progress(work, progress) is not None
    clear_saved_progress(work, progress)
    assert saved_progress(work, progress) is None


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


def _recycle_roundtrip_works(work: Path) -> bool:
    """本机回收站能不能「删了再捞回来」。"""
    probe = work / "回收站能力探测.txt"
    probe.write_text("probe", encoding="utf-8")
    from send2trash import send2trash

    send2trash(str(probe))
    return restore_from_trash(probe) and probe.exists()


@pytest.mark.skipif(os.name != "nt", reason="回收站还原依赖 Windows Shell")
def test_undo_pulls_file_back_from_recycle_bin(trash_session: Session, tmp_path: Path):
    """回收站模式下撤销必须真的把文件捞回来（这条曾经是坏的：
    旧实现拿 Item.Path 比原路径，而回收站项的 Path 指向 $Recycle.Bin，永远不相等）。

    无 Explorer 会话的机器（比如 GitHub Actions 的 runner）上 Shell 命名空间拿不到回收站，
    这时跳过而不是假装成功 —— 真机（有桌面会话）上这条是硬验证。
    """
    if not _recycle_roundtrip_works(tmp_path):
        pytest.skip("本机回收站不可用（无桌面会话 / 回收站被禁用 / 网络盘），跳过")

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
