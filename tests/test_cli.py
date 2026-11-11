"""CLI 无界面出报告（scan-report）的端到端测试。

修复回归：此前"几万张库想脚本化批量核对"的用户没有入口——
cli.py 只有打包自检，诊断脚本不属于正式入口。
"""

from __future__ import annotations

import csv
from pathlib import Path

from photodedup.cli import scan_report


def test_scan_report_writes_report(sample_dir: Path, tmp_path: Path):
    out = tmp_path / "reports"
    rc = scan_report(str(sample_dir), str(out), threshold=8, dihedral=True,
                     cache=str(tmp_path / "cache"))
    assert rc == 0
    mds = list(out.glob("去重报告-*.md"))
    csvs = list(out.glob("去重报告-*.csv"))
    assert mds and csvs, "报告与 CSV 应写入"
    text = mds[0].read_text(encoding="utf-8")
    assert "查重报告" in text and "重复组" in text
    assert "01_base.jpg" in text, "分组应出现在报告里"
    assert "低置信候选" in text, "被算法排除的连拍对应出现在低置信章节"
    assert "没有删除任何文件" in text, "只读扫描应明确声明没删文件"
    with open(csvs[0], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["组", "文件", "分辨率", "体积(字节)", "质量", "MD5", "推荐", "关系"]
    # 推荐列有值（同一张的不同版本应标出保留/删除）
    roles = {r[6] for r in rows[1:]}
    assert "保留" in roles and "删除" in roles


def test_scan_report_missing_dir(tmp_path: Path):
    rc = scan_report(str(tmp_path / "nope"), None)
    assert rc == 2
