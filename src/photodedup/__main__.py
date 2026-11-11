"""python -m photodedup 启动界面。"""

from __future__ import annotations

import sys


def main() -> int:
    from .ui.app import main as run

    return run()


if __name__ == "__main__":
    sys.exit(main())
