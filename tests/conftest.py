"""合成测试样本：覆盖同一张照片的各种版本，以及各种「长得像但不是同一张」。"""

from __future__ import annotations

import csv
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

    # 完全无关
    other = Image.fromarray(np.dstack([(yy * 0.4) % 256, ((xx * 2 + yy) * 0.2) % 256,
                                       (xx * 0.3) % 256]).astype(np.uint8))
    od = ImageDraw.Draw(other)
    for _ in range(40):
        x0, y0 = int(rng.integers(0, W - 100)), int(rng.integers(0, H - 100))
        od.polygon([(x0, y0), (x0 + 70, y0 + 15), (x0 + 25, y0 + 90)],
                   fill=tuple(int(v) for v in rng.integers(0, 256, 3)))
    save("14_other.jpg", other, 90)

    return paths


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("samples")
    build_samples(root)
    return root
