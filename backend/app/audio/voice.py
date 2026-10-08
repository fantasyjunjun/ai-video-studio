"""念白音轨：整条一次 TTS 合成 + **单一全局 atempo** + 时间轴落位。

这块是从 skills/make_voiceover_single.py 移植的，四条教训已经固化成代码，不要改回去：

1. **整条念白必须一次 TTS 请求**，不能逐句请求。逐句 + 逐句不同 atempo 系数 =
   "同一支片里听起来像换了三个人"（真实事故）。
2. **绝不逐句变速**。超过时间窗时对**整条**做等比变速（只有一个 atempo 系数），
   这是"音色不变"的关键。
3. **`atempo` 是全局系数**：只要有一行超窗，整条都被等比加速 —— 这才是"念白好赶"
   的真正机制（不是字数、不是音色）。所以 `plan_voice()` 会**点名**是哪一行超窗，
   并给出两条改法（减字 / 放宽该行 end）。判定线：`atempo > 1.02` 就是没配好，别去调音色。
4. **同语言族兜底 ≠ 选角正确**。端点抖动是"单次超时"而不是"这个音色不可用"，
   所以前 `fallback_after` 次只重试首选音色；真换了人必须在报告里标
   `voice_is_fallback`（旧实现记的是锁定音色，等于撒谎）。

另外两个实操约定：
  - 调先后]不清代理环境变量，edge-tts 会 502；
  - 中间文件永不 os.remove，只截断覆盖（托管环境的安全钩子会中止进程）。

离线可测部分全部抽成了纯函数（`plan_voice` / `voice_chain` / `norm_pitch` /
`count_stats`），不必联网即可断言行为。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .ffmpeg import clear_proxy_env, run, sec

# 兜底音色必须同语言族 —— 跨语言兜底会毁掉整条音轨
FALLBACK_BY_LANG: Dict[str, List[str]] = {
    "zh-CN": ["zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural"],
    "pt-BR": ["pt-BR-FranciscaNeural", "pt-BR-ThalitaMultilingualNeural"],
    "es-MX": ["es-MX-DaliaNeural", "es-ES-ElviraNeural", "es-AR-ElenaNeural"],
    "es-ES": ["es-ES-ElviraNeural", "es-MX-DaliaNeural", "es-AR-ElenaNeural"],
    "es-AR": ["es-AR-ElenaNeural", "es-MX-DaliaNeural", "es-ES-ElviraNeural"],
}

# 各语言默认 pronunciation 微调，来自实测（中文 TTS 默认语速偏慢，不提速会导致
# 后期变速系数 >1.4、听感发赶）
DEFAULT_VOICE_PARAMS: Dict[str, Tuple[str, str]] = {
    "zh-CN": ("+14%", "-4Hz"),
    "pt-BR": ("+6%", "-3Hz"),
    "es-MX": ("+6%", "-3Hz"),
}

#atempo 判定线
TEMPO_WARN = 1.02


def lang_of(voice: str) -> str:
    parts = voice.split("-")
    return "-".join(parts[:2]) if len(parts) >= 3 else voice


def voice_chain(voice: str) -> List[str]:
    chain = FALLBACK_BY_LANG.get(lang_of(voice), [])
    return [voice] + [v for v in chain if v != voice]


def norm_pitch(p: str) -> str:
    """edge-tts 只接受 [+-]NHz；'0Hz' 会直接抛 ValueError，统一归一。"""
    s = str(p or "").strip()
    m = re.search(r"([+-]?)\s*(\d+)\s*Hz", s, re.I)
    if not m:
        return "+0Hz"
    sign = "-" if m.group(1) == "-" else "+"
    return f"{sign}{m.group(2)}Hz"


def count_stats(text: str) -> Tuple[int, int]:
    """返回 (汉字数, 拉丁词数)。中文按字/秒，拉丁语系按词/秒判定语速。"""
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-zÀ-ÿ]+", text))
    return han, words


@dataclass
class VoiceRow:
    """时间轴里的一行：期望区间 + 文案。"""

    start: float
    end: float
    text: str

    @property
    def window(self) -> float:
        return max(0.0, self.end - self.start)


def parse_timeline(path: str) -> List[VoiceRow]:
    """解析 `start|end|text` 时间轴文件（`#` 开头为注释）。"""
    rows: List[VoiceRow] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            rows.append(VoiceRow(float(parts[0]), float(parts[1]),
                                 "|".join(parts[2:]).strip()))
    rows.sort(key=lambda r: r.start)
    return rows


def rows_from_pairs(pairs: Sequence[Tuple[float, float, str]]) -> List[VoiceRow]:
    return [VoiceRow(a, b, c) for a, b, c in pairs]


# ------------------------------------------------------------------ 计划（纯函数，可离线断言）

@dataclass
class VoicePlan:
    """念白装配计划。**tempo 是全局系数**，任何一行超窗都会推高它。"""

    tempo: float = 1.0
    block_mode: bool = True
    start_at: float = 0.0
    available: float = 0.0
    raw_dur: float = 0.0
    rows: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    ok: bool = True

    @property
    def mode_name(self) -> str:
        return "整条自然语流" if self.block_mode else "句级对齐"

    @property
    def effective_speech_sec(self) -> float:
        return self.raw_dur / self.tempo if self.tempo else self.raw_dur

    def to_dict(self) -> dict:
        return {
            "tempo": round(self.tempo, 4),
            "mode": self.mode_name,
            "start_at": round(self.start_at, 3),
            "available": round(self.available, 3),
            "speech_sec": round(self.effective_speech_sec, 3),
            "ok": self.ok,
            "warnings": self.warnings,
            "rows": self.rows,
        }


def plan_voice(
    rows: Sequence[VoiceRow],
    total: float,
    raw_dur: float,
    *,
    lead: float = 0.12,
    tail: float = 0.30,
    start: Optional[float] = None,
    cuts: Optional[Sequence[float]] = None,
    fill: bool = False,
    max_stretch: float = 1.12,
    max_tempo: float = 1.30,
    min_segment: float = 0.30,
) -> VoicePlan:
    """计算落位计划。**不碰网络、不写文件**，因此可以离线单元测试。

    cuts 给出时走"句级对齐"；否则走"整条自然语流"（默认，音色与韵律最一致）。
    """
    plan = VoicePlan(raw_dur=raw_dur)
    if not rows:
        plan.ok = False
        plan.warnings.append("时间轴为空")
        return plan
    if raw_dur <= 0.0:
        plan.ok = False
        plan.warnings.append(f"原始语音时长无效：{raw_dur}s")
        return plan

    plan.start_at = max(0.0, start if start is not None else rows[0].start + lead)
    plan.available = max(0.0, total - plan.start_at - tail)

    # ---- 切分 ----
    speech: List[Tuple[float, float, float]] = []
    if cuts:
        pts = [0.0] + [float(c) for c in cuts] + [raw_dur]
        speech = [(a, b, b - a) for a, b in zip(pts[:-1], pts[1:])]
        if len(cuts) != len(rows) - 1:
            plan.ok = False
            plan.warnings.append(
                f"切点数量 {len(cuts)} 与 {len(rows)} 行不匹配（应为 {len(rows) - 1} 个）")
        if cuts and (list(cuts) != sorted(cuts) or cuts[0] <= 0 or cuts[-1] >= raw_dur):
            plan.ok = False
            plan.warnings.append(
                f"切点必须递增且落在 (0, {raw_dur:.2f}) 内：{list(cuts)}")
        thin = [i for i, (_a, _b, ln) in enumerate(speech, 1) if ln < min_segment]
        if thin:
            plan.ok = False
            plan.warnings.append(
                f"第 {thin} 段语音不足 {min_segment:.2f}s —— 切点不合理，"
                f"宁可报错也不要出一版残废音轨")

    plan.block_mode = not speech

    # ---- 全局 atempo（整条等比，绝不逐句）----
    tempo = 1.0
    if plan.block_mode:
        if raw_dur > plan.available > 0.5:
            tempo = min(max_tempo, raw_dur / plan.available)
        elif fill and (plan.available - raw_dur) > 0.15 \
                and (plan.available / raw_dur) <= max_stretch:
            tempo = raw_dur / plan.available  # 整条等比放慢铺满
            plan.warnings.append(
                f"整条等比放慢铺满 atempo={tempo:.4f}（韵律会被压平，"
                f"atempo<0.90 时应改为加文案字数）")
    else:
        tight: List[Tuple[int, float, float, float]] = []
        for i, ((_s, _e, seg_len), row) in enumerate(zip(speech, rows), 1):
            win = max(0.5, row.window - lead - tail)
            if seg_len > win:
                tight.append((i, seg_len, win, seg_len / win))
                tempo = max(tempo, seg_len / win)
        tempo = min(tempo, max_tempo)
        if tight:
            for i, seg_len, win, ratio in tight:
                plan.warnings.append(
                    f"第 {i} 行语音 {seg_len:.2f}s > 窗口 {win:.2f}s"
                    f"（比 {ratio:.3f}）→ 推高全局 atempo。"
                    f"请减字，或把该行 end 放宽")

    if abs(tempo - 1.0) < 1e-3:
        tempo = 1.0
    plan.tempo = tempo

    # ---- 判定语速是否还能听 ----
    if tempo > TEMPO_WARN:
        plan.warnings.append(
            f"atempo={tempo:.3f} > {TEMPO_WARN}：整条会被加速，听感发赶。"
            f"**先改文案字数，别去调音色**")
    if tempo >= max_tempo:
        plan.warnings.append(
            f"已达变速上限 {max_tempo}：文案偏长，应减字而不是继续加速")
    elif 0.0 < tempo < 0.90:
        plan.warnings.append(
            f"atempo={tempo:.3f} < 0.90：放慢过多会压平韵律，建议加文案字数为宜")

    # ---- 报告行 ----
    if plan.block_mode:
        eff = raw_dur / tempo if tempo else raw_dur
        stats = [count_stats(r.text) for r in rows]
        # 整条模式没有逐句切点，只能按字数**估算**每行占用，
        # 但"哪一行超窗"必须点名 —— 否则用户只能看到整条发赶，无从下手
        units = [w if w else h for h, w in stats]
        tot_units = sum(units) or 1
        over_rows: List[Tuple[int, float, float]] = []
        for i, (row, (han, words)) in enumerate(zip(rows, stats), 1):
            est = eff * (units[i - 1] / tot_units)
            win = max(0.5, row.window - lead - tail)
            over = est - win
            rec = {
                "line": i, "start": row.start, "end": row.end,
                "text": row.text, "words": words, "han": han,
                "est_sec": round(est, 2), "window": round(win, 2),
                "over_by": round(over, 2) if over > 0.05 else 0.0,
            }
            if rec["over_by"]:
                rec["flag"] = "预计超出窗口（按字数估算），建议减字或放宽 end"
                over_rows.append((i, est, win))
            plan.rows.append(rec)
        # 只点名最严重的两行，别刷屏
        for i, est, win in sorted(over_rows, key=lambda x: -x[1] / max(0.01, x[2]))[:2]:
            plan.warnings.append(
                f"第 {i} 行预计 {est:.2f}s > 窗口 {win:.2f}s（按字数估算）："
                f"减字，或把该行 end 放宽")

        tot_w = sum(r["words"] for r in plan.rows)
        tot_h = sum(r["han"] for r in plan.rows)
        if tot_w:
            wps = tot_w / max(0.01, eff)
            if not (2.0 <= wps <= 3.6):
                plan.warnings.append(
                    f"整条语速 {wps:.2f} 词/秒，偏离 2.0-3.6，建议调 rate")
        elif tot_h:
            cps = tot_h / max(0.01, eff)
            if not (3.5 <= cps <= 6.5):
                plan.warnings.append(
                    f"整条语速 {cps:.2f} 字/秒，偏离 3.5-6.5，建议调 rate")
    else:
        for i, ((_s, _e, seg_len), row) in enumerate(zip(speech, rows), 1):
            eff = seg_len / tempo
            han, words = count_stats(row.text)
            win = max(0.5, row.window - lead - tail)
            wps = words / max(0.01, eff) if words else 0.0
            rec = {
                "line": i, "start": row.start, "end": row.end,
                "text": row.text, "seg_sec": round(seg_len, 2),
                "placed_sec": round(eff, 2), "window": round(win, 2),
                "over_by": round(eff - win, 2) if eff - win > 0.05 else 0.0,
                "words": words, "han": han, "wps": round(wps, 2),
            }
            if rec["over_by"]:
                rec["flag"] = f"超出区间 {rec['over_by']}s，建议减字"
            elif words and not (1.9 <= wps <= 3.6):
                rec["flag"] = "语速偏离 1.9-3.6 词/秒"
            plan.rows.append(rec)
    return plan


# ------------------------------------------------------------------ 句界检测

def detect_silence_cuts(
    wav: str,
    threshold_db: str = "-38",
    min_sil: float = 0.20,
    expected: Optional[int] = None,
) -> Optional[List[float]]:
    """用 silencedetect 找句间切分点；不足则返回 None（调用方降级为整条模式）。

    ⚠️ 必须剔除**首尾静音**。silencedetect 会把结尾那段长静音也报成一个区间，
    它比任何句间停顿都长（尾音后空白可达 0.8s），"取最长的 need 个"必然选中它 ——
    结果是末句只分到 0.4s 残段、整条被误压到 atempo 1.13。这是历史上
    `--align` 被误判为"不可靠"的真正原因。
    """
    from .ffmpeg import probe_duration

    raw_dur = probe_duration(wav)
    r = run(["ffmpeg", "-hide_banner", "-i", wav, "-af",
             f"silencedetect=n={threshold_db}dB:d={min_sil}", "-f", "null", "-"],
            check=False)
    log = r.stderr or ""
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    sil: List[Tuple[float, float, float]] = []
    for i, s in enumerate(starts):
        if i >= len(ends):
            continue
        e = ends[i]
        if e <= s or s <= 0.05 or e >= raw_dur - 0.05:
            continue  # 首尾静音不是句间停顿
        sil.append((s, e, e - s))

    need = (expected or 2) - 1
    if need <= 0:
        return []
    if len(sil) < need:
        return None
    if len(sil) == need:
        chosen = sil
    else:
        chosen = sorted(sorted(sil, key=lambda x: -x[2])[:need], key=lambda x: x[0])
    return [round((s + e) / 2.0, 4) for s, e, _ in chosen]


# ------------------------------------------------------------------ TTS（联网）

def _truncate(path: str) -> None:
    """截断而非删除（托管环境会拦截 os.remove）。"""
    try:
        with open(path, "wb"):
            pass
    except OSError:
        pass


async def _tts_once(text: str, voice: str, rate: str, pitch: str, out_mp3: str) -> None:
    clear_proxy_env()
    try:
        import edge_tts
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "缺少 edge-tts：pip install edge-tts -i https://mirrors.aliyun.com/pypi/simple/"
        ) from e
    c = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await c.save(out_mp3)


def synth_raw(text: str, voice: str, rate: str, pitch: str, out_mp3: str,
              retries: int = 10, fallback_after: int = 4) -> Optional[str]:
    """整条一次 TTS。**前 fallback_after 次只重试首选音色**，之后才轮换同语言族兜底。

    返回实际使用的音色，失败返回 None。
    """
    voices = voice_chain(voice)
    pitch = norm_pitch(pitch)
    for i in range(retries):
        if i < fallback_after:
            v = voice                       # 别急着换人：端点抖动是单次超时性质
        else:
            j = i - fallback_after + 1
            v = voices[min(j, len(voices) - 1)]
        if os.path.exists(out_mp3):
            _truncate(out_mp3)
        try:
            asyncio.run(_tts_once(text, v, rate, pitch, out_mp3))
            if os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 1000:
                return v
        except Exception as e:  # noqa: BLE001
            print(f"  TTS 第 {i + 1}/{retries} 次失败（{v}）: "
                  f"{type(e).__name__} {str(e)[:100]}")
            time.sleep(1.5 * (i + 1))
    return None


PROBE_TEXT = {"zh": "测试一下", "pt": "casa", "es": "casa"}


def probe_text_for(voice: str) -> str:
    return PROBE_TEXT.get(lang_of(voice)[:2], "test")


def pick_voice(voice: str, rate: str, pitch: str, workdir: str) -> Optional[str]:
    """整轨固定音色：先探测哪个音色当前可用，避免中途换人。"""
    probe = os.path.join(workdir, "probe.mp3")
    return synth_raw(probe_text_for(voice), voice, rate, pitch, probe, retries=3)


def default_params_for(lang: str) -> Tuple[str, str]:
    return DEFAULT_VOICE_PARAMS.get(lang, ("+6%", "-3Hz"))


# ------------------------------------------------------------------ 落位（离线，吃 plan）

def _silence(dur: float, path: str) -> str:
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=mono", "-t", sec(dur),
         "-c:a", "pcm_s16le", path])
    return path


def _slice(src: str, start: float, end: float, path: str, tempo: float = 1.0) -> str:
    af = f"atempo={tempo:.5f}," if abs(tempo - 1.0) > 1e-6 else ""
    af += "aresample=44100"
    run(["ffmpeg", "-y", "-v", "error", "-ss", sec(start), "-to", sec(end),
         "-i", src, "-af", af, "-ac", "1", "-ar", "44100",
         "-c:a", "pcm_s16le", path])
    return path


def render_track(
    raw_wav: str,
    rows: Sequence[VoiceRow],
    total: float,
    out: str,
    workdir: str,
    *,
    voice_used: Optional[str] = None,
    voice_locked: Optional[str] = None,
    report_name: str = "report.json",
    **plan_kwargs,
) -> Tuple[str, VoicePlan]:
    """按 plan 把原始语音落位到时间轴，输出单声道 44.1k wav。

    `plan_kwargs` 直接透传给 `plan_voice`（lead / tail / cuts / fill / ...）。
    """
    from .ffmpeg import probe_duration

    os.makedirs(workdir, exist_ok=True)
    raw_dur = probe_duration(raw_wav)
    plan = plan_voice(rows, total, raw_dur, **plan_kwargs)

    parts: List[str] = []
    if plan.block_mode:
        if plan.start_at > 0.005:
            parts.append(_silence(plan.start_at, os.path.join(workdir, "lead.wav")))
        parts.append(_slice(raw_wav, 0.0, raw_dur,
                            os.path.join(workdir, "whole_tempo.wav"), plan.tempo))
    else:
        cuts = plan_kwargs.get("cuts") or []
        pts = [0.0] + [float(c) for c in cuts] + [raw_dur]
        pos = 0.0
        for i, row in enumerate(rows, 1):
            s, e = pts[i - 1], pts[i]
            eff = (e - s) / plan.tempo
            target = row.start + plan_kwargs.get("lead", 0.12)
            gap = target - pos
            if gap > 0.005:
                parts.append(_silence(gap, os.path.join(workdir, f"gap{i:02d}.wav")))
                pos += gap
            parts.append(_slice(raw_wav, s, e,
                                os.path.join(workdir, f"seg{i:02d}.wav"), plan.tempo))
            pos += eff

    listfile = os.path.join(workdir, "list.txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")

    out_abs = os.path.abspath(out)
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", listfile, "-af", "apad", "-t", sec(total),
         "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", out_abs])

    # 报告记**实际使用的音色**，不是锁定音色（旧实现在这点上撒过谎）
    payload = {
        "voice": voice_used or "(未提供)",
        "voice_locked": voice_locked or voice_used or "",
        "voice_is_fallback": bool(voice_used and voice_locked and voice_used != voice_locked),
        "tempo": plan.tempo,
        "mode": plan.mode_name,
        "raw_dur": plan.raw_dur,
        "warnings": plan.warnings,
        "rows": plan.rows,
    }
    with open(os.path.join(workdir, report_name), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out_abs, plan


def write_timeline(rows: Sequence[VoiceRow], path: str) -> str:
    """把 rows 写成 `start|end|text` 格式文件（前端可编辑后回传）。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(f"{r.start:.2f}|{r.end:.2f}|{r.text}\n")
    return path
