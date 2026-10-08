"""主播定妆照（2x2 四视图）提示词构造。

**为什么是"一次生成四格"而不是"生成四张再拼"**：只靠文字描述的主播在每次
生成之间都会漂脸（同一批次里也是一个人一个样）。把正面 / 侧面 / 背面 / 特写
**放在同一次生成里**，模型只能在同一个潜在表征下画完四格 —— 物理上就是同一个人。
下游（分镜九宫格、整片、逐镜关键帧）再拿这张定妆照当参考图，人物就不换脸了。

**纯函数，只造提示词，不做 I/O**（生成与落盘在 api/assets.py 的 sheet 接口里）。
其中 `REAL_FACE_CONSTRAINT` 直接沿用 ClipForge 的实测文案：视频模型默认会画出
过度精修的"网红脸"，一眼就假、把整片质感拉低；实测的甜蜜点是"清爽耐看的普通人"
—— 既禁网红脸，也禁刻意丑化（列雀斑/痘/眼袋会过校正成不好看的脸）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from . import talent_spec as spec_mod

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def has_cjk(s: str) -> bool:
    return bool(s) and bool(_CJK_RE.search(s))


REAL_FACE_CONSTRAINT = {
    # 第一段：长什么样。**任何图都要** —— 参考图靠它挡住 AI 精修脸。
    "person_zh": (
        "人物是清爽耐看的普通人长相：五官端正有亲和力，看着舒服讨喜，日常淡妆，"
        "发丝自然，皮肤有自然真实质感不磨皮——不是精修网红脸，也绝不刻意丑化"
    ),
    "person_en": (
        "the person is a pleasant, ordinary-looking real human: well-proportioned "
        "approachable features that are easy on the eyes, everyday light makeup, "
        "natural hair, real un-retouched skin texture — not a polished influencer AI "
        "face, and never deliberately unattractive either"
    ),
    # 第二段：环境的随意感。**只有参考图要用**，氛围图必须禁用 ——
    # 它要求"自然光、不打影棚光、像手机随手拍"，一旦写进氛围图，就会和用户
    # 亲自选的"单硬光 4:1 光比""杂志大片质感"正面打架，结果是两边都被稀释。
    "casual_zh": "画质像手机随手拍带轻微噪点，自然光，不打影棚光、无广告片精致感",
    "casual_en": ("footage looks like a casual phone shot with slight noise and "
                  "natural light, never studio-lit, no ad-grade polish"),
}


def real_face_line(surrounding_text: str, *, casual: bool = True) -> str:
    """按周围文案的语言选约束语；空白/英文走英文。

    `casual=False` 只取"长相"那一段。氛围图必须传 False —— 见上。
    """
    zh = surrounding_text and has_cjk(surrounding_text)
    person = REAL_FACE_CONSTRAINT["person_zh" if zh else "person_en"]
    if not casual:
        return person
    casual_part = REAL_FACE_CONSTRAINT["casual_zh" if zh else "casual_en"]
    return f"{person}；{casual_part}" if zh else f"{person}; {casual_part}"


# ==========================================================================
# 形象提示词生成（文本大模型）
# ==========================================================================
#
# 用户只填"27 岁 / 女 / 中国"这种干巴巴的基本信息，直接拿去生图会得到一张
# 谁也不是的脸，而且每次都不一样。中间必须补一层：**把基本信息 + 十六个维度的
# 选择，变成一段可画的人物形象描述**。
#
# **这一层现在是"润色"，不再是"扩写"**：
#
# 旧做法是让模型从三个字段自由发明一整套外貌，结果不可预期 —— 用户点两下
# "再生成一版"，得到的可能只是同一张脸换了个说法。现在 `talent_spec` 已经把
# 每个选项写成了确定的中英双语片段（`pipeline/talent_spec.py`），模型拿到的
# 是一份**已经写完的素材**，它只负责行文：
#
#     - 不许新增素材里没有的特征（尤其是五官细节这种最容易擅自发挥的）
#     - 不许删掉素材里已有的特征
#     - 不许改写具体数值（4:1 光比、85mm、腰部以上……）
#
# 于是"选了什么就一定出现在提示词里"这件事由代码保证，模型只承担润色的不确定性
# —— 而润色失败还有兜底：直接用未润色的拼装结果，见 `polish_or_fallback`。

POLISH_SYSTEM_ZH = """\
你是广告片选角文案的编辑。你会拿到一组**已经确定好**的人物特征片段，
任务是把它们组织成一段通顺、可直接喂给文生图模型的中文描述。

严格约束（违反任何一条即判失败）：
- 只允许做三件事：调整语序、补连接词、合并重复表述
- 不得新增片段中不存在的任何特征（尤其不要擅自补五官细节如高鼻梁、双眼皮）
- 不得删除片段中已有的任何特征
- 不得改写数字、颜色词、材质词、光位与光学参数（例如 4:1 光比、85mm、腰部以上）
- 只输出这一段描述：不分点、不加解释、不加引号、不用 markdown
"""

POLISH_SYSTEM_EN = """\
You are an editor of casting copy for advertising stills. You will receive a set of
**already decided** character trait fragments. Your job is to arrange them into one
flowing English description ready to be fed to a text-to-image model.

Strict constraints (violating any one of them counts as failure):
- You may only do three things: reorder fragments, add connecting words, merge duplicates
- Never add any trait that is not already in the fragments (do not invent facial details
  such as a high nose bridge or double eyelids)
- Never drop any trait that is present in the fragments
- Never rewrite numbers, colour words, material words, light placement or optical
  parameters (e.g. a 4:1 ratio, 85mm, mid-thigh up)
- Output only that one description: no bullets, no explanation, no quotes, no markdown
"""

# 没有任何结构化素材时的兜底：让模型自由扩写。这是**退化路径**——
# 正常流程里 `talent_spec` 一定已经在前面拼好了东西，不会走到这里。
FALLBACK_SYSTEM_ZH = """\
你是电商短视频的选角导演，把人物基本信息写成一段**用于文生图的人物形象描述**。

只输出这段描述本身，不要任何解释、不要分点、不要加引号、不要用 markdown。
要求：一段连贯的话，20 到 35 个词；只描写稳定可见的外貌（年龄段性别、族裔气质、
脸型、发型发色、肤色、身材、整体气质）；不要写情绪、动作、服装、场景、光照；
不要出现真人姓名或品牌名；不要用 beautiful / hot / sexy 这类空泛词；
要真实的人，可以有自然皮肤质感，不要"完美无瑕""精修网红脸"。
"""

FALLBACK_SYSTEM_EN = """\
You are a casting director for e-commerce short videos. Turn the given basic profile
into ONE short character description suitable for a text-to-image model.

Output only that description: no explanation, no bullets, no quotes, no markdown.
Rules: one flowing sentence, 20 to 35 words, never a list. Describe only stable visible
appearance (age range and gender, ethnic look, face shape, hair style and colour, skin
tone, body build, overall presence). Never describe mood, action, clothing, scene or
lighting. Never use real person names or brand names. Avoid vague words like beautiful /
hot / sexy. Keep the person real: natural skin texture is fine, a "flawless retouched
influencer face" is not.
"""


def build_appearance_gen_messages(profile: Dict[str, Any], lang: str = "en",
                                  hint: str = "", previous: str = "",
                                  drafted: str = "") -> Tuple[str, str]:
    """构造「让文本大模型加工形象提示词」的 (system, user)。

    `drafted` 是 `talent_spec.build_spec_sentence` 已经拼好的东西 —— 有它就进入
    **润色模式**：模型只许调整行文，不许增删特征。没有它才退化到自由扩写
    （留着是为了不让旧调用路径直接崩掉）。
    """
    lang = (lang or "en").lower()
    en = lang.startswith("en")
    drafted = (drafted or "").strip()

    system = POLISH_SYSTEM_EN if en else POLISH_SYSTEM_ZH
    if not drafted:
        system = FALLBACK_SYSTEM_EN if en else FALLBACK_SYSTEM_ZH

    lines: List[str] = []
    lines.append("Basic profile:")
    for key, label in (("name", "Name"), ("age", "Age"),
                       ("gender", "Gender"), ("nationality", "Nationality")):
        v = profile.get(key)
        if v not in (None, ""):
            lines.append(f"- {label}: {v}")

    if drafted:
        lines.append("")
        lines.append("<established_fragments>")
        lines.append(drafted)
        lines.append("</established_fragments>")
        lines.append("")
        lines.append(
            "Rewrite the fragments above into one flowing paragraph"
            if en else "把上面这些片段组织成一段通顺的描述")
    else:
        lines.append(f"Write the description in {'English' if en else '中文'}.")

    if (hint or "").strip():
        lines.append(f"- Extra requirement: {hint.strip()}")
    if (previous or "").strip():
        lines.append("")
        lines.append("A previous version was rejected:")
        lines.append(f"<previous>{previous.strip()}</previous>")
        lines.append("Make this version clearly different in its actual traits "
                     "— do not merely rephrase the same look."
                     if en else
                     "这一版要换掉实际的特征组合 —— 不要只是把同一套说法换个讲法。")
    return system, "\n".join(lines)


_COVERAGE_MIN = 0.72

_STOP_EN = frozenset("""a an the with and or of in on at to for its his her their is are was were
that this these those as by from into over under about after before than then there here
very much more most some any each both few other such no not only just also own same""".split())


def _tokens(s: str, lang: str) -> set:
    """抽取特征 token。

    英文用词袋（去停用词）；中文用**二字滑窗** —— 语序调整时相邻两字基本保留，
    而整条特征被删掉时这些 bigram 会消失，正好是我们想抓的信号。
    """
    if lang == "zh":
        s = re.sub(r"[^\u4e00-\u9fff]+", "", s or "")
        return {s[i:i + 2] for i in range(len(s) - 1)}
    words = re.findall(r"[a-z']+", (s or "").lower())
    return {w for w in words if len(w) > 3 and w not in _STOP_EN}


def feature_coverage(drafted: str, polished: str, lang: str = "en") -> float:
    """改写后的文本保留了多少原始素材特征（0~1）。"""
    need = _tokens(drafted, lang)
    if not need:
        return 1.0
    have = _tokens(polished, lang)
    return len(need & have) / len(need)


def polish_or_fallback(drafted: str, polished: str, lang: str = "en",
                       *, min_coverage: float = _COVERAGE_MIN) -> Tuple[str, str]:
    """决定用哪一版：**默认使用未润色的拼装原文，只有覆盖率够高才用润色稿**。

    system prompt 里写了"不许删特征"，但那只是请求，不是保证 —— 大模型润色时
    悄悄丢东西（尤其是它觉得啰嗦的具体参数）是常态。丢一次用户根本看不出来，
    只会觉得"我明明选了这套，怎么没生效"。这里用覆盖率做硬性判据：

        覆盖率 < 0.72 → 认定它删了特征，退回未润色的拼装结果。

    返回值是 `(采用的版本, 说明)`，说明会原样进 API 响应，便于用户排查。
    """
    drafted = (drafted or "").strip()
    text = normalize_appearance(polished)
    if not text:
        return drafted, "模型没有返回可用文本，已使用结构化拼装原文"
    if not drafted:
        return text, "（无原文可对照，直接使用模型输出）"
    cov = feature_coverage(drafted, text, lang)
    if cov < min_coverage:
        return (drafted,
                f"润色稿丢失了过多已选特征（保留率 {cov:.0%} < "
                f"{min_coverage:.0%}），已改用结构化拼装原文")
    return text, f"已按所选维度拼装并润色（特征保留率 {cov:.0%}）"


_FENCE_RE = re.compile(r"^\s*(?:```[a-zA-Z]*|`)\s*(.*?)\s*(?:```|`)?\s*$", re.S)


def normalize_appearance(text: str) -> str:
    """把模型输出收敛成一行干净的提示词。

    模型很爱包 markdown 代码围栏、加引号、或先寒暄一句"好的，这是…"。
    这里只做**保守清洗**：去围栏、去首尾引号、压空白、**取第一个非空行**；
    不做任何改写（改写会让"用户看到的"和"存进库的"不一致）。
    """
    t = (text or "").strip()
    if not t:
        return ""
    m = _FENCE_RE.match(t)
    if m:
        t = (m.group(1) or "").strip() or t
    lines = [ln.strip().strip("\"'“”‘’ ").strip() for ln in t.splitlines()]
    lines = [ln for ln in lines if ln]
    # 去掉模型的寒暄前言（"Here is the description:" / "好的，这是："）——
    # **只在后面还有内容时才丢**，否则把唯一一行也丢掉了就成了空提示词。
    while len(lines) > 1 and lines[0].endswith((":", "：")):
        lines.pop(0)
    return re.sub(r"\s+", " ", lines[0]).strip() if lines else ""


# ==========================================================================
# 正面图 / 三视图
# ==========================================================================


def build_front_view_prompt(appearance: str, name: Optional[str] = None,
                            spec: Optional[Dict[str, Any]] = None) -> str:
    """正面全身图（正面图）。

    与 2x2 定妆照的区别：单图、全身入画、只有正面一个机位 —— 用于
    "看清楚这个人长什么样"的场合（商品页配图、九宫格封面）。

    这张图是下游 i2v 的参考图，所以**画面层（场景/布光/机位）一律不接受用户
    选择**，固定为中性棚拍 —— 见 `talent_spec.Dim.reference_safe`。
    """
    appearance = (appearance or "").strip()
    who = (name or "").strip()
    if has_cjk(appearance):
        return "\n".join([
            "一张人物正面全身参考图，竖构图，人物从头到脚完整入画，站姿自然放松、双臂自然下垂。",
            "浅灰纯色摄影棚背景，柔和均匀布光，画面干净无道具、无第二个人。",
            f"人物设定{('（' + who + '）') if who else ''}：{appearance}。"
            "写实人体比例，头身比约 1:7~7.5。",
            real_face_line(appearance) + "。",
            "硬性要求：画面里只有一个人且全身完整可见；不出现任何文字、水印或边框装饰。",
        ])
    return "\n".join([
        "A front-view full-body character reference photo, vertical framing, the person "
        "head to feet fully in frame, natural relaxed standing pose with arms at the sides.",
        "Plain light-gray studio background, soft even lighting, no props and no second person.",
        f"Character{(f' ({who})' if who else '')}: {appearance}. Realistic human proportions, "
        "head-to-body ratio about 1:7-7.5.",
        real_face_line(appearance) + ".",
        "Hard rules: exactly one person, full body visible; no text, watermark or decorative border.",
    ])


def build_three_view_prompt(appearance: str, name: Optional[str] = None,
                            spec: Optional[Dict[str, Any]] = None) -> str:
    """人物三视图（正面 / 左侧面 / 背面 三格横排）。

    **三格必须在同一次生成里画出来** —— 分三次生成就是三个采样，发际线和
    脸型必然对不上，三视图也就失去了"锁住同一个人"的意义。
    """
    appearance = (appearance or "").strip()
    who = (name or "").strip()
    if has_cjk(appearance):
        return "\n".join([
            "一张横向三等分的人物三视图参考图：从左到右依次是正面全身、左侧面全身、背面全身，"
            "三格等高，格与格之间只留极细的白色分隔缝，浅灰纯色摄影棚背景。",
            "三格是同一个人物在同一时刻的三个机位——同一张脸、同一发型、同一身衣服、同一身形气质。",
            f"人物设定{('（' + who + '）') if who else ''}：{appearance}。"
            "写实人体比例，头身比约 1:7~7.5。",
            real_face_line(appearance) + "。",
            "硬性要求：严格三等分；不出现任何文字、编号、水印或边框装饰；三格必须是完全同一个人。",
        ])
    return "\n".join([
        "A horizontal three-view character reference sheet: from left to right — front full body, "
        "left-side full body, back full body. Three equal-height cells separated only by "
        "hairline white gutters, on a plain light-gray studio background.",
        "All three cells are the SAME person captured at the same moment from three angles — "
        "identical face, hair, outfit and body proportions.",
        f"Character{(f' ({who})' if who else '')}: {appearance}. Realistic human proportions, "
        "head-to-body ratio about 1:7-7.5.",
        real_face_line(appearance) + ".",
        "Hard rules: strictly three equal cells; no text, numbers, watermarks or decorative "
        "borders anywhere; the person must be exactly identical in all three cells.",
    ])


def build_character_sheet_prompt(appearance: str,
                                 name: Optional[str] = None,
                                 spec: Optional[Dict[str, Any]] = None) -> str:
    """构造 2x2 四视图定妆照提示词。

    语言跟随 `appearance` 本身的语种（写中文就用中文提示词）—— 中英混写会让
    模型在两种先验之间摇摆，反而更容易漂脸。

    硬约束与分镜九宫格保持一致：严格等分、**不出现任何文字/编号/水印/边框**。
    定妆照是当参考图往下游传的，图上多一行字就会被后续镜头原样复刻进画面。
    """
    appearance = (appearance or "").strip()
    who = (name or "").strip()
    if has_cjk(appearance):
        return "\n".join([
            "一张 2x2 等分四视图人物定妆参考图，整图 1:1 正方形，格与格之间只留极细的白色分隔缝。",
            "四格是同一个人物在同一时刻的四个机位——同一张脸、同一发型、同一身衣服、"
            "同一站姿气质，浅灰纯色摄影棚背景。",
            f"人物设定{('（' + who + '）') if who else ''}：{appearance}。"
            "写实人体比例，头身比约 1:7~7.5，不做漫画式九头身拉长。",
            "四格内容：左上=正面全身；右上=左侧面全身；左下=背面全身；"
            "右下=正面肩部以上特写（清晰展示五官）。",
            real_face_line(appearance) + "。",
            "硬性要求：严格等分四格；画面里不出现任何文字、编号、水印或边框装饰；"
            "四格人物必须完全是同一个人。",
        ])
    return "\n".join([
        "A 2x2 four-view character reference sheet, square 1:1 overall, "
        "cells separated only by hairline white gutters.",
        "All four cells are the SAME person captured at the same moment from four "
        "angles — identical face, hair, outfit and posture, on a plain light-gray "
        "studio background.",
        f"Character{(f' ({who})' if who else '')}: {appearance}. Realistic human "
        "proportions, head-to-body ratio about 1:7-7.5, never stylized elongated "
        "hero proportions.",
        "Cells: top-left = front full body; top-right = left-side full body; "
        "bottom-left = back full body; bottom-right = front shoulders-up close-up "
        "(features clearly visible).",
        real_face_line(appearance) + ".",
        "Hard rules: strictly equal cells; no text, numbers, watermarks or decorative "
        "borders anywhere; the person must be exactly identical in all four cells.",
    ])


def build_poster_prompt(appearance: str, name: Optional[str] = None,
                        spec: Optional[Dict[str, Any]] = None) -> str:
    """氛围图（单张成品图）—— **唯一让"画面层"生效的地方**。

    正面图 / 三视图 / 定妆照是给下游当参考图用的，一旦写上"咖啡厅""逆光"，那个
    背景会被后续每一个镜头原样复刻进成片。所以当用户想把"书房""逆光轮廓光"
    "七分身构图"这些选项真正用在画面上时，出的应该是这张图 —— 它不当参考图，

    对照正面图的固定棚拍，这里的场景、光线、姿势、构图、色调、风格全部
    来自 `talent_spec` 的 render 层，由用户勾选。
    """
    appearance = (appearance or "").strip()
    who = (name or "").strip()
    lang = "zh" if has_cjk(appearance) else "en"
    # 画面层独立成句，放在人物描述之后 —— 生图模型对"先写人、再写怎么拍"的
    # 分段结构遵循度最好
    render = spec_mod.render_sentence(spec_mod.normalize_spec(spec), lang)
    quality = spec_mod.QUALITY_BASE[lang]

    if lang == "zh":
        parts = ["一张完成度高的人物氛围成品图，单张构图。"]
        if render:
            parts.append(f"{render}。")
        parts.append(
            f"人物设定{('（' + who + '）') if who else ''}：{appearance}。"
            "写实人体比例，头身比约 1:7~7.5。")
        parts.append(real_face_line(appearance, casual=False) + "。")
        parts.append(f"{quality}。")
        parts.append("硬性要求：画面里只有一个人物；不出现任何文字、水印或边框装饰。")
    else:
        parts = ["A finished single-frame atmospheric portrait photograph."]
        if render:
            parts.append(f"{render}.")
        parts.append(
            f"Character{(f' ({who})' if who else '')}: {appearance}. Realistic human "
            "proportions, head-to-body ratio about 1:7-7.5.")
        parts.append(real_face_line(appearance, casual=False) + ".")
        parts.append(f"{quality}.")
        parts.append("Hard rules: exactly one person in frame; no text, watermark "
                     "or decorative border.")
    return "\n".join(parts)


# kind → (提示词构造函数，中文标签，是否放行画面层)。出图接口按这张表分派，
# 加一种新图只需在这里补一行。**放在文件末尾**，因为它要引用上面全部构造函数
# （模块级字典字面量会在 import 时求值，写在定义之前会 NameError）。
#
# 第三列的 False 不是疏忽：参考图类必须保持中性棚拍，详见 `build_poster_prompt`
# 的文档串与 `talent_spec.Dim.reference_safe`。
TALENT_IMAGE_KINDS = {
    "front": (build_front_view_prompt, "正面图", False),
    "threeview": (build_three_view_prompt, "人物三视图", False),
    "sheet": (build_character_sheet_prompt, "2x2 四视图定妆照", False),
    "poster": (build_poster_prompt, "氛围图", True),
}

# 登记白名单 = 上面四类 **加上** `variant`（"其他参考图"）。
#
# 为什么 `variant` 不能直接塞进 `TALENT_IMAGE_KINDS`：出图接口是靠那张表**分派
# 提示词构造函数**的，多一个 key 就会在取函数时炸。而 `variant` 天生没有构造函数 ——
# 它只能"导入"（用户手上已有换装图），不能"生成"。
#
# 所以这里用**同一个形状**的元组把它并进来，构造函数写 `None` 表示"没有生成器"，
# 文案与是否放行画面层照旧。这样登记接口、前端下拉、画廊标签仍然只认一份真相；
# 第一列是 None 就代表"别给出图按钮"。第三列 `allows_render=False` 也如实反映了
# `services/refs.py` 的 `REF_TALENT_KINDS`：variant 是参考图，不是成品图。
REGISTERABLE_TALENT_IMAGE_KINDS = {
    **TALENT_IMAGE_KINDS,
    "variant": (None, "其他参考图", False),
}


def build_talent_image_prompt(kind: str, appearance: str,
                              name: Optional[str] = None,
                              spec: Optional[Dict[str, Any]] = None) -> str:
    """只要正向提示词（旧签名的地方仍在用）。"""
    return build_talent_image_prompt_parts(kind, appearance, name, spec)[0]


def build_talent_image_prompt_parts(kind: str, appearance: str,
                                    name: Optional[str] = None,
                                    spec: Optional[Dict[str, Any]] = None
                                    ) -> Tuple[str, str]:
    """构造 (正向提示词, 负面提示词)。

    负面词为什么要单独走一路：
     OpenAI 兼容的图片接口与 ComfyUI 工作流都有 `negative_prompt` 这个真正的入口，
    写在那里的约束不会被当成画面内容去生成 —— 而写进正文的"不要出现水印"，模型
    有时反而把它读成一个要画出来的东西（"field" 类词被当实物的经典翻车点）。
    两条路一起用是双保险： provider 支持就用专用通道，不支持则正文里的
    "硬性要求"仍兜得住。
    """
    if kind not in TALENT_IMAGE_KINDS:
        raise ValueError(
            f"未知的主播图类型：{kind}（可选：{'/'.join(TALENT_IMAGE_KINDS)}）")
    fn, _label, allow_render = TALENT_IMAGE_KINDS[kind]
    # 参考图类把画面层剔干净：用户选过的那些值不被丢弃，只是这一次不写进去
    used_spec = spec_mod.normalize_spec(spec) if allow_render else \
        spec_mod.filter_spec_for_reference(spec_mod.normalize_spec(spec))
    prompt = fn(appearance, name, used_spec)
    lang = "zh" if has_cjk(appearance) else "en"
    return prompt, spec_mod.NEGATIVE_BASE[lang]
