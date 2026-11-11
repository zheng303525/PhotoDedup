"""图像读取与静态信息提取：EXIF、JPEG 质量估计、锐度、格式探测。

所有函数都是只读的，不会修改原文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

try:  # HEIC/HEIF 支持是可选的，装了就启用
    import pillow_heif

    pillow_heif.register_heif_opener()
    HAS_HEIF = True
except Exception:  # pragma: no cover - 环境相关
    HAS_HEIF = False

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

DEFAULT_MAX_SIDE = 1024

# IJG 标准亮度量化表，用来反推 JPEG 质量
_STD_LUMA = np.array([
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
], dtype=np.float64)


def _clean_str(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).replace("\x00", "").strip()
    return "" if s in {"--", "-", "unknown", "UNKNOWN"} else s


def _clean_datetime(v: str) -> str:
    """EXIF 时间 '2024:05:03 14:22:31' -> '2024-05-03 14:22:31'；全零视为无效。"""
    s = _clean_str(v)
    if not s or s.startswith("0000") or len(s) < 10:
        return ""
    return s.replace(":", "-", 2)


def _is_zero_rational(v: Any) -> bool:
    try:
        return all(float(x) == 0.0 for x in v)
    except Exception:
        return False


def read_exif(im: Image.Image) -> dict[str, Any]:
    """抽取判断「原件还是转发副本」需要的那几个字段。"""
    out: dict[str, Any] = {
        "make": "", "model": "", "shot_time": "",
        "gps": False, "gps_zero": False, "has_icc": bool(im.info.get("icc_profile")),
        "exif_present": False,
    }
    try:
        ex = im.getexif()
    except Exception:
        return out
    if not ex:
        return out
    out["exif_present"] = True
    out["make"] = _clean_str(ex.get(271))
    out["model"] = _clean_str(ex.get(272))

    dt = _clean_str(ex.get(306))
    try:
        sub = ex.get_ifd(0x8769)
        dt = _clean_str(sub.get(36867)) or dt
    except Exception:
        pass
    out["shot_time"] = _clean_datetime(dt)

    try:
        gps = ex.get_ifd(0x8825)
        if gps:
            out["gps"] = True
            # GPSLatitude / GPSLongitude 全为 0 是软件抹除的典型痕迹
            lat, lon = gps.get(2), gps.get(4)
            if lat is not None and lon is not None:
                out["gps_zero"] = _is_zero_rational(lat) and _is_zero_rational(lon)
    except Exception:
        pass
    return out


def jpeg_quality(im: Image.Image) -> int | None:
    """用 IJG 量化表缩放关系反推 JPEG 质量，误差通常在 ±5 以内。"""
    table = None
    try:
        q = getattr(im, "quantization", None)
        if q:
            table = q.get(0) or next(iter(q.values()), None)
    except Exception:
        return None
    if not table or len(table) != 64:
        return None
    arr = np.array(table, dtype=np.float64)
    scale = float(np.mean(arr / _STD_LUMA) * 100.0)
    if scale <= 0:
        return None
    quality = (200.0 - scale) / 2.0 if scale <= 100 else 5000.0 / scale
    return int(max(1, min(100, round(quality))))


def load_rgb(path: Path | str, max_side: int = DEFAULT_MAX_SIDE) -> tuple[np.ndarray, dict[str, Any]]:
    """把图片读成 RGB numpy 数组，同时带回需要持久化的静态信息。

    要点：
    - JPEG 走 `draft()` 让解码器直接按 DCT 降采样输出，比全解码再缩小快数倍
    - 读完后按 EXIF 方向摆正，保证「同一张照片、方向标记不同」也能对上
    - 返回数组最长边不超过 max_side，节省内存与后续计算
    """
    meta: dict[str, Any] = {
        "width": 0, "height": 0, "fmt": "", "mode": "",
        "sharpness": 0.0, "jpeg_quality": None,
    }
    with Image.open(path) as im:
        meta["fmt"] = (im.format or "").upper()
        meta["mode"] = im.mode
        meta["width"], meta["height"] = im.size
        meta["jpeg_quality"] = jpeg_quality(im) if meta["fmt"] in {"JPEG", "MPO", "WEBP"} else None
        meta.update(read_exif(im))

        try:
            im.draft("RGB", (max_side, max_side))
        except Exception:
            pass

        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        elif im.mode == "L":
            im = im.convert("RGB")

        w, h = im.size
        if max(w, h) > max_side:
            s = max_side / float(max(w, h))
            im = im.resize((max(1, int(round(w * s))), max(1, int(round(h * s)))),
                           Image.Resampling.LANCZOS)
        arr = np.asarray(im, dtype=np.uint8)

    meta["sharpness"] = sharpness_of(arr)
    return np.ascontiguousarray(arr), meta


def sharpness_of(rgb: np.ndarray, size: int = 512) -> float:
    """拉普拉斯方差，作为「细节锐度」的代理指标。

    统一缩到同一边长再比，避免分辨率差异直接污染结果。
    """
    if cv2 is None or rgb.size == 0:
        return 0.0
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = g.shape[:2]
    if max(h, w) != size:
        s = size / float(max(h, w))
        g = cv2.resize(g, (max(1, int(round(w * s))), max(1, int(round(h * s)))),
                       interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def exif_score(exif: dict[str, Any]) -> int:
    """0-4 分：元数据完整度，用来判断哪一份更像原件。"""
    score = 0
    if exif.get("make") or exif.get("model"):
        score += 1
    if exif.get("shot_time"):
        score += 1
    if exif.get("gps") and not exif.get("gps_zero"):
        score += 1
    if exif.get("has_icc"):
        score += 1
    return score


def decoded_rgb_md5(path: Path | str) -> str:
    """整图解码后取像素指纹（全尺寸，不走 draft），用于「同像素」定论。"""
    import hashlib

    h = hashlib.md5()
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        h.update(("%dx%d|" % im.size).encode("ascii"))
        h.update(im.tobytes())
    return h.hexdigest()
