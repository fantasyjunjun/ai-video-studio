"""生成内置示例商品图（**零成本**：纯本地合成 SVG，不调任何图像模型）。

为什么是 SVG 而不是生成照片：
  - 示例图是**随包分发的出厂资产**，本该待在仓库里，而不是用户装机时联网生成；
  - 调图像模型要花积分（单张 5–10），为一个"让你先试试批量出片"的引导按钮
    花这笔钱不值；
  - 卡片缩略图尺寸下，干净的几何插画与实拍照片的观感差距远小于成本差距。

将来想换成真实商品照：把同名的 `.png`（或 `.jpg`）丢进
`data/uploads/examples/default/`，`app/api/examples.py` 会**按 png → jpg → svg
的顺序自动取用**，不用改代码。

跑法（托管 venv）：
    C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe make_example_images.py
"""

from __future__ import annotations

import os
import sys

W, H = 960, 540

OUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "data", "uploads", "examples", "default",
)
OUT_DIR = os.path.normpath(OUT_DIR)


def _frame(bg_a: str, bg_b: str, glow: str) -> str:
    """统一的"摄影棚"底：柔和双色渐变 + 顶部一抹高光 + 地面阴影带。"""
    return f"""
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0.4" y2="1">
      <stop offset="0%" stop-color="{bg_a}"/>
      <stop offset="100%" stop-color="{bg_b}"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.28" r="0.62">
      <stop offset="0%" stop-color="{glow}" stop-opacity="0.55"/>
      <stop offset="100%" stop-color="{glow}" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="floor" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#000000" stop-opacity="0"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0.28"/>
    </linearGradient>
    <filter id="soft" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="14"/>
    </filter>
    <filter id="drop" x="-40%" y="-40%" width="180%" height="180%">
      <feDropShadow dx="0" dy="18" stdDeviation="16" flood-color="#000" flood-opacity="0.35"/>
    </filter>
  </defs>
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <rect width="{W}" height="{H}" fill="url(#glow)"/>
  <ellipse cx="{W/2:.0f}" cy="452" rx="212" ry="30" fill="#000" opacity="0.30" filter="url(#soft)"/>
"""


def juicer() -> str:
    """便携榨汁杯：透明杯体 + 刀头 + 杯盖，薄荷色系。"""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
{_frame("#12403c", "#08201f", "#5fe3c8")}
  <g filter="url(#drop)">
    <rect x="418" y="112" width="124" height="34" rx="16" fill="#e8f7f4"/>
    <rect x="424" y="140" width="112" height="26" rx="10" fill="#bcd8d4"/>
    <rect x="404" y="164" width="152" height="252" rx="34" fill="#dff2ef" opacity="0.92"/>
    <rect x="404" y="164" width="152" height="252" rx="34" fill="none" stroke="#ffffff" stroke-opacity="0.55" stroke-width="3"/>
    <path d="M424 296 q56 -26 112 0 v96 a34 34 0 0 1 -34 34 h-44 a34 34 0 0 1 -34 -34 z" fill="#4fd1b5" opacity="0.95"/>
    <circle cx="480" cy="232" r="30" fill="none" stroke="#6b8f8b" stroke-width="7" stroke-linecap="round"/>
    <path d="M480 202 v18 M480 244 v18 M450 232 h18 M492 232 h18" stroke="#6b8f8b" stroke-width="7" stroke-linecap="round"/>
    <rect x="462" y="196" width="36" height="8" rx="4" fill="#8ab3ae"/>
    <rect x="404" y="404" width="152" height="18" rx="9" fill="#2f5e5a"/>
  </g>
  <rect x="0" y="430" width="{W}" height="{H-430}" fill="url(#floor)"/>
</svg>
"""


def coffee() -> str:
    """冷萃咖啡液：小玻璃瓶 + 深色液体 + 瓶盖，琥珀色系。"""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
{_frame("#3b2416", "#1d1008", "#f0a952")}
  <g filter="url(#drop)">
    <rect x="446" y="120" width="68" height="30" rx="8" fill="#c98b4b"/>
    <rect x="452" y="146" width="56" height="22" rx="6" fill="#e0ab6d"/>
    <path d="M436 168 q44 -14 88 0 v228 a20 20 0 0 1 -20 20 h-48 a20 20 0 0 1 -20 -20 z"
          fill="#e8d8c2" opacity="0.30"/>
    <path d="M436 244 q44 -12 88 0 v152 a20 20 0 0 1 -20 20 h-48 a20 20 0 0 1 -20 -20 z"
          fill="#5a3218"/>
    <path d="M436 168 q44 -14 88 0 v228 a20 20 0 0 1 -20 20 h-48 a20 20 0 0 1 -20 -20 z"
          fill="none" stroke="#ffffff" stroke-opacity="0.45" stroke-width="3"/>
    <rect x="454" y="262" width="52" height="76" rx="6" fill="#f4e6d2" opacity="0.90"/>
    <rect x="462" y="278" width="36" height="6" rx="3" fill="#7a4d24"/>
    <rect x="462" y="292" width="26" height="6" rx="3" fill="#a5764a"/>
    <rect x="462" y="306" width="32" height="6" rx="3" fill="#a5764a"/>
    <ellipse cx="498" cy="182" rx="8" ry="22" fill="#ffffff" opacity="0.35"/>
    <rect x="450" y="416" width="60" height="16" rx="8" fill="#3a2210"/>
  </g>
  <rect x="0" y="430" width="{W}" height="{H-430}" fill="url(#floor)"/>
</svg>
"""


def tissue() -> str:
    """云柔加厚抽纸：纸巾盒 + 抽出的纸角，柔蓝色系。"""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
{_frame("#1e3350", "#0c1729", "#8fc4ff")}
  <g filter="url(#drop)">
    <path d="M424 150 q18 -34 56 -22 q30 10 52 -2 q34 -18 56 26 l-16 42 h-132 z"
          fill="#fbfdff"/>
    <path d="M424 150 q18 -34 56 -22 q30 10 52 -2 q34 -18 56 26"
          fill="none" stroke="#c8dcf2" stroke-width="3"/>
    <rect x="368" y="196" width="224" height="212" rx="20" fill="#eaf2fb"/>
    <path d="M368 216 h224 v192 h-224 z" fill="#d7e6f7" opacity="0.55"/>
    <ellipse cx="480" cy="196" rx="72" ry="20" fill="#b9cfe8"/>
    <ellipse cx="480" cy="194" rx="54" ry="12" fill="#93aecb"/>
    <rect x="368" y="196" width="224" height="212" rx="20" fill="none"
          stroke="#ffffff" stroke-opacity="0.65" stroke-width="3"/>
    <rect x="404" y="272" width="152" height="12" rx="6" fill="#8fb2d6"/>
    <rect x="404" y="296" width="104" height="9" rx="4.5" fill="#a9c4de"/>
    <circle cx="502" cy="352" r="20" fill="#5fa8e8" opacity="0.85"/>
    <path d="M494 352 l6 6 l11 -13" stroke="#fff" stroke-width="5"
          fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  </g>
  <rect x="0" y="430" width="{W}" height="{H-430}" fill="url(#floor)"/>
</svg>
"""


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, svg in (("juicer.svg", juicer()),
                      ("coffee.svg", coffee()),
                      ("tissue.svg", tissue())):
        path = os.path.join(OUT_DIR, name)
        # 原地覆盖，不删除（环境约定禁止 os.remove —— 删了会触发安全钩子中止进程）
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg)
        print(f"written {path} ({len(svg)} bytes)", flush=True)


if __name__ == "__main__":
    main()
