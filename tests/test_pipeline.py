"""端到端判重测试：合成样本 → 扫描 → 分组 → 推荐。"""

from __future__ import annotations

from pathlib import Path

import pytest

from photodedup.core.models import REL_BURST, REL_EXACT, REL_NEAR, REL_SAME_PIXEL
from photodedup.core.pipeline import Pipeline, ScanOptions
from photodedup.core.recommend import recommend


@pytest.fixture(scope="module")
def scan(sample_dir: Path, tmp_path_factory):
    cache = tmp_path_factory.mktemp("cache")
    opt = ScanOptions(root=sample_dir, threshold=8, dihedral=True)
    pipe = Pipeline(opt, cache)
    res = pipe.run()
    pipe.close()
    return res


def _by_name(res, name: str) -> int:
    for f in res.files:
        if f.name == name:
            return f.index
    raise AssertionError(f"样本里没有 {name}")


def _group_of(res, idx: int):
    for g in res.groups:
        if idx in g.members:
            return g
    return None


def _names(res, members) -> set[str]:
    return {res.files[m].name for m in members}


def test_same_photo_versions_grouped(scan):
    """同一张照片的各种版本应聚成一组。"""
    g = _group_of(scan, _by_name(scan, "01_base.jpg"))
    assert g is not None, "原图没有进入任何重复组"
    got = _names(scan, g.members)
    expected = {
        "01_base.jpg", "02_exact.jpg", "03_samepixel.jpg", "04_scaled.jpg",
        "05_recompress.jpg", "06_brighter.jpg", "07_watermark.jpg",
        "08_rot90.jpg", "09_mirror.jpg",
    }
    assert expected <= got, f"缺少成员：{expected - got}"


def test_burst_not_grouped(scan):
    """同场景连拍是两张不同照片，绝不能判成重复。"""
    a = _by_name(scan, "11_burst_a.jpg")
    b = _by_name(scan, "11_burst_b.jpg")
    assert _group_of(scan, a) is None
    assert _group_of(scan, b) is None
    pv = scan.edges.get((min(a, b), max(a, b)))
    assert pv is not None, "连拍对应该被评估过，只是结论不同"
    assert pv.rel == REL_BURST, f"连拍被判成了 {pv.rel}"


def test_flat_and_unrelated_not_grouped(scan):
    for name in ("12_wall_a.jpg", "13_wall_b.jpg", "14_other.jpg"):
        assert _group_of(scan, _by_name(scan, name)) is None, f"{name} 被误判成重复"


def test_crop_outside_default_threshold(scan):
    """已知限制：大幅裁剪让 pHash 距离超出阈值，默认抓不到。允许漏，不允许错。"""
    g = _group_of(scan, _by_name(scan, "10_crop.jpg"))
    assert g is None, "裁剪版当前版本不应该进组（若已改进算法，请同步更新文档）"


def test_exact_and_same_pixel_verdicts(scan):
    a, b = _by_name(scan, "01_base.jpg"), _by_name(scan, "02_exact.jpg")
    assert scan.edges[(min(a, b), max(a, b))].rel == REL_EXACT
    c = _by_name(scan, "03_samepixel.jpg")
    pv = scan.edges[(min(a, c), max(a, c))]
    assert pv.rel == REL_SAME_PIXEL, f"像素相同的两份被判成了 {pv.rel}"


def test_recommend_prefers_bigger(scan):
    a = scan.files[_by_name(scan, "01_base.jpg")]
    b = scan.files[_by_name(scan, "04_scaled.jpg")]
    pv = scan.edges[(min(a.index, b.index), max(a.index, b.index))]
    rec = recommend(a, b, pv)
    assert rec.keep == a.index, "应保留分辨率高的那张"
    assert rec.confidence in ("较高", "可参考")
    assert any("像素" in r for r in rec.reasons)


def test_recommend_exact_is_certain(scan):
    a = scan.files[_by_name(scan, "01_base.jpg")]
    b = scan.files[_by_name(scan, "02_exact.jpg")]
    pv = scan.edges[(min(a.index, b.index), max(a.index, b.index))]
    rec = recommend(a, b, pv)
    assert rec.confidence == "确定"
    assert any("MD5" in r or "字节" in r for r in rec.reasons)


def test_recommend_flags_rotation(scan):
    a = scan.files[_by_name(scan, "01_base.jpg")]
    b = scan.files[_by_name(scan, "08_rot90.jpg")]
    pv = scan.edges[(min(a.index, b.index), max(a.index, b.index))]
    rec = recommend(a, b, pv)
    assert rec.confidence not in ("确定", "较高"), "旋转版本不该给到可一键删除的置信度"
    assert any("旋转" in w for w in rec.warnings), rec.warnings


def test_near_pair_always_warns(scan):
    a = scan.files[_by_name(scan, "01_base.jpg")]
    b = scan.files[_by_name(scan, "05_recompress.jpg")]
    pv = scan.edges[(min(a.index, b.index), max(a.index, b.index))]
    assert pv.rel == REL_NEAR
    rec = recommend(a, b, pv)
    assert any("不同版本" in w for w in rec.warnings)
