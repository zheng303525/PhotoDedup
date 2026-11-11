"""几何校验层：ORB 特征 + RANSAC 单应性 + 对齐后的差异分布。

这一层存在的理由：pHash 命中 + 几何对齐**都会同时骗过**——
同场景连拍两张，背景完全相同，pHash 可能只差 8 位，ORB 内点率能到 0.99，
但它们是两张不同的照片。唯一的区别是：同一张照片的不同版本，差异是分散的
编码噪声；连拍的差异集中在物体挪走的那一块。

所以第三道判据是「差异是否成块」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .imageio import load_rgb

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

DIFF_THR = 0.6          # RGB 三通道 z 分数最大差超过它的像素算「差异」
MIN_GOOD = 8            # 比率检验后至少需要的匹配数
RATIO_TEST = 0.75

# 判定阈值（2026-11 用正负样本标定过，见 docs/thresholds.md）
DEF_MIN_INLIERS = 12
DEF_MIN_RATIO = 0.30
DEF_MAX_BLOCK = 0.025   # 最大差异连通块占重叠区比例上限
DEF_MAX_DIFF = 0.04     # 总差异占比上限
DEF_MAX_FILL = 0.60     # 最大差异块的外接矩形填充率上限
DEF_MAX_BLOCKS = 6      # 显著差异块数量上限（超过说明多处内容不同）


def _z_in(a: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """只在重叠区域内做逐通道零均值单位方差归一化。

    两个坑：① 不归一化的话，整体调亮 35% 会让 98% 的像素都算「差异」；
    ② 在全图归一化的话，warp 出来的黑边会把统计量拉偏，裁剪变体会被误算成 99%。
    """
    vals = a[mask]
    mu = vals.mean(axis=0)
    sd = vals.std(axis=0)
    sd[sd == 0] = 1.0
    return (a - mu) / sd


def orb_match(ra: np.ndarray, rb: np.ndarray) -> dict[str, Any]:
    """ORB 特征匹配 + RANSAC 求单应矩阵。"""
    if cv2 is None:
        return {"matches": 0, "inliers": 0, "ratio": 0.0, "H": None,
                "low_texture": True, "note": "未安装 opencv"}
    ga = cv2.cvtColor(ra, cv2.COLOR_RGB2GRAY)
    gb = cv2.cvtColor(rb, cv2.COLOR_RGB2GRAY)
    orb = cv2.ORB_create(nfeatures=2000)
    ka, da = orb.detectAndCompute(ga, None)
    kb, db = orb.detectAndCompute(gb, None)
    if da is None or db is None or len(ka) < MIN_GOOD or len(kb) < MIN_GOOD:
        return {"matches": 0, "inliers": 0, "ratio": 0.0, "H": None,
                "low_texture": True, "note": "纹理太少，特征点不足"}

    knn = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [m for m, n in knn if m.distance < RATIO_TEST * n.distance]
    if len(good) < MIN_GOOD:
        return {"matches": len(good), "inliers": 0, "ratio": 0.0, "H": None,
                "low_texture": True, "note": "好匹配不足"}

    src = np.float32([ka[m.queryIdx].pt for m in good])
    dst = np.float32([kb[m.trainIdx].pt for m in good])
    hm, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    inl = int(mask.sum()) if mask is not None else 0
    return {"matches": len(good), "inliers": inl,
            "ratio": round(inl / float(len(good)), 3), "H": hm,
            "low_texture": False, "note": ""}


def region_stats(ra: np.ndarray, rb: np.ndarray, hm: np.ndarray,
                 diff_thr: float = DIFF_THR) -> dict[str, Any] | None:
    """把 B 变换到 A 的坐标后，统计差异的集中度。"""
    if cv2 is None or hm is None:
        return None
    h, w = ra.shape[:2]
    try:
        inv = np.linalg.inv(hm)
    except np.linalg.LinAlgError:
        return None

    aligned = cv2.warpPerspective(rb.astype(np.float32), inv, (w, h),
                                  flags=cv2.INTER_LINEAR)
    valid = cv2.warpPerspective(np.ones(rb.shape[:2], np.float32), inv, (w, h)) > 0.5
    total = float(valid.sum())
    if total < 0.2 * valid.size:
        return None

    za = _z_in(ra.astype(np.float32), valid)
    zb = _z_in(aligned, valid)
    mask = ((np.abs(za - zb).max(axis=2) > diff_thr) & valid).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    raw = float(mask.sum()) / total
    if raw > 0.4:
        # 大面积不一致：翻转/大角度旋转时单应性必然失效，这类结果不可用
        return {"ratio": round(raw, 4), "max_block": 1.0, "fill": 1.0,
                "blocks": 0, "invalid": True}

    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    max_block, fill, blocks = 0.0, 0.0, 0
    if n > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        k = int(np.argmax(areas)) + 1
        max_block = float(areas.max()) / total
        box = float(stats[k, cv2.CC_STAT_WIDTH] * stats[k, cv2.CC_STAT_HEIGHT])
        fill = float(areas.max()) / box if box else 0.0
        # 显著差异块数量：同一张的编码噪声只会散成碎屑，文字/内容改动会形成几十上百个成型块
        blocks = int((areas > total * 0.0005).sum())
    return {"ratio": round(raw, 4), "max_block": round(max_block, 4),
            "fill": round(fill, 3), "blocks": blocks, "invalid": False}


def classify(pd: int, same_dims: bool, geo: dict, region: dict | None,
             min_inliers: int = DEF_MIN_INLIERS, min_ratio: float = DEF_MIN_RATIO,
             max_block: float = DEF_MAX_BLOCK, max_diff: float = DEF_MAX_DIFF,
             max_fill: float = DEF_MAX_FILL,
             max_blocks: int = DEF_MAX_BLOCKS) -> tuple[str, str]:
    """把判据数值翻译成关系类型 + 人话说明。返回 (rel, note)。

    实测标定（见 docs/algorithm.md 第三节）：
    - 重压缩/缩放/小水印：显著差异块数 0，总差异 < 0.1%
    - 调色到高光溢出：块数 4，但最大块 7%（归一化救不回来）
    - 同场景连拍：差异集中在 1 个块，填充率高
    - **两张不同的手机截图**：总差异 2.8%、最大块只有 0.14%、块数 8
    所以「块数」是区分「内容变了」和「编码变了」的关键，光看总差异会和调色混淆。
    """
    from .models import REL_BURST, REL_DIFF, REL_NEAR, REL_WEAK

    aligned = (geo["inliers"] >= min_inliers) and (geo["ratio"] >= min_ratio)
    if geo.get("low_texture"):
        return REL_WEAK, "纹理太少，几何层无法验证"

    if aligned and region and not region.get("invalid"):
        blocks = int(region.get("blocks", 0))
        scattered = blocks >= max_blocks and region["max_block"] <= max_block
        concentrated = (region["ratio"] > max_diff
                        or region["max_block"] > max_block
                        or (region["max_block"] > 0.005 and region["fill"] > max_fill))
        if scattered:
            return REL_BURST, (
                f"有 {blocks} 处大小相近的改动（总差异 {region['ratio'] * 100:.1f}%），"
                f"是画面内容变了，不是同一张的编码差异")
        if concentrated:
            return REL_BURST, (
                f"能对齐，但差异集中在 {region['max_block'] * 100:.1f}% 的块"
                f"（填充率 {region['fill']:.2f}），像是物体挪了位置或大面积改动")
        return REL_NEAR, ("指纹接近 + 几何对齐（内点 %d）+ 差异分散（%d 处）"
                          % (geo["inliers"], blocks))

    if aligned and region and region.get("invalid") and pd <= 2:
        # 单应性对翻转无效，但指纹变体在极近距离命中，说明是同图被镜像/旋转过。
        # 不要求尺寸一致 —— 转 90 度正好会交换宽高。
        return REL_NEAR, "镜像或旋转版本（对齐失效，但指纹变体命中）"

    if aligned:
        return REL_WEAK, "几何能对齐，但重叠区域不足以验证差异分布"

    return REL_DIFF, "指纹接近但无法几何对齐"


def evaluate_pair(pa: Path | str, pb: Path | str, pd: int, same_dims: bool,
                  max_side: int = 1200, **thresholds: float) -> dict[str, Any]:
    """完整跑一遍几何层，返回可写进 PairVerdict 的字段。"""
    ra, _ = load_rgb(pa, max_side=max_side)
    rb, _ = load_rgb(pb, max_side=max_side)
    geo = orb_match(ra, rb)
    region = region_stats(ra, rb, geo["H"])
    rel, note = classify(pd, same_dims, geo, region, **thresholds)
    return {
        "rel": rel,
        "note": note,
        "matches": geo["matches"],
        "inliers": geo["inliers"],
        "ratio": geo["ratio"],
        "diff_ratio": region["ratio"] if region else 0.0,
        "max_block": region["max_block"] if region else 0.0,
        "fill": region["fill"] if region else 0.0,
        "blocks": region.get("blocks", 0) if region else 0,
    }
