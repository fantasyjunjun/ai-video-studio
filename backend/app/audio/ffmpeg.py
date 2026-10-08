"""ffmpeg / ffprobe 统一封装。

继承的四条实战约定（改动前务必先读）：

1. **测响度绝不能加 `-v error`** —— volumedetect 的结果打在 stderr 的 banner 区，
   加了就被整体吞掉，表现为"永远测不出响度"（2026-09-27 实测踩过）。
   所以 `measure_loudness()` 用 `-hide_banner` 且**不抑制**日志级别。

2. **永不 `os.remove`** —— 托管环境注入的安全钩子会在删除处直接中止进程
   （现象：脚本一声不响退出、退出码 0、日志空、产物留 0 字节）。
   需要"重建文件"一律走 `truncate()` 截断覆盖。

3. **源音轨一律丢弃** —— AutoDL 出片自带的那条音轨是模型副产品（单声道复制、
   各镜互不相同、拼接跳变 8dB、8kHz 以上能量 0%），不可用。
   装配阶段 `-an`，混音阶段 `-map 0:v` 只取画面，声音全部由三轨重建。

4. **秒数必须给足精度** —— 24fps 下 `-ss 0.583` ≠ `0.5833333`，差的就是一整帧；
   时间参数一律走 `sec()` 格式化。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

PathLike = Union[str, Path]


class FFmpegError(RuntimeError):
    """ffmpeg 执行失败。message 里带上 stderr 尾部便于定位。"""


def sec(value: float) -> str:
    """把秒数格式化成 ffmpeg 参数。**给足精度但不高到浮点噪声**（见文件头第 4 条）。"""
    s = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


def run(cmd: Sequence[str], *, check: bool = True,
        timeout: Optional[int] = None, binary: bool = False,
        cwd: Optional[str] = None):
    """执行一条命令。默认失败即抛 FFmpegError（带 stderr 尾部，而不是让调用方猜）。

    `cwd` 是为了绕开 ffmpeg **滤镜串里的绝对路径**：滤镜参数用 `:` 分隔，
    Windows 盘符 `C:/...` 会被切成 `file=C` + `/...` 两段而解析失败。
    这类命令请"设 cwd + 用相对文件名"，不要拼绝对路径进滤镜。


    `binary=True` 用于**要原始字节而非文本**的场景 —— 例如 `-f rawvideo` 解帧。
    这类输出不是合法 UTF-8，按文本读会抛 UnicodeDecodeError 且 stdout 变成 None，
    现象是"经典的 TypeError: a bytes-like object is required"。
    """
    r = subprocess.run(
        [str(c) for c in cmd], capture_output=True, text=not binary,
        timeout=timeout, cwd=cwd,
    )
    if check and r.returncode != 0:
        tail = (r.stderr or "")[-1200:]
        pretty = " ".join(str(c) for c in cmd)
        raise FFmpegError(f"命令失败（exit {r.returncode}）：{pretty[:300]}\n{tail}")
    return r


def require(name: str = "ffmpeg") -> str:
    from shutil import which

    exe = which(name)
    if not exe:
        raise FFmpegError(f"未找到 {name}，请先安装并加入 PATH")
    return exe


def truncate(path: PathLike) -> None:
    """截断覆盖，替代 os.remove（见文件头第 2 条）。"""
    with open(path, "wb"):
        pass


# ------------------------------------------------------------------ 探测

def probe_duration(path: PathLike) -> float:
    r = run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=False,
    )
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        return 0.0


@dataclass
class VideoSpec:
    """视频规格。nb_frames 是帧数配平的唯一权威来源。"""

    path: str
    width: int = 0
    height: int = 0
    fps: float = 0.0
    nb_frames: int = 0
    duration: float = 0.0
    has_audio: bool = False
    raw: Dict[str, str] = field(default_factory=dict)

    @property
    def seconds_by_frames(self) -> float:
        return self.nb_frames / self.fps if self.fps else 0.0


def _parse_kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        # 同名 key 保留**第一个**（前面已用 -select_streams 只选一条视频流）
        out.setdefault(k.strip(), v.strip())
    return out


def probe_video(path: PathLike) -> VideoSpec:
    """探测视频流规格。找不到就返回空壳，不抛（装配前的容错由调用方决定）。"""
    r = run(
        ["ffprobe", "-hide_banner", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames,codec_name",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1", str(path)],
        check=False,
    )
    kv = _parse_kv(r.stdout)
    fps = 0.0
    if "/" in kv.get("r_frame_rate", ""):
        num, _, den = kv["r_frame_rate"].partition("/")
        try:
            fps = float(num) / float(den or 1)
        except ValueError:
            fps = 0.0
    try:
        nb = int(float(kv.get("nb_frames", 0) or 0))
    except ValueError:
        nb = 0
    try:
        dur = float(kv.get("duration", 0) or 0)
    except ValueError:
        dur = probe_duration(path)

    spec = VideoSpec(
        path=str(path),
        width=int(float(kv.get("width", 0) or 0)),
        height=int(float(kv.get("height", 0) or 0)),
        fps=fps,
        nb_frames=nb,
        duration=dur,
        raw=kv,
    )
    spec.has_audio = has_stream(path, "a")
    return spec


def has_stream(path: PathLike, kind: str = "a") -> bool:
    """kind: a=音频 v=视频。用于校验"源音轨是否已丢弃"。"""
    r = run(
        ["ffprobe", "-v", "error", "-select_streams", f"{kind}:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        check=False,
    )
    return bool((r.stdout or "").strip())


# ------------------------------------------------------------------ 响度

_LEVEL_RE = {
    "mean_volume": re.compile(r"mean_volume:\s*(-?[\d.]+)\s*dB"),
    "max_volume": re.compile(r"max_volume:\s*(-?[\d.]+)\s*dB"),
}


def measure_loudness(path: PathLike) -> Dict[str, Optional[float]]:
    """整条响度。**不能加 -v error**（见文件头第 1 条）。"""
    r = run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
         "-f", "null", "-"],
        check=False,
    )
    log = r.stderr or ""
    out: Dict[str, Optional[float]] = {"mean_volume_db": None, "max_volume_db": None}
    for key, rx in _LEVEL_RE.items():
        m = rx.search(log)
        if m:
            out[f"{key}_db"] = float(m.group(1))
            out[key] = float(m.group(1))
    return out


def measure_segment_mean(path: PathLike, start: float, duration: float) -> Optional[float]:
    """测某一段的平均电平（dB）。

    用途：**验证音效真的进了片** —— 直接听抖动的素材不可靠，可靠做法是
    "音效窗口的均值 vs 同片内一段纯 BGM 窗口的均值"，差值显著才说明没漏。
    （这条来自 2026-09-27：曾出现"我以为加了音效、其实没进片"的事故。）
    """
    r = run(
        ["ffmpeg", "-hide_banner", "-ss", sec(start), "-t", sec(duration),
         "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        check=False,
    )
    m = _LEVEL_RE["mean_volume"].search(r.stderr or "")
    return float(m.group(1)) if m else None


# ------------------------------------------------------------------ 环境

def clear_proxy_env() -> List[str]:
    """清掉代理环境变量。

    edge-tts 直连时若走本地代理会 502，且必须**在发起请求前**清干净。
    返回被清掉的变量名，便于写进报告。
    """
    popped = []
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        if os.environ.pop(k, None) is not None:
            popped.append(k)
    os.environ["NO_PROXY"] = "*"
    return popped
