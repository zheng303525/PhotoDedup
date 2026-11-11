"""一条命令打包：PyInstaller 出单文件 exe，然后改名成中文名，顺手跑自检。

    .venv\\Scripts\\python scripts\\build.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "dist" / "PhotoDedup.exe"
FINAL = ROOT / "dist" / "图片去重工坊.exe"


def main() -> int:
    pyi = Path(sys.executable).with_name("pyinstaller.exe")
    if not pyi.exists():
        print(f"找不到 {pyi}，先 pip install -r requirements-dev.txt")
        return 1

    print("打包中……")
    cp = subprocess.run([str(pyi), str(ROOT / "PhotoDedup.spec"), "--noconfirm",
                         "--log-level", "WARN"], cwd=ROOT)
    if cp.returncode != 0:
        print("打包失败")
        return cp.returncode

    if not RAW.exists():
        print(f"没找到产物 {RAW}")
        return 1
    shutil.move(str(RAW), str(FINAL))
    print(f"产物：{FINAL}（{FINAL.stat().st_size / 1048576:.0f} MB）")

    print("自检中……")
    out = ROOT / "_selftest.txt"
    cp = subprocess.run([str(FINAL), "--selftest", "--out", str(out)])
    text = out.read_text(encoding="utf-8") if out.exists() else "(无输出)"
    print(text)
    out.unlink(missing_ok=True)
    if "FAIL" in text:
        print("自检有问题，看上面 FAIL 行")
        return 1
    return cp.returncode


if __name__ == "__main__":
    raise SystemExit(main())
