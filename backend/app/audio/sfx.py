"""镜头音效合成（零版权风险，纯 numpy/scipy）。

从 skills/make_sfx.py 移植，**默认必须是 mist（纯水汽）而不是 spray**。

为什么 mist 才是默认 —— 两条量化判据（2026-09-27 用户实测反馈"啪嗒声要删"）：

    | 判据                          | spray（旧版） | mist（默认） |
    |-------------------------------|---------------|--------------|
    | 起音 10ms 内峰值              | −15.3 dB      | −40.4 dB     |
    | 前 10ms 谱质心相对整体的偏移   | +177 Hz       | −313 Hz      |

    **前 10ms 谱质心偏高 = 宽带瞬态 = 听感"咔哒"**。根因是 spray 里那条为"阀门开启"
    刻意加的 8ms 宽带冲击（`bandpass(1200-12000) * exp(-t/0.008)`，权重 0.50）。
    真实雾化香水喷出时没有这个成分，所以 mist 完全不含任何冲击项。

排查"听感有咔哒"时的顺序（别乱猜）：
    1. 排除源音轨（AutoDL 自带音轨必须丢弃，见 ffmpeg.py 文件头第 3 条）；
    2. 排除音效文件有没有真的进片（比对窗口电平）；
    3. 最后才查合成代码 —— 绝大多数情况是用错了 kind。
"""

from __future__ import annotations

import os
import wave
from typing import Dict, List, Optional, Tuple

SR = 48000  # 与成片采样率一致，混音可直接吃
DEFAULT_SEED = 20260927

_NP = None
_SIG = None


def _np():
    global _NP
    if _NP is None:
        try:
            import numpy as np
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "合成音效需要 numpy + scipy："
                "pip install numpy scipy -i https://mirrors.aliyun.com/pypi/simple/"
            ) from e
        _NP = np
    return _NP


def _sig():
    global _SIG
    if _SIG is None:
        try:
            from scipy import signal
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("合成音效需要 scipy") from e
        _SIG = signal
    return _SIG


def _rng(seed: int):
    return _np().random.default_rng(seed)


def _bandpass(x, lo: float, hi: float, order: int = 4):
    signal = _sig()
    sos = signal.butter(order, [lo / (SR / 2), hi / (SR / 2)], btype="band", output="sos")
    return signal.sosfilt(sos, x)


def _lowpass(x, hi: float, order: int = 4):
    signal = _sig()
    sos = signal.butter(order, hi / (SR / 2), btype="low", output="sos")
    return signal.sosfilt(sos, x)


def _highpass(x, lo: float, order: int = 4):
    signal = _sig()
    sos = signal.butter(order, lo / (SR / 2), btype="high", output="sos")
    return signal.sosfilt(sos, x)


def _exp_env(n: int, attack: float, tau: float):
    np = _np()
    t = np.arange(n) / SR
    env = np.exp(-t / tau)
    na = max(1, int(attack * SR))
    env[:na] *= np.linspace(0.0, 1.0, na)
    return env


def _norm(x, peak_db: float):
    np = _np()
    peak = float(np.max(np.abs(x)))
    if peak <= 0:
        peak = 1.0
    return x / peak * (10 ** (peak_db / 20))


# ------------------------------------------------------------------ 各音效

def make_mist(dur: float = 0.30, seed: int = DEFAULT_SEED, peak_db: float = -10):
    """喷头「呲——」的**纯水汽版**：只有雾化气流，没有阀门开启的机械冲击。

    物理量：带通 2.5-9kHz 白噪声 = 雾化气流；尾部 <600Hz 微量 = 余压。
    包络 60ms raised-cosine 起音 + 90ms 平台 + τ=70ms 指数收尾，
    起始 60ms 再做 tanh 软限幅 —— **零冲击项**。
    """
    np = _np()
    rng = _rng(seed)
    n = int(SR * dur)
    t = np.arange(n) / SR
    base = rng.standard_normal(n)

    body = _bandpass(base, 2500, 9000, order=3)
    tail = _lowpass(base, 600, order=3)

    att = 0.060
    na = max(1, int(att * SR))
    env = np.ones(n)
    env[:na] = 0.5 - 0.5 * np.cos(np.pi * np.linspace(0.0, 1.0, na))  # raised-cosine
    t_after = np.maximum(0.0, t - (att + 0.09))
    env *= np.exp(-t_after / 0.070)

    x = body * env
    x += 0.10 * tail * env * np.exp(-t / 0.15)

    ng = min(n, int(0.060 * SR))
    if ng > 0:
        x[:ng] = np.tanh(x[:ng] * 2.0) / 2.0
    return _norm(x, peak_db)


def make_spray(dur: float = 0.45, seed: int = DEFAULT_SEED, peak_db: float = -10):
    """喷头「呲——」**含阀门开启冲击**（机械感，旧版）。

    ⚠️ 除非明确要那一声"咔哒"，否则用 `mist`。这条只作向后兼容与对照试听。
    """
    np = _np()
    rng = _rng(seed)
    n = int(SR * dur)
    t = np.arange(n) / SR
    base = rng.standard_normal(n)

    body = _bandpass(base, 2500, 9000, order=3)
    burst = _bandpass(base, 1200, 12000, order=2) * np.exp(-t / 0.008)
    tail = _lowpass(base, 600, order=3)

    x = 0.85 * body + 0.50 * burst
    x = x * _exp_env(n, 0.015, 0.10)
    x += 0.12 * tail * _exp_env(n, 0.020, 0.18)
    return _norm(x, peak_db)


def make_click(dur: float = 0.10, seed: int = DEFAULT_SEED, peak_db: float = -10):
    """瓶盖脱离的咔哒。注意：镜头若按铁律写成"开盖状态起拍"，此音效不该用。"""
    np = _np()
    rng = _rng(seed)
    n = int(SR * dur)
    t = np.arange(n) / SR
    impulse = rng.standard_normal(n) * np.exp(-t / 0.0015)
    res = _bandpass(impulse, 2200, 3200, order=2)
    ring = np.sin(2 * np.pi * 5300 * t) * np.exp(-t / 0.010) * 0.35
    return _norm(0.9 * res + ring, peak_db)


def make_glass(dur: float = 0.60, seed: int = DEFAULT_SEED, peak_db: float = -12):
    """玻璃轻碰，用于产品镜（缓慢转动 / 并排定格）。"""
    np = _np()
    n = int(SR * dur)
    t = np.arange(n) / SR
    x = (np.sin(2 * np.pi * 1850 * t) * np.exp(-t / 0.22)
         + 0.60 * np.sin(2 * np.pi * 4250 * t) * np.exp(-t / 0.13)
         + 0.25 * np.sin(2 * np.pi * 7100 * t) * np.exp(-t / 0.06))
    return _norm(x, peak_db)


def make_breath(dur: float = 1.00, seed: int = DEFAULT_SEED, peak_db: float = -14):
    """吸气（资料反复强调"吸气比呼气明显"，故只做这一层）。"""
    np = _np()
    rng = _rng(seed)
    n = int(SR * dur)
    t = np.arange(n) / SR
    nz = rng.standard_normal(n)

    dull = _lowpass(nz, 700, order=3)
    bright = _bandpass(nz, 900, 3000, order=3)
    w = np.clip(t / 0.45, 0.0, 1.0)
    x = dull * (1 - w) + bright * w

    env = np.ones(n)
    na = int(0.18 * SR)
    env[:na] = np.linspace(0.0, 1.0, na) ** 1.5
    env *= np.exp(-np.maximum(0.0, t - 0.45) / 0.20)
    return _norm(_highpass(x * env, 120), peak_db)


KINDS: Dict[str, Tuple[str, str]] = {
    # mist 放第一位：喷头声的**唯一选择**（纯水汽，无阀门咔哒）。
    # R-22（2026-09-30 用户拍板）：spray 从可选列表**整体移除** —— 前端音效
    # 下拉不再出现，链路解析也会把 spray 归一成 mist（见 script_import
    # ._normalize_sfx_kind）。合成函数 make_spray 保留，仅供对照试听与回归。
    "mist": ("make_mist", "喷头『呲——』（纯水汽，无阀门冲击）"),
    "click": ("make_click", "瓶盖咔哒"),
    "glass": ("make_glass", "玻璃轻碰"),
    "breath": ("make_breath", "吸气"),
}

# 喷香水镜头默认挂 mist，且其后留约 0.5s 完全静音（"让观众闻到"的位置不塞念白）
DEFAULT_SPRAY_KIND = "mist"
POST_SPRAY_SILENCE = 0.5

_MAKERS = {
    "mist": make_mist, "spray": make_spray, "click": make_click,
    "glass": make_glass, "breath": make_breath,
}


def list_kinds() -> List[Dict[str, str]]:
    return [{"kind": k, "desc": d, "default": k == DEFAULT_SPRAY_KIND}
            for k, (_fn, d) in KINDS.items()]


def render_sfx(kind: str, out: str, *, seed: int = DEFAULT_SEED,
               dur: Optional[float] = None,
               peak_db: Optional[float] = None) -> Dict[str, object]:
    """合成单个音效写入 wav，返回元信息（时长/峰值/是否已废弃提醒）。"""
    if kind not in _MAKERS:
        raise ValueError(f"未知音效 {kind}，可选：{sorted(_MAKERS)}")
    fn = _MAKERS[kind]
    kw: Dict[str, object] = {"seed": seed}
    if dur is not None:
        kw["dur"] = float(dur)
    if peak_db is not None:
        kw["peak_db"] = float(peak_db)
    x = fn(**kw)  # type: ignore[arg-type]

    path = os.path.abspath(out)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pcm = (x * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())

    np = _np()
    return {
        "path": path,
        "kind": kind,
        "duration": round(len(x) / SR, 3),
        "peak_db": round(20 * np.log10(max(float(np.max(np.abs(x))), 1e-9)), 1),
        "sample_rate": SR,
        "deprecated": kind == "spray",
        "note": ("" if kind != "spray"
                 else "含阀门冲击成分，会被听成『啪嗒』；除非有意保留，否则用 mist"),
    }
