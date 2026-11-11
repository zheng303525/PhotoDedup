"""生成应用图标 resources/icon.ico（两个叠放的照片框 + 一个对勾）。

    python scripts/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZES = [16, 24, 32, 48, 64, 128, 256]
BG = (30, 32, 38, 255)
ACCENT = (74, 140, 247, 255)
KEEP = (63, 166, 106, 255)
PAPER = (232, 233, 236, 255)


def draw(size: int) -> Image.Image:
    s = size * 8  # 超采样后缩小，边缘更顺
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    r = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=BG)

    # 后面那张照片（偏右上，深色）
    w, h = int(s * 0.46), int(s * 0.42)
    x0, y0 = int(s * 0.30), int(s * 0.16)
    d.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=int(s * 0.05),
                        fill=(58, 62, 72, 255))

    # 前面那张照片（偏左下，亮色）
    x1, y1 = int(s * 0.16), int(s * 0.30)
    d.rounded_rectangle([x1, y1, x1 + w, y1 + h], radius=int(s * 0.05),
                        fill=PAPER)
    # 照片里的「山」
    d.polygon([(x1 + int(w * 0.10), y1 + h - int(h * 0.16)),
               (x1 + int(w * 0.38), y1 + int(h * 0.40)),
               (x1 + int(w * 0.62), y1 + h - int(h * 0.16))],
              fill=(150, 160, 175, 255))
    d.ellipse([x1 + int(w * 0.62), y1 + int(h * 0.16),
               x1 + int(w * 0.80), y1 + int(h * 0.34)],
              fill=(240, 196, 90, 255))

    # 右下角对勾徽标
    cx, cy = int(s * 0.74), int(s * 0.74)
    rr = int(s * 0.20)
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=KEEP)
    d.line([(cx - int(rr * 0.45), cy + int(rr * 0.02)),
            (cx - int(rr * 0.08), cy + int(rr * 0.40)),
            (cx + int(rr * 0.50), cy - int(rr * 0.36))],
           fill=(255, 255, 255, 255), width=max(2, int(s * 0.045)), joint="curve")

    return im.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "resources"
    out.mkdir(exist_ok=True)
    frames = [draw(s) for s in SIZES]
    ico = out / "icon.ico"
    frames[-1].save(ico, format="ICO",
                    sizes=[(s, s) for s in SIZES])
    frames[-1].save(out / "icon.png")
    print(f"已生成 {ico}")


if __name__ == "__main__":
    main()
