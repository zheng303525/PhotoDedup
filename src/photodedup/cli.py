"""无界面自检：验证打包产物里的依赖、资源和判重内核是否完整。

界面程序是 windowed 的，没有控制台，所以结果写到文件里：

    PhotoDedup.exe --selftest --dir <目录> --out selftest.txt
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def _probe() -> list[str]:
    lines: list[str] = []
    for name in ("numpy", "cv2", "PIL", "pillow_heif", "send2trash"):
        try:
            mod = __import__(name)
            ver = getattr(mod, "__version__", "?")
            lines.append(f"OK   {name} {ver}")
        except Exception as e:
            lines.append(f"FAIL {name}: {type(e).__name__}: {e}")
    try:
        from PySide6 import QtCore

        lines.append(f"OK   PySide6 {QtCore.__version__}")
    except Exception as e:
        lines.append(f"FAIL PySide6: {e}")
    return lines


def _trash_roundtrip() -> str:
    """默认删除方式是系统回收站，撤销要靠 Shell COM 捞回来 —— 这条路在打包产物里必须能跑通，
    不然「按了删除却撤不回」是最难受的失败。所以自检里真删一次再捞回来。

    没有桌面会话的机器（CI runner）上 Shell 命名空间拿不到回收站，结果降级成 WARN，
    交给 test_session 里的硬测试在真机上把关。
    """
    if os.name != "nt":
        return "SKIP 回收站往返（非 Windows）"
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="pd_selftest_"))
    f = tmp / "自检-回收站 (1).txt"
    try:
        f.write_text("selftest", encoding="utf-8")
        from send2trash import send2trash

        send2trash(str(f))
        if f.exists():
            return "FAIL 回收站：send2trash 之后文件还在"
        from .core.session import restore_from_trash

        t0 = time.time()
        if not restore_from_trash(f):
            return ("WARN 回收站往返未通过：自动还原失败"
                    "（本机可能没有桌面会话 / 回收站被禁用 / 网络盘），撤销该机器上可能不可用")
        if f.read_text(encoding="utf-8") != "selftest":
            return "WARN 回收站往返：还原回来的内容不对"
        return f"OK   回收站往返（删除→还原 {time.time() - t0:.1f}s）"
    except Exception as e:
        return f"WARN 回收站往返异常：{type(e).__name__}: {e}"
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def selftest(directory: str | None, out: str | None) -> int:
    from . import __version__

    lines = [f"PhotoDedup {__version__}", f"frozen={getattr(sys, 'frozen', False)}",
             f"exe={sys.executable}"]
    lines += _probe()

    try:
        from .ui.app import resource_path

        icon = resource_path("icon.ico")
        lines.append(f"{'OK  ' if icon.exists() else 'FAIL'} resource icon.ico → {icon}")
    except Exception as e:
        lines.append(f"FAIL resource_path: {e}")

    lines.append(_trash_roundtrip())

    rc = 0
    if directory:
        try:
            from .core.pipeline import Pipeline, ScanOptions
            from .core.recommend import recommend

            opt = ScanOptions(root=Path(directory), threshold=8)
            cache = Path(os.environ.get("LOCALAPPDATA", ".")) / "PhotoDedup" / "cache"
            t0 = time.time()
            pipe = Pipeline(opt, cache)
            res = pipe.run()
            pipe.close()
            lines.append(f"OK   scan {res.stats.images} images in {time.time() - t0:.1f}s, "
                         f"{res.stats.groups} groups, {res.stats.duplicated} duplicated")
            for gi, g in enumerate(res.groups[:10], 1):
                names = ", ".join(res.files[m].name for m in g.members[:8])
                lines.append(f"     group {gi} ({g.size}): {names}")
            if res.groups:
                g = res.groups[0]
                if g.size >= 2:
                    a, b = res.files[g.members[0]], res.files[g.members[1]]
                    pv = res.edges.get((min(a.index, b.index), max(a.index, b.index)))
                    rec = recommend(a, b, pv)
                    lines.append(f"     advice: keep {res.files[rec.keep].name} / "
                                 f"drop {res.files[rec.drop].name} ({rec.confidence})")
                    for r in rec.reasons[:3]:
                        lines.append(f"       - {r}")
        except Exception as e:
            lines.append(f"FAIL pipeline: {type(e).__name__}: {e}")
            rc = 1

    text = "\n".join(lines)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    try:
        print(text)
    except Exception:
        pass
    return rc


def scan_report(directory: str, out: str | None, threshold: int = 8,
                dihedral: bool = True, detect_near: bool = True,
                use_orb: bool = True, cache: str | None = None,
                limit: int = 0) -> int:
    """扫描 → 报告，不进审阅界面。

    photodedup scan-report <目录> [--threshold 8] [--out 报告目录]

    报告 = Markdown + CSV：每组列出成员与推荐结论（留谁/删谁+理由），
    以及被算法排除的低置信候选。只读扫描，不删任何文件——删除仍走界面的
    逐个确认。几万张的库想先看损失再决定审不审，用这个入口。
    """
    from .core.models import REL_LABEL
    from .core.pipeline import Pipeline, ScanOptions
    from .core.recommend import recommend
    from .core.scanner import human_size

    root = Path(directory)
    if not root.is_dir():
        try:
            print(f"目录不存在：{root}")
        except Exception:
            pass
        return 2
    opt = ScanOptions(root=root, threshold=threshold, dihedral=dihedral,
                      detect_near=detect_near, use_orb=use_orb)
    cache_dir = Path(cache) if cache else (
        Path(os.environ.get("LOCALAPPDATA", ".")) / "PhotoDedup" / "cache")

    t0 = time.time()
    pipe = Pipeline(opt, cache_dir, progress=lambda *a: None)
    res = pipe.run()
    pipe.close()
    elapsed = time.time() - t0

    out_dir = Path(out) if out else root
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    md_path = out_dir / f"去重报告-{stamp}.md"
    csv_path = out_dir / f"去重报告-{stamp}.csv"

    st = res.stats
    lines = [
        "# 图片去重工坊 · 查重报告", "",
        f"- 扫描目录：`{root}`",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 扫描耗时：{elapsed:.1f} 秒（缓存命中 {st.cache_hits}）",
        f"- 文件总数：{st.total_files}，可读图片 {st.images}，跳过 {st.skipped}",
        f"- 重复组：**{st.groups}** 组，重复副本 {st.duplicated} 个，"
        f"理论可省 {human_size(st.wasted_bytes)}",
        "", "本报告由只读扫描生成，没有删除任何文件；删除请进界面逐个确认。", "",
    ]
    shown = res.groups[:limit] if limit else res.groups
    for gi, g in enumerate(shown, 1):
        lines += [f"## 组 {gi}（{g.size} 张，{REL_LABEL.get(g.rel, g.rel)}）", "",
                  "| 文件 | 分辨率 | 体积 | 质量 | MD5 |", "|---|---|---|---|---|"]
        for m in g.members:
            f = res.files[m]
            lines.append(f"| `{f.name}` | {f.width}×{f.height} | {human_size(f.size)} "
                         f"| {f.jpeg_quality or '-'} | {f.md5[:8] or '-'} |")
        if g.size >= 2:
            a, b = res.files[g.members[0]], res.files[g.members[1]]
            pv = res.edges.get((min(a.index, b.index), max(a.index, b.index)))
            rec = recommend(a, b, pv)
            lines += ["", f"**推荐**：保留 `{res.files[rec.keep].name}`，删除 "
                      f"`{res.files[rec.drop].name}`（{rec.confidence}）"]
            lines += [f"- {r}" for r in rec.reasons]
            lines += [f"- ⚠ {w}" for w in rec.warnings]
        lines.append("")

    low = getattr(res, "low_confidence_groups", [])
    if low:
        lines += ["## 低置信候选（被算法排除，未纳入分组）", "",
                  "有几何证据但没通过阈值（疑似连拍/局部重叠），如确信是重复请手动处理。", ""]
        for i, g in enumerate(low, 1):
            names = "、".join(f"`{res.files[m].name}`" for m in g.members[:6])
            more = f" 等 {g.size} 个" if g.size > 6 else ""
            lines.append(f"- 组 {i}（{REL_LABEL.get(g.rel, g.rel)}）：{names}{more}")
        lines.append("")

    import csv as _csv

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["组", "文件", "分辨率", "体积(字节)", "质量", "MD5", "推荐", "关系"])
        for gi, g in enumerate(shown, 1):
            rec_keep = rec_drop = None
            if g.size >= 2:
                a, b = res.files[g.members[0]], res.files[g.members[1]]
                pv = res.edges.get((min(a.index, b.index), max(a.index, b.index)))
                r = recommend(a, b, pv)
                rec_keep, rec_drop = r.keep, r.drop
            for m in g.members:
                f = res.files[m]
                role = ("保留" if rec_keep == m else
                        "删除" if rec_drop == m else "")
                w.writerow([gi, str(f.path), f"{f.width}×{f.height}", f.size,
                            f.jpeg_quality or "", f.md5, role,
                            REL_LABEL.get(g.rel, g.rel)])

    text = "\n".join(lines)
    md_path.write_text(text, encoding="utf-8")
    try:
        print(text)
    except Exception:
        pass
    try:
        print(f"\n报告已写入：{md_path}\nCSV 已写入：{csv_path}")
    except Exception:
        pass
    return 0
