"""Trae Agent 接入：从网页一键让 Trae 用 fragrance-ecom-video 技能生成脚本分镜提示词。

入口在「项目工作台」项目列表每行最左侧的「生成视频提示词」按钮。点击后后端：

  1. 取项目 + 关联商品（卖点事实）+ 主播锚点；
  2. 把 fragrance-ecom-video 技能知识（SKILL.md + references + assets/templates）
     拷进一个**沙箱工作目录**（与项目代码隔离，agent 的 bash/编辑能力被限定在此目录内）；
  3. 写一份 context.md（任务 + 事实 + 输出格式 + **5 份必读资料的全文**）；
  4. 拉起 trae-cli run，让它**直接下笔**产出 15 秒分镜脚本与逐镜 i2v 提示词；
  5. 读回 ./output/script.md，返回给前端弹窗展示。

## ⚡ 为什么资料要内联进 context.md（R-13，实测调优）

Trae 是 agent 循环：让它「自己读这 5 个文件」= 5-7 次**串行** LLM 请求，每步的
输入都带着累积到 2.8 万 token 的历史，而输出只有 50-90 token（纯为决定"下一个
读哪个文件"）。实测 4 次的步数分别是 18 / 9 / 8 / 7 步，且每一步都有撞上中转站
长尾的机会（实测单步 117s、361.7s，以及一次直接挂死不返回）。

这 5 份资料合计 65KB（约 3 万 token），**一次性塞进 prompt 完全放得下** ——
于是「读文件」这段准备动作被整段消灭，agent 只做真正的创作。同时：

  - `MAX_STEPS` 45 → 8（没有读文件动作了，8 步是充裕余量，也能及时止损）；
  - 单轮请求加显式超时（`SCRIPTGEN_LLM_TIMEOUT`），重试从 10 压到 1。
    上游默认不设 timeout，SDK 用 600s 且自己还重试 2 次，与外层重试叠乘，
    一次抖动就能拖成十几分钟且没有任何反馈。

## 其它设计取舍
  - **生成后自动留存为项目素材**（`assets.kind = "script"`，见 services/scripts.py）。
    早期版本只弹窗展示、不入库；但那意味着关掉弹窗脚本就丢了 —— 它承载着镜号、
    时长、念白稿、音效落点，既是这一版成片的**事实源**，也是「一键成片」自动驱动
    TTS 与混音的输入。不入库就得让用户把整篇葡语念白重新敲一遍。
  - 仍然**不触发任何出图/出片动作** —— allow_spend=False，config 里干脆不挂 MCP，
    纯 LLM 生文任务不需要工具接线。
  - 网络：清掉可能失效的本地代理环境变量（与 TTS 同理，否则 LLM 直连易 502）。
  - 超时：1200s（20 分钟）。Trae 是 agent 循环，单个 LLM 调用可能在中转站侧静默卡住数分钟，
    本地应用长请求可接受；且超时后先抢救已落盘的 ./output/script.md（见 _run_trae）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import deps
from ..db.models import Product, Project, ProjectProduct, Talent
from ..db.session import get_db
from ..services.scripts import save_script

router = APIRouter(prefix="/api", tags=["trae"])

BACKEND = Path(__file__).resolve().parents[2]
DEFAULT_TRAE_HOME = Path.home() / ".workbuddy" / "third_party" / "trae-agent" / "trae-agent-main"
TRAE_CLI = (
    Path.home()
    / ".workbuddy"
    / "binaries"
    / "python"
    / "envs"
    / "trae312"
    / "Scripts"
    / "trae-cli.exe"
)
SKILL_DIR = Path.home() / ".workbuddy" / "skills" / "fragrance-ecom-video"
RUNS_DIR = BACKEND / ".trae_runs"
BACKEND_URL = "http://127.0.0.1:8000"
# 实测校准（见 docs/TRAE-AGENT.md「生文任务的步数与超时预算」）：
#  - 单个 LLM 调用可能在中转站侧静默卡住数分钟（实测一次卡 6.5min），
#    540s 会在「读完资料、正要动笔」时把任务杀掉，产出全丢。放宽到 20 分钟。
#  - R-13 起资料改为内联，不再有「自己读 5 个文件」的 7 步准备动作，
#    典型步数降到 1-3 步；单步超时改由 SCRIPTGEN_LLM_TIMEOUT 兜住，
#    所以总超时只是最后一道保险，正常不会再走到这里。
TIMEOUT_SEC = 1200
# 步数上限（防失控）：内联资料后正常只用 3 步（分次写入 1-2 次 + task_done）。
# 从前 45 步是因为要留出读文件的余量。
# R-14 实测把它从 8 收到 5：agent 曾用满 7 步 —— 最后 4 步（step3-6）全是
# "回读文件、微调措辞"，**输出只有 1212 token 却烧掉 47 秒**，因为每一步都要
# 重发此前累积的 5 万 token 历史。提示词里已明确禁止回读；步数上限再兜一道，
# 一旦模型开始反复打磨就立刻止损，而不是白烧几轮请求。
MAX_STEPS = 5
KEEP_RUN_DAYS = 3

# 单次 HTTP 请求超时（秒），经 TRAE_LLM_TIMEOUT 传给 trae-cli 里的 OpenAI client。
# 上游默认不设 → SDK 用 600s，且 SDK 自己还重试 2 次，(600+retry) 叠加起来
# 单步最坏可达 1800s，表现为"页面一直转圈"。这里压到 300s：
# 正常一次"写 3 镜"的请求实测 117-362s，300s 是能覆盖典型值、又不至于让
# 用户干等十几分钟的上界。
SCRIPTGEN_LLM_TIMEOUT = 300
# 外层重试次数（trae-agent 的 `retry_with`：失败后随机 sleep 3-30s 再试一次）。
# 从默认的 10 压到 1：单次请求动辄要吐数千 token，一次抖动就是几分钟，
# 10 次重试只会把「卡住 5 分钟」拖成「卡住 15 分钟以上」，用户全程零反馈；
# 快速失败反而让他能立刻看到明确的错误、马上决定是重来还是换通道。
# ⚠️ 它与 SCRIPTGEN_LLM_TIMEOUT 是**乘算**关系，调大时务必先算总账。
SCRIPTGEN_MAX_RETRIES = 1


# 只用「读文件 / 写文件」两个能力，刻意**不给 bash**：
# 实测 bash 让 agent 把 4 个步数浪费在 `cd` 与 `chcp 65001`（Windows 代码页）上，
# 而且它对纯生文任务毫无必要；去掉后既省步数，也彻底杜绝误碰文件系统。
SCRIPTGEN_TOOLS = ["str_replace_based_edit_tool", "task_done"]
# 同上：`sequentialthinking` 实测吃掉 7/18 步（模型拿它长篇自问自答），
# 纯生文任务不需要，直接不启用。
SCRIPTGEN_MAX_TOKENS_FLOOR = 8192

# ---- Trae 助手（R-19：软件内嵌对话面板，MVP）----
# 每条消息 = 一次独立的 `trae-cli run`（无状态），历史对话内联进 context.md ——
# 与生文链路同一条铁律：「资料内联，别让 agent 自己读文件」。
# 工具同样**不开 bash**（对话场景更没必要，也彻底杜绝任意命令执行）；
# 步数放宽到 10（对话可能要多轮小编辑），其余护栏与生文一致。
CHAT_TOOLS = SCRIPTGEN_TOOLS
CHAT_MAX_STEPS = 10
CHAT_KEEP_TURNS = 8          # 内联的最近对话轮数上限
CHAT_TURN_CHARS = 1500       # 单轮截断
CHAT_HISTORY_CHARS = 8000    # 历史总字符预算（控制每条消息的 token 成本）

# ---- 完整自动化模式（R-21）：**开 bash**，agent 可以执行任意命令 ----
#
# ⚠️ 风险边界（必须让用户在开启前知情，前端有确认弹窗）：
#   1. trae-cli 的 bash 工具 = 直接子进程，以运行后端的当前用户身份执行，
#      **没有任何沙箱隔离**（Windows 上没有 docker 沙箱可用；trae-agent 的
#      sandbox 模式依赖 docker）。
#   2. 「工作目录」只是 agent 的默认 cwd 与提示词软约束 —— bash 里写绝对路径、
#      `cd ..` 都能逃出 RUNS_DIR，理论上可读写本机任何当前用户可及的文件。
#   3. bash 能发起网络请求 → 绕过 allow_spend=False 的 MCP 预算闸门（那只管
#      MCP 工具，管不了 shell 里的 curl/python）。
#   4. run 是一次性跑完，没有逐步人工确认；步数上限是唯一的失控兜底。
# 缓解措施（能做到的都做了，但都是软约束，见 auto 模式 context 的安全纪律段）：
#   - 独立配置文件 `trae_config.auto.yaml`（不污染手动维护的 trae_config.yaml）；
#   - 步数上限 25、总超时沿用 TIMEOUT_SEC、每次运行独立沙箱目录、3 天清理；
#   - 提示词明确禁止：出工作目录、联网下载、改后端代码/数据库/供应商配置。
AUTO_TOOLS = ["bash", "str_replace_based_edit_tool", "task_done"]
AUTO_MAX_STEPS = 25

# 内联进 context.md 的必读资料（顺序即阅读顺序）。**这是 R-13 提速改造的核心。**
#
# 从前是让 agent「自己读这 5 个文件」—— 那意味着 5-7 次串行 LLM 请求：
# 每次输入带着累积到 2.8 万 token 的历史、输出只有 50-90 token（纯粹在决定
# "下一个读哪个文件"），而且每一次都可能撞上中转站的长尾（实测单步
# 117s / 362s / 直接挂死）。这 5 份加起来才 65KB（中文约 3 万 token），
# **一次塞进 prompt 完全放得下**，于是读文件这一整段准备动作就被消灭了。
INLINE_DOCS = [
    ("SKILL.md", "角色定位、工作流路由、工作流 B 的产出物与铁律"),
    ("references/storyboard-15s.md", "15 秒分镜结构范式"),
    ("references/prompt-layering.md",
     "i2v 提示词 v2 模板（[SHOT][HERO][MOTION][CAMERA][LIGHT][PHYSICS][TEXTURE][REF][NEG]）"),
    ("references/copywriting-style.md", "念白五段式，禁止说明文 / 叫卖腔"),
    ("references/visual-storytelling.md",
     "叙事弧与卖点可视化（用户以参考片定标：不要分镜堆砌，要有美感地展示卖点与感觉）"),
    ("assets/templates/storyboard-template.md", "输出骨架"),
]

# 单次写入的建议上限：一次吐太多 token 会让请求变得又慢又容易卡。
# 实测「一次写完 3 镜」= 5965-7053 output tokens / 117-362s；
# 5 镜一次写完必然撞上 max_tokens=8192 被截断。所以按镜头数分批。
INLINE_MAX_SHOTS_PER_WRITE = 3



class GenerateScriptIn(BaseModel):
    # 注意：project_id 来自 URL 路径（/api/projects/{project_id}/generate-script），
    # **不再从 body 取** —— 从前端重复传反而容易漏传导致 422（该字段后端从不读取）。
    # 以下均可选：用户在弹窗里也能微调，不传就取项目默认值
    language: Optional[str] = None
    shots: Optional[int] = None
    duration_sec: Optional[float] = None
    instruction: Optional[str] = ""  # 用户追加的额外要求（如「走夜戏」「双型号同框」）
    # 生成方式（R-14）：
    #   "parallel"（默认）骨架 → 逐镜并发 → 合并，实测 240-322s → 约 110s；
    #   "agent"    旧链路，Trae agent 单会话串行写 5 镜，保留作质量对照与兜底。
    mode: Optional[str] = None
    # R-32 爆款复刻（工作流 C）：传一份反推报告 id，则本片按该参考片的
    # 镜头序列/节奏/运镜/光影复刻，只替换人物与商品（骨架与逐镜都吃它）。
    remake_report_id: Optional[int] = None


# ------------------------------------------------------------------ 取事实


def _gather_facts(project_id: int, db: Session) -> dict:
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")
    links = db.query(ProjectProduct).filter(ProjectProduct.project_id == project_id).all()
    products = []
    for lk in links:
        prod = db.get(Product, lk.product_id)
        if prod is None:
            continue
        products.append({
            "code": prod.code,
            "name": prod.name,
            "category": (prod.category or "other").lower(),
            "description": prod.description or "",
            "brand": prod.brand or "",
            "price": prod.price or "",
            "target_audience": prod.target_audience or "",
            "role": lk.role or "",
        })
    talent = db.get(Talent, p.talent_id) if p.talent_id else None
    talent_blob = {
        "code": talent.code if talent else None,
        "name": talent.name if talent else None,
        "appearance": (talent.appearance or "") if talent else "",
        "spec": (talent.spec or {}) if talent else {},
    }
    # R-28：最新反推报告的三档结论注入生文上下文（采纳/改造/不可采纳）——
    # 用户拆完参考片即用，不用等人工蒸馏进规则。没有报告就给空串（不注入）。
    from ..services.reverse import latest_findings_text  # noqa: PLC0415
    return {
        "project_name": p.name,
        "language": p.language,
        "products": products,
        "talent": talent_blob,
        "_reverse_text": latest_findings_text(db),
    }


def _attach_visuals(facts: dict, db: Session, project_id: int) -> List[str]:
    """生成脚本前对项目商品做图识别（R-20），把「商品图解读」注入 facts。

    - 有缓存（image_meta 带 desc）→ 直接复用，**不花钱**；
    - 没缓存 → 调一次视觉模型识别并落库（此后图片不变就一直复用）；
    - 识别失败/没配视觉模型 → 只给 warnings，**不阻断生成**（解读缺位时
      模型仍按商品文字事实写，与「图只影响画面不影响文案」的旧行为一致）。

    返回 warnings；识别文本同时写进每个 product dict 的 `_visual` 键
    （`visual_context_text` 消费）。
    """
    from ..services.product_vision import (  # noqa: PLC0415
        ensure_product_visuals, visual_context_text,
    )
    links = (db.query(ProjectProduct)
             .filter(ProjectProduct.project_id == project_id).all())
    prods = [db.get(Product, lk.product_id) for lk in links]
    prods = [x for x in prods if x is not None]
    if not prods:
        return []
    visuals, warnings = ensure_product_visuals(db, prods)
    text = visual_context_text(visuals)
    # 按 code 映射回 facts（visual_context_text 的输出是整体文本块，
    # 并发链路直接整块注入骨架消息；agent 链路同样整块注入 context.md）
    facts["_visual_text"] = text
    return warnings


# ------------------------------------------------------------------ 沙箱准备


def _copy_skill(workdir: Path) -> None:
    """把技能知识拷进沙箱（保留 SKILL.md 的相对引用：references/ 与 assets/templates/）。

    R-13 起这些文件**不再需要 agent 去读**了（正文会由 `_read_inline_docs`
    整份内联进 context.md）。保留拷贝有两个用途：
      ① `_read_inline_docs` 就读这份副本，内联的内容与沙箱里的文件永远同一份，
         不会出现"提示词是旧版、目录是新版"的错位；
      ② 出问题时可以按 workdir 直接人工核对现场。

    不拷 scripts/ —— 那是出片脚本，生文任务用不到；避免 agent 误跑去执行。
    """
    dst = workdir / "skill"
    dst.mkdir(parents=True, exist_ok=True)
    if (SKILL_DIR / "SKILL.md").exists():
        shutil.copy(SKILL_DIR / "SKILL.md", dst / "SKILL.md")
    for sub in ("references", "assets"):
        src = SKILL_DIR / sub
        if src.is_dir():
            shutil.copytree(src, dst / sub, dirs_exist_ok=True)


def _read_inline_docs(workdir: Path) -> str:
    """把 5 份必读资料的正文读出来，拼成可直接内联进 context.md 的文本。

    读的是**沙箱里刚拷好的副本**（`_copy_skill` 已先跑），而不是 SKILL_DIR ——
    这样 context.md 的内容与 agent 眼里的 `./skill/` 永远是同一份，不会出现
    "提示词里是旧版、目录里是新版"的错位。

    单份缺失时明确写出「缺失」而不是静默跳过：静默跳过会让提示词少一份
    铁律，而模型照样会写出一篇看起来很合理的脚本 —— 这种缺料最难发现。
    """
    parts: list[str] = []
    for rel, why in INLINE_DOCS:
        p = workdir / "skill" / rel
        if not p.exists():
            parts.append(f"### 【{rel}】\n\n（⚠️ 该资料缺失，请勿臆造其中内容）\n")
            continue
        body = p.read_text(encoding="utf-8", errors="replace").strip()
        parts.append(f"### 【{rel}】（{why}）\n\n{body}\n")
    return "\n---\n\n".join(parts)


def _write_context(workdir: Path, facts: dict, body: GenerateScriptIn) -> None:
    lang = body.language or facts["language"]
    shots = body.shots or 5
    dur = body.duration_sec or 15.0

    prod_lines = []
    for pr in facts["products"]:
        block = f"- [{pr['code']}] {pr['name']}（品类：{pr['category']}，角色：{pr['role'] or '主推'}）"
        if pr["brand"]:
            block += f"\n  品牌：{pr['brand']}"
        if pr["price"]:
            block += f"\n  价格：{pr['price']}"
        if pr["target_audience"]:
            block += f"\n  目标人群：{pr['target_audience']}"
        if pr["description"]:
            block += (
                "\n  卖点描述（**事实来源，必须据此写，不得编造香调/功效/成分**）：\n"
                f"  {pr['description']}"
            )
        else:
            block += "\n  （未填卖点描述：请仅基于名称/品类做合理视觉化，不要编造香调事实）"
        prod_lines.append(block)
    products_text = "\n".join(prod_lines) if prod_lines else "（本项目未关联商品）"

    # 品类自适应（R-17）：非香水项目不许硬造喷雾/开盖动作 —— 与并发链路的
    # 铁律拆分共用同一个判定（单一真相，不另立一份香水词表）。
    from ..services.scriptgen import is_perfume_facts  # noqa: PLC0415
    perfume = is_perfume_facts(facts)
    category_note = (
        "- 品类判定：香水/香氛类 —— 技能里的喷香水镜、开盖状态、瓶盖不入画等铁律全部适用；"
        "喷雾落点三种写法都合法、按叙事自选不锁死（A 近距落肤：颈侧/耳后或手腕，"
        "雾锥几厘米即散、只留极淡光泽；B 喷空中＋人物原地转圈走入光束里的薄透汽雾，"
        "须中景约 50mm＋主体背后垂直灯板＋硬逆光穿雾，汽雾薄透不遮脸；"
        "C 其他因果闭环的自然写法）；同片两镜以上喷雾时落点至少变化一次（技能铁律 27）"
        if perfume else
        "- 品类判定：非香水类 —— **不要硬造喷雾/开盖动作**，喷雾相关铁律不适用；"
        "把该品类的核心使用动作（以卖点描述为准）当作全片至少一镜的核心动作镜"
    )

    talent = facts["talent"]
    if talent["appearance"]:
        talent_text = (
            "主播锚点（跨镜逐字复用的 [REF]，写在每镜 i2v 提示词的 [REF] 锚点里）：\n"
            f"{talent['appearance']}"
        )
    else:
        # appearance 为空但已选主播：用必填资料（年龄/性别/国籍）+ spec 拼事实锚点。
        # 绝不让模型自行设计 —— 实测它编出的外貌与主播参考图对不上（R-16）。
        from ..services.scriptgen import talent_anchor_text  # noqa: PLC0415
        derived = talent_anchor_text(talent)
        if derived:
            talent_text = (
                "主播锚点（跨镜逐字复用的 [REF]；由主播资料自动生成 —— 只可照用，"
                f"禁止自行修改外貌/族裔/年龄）：\n{derived}"
            )
        else:
            talent_text = (
                "（未关联主播；如脚本需要人物，请基于商品基调自行设计一位具体人物，"
                "并给出可跨镜复用的外貌锚点）"
            )

    docs = _read_inline_docs(workdir)
    # R-20：商品图解读（视觉识别结果，含缓存）整块注入 —— 图影响画面与外观
    # 文案；其中的事实纪律（只认图中可见、禁推断香调）由 visual_context_text 自带。
    visual_text = (facts.get("_visual_text") or "").strip()
    visual_block = f"\n{visual_text}\n" if visual_text else ""
    # R-28：最新反推报告三档结论注入 —— 拆完参考片即用，不必等人工蒸馏。
    reverse_text = (facts.get("_reverse_text") or "").strip()
    reverse_block = (
        f"\n## 参考片反推结论（最新一份真实参考片的拆解判定 —— 采纳项直接体现进"
        f"叙事弧与分镜；改造项按其中写法要求执行；不可采纳项禁止照抄）\n"
        f"{reverse_text}\n" if reverse_text else ""
    )
    ctx = f"""# 任务：用 fragrance-ecom-video 技能生成 {dur:.0f} 秒带货视频脚本与逐镜 i2v 提示词

你是本工作室的导演 Agent。请**严格遵循**下面【资料：SKILL.md】里的
「工作流 B：15 秒带货视频分镜脚本」。

## 工作纪律（重要 —— 直接决定这次生成要 3 分钟还是 6 分钟）
- **资料已全部内联在本文末尾（共 {len(INLINE_DOCS)} 份），不要再读任何文件。**
  `./skill/` 目录虽然还在（供人工核对），但**不要去 `view` / `ls` 它**：
  每多读一个文件就多一轮 LLM 请求（单轮请求超时是 {SCRIPTGEN_LLM_TIMEOUT} 秒）。
- **不要长篇自问自答**。想清楚结构就直接下笔，产出才是交付物。
- **分次落盘，单次别写太多**（单次输出越长，请求越慢、越容易卡住）：
  - 镜头数 ≤ {INLINE_MAX_SHOTS_PER_WRITE}：一次性写完 `./output/script.md`；
  - 镜头更多：先写「标题头 + 分镜表 + 前 {INLINE_MAX_SHOTS_PER_WRITE} 镜」，
    之后**追加**剩余镜头，每次追加不超过 {INLINE_MAX_SHOTS_PER_WRITE} 镜。
  总计写入 2 次即可，**既不要一镜一次，也不要攒到最后一次性输出**。
- **写完就结束，绝不回头检查**：最后一镜落盘后**立刻**调 `task_done`，然后收工。
  不要 `view` 读回自己刚写的文件、不要"微调一下措辞"、不要"再确认一遍"。
  实测每多一次这类动作就多一轮完整请求（**约 40 秒**，且输入要把此前全部
  5 万 token 的历史重发一遍），而它对最终交付毫无贡献 ——
  正确性由你在下笔时保证，不靠事后回读。

## 项目事实（只据此写，不要编造未给出的香调 / 功效 / 成分）
- 项目名称：{facts['project_name']}
- 投放语言（决定念白语言与文案口吻）：{lang}
- 目标时长：{dur:.0f} 秒，目标镜头数：{shots} 镜
{category_note}

### 商品
{products_text}
{visual_block}
### 主播
{talent_text}
{reverse_block}
## 输出要求
1. 产出 **{shots} 镜** 的分镜脚本，每镜包含：
   - 镜号 / 时长（秒）/ 景别 / 运镜（量化，如 about 8% of the frame width）/ 画面动作 / 光线
   - **i2v 提示词（英文）**：严格按 v2 模板顺序，i2v 锚点 ≤25 词，含 [REF] 主播锚点、[NEG] 反向约束
   - **念白文案**：按投放语言 {lang} 写，五段式，禁止说明文与叫卖腔；短镜窗口只放少量词
2. 遵守技能里的「一致性铁律」：单镜内不出现物体凭空出现 / 消失；喷雾要喷到真实落点；因果自洽。
3. **不要触发任何出图 / 出片动作**，只产出文本脚本与提示词。
{('- 用户附加要求：' + body.instruction) if body.instruction else ''}

## 交付
把最终脚本写入 `./output/script.md`（用 storyboard-template 的结构），
完成后调 `task_done`，并用中文简短说明关键决策（如白天 / 夜里的判断依据）。


# ============ 参考资料（共 {len(INLINE_DOCS)} 份，全文已内联，**不要再读文件**）============

{docs}
"""
    (workdir / "context.md").write_text(ctx, encoding="utf-8")


# ------------------------------------------------------------------ 启动 Trae


def _ensure_config(
    cfg_name: str = "trae_config.scriptgen.yaml",
    *,
    tools: Optional[list] = None,
    max_steps: Optional[int] = None,
) -> Path:
    """生成（或复用）trae 配置，但**去掉 MCP 接线**（本任务只用 LLM）。

    复用 gen_trae_config.build_config 保证 provider/model 结构与手动生成一致，
    随后 pop 掉 mcp 相关键 —— 纯 LLM 任务不需要工具，也避免 MCP server 启动负担与花钱风险。
    `cfg_name` / `tools` / `max_steps` 供助手对话等场景生成**独立配置文件**，
    绝不覆盖用户手动维护的 trae_config.yaml（那份可能挂着 MCP 工具）。
    """
    # 单独写一份「纯生文」配置，绝不覆盖用户手动维护的 trae_config.yaml
    # （那份可能挂着 MCP 工具，用于手动让 agent 跑完整流水线）。
    cfg_path = DEFAULT_TRAE_HOME / cfg_name
    try:
        prov_cfg = deps.get_providers_config()
        prov = prov_cfg.active_llm_config()
        keys = prov.resolve_keys()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"读取文本大模型供应商配置失败：{type(e).__name__}: {e}"
        ) from e
    if not keys:
        raise HTTPException(
            status_code=400,
            detail=(
                "未配置文本大模型密钥。请先在「供应商设置」里为文本大模型填密钥"
                "（或在 providers.yaml 配置 active llm，或设置 OPENAI_API_KEY 环境变量）。"
            ),
        )

    sys.path.insert(0, str(BACKEND))
    from scripts.gen_trae_config import build_config  # noqa: PLC0415
    import yaml  # noqa: PLC0415

    doc = build_config(
        api_key=keys[0],
        base_url=prov.base_url,
        model=prov.model,
        temperature=prov.temperature,
        # 一份「5 镜分镜 + 每镜 i2v 提示词 + 念白」的产出动辄数千 token，
        # 供应商若只给 4096 会被截断，这里抬到一个下限。
        max_tokens=max(SCRIPTGEN_MAX_TOKENS_FLOOR, prov.max_tokens or 0),
        tools=tools if tools is not None else SCRIPTGEN_TOOLS,
        max_steps=max_steps if max_steps is not None else MAX_STEPS,
        max_retries=SCRIPTGEN_MAX_RETRIES,
        mcp_python=str(BACKEND / "mcp_server.py"),  # 占位：下方会被 pop 掉
        mcp_script=str(BACKEND / "mcp_server.py"),
        backend_url=BACKEND_URL,
        allow_spend=False,
    )
    doc.pop("allow_mcp_servers", None)
    doc.pop("mcp_servers", None)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return cfg_path


def _prune_old_runs() -> None:
    """清理 3 天前的沙箱运行目录（仅限受控的 .trae_runs，删的是本工具自己写的）。"""
    if not RUNS_DIR.is_dir():
        return
    cutoff = datetime.utcnow() - timedelta(days=KEEP_RUN_DAYS)
    for d in RUNS_DIR.iterdir():
        if d.is_dir():
            try:
                if datetime.utcfromtimestamp(d.stat().st_mtime) < cutoff:
                    shutil.rmtree(d)
            except OSError:
                pass


async def _run_trae(workdir: Path, config_path: Path,
                    *, output_rel: str = "output/script.md") -> dict:
    if not TRAE_CLI.exists():
        raise HTTPException(
            status_code=500,
            detail=(
                f"找不到 trae-cli：{TRAE_CLI}。请按 docs/TRAE-AGENT.md 完成 Trae 安装"
                "（源码装到 trae312 venv，确保 Scripts/trae-cli.exe 存在）。"
            ),
        )

    # 用 --file 传任务描述，避免 Windows 命令行里塞一长串中文被转义搞坏
    args = [
        str(TRAE_CLI), "run",
        "--file", str(workdir / "context.md"),
        "--config-file", str(config_path),
        "--working-dir", str(workdir),
    ]
    env = dict(os.environ)
    # 清掉可能干扰 LLM 直连的代理（本机代理常失效导致 502，与 TTS 同理）
    for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 单轮请求的超时与 SDK 层重试次数（见 SCRIPTGEN_LLM_TIMEOUT 的注释）。
    # 上游 doubao provider 不传这两个值 → SDK 用默认 timeout 600s 且自己重试 2 次；
    # 再叠上本任务配置里的 max_retries，一次网络抖动就能放大成十几分钟，
    # 而用户全程看不到任何反馈。这里把 SDK 层重试关掉（统一交给外层），
    # 并给单轮请求一个明确上界。
    env["TRAE_LLM_TIMEOUT"] = str(SCRIPTGEN_LLM_TIMEOUT)
    env["TRAE_LLM_SDK_RETRIES"] = "0"
    # 开流式。**这是 R-13 最关键的一步**：中转站前面挡着 Cloudflare，其
    # Proxy Read Timeout = 120s，非流式请求必须在这之内把整段响应生成完，
    # 否则直接吃一个 524（实测「输出 5919 token 用 116s」踩线通过，下一轮
    # 追加镜头就 524）。流式下分片持续流动，CF 不会判定超时，长输出才成为可能。
    # 单步超时在流式下的含义也随之变成「两个分片之间的最大间隔」，而不是总时长。
    env["TRAE_LLM_STREAM"] = "1"

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(workdir),
        )
    except FileNotFoundError as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"启动 trae-cli 失败：{e}") from e

    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_SEC)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        await proc.wait()
        stdout, stderr = b"", b""

    out = (stdout or b"").decode(errors="replace")
    err = (stderr or b"").decode(errors="replace")

    # 先抢救盘上已有产出：即便超时 / 非零退出，Trae 已经落盘的部分也不能丢
    # （因此 prompt 里要求它「边写边落盘、逐镜追加」而不是最后一次性输出）。
    out_path = workdir / output_rel
    script = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
    script = script.strip()
    if not script:
        # 兜底：stdout 里抠正文（产出文件没写成时至少别把回答全丢了）
        script = out.strip()

    log = (out + "\n\n--- stderr ---\n" + err)[-4000:]
    log += f"\n\n--- 运行目录 ---\n{workdir}"

    # 一点产出都没有，才真的算失败
    if not script.strip():
        if timed_out:
            raise HTTPException(
                status_code=504,
                detail=(
                    f"Trae 在 {TIMEOUT_SEC}s 内未结束，且没有任何产出落盘"
                    f"（可能卡在 LLM 调用）。详细日志见沙箱目录：{workdir}"
                ),
            )
        raise HTTPException(
            status_code=500,
            detail=(
                f"Trae 退出码 {proc.returncode}，且没有任何产出落盘。\n"
                f"--- stderr 前 2000 字符 ---\n{err[:2000]}\n"
                f"--- stdout 前 1000 字符 ---\n{out[:1000]}"
            ),
        )

    warning = ""
    if timed_out:
        warning = (
            f"Trae 在 {TIMEOUT_SEC}s 内未结束，已强制中止。下面是它**已落盘的部分产出**，"
            f"请自行核对完整性（缺的镜次可再点一次生成）。运行目录：{workdir}"
        )
    elif proc.returncode != 0:
        warning = (
            f"Trae 退出码 {proc.returncode}。下面是已落盘的部分产出，请核对完整性。"
            f"运行目录：{workdir}"
        )

    return {
        "script": script,
        "log": log,
        "workdir": str(workdir),
        "warning": warning,
        "timed_out": timed_out,
    }


# ------------------------------------------------------------------ 路由


def _attach_remake(facts: dict, report_id: int, db: Session) -> list:
    """把一份反推报告挂成**复刻基准**（R-32，技能工作流 C）。

    只从报告里取"前四步的产物"：逐镜序列（含每镜的景别/运镜/光影/画面描述）、
    有效性归因、保留方式四档、原片反推提示词、三档判定。**不在后端改写任何
    东西** —— 保留/替换的决策交给骨架 prompt 里的复刻纪律（见
    `scriptgen.remake_block_text`），这样"复刻基准"只有一处真相。

    返回 warning 列表（不阻断）：纯数据反推的报告没有逐镜提示词，只能复刻
    结构与节奏，要明确告诉用户。
    """
    from ..db.models import ReverseReport  # noqa: PLC0415 - 避免顶部导入变重

    row = db.get(ReverseReport, report_id)
    if row is None:
        raise HTTPException(404, f"反推报告不存在：{report_id}")
    st = row.structure_json or {}
    shots = st.get("shots") or []
    if not shots:
        raise HTTPException(
            400,
            "这份反推报告没有逐镜拆解，不能作为复刻基准。"
            "请先在反推工作台对参考片重新反推（看图模式会产出逐镜提示词）。",
        )

    # 原片提示词可能很长（一镜七层），逐镜展开阶段要用它比对运镜/光影写法；
    # 但骨架 prompt 预算有限，单镜截断，避免把商品事实与铁律挤出上下文。
    trimmed = []
    for s in shots:
        if not isinstance(s, dict):
            continue
        it = dict(s)
        p = str(it.get("prompt") or "")
        if len(p) > 1200:
            it["prompt"] = p[:1200] + " …（原片提示词过长，已截断）"
        trimmed.append(it)

    facts["remake"] = {
        "report_id": row.id,
        "source": Path(row.source_path).name if row.source_path else "",
        "duration": (row.spec_json or {}).get("duration"),
        "vision": bool(st.get("vision")),
        "shots": trimmed,
        "findings_text": row.findings or "",
    }
    warns = []
    if not st.get("vision"):
        warns.append(
            f"报告 #{row.id} 是纯数据反推（无逐镜提示词）：本次复刻只沿用它的"
            "镜头结构与节奏，**运镜/光影的具体写法没有原片比对**，建议人工核对")
    return warns


@router.post("/projects/{project_id}/generate-script")
async def generate_script(
    project_id: int, body: GenerateScriptIn, db: Session = Depends(get_db)
):
    """生成 15s 分镜脚本与逐镜 i2v 提示词。

    默认走 **并发链路**（R-14）：骨架（1 轮）→ 逐镜并发（N 路）→ 合并。

    为什么默认换成并发：原链路是 Trae agent 单会话**串行**写 5 镜，而实测
    耗时几乎只由「输出 token ÷ 通道吞吐」决定（本通道所有文本模型都只有
    40-90 token/s，横评 5 个模型无差别）。串行写还额外背了三笔开销：每次写入
    要把整份文件塞进工具调用参数、每轮重发累积历史、以及 agent 顺手回读文件
    微调（实测白花 47 秒）。并发把这段串行摊平到 N 路，实测约 110s。

    原链路保留为 `mode="agent"`：它的优势是"一个会话通盘构思"，适合与并发
    产出做质量对照，也可能在需要跨镜强连贯时更合适。
    """
    facts = _gather_facts(project_id, db)
    # R-20：生成脚本前先做商品图识别（有缓存不花钱），解读注入两条链路的上下文。
    # 识别失败只记 warning，不阻断生成。
    visual_warnings = _attach_visuals(facts, db, project_id)
    # R-32：爆款复刻 —— 把反推报告挂成复刻基准（骨架与逐镜都吃它）。
    remake_warnings = []
    if body.remake_report_id:
        remake_warnings = _attach_remake(facts, body.remake_report_id, db)
    warnings = list(visual_warnings) + list(remake_warnings)
    mode = (body.mode or "parallel").strip().lower()
    if mode == "agent":
        result = await _generate_with_agent(project_id, body, facts, db)
    else:
        result = await _generate_parallel(project_id, body, facts, db)
    if warnings:
        result["warning"] = ((result.get("warning") or "")
                             + ("\n" if result.get("warning") else "")
                             + "；".join(warnings)).strip()
    return result


async def _generate_with_agent(
    project_id: int, body: GenerateScriptIn, facts: dict, db: Session
) -> dict:
    """旧链路：Trae agent 单会话串行产出（保留，不删）。"""
    config_path = _ensure_config()
    _prune_old_runs()
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    workdir = RUNS_DIR / f"{ts}-p{project_id}"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "output").mkdir(parents=True, exist_ok=True)
    _copy_skill(workdir)
    _write_context(workdir, facts, body)
    result = await _run_trae(workdir, config_path)
    result["mode"] = "agent"

    # 脚本留存为项目素材。**入库失败不该让生成失败** —— 用户已经等了几分钟
    # 拿到文本，那才是主产物；素材登记只是记账，降级成一条 warning。
    try:
        asset = save_script(
            db, project_id=project_id, script=result.get("script") or "",
            source="agent", language=facts.get("language") or "",
            extra_meta={"workdir": str(workdir), "shots_requested": body.shots,
                        "duration_requested": body.duration_sec},
        )
        result["script_asset_id"] = asset.id
    except Exception as e:  # noqa: BLE001
        result["script_asset_id"] = None
        result["warning"] = ((result.get("warning") or "")
                             + f"\n[脚本素材留存失败] {type(e).__name__}: {e}").strip()
    return result


async def _generate_parallel(
    project_id: int, body: GenerateScriptIn, facts: dict, db: Session
) -> dict:
    """并发链路（R-14）：骨架 → 逐镜并发 → 合并。

    并发用线程池 + 同步的 `LLMProvider.complete()`：该实现是 urllib 阻塞调用，
    没有异步版本，而这条路本来就是 I/O 等待（等模型出字），线程池正好。
    通道并发能力已实测：3 路并发总墙钟 19.2s ≈ 单个 16.1s，无 429。
    """
    from ..services import scriptgen  # noqa: PLC0415 - 避免与 api 层循环导入

    llm = deps.get_active_llm()
    lang = body.language or facts.get("language") or "pt-BR"
    shots = body.shots or 5
    dur = body.duration_sec or 15.0

    _prune_old_runs()
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    workdir = RUNS_DIR / f"{ts}-p{project_id}-par"
    (workdir / "output").mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    try:
        res = await asyncio.to_thread(
            scriptgen.generate_parallel, facts, llm=llm, lang=lang,
            shots=shots, dur=dur, instruction=body.instruction or "",
        )
    except Exception as e:  # noqa: BLE001 - 原样告诉用户，别包装成"未知错误"
        raise HTTPException(
            status_code=502,
            detail=(f"并发生成失败：{type(e).__name__}: {e}\n"
                    f"（骨架阶段就失败时通常意味着文本通道不可用；"
                    f"运行目录：{workdir}）"),
        ) from e

    elapsed = time.time() - t0
    script = res.get("script") or ""
    (workdir / "output" / "script.md").write_text(script, encoding="utf-8")
    (workdir / "skeleton.json").write_text(
        json.dumps(res.get("skeleton") or {}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    timings = res.get("timings") or {}
    warnings = list(res.get("warnings") or [])
    log_lines = [
        f"模式：并发（骨架 1 轮 + 逐镜 {shots} 路展开）",
        f"总耗时：{elapsed:.1f}s",
        "分段耗时：" + "、".join(f"{k} {v:.1f}s" for k, v in timings.items()),
        f"骨架镜数：{len((res.get('skeleton') or {}).get('shots') or [])}",
    ]
    if warnings:
        log_lines.append("警告：" + "；".join(warnings))
    log_lines.append(f"运行目录：{workdir}")

    result: dict = {
        "script": script,
        # 骨架回传：复刻项目要在界面上显示每镜的 retention / 对应原片镜号（R-32）
        "skeleton": res.get("skeleton") or {},
        "log": "\n".join(log_lines),
        "workdir": str(workdir),
        "warning": "；".join(warnings),
        "timed_out": False,
        "mode": "parallel",
        "elapsed_sec": round(elapsed, 1),
        "timings": {k: round(v, 1) for k, v in timings.items()},
    }

    try:
        asset = save_script(
            db, project_id=project_id, script=script, source="parallel",
            language=lang,
            extra_meta={"workdir": str(workdir), "shots_requested": shots,
                        "duration_requested": dur, "mode": "parallel",
                        # 复刻项目留个溯源锚点：这份脚本是从哪份反推报告复刻来的
                        "remake_report_id": getattr(body, "remake_report_id", None),
                        "timings": result["timings"]},
        )
        result["script_asset_id"] = asset.id
    except Exception as e:  # noqa: BLE001
        result["script_asset_id"] = None
        result["warning"] = ((result.get("warning") or "")
                             + f"\n[脚本素材留存失败] {type(e).__name__}: {e}").strip()
    return result


# ------------------------------------------------------------------ Trae 助手（R-19）


class ChatTurn(BaseModel):
    """一条历史消息。role 只认 user / assistant（服务端会过滤）。"""

    role: str = "user"
    content: str = ""


class ChatIn(BaseModel):
    """助手对话入参。**无状态**：历史由前端带着，服务端内联进任务文件。

    `project_id` 传了就把项目上下文（名称 + 商品摘要）带给 agent；
    不传也能聊（工作台等全局页面的通用咨询）。
    `auto=True` 进入**完整自动化模式**（R-21）：额外开 bash 工具，agent 可
    执行任意命令（写代码/跑脚本/整理文件等）。风险边界见 AUTO_TOOLS 注释；
    前端开启时必须先弹风险确认。
    """

    project_id: Optional[int] = None
    message: str
    history: List[ChatTurn] = Field(default_factory=list)
    auto: bool = False


def _chat_project_context(project_id: Optional[int], db: Session) -> str:
    """给对话带一小段项目上下文（保持简短 —— 对话消息的 token 成本按次付）。"""
    if not project_id:
        return ""
    proj = db.get(Project, project_id)
    if proj is None:
        return ""
    lines = [f"- 项目名称：{proj.name}"]
    try:
        links = (
            db.query(ProjectProduct)
            .filter(ProjectProduct.project_id == project_id)
            .all()
        )
        for lk in links[:3]:
            p = db.get(Product, lk.product_id)
            if p is None:
                continue
            desc = (p.description or "").strip()
            if len(desc) > 80:
                desc = desc[:80] + "…"
            lines.append(f"- 商品 {p.code}：{p.name}" + (f"（{desc}）" if desc else ""))
    except Exception:  # noqa: BLE001 - 上下文取不到就空着，不影响对话
        pass
    return "## 当前项目上下文\n" + "\n".join(lines) + "\n"


@router.post("/trae/chat")
async def trae_chat(body: ChatIn, db: Session = Depends(get_db)):
    """内嵌 Trae 助手：一条消息 = 一次独立的 agent 运行（MVP，无状态）。

    - 历史**内联**进 context.md（与生文链路同一条铁律：别让 agent 自己读文件）；
    - 工具只开 `str_replace_based_edit_tool` + `task_done`，**不开 bash**；
    - 工作目录 = 本次消息的独立沙箱（`RUNS_DIR/chat_*`，3 天后自动清理）；
    - 最终回答要求写入 `output/reply.md`，没写就用 stdout 兜底。

    消耗：按 token 走文本大模型通道计费（与生文同通道同模型）。
    `auto=True`（R-21）时额外开 bash 工具 —— agent 可执行任意命令，
    **没有沙箱隔离**，前端必须在发送前让用户确认风险。
    """
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(400, "消息不能为空")
    if len(msg) > 8000:
        raise HTTPException(400, "单条消息过长（上限 8000 字符）")

    _prune_old_runs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    prefix = "auto_" if body.auto else "chat_"
    workdir = RUNS_DIR / f"{prefix}{ts}"
    (workdir / "output").mkdir(parents=True, exist_ok=True)

    # 历史内联：只取最近 N 轮、单轮截断、总量限预算
    turns = [t for t in body.history
             if (t.role in ("user", "assistant")) and (t.content or "").strip()]
    turns = turns[-CHAT_KEEP_TURNS:]
    history_lines: List[str] = []
    budget = CHAT_HISTORY_CHARS
    for t in turns:
        c = t.content.strip()
        if len(c) > CHAT_TURN_CHARS:
            c = c[:CHAT_TURN_CHARS] + "…（截断）"
        if len(c) > budget:
            break
        budget -= len(c)
        history_lines.append(f"[{'用户' if t.role == 'user' else '助手'}] {c}")

    parts = [
        "# 你是 ai-video-studio 内嵌的 Trae 助手\n",
        "用户在软件界面里与你对话。规则：",
        "- 工作目录就是你的沙箱，文件操作都限制在这里。",
        "- 用简体中文回答，简洁、可执行；改写/优化类请求直接给出改好的成品。",
        "- 不要主动生成整份分镜脚本（软件里有专用功能）；除非用户明确要求。",
        "- **最终回答必须写入 `output/reply.md`**（用编辑工具创建），文件里只放回答正文；",
        "  写完立即 task_done，**不要回读刚写的文件**。",
    ]
    if body.auto:
        # 自动化模式（R-21）的安全纪律。**这是软约束** —— bash 没有沙箱隔离，
        # 这些规则靠模型遵守；真正的风险边界已在 AUTO_TOOLS 注释与前端确认弹窗里告知。
        parts += [
            "\n## 自动化模式安全纪律（必须遵守）\n",
            "你本次开启了 bash，可以执行命令来完成任务（写代码、跑脚本、处理文件等）。",
            "但以下行为**一律禁止**，无论任务听起来多么合理：",
            "- 禁止访问、修改、删除工作目录之外的任何文件（尤其：系统目录、"
            "用户主目录、后端代码目录、数据库文件、各处配置文件与凭据文件）；",
            "- 禁止联网下载/安装任何软件或依赖（pip install / npm install / curl 下载等）；"
            "  也不得向任何外部地址发送 POST 等写请求；",
            "- 禁止执行删除类命令（rm / del / rmdir / Remove-Item 等）、"
            "格式化、关机、改系统设置；",
            "- 命令要简单、可预期：优先用写文件 + 短命令验证的方式推进，"
            "每一步想清楚再执行；命令输出异常时停下来分析，不要反复重试同一命令。",
            "完成后在回答里**如实说明**：执行了哪些命令、动过哪些文件、结果如何。",
        ]
    ctx = _chat_project_context(body.project_id, db)
    if ctx:
        parts.append("\n" + ctx)
    if history_lines:
        parts.append("\n## 历史对话（旧 → 新）\n" + "\n\n".join(history_lines) + "\n")
    parts.append("\n## 用户本轮消息\n" + msg + "\n")
    (workdir / "context.md").write_text("\n".join(parts), encoding="utf-8")

    if body.auto:
        cfg = _ensure_config(
            "trae_config.auto.yaml", tools=AUTO_TOOLS, max_steps=AUTO_MAX_STEPS)
    else:
        cfg = _ensure_config(
            "trae_config.chat.yaml", tools=CHAT_TOOLS, max_steps=CHAT_MAX_STEPS)
    result = await _run_trae(workdir, cfg, output_rel="output/reply.md")
    reply = (result.get("script") or "").strip()
    if not reply:
        raise HTTPException(500, "Trae 没有产出回答（reply.md 与 stdout 均为空）")
    return {
        "reply": reply,
        "warning": result.get("warning") or "",
        "workdir": result.get("workdir") or str(workdir),
        "timed_out": result.get("timed_out") or False,
    }
