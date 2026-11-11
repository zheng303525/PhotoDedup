"""数据模型：单张图片的指纹、一对图片的判定结论、一个重复组。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 一对图片的关系类型，按可信度从高到低
REL_EXACT = "exact"          # 字节完全一致（MD5 相同）—— 可定论
REL_SAME_PIXEL = "same_pixel"  # 解码后像素一致 —— 可定论
REL_NEAR = "near"            # 三证据通过的感知近重 —— 只给候选
REL_BURST = "burst"          # 能对齐但差异成块：同场景连拍
REL_WEAK = "weak"            # 局部重叠或指纹边缘
REL_DIFF = "different"       # 判定为不同

REL_LABEL = {
    REL_EXACT: "字节完全一致",
    REL_SAME_PIXEL: "像素完全一致",
    REL_NEAR: "感知近重（同一张的不同版本）",
    REL_BURST: "同场景不同张（疑似连拍）",
    REL_WEAK: "仅局部重叠 / 不确定",
    REL_DIFF: "不同",
}

# 这些关系可以给出「确定」结论，也是唯一允许说「删哪张都不损失」的
DECIDABLE = {REL_EXACT, REL_SAME_PIXEL}


@dataclass(slots=True)
class ImageInfo:
    """单张图片的全部可用信息，指纹阶段算好，界面只读取不重算。"""

    index: int
    path: Path
    size: int
    mtime: float
    width: int = 0
    height: int = 0
    fmt: str = ""
    mode: str = ""
    md5: str = ""
    phash: int = 0
    dhash: int = 0
    pvars: list[int] = field(default_factory=list)
    sharpness: float = 0.0
    jpeg_quality: int | None = None
    make: str = ""
    model: str = ""
    shot_time: str = ""
    gps: bool = False
    gps_zero: bool = False
    has_icc: bool = False
    exif_score: int = 0
    error: str = ""

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def ext(self) -> str:
        return self.path.suffix.lower().lstrip(".")

    @property
    def ok(self) -> bool:
        return not self.error and self.width > 0

    def rel(self, root: Path) -> str:
        try:
            return str(self.path.relative_to(root))
        except ValueError:
            return str(self.path)

    def to_cache(self) -> dict[str, Any]:
        """给 sqlite 缓存用的紧凑字典（路径不进缓存，作为主键单独存）。"""
        return {
            "size": self.size,
            "mtime": self.mtime,
            "width": self.width,
            "height": self.height,
            "fmt": self.fmt,
            "mode": self.mode,
            "md5": self.md5,
            "phash": self.phash,
            "dhash": self.dhash,
            "pvars": self.pvars,
            "sharpness": self.sharpness,
            "jpeg_quality": self.jpeg_quality,
            "make": self.make,
            "model": self.model,
            "shot_time": self.shot_time,
            "gps": self.gps,
            "gps_zero": self.gps_zero,
            "has_icc": self.has_icc,
            "exif_score": self.exif_score,
            "error": self.error,
        }

    def load_cache(self, d: dict[str, Any]) -> None:
        for k, v in d.items():
            if hasattr(self, k):
                setattr(self, k, v)

    @classmethod
    def from_cache(cls, index: int, path: Path, d: dict[str, Any]) -> "ImageInfo":
        obj = cls(index=index, path=path, size=int(d.get("size", 0)),
                  mtime=float(d.get("mtime", 0.0)))
        obj.load_cache(d)
        return obj

    def dumps(self) -> str:
        return json.dumps(self.to_cache(), ensure_ascii=False)


@dataclass(slots=True)
class PairVerdict:
    """一对图片的判定过程与结论，所有判据数值都留着，方便界面展示与排查。"""

    a: int
    b: int
    rel: str = REL_DIFF
    pd: int = 64          # pHash 最小汉明距离
    dd: int = 64          # dHash 最小汉明距离
    inliers: int = 0      # RANSAC 内点数
    ratio: float = 0.0    # 内点率
    matches: int = 0      # 通过比率检验的好匹配数
    diff_ratio: float = 0.0   # 对齐后总差异占比
    max_block: float = 0.0    # 最大差异连通块占比
    fill: float = 0.0         # 最大块的填充率
    note: str = ""

    @property
    def accepted(self) -> bool:
        return self.rel in (REL_EXACT, REL_SAME_PIXEL, REL_NEAR)

    @property
    def decidable(self) -> bool:
        return self.rel in DECIDABLE


@dataclass(slots=True)
class Group:
    """一组互为重复的图片（同一张照片的多个副本/版本）。"""

    members: list[int]
    rel: str = REL_NEAR      # 组内最弱的一环
    pairs: list[PairVerdict] = field(default_factory=list)
    dismissed: set[int] = field(default_factory=set)  # 用户选过「都留」的成员

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass(slots=True)
class ScanStats:
    total_files: int = 0
    images: int = 0
    skipped: int = 0
    groups: int = 0
    duplicated: int = 0
    wasted_bytes: int = 0
    elapsed: float = 0.0
    cancelled: bool = False
    cache_hits: int = 0
    errors: list[str] = field(default_factory=list)
