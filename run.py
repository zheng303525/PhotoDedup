"""PyInstaller 打包入口（等价于 python -m photodedup）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def _parse_selftest(argv: list[str]) -> tuple[str | None, str | None] | None:
    """--selftest [--dir X] [--out Y]，无界面跑一遍内核。"""
    if "--selftest" not in argv:
        return None
    d = out = None
    for i, a in enumerate(argv):
        if a == "--dir" and i + 1 < len(argv):
            d = argv[i + 1]
        elif a == "--out" and i + 1 < len(argv):
            out = argv[i + 1]
    return d, out


if __name__ == "__main__":
    st = _parse_selftest(sys.argv[1:])
    if st is not None:
        from photodedup.cli import selftest

        sys.exit(selftest(*st))

    from photodedup.ui.app import main

    sys.exit(main())
