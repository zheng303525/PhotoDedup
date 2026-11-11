"""扫描编排：三层判重流水线 + 指纹缓存 + 进度回报。

流水线顺序（不可颠倒，越往后越不确定）：
1. 大小分桶 → MD5          字节完全一致，可定论
2. 指纹 + 候选生成          感知粗筛，只出候选
3. 全尺寸解码比对           像素完全一致，可定论
4. ORB 对齐 + 差异分布      近重确认，只给「较高」置信

任何一步判定为「同一张」的，都会进入并查集；最终聚成组。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import scanner
from .grouping import candidate_pairs, collect_groups, unique_pairs
from .hashing import fingerprints, hamming, md5_file
from .imageio import decoded_rgb_md5, exif_score, load_rgb
from .models import (
    ImageInfo, Group, PairVerdict, ScanStats,
    REL_BURST, REL_EXACT, REL_NEAR, REL_SAME_PIXEL, REL_WEAK,
)
from .verify import (
    COLOR_DIFF_THR, DEF_MAX_BLOCK, DEF_MAX_BLOCKS, DEF_MAX_DIFF, DEF_MAX_FILL,
    DEF_MIN_INLIERS, DEF_MIN_RATIO, evaluate_pair, flat_color_signature,
)

try:
    import cv2
    cv2.setNumThreads(1)  # 我们自己开线程池，避免 OpenCV 内部再并一层
except Exception:  # pragma: no cover
    cv2 = None

ProgressFn = Callable[[str, int, int, str], None]
CancelFn = Callable[[], bool]

CONF_ORDER = {REL_EXACT: 3, REL_SAME_PIXEL: 2, REL_NEAR: 1}


def _limit_pairs(pairs: list[tuple[int, int, int]], max_links: int) -> list[tuple[int, int, int]]:
    """每张图最多保留 max_links 条最近的候选边，按距离从近到远贪心。

    同组几十张时，全组合验证是 O(N²) 次解码+ORB；实际只需要保证
    「每张图至少连到最近的几张」，传递性足以把整组串起来。
    """
    if max_links <= 0 or len(pairs) <= max_links:
        return pairs
    degree: dict[int, int] = {}
    keep: list[tuple[int, int, int]] = []
    for pa, pb, d in sorted(pairs, key=lambda x: x[2]):
        if degree.get(pa, 0) >= max_links and degree.get(pb, 0) >= max_links:
            continue
        if degree.get(pa, 0) >= max_links * 2 and degree.get(pb, 0) >= max_links * 2:
            continue
        keep.append((pa, pb, d))
        degree[pa] = degree.get(pa, 0) + 1
        degree[pb] = degree.get(pb, 0) + 1
    return keep


def _make_pv(res: dict, pd: int) -> PairVerdict:
    """把 evaluate_pair 的返回翻译成 PairVerdict（几何层与颜色层共用）。"""
    return PairVerdict(
        a=res["a"], b=res["b"], rel=res["rel"], pd=pd, dd=0,
        inliers=res["inliers"], ratio=res["ratio"],
        matches=res["matches"], diff_ratio=res["diff_ratio"],
        max_block=res["max_block"], fill=res["fill"],
        blocks=res.get("blocks", 0), note=res["note"],
    )


@dataclass
class ScanOptions:
    root: Path
    recursive: bool = True
    skip_hidden: bool = True
    threshold: int = 8
    dihedral: bool = True
    detect_near: bool = True
    detect_same_pixel: bool = True
    use_orb: bool = True
    min_inliers: int = DEF_MIN_INLIERS
    min_ratio: float = DEF_MIN_RATIO
    max_block: float = DEF_MAX_BLOCK
    max_diff: float = DEF_MAX_DIFF
    max_fill: float = DEF_MAX_FILL
    max_blocks: int = DEF_MAX_BLOCKS
    max_links: int = 12
    workers: int = 0

    def worker_count(self) -> int:
        if self.workers > 0:
            return self.workers
        return max(2, min(16, (os.cpu_count() or 4)))


@dataclass
class ScanResult:
    root: Path
    files: list[ImageInfo] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    low_confidence_groups: list[Group] = field(default_factory=list)
    edges: dict[tuple[int, int], PairVerdict] = field(default_factory=dict)
    stats: ScanStats = field(default_factory=ScanStats)
    warnings: list[str] = field(default_factory=list)

    def info(self, idx: int) -> ImageInfo:
        return self.files[idx]


class FingerprintCache:
    """sqlite 指纹缓存：同一文件（相对路径+大小+修改时间未变）二次扫描直接命中。

    键是「相对扫描根的路径」而不是绝对路径：整个照片库搬家/换盘符/根目录改名后，
    缓存仍然命中——个人用户整理目录是高频操作，绝对路径键会让每次整理都白算一遍。
    root 列记录该行最后由哪个库写入，清理失效条目时按库隔离，不误删其他库的缓存。
    取舍：两个库若存在相对路径相同、大小与修改时间也相同的文件（同一份拷贝，
    内容本就一致），会互相命中——与断点续扫缓存的 (路径,大小,mtime) 指纹同一风险级别。
    """

    def __init__(self, db_path: Path | str, root: Path | str | None = None) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._root_path = Path(root) if root is not None else None
        self.root_key = os.path.normcase(str(self._root_path)) if root is not None else None
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS fp ("
            "path TEXT PRIMARY KEY, root TEXT, size INTEGER, mtime REAL, data TEXT)"
        )
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self.conn.commit()
        if self.root_key is not None:
            self._migrate_legacy()
        # root 列可能由上面的迁移补上，索引必须在迁移之后建
        self.conn.execute("CREATE INDEX IF NOT EXISTS fp_root ON fp(root)")
        self.conn.commit()

    # ---------- 键 ----------
    def key_for(self, path: Path) -> str:
        return os.path.normcase(self._rel(path))

    def _rel(self, path: Path) -> str:
        if self._root_path is not None:
            try:
                return path.relative_to(self._root_path).as_posix()
            except ValueError:
                pass
        return path.as_posix()

    # ---------- 旧格式迁移（v1：绝对路径键、无 root 列） ----------
    def _migrate_legacy(self) -> None:
        with self._lock:
            row = self.conn.execute("SELECT v FROM meta WHERE k = 'schema'").fetchone()
            if row and row[0] == "2":
                return
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(fp)")}
            if "root" not in cols:
                self.conn.execute("ALTER TABLE fp ADD COLUMN root TEXT DEFAULT ''")
            # v1 的键是绝对路径（Windows 路径不含 "|"）：同一根下的迁到新键，其余清理
            rows = self.conn.execute(
                "SELECT path, size, mtime FROM fp WHERE instr(path, '|') = 0"
            ).fetchall()
            root_nc = Path(os.path.normcase(str(self._root_path)))
            updates, dead = [], []
            for p, s, m in rows:
                try:
                    rel = Path(os.path.normcase(p)).relative_to(root_nc).as_posix()
                except ValueError:
                    dead.append((p,))
                    continue
                updates.append((os.path.normcase(rel), self.root_key, p))
            if updates:
                self.conn.executemany(
                    "UPDATE fp SET path = ?, root = ? WHERE path = ?", updates)
            if dead:
                self.conn.executemany("DELETE FROM fp WHERE path = ?", dead)
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (k, v) VALUES ('schema', '2')")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (k, v) VALUES ('last_root', ?)",
                (self.root_key,),
            )
            self.conn.commit()

    def get(self, path: Path, size: int, mtime: float) -> dict | None:
        import json

        with self._lock:
            row = self.conn.execute(
                "SELECT size, mtime, data FROM fp WHERE path = ?", (self.key_for(path),)
            ).fetchone()
        if not row:
            return None
        c_size, c_mtime, data = row
        if int(c_size) != int(size) or abs(float(c_mtime) - float(mtime)) > 1e-6:
            return None
        try:
            return json.loads(data)
        except Exception:
            return None

    def put(self, path: Path, size: int, mtime: float, data: dict) -> None:
        import json

        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO fp (path, root, size, mtime, data) VALUES (?,?,?,?,?)",
                (self.key_for(path), self.root_key, int(size), float(mtime),
                 json.dumps(data, ensure_ascii=False)),
            )
            self.conn.commit()

    def prune(self, keep_keys: set[str]) -> int:
        """清理失效条目：只删本库（root 列）写入、且不在当前扫描里的行。

        扫描结束（未取消）时调用：改名的文件、已删除的文件的旧键被清掉，
        防止缓存无限增长。库搬家后键不变、root 列过时——先把路径仍在的行
        认领回本库再清理，否则旧标签的行永远不在清理范围内，会永久残留。
        其他库（相对路径不同）的行不受影响。
        """
        if self.root_key is None or not keep_keys:
            return 0
        with self._lock:
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS keep (k TEXT PRIMARY KEY)")
            self.conn.execute("DELETE FROM keep")
            self.conn.executemany(
                "INSERT OR IGNORE INTO keep VALUES (?)", ((k,) for k in keep_keys))
            self.conn.execute(
                "UPDATE fp SET root = ? WHERE root IS NOT ? AND path IN (SELECT k FROM keep)",
                (self.root_key, self.root_key),
            )
            cur = self.conn.execute(
                "DELETE FROM fp WHERE root = ? AND path NOT IN (SELECT k FROM keep)",
                (self.root_key,),
            )
            n = cur.rowcount
            self.conn.execute("DROP TABLE IF EXISTS keep")
            self.conn.commit()
            return max(n, 0)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def _compute_info(entry: scanner.FileEntry, index: int, with_dihedral: bool,
                  need_md5: bool) -> ImageInfo:
    """单文件指纹计算，跑在线程池里。"""
    info = ImageInfo(index=index, path=entry.path, size=entry.size, mtime=entry.mtime)
    try:
        if need_md5:
            info.md5 = md5_file(entry.path)
        rgb, meta = load_rgb(entry.path)
        fp = fingerprints(rgb, with_dihedral=with_dihedral)
        info.width = int(meta.get("width", 0))
        info.height = int(meta.get("height", 0))
        info.fmt = str(meta.get("fmt", ""))
        info.mode = str(meta.get("mode", ""))
        info.sharpness = float(meta.get("sharpness", 0.0))
        info.jpeg_quality = meta.get("jpeg_quality")
        info.make = str(meta.get("make", ""))
        info.model = str(meta.get("model", ""))
        info.shot_time = str(meta.get("shot_time", ""))
        info.gps = bool(meta.get("gps", False))
        info.gps_zero = bool(meta.get("gps_zero", False))
        info.has_icc = bool(meta.get("has_icc", False))
        info.exif_score = exif_score(meta)
        info.phash = fp["phash"]
        info.dhash = fp["dhash"]
        info.pvars = fp["pvars"]
        # 纯色图存 8×8 颜色签名：pHash 的 AC 位对噪声敏感，颜色是候选生成的唯一证据
        info.color_sig = flat_color_signature(rgb)
    except Exception as e:  # 损坏文件、缺解码器
        info.error = f"{type(e).__name__}: {e}"[:140]
    return info


class Pipeline:
    """一次完整扫描。可取消，可回报进度。"""

    def __init__(self, options: ScanOptions, cache_dir: Path | str,
                 progress: ProgressFn | None = None,
                 cancel: CancelFn | None = None) -> None:
        self.opt = options
        self.cache = FingerprintCache(Path(cache_dir) / "fingerprints.sqlite",
                                      root=Path(options.root))
        self.progress = progress
        self.cancel = cancel
        self._t0 = time.time()
        self.cache_hits = 0

    def _report(self, stage: str, done: int, total: int, text: str = "") -> None:
        if self.progress:
            self.progress(stage, done, max(total, 1), text)

    def _cancelled(self) -> bool:
        return bool(self.cancel and self.cancel())

    def run(self) -> ScanResult:
        opt = self.opt
        result = ScanResult(root=Path(opt.root))

        self._report("扫描目录", 0, 1, str(opt.root))
        entries, warns = scanner.scan_directory(
            opt.root, opt.recursive, opt.skip_hidden,
            progress=lambda n, p: self._report("扫描目录", n, max(n, 1), p),
            cancel=self.cancel,
        )
        result.warnings.extend(warns)
        result.stats.total_files = len(entries)
        if self._cancelled():
            result.stats.cancelled = True
            return result
        if not entries:
            result.stats.elapsed = time.time() - self._t0
            return result

        size_buckets = scanner.group_by_size(entries)
        need_md5 = {i for idxs in size_buckets.values() for i in idxs}

        # ---------- 阶段 1：指纹（含 MD5） ----------
        files = [None] * len(entries)  # type: ignore[list-item]
        total = len(entries)
        done = 0
        self._report("计算指纹", 0, total, "开始解码…")

        def work(item: tuple[int, scanner.FileEntry]) -> ImageInfo:
            idx, e = item
            cached = self.cache.get(e.path, e.size, e.mtime)
            if cached:
                info = ImageInfo.from_cache(idx, e.path, cached)
                if idx in need_md5 and not info.md5:
                    try:
                        info.md5 = md5_file(e.path)
                    except Exception:
                        pass
                self.cache_hits += 1
                return info
            info = _compute_info(e, idx, opt.dihedral, idx in need_md5)
            if not info.error:
                self.cache.put(e.path, e.size, e.mtime, info.to_cache())
            return info

        with ThreadPoolExecutor(max_workers=opt.worker_count()) as pool:
            futures = {pool.submit(work, (i, e)): i for i, e in enumerate(entries)}
            for fut in as_completed(futures):
                if self._cancelled():
                    for f in futures:
                        f.cancel()
                    break
                try:
                    info = fut.result()
                except Exception as e:  # pragma: no cover
                    idx = futures[fut]
                    info = ImageInfo(index=idx, path=entries[idx].path,
                                     size=entries[idx].size, mtime=entries[idx].mtime,
                                     error=str(e)[:140])
                files[info.index] = info
                done += 1
                if done % 5 == 0 or done == total:
                    self._report("计算指纹", done, total, entries[info.index].path.name)

        result.files = [f for f in files if f is not None]
        result.files.sort(key=lambda f: f.index)
        ok_idx = {f.index for f in result.files if f.ok}
        result.stats.images = len(ok_idx)
        result.stats.skipped = total - len(ok_idx)
        result.stats.cache_hits = self.cache_hits

        if self._cancelled():
            result.stats.cancelled = True
            result.stats.elapsed = time.time() - self._t0
            return result

        # ---------- 阶段 2：字节层（MD5 相同直接定论） ----------
        edges: dict[tuple[int, int], PairVerdict] = {}
        exact_of: dict[str, list[int]] = {}
        for f in result.files:
            if f.ok and f.md5:
                exact_of.setdefault(f.md5, []).append(f.index)
        for md5v, members in exact_of.items():
            for j in range(1, len(members)):
                a, b = members[0], members[j]
                edges[(min(a, b), max(a, b))] = PairVerdict(
                    a=a, b=b, rel=REL_EXACT, pd=0, dd=0,
                    note="MD5 完全相同，字节级一致",
                )

        # ---------- 阶段 3：候选生成 ----------
        alive = [f for f in result.files if f.ok]
        if not alive or self._cancelled():
            result.stats.elapsed = time.time() - self._t0
            return result

        idx_to_pos = {f.index: p for p, f in enumerate(alive)}
        variants = unique_pairs([[f.phash] + list(f.pvars) for f in alive])

        cand: set[tuple[int, int]] = set()
        if opt.detect_near or opt.detect_same_pixel:
            self._report("生成候选", 0, 1, f"{len(alive)} 张图")
            cand, _k = candidate_pairs(variants, max(opt.threshold, 0))
            if len(cand) > 1_500_000:
                result.warnings.append(
                    f"候选对过多（{len(cand)}），建议调小阈值或分目录扫描")

        # 先按精确距离分类
        near_pairs: list[tuple[int, int, int]] = []   # (pos_a, pos_b, pd)
        zero_pairs: list[tuple[int, int]] = []
        for pa, pb in cand:
            if self._cancelled():
                break
            fa, fb = alive[pa], alive[pb]
            d = min(hamming(x, y) for x in variants[pa] for y in variants[pb])
            if d == 0:
                zero_pairs.append((pa, pb))
            if d <= opt.threshold:
                near_pairs.append((pa, pb, d))

        # 一张图只需和最近的若干个候选复核：同组几十张时避免 O(N²) 次 ORB
        near_pairs = _limit_pairs(near_pairs, opt.max_links)
        zero_pairs = _limit_pairs([(a, b, 0) for a, b in zero_pairs], opt.max_links)
        zero_pairs = [(a, b) for a, b, _ in zero_pairs]

        # ---------- 纯色图颜色候选 ----------
        # pHash 的 AC 位对噪声敏感：两张纯色重压缩副本实测差 15-18，超阈值进不了候选，
        # 颜色签名（指纹阶段算好、只给纯色图存）是唯一可用证据。平坦图很少，两两比较可接受。
        color_pairs: list[tuple[int, int, int]] = []
        flat_files = [f for f in alive if f.color_sig]
        if len(flat_files) > 5000:
            result.warnings.append(f"纯色图过多（{len(flat_files)}），跳过颜色候选生成")
        elif len(flat_files) >= 2 and opt.detect_near:
            self._report("生成候选", 0, 1, f"纯色图 {len(flat_files)} 张")
            sigs = {f.index: np.array(f.color_sig, dtype=np.float32).reshape(8, 8, 3)
                    for f in flat_files}
            for i in range(len(flat_files)):
                fi = flat_files[i]
                for j in range(i + 1, len(flat_files)):
                    fj = flat_files[j]
                    if float(np.abs(sigs[fi.index] - sigs[fj.index]).sum()) <= COLOR_DIFF_THR:
                        pa, pb = idx_to_pos[fi.index], idx_to_pos[fj.index]
                        d = min(hamming(x, y) for x in variants[pa] for y in variants[pb])
                        color_pairs.append((pa, pb, d))

        # ---------- 阶段 4：全尺寸解码比像素（定论层） ----------
        same_pixel_pairs: list[tuple[int, int]] = []
        if opt.detect_same_pixel and zero_pairs:
            rgb_md5: dict[int, str] = {}

            def rgb_of(pos: int) -> str:
                if pos not in rgb_md5:
                    try:
                        rgb_md5[pos] = decoded_rgb_md5(alive[pos].path)
                    except Exception:
                        rgb_md5[pos] = ""
                return rgb_md5[pos]

            todo = [(pa, pb) for pa, pb in zero_pairs
                    if alive[pa].width == alive[pb].width
                    and alive[pa].height == alive[pb].height
                    and alive[pa].md5 != alive[pb].md5]
            total2 = max(len(todo), 1)
            for n, (pa, pb) in enumerate(todo, 1):
                if self._cancelled():
                    break
                da, db = rgb_of(pa), rgb_of(pb)
                if da and da == db:
                    same_pixel_pairs.append((pa, pb))
                    fa, fb = alive[pa], alive[pb]
                    edges[(min(fa.index, fb.index), max(fa.index, fb.index))] = PairVerdict(
                        a=fa.index, b=fb.index, rel=REL_SAME_PIXEL, pd=0, dd=0,
                        note="解码后像素逐字节相同，差异只在封装/元数据",
                    )
                if n % 3 == 0:
                    self._report("查同像素", n, total2, "")

        # ---------- 阶段 5：ORB 几何复核 ----------
        if opt.detect_near and opt.use_orb and near_pairs:
            skip = {(min(alive[a].index, alive[b].index),
                     max(alive[a].index, alive[b].index)) for a, b in same_pixel_pairs}
            todo = [(pa, pb, d) for pa, pb, d in near_pairs
                    if (min(alive[pa].index, alive[pb].index),
                        max(alive[pa].index, alive[pb].index)) not in skip
                    and (min(alive[pa].index, alive[pb].index),
                         max(alive[pa].index, alive[pb].index)) not in edges]
            total3 = max(len(todo), 1)
            self._report("几何复核", 0, total3, f"{len(todo)} 对候选")

            def verify(item: tuple[int, int, int]) -> dict | None:
                pa, pb, d = item
                fa, fb = alive[pa], alive[pb]
                try:
                    return {"a": fa.index, "b": fb.index,
                            **evaluate_pair(fa.path, fb.path, d,
                                            fa.width == fb.width and fa.height == fb.height,
                                            min_inliers=opt.min_inliers,
                                            min_ratio=opt.min_ratio,
                                            max_block=opt.max_block,
                                            max_diff=opt.max_diff,
                                            max_fill=opt.max_fill,
                                            max_blocks=opt.max_blocks)}
                except Exception:
                    return None

            workers = max(1, min(4, opt.worker_count() // 2))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(verify, it): it for it in todo}
                n = 0
                for fut in as_completed(futs):
                    n += 1
                    if self._cancelled():
                        for f in futs:
                            f.cancel()
                        break
                    res = fut.result()
                    if not res:
                        continue
                    it = futs[fut]
                    pv = _make_pv(res, it[2])
                    edges[(min(pv.a, pv.b), max(pv.a, pv.b))] = pv
                    fa, fb = alive[it[0]], alive[it[1]]
                    if n % 10 == 0 or n == total3:
                        self._report("几何复核", n, total3,
                                     f"{fa.name} ↔ {fb.name}")

        # ---------- 阶段 5b：纯色图颜色校验（不依赖 ORB，不受 use_orb 门控） ----------
        # 纯色图的 ORB 必然失败（提不到特征），evaluate_pair 里颜色门接力判定；
        # 候选双方都是纯色（color_sig 只给纯色图存），ORB 快速失败无浪费。
        if opt.detect_near and color_pairs:
            todo4 = [(pa, pb, d) for pa, pb, d in color_pairs
                     if (min(alive[pa].index, alive[pb].index),
                         max(alive[pa].index, alive[pb].index)) not in edges]
            if todo4:
                total4 = max(len(todo4), 1)
                self._report("颜色校验", 0, total4, f"{len(todo4)} 对纯色候选")

                def verify_color(item: tuple[int, int, int]) -> dict | None:
                    pa, pb, d = item
                    fa, fb = alive[pa], alive[pb]
                    try:
                        return {"a": fa.index, "b": fb.index,
                                **evaluate_pair(fa.path, fb.path, d,
                                                fa.width == fb.width and fa.height == fb.height,
                                                min_inliers=opt.min_inliers,
                                                min_ratio=opt.min_ratio,
                                                max_block=opt.max_block,
                                                max_diff=opt.max_diff,
                                                max_fill=opt.max_fill,
                                                max_blocks=opt.max_blocks)}
                    except Exception:
                        return None

                workers = max(1, min(4, opt.worker_count() // 2))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futs = {pool.submit(verify_color, it): it for it in todo4}
                    for fut in as_completed(futs):
                        if self._cancelled():
                            for f in futs:
                                f.cancel()
                            break
                        res = fut.result()
                        if not res:
                            continue
                        it = futs[fut]
                        pv = _make_pv(res, it[2])
                        edges[(min(pv.a, pv.b), max(pv.a, pv.b))] = pv
                        if len(futs) % 5 == 0:
                            self._report("颜色校验", len(futs), total4, "")

        # ---------- 阶段 6：聚组 ----------
        self._report("整理分组", 0, 1, "")
        accepted = [(e.a, e.b) for e in edges.values() if e.accepted]
        raw_groups = collect_groups(len(result.files), accepted)
        groups: list[Group] = []
        for members in raw_groups:
            pairs = [e for k, e in edges.items()
                     if k[0] in members and k[1] in members and e.accepted]
            weakest = min((p.rel for p in pairs), key=lambda r: CONF_ORDER.get(r, 0))
            groups.append(Group(members=list(members), rel=weakest, pairs=pairs))

        def reclaim(g: Group) -> int:
            total_size = sum(result.files[m].size for m in g.members)
            keep = max(result.files[m].size for m in g.members)
            return total_size - keep

        groups.sort(key=lambda g: (-g.size, -reclaim(g)))
        result.groups = groups

        # ---------- 低置信组：被算法排除但可人工复核（连拍/局部重叠） ----------
        # 这些边有几何证据（数值都留着），只是没通过阈值；静默丢弃就是「漏检」——
        # 降级展示给用户，宁漏改成可查，同时保住「不同截图」的防误判标定。
        reviewable = [(e.a, e.b) for e in edges.values()
                      if not e.accepted and e.rel in (REL_BURST, REL_WEAK)]
        if reviewable:
            low_raw = collect_groups(len(result.files), reviewable)
            low_groups: list[Group] = []
            for members in low_raw:
                pairs = [e for k, e in edges.items()
                         if k[0] in members and k[1] in members
                         and not e.accepted and e.rel in (REL_BURST, REL_WEAK)]
                weakest = REL_WEAK if any(p.rel == REL_WEAK for p in pairs) else REL_BURST
                low_groups.append(Group(members=list(members), rel=weakest, pairs=pairs))
            low_groups.sort(key=lambda g: -g.size)
            result.low_confidence_groups = low_groups

        result.edges = edges
        result.stats.groups = len(groups)
        result.stats.duplicated = sum(max(0, g.size - 1) for g in groups)
        result.stats.wasted_bytes = sum(reclaim(g) for g in groups)
        if not self._cancelled():
            # 清理失效缓存条目（改名/已删除文件的旧键）——按库隔离，只动本库的行
            removed = self.cache.prune({self.cache.key_for(e.path) for e in entries})
            if removed:
                result.warnings.append(f"已清理 {removed} 条失效缓存")
        result.stats.elapsed = time.time() - self._t0
        result.stats.cancelled = self._cancelled()
        self._report("完成", 1, 1, f"{len(groups)} 组重复")
        return result

    def close(self) -> None:
        self.cache.close()


def verify_missing_pair(result: ScanResult, a: int, b: int,
                        opt: ScanOptions) -> PairVerdict:
    """为一对没有预先计算过的图片即时补算判定（界面上按需调用）。"""
    key = (min(a, b), max(a, b))
    if key in result.edges:
        return result.edges[key]
    fa, fb = result.files[a], result.files[b]
    pd = min(hamming(x, y) for x in [fa.phash] + list(fa.pvars)
             for y in [fb.phash] + list(fb.pvars))
    try:
        res = evaluate_pair(fa.path, fb.path, pd,
                            fa.width == fb.width and fa.height == fb.height,
                            min_inliers=opt.min_inliers, min_ratio=opt.min_ratio,
                            max_block=opt.max_block, max_diff=opt.max_diff,
                            max_fill=opt.max_fill, max_blocks=opt.max_blocks)
    except Exception as e:
        return PairVerdict(a=a, b=b, pd=pd, note=f"复核失败：{e}")
    pv = PairVerdict(a=a, b=b, pd=pd, rel=res["rel"], inliers=res["inliers"],
                     ratio=res["ratio"], matches=res["matches"],
                     diff_ratio=res["diff_ratio"], max_block=res["max_block"],
                     fill=res["fill"], blocks=res.get("blocks", 0),
                     note=res["note"])
    result.edges[key] = pv
    return pv
