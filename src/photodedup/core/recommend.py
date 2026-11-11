"""推荐引擎：给出「保留哪张、删哪张、为什么」。

打分维度按「删错了会心疼什么」排序：分辨率 > 锐度 > 压缩质量 > 体积 >
元数据 > 拍摄时间 > 文件名。每个维度只在两张图确实不同时才给分并生成理由，
避免理由里出现没有信息量的废话。

三条硬规则：
1. 只有字节层/像素层能说「删哪张都不损失」，感知层必须提示差异在哪
2. 得分差距小于 6 分时，明确告诉用户「两者接近，请自己看」
3. 裁剪、镜像这类构图不同的版本，额外给出警告而不是悄悄推荐
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import REL_EXACT, REL_NEAR, REL_SAME_PIXEL, ImageInfo, PairVerdict
from .scanner import human_size

W_PIXELS = 35
W_SHARP = 20
W_QUALITY = 15
W_SIZE = 10
W_EXIF = 10
W_TIME = 5
W_NAME = 5

COPY_PAT = re.compile(
    r"(副本|拷贝|\(\d+\)|（\d+）|-\s*copy|_copy|\bcopy\b|微信图片|"
    r"_hdr|_edit|_edited|edited|export|导出|转存|saved|~1)",
    re.IGNORECASE,
)
COPY_DIR_PAT = re.compile(r"(备份|backup|副本|微信|wechat|download|下载|转发|临时|temp)",
                          re.IGNORECASE)


@dataclass
class Recommendation:
    keep: int
    drop: int
    score_keep: float = 0.0
    score_drop: float = 0.0
    confidence: str = "接近"          # 确定 / 较高 / 可参考 / 接近
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def diff(self) -> float:
        return abs(self.score_keep - self.score_drop)

    @property
    def confident(self) -> bool:
        """够不够自信到「按 Delete 就删」。"""
        return self.confidence in ("确定", "较高")


def _mp(i: ImageInfo) -> float:
    return i.pixels / 1_000_000.0


def _dims(i: ImageInfo) -> str:
    return f"{i.width}×{i.height}"


def _exif_detail(i: ImageInfo) -> str:
    bits = []
    if i.model or i.make:
        bits.append(f"{i.make} {i.model}".strip())
    if i.shot_time:
        bits.append(i.shot_time)
    return " · ".join(bits)


def _copy_marker(i: ImageInfo) -> bool:
    return bool(COPY_PAT.search(i.name)) or bool(COPY_DIR_PAT.search(str(i.path.parent)))


def _tiebreak_key(i: ImageInfo) -> tuple:
    """完全打平时的排序键，越小越该保留。

    顺序：不是副本目录 → 元数据更完整 → 层级更浅 → 时间更早 → 体积更小。
    最后一项用「更小」而不是「更大」：像素已经相同，多出来的字节通常是
    尾部附加数据或垃圾，不是画质。
    """
    return (1 if _copy_marker(i) else 0, -i.exif_score,
            len(i.path.parts), i.mtime, i.size)


def _decidable_reasons(a: ImageInfo, b: ImageInfo, rel: str,
                       rec: Recommendation) -> None:
    """字节/像素层的定论式推荐。"""
    if rel == REL_EXACT:
        rec.reasons.append("两张文件的 MD5 完全相同，字节级一致，删哪张都不损失画质")
        rec.confidence = "确定"
    else:
        rec.reasons.append("解码后像素逐字节相同，只是文件封装与元数据不同，删哪张都不损失画质")
        rec.confidence = "确定"

    ka = _tiebreak_key(a)
    kb = _tiebreak_key(b)
    keep_is_a = ka <= kb
    k, d = (a, b) if keep_is_a else (b, a)
    rec.keep, rec.drop = k.index, d.index

    if _copy_marker(d) and not _copy_marker(k):
        rec.reasons.append("保留的这份路径里没有「副本/备份/微信」这类标记")
    if len(k.path.parts) < len(d.path.parts):
        rec.reasons.append(f"保留的这份目录层级更浅（{len(k.path.parts)} 层 vs "
                           f"{len(d.path.parts)} 层），更好找")
    if k.exif_score > d.exif_score:
        detail = _exif_detail(k)
        rec.reasons.append(f"保留的这份元数据更完整（{detail or 'EXIF 齐全'}）")
    if k.size != d.size and a.pixels == b.pixels:
        ks, ds = human_size(k.size), human_size(d.size)
        if ks == ds:  # 差距太小，KB 级显示不出区别就改用字节
            ks, ds = f"{k.size:,} 字节", f"{d.size:,} 字节"
        rec.reasons.append(
            f"两份像素相同，但保留的这份体积更小（{ks} vs {ds}），"
            f"另一份多了些封装或尾部数据")
    if abs(k.mtime - d.mtime) > 86400 * 30:
        rec.reasons.append("保留的这份修改时间更早，更像原件")
    if len(rec.reasons) == 1:
        rec.reasons.append("两份内容完全相同，按目录习惯保留先出现的那份")
    rec.score_keep, rec.score_drop = 100.0, 0.0


def recommend(a: ImageInfo, b: ImageInfo, pv: PairVerdict | None = None) -> Recommendation:
    """对一对图片给出推荐。a 与 b 是候选，返回保留谁、删谁、为什么。"""
    rel = pv.rel if pv else REL_NEAR
    sa = sb = 0.0
    reasons: list[str] = []
    warnings: list[str] = []

    rec = Recommendation(keep=a.index, drop=b.index)
    if rel in (REL_EXACT, REL_SAME_PIXEL):
        _decidable_reasons(a, b, rel, rec)
        return rec

    # ---------- 分辨率 ----------
    if a.pixels != b.pixels:
        big, small = (a, b) if a.pixels > b.pixels else (b, a)
        ratio = big.pixels / max(1, small.pixels)
        if ratio >= 1.02:
            add = W_PIXELS if ratio >= 1.3 else W_PIXELS * 0.5
            if big is a:
                sa += add
            else:
                sb += add
            reasons.append(
                f"{_dims(big)}（{_mp(big):.1f} MP）比 {_dims(small)}（{_mp(small):.1f} MP）"
                f"多 {ratio:.1f} 倍像素，放大和二次裁剪空间更大")

    # ---------- 锐度 ----------
    if a.sharpness > 5 and b.sharpness > 5:
        hi, lo = (a, b) if a.sharpness > b.sharpness else (b, a)
        sharp_ratio = hi.sharpness / max(1e-6, lo.sharpness)
        if sharp_ratio >= 1.25:
            weight = W_SHARP
            if max(a.pixels, b.pixels) / max(1, min(a.pixels, b.pixels)) > 2:
                weight = W_SHARP * 0.4  # 分辨率差太大时，锐度参考性下降
            if hi is a:
                sa += weight
            else:
                sb += weight
            reasons.append(
                f"细节锐度 {a.sharpness:.0f} vs {b.sharpness:.0f}"
                f"（拉普拉斯方差，低的那张有过度压缩或缩小痕迹）")

    # ---------- 压缩质量 ----------
    qa, qb = a.jpeg_quality, b.jpeg_quality
    if qa and qb and abs(qa - qb) >= 5:
        hi, lo = (a, b) if qa > qb else (b, a)
        if hi is a:
            sa += W_QUALITY
        else:
            sb += W_QUALITY
        reasons.append(f"JPEG 质量估计 {qa} vs {qb}（从量化表反推）")
    elif {a.ext, b.ext} & {"png", "tif", "tiff", "bmp"}:
        lossless = [x for x in (a, b) if x.ext in {"png", "tif", "tiff", "bmp"}]
        if len(lossless) == 1:
            if lossless[0] is a:
                sa += W_QUALITY * 0.6
            else:
                sb += W_QUALITY * 0.6
            reasons.append(
                f"其中一张是 {lossless[0].ext.upper()} 无损格式，另一张是 JPEG 有损压缩")

    # ---------- 同分辨率下的体积 ----------
    if a.pixels == b.pixels and a.size != b.size and a.ext == b.ext:
        big, small = (a, b) if a.size > b.size else (b, a)
        if big.size >= small.size * 1.05:
            if big is a:
                sa += W_SIZE
            else:
                sb += W_SIZE
            reasons.append(
                f"同分辨率下体积 {human_size(a.size)} vs {human_size(b.size)}，"
                f"小的那份压缩更狠")

    # ---------- 元数据完整度 ----------
    if abs(a.exif_score - b.exif_score) >= 1:
        rich, poor = (a, b) if a.exif_score > b.exif_score else (b, a)
        if rich is a:
            sa += W_EXIF
        else:
            sb += W_EXIF
        detail = _exif_detail(rich)
        text = f"保留的这份元数据完整（{detail}）" if detail else "保留的这份元数据更完整"
        missing = []
        if not poor.shot_time:
            missing.append("无拍摄时间")
        if not (poor.make or poor.model):
            missing.append("无机型")
        if poor.gps_zero:
            missing.append("GPS 被清零")
        if missing:
            text += "，另一张" + "、".join(missing) + "，是转发/导出副本的典型特征"
        reasons.append(text)

    # ---------- 拍摄时间 ----------
    if a.shot_time and b.shot_time and a.shot_time != b.shot_time:
        early = min(a, b, key=lambda x: x.shot_time)
        if early is a:
            sa += W_TIME
        else:
            sb += W_TIME
        reasons.append(f"拍摄时间 {a.shot_time} vs {b.shot_time}，早的那份更像原件")

    # ---------- 文件名 ----------
    ma, mb = _copy_marker(a), _copy_marker(b)
    if ma != mb:
        clean, dirty = (b, a) if ma else (a, b)
        if clean is a:
            sa += W_NAME
        else:
            sb += W_NAME
        reasons.append(f"「{dirty.name}」带有副本/编辑标记，保留文件名干净的那份")

    # ---------- 构图差异警告 ----------
    if a.width and b.width:
        if a.width == b.height and a.height == b.width:
            warnings.append(
                f"其中一张被旋转了 90°（{_dims(a)} vs {_dims(b)}），画面内容相同，"
                f"按你习惯的查看方向留一份就行")
        else:
            ar_a = a.width / a.height
            ar_b = b.width / b.height
            if abs(ar_a - ar_b) / max(ar_a, ar_b) > 0.03:
                warnings.append(
                    f"两张的宽高比不同（{_dims(a)} vs {_dims(b)}），其中一张是裁剪过的构图，"
                    f"如果这个构图对你有意义，别按推荐删")

    if pv and pv.rel == REL_NEAR:
        warnings.append(
            f"像素并不相同，是不同版本（感知距离 {pv.pd}/64），删除前请扫一眼画面上没有你需要的差异")

    rec.score_keep, rec.score_drop = sa, sb
    rec.warnings = warnings

    if sa == sb:
        # 全平：把实测值列出来，让用户知道「判不出优劣」是有依据的，不是没算
        measured = []
        if a.sharpness and b.sharpness:
            measured.append(f"锐度 {a.sharpness:.0f} / {b.sharpness:.0f}")
        if a.jpeg_quality and b.jpeg_quality:
            measured.append(f"JPEG 质量 {a.jpeg_quality} / {b.jpeg_quality}")
        measured.append(f"体积 {human_size(a.size)} / {human_size(b.size)}")
        rec.reasons = [
            "各项指标差异都在容差内（" + "，".join(measured) + "），两张画质基本一样",
        ]
        keep = min(a, b, key=_tiebreak_key)
        rec.keep = keep.index
        rec.drop = (b if keep is a else a).index
        rec.confidence = "接近"
        drop = b if keep is a else a
        if _copy_marker(drop) and not _copy_marker(keep):
            rec.reasons.append("先删路径里带「副本/备份/微信」标记的那份")
        elif len(keep.path.parts) < len(drop.path.parts):
            rec.reasons.append("按目录层级，保留更浅的那份")
        else:
            rec.reasons.append("仅按文件时间与路径排序，留先出现的这份")
    else:
        keep = a if sa > sb else b
        rec.keep = keep.index
        rec.drop = (b if keep is a else a).index
        diff = abs(sa - sb)
        rec.reasons = reasons
        if diff >= 15:
            rec.confidence = "较高"
        elif diff >= 6:
            rec.confidence = "可参考"
        else:
            rec.confidence = "接近"
    return rec
