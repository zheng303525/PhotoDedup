"""python -m photodedup 启动界面；scan-report 子命令无界面出报告。"""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "scan-report":
        return _scan_report_cli()
    from .ui.app import main as run

    return run()


def _scan_report_cli() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="photodedup scan-report",
        description="扫描目录查重并出报告（Markdown + CSV），不进审阅界面，不删除任何文件")
    ap.add_argument("dir")
    ap.add_argument("--threshold", type=int, default=8)
    ap.add_argument("--no-dihedral", action="store_true")
    ap.add_argument("--no-near", action="store_true")
    ap.add_argument("--no-orb", action="store_true")
    ap.add_argument("--cache", default=None, help="指纹缓存目录（默认 %%LOCALAPPDATA%%\\PhotoDedup\\cache）")
    ap.add_argument("--out", default=None, help="报告输出目录（默认扫描目录）")
    ap.add_argument("--limit", type=int, default=0, help="最多列多少组（0 = 全部）")
    args = ap.parse_args(sys.argv[2:])

    from .cli import scan_report

    return scan_report(args.dir, args.out, threshold=args.threshold,
                       dihedral=not args.no_dihedral, detect_near=not args.no_near,
                       use_orb=not args.no_orb, cache=args.cache, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
