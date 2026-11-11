"""一对一的判据探针：打印全部数值，用来标定阈值。

    python scripts/probe_pair.py A.jpg B.jpg
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from photodedup.core.hashing import hamming  # noqa: E402
from photodedup.core.imageio import load_rgb  # noqa: E402
from photodedup.core.verify import evaluate_pair  # noqa: E402
from photodedup.core.hashing import fingerprints  # noqa: E402


def probe(a: Path, b: Path) -> None:
    ra, _ = load_rgb(a)
    rb, _ = load_rgb(b)
    fa, fb = fingerprints(ra), fingerprints(rb)
    pd = min(hamming(u, v) for u in [fa["phash"]] + fa["pvars"]
             for v in [fb["phash"]] + fb["pvars"])
    same_dims = ra.shape[:2] == rb.shape[:2]
    r = evaluate_pair(a, b, pd, same_dims)
    print(f"{a.name}  <>  {b.name}")
    print(f"  pHash 距离 {pd}/64   同尺寸={same_dims}")
    print(f"  内点 {r['inliers']}（率 {r['ratio']}）  匹配 {r['matches']}")
    print(f"  总差异 {r['diff_ratio'] * 100:.2f}%   最大块 {r['max_block'] * 100:.2f}%"
          f"   填充率 {r['fill']:.2f}   显著块数 {r.get('blocks', -1)}")
    print(f"  结论：{r['rel']}  —— {r['note']}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)
    probe(Path(sys.argv[1]), Path(sys.argv[2]))
