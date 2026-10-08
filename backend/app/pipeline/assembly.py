"""帧精确装配：多个镜头归一到同一规格并按**帧**配平。

从 skills/concat_shots.py 移植，继承这几条硬经验：

1. **出片总是比请求值略长**（按帧取整），所以每个片段必须先 `-t` 精确裁剪再拼，
   否则念白与画面会错位 0.5s 以上（V02 事故）。
2. **`-ss` 必须吸附 1/fps 帧网格**：24fps 下写 `0.583` 会被当成 `0.583s` 落在错误帧，
   每镜少一帧。`snap_to_grid()` 干的就是这件事。
3. **总时长按帧数配平**：15s = 90+72+96+102 = 360 帧，而不是按秒数相加 —— 秒数相加
   会因为每个商不整而攒出偏差。交付前必须 ffprobe 校验 nb_frames。
4. **`@起始秒` 用于切掉模型前摇**：结果性元素（雾/烟）的前摇随时长等比放大，
   "生成更长再裁掉"不成立，只能接受前摇再用剪辑切掉，音效按**切完之后**的真实起点落位。
5. **源音轨一律丢弃**（`-an`）：AutoDL 自带音轨是模型副产品，见 audio/ffmpeg.py 文件头。
6. **中间目录不要放系统临时目录**：`tempfile.mkdtemp` 在本环境会被沙箱拦下，
   一律用项目内的 work/tmp 子目录。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from ..audio.ffmpeg import probe_duration, probe_video, run, sec

# 允许的帧数误差（0 = 严格）
TOLERANCE_FRAMES = 0


@dataclass
class ShotInput:
    """镜头输入片段。"""

    path: str
    frames: int = 0               # 目标帧数（优先按帧，避免秒数攒偏差）
    start: float = 0.0            # 起始秒（用于切掉前摇）
    code: str = ""                # A1 / B1 …

    @property
    def duration_sec(self) -> float:
        return self.frames / 24.0


@dataclass
class AssemblePlan:
    segments: List[ShotInput] = field(default_factory=list)
    fps: float = 24.0
    width: int = 480
    height: int = 864
    total_frames: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def total_sec(self) -> float:
        return self.total_frames / self.fps if self.fps else 0.0

    def to_dict(self) -> dict:
        return {
            "fps": self.fps,
            "size": [self.width, self.height],
            "total_frames": self.total_frames,
            "total_sec": round(self.total_sec, 3),
            "warnings": self.warnings,
            "segments": [
                {"code": s.code, "path": s.path, "frames": s.frames,
                 "start": round(s.start, 6),
                 "start_snapped": bool(abs(s.start - snap_to_grid(s.start, self.fps)) < 1e-9)}
                for s in self.segments
            ],
        }


@dataclass
class AssembleResult:
    path: str = ""
    nb_frames: int = 0
    duration: float = 0.0
    expected_frames: int = 0
    has_audio: bool = False
    ok: bool = True
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "nb_frames": self.nb_frames,
            "expected_frames": self.expected_frames,
            "duration": round(self.duration, 3),
            "frame_exact": self.nb_frames == self.expected_frames,
            "has_audio": self.has_audio,
            "ok": self.ok,
            "warnings": self.warnings,
        }


def snap_to_grid(value: float, fps: float = 24.0) -> float:
    """把秒数吸附到 1/fps 帧网格。

    为什么要它：出片常常要在 0.42s、0.58s 这种非整数帧位置下刀，直接把两位小数
    喂给 ffmpeg 会落到相邻帧，**每镜差 1 帧**，五镜下来片长就配不平了。
    """
    if fps <= 0:
        return float(value)
    return round(float(value) * fps) / fps


def parse_shot_spec(spec: str) -> Tuple[str, Optional[float], Optional[float]]:
    """解析 `路径[:时长秒][@起始秒]` -> (path, dur|None, start|None)。

    从**右侧**分割，否则 Windows 盘符 `C:` 会被当成时长分隔符。
    """
    start = None
    if "@" in spec:
        spec, _, s = spec.rpartition("@")
        try:
            start = float(s)
        except ValueError:
            raise ValueError(f"起始秒无法解析：{spec}@{s}") from None

    path, sep, tail = spec.rpartition(":")
    dur = None
    if sep and tail.strip():
        try:
            dur = float(tail)
        except ValueError:
            path = spec      # 冒号不是时长分隔符（如 C:\\...）
    return path.strip(), dur, start


def build_plan(
    specs: Sequence[str],
    *,
    fps: float = 24.0,
    width: int = 480,
    height: int = 864,
    total_frames: Optional[int] = None,
    codes: Optional[Sequence[str]] = None,
) -> AssemblePlan:
    """由 `路径[:时长][@起始]` 列表构造配平计划。**帧数优先**。"""
    plan = AssemblePlan(fps=fps, width=width, height=height)
    if not specs:
        plan.warnings.append("没有提供任何片段")
        return plan

    for i, spec in enumerate(specs):
        path, dur, start = parse_shot_spec(spec)
        path = path.strip()
        if start is None:
            start = 0.0
        snapped = snap_to_grid(start, fps)
        if abs(snapped - start) > 1e-9:
            plan.warnings.append(
                f"#{i + 1} 起始 {start}s 不在 {fps}fps 网格上，已吸附到 {snapped:.6f}s")

        src_dur = probe_duration(path)
        if src_dur <= 0:
            plan.warnings.append(f"#{i + 1} 无法读取时长：{path}")

        frames = round(dur * fps) if dur else None
        if frames is None:
            frames = max(0, round((src_dur - snapped) * fps))

        if src_dur > 0 and snapped + frames / fps > src_dur + 1 / fps:
            plan.warnings.append(
                f"#{i + 1} 请求 {frames} 帧（起点 {snapped:.3f}s）超出源片 {src_dur:.3f}s，"
                f"实际只能取 {max(0, round((src_dur - snapped) * fps))} 帧")
            frames = max(0, round((src_dur - snapped) * fps))

        plan.segments.append(ShotInput(
            path=path, frames=frames, start=snapped,
            code=codes[i] if codes and i < len(codes) else f"S{i + 1}"))

    plan.total_frames = sum(s.frames for s in plan.segments)
    if total_frames is not None and plan.total_frames != total_frames:
        plan.warnings.append(
            f"帧数配平不一致：实际 {plan.total_frames} 帧，目标 {total_frames} 帧"
            f"（差 {plan.total_frames - total_frames:+d}）")
    return plan


def assemble(plan: AssemblePlan, out: str, *, tmp_root: str) -> AssembleResult:
    """按计划归一 + 拼接。**产出无音轨**（源音轨按约定丢弃）。

    `tmp_root` 必须是项目内的目录：本环境里走到系统临时目录会被沙箱拦下。
    """
    from uuid import uuid4

    res = AssembleResult(expected_frames=plan.total_frames)
    if not plan.segments:
        res.ok = False
        res.warnings.append("计划为空")
        return res

    tmp = os.path.join(tmp_root, "asm_" + uuid4().hex[:8])
    os.makedirs(tmp, exist_ok=True)

    items: List[str] = []
    vf = (f"scale={plan.width}:{plan.height}:force_original_aspect_ratio=increase,"
          f"crop={plan.width}:{plan.height},fps={plan.fps}")

    for i, s in enumerate(plan.segments, 1):
        dst = os.path.join(tmp, f"p{i:03d}.mp4")
        seek = ["-ss", sec(s.start)] if s.start > 0 else []
        dur = s.frames / plan.fps
        run(["ffmpeg", "-y", "-v", "error", *seek, "-i", s.path,
             "-t", sec(dur), "-vf", vf, "-an",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-pix_fmt", "yuv420p", dst])
        items.append(dst)

    listfile = os.path.join(tmp, "list.txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for p in items:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")

    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", listfile, "-c", "copy", out])

    spec = probe_video(out)
    res.path = str(out)
    res.nb_frames = spec.nb_frames
    res.duration = spec.duration
    res.has_audio = spec.has_audio
    if spec.has_audio:
        res.warnings.append("产出竟然带音轨 —— 装配阶段应 -an 丢弃源音轨")
        res.ok = False
    if plan.total_frames and abs(spec.nb_frames - plan.total_frames) > TOLERANCE_FRAMES:
        res.warnings.append(
            f"帧数不符：期望 {plan.total_frames}，实际 {spec.nb_frames}")
        res.ok = False
    return res
