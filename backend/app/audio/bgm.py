"""本地合成背景音乐（零版权风险，不依赖素材库）。

从 skills/make_bgm.py 移植，算法保持逐行一致，只把 CLI 改成库调用。

设计意图（对应香水带货片调性）：
  - 大调段（暖）：Fmaj7 - Am7 - Bbmaj7 - Fmaj7；
  - 小调段（暗）：Fm7 - Dbmaj7 - Bbm7 - Fm7（**无导音**，教会调式感，沉而不悲）；
  - 音色是加法合成的正弦泛音垫（软起音 0.9s、长释放）+ 双声道轻微失谐制造宽度；
  - 和弦切换处加钟琴式弱音粒，给画面"高光"；
  - 底部一层缓慢涌动的倍低音，提供重量感但不糊。

为什么要 split 两段式：**一支片里昼夜对撞时，单一调性垫乐会把夜戏配错** ——
夜段若仍用暖大调，会与画面（冷蓝轮廓光）和香调（烟熏皮革/树脂）同时打架。
日夜交界处用 xfade 交叉淡化，听不出接缝。

依赖 numpy + scipy（延迟导入，没装也能 import 本模块，只在合成时报明确错误）。
"""

from __future__ import annotations

import math
import wave
from typing import Dict, List, Optional, Tuple

SR = 44100

# 大调（暖）：用于白天/暖光段
PROG_MAJOR: List[Tuple[str, List[str], str]] = [
    ("Fmaj7", ["F3", "A3", "C4", "E4"], "F2"),
    ("Am7", ["A3", "C4", "E4", "G4"], "A2"),
    ("Bbmaj7", ["Bb3", "D4", "F4", "A4"], "Bb2"),
    ("Fmaj7", ["F3", "A3", "C4", "E4"], "F2"),
]

# 小调（暗）：用于夜段。i - VI - iv - i，无导音，沉而不悲
PROG_MINOR: List[Tuple[str, List[str], str]] = [
    ("Fm7", ["F3", "Ab3", "C4", "Eb4"], "F2"),
    ("Dbmaj7", ["Db3", "F3", "Ab3", "C4"], "Db2"),
    ("Bbm7", ["Bb3", "Db4", "F4", "Ab4"], "Bb2"),
    ("Fm7", ["F3", "Ab3", "C4", "Eb4"], "F2"),
]

_NP = None


def _np():
    """延迟导入 numpy —— 没装也能 import 本模块，只在真正合成时才报错。"""
    global _NP
    if _NP is None:
        try:
            import numpy as np
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "合成 BGM 需要 numpy + scipy："
                "pip install numpy scipy -i https://mirrors.aliyun.com/pypi/simple/"
            ) from e
        _NP = np
    return _NP


def note(name: str) -> float:
    """音名 -> 频率，支持 F3 / A#3 / Bb3 形式。"""
    names = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5,
             "F#": 6, "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10,
             "B": 11}
    pitch = name[:-1]
    octave = int(name[-1])
    midi = 12 * (octave + 1) + names[pitch]
    return 440.0 * 2 ** ((midi - 69) / 12)


def adsr(n: int, attack: float, release: float, sustain_level: float = 1.0):
    np = _np()
    a = min(int(attack * SR), n // 2)
    r = min(int(release * SR), n - a)
    env = np.ones(n) * sustain_level
    if a > 0:
        env[:a] = np.linspace(0, 1, a) ** 1.5
    if r > 0:
        env[-r:] = env[-r:] * (np.linspace(1, 0, r) ** 1.2)
    return env


def pad_voice(freq: float, n: int, detune: float = 0.0015, seed: int = 0):
    """单个加法合成垫音（返回 L/R 两路）。"""
    np = _np()
    rng = np.random.default_rng(seed)
    t = np.arange(n) / SR
    partials = [(1.0, 1.00), (2.0, 0.30), (3.0, 0.14), (4.0, 0.07), (5.0, 0.04)]
    out = np.zeros(n)
    for mult, amp in partials:
        vib = 1.0 + 0.0008 * np.sin(2 * np.pi * (0.13 + 0.05 * rng.random()) * t)
        out += amp * np.sin(2 * np.pi * freq * mult * t * vib + rng.random() * 6.28)
    l = out * (1 + detune * np.sin(2 * np.pi * 0.07 * t))
    r = out * (1 - detune * np.sin(2 * np.pi * 0.07 * t))
    return l / 4.0, r / 4.0


def bell(freq: float, n: int, decay: float = 1.8):
    np = _np()
    t = np.arange(n) / SR
    env = np.exp(-t / decay)
    return (np.sin(2 * np.pi * freq * t) * 0.5
            + np.sin(2 * np.pi * freq * 2.01 * t) * 0.16
            + np.sin(2 * np.pi * freq * 3.02 * t) * 0.07) * env


def transpose_prog(prog, root: str):
    """把一组和弦整体平移：以 F3 -> root 的半音数移调。"""
    if root == "F3":
        return prog
    semi = round(12 * math.log2(note(root) / note("F3")))
    allnames = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    flat = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}

    def shift(name: str) -> str:
        p, o = name[:-1], int(name[-1])
        p = flat.get(p, p)
        idx = allnames.index(p) + semi
        return f"{allnames[idx % 12]}{o + idx // 12}"

    return [(c, [shift(x) for x in ch], shift(b)) for c, ch, b in prog]


def render_section(n: int, prog, seed_base: int = 0):
    """把一组和弦渲染成长度 n 的立体声垫乐（不含总线淡化/限幅）。"""
    np = _np()
    L, R = np.zeros(n), np.zeros(n)
    seg = n / len(prog)
    for i, (_name, chord, bass) in enumerate(prog):
        start = int(i * seg)
        ln = min(int(seg + 0.6 * SR), n - start)
        if ln <= 0:
            continue
        env = adsr(ln, attack=0.9, release=0.9)
        for j, nm in enumerate(chord):
            l, r = pad_voice(note(nm), ln, seed=seed_base + i * 10 + j)
            L[start:start + ln] += l * env
            R[start:start + ln] += r * env
        t = np.arange(ln) / SR
        sub = np.sin(2 * np.pi * note(bass) * t) * (0.55 + 0.45 * np.sin(2 * np.pi * 0.09 * t))
        sub *= adsr(ln, 1.2, 1.0)
        L[start:start + ln] += sub * 0.22
        R[start:start + ln] += sub * 0.22
        off = start + int(0.15 * SR)
        bl = min(int(2.6 * SR), n - off)
        if bl > 0:
            b = bell(note(chord[-1]) * 2, bl)
            L[off:off + bl] += b * 0.10
            R[off:off + bl] += np.roll(b, 60) * 0.10
    return L, R


def synthesize(duration: float, root: str, level_db: float,
               root2: Optional[str] = None, split: Optional[float] = None,
               minor2: bool = False, xfade: float = 1.2):
    np = _np()
    n = int(duration * SR)
    two_part = bool(root2 and split and 0 < split < duration)

    if not two_part:
        prog = transpose_prog(PROG_MINOR if minor2 else PROG_MAJOR, root)
        L, R = render_section(n, prog, seed_base=0)
    else:
        xf = min(xfade, split, duration - split)
        n_a = int((split + xf / 2) * SR)
        n_b = n - int((split - xf / 2) * SR)
        prog_a = transpose_prog(PROG_MINOR if minor2 else PROG_MAJOR, root)
        prog_b = transpose_prog(PROG_MINOR, root2)
        la, ra = render_section(n_a, prog_a, seed_base=0)
        lb, rb = render_section(n_b, prog_b, seed_base=500)
        L, R = np.zeros(n), np.zeros(n)
        off_a, off_b = 0, int((split - xf / 2) * SR)
        ea = np.ones(n_a)
        k0, k1 = int(max(0, split - xf / 2) * SR) - off_a, int((split + xf / 2) * SR) - off_a
        k1 = min(k1, n_a)
        if k1 > k0:
            ea[k0:k1] = np.linspace(1, 0, k1 - k0)
        ea[k1:] = 0.0
        eb = np.ones(n_b)
        m1 = int((split + xf / 2) * SR) - off_b
        m1 = min(max(m1, 0), n_b)
        if m1 > 0:
            eb[:m1] = np.linspace(0, 1, m1)
        end_a, end_b = min(n, off_a + n_a), min(n, off_b + n_b)
        L[off_a:end_a] += la[:end_a - off_a] * ea[:end_a - off_a]
        R[off_a:end_a] += ra[:end_a - off_a] * ea[:end_a - off_a]
        L[off_b:end_b] += lb[:end_b - off_b] * eb[:end_b - off_b]
        R[off_b:end_b] += rb[:end_b - off_b] * eb[:end_b - off_b]

    try:
        from scipy.signal import butter, lfilter
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("合成 BGM 需要 scipy：pip install scipy -i https://mirrors.aliyun.com/pypi/simple/") from e

    rng = np.random.default_rng(7)
    air = rng.standard_normal(n)
    b, a = butter(2, [2000 / (SR / 2), 6000 / (SR / 2)], btype="band")
    air = lfilter(b, a, air)
    lfo = 0.5 + 0.5 * np.sin(2 * np.pi * 0.11 * np.arange(n) / SR)
    L += air * lfo * 0.010
    R += air * (1 - lfo) * 0.010

    fade_in = int(0.8 * SR)
    fade_out = int(2.0 * SR)
    L[:fade_in] *= np.linspace(0, 1, fade_in)
    R[:fade_in] *= np.linspace(0, 1, fade_in)
    L[-fade_out:] *= np.linspace(1, 0, fade_out)
    R[-fade_out:] *= np.linspace(1, 0, fade_out)
    peak = max(float(np.abs(L).max()), float(np.abs(R).max()))
    target = 10 ** (level_db / 20)
    gain = target / max(peak, 1e-9)
    L = np.tanh(L * gain * 1.1) / 1.1
    R = np.tanh(R * gain * 1.1) / 1.1
    return L, R


def write_wav(path: str, L, R) -> str:
    np = _np()
    data = np.stack([L, R], axis=1)
    pcm = np.clip(data * 32767, -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


def render_bgm(
    out: str,
    duration: float,
    root: str = "F3",
    level_db: float = -20.0,
    split: Optional[float] = None,
    root2: Optional[str] = None,
    minor2: bool = True,
    xfade: float = 1.4,
) -> Dict[str, object]:
    """生成 BGM wav（44.1k 立体声）。返回元信息。

    昼夜/<调性反转的片子**必须给 split + root2**，否则夜戏会被配成暖调。
    """
    if split is not None and not root2:
        raise ValueError("给了 split 就必须给 root2（第二段根音）")
    L, R = synthesize(duration, root, level_db, root2=root2, split=split,
                      minor2=minor2, xfade=xfade)
    write_wav(out, L, R)
    return {
        "path": out,
        "duration": round(duration, 3),
        "sample_rate": SR,
        "channels": 2,
        "level_db": level_db,
        "two_part": bool(root2 and split),
        "split": split,
        "root": root,
        "root2": root2,
        "minor2": bool(root2 and split and minor2),
        "xfade": xfade,
    }
