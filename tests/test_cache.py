"""指纹缓存测试：相对路径键（库搬家仍命中）、按库隔离的失效清理、v1 旧格式迁移。

修复回归：v1 用绝对路径做主键——照片库搬家/换盘符后缓存全部失效，
个人用户整理目录是高频操作，每次都白算一遍。

设计取舍说明：键是「相对扫描根的路径」，root 列记录该行最后由哪个库写入。
两个库若存在相对路径相同的文件（比如同一份拷贝），设计上共享同一行——
内容一致时互相命中是正确的；隔离性用相对路径不同的两个库验证。
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from photodedup.core.pipeline import FingerprintCache, Pipeline, ScanOptions


def _make_photos(root: Path) -> None:
    """两张小图：缓存测试只关心键与命中，不关心判重。"""
    from PIL import Image

    root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 40), (200, 30, 30)).save(root / "a.jpg", quality=90)
    sub = root / "trip"
    sub.mkdir(exist_ok=True)
    Image.new("RGB", (60, 40), (30, 200, 30)).save(sub / "b.jpg", quality=90)


def _make_other_lib(root: Path) -> None:
    """另一个库：相对路径与 _make_photos 不同，用于验证清理的库隔离。"""
    from PIL import Image

    root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 40), (90, 90, 200)).save(root / "x1.jpg", quality=90)
    sub = root / "trip"
    sub.mkdir(exist_ok=True)
    Image.new("RGB", (60, 40), (200, 200, 90)).save(sub / "y1.jpg", quality=90)


def _scan(root: Path, cache_dir: Path):
    opt = ScanOptions(root=root, threshold=8, dihedral=True)
    pipe = Pipeline(opt, cache_dir)
    res = pipe.run()
    pipe.close()
    return res


def test_rescan_hits_cache(tmp_path: Path):
    photos = tmp_path / "photos"
    _make_photos(photos)
    cache_dir = tmp_path / "cache"
    _scan(photos, cache_dir)
    res2 = _scan(photos, cache_dir)
    assert res2.stats.cache_hits == 2, f"二次扫描应命中 2 条，实际 {res2.stats.cache_hits}"


def test_cache_survives_root_move(tmp_path: Path):
    """修复点：整库搬家/改名后缓存仍命中（v1 的绝对路径键在这里会归零）。"""
    photos = tmp_path / "photos"
    _make_photos(photos)
    cache_dir = tmp_path / "cache"
    _scan(photos, cache_dir)

    moved = tmp_path / "renamed-library"
    photos.rename(moved)  # 同卷改名，文件 mtime 不变
    res2 = _scan(moved, cache_dir)
    assert res2.stats.cache_hits == 2, \
        f"整库搬家后应命中 2 条缓存，实际 {res2.stats.cache_hits}"

    # 认领：搬家后命中过的行，root 标签应跟随新根（否则会永久残留、无法清理）
    con = sqlite3.connect(str(cache_dir / "fingerprints.sqlite"))
    roots = {r[0] for r in con.execute("SELECT DISTINCT root FROM fp").fetchall()}
    con.close()
    assert roots == {os.path.normcase(str(moved))}, f"root 标签应跟随新根，实际 {roots}"


def test_changed_file_recomputed(tmp_path: Path):
    photos = tmp_path / "photos"
    _make_photos(photos)
    cache_dir = tmp_path / "cache"
    _scan(photos, cache_dir)

    from PIL import Image

    p = photos / "a.jpg"
    Image.new("RGB", (60, 40), (10, 10, 200)).save(p, quality=90)
    os.utime(p, (time.time() + 5, time.time() + 5))  # 确保 mtime 变化

    res2 = _scan(photos, cache_dir)
    assert res2.stats.cache_hits == 1, "改动的文件应重算，另一张应命中"


def test_renamed_file_gets_pruned_and_new_key_written(tmp_path: Path):
    """扫描结束清理失效条目：改名文件的旧键清掉、新键写入。"""
    photos = tmp_path / "photos"
    _make_photos(photos)
    cache_dir = tmp_path / "cache"
    _scan(photos, cache_dir)

    (photos / "a.jpg").rename(photos / "a_renamed.jpg")
    res = _scan(photos, cache_dir)

    con = sqlite3.connect(str(cache_dir / "fingerprints.sqlite"))
    rows = con.execute("SELECT path, root FROM fp").fetchall()
    con.close()
    own = os.path.normcase(str(photos))
    own_keys = {r[0] for r in rows if r[1] == own}
    assert "a.jpg" not in own_keys, "改名文件的旧键应被清理"
    assert "a_renamed.jpg" in own_keys, "新键应写入"


def test_prune_does_not_touch_other_libraries(tmp_path: Path):
    """清理按库隔离：本库的清理不误删其他库的缓存行。"""
    photos = tmp_path / "photos"
    _make_photos(photos)
    cache_dir = tmp_path / "cache"
    _scan(photos, cache_dir)

    other = tmp_path / "other-lib"
    _make_other_lib(other)
    cache2 = FingerprintCache(cache_dir / "fingerprints.sqlite", root=other)
    for name in ("x1.jpg", "trip/y1.jpg"):
        cache2.put(other / name, 100, 123.0, {"fake": True})
    cache2.close()

    # 本库改名一张后重扫，触发清理
    (photos / "a.jpg").rename(photos / "a_renamed.jpg")
    _scan(photos, cache_dir)

    con = sqlite3.connect(str(cache_dir / "fingerprints.sqlite"))
    rows = con.execute("SELECT path, root FROM fp").fetchall()
    con.close()
    other_key = os.path.normcase(str(other))
    other_rows = [r for r in rows if r[1] == other_key]
    assert len(other_rows) == 2, f"其他库的 2 条缓存不应被波及，实际 {other_rows}"


def test_legacy_absolute_path_keys_migrated(tmp_path: Path):
    """v1 旧格式（绝对路径键、无 root 列）一次性迁移：同一根下的迁到新键并保住热缓存。"""
    photos = tmp_path / "photos"
    _make_photos(photos)

    # 手工构造 v1 格式缓存：绝对路径做键、无 root 列、无 meta 表
    db = tmp_path / "cache" / "fingerprints.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE fp (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, data TEXT)")
    for name in ("a.jpg", "trip/b.jpg"):
        p = photos / name
        st = p.stat()
        con.execute("INSERT INTO fp VALUES (?,?,?,?)",
                    (str(p), st.st_size, st.st_mtime, "{}"))
    con.commit()
    con.close()

    cache = FingerprintCache(db, root=photos)
    p = photos / "a.jpg"
    hit = cache.get(p, p.stat().st_size, p.stat().st_mtime)
    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM fp").fetchone()[0]
    con.close()
    cache.close()
    assert hit is not None, "v1 旧键迁移后应命中（保住热缓存）"
    assert n == 2, f"两条旧键都应迁到新格式，实际剩 {n} 条"
