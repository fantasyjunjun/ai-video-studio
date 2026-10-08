"""音频后期工具包（P3）。

六个方面，每个都对应一次真实事故的固化：
    ffmpeg  —— 封装 + 响度测量禁 -v error + 永不 os.remove
    onset   —— 实结果性元素起点（每次换提示词都必须重测）
    bgm     —— 本地合成垫乐，昼夜必须分段
    sfx     —— 本地合成音效，喷香水默认 mist（无起音冲击）
    voice   —— 整条一次 TTS + 单一全局 atempo + 点名超窗行
    mix     —— 念白闪避 + limiter + -14 LUFS
"""

from . import bgm, ffmpeg, mix, onset, sfx, voice  # noqa: F401
from .ffmpeg import FFmpegError, measure_loudness, probe_duration, probe_video, sec  # noqa: F401
from .onset import detect_onset  # noqa: F401
from .bgm import render_bgm  # noqa: F401
from .sfx import render_sfx, list_kinds  # noqa: F401
from .mix import mix_final, parse_sfx_spec  # noqa: F401
from .voice import VoiceRow, plan_voice  # noqa: F401
