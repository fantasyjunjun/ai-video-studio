"""最终混音：画面 + 念白 + BGM + 镜头音效（ducking + limiter + 响度归一）。

从 skills/mix_final_av.py 移植，四轨分工**不可混**：

    念白  主导，是 BGM 闪避的唯一触发源；
    BGM   垫底，默认 -20dB 起，念白一说话就被压下去；
    音效  镜头自带（喷头 mist / 瓶盖 click / 玻璃 glass / 吸气 breath），
          按 '@秒数' 逐个落位，**不参与闪避侧链也不被闪避** —— 被压成
          "背景沙沙声"就白做了；
    视频源 只取画面。源音轨一律丢弃（-map 0:v）。

做五件事：
    1. 音效 adelay 落位合并成 sfx 总线；
    2. BGM 按 bgm_gain 压低；
    3. sidechaincompress 做念白触发闪避（说话时音乐下沉，说完回弹）；
    4. amix normalize=0 混音 + alimiter 防削顶；
    5. loudnorm I=-14 LUFS（短视频平台通用目标），AAC 192k，视频流直接 copy。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .ffmpeg import measure_loudness, probe_duration, probe_video, run, sec

# 念白/BGM/音效统一到同一格式，避免 amix 因格式不一致失败
_COMMON = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"


@dataclass
class MixResult:
    path: str
    layers: List[str] = field(default_factory=list)
    sfx_cues: List[Tuple[str, float]] = field(default_factory=list)
    duration: float = 0.0
    loudness: Dict[str, Optional[float]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "layers": self.layers,
            "sfx_cues": [{"path": p, "at": t} for p, t in self.sfx_cues],
            "duration": round(self.duration, 3),
            "loudness": self.loudness,
            "warnings": self.warnings,
        }


def parse_sfx_spec(spec: str) -> Tuple[str, float]:
    """`path/to/mist.wav@3.40` -> (path, 3.40)；未带 @ 视为 0.0 秒。"""
    if "@" not in spec:
        return spec, 0.0
    path, _, t = spec.rpartition("@")
    try:
        return path, float(t)
    except ValueError:
        return spec, 0.0


def build_mix_argv(
    video: str,
    voice: str,
    out: str,
    bgm: Optional[str] = None,
    sfx: Sequence[str] = (),
    bgm_gain: float = 0.30,
    sfx_gain: float = 0.90,
    lufs: float = -14.0,
) -> Tuple[List[str], List[str], List[Tuple[str, float]]]:
    """构造 ffmpeg 命令行。**拆出来是为了能离线断言图结构**（见 _smoke_p3）。

    返回 (argv, 滤镜节点列表, [(音效路径, 落位秒)])
    """
    cues = [parse_sfx_spec(s) for s in sfx]
    use_bgm = bool(bgm)

    # 缺文件早报错，别让 ffmpeg 用一句难懂的 stderr 收场
    for p, _t in cues:
        if not os.path.exists(p):
            raise FileNotFoundError(f"音效文件不存在: {p}")
    if use_bgm and not os.path.exists(bgm):  # type: ignore[arg-type]
        raise FileNotFoundError(f"BGM 文件不存在: {bgm}")

    inputs = ["-i", str(video), "-i", str(voice)]
    if use_bgm:
        inputs += ["-i", str(bgm)]
    sfx_base = 3 if use_bgm else 2
    for p, _t in cues:
        inputs += ["-i", p]

    parts: List[str] = []
    mixed: List[str] = []
    if use_bgm:
        parts.append(f"[1:a]{_COMMON},asplit=2[vox][sc]")
    else:
        parts.append(f"[1:a]{_COMMON}[vox]")

    # ---- 音效总线：逐个 adelay 落位后求和（不掉权重、不进侧链）----
    if cues:
        for i, (_p, t) in enumerate(cues):
            ms = int(round(max(0.0, t) * 1000))
            delay = f"adelay={ms}|{ms}" if ms > 0 else "anull"
            parts.append(f"[{sfx_base + i}:a]{_COMMON},volume={sfx_gain},{delay}[s{i}]")
        if len(cues) == 1:
            parts.append("[s0]anull[sfxall]")
        else:
            labels = "".join(f"[s{i}]" for i in range(len(cues)))
            parts.append(f"{labels}amix=inputs={len(cues)}:duration=longest:"
                         f"normalize=0[sfxall]")
        mixed.append("[sfxall]")

    if use_bgm:
        parts.append(f"[2:a]{_COMMON},volume={bgm_gain}[bg]")
        parts.append("[bg][sc]sidechaincompress=threshold=0.02:ratio=8:"
                     "attack=20:release=420:makeup=1[duck]")
        mixed.append("[duck]")

    n = 1 + len(mixed)
    parts.append(f"[vox]{''.join(mixed)}amix=inputs={n}:duration=first:normalize=0,"
                 f"alimiter=limit=0.95,loudnorm=I={lufs}:TP=-1.5:LRA=11[out]")

    argv = (["ffmpeg", "-y", "-v", "error"] + inputs
            + ["-filter_complex", ";".join(parts),
               "-map", "0:v", "-map", "[out]",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
               "-shortest", str(out)])
    return argv, parts, cues


def mix_final(
    video: str,
    voice: str,
    out: str,
    bgm: Optional[str] = None,
    sfx: Sequence[str] = (),
    bgm_gain: float = 0.30,
    sfx_gain: float = 0.90,
    lufs: float = -14.0,
) -> MixResult:
    """混音出成片，并**顺手校验**（发赶/漏音轨这些事故大多是"没校验"造成的）。"""
    argv, _parts, cues = build_mix_argv(
        video, voice, out, bgm=bgm, sfx=list(sfx),
        bgm_gain=bgm_gain, sfx_gain=sfx_gain, lufs=lufs)

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    run(argv)

    res = MixResult(path=str(out), sfx_cues=[(str(p), t) for p, t in cues])
    res.duration = probe_duration(out)
    res.layers = ["念白"] + (["BGM"] if bgm else []) + ([f"{len(cues)} 条音效"] if cues else [])

    v = probe_video(out)
    src = probe_video(video)
    if src.fps and v.fps and abs(src.fps - v.fps) > 1e-6:
        res.warnings.append(f"帧率变化 {src.fps} → {v.fps}（应为 copy 直传）")
    if src.nb_frames and v.nb_frames and v.nb_frames != src.nb_frames:
        res.warnings.append(
            f"帧数变化 {src.nb_frames} → {v.nb_frames}（-shortest 可能裁掉了尾巴）")

    # 响度不能加 -v error（会被吞），probe 用 measure_loudness
    res.loudness = measure_loudness(out)
    mean = res.loudness.get("mean_volume_db")
    if mean is not None and mean > -8:
        res.warnings.append(f"整片平均电平 {mean:.1f}dB 偏高，容易触发平台二次归一")
    return res
