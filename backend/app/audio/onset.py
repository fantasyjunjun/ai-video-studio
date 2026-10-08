"""实测"结果性元素"（雾/汽雾/烟/飞溅）在镜头里的**首次出现时刻**。

为什么必须实测：
    模型对结果性元素有**固有前摇**，而且**前摇不是常数** —— 同一个工作流、
    同一个 4s 请求，随提示词变化：喷空气 0.92s / "雾在第一帧已存在" 0.83s /
    喷颈侧 0.72s / 加"短促后停" ≈1.10s。**每次换提示词都必须重新实测**，
    不能复用上次的结果。

为什么"生成更长再裁掉前摇"不成立：
    请求更长只会把前摇**等比放大**（4s→0.92s，5s→1.83s），所以加时长无用。
    正确做法是：**接受前摇，装配时用 `-ss` 把它切掉**，并让音效按**裁切后的
    真实起点**落位。

原理：
    在 ROI 里统计逐帧平均亮度。雾是明亮且低对比的软团，出现后该区域亮度
    **持续抬升**（不是单帧噪声）。判据取"8 帧窗口内的持续跃升"，因为缓慢
    运镜造成的亮度漂移分摊到窗口只有 ~0.02，而雾的出现是 ~0.10，差一个量级。

⚠️ 本工具是**辅助，不是判据**。ROI 需按镜头构图给（180° 转身会把雾拉到画面
另一侧，默认 ROI 会漏检）。结论应当抽帧目视复核。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .ffmpeg import run

# 480×864 竖屏下雾通常出现在喷嘴右上方
DEFAULT_ROI: Tuple[float, float, float, float] = (0.55, 0.22, 0.95, 0.58)

MIN_RISE = 0.05  # 低于此值视作运镜漂移，不算元素出现


@dataclass
class OnsetResult:
    """检测结果。`onset_sec` 为 None 表示未检出（未必是坏事：该镜可能确实没雾）。"""

    path: str
    frames: int = 0
    fps: float = 24.0
    roi: Tuple[float, float, float, float] = DEFAULT_ROI
    onset_sec: Optional[float] = None
    onset_frame: Optional[int] = None
    rise: float = 0.0
    curve: List[float] = field(default_factory=list)
    candidates: List[Tuple[float, float]] = field(default_factory=list)
    note: str = ""

    @property
    def detected(self) -> bool:
        return self.onset_sec is not None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "frames": self.frames,
            "fps": self.fps,
            "roi": list(self.roi),
            "onset_sec": self.onset_sec,
            "onset_frame": self.onset_frame,
            "rise": round(self.rise, 4),
            "candidates": [[round(t, 3), round(r, 4)] for t, r in self.candidates],
            "detected": self.detected,
            "note": self.note,
        }


def _frames_gray(path: str, w: int = 160, h: int = 288):
    """把整条视频解成灰度帧数组 (n, h, w)。"""
    try:
        import numpy as np
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("detect_onset 需要 numpy：pip install numpy") from e

    r = run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf",
         f"scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
        check=False, binary=True,
    )
    buf = np.frombuffer(r.stdout, dtype=np.uint8)
    n = len(buf) // (w * h)
    if n == 0:
        raise RuntimeError(f"无法解码视频帧：{path}")
    return buf[: n * w * h].reshape(n, h, w).astype(np.float32) / 255.0


def detect_onset(
    path: str,
    roi: Tuple[float, float, float, float] = DEFAULT_ROI,
    fps: float = 24.0,
    min_rise: float = MIN_RISE,
    top_n: int = 3,
) -> OnsetResult:
    """检测元素首次出现时刻。

    `candidates` 会返回前 top_n 个候选跃升（按强度降序）。这是给"模型把中期
    动作误判成最强跃升"留的后手 —— 例如 S05 B1_v2 里真正的喷发在 t≈0.45s，
    但转身进灯区（t≈2.6s）的跃升更强，candidates 让人能看到两者并自行选择。
    """
    import numpy as np

    f = _frames_gray(path)
    H, W = f.shape[1], f.shape[2]
    n = f.shape[0]
    x0, y0, x1, y1 = (int(roi[0] * W), int(roi[1] * H),
                      int(roi[2] * W), int(roi[3] * H))
    s = f[:, y0:y1, x0:x1].mean(axis=(1, 2))

    k = np.ones(3) / 3
    s = np.convolve(s, k, mode="same")

    # 跳过起幅：首帧常偏暗，会在 t≈0 伪造一次跃升
    skip = max(2, int(0.25 * fps))

    scored: List[Tuple[int, float]] = []
    for i in range(skip, n - 8):
        after = s[i + 2: i + 8].mean()
        before = s[max(skip, i - 6): max(skip + 1, i - 1)].mean()
        scored.append((i, float(after - before)))

    res = OnsetResult(path=str(path), frames=n, fps=fps, roi=roi)
    res.curve = [round(float(v), 4) for v in s[:: max(1, n // 20)][:20]]

    if not scored:
        res.note = "视频过短，无法计算跃升"
        return res

    ranked = sorted(scored, key=lambda x: -x[1])
    res.candidates = [(round(i / fps, 3), round(float(v), 4))
                      for i, v in ranked[: max(1, top_n)]]

    best_i, best = ranked[0]
    res.rise = best
    if best < min_rise:
        res.note = (
            f"最大持续跃升 {best:+.4f} < 阈值 {min_rise:g} → 未检出结果性元素"
            f"（该镜可能没有雾/烟，或 ROI 不对；必要时应抽帧目视复核）"
        )
        return res

    res.onset_frame = int(best_i)
    res.onset_sec = round(best_i / fps, 3)
    res.note = f"最大持续跃升 {best:+.4f} @ 帧 {best_i} → t={res.onset_sec:.2f}s"
    return res
