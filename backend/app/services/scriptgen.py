# -*- coding: utf-8 -*-
"""并发生成 15s 带货脚本（R-14）：骨架 → 逐镜并发 → 合并。

## 为什么要这条路

原链路（Trae agent 单会话）实测 240-322s、3-7 轮、输出 12.8k-16.4k token。
拆开 trajectory 后看到**瓶颈是「输出 token ÷ 通道吞吐」**：本通道横评 5 个
文本模型，全部只有 40-90 token/s（同一渠道三档无差别；
推理模型思考量是正文的数倍）。也就是说 —— 耗时与
**输出 token 量几乎线性相关**，调超时、调重试都改变不了这个上限。

而输出 token 里有很大一块是「串行写 5 镜」本身造成的：
  ① 每次写入的工具调用参数要把文件内容整份放进 JSON（实测约 4k token 开销）；
  ② 每一轮都要把此前累积的 3-5 万 token 历史重发一遍；
  ③ agent 还会顺手回读文件、微调措辞（实测白花 47 秒，输出只有 1212 token）。

改法是把「通盘构思」与「逐镜展开」拆开：
  ① **骨架**（1 轮，约 1.5k token）：定下统一参考职责（R-25 起 H3 R2V 语义）、
     光线走向，以及每镜的动作 / 景别 / 运镜 / 念白 / 起止状态；
  ② **逐镜并发**（N 路同时，每路约 2.2k token）：每路只展开一镜；
  ③ **合并**：分镜表与念白稿由代码拼装（确定性，且不花一个 token）。
通道并发已实测可用：3 路并发总墙钟 19.2s ≈ 单个 16.1s，无 429 限流。

## 一致性怎么保证（并发方案唯一真正的风险）

并发最大的风险是「5 镜各写各的、拼起来不像一支片」。三道闸门：
  ① 锚点原文写进骨架，单镜提示词要求**逐字复用**；
  ② 每镜提示词里带上**前一镜的结束状态 / 后一镜的起始状态**——衔接靠数据传递，
     不靠模型自觉；
  ③ 合并时**用代码校验** `[REF]` / `[PRODUCT]` 行是否与骨架一致，不一致就以
     骨架为准替换并记 warning。**不指望模型每次都听话**。
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from app.pipeline import talent_spec as _talent_spec
from app.pipeline.script_import import _SFX_INLINE_RE, _normalize_sfx_kind

# ------------------------------------------------------------------ 规范常量

# v2 提示词模板的层序（来源：技能 `references/prompt-quality.md` 第二节）。
# 与旧七层的差别：运动优先重排，`[REF]` 从 110 词瘦身到 ≤25 词，
# 新增 `[HERO]`（解决"画面没有焦点"），`[NEG]` 要求针对本镜具体风险。
# R-25（H3 R2V）：`[REF]` 从"i2v 锚点"改为**参考图职责分配**（H3 官方规范：
# 每张参考图只负责一项，禁笼统的"与参考图保持一致"）；新增 `[AUDIO]` 声音层
# （H3 原生生成同步音频，不显式管理它就自己编，与后期配音打架）。
V2_LAYERS = (
    "[SHOT]    一句话：这一镜的主动作，≤15 词\n"
    "[HERO]    画面里唯一清晰焦点；其余元素显式降级\n"
    "[MOTION]  动作如何发生 + 时间位置（at the start / over the first second / "
    "unchanged through the clip）\n"
    "[CAMERA]  量化运镜（带位移量）+ 明确不做什么（no cut / no zoom / no rotation）\n"
    "[LIGHT]   光位 + 角度 + 光比 + 色温，用可执行参数\n"
    "[PHYSICS] 结果性元素的起点 / 方向 / 亮度关系（本镜没有就省略此层）\n"
    "[TEXTURE] 本镜要 highlight 的 2-3 个材质反应，不要罗列全部\n"
    "[REF]     R2V 参考职责分配，≤25 词：写清**该参考只负责什么**（人物参考只定义"
    "长相 / 发型 / 服装；产品参考只定义瓶型 / 材质 / 颜色）；**不重复描述外观细节**，"
    "禁止 consistent with the reference 这类笼统话\n"
    "[AUDIO]   声音层：环境声按画面挑 1-2 样（如 soft bathroom ambience），"
    "并以 no dialogue, no background music 收尾（念白与音效由后期配音混音）\n"
    "[NEG]     祈使句反向约束 3-5 条，按本镜具体风险裁剪，禁止抄模板"
)

# 一致性铁律（R-17 起拆成三段：通用 + 香水专属 + 非香水替代，按品类拼装）。
# 来源：技能 SKILL.md 通用铁律 + `references/storyboard-15s.md` 第五点五节 +
# `references/prompt-layering.md` 的喷香水模板。只摘与本任务直接相关的，
# 避免单镜提示词过长而稀释重点。
# R-16 的教训在里面留着：11 的"不许编造部件"来自实测编出商品图里根本没有的
# `silver metal cap with vertical ridges`；10 的水珠禁写来自油珠 + 油膜穿帮。
CONSISTENCY_RULES = """1. **单一动作**：≤3s 的镜只给一个完整动作；1-2s 的镜只给"一个半动作"（如轻微侧头回眸，不做整体转身）；4-5s 的镜可以一个动作 + 轻微延续。
2. **单镜内不得有物体状态改变**：开盖 / 拧盖 / 开盒 / 抽取 / 遮挡后现身 —— 必崩。
3. **运镜量化并锁死**：写出位移量（如 `slow push-in about 8% of the frame width`）并明确 `no cut / no zoom / no rotation`；不要只写"缓慢推镜"。**但反向词不许与本镜动作打架**：若本镜 `[MOTION]` 要求转身（`turns on the spot`）、步伐位移或推近，就**不要**在本镜 `[NEG]` 里再写 `no rotation / no camera movement / no pan` —— 模型收到互相矛盾的两条指令会随机取舍，实测把核心动作（喷雾后原地转身走入汽雾）整个丢掉；静止镜照常锁死即可。
4. **结果性元素必须绑物理锚点**：雾 / 烟 / 水珠 / 飘落物要给出起点、方向、亮度关系（示例格式 `mist shoots from the nozzle toward her skin, catching the key light as a thin visible beam`；香水片的喷雾落点选型见第 16 条，不要每片都默认同一落点）。
5. **风格词必须是可被摄影师执行的参数**，不能是"高级感""氛围感"这类情绪词。
6. **手部与产品主体接触是最易崩的组合**：优先"手部局部特写 + 产品静止"，不要写大幅手部动作。**画面只允许一位人物**：禁止第二个人入镜，也**禁止第二个人的任何身体部位**（手 / 脸 / 手臂 / 腿等）出现在画面里——人物与产品同框的镜，`[NEG]` 必锁 `no second person, no second person's hands or any other body part`，并写明画面里所有部位都属于人物本人（`every hand and body part in frame belongs to the woman`）。实测：主播在后景、前景一只拳 + 一只手持瓶——穿帮的本质不是"多了一只手"，是**第二个人的部位进了画面**，光锁"手"挡不住第二个人的脸和胳膊。
7. **不写液位 / 容量 / 用量变化**（模型本不会自己加，写了必崩）。
8. **产品主体上的小字（标签 / 克数 / 编号）一律后期叠加**，不要在提示词里指望模型出字。
9. **反向约束内嵌**：本通道不支持 `negative_prompt` 字段，一律写进 `[NEG]` 层。
10. **喷雾/水雾落点只留"极淡均匀光泽"**：凡写了喷雾动作的镜，`[SHOT]` / `[PHYSICS]` / `[TEXTURE]` 禁写 droplets / beads / drips / streaks / wet patches（模型会把它们画成油珠 + 油膜 + 流挂，实测穿帮）；要光泽就写 `a faint even sheen`，反向写 `no raised droplets, no beads, no drips`。
11. **产品锚点只写商品事实可证实的细节**：主体形态 / 颜色 / 材质必须来自商品名称与卖点描述，**不许编造商品图里没有的部件**（实测编出商品图里根本没有的 `silver metal cap with vertical ridges`）。
12. **参考图走 H3 R2V 职责分配，不写 i2v 锚点**：`[REF]` / `[PRODUCT]` 只写"该参考**负责什么**"（人物参考只定义长相 / 发型 / 服装；产品参考只定义瓶型 / 材质 / 颜色），≤25 词；**禁止把外观细节在提示词里再写一遍**（与参考图构成两套互相冲突的指令，脸会被"平均化"），禁止 `consistent with the reference` 这类笼统话。提示词必须**自足**：场景 / 动作 / 镜头 / 声音讲清楚，画面不是由图单独决定的。
13. **每镜必写 `[AUDIO]` 声音层**：H3 原生生成同步音频，不写它就会自己编（乱加音乐 / 环境声，与后期配音打架）。写法：环境声按画面挑 1-2 样（如 `soft bathroom ambience`），并以 `no dialogue, no background music` 收尾——念白与音效由后期配音混音，不进视频提示词。"""

# 香水/香氛专属（品类判定为香水时追加；判定见 is_perfume_products）
PERFUME_RULES = """14. **喷香水镜（全片必须至少一镜）**：第一帧就已开盖、瓶盖根本不入画；食指指腹按在喷头（不是瓶身）上按压一次；雾与按压在同一镜内构成因果；写 `fades within the shot` 给出明确终点。**绝不在同一镜里完成开盖**。
15. **喷香水镜瓶盖不入画**：统一写 `no cap visible in frame`，禁止一边写瓶盖反光一边在 `[NEG]` 禁盖（自相矛盾会让模型随机取舍，实测瓶盖留在瓶上）。
16. **喷雾落点按叙事自选，不锁死**（技能铁律 27 的三种合法写法，都可用、可混用，选型由你按情绪与叙事定）：A 近距落肤（默认）——颈侧/耳后或手腕，喷头距皮肤几厘米，`only a few centimetres long: it reaches the skin and disperses there`，落点只留 `a faint even sheen`；B 喷空中＋人物走入汽雾——`she turns on the spot then walks a few steps forward into the vapor`，须中景约 50mm、主体背后有垂直发光灯板＋硬逆光 rake 穿过汽雾，汽雾写 `thin translucent vapor, catches a faint golden sheen, never obscures the face`（建议排 4-5 秒的镜）；C 其他自然写法也允许，只要因果闭环（第 7 条）且落点收敛于极淡光泽。**唯一硬要求**：同一片里有两镜以上喷雾时，落点至少变化一次，不要每镜都喷同一位置。"""

# 非香水品类替代（不硬造喷雾动作，把品类核心使用动作当作核心镜）
NON_PERFUME_RULES = """14. **本项目商品不是香水类**：不要硬造喷雾 / 开盖动作；把该品类的**核心使用动作**（涂抹 / 按压泵头 / 倒出 / 展开 / 佩戴……以卖点描述为准）当作全片必须有至少一镜的"核心动作镜"，同样遵守第 2 条（该动作的前置状态不许在同一镜内改变，如盖子/盒盖不许在镜内被打开）。"""

_PERFUME_TEXT_RE = re.compile(
    r"perfume|fragrance|parfum|香水|香氛|\beau\s+de\b|\bed[pt]\b", re.I)


def is_perfume_products(products: Any) -> bool:
    """这批商品里有没有香水/香氛类。

    双判据：`category == "perfume"`（R-17 新增品类，判得准）或
    名称 / 卖点描述命中香水词表（兜底老数据 —— 存量香水商品品类都填的 beauty）。
    """
    for pr in products or []:
        if not isinstance(pr, dict):
            continue
        if (pr.get("category") or "").strip().lower() == "perfume":
            return True
        blob = f"{pr.get('name') or ''} {pr.get('description') or ''}"
        if _PERFUME_TEXT_RE.search(blob):
            return True
    return False


def is_perfume_facts(facts: Dict[str, Any]) -> bool:
    return is_perfume_products((facts or {}).get("products"))


def rules_for(perfume: bool) -> str:
    """按品类拼装铁律正文：通用 + （香水专属 ｜ 非香水替代）。"""
    return CONSISTENCY_RULES + "\n" + (PERFUME_RULES if perfume else NON_PERFUME_RULES)


def talent_anchor_text(talent: Dict[str, Any]) -> str:
    """`appearance` 为空但有主播时，用**必填资料**拼一个事实性人物锚点。

    为什么不让模型自己设计（R-16）：实测编出了 `Brazilian woman, early 30s,
    olive skin…`，与主播真实参考图对不上，一致性必崩。年龄 / 性别 / 国籍是
    选角三硬维度（接口层强校验必填），spec 里的造型维度是英文短语 ——
    这些足够拼出一句"事实性锚点"，一个字都不用编。
    """
    gender = (talent.get("gender") or "").strip().lower()
    age = talent.get("age")
    # 身份三硬维度一个都没有 → 视同"未关联主播"，返回空串让调用方走
    # 「未关联主播」分支；否则会把空资料拼成一句无意义的 "person" 锚点
    if not any([
        isinstance(age, int) and age > 0,
        gender,
        (talent.get("nationality") or "").strip(),
    ]):
        return ""
    who = _GENDER_EN.get(gender, "person")
    parts: List[str] = []
    if isinstance(age, int) and age > 0:
        parts.append(f"{age}-year-old {who}")
    else:
        parts.append(who)
    nat = (talent.get("nationality") or "").strip()
    if nat:
        nat_en = _NATION_EN.get(nat)
        # 映射表没收录的中文词不硬塞进英文锚点
        if nat_en:
            parts.append(nat_en)
        elif not re.search(r"[\u4e00-\u9fff]", nat):
            parts.append(nat)
    desc = (talent.get("description") or "").strip()
    # 一句话人物设定可能含中文，只收纯西文片段
    if desc and not re.search(r"[\u4e00-\u9fff]", desc):
        parts.append(desc)
    spec = talent.get("spec") or {}
    for key in ("outfit", "makeup", "hair_detail", "hair_color"):
        val = (spec.get(key) or "").strip()
        if val and not re.search(r"[\u4e00-\u9fff]", val):
            parts.append(val)
    parts = list(dict.fromkeys(p for p in parts if p))
    return ", ".join(parts) if parts else ""


_NATION_EN = {
    "东亚": "East Asian", "东南亚": "Southeast Asian", "欧美": "Western",
    "南亚": "South Asian", "拉美": "Latin American", "中东": "Middle Eastern",
    "非洲": "African", "中亚": "Central Asian",
}
_GENDER_EN = {"female": "woman", "male": "man", "neutral": "person"}


# 复刻的保留方式四档 —— **直接用 H3 官方 `retention_analysis` 的标记词**
# （技能铁律 34 /`references/h3-official-format.md` 第五节），不用自创的两分法：
# 粒度更细，且与模型对参考标签的语义对齐。
#
# ★R-33 实测修正：原口径只写"镜头序列/时间轴/运镜/光影原样复用"，于是模型
#   把 5 镜全标成 `fully_preserved` —— 而实际 A1 丢了玫瑰与咖啡豆、A3 丢了白色
#   花束与深色葡萄、景别也变了（特写→中景）。**标全绿就等于放弃了分级约束**，
#   后面没有任何一关会拦。所以把 `fully_preserved` 的门槛写死到"连道具、构图、
#   景别都不许变"，让降级成为模型必须做的一个判断。
RETENTION_SPEC = (
    "`fully_preserved` 完整保留——**画面元素、构图、景别、道具一个都不许少**，"
    "只在主体身份上换人换品（镜头序列/时间轴/运镜/光影/可见道具原样复用）"
    "｜`partially_preserved` 部分保留——手法（运镜/光影/节奏）保留，"
    "但道具、场景陈设、构图或景别按本片情况调整了"
    "｜`attribute_transfer` 特征迁移（原片主体的特征迁移到新主体：换人换品）"
    "｜`weak_reference` 只保留宽泛相似（仅氛围调性相近，画面不照搬）"
)
# 判据：只要动了景别/道具/构图，就只能标 partially_preserved —— 上面那句
# 会被 `align_retention()` 用真实景别做一次机器校验（标全绿但景别变了 = 自动降级）。


def remake_timeline(facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    """复刻时间轴：把反推报告里**实测的剪辑点**取出来，作为本片的硬约束。

    ★R-33 定责（复刻失败的根因一）：上一版只把报告摊成一段文字、写一句
    "镜头数量与顺序、每镜时长节奏原样沿用"，而骨架 prompt 同时收到了**本次请求
    显式传的 `shots=5 / dur=15`**。两个真相源打架时模型服从了请求 —— 报告是
    6 镜（3/3/2/1/4/2），成片压成了 5 镜（3/3/2/3/4），**喷雾高潮从 5s 砍到 3s、
    收尾从 2s 拉到 4s**，整片节奏与原片就差在这里。

    所以这里把它变成可执行的东西：镜数与逐镜时间码由报告给定，代码在骨架产出
    后**强制写回**（`apply_timeline`），不再只是提示词里的一句请求。
    """
    rm = (facts or {}).get("remake") or {}
    out: List[Dict[str, Any]] = []
    for s in rm.get("shots") or []:
        if not isinstance(s, dict):
            continue
        try:
            start = float(s.get("start"))
            end = float(s.get("end"))
        except (TypeError, ValueError):
            continue
        if end - start < 0.5:          # 不成镜的残段（模型偶尔给 0s 段）不算剪辑点
            continue
        out.append({
            "idx": len(out) + 1,
            "start": round(start, 3),
            "end": round(end, 3),
            "dur": round(end - start, 3),
            "shot_size": str(s.get("shot_size") or "").strip(),
            "retention": str(s.get("retention") or "").strip(),
        })
    return out


def remake_block_text(facts: Dict[str, Any]) -> str:
    """爆款复刻基准块（R-32，技能工作流 C）。

    `facts["remake"]` 由 `api/trae.generate_script` 从一份反推报告注入。它把
    「拆解 → 归因 → 反推 → 分类 → 替换 → 重组」六步里**前四步的产物**带进骨架
    环节，让本片骨架直接在原片的结构上长出来，而不是另起一套分镜。

    与 `_reverse_text`（只带三档结论）的区别：这里带的是**逐镜序列 + 每镜的
    有效性归因 + 保留方式 + 原片提示词**，是"复刻"这件事真正需要的输入。

    ★R-33：额外给一张**硬时间轴表**（实测剪辑点 → 本片逐镜时间码），把"沿用节奏"
    从一句请求升级成可被代码校验的约束（校验在 `apply_timeline`）。
    """
    rm = (facts or {}).get("remake") or {}
    shots = rm.get("shots") or []
    if not shots:
        return ""
    lines = [
        "",
        "## 复刻基准：参考片拆解（★**画面级复刻** —— 只换主体，其余一个元素都不动）",
        f"- 参考片：{rm.get('source') or '-'}（反推报告 #{rm.get('report_id')}，"
        f"{rm.get('duration') or '-'}s，{rm.get('vision') and '看图反推' or '数据反推'}）",
        "- **只替换两样东西：① 人物（换成本项目主播）② 产品（换成本项目商品）。**",
        "- 除此之外的一切**逐条沿用原片**：场景与空间、背景与材质、陈设与道具（种类、"
        "数量、位置、材质）、构图与景别、运镜、光影结构、时间轴、转场、音画关系。"
        "**原片画面里出现过的东西，一个都不许少、也不许换成别的**；原片没有的道具，"
        "一个也不许加。",
        "- 下面每一镜的「画面」描写就是该镜的**沿用清单**：它是逐字读原片得来的事实，"
        "必须逐条出现在本镜提示词的 `[SHOT]` / `[HERO]` / `[TEXTURE]` 里。",
    ]
    for s in shots:
        try:
            idx = int(s.get("idx") or 0)
        except (TypeError, ValueError):
            idx = 0
        head = (f"  #{idx} {s.get('start', '-')}~{s.get('end', '-')}s ｜ "
                f"景别 {s.get('shot_size') or '-'} ｜ 运镜 {s.get('camera') or '-'} ｜ "
                f"光 {s.get('light') or '-'}")
        desc = (s.get("desc") or "").strip()
        if desc:
            head += f" ｜ 画面（沿用清单）：{desc}"
        if s.get("retention"):
            head += f" ｜ 保留方式：{s['retention']}（四档标记见下）"
        if s.get("why_effective"):
            head += f" ｜ 有效性：{s['why_effective']}"
        lines.append("  - " + head.lstrip())
        elems = [str(e).strip() for e in (s.get("result_elements") or []) if str(e).strip()]
        if elems:
            lines.append("    原片该镜的**结果性元素**（画面里必须能看到，逐条落实）："
                         + "；".join(elems))
        causes = [str(c).strip() for c in (s.get("causes") or []) if str(c).strip()]
        if causes:
            lines.append("    成因（结果性元素在画面内的来源，`[PHYSICS]` 照此写）："
                         + "；".join(causes))
        if s.get("prompt"):
            lines.append(
                "    原片该镜的反推提示词（**沿用它的画面写法**：场景、陈设、道具、"
                "构图、景别、运镜、光影、物理关系照它写，只把主体换掉）："
                f"{s['prompt']}")

    tl = remake_timeline(facts)
    if tl:
        total = tl[-1]["end"]
        lines += [
            "",
            f"### 复刻时间轴（**硬约束，不是建议**）—— 本片必须恰好 {len(tl)} 镜、"
            f"总长 {total:g} 秒",
            "下面每一行就是本片的一个镜头槽位：**镜数、顺序、逐镜 start_sec / end_sec "
            "必须逐字照抄**（那些时间点是实测的真实剪辑点，动一个整片节奏就废）。",
            "- **不许合并镜头**（把两个剪辑点之间的两段并成一镜 = 丢掉一个节拍）；",
            "- **不许拆分、不许新增、不许删除**任何一镜；",
            "- **不许改时间码**（哪怕你觉得某镜太短 / 太长，原片的节奏就是它的有效性来源）；",
            "- 输出的 shots 数组长度若与下表不等，本次产出作废重来。",
            "",
            "| 本片镜号 | start_sec | end_sec | 时长 | 对应原片镜 | 原片景别 | 原片保留方式 |",
            "|---|---|---|---|---|---|---|",
        ]
        for j, t in enumerate(tl, start=1):
            lines.append(
                f"| A{j} | {t['start']:g} | {t['end']:g} | {t['dur']:g}s "
                f"| #{t['idx']} | {t['shot_size'] or '-'} | {t['retention'] or '-'} |")
        lines.append("")
    if rm.get("findings_text"):
        lines += [
            "### 反推三档判定（可直接采纳 / 须改造 / 不可采纳）",
            str(rm["findings_text"]).strip(),
        ]
    lines += [
        "### 复刻纪律（逐条照做）",
        f"1. **每镜必须带 `retention` 四档标记**：{RETENTION_SPEC}；"
        "`source_shot` 写它对应原片第几镜，`source_prompt` 原样抄原片该镜的提示词"
        "（没有就留空）；**画面级复刻默认就是 `fully_preserved`** —— 本片景别由时间轴"
        "强制沿用原片，可见道具一个都不许少，**照实标 `fully_preserved` 即可**"
        "（只有本片主动改过某镜的道具/陈设/景别时才降档）；",
        "2. **只换主体**：原片的**人物**换成本项目主播（锚点见上文）、**产品**换成"
        "本项目的商品。**除这两样以外，一个字、一件道具、一块背景都不许动** —— "
        "场景与空间、背景与材质、台面与陈设、道具的种类/数量/位置/材质、构图、景别、"
        "光影结构一律**照原片写**。原片画面里出现过的东西一个都不许少（**丢了道具 = "
        "这一镜复刻失败**），原片没有的道具一个也不许加；",
        "3. **镜头语言必须留住**：镜头序列、逐镜时长、运镜、光影结构、转场、音画关系"
        "原样复用到本片；",
        "4. **每镜必须给出 `inherit_checklist`**：把上面该镜「沿用清单」里的画面元素"
        "逐条拆成英文短语（例如 `cut lemon halves`、`scattered coffee beans`、"
        "`white rose blooms`、`dark marble counter`），**每一条都必须原样出现在本镜"
        "提示词的 `[SHOT]` / `[HERO]` / `[TEXTURE]` 里**。代码会拿这份清单逐条回查"
        "提示词，查不到就报警；",
        "5. **本镜服装也沿用原片**：`talent_look` 写**原片该镜的服装与配饰**"
        "（从该镜的「画面」描写与原片提示词 `[REF]` 里读出来，英文短语，如 "
        "`black off-shoulder satin dress`；**不要写发型** —— 发型属于模特身份、随主播锚点走，"
        "写进来会与锚点自带的发型并存在同一句里，模型只会随机取舍）。"
        "主播锚点里自带的着装会被这一项**替换掉**"
        "—— 一律只允许出现**一件**衣服，绝不许把锚点的衣服与本镜造型并排写在一起"
        "（两件衣服会让模型合成出一件不存在的混血裙）；",
        "6. **不可采纳项不要照抄**（与铁律冲突或模型做不到的：精细手部多关节、"
        "画内文字、镜头内变速等），改写进 summary_zh 说明为什么换写法；",
        "7. 念白**不得照抄原片**：按本项目卖点重写，但要**对齐原片的念白节奏与"
        "字数密度**（原片念白停顿在哪、每镜几个词，本片保持同样的疏密）。",
    ]
    return "\n".join(lines) + "\n"



def build_skeleton_messages(
    facts: Dict[str, Any],
    *,
    lang: str,
    shots: int,
    dur: float,
    instruction: str = "",
    fixed_timeline: Optional[List[Dict[str, Any]]] = None,
    corrective: str = "",
) -> Tuple[str, str]:
    """骨架阶段：定全片结构 + 统一锚点。**只输出 JSON**，便于代码解析与并发复用。

    `fixed_timeline`（★R-33 复刻模式）：报告的实测剪辑点 → 本片逐镜槽位。给了它
    就必须**恰好**产出等长的 shots（代码在 `apply_timeline` 里复核）。
    `corrective`：上一次骨架不合格时的纠正说明，拼进用户消息尾部重试一次。
    """
    prod_lines: List[str] = []
    for pr in facts["products"]:
        block = f"- [{pr['code']}] {pr['name']}（品类 {pr['category']}，角色 {pr['role'] or '主推'}）"
        if pr["brand"]:
            block += f"\n  品牌：{pr['brand']}"
        if pr["price"]:
            block += f"\n  价格：{pr['price']}"
        if pr["target_audience"]:
            block += f"\n  目标人群：{pr['target_audience']}"
        if pr["description"]:
            block += f"\n  卖点描述（**事实来源，必须据此写，不得编造香调/功效/成分**）：{pr['description']}"
        else:
            block += "\n  （未填卖点描述：只基于名称/品类做合理视觉化，不要编造香调事实）"
        prod_lines.append(block)
    products_text = "\n".join(prod_lines) if prod_lines else "（本项目未关联商品）"

    # R-20：商品图解读（视觉识别，含缓存）整块注入。此前铁律是「脚本生成阶段
    # 完全不喂图」——现在仍然**不喂图文件**（不花 vision token 逐镜看图），
    # 但把一次性的识别结论（每张图的中文画面描述）以文字注入，让外观/包装
    # 细节能进锚点与画面设计。事实纪律（只认图中可见、禁推断香调）由
    # visual_context_text 自带，这里不重复。
    visual_text = str((facts or {}).get("_visual_text") or "").strip()
    visual_block = f"\n{visual_text}\n" if visual_text else ""

    # R-28：最新反推报告三档结论整块注入 —— 拆完参考片即用，不必等人工蒸馏。
    reverse_text = str((facts or {}).get("_reverse_text") or "").strip()
    reverse_block = (
        f"\n## 参考片反推结论（最新一份真实参考片的拆解判定 —— 采纳项直接体"
        f"现进叙事弧与分镜；改造项按其中写法要求执行；不可采纳项禁止照抄）\n"
        f"{reverse_text}\n" if reverse_text else ""
    )

    # R-32：爆款复刻基准（原片逐镜序列 + 归因 + 保留方式 + 原片提示词）。
    # 有它时本片不是"另起一套分镜"，而是在原片结构上换人换品。
    remake_blk = remake_block_text(facts)
    remake_on = bool(remake_blk)
    # 复刻模式下骨架每镜要多带三个字段：保留方式 + 对应原片镜号 + 原片该镜提示词。
    # 原片提示词要逐字带下来，逐镜展开阶段才有的可比对（见 build_shot_messages）。
    remake_fields = (
        ",\n      \"retention\": \"<四档标记之一，复刻项目必填："
        + RETENTION_SPEC.replace("｜", " / ").replace("`", "")
        + ">\",\n      \"source_shot\": \"<int：对应原片第几镜（复刻项目必填，"
          "非复刻填 0）>\",\n      \"source_prompt\": \"<英文：原片该镜的反推提示词，"
          "逐字抄上面复刻基准里的那一条；没有就空字符串>\",\n"
          "      \"inherit_checklist\": [\"<英文短语：本镜**必须沿用原片**的画面元素，"
          "逐条拆自上面该镜的「画面（沿用清单）」/ 结果性元素 / 原片提示词 —— "
          "道具、陈设、材质、背景、台面都要列。示例：cut lemon halves / scattered "
          "coffee beans / white rose blooms / dark marble counter。每一条都会被代码"
          "回查是否出现在本镜提示词里>\"],\n"
          "      \"talent_look\": \"<英文短语：**原片该镜的服装与配饰**（画面级复刻："
          "服装属原片内容，必须沿用；从该镜的「画面」描写与原片提示词里读出来，如 "
          "black off-shoulder satin dress。**不要写发型** —— 发型随主播锚点，"
          "写两处会自相矛盾）。原片该镜没有可辨服装就留空字符串。"
          "本项会**替换**主播锚点里自带的着装，绝不许两件衣服并存>\""
    ) if remake_on else ""

    # ---- 复刻时间轴：硬约束槽位（★R-33）----
    tl = list(fixed_timeline or [])
    if not tl:
        tl = remake_timeline(facts)
    slot_block = ""
    count_rule = ""
    if tl:
        slots = "\n".join(
            f"- 第 {j} 镜：code `A{j}`，start_sec `{t['start']:g}`，"
            f"end_sec `{t['end']:g}`（{t['dur']:g}s）"
            + (f"，原片景别 `{t['shot_size']}`" if t.get("shot_size") else "")
            + (f"，原片保留方式 `{t['retention']}`" if t.get("retention") else "")
            for j, t in enumerate(tl, start=1)
        )
        slot_block = (
            f"\n## 本片槽位（**硬约束**，与「复刻时间轴」表一一对应）\n"
            f"{slots}\n"
            f"- shots 数组**必须恰好 {len(tl)} 个元素**，第 i 个元素的 "
            f"code / start_sec / end_sec **逐字照抄上表**，顺序不许调换。\n"
            f"- 多一个、少一个、或改了任一时间码 = 本次产出不合格（会被打回重做）。\n"
        )
        count_rule = (
            f"- 目标镜数与逐镜时间码：**由「复刻时间轴」硬性给定（{len(tl)} 镜、"
            f"总长 {tl[-1]['end']:g} 秒），一个字都不许改**"
        )
    else:
        count_rule = f"- 目标时长：{dur:.0f} 秒 ｜ 目标镜数：{shots} 镜"

    # 逐镜商品分配（★R-33）：多商品项目里"每镜都把两瓶写进去"是复刻失败
    # 根因之二 —— 模型只能靠文本自己消歧，而且产品主导镜的首帧参考图也会挂错。
    # 所以把"这一镜用哪个商品"变成骨架必须给出的显式字段。
    multi_prod = len(facts["products"]) > 1
    prod_rule = (
        "\n## 逐镜商品分配（**多商品项目的硬要求**）\n"
        "- 每个镜头必须用 `products` 字段写明**本镜画面里出现的是哪几个商品**"
        "（填 code，如 `[\"P01\"]`；两个商品同框才写两个）。\n"
        "- **单商品镜只写一个 code**：本镜没出现的商品，一个字都不许提、也不许画进画面"
        "—— 实测「每镜都把两瓶写进去」会让模型把另一瓶硬塞进画面，"
        "而首帧参考图也只能靠猜。\n"
        "- anchors 里每个商品各给一段 `product_refs[code]` 参考职责；"
        "逐镜只复用本镜商品那一段。\n"
        "- 全片应当让两个商品各自主导至少一镜（若叙事允许），不要所有镜都糊在一起。\n"
        if multi_prod else
        "\n## 逐镜商品分配\n"
        "- 本项目只有一个商品，每镜 `products` 都填 `[\"" + (facts["products"][0]["code"] if facts["products"] else "P01") + "\"]`。\n"
    )

    talent = facts["talent"]
    if talent["appearance"]:
        talent_text = (
            "主播形象（**全片统一，必须逐字复用**；R2V 语义下它是人物参考图的"
            "职责素材——外观由参考图承载，提示词不重复描述）：\n" + talent["appearance"]
        )
    else:
        derived = talent_anchor_text(talent)
        if derived:
            talent_text = (
                "主播形象（**全片统一，必须逐字复用**；下面这句由主播资料"
                "自动生成 —— 只可照用，**禁止自行修改外貌 / 族裔 / 年龄**，"
                "否则会与主播参考图对不上）：\n" + derived
            )
        else:
            talent_text = (
                "（未关联主播：请自行设计一位具体人物，年龄/性别/国籍明确，"
                "并给出可跨镜逐字复用的人物参考职责）"
            )

    system = (
        "你是电商带货短视频的导演，负责设计 15 秒带货片的整体结构。\n"
        "**只输出一个 JSON 对象**，不要任何解释文字，不要 markdown 代码块包裹。"
    )
    perfume = is_perfume_facts(facts)
    # anchors 里的产品参考职责：多商品项目要**逐商品各写一段**（按 code 索引），
    # 逐镜只复用本镜那几个商品的那几段 —— 这是"每镜两瓶都写上"的根治办法。
    _prod_ref_tpl = (
        "    \"<商品 code>\": \"<英文，≤25 词：该商品的参考图**职责说明**"
        "（该参考只负责定义它的形态 / 颜色 / 材质，主体随品类：瓶 / 管 / 罐 / 盒……"
        "只写商品事实可证实的细节，**不编造商品图里没有的部件**）>\"")
    if multi_prod:
        codes_lines = ",\n".join(
            _prod_ref_tpl.replace("<商品 code>", pr["code"]) for pr in facts["products"])
        anchors_prod = (
            "    \"talent_ref\": \"<英文，≤25 词：人物参考图的**职责说明**（该参考只负责定义主播的\"\n"
            "                  \"长相 / 发型 / 服装气质；不写场景，不重复外观细节——H3 R2V 规范，\"\n"
            "                  \"外观由参考图承载）>\",\n"
            "    \"product_refs\": {\n"
            f"{codes_lines}\n"
            "    },"
        )
    else:
        anchors_prod = (
            "    \"talent_ref\": \"<英文，≤25 词：人物参考图的**职责说明**（该参考只负责定义主播的\"\n"
            "                  \"长相 / 发型 / 服装气质；不写场景，不重复外观细节——H3 R2V 规范，\"\n"
            "                  \"外观由参考图承载）>\",\n"
            "    \"product_ref\": \"<英文，≤25 词：产品参考图的**职责说明**（该参考只负责定义主体\"\n"
            "                   \"形态 / 颜色 / 材质，主体随品类：瓶 / 管 / 罐 / 盒……\"\n"
            "                   \"只写商品事实可证实的细节，**不编造商品图里没有的部件**）>\","
        )
    # 造型字段：复刻模式下由 `remake_fields` 单独定义（必须是**原片该镜**的服装，
    # 而不是自由设计），这里不能重复输出同一个 key —— 同一份 JSON 里出现两次
    # `talent_look` 会让模型只认后一个、把前一个的要求丢掉。
    look_rule = "" if remake_on else (
        "      \"talent_look\": \"<英文短语：**本镜的服装与发型**（章节级差异化）。\"\n"
        "                      \"全片统一造型就留空字符串；但当全片存在昼夜 / 情绪 / 场景的\"\n"
        "                      \"章节切换时，**至少有一镜必须改写服装或发型**让章节可辨\"\n"
        "                      \"（面部 / 体型 / 族裔锚点不变，只换造型）>\",\n"
    )
    user = f"""为下面这个项目设计一支 {dur:.0f} 秒、{shots} 镜的带货视频骨架。

## 项目事实（只据此写，不要编造未给出的功效 / 成分 / 香调）
- 项目名称：{facts['project_name']}
- 投放语言（决定念白语言与口吻）：{lang}
{count_rule}
- 品类判定：{'香水/香氛类 —— 喷香水镜、开盖状态、瓶盖不入画等专属铁律全部适用' if perfume else '非香水类 —— 不要硬造喷雾/开盖动作，按品类核心使用动作设计核心镜'}

### 商品
{products_text}
{visual_block}
### 主播
{talent_text}
{remake_blk}{slot_block}{prod_rule}
## 叙事弧（先讲故事，再排镜头 —— 镜头为叙事服务，不是分镜的堆砌）
- **concept 里必须先定一条情绪弧**：观众从什么感受开始、经过什么、以什么感受收尾
  （范例：好奇 → 亲近 → 使用 → 沉浸 → 自信绽放）。{shots} 镜各落在弧线的一个点上，
  情绪**只进不退**，念白与画面共同推进这条弧
- **卖点可视化（最重要）**：念白提到的每个卖点 / 香调 / 功效，必须在本镜画面里有
  **可见的对应物**——道具（柠檬片 / 杏仁 / 花瓣……只从商品事实与商品图解读里的
  可见元素取）、使用动作（喷 / 抹 / 闻）、或光对材质的反应。**不许"嘴上说柠檬、
  画面里没有柠檬"**；画面里也不许出现念白没提的孤立道具抢戏
- **每镜必须有事件**：主动作写"谁对什么做了什么"，禁止"X remains still / nothing
  happens"式的空镜连排；全片**最多 1 镜**纯产品静物（放在钩子），人物可读的情绪
  表达（闭眼陶醉 / 微笑 / 回眸 / 仰头……）至少出现在 **2 镜**里
- **相邻镜动作衔接**：上一镜的 exit_state 与下一镜的 entry_state 必须是**同一个
  姿态 / 位置**（剪辑连续性），像一条连续的动作弧被切开，而不是各自摆拍
{reverse_block}
## 节奏结构（15 秒片：钩子 0-3s / 主体 3-11s / 收尾 11-15s）
- 钩子：第一帧就要有信息量，不要从黑场开始
- 主体：每 2-3 秒必须有一次画面变化
- 收尾：产品完整清晰定格，留出字幕安全区

## 一致性铁律（设计每一镜时必须同时满足）
{rules_for(perfume)}

## 念白规则
- 语音占比 **55-65%**：{dur:.0f} 秒片里语音约 {dur * 0.6:.1f} 秒，其余留给关键动作静音（≥0.5s）与句间留白
- 每句窗口比语音至少多 0.5s，否则后期会被全局加速（听感"发赶"）
- 先扣静音与留白，再按语速反推词数。**不要先写字数再挤时间**
- 念白**逐字用 {lang} 写**：成片念白里**一个汉字都不许出现**（实测混入中文
  导致成片配音崩坏）；也不要把商品图里的道具 / 香调卡文字直接翻译堆进去 ——
  念白只说卖点与情绪，与画面的卖点可视化呼应

## 输出 JSON（严格按此结构，字段名与类型都不要改）
{{
  "concept": "<中文一句话：创意概念 / 钩子机制>",
  "anchors": {{
{anchors_prod}
    "light_arc": "<英文，全片光线与色温走向，可执行参数>",
    "style": "<英文，可被摄影师执行的风格参数，不要情绪词>"
  }},
  "shots": [
    {{
      "code": "A1",
      "start_sec": 0.0,
      "end_sec": 3.0,
      "beat": "钩子",
      "products": ["<本镜出现的商品 code；单商品镜只写一个，两个同框才写两个>"],
{look_rule}      "summary_zh": "<中文一句话，给人类看画面内容>",
      "emotion": "<中文短语：本镜落在情绪弧的哪个点，如「好奇」「沉浸」>",
      "visual_proof": "<英文短语：本镜画面里与念白卖点/香调对应的**可见元素**，"
                      "如 lemon slices and almonds beside the bottle；本镜念白没有"
                      "具体卖点就写 none>",
      "action": "<英文，单一动作，≤15 词>",
      "shot_size": "<如 Close-up / Medium shot / Extreme close-up>",
      "camera": "<英文，量化运镜 + 明确不做什么>",
      "light": "<英文，光位 + 色温参数>",
      "entry_state": "<英文，本镜第一帧的状态>",
      "exit_state": "<英文，本镜最后一帧的状态>",
      "voiceover": "<{lang} 念白，落本镜窗口内>",
      "sfx": "<音效挂点，必须写成 `英文关键词 @绝对秒数`，如 `mist @3.0s`；"
             "关键词只能从 mist / click / glass / breath 里选 —— "
             "喷头/喷雾声一律写 mist（纯水汽）；**禁止写 spray**"
             "（含阀门机械冲击，听感是「啪嗒」，已被用户否决），"
             "秒数是全片时间轴上的绝对秒（不是本镜内的相对秒）；该镜无音效就写空字符串>"
             "{remake_fields}"
    }}
  ]
}}

{('- 用户附加要求（必须满足）：' + instruction) if instruction else ''}
{corrective}"""
    return system, user


# ------------------------------------------------------- 逐镜商品 / 造型 / 时间轴


def shot_products(shot: Dict[str, Any], facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    """这一镜用到的商品（按骨架的 `products` 字段筛）。

    字段缺失 / 全是无效 code 时回退"全部商品"—— 与 R-33 之前的行为一致，
    不会因为模型漏写一个字段就把整镜的商品事实抽空。
    """
    codes = {str(c).strip().upper() for c in (shot.get("products") or [])
             if str(c or "").strip()}
    prods = [p for p in (facts.get("products") or []) if isinstance(p, dict)]
    if not codes:
        return prods
    picked = [p for p in prods if str(p.get("code") or "").upper() in codes]
    return picked or prods


def product_ref_for_shot(anchors: Dict[str, Any], shot: Dict[str, Any],
                         facts: Dict[str, Any]) -> str:
    """本镜 `[PRODUCT]` 该写的那一行。

    多商品项目：只拼接**本镜商品**各自那一段职责说明（`anchors.product_refs`），
    没出现的商品一个字都不进这一行 —— 这是"每镜都把两瓶写进去"的根治办法。
    """
    refs = anchors.get("product_refs")
    if isinstance(refs, dict) and refs:
        parts = []
        for p in shot_products(shot, facts):
            txt = str(refs.get(p.get("code")) or "").strip()
            if txt:
                parts.append(f"[{p.get('code')}] {txt}")
        if parts:
            return " ｜ ".join(parts)
    return str(anchors.get("product_ref") or "").strip()


# ---------------------------------------------------------------- 本镜 [REF] 口径
# 画面里是否出现人物 —— 决定这一镜的 `[REF]` 该写"主播身份块"还是"本镜无人物"。
# 词表与 `refs._PERSON_HINTS` **同一套**（窄表：人称 + face）：`refs.shot_has_person`
# 用它对**最终提示词**判"要不要挂主播图"，这里用它对**骨架构想**判同一件事。
# 两处必须是同一个口径，否则会出现"提示词写着没人、参考图却把主播挂上去"。
# 只用「窄表」不用「宽表（含 hand/wrist/finger）」的原因：骨架里 `glass body`
# 这类写法会撞上宽表的 body，实测把纯产品静物镜误判成人物镜。
_SKELETON_SCENE_KEYS = ("action", "summary_zh", "entry_state", "exit_state", "visual_proof")
_PERSON_WORDS = re.compile(
    r"\b(woman|women|man|men|girl|boy|female|male|person|model|she|her|hers|he|him|his|face)\b",
    re.I,
)

# 纯产品镜的 `[REF]`：**显式声明本镜没有人物**，而不是把主播身份块塞进去。
# 实测（R-33 复刻复核）：A1 静物镜 / A3 产品氛围镜都是纯产品镜，`[NEG]` 明写
# no people，而 `[REF]` 却逐字注入 "the woman's face shape, porcelain skin tone…"
# —— 一边告诉模型"有个女人"、一边禁止人出现，正是 R-16 那类穿帮的温床。
_NO_PERSON_REF = ("this shot has no person: the frame is the product and its setting only "
                  "— no person, no face, no second figure")


def skeleton_shot_has_person(shot: Dict[str, Any]) -> bool:
    """骨架构想里这一镜是否出现人物（只看描述画面的字段，锚点不参与）。"""
    text = " ".join(str(shot.get(k) or "") for k in _SKELETON_SCENE_KEYS)
    return bool(_PERSON_WORDS.search(text))


def ref_line_for_shot(anchors: Dict[str, Any], shot: Dict[str, Any]) -> str:
    """本镜 `[REF]` 的**唯一口径**：人物镜给身份锚点（＋本镜造型），纯产品镜给「本镜无人物」。

    `build_shot_messages`（写给模型看的要求）与 `enforce_anchors`（代码强制覆盖）
    必须取同一个值 —— 否则模型被要求写 A、代码却强制成 B，白记一条 warning。
    """
    if not skeleton_shot_has_person(shot):
        return _NO_PERSON_REF
    return talent_ref_for_shot(anchors, shot)


# `talent_look` 里的发型片段（★R-38d）：发型属于**模特身份**（由锚点 `appearance`
# 与形象图定义），造型层不允许再写一份 —— 两处并存就是自相矛盾（见 talent_ref_for_shot）。
_HAIR_KW_RE = re.compile(
    r"\b(hair|buns?|waves?|curls?|braids?|ponytail|updos?|chignon|bob)\b", re.I)


def strip_hair_from_look(look: str) -> str:
    """从本镜造型里剔除发型片段（按逗号切分，含发型词的片段整段去掉）。

    只去发型，不动服装与配饰 —— `black off-shoulder satin dress, long dark brown
    wavy hair` → `black off-shoulder satin dress`。
    """
    parts = [p.strip() for p in str(look or "").split(",")]
    return ", ".join([p for p in parts if p and not _HAIR_KW_RE.search(p)])


def talent_ref_for_shot(anchors: Dict[str, Any], shot: Dict[str, Any]) -> str:
    """本镜 `[REF]`：主播**身份层** + 本镜**着装层**（着装是替换，不是追加）。

    身份维度（脸 / 肤 / 发 / 体型 / 族裔 / 妆）跨镜**逐字不变**；着装与配饰属于
    「造型」，由本镜 `talent_look` 决定。

    ★R-38 定责（混血裙的真根因）：原实现是 `f"{base} ｜ {look}"` —— **追加**。
    而 `base`（主播锚点）本身就把参考图的着装焊在里面（M02 实测：
    `…wearing a champagne satin slip dress falling just below the knee and elegant
    drop earrings…`）。于是同一句 `[REF]` 里同时出现两件衣服
    （`champagne satin slip dress ｜ black off-shoulder satin dress`），
    模型只好合成一件「细吊带＋一字肩」的混血裙（成片 A4/A5 实测）。
    现在改为：把 base 切出**着装从句**，本镜造型到位时**替掉它**，一句话只有一件衣服。
    造型缺位时回落到锚点自带的着装（不改变原行为）。

    ★R-41 定责（成片人物镜"裸胸"）：骨架给 idx=1 的 `talent_look` 只写了配饰
    （`thin gold necklace, small gold hoop earring, light pink manicured nails`），
    没有服装词 → 旧实现把这段**配饰**当成"本镜着装"直接顶掉了锚点自带的着装
    （而 M01 锚点本就没服装，于是 `[REF]` 只剩"wearing 项链"，成片直接裸胸＋项链）。
    现在：本镜 `talent_look` **只含配饰（无 `_GARMENT_KW` 命中）时**，不许顶掉锚点
    自带的着装——而是把配饰**叠加**在锚点服装之后（一句话仍只一件衣服、一个 wearing
    句式）；锚点也没有服装时，把配饰带上、身份层原样返回，**留给第八道正向闸门**
    `enforce_wear_positive` 强制注入一件基础着装兜底。
    """
    base = str(anchors.get("talent_ref") or "").strip()
    look = strip_hair_from_look(str(shot.get("talent_look") or "").strip())
    if not base:
        # 无锚点：本镜造型直接当着装层。有服装词就穿；只有配饰则先照写，
        # 由 enforce_wear_positive 兜底补衣服（不存在"顶掉锚点服装"的隐患）。
        if look:
            return (_join_wearing("", look) if _GARMENT_KW.search(look)
                    else f"wearing {look}")
        return ""
    identity, garment = split_identity_garment(base)
    # ★R-38d：`talent_look` 只负责**服装与配饰**，发型凡出现一律剔除（详见 R-38d）。
    if look:
        if _GARMENT_KW.search(look):
            # 本镜给的是真·服装 → 替换锚点着装（R-38 原行为，保持）。
            return _join_wearing(identity, look)
        # ★R-41：配饰镜（无服装词）—— 不许顶掉锚点自带着装，配饰叠加在着装之后。
        if garment:
            return _join_wearing(identity, f"{garment}, {look}")
        # 锚点也无服装：带身份层 + 配饰返回，交给 enforce_wear_positive 兜底。
        return _join_wearing(identity, look)
    if garment:
        return _join_wearing(identity, garment)
    return base


def _tidy(text: str) -> str:
    """收尾清理：空格贴标点、多余逗号、" ," 之类切分残留。"""
    t = re.sub(r"\s+([,.;])", r"\1", str(text or ""))
    t = re.sub(r"\s*,\s*,\s*", ", ", t)
    return t.strip().strip(",;")


def _join_wearing(identity: str, garment: str) -> str:
    """把着装层接回身份层（统一用 `, wearing …` 句式，避免出现两件衣服）。"""
    head = _tidy(identity).rstrip(".").strip()
    tail = _tidy(garment)
    if not head:
        return f"wearing {tail}" if tail else ""
    if not tail:
        return head
    return f"{head}, wearing {tail}"


# 主播锚点里的**着装从句**（R-38）：`appearance` 是「身份＋着装」混写的自然语言，
# 而 R2V 规范要求每张参考只负责一件事、每句只定义一件衣服。两种写法都要认：
#   ① 从句式：`, wearing a champagne satin slip dress falling just below the knee and
#      elegant drop earrings with a single stone, with full glam makeup…`
#   ② 列表式（模型把锚点压缩成逗号清单时常见）：
#      `…hair styled in sleek low bun, champagne satin slip dress, drop earrings,
#       editorial makeup with contoured cheeks and smoky lids.`
_GARMENT_CLAUSE_RE = re.compile(
    r",?\s*(?:and\s+)?wearing\s+(.+?)(?=,\s*(?:with|displaying|showing|and\s+displaying)\b|\Z)",
    re.I | re.S)
# 服装名词表（只收**明确的衣着品类**，不收 satin/velvet 这类面料词 —— 面料会撞上
# 皮肤/发色的描述，把身份层误切走进着装层）
_GARMENT_KW = re.compile(
    r"\b(dress|gown|slip|skirt|suit|blaz\w+|jacket|coat|trench|top|blouse|shirt|"
    r"tee|t-?shirt|sweater|knit|cardigan|jumpsuit|romper|bodysuit|leotard|corset|"
    r"bustier|lingerie|bralette|camisole|pants|trousers|jeans|shorts|robe|kaftan|"
    r"caftan|kimono|sari|qipao|cheongsam|loungewear)\b", re.I)
# 配饰与鞋包随服装一起走（同属"造型层"，服装换掉时配饰一起换才自洽）
_ACCESSORY_KW = re.compile(
    r"\b(earrings?|necklace|pendant|bracelet|bangle|ring|rings|anklet|heels?|"
    r"stilettos?|pumps?|shoes?|sandals?|boots?|clutch|handbag|bag|purse|belt|"
    r"scarf|hat|veil|gloves?|tiara|brooch)\b", re.I)


def split_identity_garment(text: str) -> Tuple[str, str]:
    """把主播锚点切成 `(身份层, 着装层)` —— R2V「一张参考只负责一件事」。

    识别不到着装时返回 `(原文, "")`，调用方据此回落（不做破坏性切分）。
    """
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if not t:
        return "", ""
    m = _GARMENT_CLAUSE_RE.search(t)
    if m:
        garment = _tidy(m.group(1))
        identity = _tidy(t[:m.start()] + " " + t[m.end():])
        return identity, garment
    # 列表式：把含服装/配饰词的逗号项摘出来
    items = [seg.strip() for seg in t.split(",") if seg.strip()]
    if len(items) < 2:
        return t, ""
    keep: List[str] = []
    moved: List[str] = []
    for it in items:
        if _GARMENT_KW.search(it) or _ACCESSORY_KW.search(it):
            moved.append(it)
        else:
            keep.append(it)
    if not moved:
        return t, ""
    return _tidy(", ".join(keep)), _tidy(", ".join(moved))



# 景别归一（跨镜/跨片比对用）。两处**顺序敏感**：
# ① ASCII 侧：宽的/长的先判（"extreme wide shot" 该归 ws 而不是 ecu），再判
#    特写家族 —— 且 `ecu` 必须显式匹配，`endswith("cu")` 会把 "ECU" 错判成 CU。
# ② 中文侧：反推报告的 `shot_size` 就是中文（常带后缀，如「中近景/产品主导」），
#    靠 `_SIZE_CN` 兜底；**「中近景」必须先于「近景」判**，否则被 `近景` 抢先命中。
_SIZE_CN = (
    ("大特写", "ecu"), ("极特写", "ecu"), ("中近景", "mcu"),
    ("特写", "cu"), ("近景", "cu"), ("中景", "ms"),
    ("全景", "ws"), ("远景", "ws"), ("广角", "ws"), ("全身", "fs"),
)


def normalize_shot_size(raw: Any) -> str:
    """把各种写法（Close-up / close up / CU / ECU / 中近景）压成同一个桶。

    只用于**一致性比对**（`align_retention`）；认不出就原样返回，调用方跳过比较，
    不会因为一个没见过的写法误判成"景别变了"。

    中文分支是必需的：反推报告的 `shot_size` 是「中近景/产品主导」这类中文串，
    没有它这一层复核对中文报告**整个失效**（R-33 实测发现）。
    """
    s = str(raw or "")
    t = re.sub(r"[^a-z]", "", s.lower())
    if t:
        if t == "ws" or "wide" in t:
            return "ws"
        if t == "fs" or "full" in t:
            return "fs"
        if t == "ls" or t.startswith("long"):
            return "ls"
        if t == "ecu" or "extreme" in t:
            return "ecu"
        if t == "mcu" or "mediumclose" in t:
            return "mcu"
        if t == "ms" or "medium" in t:
            return "ms"
        if t == "cu" or "close" in t:
            return "cu"
        return t
    for kw, bucket in _SIZE_CN:
        if kw in s:
            return bucket
    return ""


def apply_timeline(skeleton: Dict[str, Any],
                   timeline: List[Dict[str, Any]]) -> List[str]:
    """把参考片的实测剪辑点**强制写回骨架**（★R-33 根因一的代码闸门）。

    骨架上写着"逐镜 start_sec / end_sec 必须照抄"，但**不指望模型每次都听话**
    （并发链路三道闸门的同一条原则）——这里做确定性的对齐：
      - 镜数比时间轴多 → 截断（复刻片必须与参考片等长，多出来的节拍会把总长撑破）；
      - 镜数比时间轴少 → 保留并记警告（内容缺失无法由代码补出来，交给用户判断）；
      - 逐镜 code / start_sec / end_sec → 一律覆盖成时间轴的值；
      - 逐镜 `shot_size` → 也覆盖成原片该镜的景别（★R-38 画面级复刻）。

    ★R-38（景别闸门）：画面级复刻下"景别"属于**原片内容**，不是可自由改的手法。
    旧实现只在骨架提示词里请求"景别与原片一致"，然后靠 `align_retention` **事后
    降级标签** —— 实测 A1/A2/A4/A5 全部被推近（A1 中近景→产品特写、A4 原片到膝→
    成片到腰），标签降了级、画面还是错的。现在与时间码同一待遇：**代码强制写回**，
    景别漂移在源头就没有发生的机会。
    """
    warns: List[str] = []
    shots = list(skeleton.get("shots") or [])
    if not timeline or not shots:
        return warns
    if len(shots) > len(timeline):
        extra = [str(s.get("code") or f"#{i + 1}")
                 for i, s in enumerate(shots[len(timeline):], start=len(timeline))]
        warns.append(
            f"复刻模式：骨架多出 {len(extra)} 镜（{'、'.join(extra)}），"
            f"已按参考片的 {len(timeline)} 个剪辑点丢弃 —— 复刻片必须与参考片等长")
        shots = shots[:len(timeline)]
    elif len(shots) < len(timeline):
        missing = "、".join(f"{t['dur']:g}s" for t in timeline[len(shots):])
        warns.append(
            f"复刻模式：骨架只给了 {len(shots)} 镜，参考片有 {len(timeline)} 个剪辑点"
            f"（缺的节拍：{missing}）—— 缺少的内容代码补不出来，请人工核对或重生成")
    forced: List[str] = []
    size_forced: List[str] = []
    for i, t in enumerate(timeline[:len(shots)]):
        s = shots[i]
        old_code, old_start, old_end = s.get("code"), s.get("start_sec"), s.get("end_sec")
        s["code"] = f"A{i + 1}"
        s["start_sec"], s["end_sec"] = t["start"], t["end"]
        if old_start != t["start"] or old_end != t["end"]:
            forced.append(f"{old_code or f'A{i + 1}'}→{t['start']:g}~{t['end']:g}s")
        want_size = str(t.get("shot_size") or "").strip()
        if want_size:
            old_size = str(s.get("shot_size") or "").strip()
            s["source_shot_size"] = want_size
            s["shot_size"] = want_size
            if old_size and normalize_shot_size(old_size) != normalize_shot_size(want_size):
                size_forced.append(f"{s['code']}「{old_size}」→「{want_size}」")
    if forced:
        warns.append("复刻时间轴已强制对齐参考片剪辑点（模型写的时间码被覆盖）："
                     + "、".join(forced))
    if size_forced:
        warns.append(
            "画面级复刻：景别已强制沿用原片（模型改过的景别被覆盖）——"
            + "、".join(size_forced))
    skeleton["shots"] = shots
    return warns


def align_retention(skeleton: Dict[str, Any],
                    timeline: List[Dict[str, Any]]) -> List[str]:
    """`retention` 自评复核（★R-33 根因四）。

    上一轮 5 镜全标 `fully_preserved`，而实际景别、道具、构图都改了 ——
    标全绿就没有任何一关会拦。这里用**可机器比对的景别**做一次复核：
    标了 `fully_preserved` 却把景别改了，自动降为 `partially_preserved` 并记警告。
    道具/构图做不到机器判定，只能靠提示词纪律（RETENTION_SPEC 的新口径）。
    """
    warns: List[str] = []
    for i, s in enumerate(skeleton.get("shots") or []):
        if i >= len(timeline):
            break
        if str(s.get("retention") or "").strip() != "fully_preserved":
            continue
        want = normalize_shot_size(timeline[i].get("shot_size"))
        got = normalize_shot_size(s.get("shot_size"))
        if want and got and want != got:
            s["retention"] = "partially_preserved"
            warns.append(
                f"{s.get('code')}：标了 fully_preserved，但景别由原片 "
                f"{timeline[i].get('shot_size')} 改成了 {s.get('shot_size')} "
                f"→ 已自动降级为 partially_preserved（标 fully 就该一个元素都不动）")
    return warns


# 喷雾镜的必查项（R-33 附）：A2 的 `[NEG]` 明明写了 no cap visible in frame，
# 成片里瓶子仍带盖入画 —— 模型对单条反向约束的服从并不可靠，所以把技能铁律
# 14/15 的三条硬要求摊成一份"本镜清单"，逐条出现在逐镜 prompt 里。
_SPRAY_RE = re.compile(r"\b(nozzle|spray|mist|press(?:es|ed)?|atomis)\b", re.I)

# 锚点层（`[REF]` / `[PRODUCT]`）与反向层（`[NEG]`）—— 判"这一镜在干什么"时必须剔除。
# 锚点是**逐字引用的参考职责定义**，会被注入到每一镜里，拿它当正文用会让其中性词
# 污染判断：R-33 复刻复核实测，`[PRODUCT]` 锚点里一句 "oval black cap"（描述真实
# 瓶身）就让 `cap_in_body` 恒为真 → 自动补 `no cap visible in frame` 的兜底
# **被静默禁用**，而"成片里瓶子带着盖"正是这条兜底要防的穿帮。
_ANCHOR_LAYER_RE = re.compile(r"^\[(?:REF|PRODUCT)\].*?(?=^\[[A-Z]+\]|\Z)", re.S | re.M)
_NEG_LAYER_RE = re.compile(r"^\[NEG\].*?(?=^\[[A-Z]+\]|\Z)", re.S | re.M)
_PRODUCT_LAYER_RE = re.compile(r"^\[PRODUCT\]\s*(.+?)(?=^\[[A-Z]+\]|\Z)", re.S | re.M)


def _product_anchor_text(prompt_text: str) -> str:
    """取 `[PRODUCT]` 锚点的文字（用来判断锚点/参考图是否在描述带盖瓶身）。"""
    m = _PRODUCT_LAYER_RE.search(prompt_text)
    return m.group(1) if m else ""


def _directorial_body(prompt_text: str) -> str:
    """**导演正文** = 全文剔除 `[REF]`/`[PRODUCT]`（锚点）与 `[NEG]`（反向）。

    这三层都不是"画面在演什么"：锚点是引用、`[NEG]` 是禁止项。
    判"是不是喷雾镜"“正文有没有提瓶盖”都必须只看正文。
    """
    scene = _ANCHOR_LAYER_RE.sub("", prompt_text)
    return _NEG_LAYER_RE.sub("", scene)


def spray_checklist(shot: Dict[str, Any]) -> str:
    blob = " ".join(str(shot.get(k) or "") for k in
                    ("action", "summary_zh", "camera", "visual_proof", "voiceover"))
    if not _SPRAY_RE.search(blob):
        return ""
    return (
        "\n## 本镜是喷雾镜（逐条落实 —— 实测最易崩的一类）\n"
        "- 第一帧就已开盖、**瓶盖根本不入画**：`[NEG]` 必写 `no cap visible in frame`，"
        "且正文任何一层都不许提 cap / 瓶盖反光（自相矛盾会让模型随机取舍）；\n"
        "- 食指指腹按在**喷头**（不是瓶身）上按压一次，雾与按压在同一镜内构成因果；\n"
        "- 雾的落点按本片叙事自选（近距落肤 / 喷空中＋人物走入汽雾 / 其他因果闭环写法），"
        "同片多镜喷雾时落点至少变化一次；\n"
        "- 落点只留 `a faint even sheen`，禁写 droplets / beads / drips / streaks。\n"
    )


def build_shot_messages(
    skeleton: Dict[str, Any],
    shot: Dict[str, Any],
    *,
    prev_shot: Optional[Dict[str, Any]],
    next_shot: Optional[Dict[str, Any]],
    perfume: bool = True,
    facts: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """单镜展开阶段：一镜一路，输入里带上前后镜的状态用于衔接。

    `perfume` 控制铁律第 12 条注入香水专属还是非香水替代（R-17 品类自适应）。

    ★R-33：`[REF]` / `[PRODUCT]` 改为**按镜**取（`talent_ref_for_shot` /
    `product_ref_for_shot`）—— 造型可按章节差异化，商品只写本镜用到的那几个。
    并把本镜商品的事实（名称/卖点/画面解读）也带上，逐镜展开不必回头猜。
    """
    facts = facts or {}
    anchors = skeleton.get("anchors") or {}
    ref_line = ref_line_for_shot(anchors, shot)
    # ★R-35：纯产品镜不许再注入主播身份块（详见 `ref_line_for_shot`）。
    # 这里把要求明写给模型，让它主动写对，而不是等代码事后覆盖。
    has_person = skeleton_shot_has_person(shot)
    no_person_block = (
        "\n## 本镜**没有人物**（纯产品镜）\n"
        "- `[REF]` 就照上面给的「本镜无人物」那句原样写，"
        "**不要**把主播的脸/发型/服装/身材写进 [REF]；\n"
        "- `[NEG]` 必须含 `no person, no figure, no face`，画面里不许出现人、人影或脸"
        "（空镜里凭空冒人是最常见的穿帮）。\n"
        if not has_person else ""
    )
    # ★R-38：人物镜的着装合规提醒。定责结论是"上游把无内衬薄缎面当成了身份"，所以
    # 除了代码兜底补 [NEG]，还要在**写给模型看的要求**里说清：薄面料要同时锁"平整不透"。
    wear_block = (
        "\n## 本镜着装合规（人物镜必查 —— 用户已提过成片出现胸前凸点）\n"
        "- `[NEG]` 必须含走光类反向约束（照抄这一串）：`no visible nipples, "
        "no nipple outline showing through the fabric, no sheer or see-through "
        "fabric on the bodice, the neckline fabric stays smoothly draped and opaque`；\n"
        "- `[TEXTURE]` 提到缎面 / 雪纺 / 真丝这类薄面料时，**必须同时说明织物平整不透**"
        "（`smoothly draped, opaque satin`）—— 只写 `liquid satin sheen` 会把内衣痕迹"
        "渲染得更清楚；\n"
        "- **不要**改身材、领口形状或敞露程度（那是参考图决定的，改了就不是同一个人），"
        "只锁「面料不透、不走光」。\n"
        if has_person else ""
    )
    prod_line = product_ref_for_shot(anchors, shot, facts) or str(anchors.get("product_ref") or "")
    mine = shot_products(shot, facts)
    if mine:
        plist: List[str] = []
        for p in mine:
            line = f"- [{p.get('code')}] {p.get('name')}"
            if p.get("description"):
                line += f"\n  卖点描述（事实来源，不得编造）：{p['description']}"
            visual = str(p.get("_visual") or "").strip()
            if visual:
                line += f"\n  商品图解读（只认图中可见）：{visual}"
            plist.append(line)
        prod_block = (
            "\n## 本镜商品（**画面里只许出现这些**）\n" + "\n".join(plist) + "\n"
            "- 本镜没列出的商品，**一个字都不许提、也不许画进画面**"
            "（多商品项目里「每镜都写两瓶」会让模型把另一瓶硬塞进来）。\n"
        )
    else:
        prod_block = ""
    look = str(shot.get("talent_look") or "").strip()
    look_block = (
        f"\n## 本镜造型（沿用原片该镜的服装与配饰）\n- {look}\n"
        "- 服装与配饰**只写这一套**；主播锚点里自带的着装已被本项替换掉，"
        "**绝不许把另一件衣服也写进来**（同一句出现两件衣服会让模型合成出"
        "一件不存在的混血裙）；\n"
        "- **发型只许用主播锚点里那一套**（本镜造型不含发型，两处写法会被模型随机取舍）；\n"
        "- **面部、体型、族裔等身份锚点与全片逐字一致**，不许动。\n"
        if look else ""
    )
    spray_block = spray_checklist(shot)
    system = (
        "你是电商带货短视频的分镜提示词撰稿人，按 H3 R2V 规范（v2 模板）为指定的"
        "**一个**镜头写参考生视频英文提示词。只输出这一镜，不要写别的镜头，"
        "也不要写分镜表或念白表。"
    )

    prev_txt = (
        f"{prev_shot.get('code')} 的结束状态：{prev_shot.get('exit_state')}"
        if prev_shot else "（这是第一镜，无需承接上一镜）"
    )
    next_txt = (
        f"{next_shot.get('code')} 的起始状态：{next_shot.get('entry_state')}"
        if next_shot else "（这是最后一镜，收尾定格）"
    )

    # R-32 复刻模式：把"本镜对应原片第几镜 + 原片该镜提示词 + 保留方式"带进逐镜展开。
    # ★R-38 口径改为**画面级复刻**：沿用原片的**整个画面**（场景/陈设/道具/构图/
    # 景别/运镜/光影），只换人物与产品 —— 不再是"只借运镜/光影写法"。
    remake_txt = ""
    if shot.get("source_prompt") or shot.get("retention"):
        r = []
        if shot.get("source_shot"):
            r.append(f"- 本镜对应**原片第 {shot['source_shot']} 镜**")
        if shot.get("retention"):
            r.append(f"- 保留方式（四档）：{shot['retention']}")
        if shot.get("source_prompt"):
            r.append(
                "- 原片该镜的反推提示词（**沿用它的整个画面写法**：场景、陈设、道具的"
                "种类与位置、材质、构图、景别、运镜、光影、物理关系全部照它写；"
                "**只把人物与产品换成我们自己的**，禁止照抄原片人物与产品）：\n"
                f"  {shot['source_prompt']}"
            )
        items = shot.get("inherit_checklist")
        if isinstance(items, list) and items:
            r.append(
                "- **本镜必须沿用的原片画面元素**（逐条写进 `[SHOT]`/`[HERO]`/`[TEXTURE]`；"
                "一个都不许少，也不许换成别的道具）：\n"
                + "\n".join(f"    · {str(i).strip()}" for i in items if str(i).strip())
            )
            r.append(
                "- 原片没有的道具**一个也不许加**；背景与材质必须与原片一致"
                "（原片是浅色台面就写浅色，不许改成深色）。"
            )
        if r:
            remake_txt = ("\n## 复刻基准（工作流 C ★画面级复刻：只换主体，其余原样）\n"
                          + "\n".join(r) + "\n")

    user = f"""按 H3 R2V 规范写第 **{shot.get('code')}** 镜的视频提示词。

## 参考图职责（**逐字原样使用，一个字都不许改写或增删**；R2V：每张参考只负责一项）
[REF] {'人物参考职责（本镜值）' if has_person else '人物参考（本镜无人物）'}：{ref_line}
[PRODUCT] 产品参考职责（本镜值）：{prod_line}
全片光影走向：{anchors.get('light_arc', '')}
风格参数：{anchors.get('style', '')}
{prod_block}{look_block}{no_person_block}{wear_block}
## 本镜的骨架设定（照此展开，不要另起炉灶）
- 镜号：{shot.get('code')}（{shot.get('start_sec')}-{shot.get('end_sec')}s，{shot.get('beat')}）
- 情绪点（全片情绪弧上本镜的位置）：{shot.get('emotion') or '—'}
- 卖点可视化：{shot.get('visual_proof') or 'none'}（不是 none 时必须让它**出现在画面里**
  ——写进 [SHOT]/[TEXTURE] 的可见元素，不许只停留在念白里；是 none 时不要硬塞道具）
- 主动作：{shot.get('action')}
- 景别：{shot.get('shot_size')}
- 运镜：{shot.get('camera')}
- 光线：{shot.get('light')}
- 第一帧状态：{shot.get('entry_state')}
- 最后一帧状态：{shot.get('exit_state')}
- 本镜念白（{shot.get('voiceover')}）—— **只写进念白表，不要写进视频提示词**（画面里不出现文字；`[AUDIO]` 层也只写环境声并锁 no dialogue）

## 前后镜衔接（用于保证不穿帮，不要在本镜里重做别的镜的动作）
- 上一镜：{prev_txt}
- 下一镜：{next_txt}
{remake_txt}{spray_block}
## 一致性铁律（逐条检查后再落笔）
{rules_for(perfume)}

## v2 模板层序（严格按此顺序与标记；本镜不需要的层可省略，但顺序不许变）
{V2_LAYERS}

## 输出格式（严格照此两段，不要多写别的）
第一段是提示词本体，放在 text 代码块里：

```text
[REF] <逐字复用上面的人物参考职责；本镜无人物时**照抄上面的「本镜无人物」那句**，不许写主播外貌>
[PRODUCT] <逐字复用上面的产品参考职责>
[SHOT] ...
[HERO] ...
[MOTION] ...
[CAMERA] ...
[LIGHT] ...
[PHYSICS] ...（本镜没有结果性元素就整层省略）
[TEXTURE] ...
[AUDIO] ...（每镜必写）
[NEG] ...
```

第二段是中文直译，供人工审核：

**中文直译**：
[参考] ...
[产品] ...
[镜头] ...
...（逐层对应，每层一句中文，[AUDIO] 对应 [声音]）
"""
    return system, user


# ------------------------------------------------------------------ 解析


def _strip_code_fence(raw: str) -> str:
    """去掉 ```json / ```text 包裹（模型经常加，不加时原样返回）。"""
    t = (raw or "").strip()
    m = re.search(r"```[a-zA-Z]*\s*\n(.*?)```", t, re.S)
    if m:
        return m.group(1).strip()
    return t


def parse_skeleton(raw: str) -> Dict[str, Any]:
    """解析骨架 JSON。容错：去代码块包裹、截取首个 { 到末个 }。"""
    text = _strip_code_fence(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            raise ValueError(f"骨架不是合法 JSON：{raw[:300]}") from None
        data = json.loads(text[i:j + 1])
    if not isinstance(data, dict) or not data.get("shots"):
        raise ValueError(f"骨架缺少 shots：{raw[:300]}")
    if not isinstance(data.get("anchors"), dict):
        data["anchors"] = {}
    return data


def parse_shot_output(raw: str) -> Tuple[str, str]:
    """从单镜返回里切出 (提示词正文, 中文直译)。切不出来就整体当正文。"""
    t = (raw or "").strip()
    m = re.search(r"```(?:text)?\s*\n(.*?)```", t, re.S)
    if m:
        body = m.group(1).strip()
        rest = (t[:m.start()] + t[m.end():]).strip()
    else:
        body, rest = t, ""
    zh = ""
    zh_m = re.search(r"\*\*中文直译\*\*\s*[:：]?\s*(.+)$", rest, re.S)
    if zh_m:
        zh = zh_m.group(1).strip()
    return body, zh


def _anchor_lines(prompt_text: str) -> Dict[str, str]:
    """取出 [REF] / [PRODUCT] 两层的实际内容（可能跨行）。"""
    out: Dict[str, str] = {}
    for tag in ("REF", "PRODUCT"):
        m = re.search(rf"^\[{tag}\]\s*(.+?)(?=^\[[A-Z]+\]|\Z)", prompt_text,
                      re.S | re.M)
        if m:
            out[tag] = re.sub(r"\s+", " ", m.group(1)).strip()
    return out


def enforce_anchors(
    prompt_text: str, anchors: Dict[str, Any],
    *, ref: Optional[str] = None, product: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """用骨架里的锚点**强制覆盖**单镜产出里的 [REF] / [PRODUCT]。

    为什么不"检查不一致就报错"：并发场景下让某一镜失败重跑，代价是多等一轮，
    而锚点不一致是可确定修复的 —— 直接以后者为准替换，并记 warning 即可。
    这是"三道闸门"里的第三道：不指望模型每次都听话。

    ★R-33：`ref` / `product` 允许传**本镜的值**（造型按章节差异化的 [REF]、
    只含本镜商品的 [PRODUCT]）；不传则退回骨架的全局锚点。
    """
    warnings: List[str] = []
    text = prompt_text
    want = {
        "REF": (ref if ref is not None else (anchors.get("talent_ref") or "")).strip(),
        "PRODUCT": (product if product is not None
                    else (anchors.get("product_ref") or "")).strip(),
    }
    got = _anchor_lines(text)
    for tag, expect in want.items():
        if not expect:
            continue
        actual = got.get(tag, "")
        if not actual:
            # 完全没写这一层：补在最前面（v2 层序里 [REF]/[PRODUCT] 靠后，
            # 但缺失时补在开头比丢掉整层锚点安全 —— 一致性优先）
            text = f"[{tag}] {expect}\n" + text
            warnings.append(f"{tag} 层缺失，已用骨架锚点补上")
            continue
        if re.sub(r"\s+", " ", actual).strip() != re.sub(r"\s+", " ", expect).strip():
            text = re.sub(
                rf"^\[{tag}\].*?(?=^\[[A-Z]+\]|\Z)",
                f"[{tag}] {expect}\n",
                text, count=1, flags=re.S | re.M,
            )
            warnings.append(f"{tag} 层与骨架不一致，已强制统一为骨架锚点")
    return text, warnings


# ------------------------------------------------------------------ 合并


def _self_check_laws(
    parts: List[Tuple[Dict[str, Any], str, str]], *, perfume: bool = True
) -> List[str]:
    """铁律自检（第四道闸门）。

    并发链路**没有"一个 agent 通盘把关"**——原链路里 agent 会在最后自检评分，
    并发模式下每个镜是独立生成的，没有谁替全片做检查。所以把技能里最硬、
    漏掉就一定崩的两条变成代码检查：

      ① **必须有喷香水镜**（仅香水品类，R-17 起按品类开关）—— 技能口径里
         "成片没有喷香水镜头"是常见错误自查项，而香水片缺了这个核心动作，
         投放效果直接废掉；
      ② **喷雾镜必须交代开盖状态** —— 技能里最典型的崩点是"瓶盖凭空消失"，
         正确写法是让瓶盖根本不入画（`no cap visible in frame`）或
         `Do not show cap removal motion`。光写"按压喷头"是不够的。
         这条按**镜内内容**触发：非香水品类里真写了喷雾动作的镜照样查。

    `perfume=False` 时不再强制"必须有喷香水镜"（口红/面霜片本来就不该有），
    但已识别出的喷雾镜仍查开盖状态。

    这里只**报警不阻断**：产出仍然给用户（他能自己补一句再出片），但必须
    把风险明确写出来，不能让一个违反铁律的脚本静默通过。
    """
    warns: List[str] = []
    texts = [(s.get("code"), p) for s, p, _ in parts if p.strip()]
    spray = [(c, t) for c, t in texts if re.search(r"nozzle|spray|press", t, re.I)]
    if not spray:
        if perfume:
            warns.append(
                "铁律自检：全片没有可识别的喷香水镜（香水片的核心动作），建议重生成"
            )
        return warns
    if not [c for c, t in spray
            if re.search(r"no cap|uncapped|cap is not|cap removal|cap stays off",
                         t, re.I)]:
        warns.append(
            "铁律自检：喷香水镜未交代开盖状态（瓶盖极易在两帧之间凭空消失）—— "
            "建议在那一镜的 [NEG] 补 `Do not show cap removal motion`，"
            "或在 [PRODUCT] 写明 `no cap visible in frame`"
        )
    return warns


_DROPLET_NEG = "no raised droplets, no beads, no drips"
_CAP_NEG = "no cap visible in frame"


def enforce_spray_negs(prompt_text: str) -> Tuple[str, List[str]]:
    """喷香水镜的反向约束兜底（第五道闸门）。

    实测穿帮：`[PHYSICS]` / `[TEXTURE]` 主动写了 droplets / wet sheen，而
    `[NEG]` 一条"不要凸起水珠"都没有 → 模型照字面画出油珠 + 油膜 + 流挂。
    铁律 11 的反向写法是**确定性可补**的 —— 喷香水镜的 `[NEG]` 缺水珠反向
    就自动补上并记 warning；瓶盖分两种情况：
      - **正文**没提 cap → 自动补 `no cap visible in frame`（铁律 3：喷雾镜第一帧
        就已开盖、瓶盖根本不入画，这是技能钦定写法）；
      - **正文**提了 cap（如瓶盖反光细节）→ 自动补会制造自相矛盾，只能**报警**
        让人删细节（不指望代码改写语义）。

    ★作用域（R-35）：「是不是喷雾镜」与「正文有没有提 cap」都只看**导演正文**
    （剔除 `[REF]`/`[PRODUCT]`/`[NEG]`）。原实现拿全文判，锚点里的品类词
    （"perfume"/"bottle"）让每一镜都被当成喷雾镜、锚点里的瓶身描述
    （"oval black cap"）让 `cap_in_body` 恒为真 → 补 cap 的兜底**从未生效**。
    """
    warnings: List[str] = []
    neg_m = re.search(r"^\[NEG\]\s*(.+?)(?=^\[[A-Z]+\]|\Z)", prompt_text, re.S | re.M)
    neg = neg_m.group(1) if neg_m else ""
    body = _directorial_body(prompt_text)
    if not _SPRAY_RE.search(body):
        return prompt_text, warnings          # 正文里没有喷雾动作，不是喷香水镜
    add: List[str] = []
    if not re.search(r"droplet|bead|drip", neg, re.I):
        add.append(_DROPLET_NEG)
    cap_in_body = bool(re.search(r"\bcap\b", body, re.I))
    if not cap_in_body and not re.search(r"\bcap\b", neg, re.I):
        add.append(_CAP_NEG)
    if add:
        addition = ("; " + ", ".join(add) + ".") if neg.strip() else \
            (" " + ", ".join(add) + ".")
        if neg_m:
            prompt_text = prompt_text[:neg_m.end(1)] + addition + prompt_text[neg_m.end(1):]
        else:
            prompt_text = prompt_text.rstrip() + f"\n[NEG]{addition}"
        warnings.append("喷香水镜 [NEG] 缺反向约束，已自动补：" + "、".join(add))
    if cap_in_body:
        warnings.append(
            "喷香水镜正文提到瓶盖（cap）—— 按铁律 12 应删掉瓶盖细节、统一 "
            "no cap visible in frame，否则瓶盖极易留在瓶上（代码不改写语义，请人工确认）"
        )
    elif re.search(r"\bcap\b", _product_anchor_text(prompt_text), re.I):
        # 正文没提，但**商品锚点**在描述带盖的瓶身（锚点逐字来自参考图识别）。
        # 这是"成片带盖"的真正根因：参考图本身就是带盖拍的，模型照参考图保留瓶盖。
        # 代码不能改锚点语义（锚点是与参考图对表的），所以如实指出可动作的修法。
        warnings.append(
            "喷香水镜的商品锚点在描述**带盖的瓶身** —— 参考图本身就是带盖拍的，"
            "模型照参考图保留瓶盖的概率很高。建议在商品库把该商品的主图换成"
            "开盖/无盖的瓶身照，或接受模型自行取舍"
        )
    return prompt_text, warnings


# 纯产品镜的人物反向：只禁"人"，**不禁手** —— 窄词表可能把"a finger presses…"
# 这种手部动作镜判成无人物，禁掉手会与本镜动作自相矛盾（手要不要出现由该镜
# 自己的 [NEG] 决定，代码不越权）。
_NO_PERSON_NEG = "no person, no figure, no face"


def enforce_no_person_neg(prompt_text: str, no_person: bool) -> Tuple[str, List[str]]:
    """纯产品镜的 `[NEG]` 兜底（第六道闸门）：补一条"画面里不要出现人"。

    配合 `ref_line_for_shot` 的「本镜无人物」`[REF]`：一边声明没有人物参考，
    一边用 `[NEG]` 堵住模型顺手塞个人进来（R-16 同类穿帮的预防点）。
    """
    warnings: List[str] = []
    if not no_person:
        return prompt_text, warnings
    neg_m = re.search(r"^\[NEG\]\s*(.+?)(?=^\[[A-Z]+\]|\Z)", prompt_text, re.S | re.M)
    neg = neg_m.group(1) if neg_m else ""
    # 已经提过人物/人影/脸（多为 "Do not add hands or people"）→ 不重复补
    if re.search(r"\b(person|people|figure|human|face)\b", neg, re.I):
        return prompt_text, warnings
    if neg_m:
        prompt_text = (prompt_text[:neg_m.end(1)] + "; " + _NO_PERSON_NEG + "."
                       + prompt_text[neg_m.end(1):])
    else:
        prompt_text = prompt_text.rstrip() + f"\n[NEG] {_NO_PERSON_NEG}."
    warnings.append("纯产品镜 [NEG] 缺人物反向，已自动补：" + _NO_PERSON_NEG)
    return prompt_text, warnings


# 走光类反向约束（第七道闸门）—— 只禁"走光"，**不禁身材**：模特本身的身材、
# 曲线、领口形状都由参考图决定，代码不越权改写形象。
# ★R-41：旧串只防"透"（no sheer / opaque），对"**根本没穿**"是语义缺口 ——
# 当 [REF] 一整句没有任何服装名词时，整条约束在"有衣服前提"下失效，成片裸胸。
# 这里补上"上身必须有衣物覆盖 / 不得裸胸"的硬约束，与第八道正向闸门
# `enforce_wear_positive`（强制 [REF] 含一件服装）双保险。
_WEAR_NEG = ("the model's chest and torso must remain fully covered by opaque clothing "
             "at all times, no bare chest, no exposed torso, no visible nipples, "
             "no nipple outline showing through the fabric, no sheer or see-through "
             "fabric on the bodice, the neckline fabric stays smoothly draped and opaque")
# 已提过任何一条同类约束就不重复补（幂等）
_WEAR_DONE_RE = re.compile(
    r"nipple|areola|see-?through|sheer|transparent fabric|braless|no bra|"
    r"opaque fabric|draped and opaque|bare chest|exposed torso|fully covered", re.I)


def enforce_wear_negs(prompt_text: str, has_person: bool) -> Tuple[str, List[str]]:
    """人物镜的着装合规兜底（第七道闸门）。

    ★R-38 定责（用户反馈"人物胸部有凸起的点"）：根因不在视频模型，而在**上游把
    「无内衬薄缎面贴身」当成了身份的一部分** —— 主播锚点里焊着 `wearing a
    champagne satin slip dress`（薄缎面、无内衬），参考图本身就是这么拍的，
    全流程**零走光类反向词**（`nipple / braless / cleavage / areola` 在 `app/`
    下 grep 命中 0 次），而 `[TEXTURE]` 还在强调 `liquid satin sheen`
    （缎面光泽会加强内衣痕迹）。模型只是忠实还原了参考图。

    这里做**确定性可补**的那一半：人物镜的 `[NEG]` 缺走光约束就自动补上。
    另一半（锚点着装层）由 `talent_ref_for_shot` + 本镜 `talent_look` 的
    「替换而非追加」负责。**代码只能降低概率，不能保证消除** —— 参考图本身
    把形态焊死了，最彻底的做法是换一张有内衬/有领口结构的形象图，脚本里会如实报出。
    """
    warnings: List[str] = []
    if not has_person:
        return prompt_text, warnings
    neg_m = re.search(r"^\[NEG\]\s*(.+?)(?=^\[[A-Z]+\]|\Z)", prompt_text, re.S | re.M)
    neg = neg_m.group(1) if neg_m else ""
    if _WEAR_DONE_RE.search(neg):
        return prompt_text, warnings
    if neg_m:
        prompt_text = (prompt_text[:neg_m.end(1)] + "; " + _WEAR_NEG + "."
                       + prompt_text[neg_m.end(1):])
    else:
        prompt_text = prompt_text.rstrip() + f"\n[NEG] {_WEAR_NEG}."
    warnings.append(
        "人物镜 [NEG] 缺着装合规反向约束，已自动补（走光类）：可见乳点 / 透薄面料 / "
        "领口织物必须平整不透 —— 若成片仍有凸起，请换一张有内衬或领口结构的形象图")
    return prompt_text, warnings


# ★R-41 第八道闸门：人物镜 [REF] **必须含一件服装**（正向兜底）。
# 与第七道 `enforce_wear_negs`（只防"透"）互补——后者在"根本没穿"时整条失效。
# 本闸门在 `enforce_anchors` 把最终 [REF] 强制覆盖之后运行，直接对成品 [REF] 判：
# 没有 `_GARMENT_KW` 命中（只有配饰或为空）→ 强制注入一件基础着装。
# 优先级：主播 spec.outfit 英文短语（词表唯一来源 `pipeline/talent_spec.py`）
#          → 常量兜底 `_DEFAULT_GARMENT`（含 top 命中，安全、不露肤）。
_DEFAULT_GARMENT = "a casual top with full sleeves"


def _default_outfit_phrase(spec: Any) -> Optional[str]:
    """从主播 spec 取 outfit 维度的英文短语（不含 leading 'wearing '）。

    词表唯一事实来源 `pipeline/talent_spec.py`：spec 里是 `{"outfit": "knit-casual"}`
    这类 value，转成英文裸短语（如 `a soft oatmeal wool knit sweater with
    straight-leg trousers`）交给 [REF] 注入，避免与 `wearing ` 前缀重复。
    """
    if not isinstance(spec, dict):
        return None
    outfit = spec.get("outfit")
    if not outfit:
        return None
    opt = _talent_spec.OPT_BY_KEY.get("outfit", {}).get(str(outfit).strip())
    if opt and getattr(opt, "en", ""):
        return opt.en
    return None


def enforce_wear_positive(
    prompt_text: str, has_person: bool,
    fact_spec: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[str]]:
    """人物镜 [REF] 缺服装名词时强制注入一件基础着装（第八道闸门）。

    ★R-41 根因：M01 idx=1 的 [REF] 经 `talent_ref_for_shot` 后只剩
    "composed natural presence, wearing thin gold necklace…"（配饰顶掉了本就空缺的
    锚点服装），[HERO] 又正向引导露肤 → 成片裸胸＋项链。第七道闸门 `enforce_wear_negs`
    整句预设"有衣服"，此时完全失效。本闸门在合并阶段对**最终 [REF]** 做确定性补衣：

      - 已有服装名词 → 不动（与 R-38 替换/保留逻辑衔接，不回退）；
      - 已有 `wearing X` 但 X 只有配饰（无 _GARMENT_KW）→ 把默认服装插到 wearing 之后、
        其余之前，仍是**一个 wearing 句式**（不破坏 R-38「一句话只一件衣服」铁律）；
      - 完全没有 wearing 从句 → 身份层后追加 `, wearing <默认服装>`；
      - [REF] 为空 → 直接置为 `wearing <默认服装>`。

    纯产品镜（`has_person=False`）不动；已写过同类约束由 `_GARMENT_KW` 命中判定幂等。
    """
    warnings: List[str] = []
    if not has_person:
        return prompt_text, warnings
    ref_m = re.search(r"^\[REF\]\s*(.+?)(?=^\[[A-Z]+\]|\Z)",
                      prompt_text, re.S | re.M)
    if not ref_m:
        return prompt_text, warnings
    ref = ref_m.group(1).strip()
    if _GARMENT_KW.search(ref):
        return prompt_text, warnings
    # 防御：`has_person` 与 [REF] 自述冲突时**听 [REF] 的**。
    # 纯产品镜的 [REF] 是 `_NO_PERSON_REF`（"…no person, no face…"），若照样注入
    # "wearing a casual top with full sleeves" 就成了「本镜无人物却穿着上衣」的
    # 自相矛盾指令 —— 比漏穿衣服更糟，会让模型凭空加人。
    if re.search(r"\bno\s+(person|people|figure|human|face)\b", ref, re.I):
        return prompt_text, warnings
    garment = _default_outfit_phrase(fact_spec) or _DEFAULT_GARMENT
    if not ref:
        new_ref = f"wearing {garment}"
    elif re.search(r"\bwearing\b", ref, re.I):
        new_ref = re.sub(r"(\bwearing\b)", lambda m: f"{m.group(1)} {garment},",
                         ref, count=1, flags=re.I)
    else:
        new_ref = ref.rstrip() + f", wearing {garment}"
    prompt_text = (prompt_text[:ref_m.start(1)] + new_ref
                   + prompt_text[ref_m.end(1):])
    warnings.append(
        "人物镜 [REF] 缺服装名词（本镜只写了配饰或为空），已强制注入一件基础着装："
        + garment + " —— 若与主播形象图冲突，请补全主播 spec.outfit 或本镜 talent_look")
    return prompt_text, warnings


# ★R-38b 沿用清单的**实体级**核对表（「清单提到了哪类东西」→「提示词里有没有」）。
#
# 为什么不用逐词命中率（R-38 第一版实现，实测假阳性高、真阳性还漏 —— 拿真实产出核过）：
#   ① 同义改写被误判：清单写 `bunch of dark purple-black berries on right`，提示词写
#      `dark purple grapes right` —— 同一个东西换了写法，逐词只命中 1/4 → 假报警；
#   ② 复合词整体匹配：`purple-black` / `five-petaled` / `yellow-edged` 在提示词里被拆开写
#      （`dark purple` / `white flowers`）就永远匹配不上；
#   ③ 反过来真丢的还漏：`thin gold ring on right ring finger` 因为 `thin/gold/right` 几个
#      高频词命中就过关，可原片手指上那枚戒指提示词里确实没写。
# 实体级只看**东西有没有**、不看修饰词写没写全；组内同义词互相顶替（berries≈grapes、
# flowers≈roses≈blooms、earrings≈hoops）。每条写成**正则片段**而不是裸词：
#   ① 允许衍生的用 `\b<词根>`（`\bberr` 收 berries/berry、`\bleaf` 收 leaf/leaves、
#      `\bvein` 收 veins/veining），这样"清单写 berries、提示词写 grape"不会互相漏掉；
#   ② 容易撞车的短词必须带词尾边界 —— 实测 `\bbun` 会命中 **bunch**（把 A3 的
#      "bunch of dark purple-black berries" 误报成"少了发型"）、`\btub` 会命中 tube、
#      `\bstud` 会命中 study、`\btap` 会命中 tapestry → 一律收紧成 `\bbuns?\b` 这类。
_INHERIT_ENTITY: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("flowers", (r"\bflower", r"\bbloom", r"\bblossom", r"\brose", r"\bpetal", r"\bfloral")),
    ("berries or grapes", (r"\bberr", r"\bgrape")),
    ("leaves", (r"\bleaf", r"\bfoliage")),
    ("lemon or citrus", (r"\blemon", r"\bcitrus", r"\blime")),
    ("almonds", (r"\balmond",)),
    ("coffee beans", (r"\bcoffee\s+bean", r"\bbeans?\b")),
    ("counter or tabletop", (r"\bcounter", r"\btabletops?\b", r"\btable\s+top",
                             r"\bsurface", r"\bslab")),
    ("marble", (r"\bmarble", r"\bvein")),
    ("bottle", (r"\bbottle", r"\bflacon")),
    ("cap or lid", (r"\bcap", r"\blids?\b", r"\bstopper")),
    ("faucet", (r"\bfaucet", r"\btaps?\b")),
    ("bathtub", (r"\bbathtub", r"\btubs?\b")),
    ("tiles", (r"\btile", r"\bgrout")),
    ("bar lamp or light pillar", (r"\blamp", r"\blight\s+(?:bar|column|pillar|strip|tube|panel)")),
    ("steam or vapor", (r"\bsteam", r"\bvapo", r"\bmist", r"\bsmoke", r"\bhaze")),
    ("earrings", (r"\bearring", r"\bhoop", r"\bstuds?\b")),
    ("necklace or pendant", (r"\bnecklace", r"\bpendant", r"\bchains?\b")),
    ("bracelet", (r"\bbracelet", r"\bbangle")),
    ("ring", (r"\brings?\b",)),
    ("reflection", (r"\breflect", r"\bmirror")),
    ("hair style", (r"\bhair", r"\bbuns?\b", r"\bwaves?\b", r"\bbraid")),
    ("dress", (r"\bdress", r"\bgown", r"\bsatin")),
    ("stone", (r"\bstone", r"\brock", r"\bboulder")),
    ("label or printed text", (r"\blabel", r"\bprinted\s+text", r"\btext\s+reading")),
    ("tray or tableware", (r"\btray", r"\bplates?\b", r"\bdish", r"\bcups?\b",
                           r"\bbowls?\b", r"\bglassware")),
)

_INHERIT_ENTITY_RE = tuple(
    (label, tuple(re.compile(pat, re.I) for pat in pats))
    for label, pats in _INHERIT_ENTITY
)


def verify_inherit(parts: List[Tuple[Dict[str, Any], str, str]]) -> List[str]:
    """复刻「沿用清单」回查（★R-38 画面级复刻的机器闸门）。

    画面级复刻的全部要求就是"原片的画面元素一个都不许少"，而唯一可机器核对的抓手
    是骨架自己产出的 `inherit_checklist`。判据是**实体级**（理由见 `_INHERIT_ENTITY`
    表上方的注释）：

      清单项里提到某类实体 → 该类实体必须在提示词里出现；没出现就点名报警。

    只报警不自动改提示词：清单是模型产出的，提示词里可能刻意换了写法，代码硬补会
    写出重复描述甚至自相矛盾（与 `enforce_spray_negs` 对 cap 的处理同一原则）。
    报警里带上**清单原句**，用户/复刻环节才知道到底缺的是哪一件东西。
    """
    warnings: List[str] = []
    for shot, prompt_text, _ in parts:
        items = shot.get("inherit_checklist")
        if not isinstance(items, list) or not items:
            continue
        hay = str(prompt_text or "")
        missing: List[str] = []
        seen: List[str] = []
        for it in items:
            text = str(it or "").strip()
            if not text:
                continue
            for label, res in _INHERIT_ENTITY_RE:
                # 清单项没提到这类实体就跳过（清单项千差万别，只查它自己说了的）
                if not any(r.search(text) for r in res):
                    continue
                # 组内任一关键词在提示词里出现即算保留（同义改写不报警）
                if any(r.search(hay) for r in res):
                    continue
                if label in seen:
                    continue
                seen.append(label)
                missing.append(f"{label}（清单原句：{text}）")
        if missing:
            warnings.append(
                "画面级复刻：原片该镜提到的东西在提示词里找不到 → "
                + "；".join(missing)
                + "（原片的场景/陈设/道具一个都不许少，请补进 [SHOT]/[HERO]/[TEXTURE]）")
    return warnings


# 人物配饰归 `[HERO]`（主体层）：耳环/项链这些写在主体描写里，才不会被 i2v 模型
# 当成"画面角落的陈设"而丢掉。
_INHERIT_ACCESSORY_RE = re.compile(
    r"\b(earring|hoop|stud|necklace|pendant|chain|bracelet|bangle|rings?|anklet|tiara|brooch)",
    re.I)


def _append_layer(text: str, layer: str, clause: str) -> str:
    """把 `clause` 追加到以 `[layer]` 开头那一行的行尾（找不到该层则原样返回）。"""
    m = re.search(r"(?m)^\[" + layer + r"\](.+)$", text)
    if m is None:
        return text
    line = m.group(0).rstrip()
    if not line.endswith("."):
        line += "."
    return text[:m.start()] + line + " " + clause + text[m.end():]


def repair_inherit(shot: Dict[str, Any], prompt_text: str) -> Tuple[str, List[str]]:
    """把**实体级核对确认缺席**的原片画面元素补进本镜提示词（★R-38b）。

    为什么这里敢"代码硬补"，而 `verify_inherit` 的旧实现坚持"只报警不改"：
    判据换成实体级之后，报警的假阳性实测为 0（拿项目 12 的真实产出回放：报 4 条，
    逐条人工核对全部为真丢 —— 柠檬、戒指、手镯、耳环、浴缸边沿，全是原片画面里
    有、提示词里没有的）。"连词根都没出现"意味着**这件东西确实不在画面描写里**，
    补上它是**恢复原片画面**，不是往画面里加原本没有的东西 —— 与 `enforce_spray_negs`
    自动补 NEG 属于同一类确定性兜底。

    补法只管**追加**、绝不删改既有内容：台面道具/背景陈设追加进 `[SHOT]`（画面层），
    人物配饰追加进 `[HERO]`（主体层）。两层都找不到时不动它，交回 `verify_inherit` 报警。
    """
    items = shot.get("inherit_checklist")
    if not isinstance(items, list) or not items:
        return prompt_text, []
    text = str(prompt_text or "")
    if not text.strip():
        return text, []
    missing: List[str] = []
    for it in items:
        raw = str(it or "").strip()
        if not raw:
            continue
        for _label, res in _INHERIT_ENTITY_RE:
            # 清单项没提这类实体 → 不是它要管的事
            if not any(r.search(raw) for r in res):
                continue
            # 提示词里已经有了（同义词组内任一命中）→ 不补
            if any(r.search(text) for r in res):
                break
            if raw not in missing:
                missing.append(raw)
            break
    if not missing:
        return text, []

    acc = [m for m in missing if _INHERIT_ACCESSORY_RE.search(m)]
    rest = [m for m in missing if m not in acc]
    notes: List[str] = []

    def _place(prefer: str, group: List[str]) -> None:
        nonlocal text
        if not group:
            return
        clause = "Also present in the frame: " + ", ".join(group) + "."
        for layer in (prefer, "HERO" if prefer == "SHOT" else "SHOT"):
            new_text = _append_layer(text, layer, clause)
            if new_text != text:
                text = new_text
                notes.append(f"画面级复刻：原片有、提示词漏了的元素已自动补进 [{layer}] → "
                             + "、".join(group))
                return

    _place("SHOT", rest)
    _place("HERO", acc)
    return text, notes


# ★R-38c `[NEG]` 把主动作一起禁掉：模型写反向词时爱抄一份"静止机位"模板
# （`no zoom / no pan / no rotation`），而复刻原片又要求这一镜**原地转身**。
# 实测（项目 12 的第 5 镜，真实产出）：同一镜里并存
#   `[MOTION] at 1.5s she rotates on the spot about 120 degrees … turns back to face camera`
#   `[NEG] … no cut, no zoom, no rotation`
# i2v 模型收到的是**互相打架**的两条指令，而 NEG 通常压过正文 → 核心动作被抹掉
# （这镜的全部意义就是"喷雾 + 转身走入汽雾"，不转就废了）。
#
# 只处理**镜头运动类**反向词。内容性禁止（`no mist` / `no cap` / `no droplets`）是防穿帮
# 闸门**有意为之**（非喷雾镜必须禁雾、带盖主图必须禁盖），绝不能因为"正文提到了雾"
# 就把它们删掉 —— 那是两回事：前者是代码写的约束，后者是模型抄的模板。
_MOTION_NEG_ITEMS = (
    ("rotation", re.compile(r"\brotat", re.I)),
    ("zoom", re.compile(r"\bzoom", re.I)),
    ("pan", re.compile(r"\bpan(?:s|ning)?\b", re.I)),
    ("tilt", re.compile(r"\btilt", re.I)),
    ("camera movement", re.compile(r"\bcamera\s+mov", re.I)),
    ("dolly", re.compile(r"\bdolly", re.I)),
    ("tracking", re.compile(r"\btrack", re.I)),
    ("crane", re.compile(r"\bcrane", re.I)),
)
# `(?<!no\s)`：正文自己写的 "no zoom" 不算正向动作（否则会把自己 NEG 里的 no zoom 删掉）。
# `turn` 一并收进 rotation —— 转身就是绕自身轴的旋转，模型两种写法都会用。
_MOTION_POS_ITEMS = {
    "rotation": re.compile(r"(?<!no\s)\b(?:rotat|turn(?:s|ing|ed)?|swivel|spin)", re.I),
    "zoom": re.compile(r"(?<!no\s)\bzoom", re.I),
    "pan": re.compile(r"(?<!no\s)\bpan(?:s|ning)?\b", re.I),
    "tilt": re.compile(r"(?<!no\s)\btilt", re.I),
    "camera movement": re.compile(r"(?<!no\s)\bcamera\s+(?:mov|travel|push|pull)", re.I),
    "dolly": re.compile(r"(?<!no\s)\bdolly", re.I),
    "tracking": re.compile(r"(?<!no\s)\btrack", re.I),
    "crane": re.compile(r"(?<!no\s)\bcrane", re.I),
}


def enforce_motion_negs(prompt_text: str) -> Tuple[str, List[str]]:
    """剔除与 [MOTION] 正向动作打架的镜头运动类 `[NEG]` 项（★R-38c）。

    只**删**那些"反向词禁止的动作、正文恰恰要求做"的项（如正文转身 + NEG no rotation），
    其余反向词一律不动。删的是**冗余且有害**的一条：正文已经明说做什么，NEG 再禁它
    只会让模型收到矛盾指令。
    """
    m = re.search(r"(?m)^\[NEG\](.*)$", prompt_text)
    if m is None:
        return prompt_text, []
    # 导演正文：剔除锚点行与 NEG 自身（与 `shot_has_person` 同一原则 ——
    # 锚点是参考图说明、NEG 是禁令，都不是"这一镜要演什么"）
    body = re.sub(r"(?m)^\[(?:REF|PRODUCT|NEG)\].*$", "", prompt_text)
    kept: List[str] = []
    dropped: List[str] = []
    for piece in m.group(1).split(","):
        item = piece.strip()
        if not item:
            continue
        label = ""
        if re.match(r"(?i)^no\b", item):
            for name, neg_re in _MOTION_NEG_ITEMS:
                if neg_re.search(item):
                    label = name
                    break
        pos_re = _MOTION_POS_ITEMS.get(label) if label else None
        if pos_re is not None and pos_re.search(body):
            dropped.append(item)
            continue
        kept.append(item)
    if not dropped:
        return prompt_text, []
    new_text = prompt_text[:m.start()] + "[NEG] " + ", ".join(kept) + prompt_text[m.end():]
    return new_text, [
        "反向词与本镜动作自相矛盾，已从 [NEG] 删除：" + "、".join(dropped)
        + "（[MOTION] 要求做这个动作，NEG 又禁它 —— 模型收到打架的指令大概率把主动作丢掉）"]


def merge_script(
    skeleton: Dict[str, Any],
    shot_parts: List[Tuple[Dict[str, Any], str, str]],
    *,
    project_name: str,
    lang: str,
    dur: float,
    source_note: str = "",
) -> str:
    """把骨架 + 各镜产出拼成最终脚本。

    **分镜表、念白稿、音效挂点三块由代码拼装**，不让模型重复输出：
    既省 token（原来这三块要模型再写一遍），也保证它们与逐镜内容严格一致
    （模型自己写表时经常与正文对不上）。
    """
    anchors = skeleton.get("anchors") or {}
    lines: List[str] = []
    lines.append(f"# {dur:.0f} 秒带货视频分镜脚本")
    lines.append("")
    lines.append(f"**项目**：{project_name}")
    lines.append(f"**投放语言**：{lang}")
    lines.append(f"**创意概念（钩子机制）**：{skeleton.get('concept') or '—'}")
    lines.append(f"**人物锚点**：{anchors.get('talent_ref') or '—'}")
    lines.append(f"**产品锚点**：{anchors.get('product_ref') or '—'}")
    lines.append(f"**光影走向**：{anchors.get('light_arc') or '—'}")
    lines.append(f"**风格参数**：{anchors.get('style') or '—'}")
    if source_note:
        lines.append("")
        lines.append(f"> {source_note}")
    lines.append("")

    lines.append("## 分镜表")
    lines.append("")
    lines.append("| 镜号 | 时间码 | 商品 | 造型 | 段落 | 画面内容 | 景别/运镜 | 念白 | 音效 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for shot, _, _ in shot_parts:
        summary = _cell(shot.get('summary_zh'))
        emo = (shot.get('emotion') or '').strip()
        if emo:
            summary = f"{summary}（情绪：{emo}）"
        prods = shot.get("products")
        prods_cell = "、".join(str(c) for c in prods) if isinstance(prods, list) and prods \
            else "—"
        lines.append(
            f"| {shot.get('code')} | {shot.get('start_sec')}-{shot.get('end_sec')}s "
            f"| {_cell(prods_cell)} | {_cell(shot.get('talent_look'))} "
            f"| {shot.get('beat')} | {summary} "
            f"| {_cell(shot.get('shot_size'))} + {_cell(shot.get('camera'))} "
            f"| {_cell(shot.get('voiceover'))} | {_cell(shot.get('sfx'))} |"
        )
    lines.append("")
    # 「商品」列是逐镜商品分配的**唯一落库入口**（refs.py 靠它决定挂哪个商品的图）；
    # 「造型」列是章节级形象差异化的记录。两列都由 parse_script 读回 Shot。
    if any(isinstance(s.get("products"), list) and s.get("products")
           for s, _, _ in shot_parts):
        lines.append("> 「商品」列 = 本镜画面里出现的商品（单商品镜只写一个）——"
                     "出片时**只有这一列的商品的图会挂给该镜**，"
                     "其它商品连参考图都不会发出去。")
        lines.append("")

    # R-32 复刻项目：额外出一张"本片镜号 ↔ 原片镜号 ↔ 保留方式"对照表，
    # 让脚本自己说清哪几镜是原样保留手法、哪几镜是特征迁移（换人换品）。
    remake_rows = [s for s, _, _ in shot_parts
                   if s.get("retention") or s.get("source_shot")]
    if remake_rows:
        lines.append("## 复刻对照（保留原片手法 → 替换人物与产品）")
        lines.append("")
        lines.append("| 本片镜号 | 对应原片镜 | 保留方式（四档） | 原片该镜手法（比对用） |")
        lines.append("|---|---|---|---|")
        for s in remake_rows:
            lines.append(
                f"| {s.get('code')} | #{s.get('source_shot') or '-'} "
                f"| {_cell(s.get('retention'))} "
                f"| {_cell(s.get('source_prompt'))} |"
            )
        lines.append("")
        lines.append(f"> 保留方式口径：{RETENTION_SPEC}")
        lines.append("")

    lines.append("## 逐镜提示词")
    lines.append("")
    for shot, prompt_text, zh in shot_parts:
        lines.append(
            f"### {shot.get('code')}（{shot.get('start_sec')}-{shot.get('end_sec')}s"
            f"｜{shot.get('beat')}）"
        )
        lines.append("")
        lines.append("```text")
        lines.append(prompt_text.strip())
        lines.append("```")
        lines.append("")
        if zh:
            lines.append("**中文直译**：")
            lines.append("")
            lines.append(zh.strip())
            lines.append("")

    lines.append("## 念白稿（对齐时间轴）")
    lines.append("")
    lines.append("| 时间码 | 念白 | 对应镜头 | 窗口（秒） |")
    lines.append("|---|---|---|---|")
    for shot, _, _ in shot_parts:
        try:
            win = float(shot.get("end_sec")) - float(shot.get("start_sec"))
            win_s = f"{win:.1f}"
        except (TypeError, ValueError):
            win_s = "—"
        lines.append(
            f"| {shot.get('start_sec')}-{shot.get('end_sec')}s "
            f"| {_cell(shot.get('voiceover'))} | {shot.get('code')} | {win_s} |"
        )
    lines.append("")
    voiced = [s for s, _, _ in shot_parts if (s.get("voiceover") or "").strip()]
    lines.append(f"**有念白的镜数**：{len(voiced)} / {len(shot_parts)}"
                 f"（其余窗口留给音效与留白）")
    lines.append("")

    # 念白语言混杂检测（R-27 实测：pt-BR 念白混入中文"黑色葡萄纷纷成熟"导致
    # 成片配音崩坏）。投放语言不是中文而念白含汉字 → 显式标出，交给人工重写。
    if "zh" not in (lang or "").lower():
        mixed = [(s, _, _) for s, _, _ in shot_parts
                 if re.search(r"[\u4e00-\u9fff]", (s.get("voiceover") or ""))]
        if mixed:
            lines.append(f"> ⚠️ 念白语言混杂：投放语言是 {lang}，但以下镜头的念白含中文"
                         f"——后期配音前必须重写：")
            for s, _, _ in mixed:
                lines.append(f"> - {s.get('code')}：{_cell(s.get('voiceover'))}")
            lines.append("")

    # 音效挂点。**必须出表格**：下游 `parse_post_plan` 只从表格读数
    # （列表形式它认不出，实测解析出 0 条 → 一键成片没有音效）。
    lines.append("## 音效挂点")
    lines.append("")
    lines.append("| 落位时间 | 音效类型 | 对应镜头 | 说明 |")
    lines.append("|---|---|---|---|")
    unparsed: List[str] = []
    for shot, _, _ in shot_parts:
        raw = (shot.get("sfx") or "").strip()
        if not raw:
            continue
        cue = _sfx_cue(shot)
        if cue is None:
            unparsed.append(f"{shot.get('code')}：{raw}")
            continue
        at, kind, note = cue
        lines.append(f"| {at} | {kind} | {shot.get('code')} | {_cell(note)} |")
    lines.append("")
    if unparsed:
        # 如实标注，别假装解析成功。**音效类型不在白名单里时下游本来就不会响**
        # —— 与其静默丢掉，不如写进脚本让人工一眼看到。
        lines.append("> ⚠️ 以下音效没有写成 `英文关键词 @秒数`（关键词限 "
                     "mist / click / glass / breath；喷雾一律 mist，spray 已禁用），一键成片不会响：")
        for u in unparsed:
            lines.append(f"> - {u}")
        lines.append("")
    return "\n".join(lines)


def _cell(v: Any) -> str:
    """表格单元格：换行与竖线会破坏 markdown 表，统一压平。"""
    return re.sub(r"\s+", " ", str(v if v is not None else "—")).replace("|", "/").strip() or "—"


def _sfx_cue(shot: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """把模型写的音效挂点归一成 `(落位时间, 类型, 说明)`，认不出返回 None。

    判据**复用下游解析器那一套**（`_SFX_INLINE_RE` / `_normalize_sfx_kind`），
    不在这里另立一份白名单 —— 否则会出现"脚本里看着有音效、一键成片却不响"。
    模型若写成中文「喷雾声」，`_normalize_sfx_kind` 本来就认不出，这里如实
    返回 None，由 `merge_script` 标出来给人工看，**不自作主张翻译成 mist**。
    唯一的例外是 spray → mist：那是 R-22 用户明确授权的改写（啪嗒声否决），
    且 parse_post_plan 会在成片链路里显式告知，不属于静默偷换。
    """
    raw = (shot.get("sfx") or "").strip()
    if not raw:
        return None
    m = _SFX_INLINE_RE.search(raw)          # `mist @3.0s`
    if m:
        try:
            at = f"{float(m.group(2)):.1f}s"
        except (TypeError, ValueError):
            at = f"{float(shot.get('start_sec') or 0):.1f}s"
        # R-22：spray 一律归一成 mist（含阀门冲击已被用户否决）——
        # 走与解析器同一个 `_normalize_sfx_kind`，别在这里单写一份映射。
        return at, _normalize_sfx_kind(m.group(1)) or m.group(1).lower(), raw
    kind = _normalize_sfx_kind(raw)          # 只写了关键词，没有时间
    if not kind:
        return None
    try:
        at = f"{float(shot.get('start_sec') or 0):.1f}s"
    except (TypeError, ValueError):
        at = "0.0s"
    return at, kind, raw


# ------------------------------------------------------------------ 编排


def generate_parallel(
    facts: Dict[str, Any],
    *,
    llm: Any,
    lang: str,
    shots: int,
    dur: float,
    instruction: str = "",
    max_workers: int = 5,
    on_step: Optional[Any] = None,
) -> Dict[str, Any]:
    """骨架 → 逐镜并发 → 合并。返回 {script, skeleton, warnings, timings}。

    `on_step(name, detail)` 可选回调，便于上层记录进度（当前用于日志）。
    """
    import time

    warnings: List[str] = []
    timings: Dict[str, float] = {}
    perfume = is_perfume_facts(facts)          # 品类判定一次，全链共用（R-17）

    def note(name: str, detail: str = "") -> None:
        if on_step is not None:
            try:
                on_step(name, detail)
            except Exception:  # noqa: BLE001
                pass

    # ★R-33 复刻模式：**镜数与逐镜时间码由参考片的实测剪辑点给定**，请求里传的
    # shots / dur 不再有决定权。上一版正是它们把报告的 6 镜压成了 5 镜 ——
    # 报告（6 镜 3/3/2/1/4/2）与请求（5 镜 / 15s）两个真相源打架时，模型服从了请求。
    timeline = remake_timeline(facts)
    if timeline:
        shots = len(timeline)
        dur = float(timeline[-1]["end"])
        warnings.append(
            f"复刻模式：镜数与时间码按参考片的 {shots} 个实测剪辑点对齐"
            f"（总长 {dur:g}s）—— 请求里填的镜数/时长在复刻模式下不生效")

    # ① 骨架
    t0 = time.time()

    def _skeleton_call(corrective: str = "") -> Dict[str, Any]:
        s_msg, u_msg = build_skeleton_messages(
            facts, lang=lang, shots=shots, dur=dur, instruction=instruction,
            fixed_timeline=timeline or None, corrective=corrective,
        )
        res = llm.complete(s_msg, u_msg, temperature=0.5, max_tokens=4000)
        return parse_skeleton(res.text)

    note("skeleton", "开始")
    skeleton = _skeleton_call()
    # 复刻模式下镜数是硬约束：模型没照做就打回重做一次（重做一次远比拿一份
    # 节奏错的骨架去出片便宜 —— 出片是逐镜真金白银）。
    if timeline:
        got = len(skeleton.get("shots") or [])
        want = len(timeline)
        if got != want:
            note("skeleton", f"镜数 {got}≠{want}，纠正重试")
            try:
                fixed = _skeleton_call(corrective=(
                    f"\n## 上一次产出不合格（必须修正）\n"
                    f"你输出了 {got} 个镜头，而本次要求**恰好 {want} 个**。\n"
                    f"请重新输出完整 JSON：shots 数组长度必须是 {want}，"
                    f"第 i 个元素的 code / start_sec / end_sec 逐字照抄「本片槽位」表。"
                    f"不许合并、不许拆分、不许新增或删除镜头。\n"))
                if len(fixed.get("shots") or []) == want:
                    skeleton = fixed
                    warnings.append(f"骨架首轮镜数不符（{got}≠{want}），纠正重试后已对齐")
                else:
                    skeleton = fixed if len(fixed.get("shots") or []) else skeleton
                    warnings.append(
                        f"骨架纠正重试后镜数仍为 {len(skeleton.get('shots') or [])}"
                        f"（要求 {want}）—— 已按顺序尽可能对齐，请人工核对镜数")
            except Exception as e:  # noqa: BLE001 - 重试失败就用首轮结果，不阻断
                warnings.append(f"骨架纠正重试失败（{type(e).__name__}: "
                                f"{state_err(str(e))}），沿用首轮结果")
        warnings.extend(apply_timeline(skeleton, timeline))
        warnings.extend(align_retention(skeleton, timeline))
    shot_specs = list(skeleton.get("shots") or [])[:shots]
    if not shot_specs:
        raise ValueError("骨架里没有可用的镜头")
    timings["skeleton"] = time.time() - t0
    note("skeleton", f"完成 {len(shot_specs)} 镜，{timings['skeleton']:.1f}s")

    # ② 逐镜并发
    t1 = time.time()
    results: Dict[int, Tuple[str, str]] = {}
    errors: Dict[int, str] = {}

    def one(idx: int) -> Tuple[int, str, str]:
        shot = shot_specs[idx]
        s_msg, u_msg = build_shot_messages(
            skeleton, shot,
            prev_shot=shot_specs[idx - 1] if idx > 0 else None,
            next_shot=shot_specs[idx + 1] if idx + 1 < len(shot_specs) else None,
            perfume=perfume, facts=facts,
        )
        res = llm.complete(s_msg, u_msg, temperature=0.45, max_tokens=3000)
        return idx, *parse_shot_output(res.text)

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(shot_specs)))) as pool:
        futures = {pool.submit(one, i): i for i in range(len(shot_specs))}
        for fut in as_completed(futures):
            idx = futures[fut]
            code = shot_specs[idx].get("code")
            try:
                i, prompt_text, zh = fut.result()
                if not prompt_text.strip():
                    raise ValueError("返回内容为空")
                results[i] = (prompt_text, zh)
                note("shot", f"{code} 完成")
            except Exception as e:  # noqa: BLE001 - 单镜失败不拖累其余镜
                errors[idx] = f"{type(e).__name__}: {e}"
                note("shot", f"{code} 失败：{state_err(errors[idx])}")

    # 单镜失败重试一次（并发下重试成本很低，而缺一镜的脚本基本不可用）
    if errors:
        for idx in list(errors.keys()):
            shot = shot_specs[idx]
            try:
                s_msg, u_msg = build_shot_messages(
                    skeleton, shot,
                    prev_shot=shot_specs[idx - 1] if idx > 0 else None,
                    next_shot=shot_specs[idx + 1] if idx + 1 < len(shot_specs) else None,
                    perfume=perfume, facts=facts,
                )
                res = llm.complete(s_msg, u_msg, temperature=0.45, max_tokens=3000)
                prompt_text, zh = parse_shot_output(res.text)
                if prompt_text.strip():
                    results[idx] = (prompt_text, zh)
                    warnings.append(f"{shot.get('code')} 首次失败已重试成功")
                    errors.pop(idx, None)
            except Exception as e:  # noqa: BLE001
                errors[idx] = f"{type(e).__name__}: {e}"

    timings["shots"] = time.time() - t1

    # ③ 合并（锚点强制统一 + 缺镜显式标注）
    t2 = time.time()
    anchors = skeleton.get("anchors") or {}
    parts: List[Tuple[Dict[str, Any], str, str]] = []
    for idx, shot in enumerate(shot_specs):
        if idx in results:
            prompt_text, zh = results[idx]
            # ★R-33：锚点按镜强制 —— [REF] 带本镜造型、[PRODUCT] 只含本镜商品。
            prompt_text, ws = enforce_anchors(
                prompt_text, anchors,
                ref=ref_line_for_shot(anchors, shot) or None,
                product=product_ref_for_shot(anchors, shot, facts) or None,
            )
            prompt_text, ws2 = enforce_spray_negs(prompt_text)
            prompt_text, ws3 = enforce_no_person_neg(
                prompt_text, not skeleton_shot_has_person(shot))
            prompt_text, ws4 = enforce_wear_negs(
                prompt_text, skeleton_shot_has_person(shot))
            # ★R-41 第八道闸门：人物镜 [REF] 缺服装名词 → 强制注入一件基础着装。
            # 在 enforce_anchors（已把最终 [REF] 强制覆盖）之后运行，直接对成品 [REF] 判；
            # 与第七道 enforce_wear_negs（只防"透"）双保险，根治"根本没穿"的裸胸缺口。
            prompt_text, ws7 = enforce_wear_positive(
                prompt_text, skeleton_shot_has_person(shot),
                fact_spec=(facts.get("talent") or {}).get("spec"))
            # ★R-38b：实体级回查发现的原片画面元素缺席 → 确定性补进 [SHOT]/[HERO]。
            # 放在 `verify_inherit` **之前**：补得上的就不该再报警，报警留给"两层都补不进"
            # 的极端情况（那时提示词结构本身有问题，需要人看）。
            prompt_text, ws5 = repair_inherit(shot, prompt_text)
            # ★R-38c：模型抄来的"静止机位"反向词可能把这一镜的主动作一起禁掉
            # （正文要求原地转身、NEG 写着 no rotation）→ 删掉打架的那一条。
            prompt_text, ws6 = enforce_motion_negs(prompt_text)
            for w in ws + ws2 + ws3 + ws4 + ws7 + ws5 + ws6:
                warnings.append(f"{shot.get('code')}：{w}")
            parts.append((shot, prompt_text, zh))
        else:
            code = shot.get("code")
            parts.append((
                shot,
                f"（本镜生成失败：{state_err(errors.get(idx, '未知'))}；"
                f"可点「重试」只补这一镜）",
                "",
            ))
            warnings.append(f"{code} 生成失败：{state_err(errors.get(idx, '未知'))}")

    source_note = ""
    if any(i not in results for i in range(len(shot_specs))):
        source_note = ("⚠️ 有镜头生成失败，脚本不完整 —— 上表与下列逐镜内容里已就地标注。")
    warnings.extend(_self_check_laws(parts, perfume=perfume))
    # ★R-38：画面级复刻的元素回查（唯一能发现"道具被丢了"的闸门）
    warnings.extend(verify_inherit(parts))
    script = merge_script(
        skeleton, parts, project_name=facts.get("project_name") or "",
        lang=lang, dur=dur, source_note=source_note,
    )
    timings["merge"] = time.time() - t2

    return {
        "script": script,
        "skeleton": skeleton,
        "warnings": warnings,
        "timings": timings,
        "steps": len(shot_specs) + 1,
    }


def state_err(msg: str) -> str:
    """错误串截断（warning 里不该塞整段 traceback）。"""
    return (msg or "").replace("\n", " ")[:180]
