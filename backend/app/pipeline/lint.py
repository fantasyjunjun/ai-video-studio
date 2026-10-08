"""提示词静态检查（lint 门禁）—— 由 skills/scripts/lint_prompt.py 移植为库函数。

移植原则：**逻辑逐行等效**，保证旧基线分数零漂移。
任何改动都必须重跑基线（见 _smoke_p1.py 的基线校验），
修的是误判，不是把标准放松。

7 个维度，各 0–2 分，满分 14；低于阈值（默认 8）不得提交生成。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

# ---------------------------------------------------------------- 词表

ACTION_STEMS = [
    "lift", "raise", "press", "push", "turn", "rotate", "open", "close",
    "walk", "step", "reach", "touch", "spray", "tilt", "lower", "drop",
    "pick", "put", "place", "take", "exhale", "inhale", "breathe", "smile",
    "blink", "nod", "lean", "bend", "slide", "twist", "uncap", "pull",
    "bring", "wipe", "sweep", "brush", "apply", "dab", "wrap",
    "cross", "rise", "grip", "grasp", "insert", "remove", "squeeze",
    "shake", "pour", "dip", "swirl",
]

# 限定词后的同形词是名词用法（"the press"），不算节拍
DETERMINERS = {"the", "a", "an", "this", "that", "its", "their", "each",
               "one", "my", "her", "his", "another"}

# 与动词同形的比较级（"settle lower"）
COMPARATIVE_ADJS = {"lower", "higher", "closer", "farther", "further",
                    "wider", "tighter", "softer", "longer", "slower"}
_AFTER_COMPARATIVE = re.compile(r"(at|than|to|into|on|over|down|and|or|from)\b")

# 系动词后的 -ed 是状态描述（"her eyes are closed"）
STATIVE_PRECEDERS = {"is", "are", "was", "were", "am", "be", "been", "being",
                     "stay", "stays", "stayed", "remain", "remains", "remained",
                     "keep", "keeps", "kept", "sit", "sits", "sat",
                     "rest", "rests", "rested", "look", "looks", "looked",
                     "feel", "feels", "felt", "seem", "seems", "seemed"}

STATIC_VERBS = [
    "stays", "stay", "keeps", "keep", "remains", "remain", "stands", "stand",
    "lies", "lying", "rests", "resting", "sits", "sit", "sitting",
    "hold", "holds", "holding", "held",
]

RESULT_ELEMENTS = [
    "mist", "spray", "sprays", "smoke", "steam", "droplet", "droplets",
    "particle", "particles", "dust motes", "splash", "spatter", "foam",
    "condensation", "beads of water", "petals falling", "falling petals",
    "ashes", "embers",
]

ANCHOR_PATTERNS = [
    r"originat\w+ at", r"emerg\w+ from", r"from the nozzle", r"from the tip",
    r"at the nozzle", r"from the nozzle tip", r"extends from",
    r"clinging to", r"clings to", r"clinging on", r"attached to",
    r"in the (same )?direction", r"in the direction the nozzle",
    r"from the rim", r"from the glass", r"on the (glass|bottle|surface)",
    r"falling off", r"off the (rim|edge|tip)", r"drifts? (away )?from",
    r"pours? from", r"rises? from",
    r"keeps? its position", r"in place", r"without (travelling|drifting)",
    r"does not (travel|drift)", r"never (travels|drifts)",
    r"only (thins|fades|loses|dims)", r"loses? (its )?brightness",
]

GENERIC_STYLE = [
    "cinematic color grade", "luxury fragrance commercial",
    "luxury product commercial", "high contrast", "slow motion",
    "film grain", "moody", "dramatic", "beautiful", "stunning", "elegant",
    "high quality", "4k", "8k", "masterpiece", "aesthetic", "professional",
    "ultra detailed", "trending on artstation", "hyper realistic",
]

CONCRETE_STYLE = [
    r"\b\d{2,3}\s*mm\b",
    r"\bT\s*\d+(\.\d+)?\b",
    r"\bf/\d+(\.\d+)?\b",
    r"\b\d{3,4}\s*K\b",
    r"\b\d+\s*:\s*\d+\b",
    r"\b\d+\s*°",
    r"black flag", r"scrim", r"softbox", r"bounce", r"practical light",
    r"hard (key|light)", r"soft (key|light)", r"rim light", r"kicker",
    r"portra", r"vision3", r"cinestill", r"ektachrome",
    r"caustic", r"subsurface", r"specular", r"falloff",
    r"camera-left", r"camera-right", r"upper left", r"upper right",
]

BOILERPLATE_NEG = [
    "no blur", "no flickering", "no morphing", "no watermark",
    "no text overlay", "no extra objects in frame", "no distortion",
]

TAG_OLD = ["L1", "L2", "L3", "L4", "L5", "L6", "L7"]
TAG_NEW = ["SHOT", "HERO", "MOTION", "CAMERA", "LIGHT", "TEXTURE", "REF", "NEG"]


# ---------------------------------------------------------------- 解析


def split_sections(text: str) -> Dict[str, str]:
    secs: Dict[str, str] = {}
    hits = list(re.finditer(r"\[([A-Za-z0-9_]+)\]", text))
    if not hits:
        secs["__plain__"] = text.strip()
        return secs
    for i, m in enumerate(hits):
        tag = m.group(1).upper()
        start = m.end()
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        body = text[start:end].strip()
        secs[tag] = (secs.get(tag, "") + "\n" + body).strip()
    return secs


def words(s: str) -> int:
    return len(re.findall(r"[A-Za-zÀ-ÿ']+", s or ""))


def detect_format(secs: Dict[str, str]) -> str:
    if any(t in secs for t in ("SHOT", "HERO", "MOTION", "REF", "NEG")):
        return "new"
    if any(t in secs for t in TAG_OLD):
        return "old"
    return "plain"


def strip_negation_clauses(s: str) -> str:
    parts = re.split(r"[;.,]|\band\b|\bthen\b", s or "")
    stem_re = r"\b(" + "|".join(ACTION_STEMS) + r")(s|es|ed|ing)?\b"
    keep = []
    for p in parts:
        low = p.lower()
        if re.search(r"\b(no|not|never|without|avoid|nor)\b", low):
            continue
        if any(v in low for v in STATIC_VERBS):
            if not re.search(stem_re, low):
                continue
            keep.append(p)
            continue
        keep.append(p)
    return " ".join(keep)


def strip_negated_only(s: str) -> str:
    parts = re.split(r"[;.,]|\band\b|\bthen\b", s or "")
    keep = [p for p in parts
            if not re.search(r"\b(no|not|never|without|avoid|nor)\b", p.lower())]
    return " ".join(keep)


def count_beats(action_text: str) -> int:
    """数主动作（按词干归并）。三类同形假阳性已排除：名词/比较级/状态分词。"""
    cleaned = strip_negation_clauses(action_text)
    low = cleaned.lower()
    stems: List[str] = []
    for m in re.finditer(r"\b([a-z]+)\b", low):
        w = m.group(1)
        hit = None
        for stem in ACTION_STEMS:
            if re.fullmatch(re.escape(stem) + r"(s|es|ed|ing)?", w):
                hit = stem
                break
        if not hit:
            continue
        prev = low[:m.start()].split()
        if prev and prev[-1] in DETERMINERS:
            continue
        if w in COMPARATIVE_ADJS:
            rest = low[m.end():].lstrip()
            if not rest or rest[0] in ".,;:" or _AFTER_COMPARATIVE.match(rest):
                continue
        if w.endswith("ed") and prev and prev[-1] in STATIVE_PRECEDERS:
            continue
        if hit not in stems:
            stems.append(hit)
    return len(stems)


def get_action_text(secs: Dict[str, str], fmt: str) -> str:
    if fmt == "new":
        return (secs.get("SHOT", "") + " " + secs.get("MOTION", "")).strip()
    if fmt == "old":
        return secs.get("L2", "")
    return secs.get("__plain__", "")


def get_anchor_text(secs: Dict[str, str], fmt: str) -> str:
    if fmt == "new":
        return secs.get("REF", "")
    if fmt == "old":
        return secs.get("L1", "")
    return ""


def get_camera_text(secs: Dict[str, str], fmt: str) -> str:
    if fmt == "new":
        return secs.get("CAMERA", "")
    if fmt == "old":
        return secs.get("L3", "")
    return ""


def get_style_text(secs: Dict[str, str], fmt: str) -> str:
    if fmt == "new":
        return (secs.get("LIGHT", "") + " " + secs.get("TEXTURE", "")).strip()
    if fmt == "old":
        return (secs.get("L4", "") + " " + secs.get("L5", "")
                + " " + secs.get("L6", "")).strip()
    return ""


def get_neg_text(secs: Dict[str, str], fmt: str) -> str:
    if fmt == "new":
        return secs.get("NEG", "")
    if fmt == "old":
        return secs.get("L7", "")
    return ""


def beats_allowed(duration: float) -> int:
    if duration <= 3.0:
        return 1
    if duration <= 6.0:
        return 2
    return 3


# ---------------------------------------------------------------- 评分


@dataclass
class LintReport:
    name: str
    fmt: str
    duration: float
    beats: int
    beats_allow: int
    anchor_words: int
    scores: Dict[str, int] = field(default_factory=dict)
    total: int = 0
    issues: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.total >= DEFAULT_MIN_SCORE

    def to_dict(self) -> dict:
        return {
            "name": self.name, "format": self.fmt, "duration": self.duration,
            "beats": self.beats, "beats_allow": self.beats_allow,
            "anchor_words": self.anchor_words, "scores": self.scores,
            "total": self.total, "issues": self.issues, "notes": self.notes,
        }


DEFAULT_MIN_SCORE = 8


def lint_prompt(text: str, duration: float, name: str = "") -> LintReport:
    """对单条提示词打分。与原 lint_prompt.py::score_prompt 逐行等效。"""
    secs = split_sections(text)
    fmt = detect_format(secs)
    issues: List[str] = []
    notes: List[str] = []

    # --- 结构
    if fmt == "new":
        required = ["SHOT", "HERO", "MOTION", "CAMERA", "LIGHT", "REF", "NEG"]
        missing = [t for t in required if t not in secs]
        if missing:
            issues.append(f"缺少结构层: {', '.join('[' + m + ']' for m in missing)}")
    elif fmt == "old":
        missing = [t for t in TAG_OLD if t not in secs]
        if missing:
            issues.append(f"缺少层: {', '.join(missing)}")
        notes.append("旧七层格式（建议迁移到 v2 模板）")
    else:
        issues.append("未找到任何 [TAG] 结构标记")

    # --- 1 节拍
    atxt = get_action_text(secs, fmt)
    beats = count_beats(atxt)
    allow = beats_allowed(duration)
    if beats <= allow:
        s_beats = 2
    elif beats <= allow + 1:
        s_beats = 1
        issues.append(f"节拍偏多：{beats} 个（{duration:g}s 镜建议 ≤{allow}）→ 会退化成'静止图感'")
    else:
        s_beats = 0
        issues.append(f"节拍过多：{beats} 个（{duration:g}s 镜建议 ≤{allow}）→ 必须拆镜或砍动作")

    # --- 2 锚点精简（剔除引号内字面文本，如逐瓶指派的标签文字）
    anchor = get_anchor_text(secs, fmt)
    n_anchor = words(re.sub(r'"[^"]*"', " ", anchor))
    if n_anchor == 0:
        s_anchor = 0
        issues.append("没有主体锚点段：模型没有人物/产品的一致性依据")
    elif n_anchor <= 25:
        s_anchor = 2
    elif n_anchor <= 60:
        s_anchor = 1
        issues.append(f"锚点段 {n_anchor} 词（建议 ≤25）→ 与参考图互相打架，脸会被'平均化'")
    else:
        s_anchor = 0
        issues.append(f"锚点段 {n_anchor} 词，严重超长（建议 ≤25）→ i2v 里越长越差")

    # --- 3 运镜量化
    cam = get_camera_text(secs, fmt)
    has_amount = bool(re.search(
        r"(\d+(\.\d+)?\s*%|frame (width|height)|\b\d{2,3}\s*mm\b|degrees|"
        r"about \d|by about)", cam, re.I))
    has_lock = bool(re.search(
        r"(no cut|does not cut|never cuts|no zoom|no rotation|no shake|"
        r"static camera|locked[- ]off|no change of angle|no pan|no tilt)", cam, re.I))
    if has_amount and has_lock:
        s_cam = 2
    elif has_amount or has_lock:
        s_cam = 1
        issues.append("运镜只写对了一半：" + ("缺'不切不摇'锁定" if has_amount else "缺位移量（模型无法标定 'extremely slow'）"))
    else:
        s_cam = 0
        issues.append("运镜没有量化也没有锁死：模型会自己乱动或中途切镜")

    # --- 4 物理锚点
    positive = strip_negated_only(text).lower()
    whole = text.lower()
    present = [e for e in RESULT_ELEMENTS if re.search(r"\b" + re.escape(e) + r"\b", positive)]
    if not present:
        s_phys = 2
        notes.append("本镜无结果性元素，物理锚点项不打折")
    else:
        anchored = any(re.search(p, whole) for p in ANCHOR_PATTERNS)
        if anchored:
            s_phys = 2
        else:
            s_phys = 0
            issues.append(
                f"有结果性元素（{', '.join(sorted(set(present))[:3])}）但没有物理锚点 → "
                "会画成不属于任何物体的通用粒子云")

    # --- 5 风格具体
    sty = get_style_text(secs, fmt).lower()
    n_generic = sum(1 for g in GENERIC_STYLE if g in sty)
    n_concrete = sum(1 for p in CONCRETE_STYLE if re.search(p, sty, re.I))
    if n_concrete >= 2 and n_generic <= 2:
        s_style = 2
    elif n_concrete >= 1:
        s_style = 1
        notes.append(f"风格参数 {n_concrete} 项 / 通用词 {n_generic} 项，可再具体化")
    else:
        s_style = 0
        issues.append(f"风格全是通用词（命中 {n_generic} 个："
                      + "、".join([g for g in GENERIC_STYLE if g in sty][:3])
                      + "）→ 会得到'模型默认电影感'")

    # --- 6 hero
    hero = secs.get("HERO", "")
    if hero and re.search(r"(only|sole|single|subordinate|soft|out of focus|"
                          r"defocused|sharp)", hero, re.I):
        s_hero = 2
    elif hero:
        s_hero = 1
        notes.append("[HERO] 已声明但没写其余元素的降级方式")
    else:
        s_hero = 0
        issues.append("没有 [HERO] 声明 → 画面每样东西都在抢注意力，等于没有焦点")

    # --- 7 反向约束
    neg = get_neg_text(secs, fmt)
    n_neg = len(re.findall(r"\b(no|not|never|do not|does not|without)\b", neg, re.I))
    specific = False
    if neg:
        stripped = neg.lower()
        for b in BOILERPLATE_NEG:
            stripped = stripped.replace(b, "")
        specific = bool(re.search(r"\b(no|not|never)\b", stripped))
    if n_neg >= 3 and specific:
        s_neg = 2
    elif n_neg >= 1:
        s_neg = 1
        if not specific:
            notes.append("反向约束全是通用模板，建议补 1–2 条本镜特有风险")
    else:
        s_neg = 0
        issues.append("没有反向约束：常见崩坏（手、闪烁、变形）不会被抑制")

    total = s_beats + s_anchor + s_cam + s_phys + s_style + s_hero + s_neg
    return LintReport(
        name=name, fmt=fmt, duration=duration, beats=beats, beats_allow=allow,
        anchor_words=n_anchor,
        scores={
            "节拍": s_beats, "锚点精简": s_anchor, "运镜量化": s_cam,
            "物理锚点": s_phys, "风格具体": s_style, "hero": s_hero,
            "反向约束": s_neg,
        },
        total=total, issues=issues, notes=notes,
    )


def lint_many(items, min_score: int = DEFAULT_MIN_SCORE):
    """items: [(name, text, duration), ...]"""
    return [lint_prompt(t, d, n) for n, t, d in items]
