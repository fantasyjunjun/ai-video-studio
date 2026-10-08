"""遮罩时序稳定性体检（遮挡重绘路线用）。

为什么需要它：遮挡重绘的头号杀手**不是**"遮罩盖得准不准"，而是"遮罩自己在抖"。
逐帧独立分割 + 膨胀，边界每帧微微跳一下，重绘区就跟着跳，
成片表现为**边缘闪烁 / 呼吸**——比道具丢了更刺眼，而且事后修不掉。

判据：
  - 覆盖率  cover      每帧白像素占比。突然跳 = 有帧漏了主体。
  - 逐帧 IoU           相邻两帧遮罩的交并比。**这是核心指标**，越高越稳。
  - 翻转率  flip       相邻两帧状态不同的像素占比。直接对应"闪烁像素"。
  - 面积抖动  cv       覆盖率序列的变异系数（std/mean）。

经验线（15s 竖版带货片、遮挡重绘）：
  IoU  ≥ 0.98 很好 | 0.95-0.98 可接受（边缘轻微呼吸）| < 0.95 会看见闪
  flip ≤ 1.5% 很好 | 1.5-3% 边缘抖 | > 3% 明显闪
  单帧突跳 ≤ 1.5 pt 很好 | > 4 pt 疑似漏分割

⚠️ 覆盖率要分两件事看，别混：
  - **缓慢漂移**（首→尾平滑变化）＝ 镜头推拉，正常，不算闪烁；
  - **单帧突跳**＝ 某帧漏了主体，致命，必须修。
  只看极差会把推拉误判成闪烁（踩过）。

用法：
    python scripts/mask_stats.py --frames _masks/p13/frames --prefix s5
    python scripts/mask_stats.py --frames-dir ... --csv out.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

GOOD = {"iou": 0.98, "flip": 1.5, "jump": 1.5}
OK = {"iou": 0.95, "flip": 3.0, "jump": 4.0}


def load(frames_dir: Path, prefix: str) -> tuple[np.ndarray, list[str]]:
    pat = re.compile(rf"^{re.escape(prefix)}_(\d+)\.png$") if prefix else re.compile(r"_(\d+)\.png$")
    items: list[tuple[int, Path]] = []
    for p in frames_dir.glob("*.png"):
        m = pat.match(p.name)
        if m:
            items.append((int(m.group(1)), p))
    if not items:
        raise SystemExit(f"没找到匹配的帧：{frames_dir} prefix={prefix!r}")
    items.sort()
    arrs, names = [], []
    for _, p in items:
        im = Image.open(p).convert("L")
        arrs.append(np.asarray(im) > 127)
        names.append(p.name)
    return np.stack(arrs), names


def grade(v: float, good: float, ok: float, lower_is_better: bool) -> str:
    if lower_is_better:
        return "很好" if v <= good else ("可接受" if v <= ok else "会看见闪/跳")
    return "很好" if v >= good else ("可接受" if v >= ok else "会看见闪/跳")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default="_masks/p13/frames", help="逐帧 PNG 目录")
    ap.add_argument("--prefix", default="", help="文件名前缀（如 s5）；留空取全部")
    ap.add_argument("--csv", default="", help="逐帧明细写到这个 CSV")
    a = ap.parse_args()

    base = Path(__file__).resolve().parent.parent
    fdir = Path(a.frames)
    if not fdir.is_absolute():
        fdir = base / fdir
    m, names = load(fdir, a.prefix)
    t = m.shape[0]
    cover = m.reshape(t, -1).mean(axis=1)

    ious, flips = [], []
    for i in range(1, t):
        a0, a1 = m[i - 1], m[i]
        inter = np.logical_and(a0, a1).sum()
        union = np.logical_or(a0, a1).sum()
        ious.append(float(inter) / float(union) if union else 1.0)
        flips.append(float(np.logical_xor(a0, a1).mean()) * 100.0)

    iou = float(np.mean(ious)) if ious else 1.0
    iou_min = float(np.min(ious)) if ious else 1.0
    flip = float(np.mean(flips)) if flips else 0.0
    flip_max = float(np.max(flips)) if flips else 0.0
    cmean = float(cover.mean()) * 100
    cv = float(cover.std() / cover.mean() * 100) if cover.mean() else 0.0
    crange = float((cover.max() - cover.min()) * 100)
    # 覆盖率突变点：找出跳得最狠的那一帧（通常是漏分割的那帧）
    worst_i, worst_d = -1, 0.0
    for i in range(1, t):
        d = abs(float(cover[i] - cover[i - 1])) * 100
        if d > worst_d:
            worst_i, worst_d = i, d

    print(f"遮罩序列 {fdir}")
    print(f"  帧数 {t}   文件 {names[0]} … {names[-1]}")
    print()
    # 覆盖率要分两件事看：**缓慢漂移**（镜头推拉，正常）vs **单帧突跳**（漏分割，致命）。
    # 只看极差会把推拉误判成闪烁 —— 踩过这个坑，所以这里分开判。
    drift = cmean and abs(float(cover[-1] - cover[0])) * 100
    trend = "上升" if cover[-1] > cover[0] else "下降"
    print(f"  覆盖率       均值 {cmean:.1f}%   极差 {crange:.1f} pt   cv {cv:.1f}%")
    print(f"               首→尾 {cover[0] * 100:.1f}% → {cover[-1] * 100:.1f}%"
          f"（缓慢{trend} {drift:.1f} pt，镜头推拉的正常现象，不算闪烁）")
    print(f"  单帧突跳     最大 {worst_d:.2f} pt"
          + (f"（第 {worst_i} 帧 {names[worst_i]}）" if worst_i > 0 else "")
          + f"   [{grade(worst_d, GOOD['jump'], OK['jump'], True)}]")
    print(f"  逐帧 IoU     均值 {iou:.4f}   最低 {iou_min:.4f}   "
          f"[{grade(iou, GOOD['iou'], OK['iou'], False)}]")
    print(f"  翻转率       均值 {flip:.2f}%   最高 {flip_max:.2f}%   "
          f"[{grade(flip, GOOD['flip'], OK['flip'], True)}]")
    print()
    print("  判据：IoU ≥0.98 很好 / <0.95 会闪；翻转率 ≤1.5% 很好 / >3% 明显闪；"
          "单帧突跳 ≤1.5 pt 很好")

    if a.csv:
        out = Path(a.csv)
        if not out.is_absolute():
            out = base / out
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["idx", "file", "cover_pct", "iou_prev", "flip_pct_prev"])
            for i in range(t):
                w.writerow([i, names[i], f"{cover[i] * 100:.3f}",
                            f"{ious[i - 1]:.5f}" if i else "",
                            f"{flips[i - 1]:.3f}" if i else ""])
        print(f"  逐帧明细 -> {out}")

    bad = (iou < OK["iou"]) or (flip > OK["flip"]) or (worst_d > OK["jump"])
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
