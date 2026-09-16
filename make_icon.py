#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 exe 图标（朱砂圆角方块 + 白色下载箭头）。

用法: python make_icon.py [输出路径]   # 默认 app.ico
"""

import sys

from PIL import Image, ImageDraw

ZHU_SHA = (192, 58, 43, 255)
WHITE = (255, 255, 255, 255)
SIZE = 256
S = 8  # 超采样倍数，先画大图再缩小，边缘才平滑


def rounded(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def build() -> Image.Image:
    n = SIZE * S
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角底板
    rounded(d, (0, 0, n - 1, n - 1), int(n * 0.22), ZHU_SHA)

    # 下载箭头：竖杆 + 三角头
    cx = n / 2
    bar_w = n * 0.115
    bar_top = n * 0.24
    bar_bot = n * 0.50
    rounded(d, (cx - bar_w / 2, bar_top, cx + bar_w / 2, bar_bot), int(bar_w * 0.28), WHITE)

    head_w = n * 0.34
    d.polygon(
        [
            (cx - head_w / 2, bar_bot - n * 0.02),
            (cx + head_w / 2, bar_bot - n * 0.02),
            (cx, bar_bot + n * 0.17),
        ],
        fill=WHITE,
    )

    # 底部托盘横线
    tray_w = n * 0.46
    tray_h = n * 0.075
    tray_y = n * 0.745
    rounded(
        d,
        (cx - tray_w / 2, tray_y, cx + tray_w / 2, tray_y + tray_h),
        int(tray_h / 2),
        WHITE,
    )

    return img.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "app.ico"
    img = build()
    img.save(out, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    img.resize((256, 256), Image.LANCZOS).save(out.replace(".ico", "_preview.png"))
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
