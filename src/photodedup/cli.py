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
