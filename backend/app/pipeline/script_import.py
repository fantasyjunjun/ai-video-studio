"""把分镜脚本（markdown）解析成可落库的镜头。

两种来源都要吃得下：

  1. **Trae 按 fragrance-ecom-video 模板产出的脚本**（结构固定，走严格路径）；
  2. **用户从别处粘贴的外部脚本**（格式五花八门，走宽松兜底）。

模板长这样：

    ### S1（0.0-3.0s｜钩子）

    ```text
    [L1] ... [L7] ...
    ```

    **中文直译**：...

解析器只依赖两样东西：**小节标题行**（拿镜号与时间码）与**提示词本体**。
提示词按优先级取：**围栏块** > `**提示词**：` 标签行 > 整段正文兜底。
对外部脚本额外放宽：`##`/`###`/`####` 都算小节、`Shot 1`/`镜头1` 这类前缀
可识别、没写镜号时自动编 `S1..Sn`、时间码兼容 `0-3s` / `0s-3s` / `0～3秒`。

刻意**不解析 markdown 表格**（提示词部分）：模板里那张「分镜表」的中文列是画面简述
与念白，**不是** i2v 提示词，把中文散文当提示词导入只会产出跑偏的镜头。检测到看起来
像提示词表的表格时，只给一条提示让用户改成分节格式。

但**念白稿表与音效落位表另开一个入口**（`parse_post_plan`）：那两张表的列义是稳定的
（时间码 + 文本 / 类型 + 落点），而且它们**不参与提示词**、只用来驱动 TTS 与混音。
交给人工在音频页逐行重敲既慢又易错，所以由脚本直接喂给一键成片。

解析失败**不抛异常**：返回 (shots, warnings)，由调用方决定是报错还是给提示。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 逐镜小节标题：## / ### / ####（Trae 用 ###，外部脚本常用 ##）
_SECTION_RE = re.compile(r"^#{2,4}[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)
# 标题里的「Shot / Scene / 镜头…」前缀词，剥掉再取镜号
_CODE_PREFIX_RE = re.compile(
    r"^[ \t]*(?:shot|scene|clip|cut|镜头|片段|场景|画面)[ \t]*", re.IGNORECASE
)
# 镜号：S1 / A1 / B1b / 1
_CODE_RE = re.compile(r"^[ \t]*(?P<code>[A-Za-z]{0,2}\d+[A-Za-z]?)")
# 时间码：0.0-3.0s（兼容半角/全角破折号与波浪号、至/到，秒单位两侧均可省）
# 全角符号要显式列出：`～`(U+FF5E) / `〜`(U+301C) / `－`(U+FF0D) 都跟 ASCII 的 `~`/`-`
# 不是同一个码位，只写半角会把「镜头3（7～11s）」判成没写时间码。
_RANGE_SEP = r"[-–—~～〜－至到]"
_RANGE_RE = re.compile(
    r"(?P<a>\d+(?:\.\d+)?)\s*(?:s|sec|秒)?\s*" + _RANGE_SEP + r"\s*"
    r"(?P<b>\d+(?:\.\d+)?)\s*(?:s|sec|秒)?"
)
# 提示词围栏块（语言标记可有可无：text / markdown / 无）
_FENCE_RE = re.compile(
    r"```[ \t]*(?:text|txt|markdown|md|prompt|en)?[ \t]*\r?\n(?P<body>.*?)```",
    re.DOTALL,
)
# 「**英文提示词（i2v）**：」「Prompt:」这类标签行（读到空行或下一个小标题为止）
_LABEL_RE = re.compile(
    r"^[ \t]*(?:\*\*)?[ \t]*(?:i2v[ \t]*prompt|english[ \t]*prompt|prompt|"
    r"英文提示词|正向提示词|提示词)[ \t]*(?:\*\*)?[ \t]*[：:][ \t]*"
    r"(?P<body>.*?)(?=\n[ \t]*\n|\n[ \t]*(?:#{1,6}[ \t])|"
    r"\n[ \t]*(?:\*\*)?[ \t]*(?:中文直译|直译|念白|音效|时间码|镜号)|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
# 中文直译行
_ZH_RE = re.compile(r"\*\*中文直译\*\*\s*[：:]\s*(?P<zh>.+)")
# 疑似「提示词表格」：表头里出现 prompt / 提示词 / 英文
_TABLE_PROMPT_RE = re.compile(r"^\s*\|[^\n]*(?:prompt|提示词|英文)", re.IGNORECASE | re.MULTILINE)
# 整段正文兜底的最低长度：太短的正文多半不是提示词（避免把「钩子」两字当提示词）
_MIN_FALLBACK_CHARS = 60
_ASCII_RE = re.compile(r"[A-Za-z]{3}")
# 正文兜底要求「以英文为主」：i2v 提示词是英文，非逐镜小节（念白稿/关键决策说明）
# 都是中文散文，用 ASCII 占比就能把它们挡在门外（实测 32% / 62% vs 提示词 ~100%）
_MIN_ASCII_RATIO = 0.70

# ---------------------------------------------------------------- 分镜表的两列元数据
# ★R-33：`scriptgen.merge_script` 在分镜表里多写了「商品」「造型」两列 ——
# 前者是**逐镜商品分配的唯一落库入口**（出片时按它决定挂哪个商品的参考图），
# 后者是章节级形象差异化的记录。这两列是**我们自己写的固定列**，所以从表格
# 读回是安全的；读不到就留空，`refs` 回退"挂项目全部商品"的旧行为。
_META_SHOTID_COLS = ("镜号", "镜头", "shot")
_META_PRODUCT_COLS = ("商品", "product")
_META_LOOK_COLS = ("造型", "服装", "look")
# 商品 code：服务端生成的是 P01 / P02，允许带一个尾字母（P01A 这类手工变体）。
# **只认 P 前缀**——放宽成 `[A-Z]\d+` 会把镜号（A1、B1b）也吞进来。
_PRODUCT_CODE_RE = re.compile(r"\bP\d{1,4}[A-Za-z]?\b")


def _shot_meta_from_tables(text: str) -> Dict[str, Dict[str, Any]]:
    """从分镜表读回 `{镜号: {"products": [...], "talent_look": "..."}}`。"""
    out: Dict[str, Dict[str, Any]] = {}
    for tbl in _parse_tables(text):
        if len(tbl) < 2:
            continue
        header = tbl[0]
        ci = _find_col(header, _META_SHOTID_COLS)
        pi = _find_col(header, _META_PRODUCT_COLS)
        li = _find_col(header, _META_LOOK_COLS)
        if ci is None or (pi is None and li is None):
            continue
        for cells in tbl[1:]:
            m = _CODE_RE.match(_CODE_PREFIX_RE.sub("", _cell(cells, ci)))
            if not m:
                continue
            rec = out.setdefault(_normalize_code(m.group("code")), {})
            if pi is not None:
                codes = _PRODUCT_CODE_RE.findall(_cell(cells, pi).upper())
                if codes:
                    rec["products"] = list(dict.fromkeys(codes))
            if li is not None:
                look = _cell(cells, li).strip()
                if look and look not in ("—", "-", "/"):
                    rec["talent_look"] = look
    return out


def _ascii_ratio(s: str) -> float:
    return sum(1 for c in s if ord(c) < 128) / max(1, len(s))


def _normalize_code(raw: str) -> str:
    """把裸数字镜号补成项目惯用的 S 前缀（1 → S1）。"""
    raw = raw.strip()
    return f"S{raw}" if raw.isdigit() else raw


@dataclass
class ParsedShot:
    code: str
    duration_sec: float
    prompt_en: str
    prompt_zh: str = ""
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None
    # 提示词是从哪来的：fence / label / body —— 给调用方决定要不要提示用户
    prompt_source: str = "fence"
    # ★R-33 逐镜商品分配：本镜画面里出现的商品 code（从分镜表的「商品」列读回）。
    # 出片时**只有这些商品的图**会挂给该镜（services/refs.py），为空表示
    # "脚本没写"→ 回退旧行为（挂项目全部商品的图）。
    products: List[str] = field(default_factory=list)
    # ★R-33 章节级造型：本镜的服装/发型覆盖（分镜表「造型」列），空=沿用主播锚点。
    talent_look: str = ""


def _clean_body(block: str) -> str:
    """整段正文兜底：去掉中文直译行、表格行、分隔线后剩下的正文。"""
    kept: List[str] = []
    for ln in block.splitlines():
        s = ln.strip()
        if not s or s in {"---", "***", "___"}:
            continue
        if s.startswith("|"):          # 表格行：模板里那是中文简述，不是提示词
            continue
        if _ZH_RE.search(ln):          # 直译行不是提示词
            continue
        kept.append(ln.rstrip())
    return "\n".join(kept).strip()


def _split_sections(text: str) -> List[Tuple[str, str]]:
    """按 `##`/`###`/`####` 切小节 → [(标题, 块正文)]。"""
    heads = list(_SECTION_RE.finditer(text))
    out: List[Tuple[str, str]] = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((h.group("title").strip(), text[h.end():end]))
    return out


def parse_script(md: str) -> Tuple[List[ParsedShot], List[str]]:
    """解析分镜脚本 markdown → (shots, warnings)。"""
    warnings: List[str] = []
    text = md or ""
    if not text.strip():
        return [], ["脚本内容为空"]

    sections = _split_sections(text)
    if not sections:
        if _TABLE_PROMPT_RE.search(text):
            return [], [
                "没找到镜头小节标题，但检测到疑似「提示词表格」。"
                "表格列的顺序/含义各家不同，无法可靠解析——请改成分节格式："
                "每个镜头一个 `### S1（0.0-3.0s）` 标题，提示词放进 ```text 代码块。"
            ]
        return [], ["未找到镜头小节标题（应形如 '### S1（0.0-3.0s｜钩子）'）"]

    shots: List[ParsedShot] = []
    seen: set[str] = set()
    for ordinal, (title, block) in enumerate(sections, start=1):
        if not block.strip():
            continue                    # 空小节（如 '## 逐镜提示词' 后紧跟 '### S1'）静默跳过

        fm = _FENCE_RE.search(block)
        bare = _CODE_PREFIX_RE.sub("", title)
        cm = _CODE_RE.match(bare) or _CODE_RE.match(title)

        # 只认「像镜头」的小节：标题带镜号 / 带时间码 / 正文里有提示词围栏块。
        # 否则像「## 分镜表」「## 念白稿」「## 自检评分」这类章节会被正文兜底
        # 误当成镜头导入（Trae 的模板在镜头之后还有 4 个 ## 章节）。
        if not (cm or _RANGE_RE.search(title) or fm):
            continue

        # 1) 提示词：围栏块 > 标签行 > 整段正文
        if fm and fm.group("body").strip():
            prompt_en, source = fm.group("body").strip(), "fence"
        else:
            lm = _LABEL_RE.search(block)
            if lm and lm.group("body").strip():
                prompt_en, source = lm.group("body").strip(), "label"
            else:
                body = _clean_body(block)
                if len(body) < _MIN_FALLBACK_CHARS or _ascii_ratio(body) < _MIN_ASCII_RATIO:
                    continue            # 太短或中文为主：这不是 i2v 提示词
                prompt_en, source = body, "body"

        # 2) 镜号：显式取，取不到就按小节序号自动编
        if cm:
            code = _normalize_code(cm.group("code"))
        else:
            code = f"S{ordinal}"
            warnings.append(f"第 {ordinal} 个小节没写镜号，已自动命名为 {code}")
        if code in seen:                # 同一镜号出现两次：保留最后一次
            shots = [s for s in shots if s.code != code]
        seen.add(code)

        # 3) 时间码（标题里找不到就看正文开头）
        rng = _RANGE_RE.search(title) or _RANGE_RE.search(block[:200])
        start = end_s = None
        if rng:
            start, end_s = float(rng.group("a")), float(rng.group("b"))
        dur = round(end_s - start, 3) if (start is not None and end_s is not None
                                          and end_s > start) else 3.0
        if start is None:
            warnings.append(f"{code}：没写时间码，按时长 3.0s 处理（可在分镜页改）")

        if source == "label":
            warnings.append(f"{code}：没找到 ```text 提示词块，已取「提示词」标签行内容")
        elif source == "body":
            warnings.append(f"{code}：没找到提示词块/标签，已把整段正文当提示词")

        zm = _ZH_RE.search(block)
        shots.append(ParsedShot(
            code=code, duration_sec=dur, prompt_en=prompt_en,
            prompt_zh=(zm.group("zh").strip() if zm else ""),
            start_sec=start, end_sec=end_s, prompt_source=source,
        ))

    if not shots:
        warnings.append("未解析到任何可用镜头（请检查小节标题与提示词围栏块）")
    else:
        # ★R-33：把分镜表的「商品 / 造型」两列贴回各自镜头（逐镜商品分配）。
        meta = _shot_meta_from_tables(text)
        for s in shots:
            rec = meta.get(s.code) or {}
            s.products = list(rec.get("products") or [])
            s.talent_look = str(rec.get("talent_look") or "")
        chinese_only = [s.code for s in shots if not _ASCII_RE.search(s.prompt_en)]
        if chinese_only:
            warnings.append(
                "以下镜头的提示词里没有英文，出片效果可能受限：" + "、".join(chinese_only)
            )
    return shots, warnings


# ---------------------------------------------------------------- 后期计划（念白 / 音效）
#
# 与提示词解析**完全分开**：上面 `parse_script` 刻意不碰表格（列义不可靠、会污染提示词），
# 这里解析的两张表却是给后期用的 —— 念白稿（时间码 + 文本）与音效落位（类型 + 落点）。
# 让脚本自己喂 TTS 与混音，用户就不必在音频页把 4~5 行葡语念白逐字重敲一遍。

# 小节标题关键词。**不要放裸 "vo"**："video" 里也有 vo，会误命中。
_VO_TITLE_KEYS = ("念白", "旁白", "口播", "配音", "voice", "narration")
_SFX_TITLE_KEYS = ("音效", "sfx", "sound")
# 表头列名关键词（按优先级）
_VO_TIME_COLS = ("时间码", "时间区间", "区间", "起止", "时间")
_VO_TEXT_COLS = ("念白", "旁白", "口播", "配音", "文案", "字幕", "text", "voice")
_VO_SHOT_COLS = ("对应镜头", "镜头", "镜号", "shot")
_SFX_TIME_COLS = ("落位时间", "落位", "时间")
_SFX_KIND_COLS = ("音效类型", "类型", "音效", "kind")

_TABLE_ROW_RE = re.compile(r"^[ \t]*\|(?P<body>[^\n]*)\|[ \t]*$", re.MULTILINE)
_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")
# 分镜表「音效」列这种内联写法：`喷头声 mist @3.2s`
_SFX_KINDS = ("mist", "spray", "click", "glass", "breath")
_SFX_INLINE_RE = re.compile(
    r"\b(" + "|".join(_SFX_KINDS) + r")\b[ \t]*@[ \t]*(\d+(?:\.\d+)?)[ \t]*s?,?",
    re.IGNORECASE,
)


@dataclass
class ParsedPostPlan:
    """脚本里能直接驱动后期的信息。"""

    vo_rows: List[Dict[str, object]] = None       # [{start,end,text,shot}]
    sfx_cues: List[Dict[str, object]] = None      # [{kind,at,note}]
    warnings: List[str] = None

    def __post_init__(self) -> None:
        self.vo_rows = self.vo_rows or []
        self.sfx_cues = self.sfx_cues or []
        self.warnings = self.warnings or []

    def to_dict(self) -> Dict[str, object]:
        return {"vo_rows": self.vo_rows, "sfx_cues": self.sfx_cues,
                "warnings": self.warnings}

    @property
    def vo_text(self) -> str:
        """整条念白文本（**一次 TTS 请求**用；音色在开头一次性锁定）。"""
        return " ".join(str(r.get("text", "")).strip()
                        for r in self.vo_rows if str(r.get("text", "")).strip())


def _table_cells(row: str) -> List[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _is_sep_row(cells: List[str]) -> bool:
    meaningful = [c for c in cells if c]
    return bool(meaningful) and all(_SEP_CELL_RE.match(c) for c in meaningful)


def _parse_tables(block: str) -> List[List[List[str]]]:
    """解析块内所有 markdown 表格 → [表][行][列]（分隔行已剔除）。

    表格之间以"行号不连续"为界 —— 比"空行分表"可靠，因为表格内部没有空行。
    """
    tables: List[List[List[str]]] = []
    cur: List[List[str]] = []
    prev_line: Optional[int] = None
    for m in _TABLE_ROW_RE.finditer(block):
        line_no = block.count("\n", 0, m.start())
        if prev_line is not None and line_no != prev_line + 1 and cur:
            tables.append(cur)
            cur = []
        prev_line = line_no
        cells = _table_cells(m.group("body"))
        if _is_sep_row(cells):
            continue
        cur.append(cells)
    if cur:
        tables.append(cur)
    return tables


def _find_col(header: List[str], keys: Tuple[str, ...]) -> Optional[int]:
    for i, h in enumerate(header):
        hl = h.lower()
        if any(k.lower() in hl for k in keys):
            return i
    return None


def _cell(cells: List[str], i: Optional[int]) -> str:
    return cells[i] if (i is not None and 0 <= i < len(cells)) else ""


def _normalize_sfx_kind(s: str) -> Optional[str]:
    """音效关键词归一。**spray 一律映射成 mist**（R-22，2026-09-30 用户拍板）。

    spray 的 8ms 阀门冲击实测听感是「啪嗒」，用户明确要求香水视频里全部用
    mist（纯水汽、零冲击）。关键词表里保留 spray（老脚本/模型偶尔还会写），
    但认出后**直接当 mist 用** —— 改写对用户可见（parse_post_plan 会给 warning），
    不做静默偷换。中文等认不出的词仍返回 None，由调用方如实标注。
    """
    low = (s or "").lower()
    if "spray" in low:
        return "mist"
    for k in _SFX_KINDS:
        if k != "spray" and k in low:
            return k
    return None


def _clean_vo_text(raw: str) -> str:
    """清洗念白单元格。**宁可返回空**，也不要把中文说明当念白念出来。

    Trae 模板里收尾镜常写「（留白 2.5s 给 CTA 字幕与产品定格）」—— 这不是念白。
    判据：出现"留白"，或整段没有连续 3 个拉丁字母（pt-BR/es/en 念白必有），一律跳过。
    """
    s = (raw or "").replace("**", "").strip().strip("｜|").strip()
    if not s or "留白" in s:
        return ""
    if not _ASCII_RE.search(s):
        return ""
    return " ".join(s.split())


def _vo_rows_from_table(tbl: List[List[str]]) -> List[Dict[str, object]]:
    if len(tbl) < 2:
        return []
    header, body = tbl[0], tbl[1:]
    ti = _find_col(header, _VO_TIME_COLS)
    xi = _find_col(header, _VO_TEXT_COLS)
    si = _find_col(header, _VO_SHOT_COLS)
    if ti is None or xi is None:
        return []
    out: List[Dict[str, object]] = []
    for cells in body:
        rng = _RANGE_RE.search(_cell(cells, ti))
        if not rng:
            continue
        start, end = float(rng.group("a")), float(rng.group("b"))
        if end <= start:
            continue
        text = _clean_vo_text(_cell(cells, xi))
        if not text:
            continue
        out.append({"start": round(start, 3), "end": round(end, 3),
                    "text": text, "shot": _cell(cells, si)})
    return out


def _sfx_rows_from_table(tbl: List[List[str]]) -> List[Dict[str, object]]:
    if len(tbl) < 2:
        return []
    header, body = tbl[0], tbl[1:]
    ai = _find_col(header, _SFX_TIME_COLS)
    ki = _find_col(header, _SFX_KIND_COLS)
    if ai is None:
        return []
    out: List[Dict[str, object]] = []
    for cells in body:
        tm = re.search(r"(\d+(?:\.\d+)?)", _cell(cells, ai))
        if not tm:
            continue
        kind = _normalize_sfx_kind(_cell(cells, ki)) or _normalize_sfx_kind(" ".join(cells))
        if not kind:
            continue
        out.append({"kind": kind, "at": round(float(tm.group(1)), 3),
                    "note": _cell(cells, ki)})
    return out


def _dedup_cues(cues: List[Dict[str, object]]) -> List[Dict[str, object]]:
    seen: set = set()
    out: List[Dict[str, object]] = []
    for c in cues:
        key = (c.get("kind"), c.get("at"))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def parse_post_plan(md: str) -> ParsedPostPlan:
    """从脚本里抽出念白行与音效落点，供「一键成片」直接使用。

    找不到不算错：返回空列表 + 一条 warning，由调用方决定是退回人工填写
    还是只出画面（**绝不静默产出一条没有念白的片子** —— 那正是用户投诉的
    "视频里只有背景音"）。
    """
    plan = ParsedPostPlan()
    text = md or ""
    if not text.strip():
        plan.warnings.append("脚本内容为空，无法提取念白/音效")
        return plan

    for title, block in _split_sections(text):
        tl = title.lower()
        if not plan.vo_rows and any(k in title for k in _VO_TITLE_KEYS):
            for tbl in _parse_tables(block):
                rows = _vo_rows_from_table(tbl)
                if rows:
                    plan.vo_rows = rows
                    break
        if not plan.sfx_cues and any(k in tl for k in _SFX_TITLE_KEYS):
            for tbl in _parse_tables(block):
                rows = _sfx_rows_from_table(tbl)
                if rows:
                    plan.sfx_cues = rows
                    break

    # 兜底：全文扫 `mist @3.2s` 这种内联写法（模板分镜表的「音效」列就长这样）
    if not plan.sfx_cues:
        plan.sfx_cues = _dedup_cues([
            {"kind": _normalize_sfx_kind(m.group(1)) or m.group(1).lower(),
             "at": round(float(m.group(2)), 3),
             "note": "内联写法兜底"}
            for m in _SFX_INLINE_RE.finditer(text)
        ])
    else:
        plan.sfx_cues = _dedup_cues(plan.sfx_cues)

    if not plan.vo_rows:
        plan.warnings.append(
            "脚本里没有可用的念白行（念白稿表为空或全是留白）——"
            "成片将只有画面与垫乐；如需旁白请在音频页手动补念白稿")
    if re.search(r"\bspray\b", text, re.IGNORECASE):
        # R-22：spray → mist 已在 _normalize_sfx_kind 里自动归一，这里只负责
        # 把改写**显式说出来**（限量/改写绝不静默）—— 用户对音效变化是敏感的。
        plan.warnings.append(
            "脚本里写了 spray（含阀门机械冲击，实测听感像「啪嗒」）——"
            "已按你的要求全部自动改用 mist（纯水汽、零冲击），无需手动修改")
    return plan
