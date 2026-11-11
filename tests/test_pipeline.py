"""端到端判重测试：合成样本 → 扫描 → 分组 → 推荐。"""

from __future__ import annotations

from pathlib import Path

import pytest

from photodedup.core.models import (
    REL_BURST, REL_EXACT, REL_NEAR, REL_SAME_PIXEL, REL_WEAK,
)
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


def test_burst_pair_shows_as_low_confidence_group(scan):
    """低置信组：被算法排除的连拍对降级展示，不再静默漏检。"""
    from photodedup.core.models import REL_BURST as BURST

    a = _by_name(scan, "11_burst_a.jpg")
    b = _by_name(scan, "11_burst_b.jpg")
    low = scan.low_confidence_groups
    assert any({a, b} <= set(g.members) for g in low), \
        f"连拍对应出现在低置信组里，实际 {[g.members for g in low]}"
    g = next(g for g in low if {a, b} <= set(g.members))
    assert g.rel == BURST
    assert g.pairs, "低置信组应带着判定证据（数值留痕）"
    # 不影响正常分组：连拍仍不进重复组
    assert _group_of(scan, a) is None and _group_of(scan, b) is None
    # 成员全部已在确认组里的组件不展示（weak 边冗余，展示是噪音）
    grouped = {m for grp in scan.groups for m in grp.members}
    for lg in low:
        assert not all(m in grouped for m in lg.members), \
            f"全部已在确认组里的低置信组件是噪音：{lg.members}"


def test_flat_and_unrelated_not_grouped(scan):
    """不同颜色的墙与无关图不该被并组（12_wall_a 有副本，由颜色门测试覆盖）。"""
    for name in ("13_wall_b.jpg", "14_other.jpg"):
        assert _group_of(scan, _by_name(scan, name)) is None, f"{name} 被误判成重复"


def test_flat_duplicate_grouped_by_color_gate(scan):
    """修复：纯色图的重压缩副本（pHash 全同 + ORB 无特征）由颜色差校验层并组。

    这是此前静默漏检的一类：纯色图的 pHash 全同（DC 主导）、ORB 提不到特征，
    唯一可用的证据是 8×8 颜色分布（标定：同一张重压缩差 13.1，不同墙 2688）。
    """
    a = _by_name(scan, "12_wall_a.jpg")
    c = _by_name(scan, "17_wall_a_copy.jpg")
    g = _group_of(scan, c)
    assert g is not None, "纯色重压缩副本应被颜色校验层并组"
    assert a in g.members, "应与原图同组"
    pv = scan.edges.get((min(a, c), max(a, c)))
    assert pv is not None and pv.accepted, f"颜色门并组的边应是 accepted，实际 {pv}"
    assert "颜色" in pv.note


def test_flat_different_colors_stay_low_confidence(scan):
    """两面不同的墙：颜色差 2688 超过候选阈值 256，算法正确地不作任何判定——

    它们本来就不是重复：既不并组、也不进待查（颜色门的候选阈值与判定阈值
    一致，能通过门的一定是候选，通不过的连候选都不是）。
    """
    a = _by_name(scan, "12_wall_a.jpg")
    b = _by_name(scan, "13_wall_b.jpg")
    assert _group_of(scan, b) is None, "两面不同的墙不该被并组"
    key = (min(a, b), max(a, b))
    assert key not in scan.edges, "不同颜色的纯色对不该被评估（颜色差超阈值）"
    assert all(not {a, b} <= set(g.members) for g in scan.low_confidence_groups), \
        "不同颜色的纯色对不该出现在低置信组里"


class TestColorGateWithRealNumbers:
    """用真实样本标定的颜色差数值做回归（同一张重压缩 13.1 / 不同墙 2688.4）。"""

    @staticmethod
    def _geo(flat: bool, diff: float) -> dict:
        return {"inliers": 0, "ratio": 0.0, "matches": 0,
                "low_texture": True, "note": "",
                "color": {"flat": flat, "diff": diff}}

    def test_flat_same_colors_near(self):
        from photodedup.core.verify import classify

        rel, note = classify(0, True, self._geo(True, 13.1), None)
        assert rel == REL_NEAR
        assert "颜色" in note

    def test_flat_different_colors_weak(self):
        from photodedup.core.verify import classify

        rel, _ = classify(0, True, self._geo(True, 2688.4), None)
        assert rel == REL_WEAK, "两面不同的墙不该被颜色门并组"

    def test_textured_pair_not_color_gated(self):
        from photodedup.core.verify import classify

        rel, _ = classify(0, True, self._geo(False, 13.1), None)
        assert rel == REL_WEAK, "有一方有纹理时颜色门不应放行，交给几何层/待查"


def test_same_layout_different_content_not_grouped(scan):
    """同版式不同文字的两张截图不能被并进重复组。"""
    a = _by_name(scan, "15_screen_a.png")
    b = _by_name(scan, "16_screen_b.png")
    assert _group_of(scan, a) is None, "同版式不同内容的截图被并进重复组了"
    assert _group_of(scan, b) is None


class TestClassifyWithRealNumbers:
    """用 928 张真实截图实测出来的判据数值做回归。

    这组数字是本项目最重要的一条经验：真实副本（重压缩/缩放/小水印）的
    「显著差异块数」是 0，而两张不同截图是 8 —— 光看总差异会和「调了亮度」混淆。
    """

    @staticmethod
    def _geo(inliers: int, ratio: float) -> dict:
        return {"inliers": inliers, "ratio": ratio, "matches": inliers,
                "low_texture": False, "note": ""}

    def test_true_duplicate_reencode(self):
        from photodedup.core.verify import classify

        # 同一张截图另存为 q60：块数 0，差异 0.00%
        region = {"ratio": 0.0, "max_block": 0.0, "fill": 0.0,
                  "blocks": 0, "invalid": False}
        rel, _ = classify(0, True, self._geo(1868, 0.996), region)
        assert rel == REL_NEAR

    def test_true_duplicate_small_watermark(self):
        from photodedup.core.verify import classify

        region = {"ratio": 0.0, "max_block": 0.0, "fill": 0.0,
                  "blocks": 0, "invalid": False}
        rel, _ = classify(0, True, self._geo(1915, 0.997), region)
        assert rel == REL_NEAR

    def test_two_different_screenshots_rejected(self):
        from photodedup.core.verify import classify

        # 相隔 5 分钟的两张不同手机截图：总差异 2.81%、最大块仅 0.14%、块数 8
        region = {"ratio": 0.0281, "max_block": 0.0014, "fill": 0.92,
                  "blocks": 8, "invalid": False}
        rel, note = classify(8, True, self._geo(464, 0.784), region)
        assert rel != REL_NEAR, f"不同截图被判成 {rel}"
        assert "处" in note

    def test_burst_rejected(self):
        from photodedup.core.verify import classify

        # 同场景连拍：差异集中在 2.86% 的一块，填充率 0.79
        region = {"ratio": 0.0571, "max_block": 0.0286, "fill": 0.788,
                  "blocks": 1, "invalid": False}
        rel, _ = classify(4, True, self._geo(880, 0.981), region)
        assert rel == REL_BURST


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
