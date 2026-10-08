"""逐帧遮罩生成（遮挡重绘 POC 用）。

给 VACE 这类"遮罩内重绘"的工作流喂输入：白＝要重绘（人 + 产品），黑＝逐像素保留。

模型：U^2-Net 系列 ONNX。
  - 人物  `u2net_human_seg.onnx`（人像专用，边界比通用显著性好）
  - 产品  `u2net.onnx`（通用显著目标，静物镜里最显著的就是瓶子）
两者求并集，再膨胀 + 时序多数滤波。

为什么必须膨胀：手拿瓶、瓶贴脸这类交互，分割边界往往只盖住"实体本身"，
不盖"接触区"。漏掉接触区就会出现「瓶子重绘了、手还是原片的手」——
这是这条路线最刺眼的穿帮。宁可多盖一点。

输出（--out 目录）：
  <name>.mp4           遮罩视频（白底黑遮罩），直接喂工作流的 video 入参
  frames/<name>_%04d.png  逐帧 PNG（需要序列入参的工作流用）
  preview.jpg          每镜中点的「原帧 | 遮罩 | 叠加」三联预览

用法：
    python scripts/make_mask.py --video <原片> --out _masks/p13 \\
        --targets person,product --slice 13-15 --dilate 9
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy.ndimage import binary_dilation, binary_closing, gaussian_filter
from PIL import Image

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.audio.ffmpeg import require  # noqa: E402

FF = require("ffmpeg")
FP = FF.replace("ffmpeg", "ffprobe")

MODELS_DIR = BASE / "_models"
MODEL_URLS = {
    "person": ("u2net_human_seg.onnx",
               "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net_human_seg.onnx"),
    "product": ("u2net.onnx",
                "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx"),
}
INPUT_SIZE = 320
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def ensure_model(kind: str) -> Path:
    fn, url = MODEL_URLS[kind]
    dest = MODELS_DIR / fn
    if dest.exists() and dest.stat().st_size > 1_000_000:
        return dest
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"下载模型 {fn} …（首次较慢）")
    r = subprocess.run(["curl", "-sSL", "--max-time", "1800", "-o", str(dest), url])
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size < 1_000_000:
        raise RuntimeError(f"模型下载失败：{url}")
    print(f"  {fn}  {dest.stat().st_size // 1024 // 1024} MB")
    return dest


def load_frames(video: Path, w: int, h: int) -> tuple[np.ndarray, float]:
    pr = subprocess.run([FP, "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0",
                         str(video)], capture_output=True, text=True)
    num, _, den = (pr.stdout.strip() or "24/1").partition("/")
    fps = float(num) / float(den or 1)
    raw = subprocess.run([FF, "-v", "error", "-i", str(video),
                          "-vf", f"scale={w}:{h}", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    per = w * h * 3
    if not raw or len(raw) % per:
        raise RuntimeError(f"解码失败或尺寸不符：{video}")
    n = len(raw) // per
    return np.frombuffer(raw, np.uint8)[: n * per].reshape(n, h, w, 3), fps


class Segmenter:
    def __init__(self, kind: str):
        import onnxruntime as ort
        self.path = ensure_model(kind)
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        # 关掉 BFC 内存池：默认按**峰值**缓存整块显存/内存且不归还，
        # 两个会话叠加在 8GB 机器上必 OOM（本机实测 bad allocation）。
        opts.enable_cpu_mem_arena = False
        # 限线程：多进程并行时线程数×会话数会把内存吃穿。
        opts.intra_op_num_threads = 2
        self.sess = ort.InferenceSession(str(self.path), sess_options=opts,
                                         providers=["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name
        self.names = [o.name for o in self.sess.get_outputs()]

    def __call__(self, frames: np.ndarray) -> np.ndarray:
        """(T,H,W,3) uint8 -> (T,H,W) float32 0-1

        逐帧 resize + 逐帧 run：**不要**先堆出整段 (T,320,320,3) 的中间张量
        （120 帧就是 147MB，叠上两个会话直接 OOM）。峰值降到单帧 1.2MB。
        """
        t, h, w, _ = frames.shape
        out = np.zeros((t, h, w), np.float32)
        for i in range(t):
            small = np.asarray(Image.fromarray(frames[i])
                               .resize((INPUT_SIZE, INPUT_SIZE), Image.BILINEAR))
            x = small.astype(np.float32) / 255.0
            x = (x - MEAN) / STD
            x = x.transpose(2, 0, 1)[None]
            res = self.sess.run(self.names, {self.input_name: x})
            d = np.asarray(res[0])[0, 0]
            lo, hi = float(d.min()), float(d.max())
            d = (d - lo) / max(hi - lo, 1e-6)
            out[i] = np.asarray(Image.fromarray((d * 255).astype(np.uint8))
                                .resize((w, h), Image.BILINEAR), dtype=np.float32) / 255.0
        return out


def temporal_majority(mask: np.ndarray, k: int = 3) -> np.ndarray:
    """时序多数滤波：一个像素要在窗口内拿到**多数票**才算前景，去掉单帧跳变。

    逐帧独立分割会闪 —— 这一层是"遮罩也闪烁"的最小成本解。
    阈值用 k//2+1：k=3 时仍是 2（与历史行为一致），k=5 时是 3（真多数）。
    """
    if mask.shape[0] < 3 or k < 3:
        return mask
    need = max(2, k // 2 + 1)
    out = mask.copy()
    half = k // 2
    for i in range(mask.shape[0]):
        a = max(0, i - half)
        b = min(mask.shape[0], i + half + 1)
        win = mask[a:b]
        out[i] = win.sum(axis=0) >= need
    return out


def temporal_union(mask: np.ndarray, k: int = 3) -> np.ndarray:
    """时序并集：窗口内**任一帧**是前景，当前帧就算前景 —— 专治"漏主体"。

    与 `temporal_majority` 方向相反，两者治的病不一样：
      - majority：某帧比邻帧多出一块（**多**了）→ 多数票把它投票掉。
      - union：某帧比邻帧少了一块（**少**了，典型是深色物体被模型判成背景）
                → 并集把它从邻帧补回来。

    遮挡重绘里"漏主体"比"多一块"致命得多：漏了瓶盖就会成片出现
    「瓶身重绘成新品牌、瓶盖还是原片的」——事后修不掉。
    实测 A1（静物）用 K=5 把单帧突跳 5.75pt → 2.55pt，覆盖率只 +0.4pt。
    """
    if mask.shape[0] < 3 or k < 3:
        return mask
    out = mask.copy()
    half = k // 2
    for i in range(mask.shape[0]):
        a = max(0, i - half)
        b = min(mask.shape[0], i + half + 1)
        out[i] = mask[a:b].any(axis=0)
    return out


def build_mask(frames: np.ndarray, targets: list[str], dilate: int,
               thresh: float, smooth: int = 3, fill: int = 0) -> np.ndarray:
    import gc

    t, h, w, _ = frames.shape
    acc = np.zeros((t, h, w), np.float32)
    for kind in targets:
        seg = Segmenter(kind)
        print(f"  分割 {kind} …")
        p = seg(frames)
        print(f"    {kind} 前景占比 {float((p > thresh).mean()) * 100:.1f}%")
        acc = np.maximum(acc, p)
        # 一个模型一个模型地载、算完立刻放：两个 176MB 会话同时驻留会在本机 OOM。
        del seg, p
        gc.collect()
    m = acc > thresh
    del acc
    m = temporal_majority(m, smooth)
    if fill >= 3:
        before = float(m.mean()) * 100
        m = temporal_union(m, fill)
        print(f"  时序并集补漏 K={fill}  覆盖率 {before:.1f}% -> {float(m.mean()) * 100:.1f}%"
              f"（补回被漏掉的主体，代价极小）")
    # 先闭运算补小洞，再膨胀吃下接触区（逐帧做，避免整段 copy 翻倍内存）
    m = np.stack([binary_closing(f, iterations=2) for f in m])
    if dilate > 0:
        m = np.stack([binary_dilation(f, iterations=dilate) for f in m])
    soft = np.stack([gaussian_filter(f.astype(np.float32), sigma=1.5) for f in m])
    return (soft > 0.4).astype(np.uint8) * 255


def write_outputs(mask: np.ndarray, frames: np.ndarray, fps: float,
                  out: Path, name: str) -> None:
    t, h, w = mask.shape
    out.mkdir(parents=True, exist_ok=True)
    raw = np.repeat(mask[:, :, :, None], 3, axis=3).tobytes()
    r = subprocess.run([FF, "-y", "-v", "error", "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", f"{fps}",
                        "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-crf", "12", str(out / f"{name}.mp4")],
                       input=raw)
    print(f"  遮罩视频 {'ok' if r.returncode == 0 else 'FAIL'}  {out / (name + '.mp4')}")

    fdir = out / "frames"
    fdir.mkdir(exist_ok=True)
    for i in range(t):
        Image.fromarray(mask[i]).save(fdir / f"{name}_{i:04d}.png")
    print(f"  逐帧 PNG {t} 张  {fdir}")


def preview(mask: np.ndarray, frames: np.ndarray, out: Path, name: str,
            idxs: list[int]) -> None:
    """idxs 是**片段内的帧号**（不是原片时间），调用方负责换算。"""
    tiles = []
    for i in idxs:
        i = min(mask.shape[0] - 1, max(0, i))
        f = frames[i]
        m = mask[i]
        over = (f * 0.45).astype(np.uint8)
        over[..., 0] = np.maximum(over[..., 0], m)      # 遮罩区染红
        tiles.append(np.concatenate([f, np.repeat(m[:, :, None], 3, 2), over], axis=1))
    sheet = np.concatenate(tiles, axis=0)
    p = out / f"{name}_preview.jpg"
    Image.fromarray(sheet).save(p, quality=92)
    print(f"  预览（每镜「原帧 | 遮罩 | 叠加」）帧号 {[min(mask.shape[0] - 1, max(0, i)) for i in idxs]}")
    print(f"    {p}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True, help="原片")
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", default="mask")
    ap.add_argument("--targets", default="person,product")
    ap.add_argument("--slice", default="", help="起-止（秒），如 13-15；不填取全片")
    ap.add_argument("--dilate", type=int, default=9, help="膨胀像素数（吃下接触区）")
    ap.add_argument("--thresh", type=float, default=0.5)
    ap.add_argument("--smooth", type=int, default=3, metavar="K",
                    help="时序多数滤波窗口。3=默认；**人物镜建议 5**（实测 IoU 0.978→0.983、"
                         "翻转 1.67%%→1.30%%）；7 还有小幅收益但边际递减。")
    ap.add_argument("--fill-missing", type=int, default=0, metavar="K",
                    help="时序并集补漏窗口（>=3 生效）。**治「漏主体」**：窗口内任一帧是前景就补回来。"
                         "静物镜建议 5 —— 深色物体（黑瓶盖）常被模型判成背景，"
                         "漏了就会成片出现「瓶身换了品牌、瓶盖还是原片的」。"
                         "实测 A1 突跳 5.75pt→2.55pt，覆盖率仅 +0.4pt。")
    ap.add_argument("--preview-at", default="",
                    help="预览时间点（**原片绝对时间**，逗号分隔）。"
                         "注意：本脚本会按 --slice 换算成片段内帧号 —— "
                         "不换算的话切片后所有时间点都会落到最后一帧（踩过）。")
    ap.add_argument("--preview-only", action="store_true",
                    help="只重出预览：复用 frames/ 里已有的 PNG，不重跑分割（省几分钟）")
    ap.add_argument("--smooth-only", type=int, default=0, metavar="K",
                    help="只加厚时序平滑：对 frames/ 里已有遮罩再跑一次窗口 K 的"
                         "时序多数滤波并重写产物，不重跑分割。K=5 能明显压抖动，几秒出结果。")
    a = ap.parse_args()

    src = Path(a.video) if Path(a.video).is_absolute() else (BASE / a.video)
    if not src.exists():
        print(f"原片不存在：{src}")
        return 2
    out = Path(a.out) if Path(a.out).is_absolute() else (BASE / a.out)

    pr = subprocess.run([FP, "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=width,height", "-of", "csv=p=0",
                         str(src)], capture_output=True, text=True)
    w, h = (int(v) for v in pr.stdout.strip().split(",")[:2])
    print(f"原片 {src}  {w}x{h}")

    clip = src
    t0 = 0.0
    if a.slice:
        s, _, e = a.slice.partition("-")
        t0 = float(s)
        # 片段名带上 --name：否则两个镜并行跑会互相覆盖同一个 _clip.mp4
        tmp = out / f"_{a.name}_clip.mp4"
        out.mkdir(parents=True, exist_ok=True)
        subprocess.run([FF, "-y", "-v", "error", "-ss", s, "-i", str(src),
                        "-t", str(float(e) - t0), "-c:v", "libx264", "-crf", "12",
                        "-an", str(tmp)], check=False)
        clip = tmp
        print(f"取片段 {a.slice}s -> {tmp}")

    frames, fps = load_frames(clip, w, h)
    print(f"帧数 {frames.shape[0]}  fps {fps}")

    # 预览时间点是**原片绝对时间**，要减掉切片起点才是片段内帧号。
    picks = ([float(x) for x in a.preview_at.split(",") if x.strip()]
             if a.preview_at else [t0 + round(frames.shape[0] / fps / 2, 2)])
    idxs = [int(round((p - t0) * fps)) for p in picks]

    if a.smooth_only:
        fdir = out / "frames"
        files = sorted(fdir.glob(f"{a.name}_*.png"))
        if not files:
            print(f"没有可复用的遮罩帧：{fdir}/{a.name}_*.png")
            return 2
        m = np.stack([np.asarray(Image.open(p).convert("L")) > 127 for p in files])
        before = float((m > 0).mean()) * 100
        m = temporal_majority(m, a.smooth_only)
        after = float((m > 0).mean()) * 100
        print(f"复用已有遮罩 {m.shape[0]} 帧，时序多数滤波窗口 K={a.smooth_only}")
        print(f"  覆盖率 {before:.1f}% -> {after:.1f}%（滤波会略微收窄边界，正常）")
        if a.fill_missing >= 3:
            b2 = float(m.mean()) * 100
            m = temporal_union(m, a.fill_missing)
            print(f"  时序并集补漏 K={a.fill_missing}  覆盖率 {b2:.1f}% -> "
                  f"{float(m.mean()) * 100:.1f}%")
        mask = m.astype(np.uint8) * 255
        write_outputs(mask, frames, fps, out, a.name)
        preview(mask, frames, out, a.name, idxs)
        return 0

    if a.preview_only:
        fdir = out / "frames"
        files = sorted(fdir.glob(f"{a.name}_*.png"))
        if not files:
            print(f"没有可复用的遮罩帧：{fdir}/{a.name}_*.png")
            return 2
        mask = np.stack([np.asarray(Image.open(p).convert("L")) for p in files])
        print(f"复用已有遮罩 {mask.shape[0]} 帧（跳过分割）")
        preview(mask, frames, out, a.name, idxs)
        return 0

    targets = [x.strip() for x in a.targets.split(",") if x.strip()]
    for t in targets:
        if t not in MODEL_URLS:
            print(f"不支持的 target：{t}（可选 {list(MODEL_URLS)}）")
            return 2

    print("生成遮罩 …")
    mask = build_mask(frames, targets, a.dilate, a.thresh, a.smooth, a.fill_missing)
    cover = float((mask > 0).mean()) * 100
    print(f"遮罩覆盖率 {cover:.1f}%（经验区间 20-55%；>70% 说明等于整帧重绘，失去意义）")

    write_outputs(mask, frames, fps, out, a.name)

    preview(mask, frames, out, a.name, idxs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
