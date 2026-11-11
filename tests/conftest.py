"""合成测试样本：覆盖同一张照片的各种版本，以及各种「长得像但不是同一张」。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # UI 测试无头运行，须在导入 PySide6 前设置

if os.name == "nt":
    # 在 DSH 沙箱等受限环境下，带限制 mode（如 tempfile.mkdtemp 的 0o700）创建的
    # 目录会丢失工作区授权条目，目录内一切写入被拒（实测：默认 mode 正常，
    # mode=0o700 被拒）。测试进程统一用默认 mode 建临时目录；常规 Windows 环境
    # 本就忽略 mode 参数，此补丁无副作用。
    _real_mkdir = os.mkdir

    def _sandbox_safe_mkdir(path, mode=0o777):
        return _real_mkdir(path, 0o777)

    os.mkdir = _sandbox_safe_mkdir

import csv
import hashlib
import os
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

W, H = 800, 600


def _rich_base(seed: int = 7) -> Image.Image:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W]
    arr = np.dstack([(xx * 0.25) % 256, (yy * 0.34) % 256,
                     ((xx + yy) * 0.15) % 256]).astype(np.uint8)
    im = Image.fromarray(arr)
    dr = ImageDraw.Draw(im)
    for i in range(45):
        x0, y0 = int(rng.integers(0, W - 120)), int(rng.integers(0, H - 120))
        x1, y1 = x0 + int(rng.integers(20, 140)), y0 + int(rng.integers(20, 140))
        c = tuple(int(v) for v in rng.integers(0, 256, 3))
        if i % 3 == 0:
            dr.rectangle([x0, y0, x1, y1], outline=c, width=4)
        elif i % 3 == 1:
            dr.ellipse([x0, y0, x1, y1], fill=c)
        else:
            dr.line([x0, y0, x1, y1], fill=c, width=3)
    dr.rectangle([100, 80, 350, 300], fill=(230, 45, 45))
    dr.ellipse([400, 150, 700, 450], fill=(25, 90, 220))
    return im


def _noisy(im: Image.Image, seed: int, strength: int = 7) -> Image.Image:
    rng = np.random.default_rng(seed)
    a = np.asarray(im).astype(np.int16)
    a += rng.integers(-strength, strength + 1, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def build_samples(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    base = _rich_base()
    paths: dict[str, Path] = {}

    def save(name: str, im: Image.Image, quality: int = 92) -> Path:
        p = root / name
        im.save(p, quality=quality)
        paths[name] = p
        return p

    save("01_base.jpg", base)

    # 字节完全一致
    shutil.copy2(root / "01_base.jpg", root / "02_exact.jpg")
    paths["02_exact.jpg"] = root / "02_exact.jpg"

    # 像素一致，只多了尾部元数据
    raw = (root / "01_base.jpg").read_bytes()
    (root / "03_samepixel.jpg").write_bytes(raw + b"\r\n<!-- trailer -->\r\n")
    paths["03_samepixel.jpg"] = root / "03_samepixel.jpg"

    save("04_scaled.jpg", base.resize((480, 360), Image.Resampling.LANCZOS), 90)
    save("05_recompress.jpg", base, 35)
    save("06_brighter.jpg",
         ImageEnhance.Contrast(ImageEnhance.Brightness(base).enhance(1.35)).enhance(0.85), 90)

    wm = base.copy()
    ImageDraw.Draw(wm).text((640, 540), "COPY 2026", fill=(255, 255, 0))
    save("07_watermark.jpg", wm, 90)

    save("08_rot90.jpg", base.transpose(Image.Transpose.ROTATE_90), 90)
    save("09_mirror.jpg", ImageOps.mirror(base), 90)
    save("10_crop.jpg", base.crop((150, 100, 750, 550)), 90)

    # 同场景连拍：背景一样，物体位置不同 —— 必须被判成「不同」
    rng = np.random.default_rng(11)
    yy, xx = np.mgrid[0:H, 0:W]
    bg = Image.fromarray(np.dstack([(xx * 0.18) % 256, (yy * 0.22) % 256,
                                    ((xx + yy) * 0.10) % 256]).astype(np.uint8))
    bd = ImageDraw.Draw(bg)
    for _ in range(20):
        x0, y0 = int(rng.integers(0, W - 90)), int(rng.integers(0, H - 90))
        bd.rectangle([x0, y0, x0 + 60, y0 + 60], outline=(255, 255, 255), width=3)
    for tag, cx in (("a", 250), ("b", 520)):
        im = bg.copy()
        dr = ImageDraw.Draw(im)
        dr.ellipse([cx, 240, cx + 130, 370], fill=(215, 45, 40))
        dr.text((30, 30), "SAME SCENE DIFFERENT FRAME", fill=(0, 0, 0))
        save(f"11_burst_{tag}.jpg", im, 90)

    # 纯色墙
    save("12_wall_a.jpg", _noisy(Image.new("RGB", (W, H), (240, 238, 234)), 3), 90)
    save("13_wall_b.jpg", _noisy(Image.new("RGB", (W, H), (226, 224, 220)), 4), 90)
    # 同一张纯色墙的重压缩副本：纯色图 pHash 全同（DC 主导）、ORB 提不到特征，
    # 颜色差校验层（verify.color_gate，标定差 13.1 vs 2688）是唯一能并组它的证据
    save("17_wall_a_copy.jpg", _noisy(Image.new("RGB", (W, H), (240, 238, 234)), 3), 85)

    # 完全无关
    other = Image.fromarray(np.dstack([(yy * 0.4) % 256, ((xx * 2 + yy) * 0.2) % 256,
                                       (xx * 0.3) % 256]).astype(np.uint8))
    od = ImageDraw.Draw(other)
    for _ in range(40):
        x0, y0 = int(rng.integers(0, W - 100)), int(rng.integers(0, H - 100))
        od.polygon([(x0, y0), (x0 + 70, y0 + 15), (x0 + 25, y0 + 90)],
                   fill=tuple(int(v) for v in rng.integers(0, 256, 3)))
    save("14_other.jpg", other, 90)

    # 同版式不同内容的手机截图：布局完全一样，文字不同 —— 真实库里最容易误判的一类
    #
    # 注意：这两张图必须**不依赖字体**就有差异。CI 上没有 arial/segoeui/msyh，
    # 只会拿到 load_default() 的位图小字，若差异全靠文字渲染，图会退化成几乎相同，
    # 测试就变成「看机器装的什么字体」。所以每行的色块颜色由行文字哈希决定 ——
    # 布局一致、多处小色块不同，正好对应真实截图「同版式不同内容」的差异形态。
    from PIL import ImageFont

    def _font(size: int):
        for name in ("arial.ttf", "segoeui.ttf", "msyh.ttc", "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
        try:
            return ImageFont.load_default(size=size)  # Pillow >= 10.1 支持可缩放默认字体
        except Exception:
            return ImageFont.load_default()

    def _badge(ln: str) -> tuple[int, int, int]:
        h = int(hashlib.md5(ln.encode("utf-8")).hexdigest()[:6], 16)
        return (50 + (h & 0x7F), 60 + ((h >> 8) & 0x7F), 90 + ((h >> 16) & 0x5F))

    def screen(rows: list[str], accent: tuple[int, int, int]) -> Image.Image:
        im = Image.new("RGB", (540, 960), (250, 250, 252))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 540, 46], fill=(40, 44, 52))
        d.text((14, 12), "9:41   100%", font=_font(20), fill=(255, 255, 255))
        d.rectangle([0, 46, 540, 150], fill=accent)
        d.text((20, 70), rows[0], font=_font(30), fill=(255, 255, 255))
        y = 175
        for ln in rows[1:]:
            d.rectangle([20, y + 6, 60, y + 46], fill=_badge(ln))
            d.text((76, y + 8), ln, font=_font(26), fill=(30, 30, 30))
            d.text((470, y + 12), ">", font=_font(24), fill=(150, 150, 150))
            d.line([20, y + 62, 520, y + 62], fill=(222, 222, 226))
            y += 72
        return im

    save("15_screen_a.png", screen(
        ["Mail", "Inbox", "Sent items", "Drafts", "Archive", "Trash",
         "Settings", "Help centre"], (60, 120, 216)), 95)
    save("16_screen_b.png", screen(
        ["Mail", "STARRED", "SNOOZED", "IMPORTANT", "ALL MAIL", "SPAM",
         "LABELS", "FILTERS"], (60, 120, 216)), 95)

    return paths


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("samples")
    build_samples(root)
    return root
