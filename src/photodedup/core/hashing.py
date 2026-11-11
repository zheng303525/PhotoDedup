"""哈希层：文件 MD5、感知哈希（pHash / dHash）、旋转镜像变体、汉明距离。

设计要点：
- 全部用 64 位整数表示，便于位运算与快速比对
- pHash 取 DCT 低频 8x8，dHash 取水平梯度，两者互补：前者对亮度/对比度稳，
  后者对结构边缘敏感
- 提供 8 种二面体变体（旋转 + 镜像），解决「照片被转过 90 度」这类同图
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

_POPCNT = bytes(bin(i).count("1") for i in range(256))
_CHUNK = 1 << 20


def hamming(a: int, b: int) -> int:
    """64 位汉明距离。"""
    x = (a ^ b) & 0xFFFFFFFFFFFFFFFF
    return (_POPCNT[x & 0xFF] + _POPCNT[(x >> 8) & 0xFF]
            + _POPCNT[(x >> 16) & 0xFF] + _POPCNT[(x >> 24) & 0xFF]
            + _POPCNT[(x >> 32) & 0xFF] + _POPCNT[(x >> 40) & 0xFF]
            + _POPCNT[(x >> 48) & 0xFF] + _POPCNT[(x >> 56) & 0xFF])


def md5_file(path: Path | str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(_CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def to_gray(rgb: np.ndarray, w: int, h: int) -> np.ndarray:
    """按 ITU-R 601 转灰度并缩放到指定尺寸，返回 float64 数组。"""
    im = Image.fromarray(rgb).convert("L").resize((w, h), Image.Resampling.LANCZOS)
    return np.asarray(im, dtype=np.float64)


@lru_cache(maxsize=8)
def _dct_matrix(n: int) -> np.ndarray:
    """正交 DCT-II 变换矩阵（预先算好，避免每次重复求 cos）。"""
    k = np.arange(n).reshape(-1, 1)
    x = np.arange(n).reshape(1, -1)
    m = np.cos(np.pi * (2 * x + 1) * k / (2.0 * n))
    m *= np.sqrt(2.0 / n)
    m[0] *= np.sqrt(0.5)
    return m


def _bits_to_int(bits: np.ndarray) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | int(bool(b))
    return v


def phash_from_gray(g32: np.ndarray) -> int:
    """32x32 灰度图的 DCT 感知哈希（64 位）。"""
    m = _dct_matrix(32)
    d = m @ g32 @ m.T
    low = d[:8, :8].flatten()
    med = float(np.median(low[1:]))  # 去掉直流分量再取中位数
    return _bits_to_int(low > med)


def dhash_from_gray(g98: np.ndarray) -> int:
    """9x8 灰度图的水平梯度哈希（64 位）。"""
    return _bits_to_int((g98[:, 1:] > g98[:, :-1]).flatten())


def dihedral_variants(gray: np.ndarray) -> list[np.ndarray]:
    """8 种二面体变换：原图、三次旋转、水平镜像及其三次旋转。"""
    base = [gray, np.rot90(gray, 1), np.rot90(gray, 2), np.rot90(gray, 3)]
    flip = np.fliplr(gray)
    return base + [flip, np.rot90(flip, 1), np.rot90(flip, 2), np.rot90(flip, 3)]


def fingerprints(rgb: np.ndarray, with_dihedral: bool = True) -> dict:
    """从一张 RGB 数组算出全部感知指纹。"""
    g32 = to_gray(rgb, 32, 32)
    g98 = to_gray(rgb, 9, 8)
    out = {"phash": phash_from_gray(g32), "dhash": dhash_from_gray(g98),
           "pvars": [], "dvars": []}
    if with_dihedral:
        out["pvars"] = [phash_from_gray(v) for v in dihedral_variants(g32)]
        out["dvars"] = [dhash_from_gray(v) for v in dihedral_variants(g98)]
    return out


def min_dist(h_main: int, h_vars: list[int], other: int, other_vars: list[int]) -> int:
    """两组变体之间取最小汉明距离。"""
    best = hamming(h_main, other)
    for x in h_vars:
        d = hamming(x, other)
        if d < best:
            best = d
    for y in other_vars:
        d = hamming(h_main, y)
        if d < best:
            best = d
    for x in h_vars:
        for y in other_vars:
            d = hamming(x, y)
            if d < best:
                best = d
    return best


def pair_distance(va: list[int], vb: list[int]) -> int:
    """两组哈希变体的最小距离（集合之间两两取最小）。"""
    best = 64
    for x in va:
        for y in vb:
            d = hamming(x, y)
            if d < best:
                best = d
                if best == 0:
                    return 0
    return best
