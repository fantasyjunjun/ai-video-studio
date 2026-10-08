"""验收线：原片 vs 成片的客观对照。

为什么需要它：反复出现「看着像但就是说不出差在哪」的争论。
把能算的算出来（色调/构图/细节密度/剪辑点），剩下的主观项列成清单人工勾。
**没有这条线，换任何技术路线都判不出"够不够好"。**

只依赖 ffmpeg + numpy（环境自带），不引入新包。

产出（全在 --out 目录）：
  00_overview.jpg   逐镜 AB 对照图（左原片 / 右成片）
  01_cuts.txt       两侧剪辑点比对（结构漂移）
  02_metrics.csv    逐镜客观指标
  03_checklist.md   人工验收清单（客观项自动填好，主观项留空）

用法：
    python scripts/accept_check.py --orig <原片> --new <成片> --out _accept/p13
    python scripts/accept_check.py --orig ... --new ... --timeline "A1:0-3,A2:3-6,A3:6-8,A4:8-13,A5:13-15"

不给 --timeline 时从原片自动检测剪辑点。
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy.signal import find_peaks

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.audio.ffmpeg import require  # noqa: E402

FF = require("ffmpeg")
FP = FF.replace("ffmpeg", "ffprobe")

SCALE_W = 240          # 分析用低分辨率宽度（够算指标，又便宜）
CUT_MERGE_SEC = 0.4    # 剪辑点合并窗口

# 报警阈值（启发式，宁可多报也别漏报 —— 它是提示人去看，不是判决）
TH_HIST_TV = 0.22      # 色彩直方图总变差
TH_HUE = 25.0          # 色相偏移（度）
TH_LUMA = 12.0         # 平均亮度差（0-255）
TH_EDGE_DROP = 0.45    # 边缘密度相对下降比例（细节/道具丢失）
TH_CENTROID = 0.08     # 构图重心位移（归一化）


def probe(path: Path) -> dict:
    r = subprocess.run(
        [FP, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate:format=duration",
         "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True)
    info = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()
    num, _, den = (info.get("r_frame_rate") or "24/1").partition("/")
    fps = float(num) / float(den or 1)
    return {
        "w": int(info.get("width") or 0),
        "h": int(info.get("height") or 0),
        "fps": fps,
        "duration": float(info.get("duration") or 0.0),
    }


def decode(path: Path, fps: float, src_w: int, src_h: int) -> np.ndarray:
    """按固定 fps 解码成 (T,H,W,3) uint8。两片用同一个 fps 才有可比性。

    高度按 `scale=240:-2` 的规则**算**出来，不从字节数反推 ——
    反推会命中一堆更小的公约数（h=2 也整除），得到完全错误的时间维。
    """
    h = max(2, int(round(src_h * SCALE_W / max(src_w, 1) / 2)) * 2)
    cmd = [FF, "-v", "error", "-i", str(path),
           "-vf", f"scale={SCALE_W}:{h},fps={fps:.6f}",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    if not raw:
        raise RuntimeError(f"解码失败（0 字节）：{path}")
    tmp = np.frombuffer(raw, np.uint8)
    per = SCALE_W * h * 3
    if per <= 0 or tmp.size % per != 0:
        raise RuntimeError(f"解码尺寸对不上：{tmp.size} 字节，期望 {SCALE_W}x{h} 的整数倍")
    n = tmp.size // per
    return tmp[: n * per].reshape(n, h, SCALE_W, 3)


def frame_diffs(frames: np.ndarray) -> np.ndarray:
    return np.abs(np.diff(frames.astype(np.int16), axis=0)).mean(axis=(1, 2, 3))


def detect_cuts(frames: np.ndarray, fps: float, duration: float) -> list[float]:
    """镜头切换点。用 scipy 找峰值，阈值走 MAD（对少数几个硬切很稳）。

    早先用 `mean + 4*std` 会把 3 个硬切自己算进 std 里，阈值被抬高到切点之上 → 一个都检不到。
    """
    d = frame_diffs(frames)
    if d.size == 0:
        return [0.0, round(duration, 2)]
    med = float(np.median(d))
    mad = float(np.median(np.abs(d - med))) * 1.4826
    thr = max(8.0, med + 6.0 * mad) if mad > 1e-6 else max(8.0, med * 3.0)
    peaks, props = find_peaks(d, height=thr, distance=max(1, int(CUT_MERGE_SEC * fps)))
    heights = props.get("height", np.zeros(len(peaks)))
    if len(peaks) > 12:                      # 只保留最强的 12 个
        keep = np.argsort(heights)[-12:]
        peaks = np.sort(peaks[keep])
    times = [round((int(i) + 1) / fps, 2) for i in peaks]
    out = [0.0] + times + [round(duration, 2)]
    clean = [out[0]]
    for t in out[1:]:
        if t - clean[-1] >= 0.2:
            clean.append(t)
    if clean[-1] < duration - 0.2:
        clean.append(round(duration, 2))
    return clean


def hue_sat(rgb: np.ndarray) -> tuple[float, float, float]:
    """-> (圆均值色相 度, 平均饱和度, 平均亮度)。色相按饱和度加权，避免灰区噪声。"""
    x = rgb.astype(np.float32) / 255.0
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    mx = x.max(axis=-1)
    mn = x.min(axis=-1)
    d = mx - mn
    dd = np.maximum(d, 1e-6)
    sat = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0.0)
    idx = np.argmax(x, axis=-1)
    h = np.where(idx == 0, ((g - b) / dd) % 6,
                 np.where(idx == 1, ((b - r) / dd) + 2, ((r - g) / dd) + 4)) * 60.0
    nz = d > 1e-3
    hue = 0.0
    if nz.any():
        w = sat[nz]
        rad = np.deg2rad(h[nz])
        if w.sum() > 1e-6:
            ang = np.arctan2(float((np.sin(rad) * w).mean()),
                             float((np.cos(rad) * w).mean()))
            hue = float(np.rad2deg(ang)) % 360.0
    luma = float((0.299 * r + 0.587 * g + 0.114 * b).mean() * 255.0)
    return hue, float(sat.mean()), luma


def hist64(rgb: np.ndarray, bins: int = 4) -> np.ndarray:
    q = (rgb.astype(np.int32) * bins // 256).clip(0, bins - 1)
    idx = q[..., 0] * bins * bins + q[..., 1] * bins + q[..., 2]
    h = np.bincount(idx.ravel(), minlength=bins ** 3).astype(np.float64)
    return h / max(h.sum(), 1.0)


def edge_stats(rgb: np.ndarray) -> tuple[float, float, float]:
    """-> (边缘密度, 重心x, 重心y)，均归一化到 0-1。接受 (T,H,W,3) 或 (H,W,3)。"""
    x = rgb.astype(np.float32) / 255.0
    lum = 0.299 * x[..., 0] + 0.587 * x[..., 1] + 0.114 * x[..., 2]
    if lum.ndim == 2:
        lum = lum[None, ...]
    gy = np.abs(np.diff(lum, axis=1))       # (T,H-1,W)
    gx = np.abs(np.diff(lum, axis=2))       # (T,H,W-1)
    e = (gy[:, :, :-1] + gx[:, :-1, :]).mean(axis=0)   # 对时间取平均 -> (H-1,W-1)
    tot = float(e.sum())
    if e.size == 0 or tot <= 1e-6:
        return 0.0, 0.5, 0.5
    h, w = e.shape
    ys = np.arange(h, dtype=np.float32)[:, None]
    xs = np.arange(w, dtype=np.float32)[None, :]
    cy = float((e * ys).sum() / tot) / max(h - 1, 1)
    cx = float((e * xs).sum() / tot) / max(w - 1, 1)
    return float(e.mean()), cx, cy


def slice_metrics(frames: np.ndarray, fps: float, a: float, b: float) -> dict:
    i0 = max(0, int(round(a * fps)))
    i1 = min(frames.shape[0], int(round(b * fps)))
    if i1 - i0 < 2:
        i1 = min(frames.shape[0], i0 + 2)
    seg = frames[i0:i1]
    hue, sat, luma = hue_sat(seg)
    dens, cx, cy = edge_stats(seg)
    return {"hue": hue, "sat": sat, "luma": luma, "edge": dens, "cx": cx, "cy": cy,
            "hist": hist64(seg), "n": int(seg.shape[0])}


def hue_delta(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def frame(path_in: Path, t: float, path_out: Path) -> bool:
    r = subprocess.run([FF, "-y", "-loglevel", "error", "-ss", f"{t:.3f}",
                        "-i", str(path_in), "-frames:v", "1", "-q:v", "2",
                        str(path_out)], check=False)
    return r.returncode == 0 and path_out.exists() and path_out.stat().st_size > 0


def hstack_pair(a: Path, b: Path, out: Path) -> bool:
    r = subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(a), "-i", str(b),
                        "-filter_complex", "hstack", "-q:v", "3", str(out)], check=False)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 0


def overview(pairs: list[Path], out: Path, cols: int) -> bool:
    if not pairs:
        return False
    if len(pairs) == 1:
        return hstack_pair(pairs[0], pairs[0], out)
    pr = subprocess.run([FP, "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=width,height", "-of", "csv=p=0",
                         str(pairs[0])], capture_output=True, text=True)
    try:
        w, h = (int(v) for v in pr.stdout.strip().split(",")[:2])
    except Exception:  # noqa: BLE001
        return False
    rows = (len(pairs) + cols - 1) // cols
    pad = rows * cols - len(pairs)
    items = list(pairs)
    if pad:
        filler = out.with_name(out.stem + "_pad.jpg")
        if subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi",
                           "-i", f"color=c=black:s={w}x{h}", "-frames:v", "1",
                           "-q:v", "3", str(filler)], check=False).returncode == 0:
            items += [filler] * pad
    layout = "|".join(f"{(i % cols) * w}_{(i // cols) * h}" for i in range(len(items)))
    cmd = [FF, "-y", "-loglevel", "error"]
    for p in items:
        cmd += ["-i", str(p)]
    cmd += ["-filter_complex", f"xstack=inputs={len(items)}:layout={layout}",
            "-q:v", "3", str(out)]
    return subprocess.run(cmd, check=False).returncode == 0 and out.exists()


def parse_timeline(spec: str) -> list[tuple[str, float, float]]:
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        code, span = part.split(":", 1)
        a, b = span.split("-", 1)
        out.append((code.strip(), float(a), float(b)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orig", required=True, help="原片（基准）")
    ap.add_argument("--new", required=True, help="成片/待验收片")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeline", default="", help="镜号:起-止, 例 A1:0-3,A2:3-6")
    ap.add_argument("--cols", type=int, default=2)
    ap.add_argument("--no-sheet", action="store_true", help="只算指标不出对照图")
    a = ap.parse_args()

    orig = Path(a.orig) if Path(a.orig).is_absolute() else (BASE / a.orig)
    new = Path(a.new) if Path(a.new).is_absolute() else (BASE / a.new)
    for p, tag in ((orig, "原片"), (new, "成片")):
        if not p.exists():
            print(f"{tag}不存在：{p}")
            return 2

    out = Path(a.out) if Path(a.out).is_absolute() else (BASE / a.out)
    out.mkdir(parents=True, exist_ok=True)

    po, pn = probe(orig), probe(new)
    print(f"原片  {po}   {orig}")
    print(f"成片  {pn}   {new}")
    fps = po["fps"] or 24.0
    # 两片都按**原片**的几何解码，否则尺寸不同没法逐帧比
    fo = decode(orig, fps, po["w"], po["h"])
    fn = decode(new, fps, po["w"], po["h"])
    print(f"解码  原片 {fo.shape}  成片 {fn.shape}  fps={fps}")

    cuts_o = detect_cuts(fo, fps, po["duration"])
    cuts_n = detect_cuts(fn, fps, pn["duration"])

    lines = ["=== 剪辑点比对（结构漂移）===",
             f"原片 ({len(cuts_o) - 1} 镜): " + " / ".join(f"{t:g}" for t in cuts_o),
             f"成片 ({len(cuts_n) - 1} 镜): " + " / ".join(f"{t:g}" for t in cuts_n)]
    same_count = (len(cuts_o) == len(cuts_n))
    drift = [abs(x - y) for x, y in zip(cuts_o, cuts_n)] if same_count else []
    lines.append(f"镜数一致: {same_count}")
    if drift:
        lines.append("逐点偏差: " + " / ".join(f"{d:.2f}s" for d in drift))
        bad = [f"{d:.2f}s" for d in drift if d > 0.3]
        lines.append("超 0.3s 的偏差: " + ("、".join(bad) if bad else "无"))
    (out / "01_cuts.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    for ln in lines:
        print(ln)

    shots = parse_timeline(a.timeline) if a.timeline else [
        (f"S{i + 1}", cuts_o[i], cuts_o[i + 1]) for i in range(len(cuts_o) - 1)
    ]

    rows, flags, pairs = [], [], []
    for code, t0, t1 in shots:
        mo = slice_metrics(fo, fps, t0, t1)
        mn = slice_metrics(fn, fps, t0, t1)
        tv = float(0.5 * np.abs(mo["hist"] - mn["hist"]).sum())
        dh = hue_delta(mo["hue"], mn["hue"])
        dl = mn["luma"] - mo["luma"]
        edge_drop = (mo["edge"] - mn["edge"]) / mo["edge"] if mo["edge"] > 1e-6 else 0.0
        dcx = mn["cx"] - mo["cx"]
        dcy = mn["cy"] - mo["cy"]
        rows.append({
            "shot": code, "t_start": round(t0, 2), "t_end": round(t1, 2),
            "dur": round(t1 - t0, 2),
            "hue_orig": round(mo["hue"], 1), "hue_new": round(mn["hue"], 1), "d_hue": round(dh, 1),
            "luma_orig": round(mo["luma"], 1), "luma_new": round(mn["luma"], 1), "d_luma": round(dl, 1),
            "sat_orig": round(mo["sat"], 3), "sat_new": round(mn["sat"], 3),
            "hist_tv": round(tv, 3),
            "edge_orig": round(mo["edge"], 4), "edge_new": round(mn["edge"], 4),
            "edge_drop": round(edge_drop, 3),
            "cx_orig": round(mo["cx"], 3), "cx_new": round(mn["cx"], 3),
            "cy_orig": round(mo["cy"], 3), "cy_new": round(mn["cy"], 3),
        })
        f = []
        if tv > TH_HIST_TV:
            f.append(f"色调分布漂移（TV={tv:.2f}）")
        if dh > TH_HUE:
            f.append(f"色相偏移 {dh:.0f}°")
        if abs(dl) > TH_LUMA:
            f.append(f"亮度{'变亮' if dl > 0 else '变暗'} {abs(dl):.0f}")
        if edge_drop > TH_EDGE_DROP:
            f.append(f"细节密度降 {edge_drop * 100:.0f}%（疑似道具丢失）")
        if max(abs(dcx), abs(dcy)) > TH_CENTROID:
            f.append(f"构图重心偏移 dx={dcx:+.2f} dy={dcy:+.2f}")
        flags.append((code, f))

        if not a.no_sheet:
            tmid = round((t0 + t1) / 2, 2)
            po_f = out / f"{code}_{tmid}_orig.jpg"
            pn_f = out / f"{code}_{tmid}_new.jpg"
            cmp_f = out / f"{code}_{tmid}_cmp.jpg"
            if frame(orig, tmid, po_f) and frame(new, tmid, pn_f) and hstack_pair(po_f, pn_f, cmp_f):
                pairs.append(cmp_f)

    csv_path = out / "02_metrics.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print("\n=== 逐镜指标（左原片 / 右成片）===")
    hdr = f"{'镜':<4}{'时长':>5} {'色相°':>13} {'亮度':>13} {'直方图TV':>9} {'边缘密度':>15} {'重心x':>13}"
    print(hdr)
    for r in rows:
        print(f"{r['shot']:<4}{r['dur']:>5.1f} "
              f"{r['hue_orig']:>5.0f}->{r['hue_new']:<5.0f} "
              f"{r['luma_orig']:>5.0f}->{r['luma_new']:<5.0f} "
              f"{r['hist_tv']:>9.2f} "
              f"{r['edge_orig']:>6.3f}->{r['edge_new']:<6.3f} "
              f"{r['cx_orig']:>5.2f}->{r['cx_new']:<5.2f}")

    print("\n=== 报警 ===")
    any_flag = False
    for code, f in flags:
        if f:
            any_flag = True
            print(f"  {code}: " + "；".join(f))
    if not any_flag:
        print("  （无）")

    md = ["# 验收清单", "",
          f"- 原片：`{orig}`", f"- 成片：`{new}`", "",
          "## 一、自动项（脚本算出）", "",
          "```", *lines, "```", "",
          "| 镜 | 时长 | 色相 | 亮度 | 直方图TV | 边缘密度 | 构图重心 | 报警 |",
          "|---|---|---|---|---|---|---|---|"]
    for r, (code, f) in zip(rows, flags):
        md.append(f"| {r['shot']} | {r['dur']}s | {r['hue_orig']:.0f}→{r['hue_new']:.0f} | "
                  f"{r['luma_orig']:.0f}→{r['luma_new']:.0f} | {r['hist_tv']:.2f} | "
                  f"{r['edge_orig']:.3f}→{r['edge_new']:.3f} | "
                  f"({r['cx_orig']:.2f},{r['cy_orig']:.2f})→({r['cx_new']:.2f},{r['cy_new']:.2f}) | "
                  f"{'；'.join(f) or '—'} |")
    md += ["", "## 二、人工项（必须逐条看，脚本判不了）", "",
           "- [ ] **道具清单**：原片该镜有的道具，成片是否一个不少、且没有多出的",
           "- [ ] **动作起止**：动作的起点与终点是否与原片一致（重点看有没有多出原片没有的动作）",
           "- [ ] **景别与构图**：机位、景别、主体大小是否一致",
           "- [ ] **光影方向**：主光方向、色温、软硬是否一致",
           "- [ ] **接触关系**：手与瓶、瓶与台面的接触点是否正确，投影是否合理",
           "- [ ] **跨镜人物一致性**：同一个人在各镜之间是否是同一张脸/同一个体型",
           "- [ ] **无中生有**：成片里有没有原片完全不存在的人、物、动作",
           "", "## 三、对照图", "",
           f"- `00_overview.jpg`（左原片 / 右成片，每镜取中点）"
           if pairs else "- （本次未生成对照图）", ""]
    (out / "03_checklist.md").write_text("\n".join(md), encoding="utf-8")

    if pairs and overview(pairs, out / "00_overview.jpg", a.cols):
        print(f"\n对照图 {out / '00_overview.jpg'}")
    print(f"指标   {csv_path}")
    print(f"清单   {out / '03_checklist.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
