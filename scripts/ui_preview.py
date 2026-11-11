"""离屏渲染各页面截图，用来检查排版（不弹窗、不动真实文件）。

    python scripts/ui_preview.py --dir <样本目录> --out _shots
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from photodedup.ui import theme  # noqa: E402
from photodedup.ui.main_window import MainWindow  # noqa: E402


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def wait_for(app: QApplication, pred, timeout: float = 60.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default="_shots")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    args = ap.parse_args()

    src = Path(args.dir)
    work = Path(args.out) / "_work"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(src, work)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    from photodedup import APP_NAME  # noqa: F401

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.QSS)

    win = MainWindow()
    win.resize(args.width, args.height)
    win.show()
    pump(app, 0.4)
    win.grab().save(str(out / "01_start.png"))

    win.start_page.ed_dir.setText(str(work))
    win.start_scan({"root": work, "threshold": 8, "detect_near": True,
                    "dihedral": True, "mode": "staging", "allow_unsure": False})
    pump(app, 0.3)
    win.grab().save(str(out / "02_progress.png"))

    ok = wait_for(app, lambda: win.stack.currentWidget() is win.compare_page, 120)
    if not ok:
        print("扫描没有完成或没有重复组")
        win.grab().save(str(out / "03_failed.png"))
        return 1
    pump(app, 0.5)
    win.grab().save(str(out / "03_compare_side.png"))

    win.compare_page.cmb_mode.setCurrentIndex(1)
    pump(app, 0.4)
    win.grab().save(str(out / "04_compare_overlay.png"))

    win.compare_page.cmb_mode.setCurrentIndex(2)
    pump(app, 0.4)
    win.grab().save(str(out / "05_compare_diff.png"))

    win.compare_page.cmb_mode.setCurrentIndex(0)
    pump(app, 0.2)
    # 删掉右侧挑战者，看下一对
    win.compare_page.delete_side(1)
    pump(app, 0.4)
    win.grab().save(str(out / "06_after_delete.png"))

    win.compare_page.undo()
    pump(app, 0.4)
    win.grab().save(str(out / "07_after_undo.png"))

    win.finish()
    pump(app, 0.5)
    win.grab().save(str(out / "08_finish.png"))
    print("截图输出到", out.resolve())
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
