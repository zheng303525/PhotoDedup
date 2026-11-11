"""开发用诊断脚本：不启动界面，直接扫描目录并打印分组与推荐结论。

    python scripts/diag_scan.py <目录> [--threshold 8] [--no-dihedral]

用来在不碰界面的情况下验证算法、标定阈值、排查误判。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from photodedup.core.models import REL_LABEL  # noqa: E402
from photodedup.core.pipeline import Pipeline, ScanOptions  # noqa: E402
from photodedup.core.recommend import recommend  # noqa: E402
from photodedup.core.scanner import human_size  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--threshold", type=int, default=8)
    ap.add_argument("--no-dihedral", action="store_true")
    ap.add_argument("--no-near", action="store_true")
    ap.add_argument("--cache", default=str(Path.home() / ".photodedup" / "cache"))
    ap.add_argument("--limit", type=int, default=40, help="最多打印多少组")
    args = ap.parse_args()

    opt = ScanOptions(root=Path(args.dir), threshold=args.threshold,
                      dihedral=not args.no_dihedral,
                      detect_near=not args.no_near)
    last = [""]

    def progress(stage: str, done: int, total: int, text: str) -> None:
        line = f"\r[{stage}] {done}/{total} {text[:40]:<40}"
        if line != last[0]:
            sys.stdout.write(line)
            sys.stdout.flush()
            last[0] = line

    t0 = time.time()
    pipe = Pipeline(opt, args.cache, progress=progress)
    res = pipe.run()
    pipe.close()
    print()

    st = res.stats
    print(f"\n目录：{res.root}")
    print(f"耗时 {time.time() - t0:.1f}s  文件 {st.total_files}  可读图片 {st.images}  "
          f"跳过 {st.skipped}  缓存命中 {st.cache_hits}")
    print(f"重复组 {st.groups}  重复副本 {st.duplicated}  理论可省 {human_size(st.wasted_bytes)}")
    for w in res.warnings:
        print(f"警告：{w}")

    for gi, g in enumerate(res.groups[: args.limit], 1):
        print(f"\n组 {gi}（{g.size} 张，{REL_LABEL.get(g.rel, g.rel)}）")
        for m in g.members:
            f = res.files[m]
            print(f"    {f.name:<26} {f.width}x{f.height}  {human_size(f.size):>9}  "
                  f"q={f.jpeg_quality}  锐度={f.sharpness:6.1f}  "
                  f"md5={f.md5[:8] or '-'}")
        if g.size >= 2:
            pv = None
            key = (min(g.members[0], g.members[1]), max(g.members[0], g.members[1]))
            pv = res.edges.get(key)
            rec = recommend(res.files[g.members[0]], res.files[g.members[1]], pv)
            print(f"    推荐：留 {res.files[rec.keep].name} / 删 "
                  f"{res.files[rec.drop].name}（{rec.confidence}）")
            for r in rec.reasons:
                print(f"      · {r}")
            for w in rec.warnings:
                print(f"      ! {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
