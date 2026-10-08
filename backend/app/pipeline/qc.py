"""出片质量门（P-6）：把"交付前自检"从提示词结构（lint）扩到**出片结果**。

为什么必须有这一层：
    lint 满分 ≠ 出片没问题。lint 只管提示词结构（锚点/节拍/量化），
    管不了模型的**时序行为**与**构图漂移** —— 提示词写"雾在第一帧已存在"
    可以拿到 14/14，实拍出来雾仍然在 0.83s 才出现，而且构图被破坏
    （这在本项目的技能库里是实测过的）。所以"某元素何时出现""画面是否真的动过"
    这类问题，只能在**出片之后**量。

能自动判的（本模块）：
    · 整镜静止 / 中段或尾部冻结帧        —— 模型没动 or 生成截断
    · 黑场 / 亮度跳变 / 重复帧占比       —— 拼接点、解码异常、掉帧
    · 结果性元素（雾/烟/水花）**未出现** —— 提示词写了"喷雾"却检不到 → 喷雾无来源预警
    · 实测起点与台账 `onset_sec` 偏离    —— 音效落位会声画错位
    · 镜头自带音轨残留                   —— 拼接时应丢源音轨，没丢会串声
    · 指定 ROI 内**物件数量突变**        —— 「瓶盖消失 / 同物复制」这类穿帮的预警

判不了的（必须人看）：构图漂移、手部畸变、物件形变。
所以本模块同时输出 `contact_sheet()`（ffmpeg tile 逐格拼图），
**自动判据用来"圈重点"，拼图用来"下结论"** —— 与技能库里
`detect_element_onset.py` 的定位一致：工具是辅助，不是判据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..audio.ffmpeg import FFmpegError, probe_video, run
from ..audio.onset import DEFAULT_ROI

SEVERITY_RANK: Dict[str, int] = {"high": 3, "medium": 2, "low": 1, "info": 0}
SEVERITY_LABELS: Dict[str, str] = {
    "high": "高危 · 建议重出",
    "medium": "需复核",
    "low": "提示",
    "info": "信息",
}

# 结果性元素的中/英/葡/西关键词 → 归一化名字。
# 用途：提示词里声明了某元素，画面就该检出它的起点；检不到 = "无来源"。
ELEMENT_HINTS: Dict[str, Sequence[str]] = {
    "mist": ("mist", "spray", "spritz", "fog", "雾", "喷雾", "水雾", "汽雾",
             "névoa", "bruma", "neblina", "niebla"),
    "smoke": ("smoke", "烟", "fumaça", "humo"),
    "splash": ("splash", "water splash", "飞溅", "水花", "respingo", "salpicadura"),
    "steam": ("steam", "vapor", "蒸汽", "蒸汽感"),
    "droplets": ("droplet", "droplets", "水珠", "水滴", "gotas"),
}

# 默认判据参数（可按镜头覆盖）
DEFAULT_MIN_RISE = 0.05      # 与 audio.onset 一致：低于此值算运镜漂移
DEFAULT_FREEZE_EPS = 0.0015  # 帧间平均绝对差低于此值视为"没动"
DEFAULT_DUP_EPS = 0.0008     # 低于此值视为重复帧
DEFAULT_BLACK_LEVEL = 0.03   # 平均亮度低于此值视为黑场
DEFAULT_FLICKER_DB = 0.18    # 相邻帧平均亮度跳变阈值
DEFAULT_LATE_SEC = 1.2       # 元素起点晚于此值 → 前摇过长
DEFAULT_ONSET_TOL = 0.15     # 台账 onset 与实测起点的允许偏差


@dataclass
class QcFinding:
    code: str
    severity: str
    label: str
    detail: str
    suggestion: str = ""
    at_sec: Optional[float] = None
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "severity_label": SEVERITY_LABELS.get(self.severity, self.severity),
            "label": self.label,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "at_sec": self.at_sec,
            "evidence": self.evidence,
        }


@dataclass
class QcReport:
    path: str
    frames: int = 0
    fps: float = 24.0
    duration: float = 0.0
    size: Tuple[int, int] = (0, 0)
    has_audio: bool = False
    metrics: Dict[str, Any] = field(default_factory=dict)
    findings: List[QcFinding] = field(default_factory=list)
    note: str = ""

    @property
    def counts(self) -> Dict[str, int]:
        c = {"high": 0, "medium": 0, "low": 0, "info": 0}
        for f in self.findings:
            c[f.severity] = c.get(f.severity, 0) + 1
        return c

    @property
    def blocked(self) -> bool:
        return self.counts["high"] > 0

    @property
    def worst(self) -> str:
        for s in ("high", "medium", "low", "info"):
            if self.counts[s]:
                return s
        return "clean"

    @property
    def issues(self) -> List[str]:
        """并入 `/verify` 的 issues（只用 high/medium，避免刷屏）。"""
        return [f"{f.label}：{f.detail}" for f in self.findings
                if f.severity in ("high", "medium")]

    def to_dict(self, *, with_findings: bool = True) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "path": self.path,
            "frames": self.frames,
            "fps": self.fps,
            "duration": round(self.duration, 3),
            "size": list(self.size),
            "has_audio": self.has_audio,
            "metrics": self.metrics,
            "counts": self.counts,
            "total": len(self.findings),
            "blocked": self.blocked,
            "worst": self.worst,
            "note": self.note,
        }
        if with_findings:
            out["findings"] = [f.to_dict() for f in self.findings]
            out["issues"] = self.issues
        return out


# --------------------------------------------------------------------------- #
# 帧与度量
# --------------------------------------------------------------------------- #

def _gray_frames(path: str, w: int = 96, h: int = 172):
    """解成灰度帧 (n,h,w) float32 0..1。分辨率刻意小 —— 判据是整体趋势。"""
    try:
        import numpy as np
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("qc 需要 numpy：pip install numpy") from e

    r = run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
             f"scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
            check=False, binary=True)
    buf = np.frombuffer(r.stdout, dtype=np.uint8)
    n = len(buf) // (w * h)
    if n == 0:
        raise FFmpegError(f"无法解码视频帧：{path}")
    return buf[: n * w * h].reshape(n, h, w).astype(np.float32) / 255.0


def _runs(mask: List[bool]) -> List[Tuple[int, int]]:
    """把布尔序列压缩成 [(start, end)] 连续段（半开区间）。"""
    out: List[Tuple[int, int]] = []
    i = 0
    n = len(mask)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < n and mask[j]:
            j += 1
        out.append((i, j))
        i = j
    return out


def find_bad_runs(series: List[float], *, limit: float,
                  min_frames: int) -> List[Tuple[int, int]]:
    """值**低于** limit 且持续 >= min_frames 的区间（用于冻结帧/黑场/重复帧）。"""
    return [(a, b) for a, b in _runs([v < limit for v in series])
            if b - a >= max(1, int(min_frames))]


def _value_runs(values: List[int]) -> List[Tuple[int, int, int]]:
    """把序列压成 [(start, end, value)] 的等值连续段（半开区间）。"""
    out: List[Tuple[int, int, int]] = []
    i, n = 0, len(values)
    while i < n:
        j = i
        while j < n and values[j] == values[i]:
            j += 1
        out.append((i, j, int(values[i])))
        i = j
    return out


def steady_changes(values: List[int], *, hold: int) -> List[Tuple[int, int, int]]:
    """数量**稳定地**从 A 变成 B 的位置。

    为什么要"稳定"：运动画面里的光斑会让连通域数量逐帧抖（1→2→1→0…），
    那不是穿帮，是噪声。只有"变过去之后**保持住**"才算真的少了个/多个东西 ——
    这正是「瓶盖消失」与「反光闪了一下」的区别。
    """
    runs = _value_runs(list(values))
    out: List[Tuple[int, int, int]] = []
    for k in range(1, len(runs)):
        a0, a1, av = runs[k - 1]
        b0, b1, bv = runs[k]
        if bv == av:
            continue
        if (a1 - a0) >= hold and (b1 - b0) >= hold:
            out.append((b0, av, bv))
    return out


def blob_count_series(roi_gray, *, frac: float = 0.6, min_area: int = 12,
                      flat_tol: float = 0.05):
    """ROI 内"亮物体"数量随时间的变化序列。

    阈值取 ROI 自身的**动态范围**分位点：`thr = p5 + frac*(p99.5 - p5)`。
    为什么不用 `mean + k*std`（技能库里 onset 用的是这个）：本场景常是
    "黑桌面上一个白盖子"，白占了 ROI 大半，`mean+k*std` 会算到 1.0 以上，
    结果**永远数不到东西**（实测踩过）。分位点法对这种二值化场景稳得多。

    ROI 内部几乎平坦（动态范围 < flat_tol）时返回 0：没有可数的物体。

    局限要说清：光斑、高光反光、手入画都会干扰；它只用来**圈重点**，
    结论必须抽帧目视（`contact_sheet`）。
    """
    import numpy as np
    from scipy import ndimage

    n = roi_gray.shape[0]
    counts = np.zeros(n, dtype=np.int32)
    for i in range(n):
        fr = roi_gray[i]
        lo = float(np.percentile(fr, 5))
        hi = float(np.percentile(fr, 99.5))
        if hi - lo < flat_tol:
            counts[i] = 0
            continue
        mask = fr > lo + frac * (hi - lo)
        lab, num = ndimage.label(mask)
        if num == 0:
            counts[i] = 0
            continue
        sizes = ndimage.sum(mask, lab, index=range(1, num + 1))
        counts[i] = int(sum(1 for s in sizes if s >= min_area))
    return counts


# --------------------------------------------------------------------------- #
# 主分析
# --------------------------------------------------------------------------- #

def detect_expected_elements(prompt: str) -> List[str]:
    """提示词里声明了哪些"结果性元素"。返回归一化名字（mist/smoke/...）。

    只有**声明了**才去检起点：没写喷雾的镜头检不到雾是正常的，
    不该报"喷雾无来源"。
    """
    if not prompt:
        return []
    low = prompt.lower()
    hits: List[str] = []
    for name, words in ELEMENT_HINTS.items():
        if any(w.lower() in low for w in words):
            hits.append(name)
    return hits


def analyze(
    path: str,
    *,
    fps: float = 24.0,
    roi: Optional[Tuple[float, float, float, float]] = None,
    prompt: str = "",
    expect_elements: Optional[Sequence[str]] = None,
    recorded_onset_sec: Optional[float] = None,
    expect_silent: bool = False,
    count_roi: Optional[Tuple[float, float, float, float]] = None,
    min_rise: float = DEFAULT_MIN_RISE,
    freeze_eps: float = DEFAULT_FREEZE_EPS,
    black_level: float = DEFAULT_BLACK_LEVEL,
    flicker_db: float = DEFAULT_FLICKER_DB,
    late_sec: float = DEFAULT_LATE_SEC,
    onset_tol: float = DEFAULT_ONSET_TOL,
) -> QcReport:
    """全量分析一条出片。**纯读、不写库、不发请求**。"""
    import numpy as np

    rep = QcReport(path=str(path), fps=float(fps))
    try:
        v = probe_video(path)
        rep.fps = float(v.fps or fps)
        rep.duration = float(v.duration or 0.0)
        rep.frames = int(v.nb_frames or 0)
        rep.size = (int(v.width or 0), int(v.height or 0))
        rep.has_audio = bool(v.has_audio)
    except Exception as e:  # noqa: BLE001
        rep.note = f"无法探测视频：{type(e).__name__}: {e}"
        rep.findings.append(QcFinding(
            code="probe_failed", severity="high", label="无法读取视频",
            detail=rep.note, suggestion="检查产物是否损坏或路径是否失效",
        ))
        return rep

    try:
        f = _gray_frames(str(path))
    except Exception as e:  # noqa: BLE001
        rep.note = f"无法解码视频帧：{type(e).__name__}: {e}"
        rep.findings.append(QcFinding(
            code="decode_failed", severity="high", label="无法解码视频帧",
            detail=rep.note, suggestion="产物可能损坏；重出该镜",
        ))
        return rep

    n, H, W = f.shape
    if rep.frames <= 0:
        rep.frames = n
    bright = f.mean(axis=(1, 2))
    # 帧间平均绝对差 = "动了多少"；下标 i 对应第 i 帧与第 i-1 帧之间
    motion = np.zeros(n, dtype=np.float32)
    if n > 1:
        motion[1:] = np.abs(f[1:] - f[:-1]).mean(axis=(1, 2))
    # 第 0 帧没有"前一帧"，差值恒为 0 —— 会被误判成开头冻结，故置为哨兵大值
    if n > 0:
        motion[0] = 1.0

    rep.metrics = {
        "brightness_first": round(float(bright[0]), 4),
        "brightness_last": round(float(bright[-1]), 4),
        "brightness_mean": round(float(bright.mean()), 4),
        "brightness_std": round(float(bright.std()), 4),
        "motion_mean": round(float(motion[1:].mean()), 5) if n > 1 else 0.0,
        "motion_max": round(float(motion.max()), 5),
        "dup_ratio": (round(float((motion[1:] < DEFAULT_DUP_EPS).mean()), 3)
                      if n > 1 else 0.0),
    }

    per_sec = max(1.0, rep.fps or 24.0)

    # ---- 1. 黑场 ----
    black_runs = find_bad_runs(list(bright), limit=black_level,
                               min_frames=int(0.3 * per_sec))
    for a, b in black_runs:
        rep.findings.append(QcFinding(
            code="black", severity="high", label="黑场",
            detail=f"{a / per_sec:.2f}s–{b / per_sec:.2f}s 共 {b - a} 帧平均亮度"
                   f"<{black_level:g}",
            suggestion="生成截断或转场异常；重出该镜，或确认是否刻意的黑场转场",
            at_sec=round(a / per_sec, 3),
            evidence={"start_frame": int(a), "end_frame": int(b)},
        ))

    # ---- 2. 静止 / 冻结帧 ----
    if n > 1:
        tail = motion[1:][-int(0.5 * per_sec):] if n > int(0.5 * per_sec) else motion[1:]
        if len(tail) and float(tail.mean()) < freeze_eps:
            rep.findings.append(QcFinding(
                code="freeze_tail", severity="high", label="尾部冻结帧",
                detail=f"最后 {len(tail)} 帧几乎无变化（帧差均值 "
                       f"{float(tail.mean()):.5f}）",
                suggestion="生成在结尾停下（时长给多了）。装配时用 -t 裁到动作收尾处，"
                           "或缩短请求时长",
                at_sec=round((n - len(tail)) / per_sec, 3),
            ))
        mid_runs = find_bad_runs(list(motion), limit=freeze_eps,
                                 min_frames=int(0.5 * per_sec))
        for a, b in mid_runs:
            if b >= n - int(0.5 * per_sec):
                continue  # 尾部已单独报过
            rep.findings.append(QcFinding(
                code="freeze_mid", severity="medium", label="中段冻结帧",
                detail=f"{a / per_sec:.2f}s–{b / per_sec:.2f}s 共 {b - a} 帧几乎无变化",
                suggestion="这一段画面是「死」的；若该镜应有持续动作，重出",
                at_sec=round(a / per_sec, 3),
            ))
        if float(motion[1:].mean()) < freeze_eps:
            rep.findings.append(QcFinding(
                code="static", severity="high", label="整镜几乎无运动",
                detail=f"全片帧差均值 {float(motion[1:].mean()):.5f} < {freeze_eps:g}",
                suggestion="模型没有让画面动起来（可能只生成了静帧）。检查提示词的 "
                           "[MOTION] 段是否被吞，或重出",
            ))
        dup = float((motion[1:] < DEFAULT_DUP_EPS).mean())
        if dup > 0.5:
            rep.findings.append(QcFinding(
                code="duplicate_frames", severity="medium", label="重复帧过多",
                detail=f"{dup * 100:.0f}% 的相邻帧几乎完全相同",
                suggestion="等效于掉帧，动作会一顿一顿；重出或提高生成帧率",
            ))

    # ---- 3. 亮度跳变（拼接 / 闪烁）----
    if n > 2:
        d = np.abs(np.diff(bright))
        for i in np.where(d > flicker_db)[0]:
            rep.findings.append(QcFinding(
                code="flicker", severity="medium", label="亮度跳变",
                detail=f"{i / per_sec:.2f}s 处亮度突跳 {float(d[i]):.3f}"
                       f"（阈值 {flicker_db:g}）",
                suggestion="典型拼接点或曝光闪烁；若是拼接，核对切口处的构图与色调是否连续",
                at_sec=round(i / per_sec, 3),
            ))

    # ---- 4. 结果性元素起点（复用技能库同一套判据）----
    r = roi or DEFAULT_ROI
    x0, y0, x1, y1 = (int(r[0] * W), int(r[1] * H), int(r[2] * W), int(r[3] * H))
    s = f[:, y0:y1, x0:x1].mean(axis=(1, 2))
    ks = np.ones(3) / 3
    s = np.convolve(s, ks, mode="same")
    skip = max(2, int(0.25 * per_sec))
    best, best_i = 0.0, None
    for i in range(skip, n - 8):
        after = float(s[i + 2:i + 8].mean())
        before = float(s[max(skip, i - 6):max(skip + 1, i - 1)].mean())
        if after - before > best:
            best, best_i = after - before, i
    detected = bool(best_i is not None and best >= min_rise)
    onset_sec = round(best_i / per_sec, 3) if detected else None
    rep.metrics["element_rise"] = round(float(best), 4)
    rep.metrics["element_onset_sec"] = onset_sec

    want = list(expect_elements) if expect_elements is not None else \
        detect_expected_elements(prompt)
    rep.metrics["expected_elements"] = want

    if want and not detected:
        rep.findings.append(QcFinding(
            code="element_missing", severity="high",
            label=f"声明了{'/'.join(want)}但未检出结果性元素",
            detail=f"提示词声明了 {'/'.join(want)}，但 ROI 内最大持续跃升 "
                   f"{best:+.4f} < 阈值 {min_rise:g}",
            suggestion="这就是「喷雾无来源／雾没出来」。要么改提示词让元素真的发生，"
                       "要么该镜本来就不该有元素（那就把元素词从提示词删掉）；"
                       "ROI 不对也会漏检，先抽帧拼图目视确认",
            evidence={"roi": list(r), "rise": round(float(best), 4)},
        ))
    elif detected and onset_sec is not None and onset_sec > late_sec:
        rep.findings.append(QcFinding(
            code="element_late", severity="medium", label="结果性元素前摇过长",
            detail=f"元素在 {onset_sec:.2f}s 才出现（阈值 {late_sec:g}s）",
            suggestion="模型对结果性元素有固有前摇。装配时用 --shot 路径:时长@起始 "
                       "把开头裁掉，音效按**裁切后的真实起点**落位",
            at_sec=onset_sec,
        ))

    # ---- 5. 实测起点 vs 台账起点（音效落位会错位）----
    if recorded_onset_sec is not None and onset_sec is not None:
        delta = abs(float(recorded_onset_sec) - onset_sec)
        if delta > onset_tol:
            rep.findings.append(QcFinding(
                code="onset_mismatch", severity="medium", label="台账起点与实测不符",
                detail=f"台账 onset={float(recorded_onset_sec):.2f}s，"
                       f"实测 {onset_sec:.2f}s，差 {delta:.2f}s",
                suggestion="音效（喷头声）会与雾的可见起点错位。换提示词必须重测起点，"
                           "然后重排音效落点",
                at_sec=onset_sec,
            ))

    # ---- 6. 源音轨残留 ----
    if expect_silent and rep.has_audio:
        rep.findings.append(QcFinding(
            code="audio_residue", severity="medium", label="镜头自带音轨未剔除",
            detail="这条产物带音轨，但镜头音轨应在装配时丢弃（-an）并由本地重建",
            suggestion="拼接前用 -an 丢掉源音轨；否则各镜环境声不同会造成拼接跳变",
        ))

    # ---- 7. ROI 内物件数量突变（瓶盖消失 / 复制）----
    if count_roi is not None:
        cx0, cy0, cx1, cy1 = (int(count_roi[0] * W), int(count_roi[1] * H),
                              int(count_roi[2] * W), int(count_roi[3] * H))
        if cx1 > cx0 and cy1 > cy0:
            try:
                counts = blob_count_series(f[:, cy0:cy1, cx0:cx1])
                modes = [int(x) for x in counts]
                rep.metrics["count_series_mode"] = modes[0] if modes else 0
                hold = max(3, int(0.4 * per_sec))   # 变化必须"站得住"才算
                changes = steady_changes(modes, hold=hold)
                for i, prev, cur in changes:
                    rep.findings.append(QcFinding(
                        code="object_count_change", severity="medium",
                        label="ROI 内物件数量突变",
                        detail=f"{i / per_sec:.2f}s 处亮物体数量 {prev} → {cur}"
                               f"（疑似物件消失/复制）",
                        suggestion="先出逐格拼图目视确认（autocontact sheet）；"
                                   "确认穿帮就重出，并在 [NEG] 里锁数量，"
                                   "例如「One cap only: no second cap」",
                        at_sec=round(i / per_sec, 3),
                        evidence={"prev": prev, "cur": cur, "roi": list(count_roi)},
                    ))
            except Exception as e:  # noqa: BLE001 - 检测失败不该毁整份报告
                rep.metrics["count_check_error"] = f"{type(e).__name__}: {e}"

    rep.findings.sort(key=lambda x: (x.at_sec if x.at_sec is not None else -1,
                                     -SEVERITY_RANK.get(x.severity, 0)))
    return rep


# --------------------------------------------------------------------------- #
# 逐格拼图（自动判据圈重点，拼图下结论）
# --------------------------------------------------------------------------- #

def contact_sheet(path: str, out: str, *, cols: int = 4, rows: int = 4,
                  cell_width: int = 240, seconds: Optional[Sequence[float]] = None
                  ) -> str:
    """出逐格拼图 PNG，供目视复核构图漂移/物件穿帮。

    `seconds` 给定时**按指定时刻抽帧**（例如 onset 前后、数量突变处），
    这比等间隔抽帧有用得多 —— 你只想看"出事的那一下"。
    """
    out_p = Path(out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    total = cols * rows
    if seconds:
        picks = list(seconds)[:total]
        # 按实际抽帧数收窄行数：给了 4 个时刻却排 4×4，剩下 12 格会填成白块，
        # 整张图大半是空白，看起来像出错。
        use_rows = max(1, -(-len(picks) // max(1, cols)))
        # select 表达式：逐帧比对目标时刻最近的帧号
        terms = []
        for t in picks:
            idx = max(0, int(round(float(t) * 24)))
            terms.append(f"eq(n\\,{idx})")
        sel = "+".join(terms) if terms else "eq(n\\,0)"
        vf = (f"select='{sel}',scale={cell_width}:-1,"
              f"tile={cols}x{use_rows}:padding=4:color=white")
    else:
        vf = (f"fps=1,scale={cell_width}:-1,"
              f"tile={cols}x{rows}:padding=4:color=white")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vf", vf, "-frames:v", "1", str(out_p)])
    return str(out_p)
