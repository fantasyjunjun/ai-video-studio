"""参考片反推：本地抽帧/量测 + LLM 结构化解构 + 三档判定。

流程刻意分两段：
  1. **事实段（本地、零成本、可复现）**：ffprobe 取规格、ffmpeg 抽拼图与逐帧亮度/帧差；
  2. **判断段（走 LLM）**：把事实 + 知识库铁律交给模型，让它做结构化解构与三档判定。

为什么要分：**模型的时序/构图判断不可靠，但亮度与帧差是实测的**。
把可测的部分留在本地，模型只在"如何归类"上发言，报告才有核对价值。

R-32 增补：判断段分「看图」与「纯数据」两条路，**默认走看图**。
   - **看图（vision）**：把 ffmpeg 抽的抽帧拼图（一张网格图，按时间从左到右、
     从上到下）交给支持视觉的通道 → 模型真的能看到画面，于是可以**逐镜反推
     可复用的英文提示词**、并做有效性归因。这是"逆推提示词"能成立的前提：
     纯文本通道只能拿到亮度序列，反推出的画面描述必然是编的。
   - **纯数据（fallback）**：通道不支持视觉（NotImplementedError）或调用失败时
     自动降级为原来的数据判定（只出结构，不出提示词），并在报告里标注
     `vision=false` 让用户知道这份报告为什么没有提示词。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..audio.ffmpeg import probe_video, run
from ..db.models import ReverseReport
from ..kb.loader import KnowledgeBase
from ..providers.base import LLMProvider

_FENCE_HEAD = re.compile(r"^\s*```[a-zA-Z]*\s*")   # 开头围栏（``` / ```json / ```JSON）

# 反推一次要看 30 格拼图并逐镜写完整提示词，输出比"只写结构"长得多。
# 中转站前置 Cloudflare 有 100s 硬超时（HTTP 524）：上游在 100s 内没吐出任何字节就被掐。
# 三层把"首 token"压进 100s：A 缩图（最长边 960）+ B 控 token + C 快视觉模型
# （设置页填，复用同一中继与密钥）。
# D（流式）是最后一环：看图调用走 SSE 逐 token 读取，token 持续流动 → Cloudflare 视作
# "连接活跃"不再发 524，于是**即使总时长 > 100s 也不会被掐**。这里的 VISION_TIMEOUT 是
# 流式的**空闲超时**——只要 token 持续到达就不触发；首字节（TTFT）超过它则抛清晰超时
# 而非 524。
#
# ⚠️ 输出上限（VISION_MAX_TOKENS）与超时是**两件事**，早期版本把两者混为一谈：
#    为了"压进 100s"把上限从 6000 砍到 3500，结果逐镜（英文七层提示词 + 中文直译）
#    10~15 镜在数组中途被腰斩 → 报"LLM 输出不是合法 JSON"。**流式落地后 524 已由 D 兜住，
#    输出上限不需要再为它让路**，故回到 8000（仍可在设置页调大）。
VISION_TIMEOUT = 90
VISION_MAX_TOKENS = 8000
VISION_SHEET_MAX_SIDE = 960          # A：看图前把拼图最长边压到 960，降低请求体量
TEXT_MAX_TOKENS = 4000

# ★R-38 逐镜高清复核（治"漏写道具"）
# 第一遍把整片 15s 压成 30 格 240px 网格图（5x6@2fps → 1200×2592）：模型要在一张图里
# 同时做"切镜点对齐 + 逐镜画面描述 + 逐镜提示词"，注意力被摊到 30 格上，**道具成批漏写**
# —— 实测报告 #6 漏了原片 A3 的一簇白色小花与一串黑樱桃、把 A1 的白玫瑰花丛写成"花瓣"、
# 把 A1/A6 的灰色石柱与浅色台面写成"深棕背景 / 深色台面"，而这些元素在 240px 网格格子里
# **清晰可见**（已裁格验证，`truncated=False`、14204 字符未截断）→ 是注意力问题，不是输入
# 不清。所以补一遍**逐镜单独看图**：每镜取 1-3 张**原始分辨率**帧单独问一次，只描述这一镜
# 的画面元素，再合并回报告（逐镜复核结果覆盖第一遍的 desc / 结果性元素 / 提示词）。
VISION_SHOT_MAX_SIDE = 768           # 逐镜高清帧最长边（只缩不放：480×864 原样送）
VISION_SHOT_TIMEOUT = 90
PER_SHOT_MAX_FRAMES = 3              # ≥3s 取 3 帧、≥1.5s 取 2 帧、更短取 1 帧
PER_SHOT_RETRY = 1                   # 视觉通道**间歇性拒图**时的重试次数（见下方注释）

# ★R-38b 视觉通道的**间歇性拒图**（探针实测，换模型/换策略前必须验的那一枪）：
# 同一批原始分辨率帧、同一模型同一参数，A1/A3 描述准确（数出 6-8 朵白花、认出抛光石台
# 的反光、读出瓶身文字），**A6 却直接回 `I DID NOT RECEIVE AN IMAGE.`**（耗时仅 6s）。
# 危害有两条：
#   ① 轻——该镜的复核白做（浪费一次调用，报告里那一镜仍是网格图初稿）；
#   ② 重——若把这句话当正常输出解析，**"我没收到图片"会被写进 desc/沿用清单**，
#      后面复刻环节就会照着一条"其实什么也没说"的清单去复刻（比漏写更坏：漏写是缺，
#      这是错）。同门的 MiniMax-M2 更有前科：**没有视觉能力却硬编一段像模像样的 desc 落库**。
# 所以三方设防：system 明令"没收到图就只回 NO IMAGE RECEIVED"；代码识别这类回答并
# **重试一次**；重试仍失败就如实记进备注、该镜沿用网格图初稿 —— 绝不产出垃圾。
_VISION_REFUSED_RE = re.compile(
    r"no\s+image\s+(?:received|was\s+received|attached|provided|uploaded|visible)"
    r"|did\s+not\s+receive\s+an?\s+image"
    r"|(?:can\s*not|can't|cannot|unable\s+to)\s+(?:see|view|access|read)\s+"
    r"(?:any\s+|the\s+)?image"
    r"|i\s+don'?t\s+see\s+(?:an?\s+)?image"
    r"|没有(?:收到|看到|接收到)(?:任何)?(?:图片|图像|画面)"
    r"|未(?:收到|看到)(?:任何)?(?:图片|图像)"
    r"|无法(?:查看|看到|读取)(?:任何)?(?:图片|图像)", re.I)


def _vision_refused(text: str) -> bool:
    """判断这次的视觉回答是不是「我没收到图片」这类拒绝式回答（而非画面描述）。

    只看**前 200 字符**：正常的画面描述开篇必然是画面内容；真拒绝时那句话一定在开头
    （探针实测 `I DID NOT RECEIVE AN IMAGE.` 就 27 个字符、占满全文）。限制长度是为了
    避免正文里偶然出现 "no image of the label visible" 这类**描述性**短语被误判成拒图。
    """
    head = (text or "").strip()[:200]
    return bool(head) and bool(_VISION_REFUSED_RE.search(head))


# 反推看图的视觉模型覆盖：默认空字符串 = 使用当前 active LLM 的模型。
# 想加速看图（C）就在设置页填一个更快的视觉模型名（如 gpt-4o / claude-sonnet-*），
# 它复用当前 active LLM 的 base_url 与同一把密钥，只覆盖模型名——
# 不需要（也不应该）单独建一条 provider 条目（那条会查不到凭据库里的密钥）。
REVERSE_VISION_MODEL_DEFAULT = ""

# 可迁移 / 不可迁移 / 需适配的分档口径（技能 `video-reverse-engineering.md` 步骤 4），
# 与官方 `retention_analysis` 四档标记的对应关系写进提示词，避免模型自创一套词。
RETENTION_SPEC = (
    "fully_preserved（完整保留：镜头序列/时间轴/运镜/光影原样复用）"
    "| partially_preserved（部分保留：手法保留但按本片调整）"
    "| attribute_transfer（特征迁移：把原片主体的特征迁移到新主体，如人物/产品替换）"
    "| weak_reference（只保留宽泛相似：仅氛围或调性相近，画面不照搬）"
)


@dataclass
class FrameMetric:
    time: float
    yavg: float = 0.0     # 平均亮度
    ydif: float = 0.0     # 与前一帧的差（近似"运动/切点"）

    def to_dict(self) -> dict:
        return {"time": round(self.time, 3), "yavg": round(self.yavg, 2),
                "ydif": round(self.ydif, 3)}


@dataclass
class ReverseResult:
    source_path: str
    spec: Dict[str, Any] = field(default_factory=dict)
    sheet_path: str = ""
    metrics: List[FrameMetric] = field(default_factory=list)
    cut_candidates: List[float] = field(default_factory=list)
    analysis: Dict[str, Any] = field(default_factory=dict)
    findings: Dict[str, List[str]] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "source_path": self.source_path,
            "spec": self.spec,
            "sheet_path": self.sheet_path,
            "metrics": [m.to_dict() for m in self.metrics],
            "cut_candidates": [round(c, 3) for c in self.cut_candidates],
            "analysis": self.analysis,
            "findings": self.findings,
            "error": self.error,
        }


def parse_signalstats(text: str) -> List[FrameMetric]:
    """解析 `signalstats,metadata=print` 的输出。"""
    rows: List[FrameMetric] = []
    cur: Optional[FrameMetric] = None
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("frame:"):
            m = re.search(r"pts_time:\s*([0-9.]+)", line)
            cur = FrameMetric(time=float(m.group(1)) if m else float(len(rows)))
            rows.append(cur)
            continue
        if cur is None:
            continue
        if "signalstats.YAVG=" in line:
            try:
                cur.yavg = float(line.split("=", 1)[1])
            except ValueError:
                pass
        elif "signalstats.YDIF=" in line:
            try:
                cur.ydif = float(line.split("=", 1)[1])
            except ValueError:
                pass
    return rows


def detect_cuts(rows: List[FrameMetric], factor: float = 3.0,
                floor: float = 4.0) -> List[float]:
    """帧差突增 = 疑似切点。阈值取"下四分位 × factor"，并给一个地板值。

    为什么用下四分位而不是中位数：**切点本身就是离群值**。
    短片里切点能占到一半采样点（例如只有 2 个点时中位数直接等于那个尖峰），
    中位数会被自己的检测目标抬高到永远检不出来；下四分位取的是"常态水平"。
    地板值则防住另一头：全静止片常态接近 0，纯倍数会把噪声全判成切点。
    """
    if not rows:
        return []
    vals = sorted(r.ydif for r in rows)
    q1 = vals[max(0, int(0.25 * (len(vals) - 1)))]
    thr = max(floor, q1 * factor)
    out = [r.time for r in rows if r.ydif >= thr]
    # 相邻采样点去重（同一切点会连续两三帧都超阈值）
    dedup: List[float] = []
    for t in out:
        if dedup and t - dedup[-1] < 0.4:
            continue
        dedup.append(t)
    return dedup


def latest_findings_text(db: Session, max_chars: int = 1800) -> str:
    """最新一份反推报告的三档结论，拼成可直接注入生文上下文的文本块（R-28）。

    反推产物此前只躺在报告页里 —— 用户拆完参考片还要等人（或 agent）把结论
    蒸馏进规则才能影响生成。现在取**最新一份**报告（用户最新拆的参考片代表
    当前定标），把 adopt/adapt/reject 原文注入两条生文链路（骨架 prompt 与
    agent context.md）。没有报告 / findings 为空 → 返回空串，调用方不注入。
    超长截断（findings 是 LLM 产出，长度不可控，不能挤占骨架 prompt 预算）。
    """
    row = db.query(ReverseReport).order_by(ReverseReport.id.desc()).first()
    if row is None:
        return ""
    findings = (row.findings or "").strip()
    if not findings:
        return ""
    if len(findings) > max_chars:
        findings = findings[:max_chars] + "……（已截断）"
    created = row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "-"
    src = Path(row.source_path).name if row.source_path else "-"
    return (
        f"参考片反推报告 #{row.id}（{created}，来源：{src}）的三档结论 ——\n"
        f"【可直接采纳】= 已验证有效的手法，直接体现进叙事弧与分镜设计；\n"
        f"【须改造】= 手法有用但必须按写法要求执行（如喷雾绑光源锚点）；\n"
        f"【不可采纳】= 与铁律冲突或数据无法验证，**禁止照抄**。\n"
        f"{findings}"
    )


class ReverseEngine:
    def __init__(self, llm: LLMProvider, kb: KnowledgeBase, work_root: Path) -> None:
        self.llm = llm
        self.kb = kb
        self.work_root = Path(work_root)

    # ---------------------------------------------------------- 事实段

    def probe(self, path: str) -> Dict[str, Any]:
        v = probe_video(path)
        return {
            "width": v.width, "height": v.height, "fps": round(v.fps, 3),
            "duration": round(v.duration, 3), "nb_frames": v.nb_frames,
            "has_audio": v.has_audio, "codec": v.raw.get("codec_name", ""),
        }

    def extract(self, path: str, workdir: Path, *, sample_fps: float = 2.0,
                tile: str = "5x6") -> tuple[str, List[FrameMetric]]:
        """抽拼图 + 逐帧量测。**滤镜里不塞绝对路径**（盘符冒号会被当分隔符）。"""
        workdir.mkdir(parents=True, exist_ok=True)
        sheet = "sheet.jpg"
        run(["ffmpeg", "-y", "-v", "error", "-i", path,
             "-vf", f"fps={sample_fps},scale=240:-2,tile={tile}",
             "-frames:v", "1", "-q:v", "4", sheet],
            cwd=str(workdir))

        # signalstats 一并给 YAVG（亮度）与 YDIF（帧差），一次解码两样都拿到
        run(["ffmpeg", "-y", "-hide_banner", "-i", path,
             "-vf", f"fps={sample_fps},scale=64:64,signalstats,"
                    f"metadata=print:file=stats.txt",
             "-f", "null", "-"],
            cwd=str(workdir))

        stats = (workdir / "stats.txt")
        text = stats.read_text(encoding="utf-8", errors="ignore") if stats.exists() else ""
        return str(workdir / sheet), parse_signalstats(text)

    # ---------------------------------------------------------- 判断段

    def build_system(self, *, vision: bool = True) -> str:
        kb_text = self.kb.render(tags=["reverse", "always"])
        head = (
            "你是电商短视频的参考片拆解专家。\n"
            if not vision else
            "你是电商短视频的参考片拆解专家。\n"
            "你会同时拿到：① 一张**抽帧拼图**（原片按时间顺序抽帧拼成的网格，"
            "**从左到右、从上到下依次是接下来的时刻**，每格下方无标注，请自行按顺序对齐）；"
            "② 该片的实测数据（规格、逐帧亮度、帧差/切点候选）。\n"
            "以你**看到的画面**为主要依据，实测数据用来校准时间轴与切点。\n"
        )
        body = (
            "必须严格遵守下面的【领域知识库】做三档判定（可直接采纳 / 须改造 / 不可采纳）。\n\n"
            "输出要求：\n"
            "1. 只输出 JSON；\n"
            "2. 结构：{\"shots\": [{\"idx\": int, \"start\": float, \"end\": float, "
            "\"shot_size\": str, \"camera\": str, \"light\": str, \"desc\": str, "
            "\"result_elements\": [str], \"causes\": [str], "
        )
        if vision:
            body += (
                "\"why_effective\": str, \"retention\": str, "
                "\"prompt\": str, \"prompt_zh\": str}], "
            )
        else:
            body += "}], "
        body += (
            "\"findings\": {\"adopt\": [str], \"adapt\": [str], \"reject\": [str]}}；\n"
        )
        if vision:
            body += (
                "3. **逐镜反推提示词（prompt / prompt_zh）—— 这是本任务的交付核心**：\n"
                "   - `prompt`：按七层结构写**可直接投喂图生视频模型的英文提示词**，"
                "层标记顺序为 [SHOT] [HERO] [MOTION] [CAMERA] [LIGHT] [PHYSICS] [TEXTURE] "
                "[AUDIO] [NEG]；本镜没有的层整层省略，顺序不许变。\n"
                "     · 只写**画面里真实存在**的元素（你看到的）：主体、动作起点→终点、"
                "景别、运镜（带位移量并在 [NEG] 锁 no cut / no zoom）、光位与色温、"
                "可见的结果性元素（雾/水珠/飘落物）及其**画面内成因**；\n"
                "     · 参考图是原片的，你要写的是**画面复刻指令**，不要写"
                "\"consistent with the reference\" 这类笼统话；\n"
                "     · 原片里的人拍不出/模型做不到的（精细手部多关节、画内文字、"
                "镜头内变速）**不要写进 prompt**，改写到 findings.improve/adopt 里说明；\n"
                "   - `prompt_zh`：上面的中文直译，供人工核对；\n"
                "4. `why_effective`：这条镜头**为什么有效**，必须落在四类机制之一并点明是哪类 ——"
                "留存机制（防划走：视觉冲击/悬念/反差）/ 说服机制（建立信任与欲望：质感展示/"
                "使用场景/对比）/ 节奏机制（维持注意力：变化频率/音乐卡点）/ 转化机制（促成行动："
                "卖点重复/CTA）。说不出机制的镜头要明说\"可删\"；\n"
                f"5. `retention`：从四档里选一个 —— {RETENTION_SPEC}；\n"
                "6. `start` / `end` 以秒为单位、精确到 0.5s（用实测切点校准，"
                "没有切点的段落按画面变化点切分）；\n"
                "7. reject 必须写明**与哪条铁律冲突**，不许只写『不行』；\n"
                "8. 香调/成分一律不得推断 —— 画面里没有的就不写；\n"
                "9. **所有文字字段（desc / why_effective / result_elements / causes / "
                "findings 三档 / prompt_zh）一律用简体中文**，`prompt` 用英文，"
                "专业术语可保留英文原文（如 Close-up、mist）。\n\n"
                if vision else
                "3. reject 必须写明**与哪条铁律冲突**，不许只写『不行』；\n"
                "4. 香调/成分一律不得推断 —— 数据里没有的就不写；\n"
                "5. **所有文字字段（desc / result_elements / causes / findings 三档）"
                "一律用简体中文**，专业术语可保留英文原文（如 Close-up、mist）。\n"
                "6. 你看不到画面本身，**不要编造画面细节**：只依据亮度/帧差与切点"
                "做结构与手法层面的判断。\n\n"
            )
        return (
            head + body
            + "【领域知识库】\n"
            + f"{kb_text}\n"
        )

    @staticmethod
    def build_user(spec: Dict[str, Any], metrics: List[FrameMetric],
                   cuts: List[float], note: str, *,
                   vision: bool = True, tiles: int = 0) -> str:
        bright = [m.yavg for m in metrics]
        lines = [
            f"参考片规格：{spec['width']}x{spec['height']} @ {spec['fps']}fps, "
            f"{spec['duration']}s, {spec['nb_frames']} 帧, 音轨={spec['has_audio']}",
            f"采样：每 {1.0 / max(0.1, len(metrics) / max(spec['duration'], 0.1)):.2f}s 一点，共 {len(metrics)} 点",
            f"亮度区间：{min(bright) if bright else 0:.1f} ~ {max(bright) if bright else 0:.1f}",
            f"疑似切点（秒）：{[round(c, 2) for c in cuts]}",
        ]
        if vision and tiles:
            lines += [
                "",
                f"拼图说明：随消息附上的网格图共 {tiles} 格，按**左→右、上→下**"
                f"的时间顺序排列（每格间隔约 "
                f"{max(spec['duration'], 0.1) / max(tiles, 1):.2f}s）；"
                "对照上面的切点列表即可定出每镜起止。",
            ]
        lines += ["", "逐点数据（时间/亮度/帧差）："]
        for m in metrics:
            lines.append(f"  {m.time:6.2f}s  YAVG={m.yavg:6.2f}  YDIF={m.ydif:7.3f}")
        if note:
            lines += ["", f"用户补充：{note}"]
        lines += ["", "请输出 JSON。"]
        return "\n".join(lines)

    @staticmethod
    def parse_llm(raw: str) -> Dict[str, Any]:
        """解析模型输出为 dict —— **宽容到能把被腰斩的输出救回来**。

        为什么必须宽容：逐镜要写英文七层提示词 + 中文直译，一份 10~15 镜的输出很长，
        撞 `max_tokens` 上限（或流被中途掐断）时 JSON 会在数组中途断掉。这时整份输出
        "不是合法 JSON"，但**前面若干镜是完全完整的** —— 直接报错整份作废太浪费。

        四级降级：
          1. 去掉 ``` 围栏（**含未闭合的围栏**）后整体解析；
          2. 取首 `{` 到末 `}` 再解析（容忍模型前后多写解释）；
          3. **残卷抢救**：括号配对扫描出所有完整的对象片段，按形状归位
             （有 `idx` = 镜头 / 有 adopt·adapt·reject = findings），
             结果里标 `_truncated=True` 让调用方提示用户"这是残卷"；
          4. 都失败才抛错（错误信息里带上字符数与原文片段，便于定位）。
        """
        text = _strip_fence(raw or "")
        if not text:
            raise ValueError("LLM 输出为空（未收到任何内容）")
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            try:
                data = json.loads(text[i:j + 1])
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass

        # ③ 残卷抢救：把每个括号配对完整的对象捞出来，按形状归位
        shots: List[Any] = []
        findings: Dict[str, Any] = {}
        for chunk in _balanced_objects(text):
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if isinstance(obj.get("shots"), list):
                shots.extend([s for s in obj["shots"] if isinstance(s, dict)])
                if isinstance(obj.get("findings"), dict):
                    findings = obj["findings"]
            elif isinstance(obj.get("idx"), (int, float)):
                shots.append(obj)
            elif any(k in obj for k in ("adopt", "adapt", "reject")):
                findings = {k: (obj.get(k) or []) for k in ("adopt", "adapt", "reject")}

        # 去重（同一镜可能既被 root 的 shots 数组包含、又被单独扫到）
        uniq: List[Any] = []
        seen: set = set()
        for s in shots:
            k = s.get("idx")
            if k is not None:
                if k in seen:
                    continue
                seen.add(k)
            uniq.append(s)

        if uniq:
            return {"shots": uniq, "findings": findings, "_truncated": True}

        raise ValueError(
            f"LLM 输出不是合法 JSON（已收到 {len(raw or '')} 字符，且无法从残卷中救回镜头）："
            f"{(raw or '')[:300]}"
        )

    # ---------------------------------------------------------- 主流程

    def analyze(self, path: str, *, sample_fps: float = 2.0, note: str = "",
                use_llm: bool = True, tile: str = "5x6",
                use_vision: bool = True, slug: str = "",
                vision_model: Optional[str] = None,
                vision_max_tokens: Optional[int] = None,
                per_shot: bool = True) -> ReverseResult:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"参考片不存在：{path}")

        res = ReverseResult(source_path=str(p))
        res.spec = self.probe(str(p))
        workdir = self.work_root / (slug or p.stem)
        res.sheet_path, res.metrics = self.extract(
            str(p), workdir, sample_fps=sample_fps, tile=tile)
        res.cut_candidates = detect_cuts(res.metrics)

        if not use_llm:
            return res

        tiles = _tile_count(tile)
        max_tok = int(vision_max_tokens or VISION_MAX_TOKENS)
        # ① 优先走看图（流式 D）：有拼图 + 通道支持视觉 → 真的能看到画面，才谈得上"逆推提示词"
        #    vision_model 非空时只覆盖模型名（C：更快的视觉模型，复用同一中继与密钥）。
        if use_vision and res.sheet_path and tiles:
            raw = ""
            try:
                img = _read_image_scaled(res.sheet_path, VISION_SHEET_MAX_SIDE)
                if img is not None:
                    r = self.llm.complete_vision_stream(
                        self.build_system(vision=True),
                        self.build_user(res.spec, res.metrics, res.cut_candidates,
                                        note, vision=True, tiles=tiles),
                        [img],
                        temperature=0.4, max_tokens=max_tok,
                        timeout=VISION_TIMEOUT,
                        model=(vision_model or None),
                    )
                    raw = r.text
                    if _vision_refused(raw):
                        # 网格图这一遍同样偶发拒图（同一通道、同一模型）。这里**必须降级**：
                        # 若放任这句话往下走，轻则 parse 失败走 except（无害），重则
                        # 模型一边说"没收到图片"一边给出 JSON → parse 成功 → 一份纯编造的
                        # 逐镜报告进库，后面复刻全跟着错。宁可这一遍没有逐镜提示词。
                        raise RuntimeError(
                            "视觉通道回「没收到图片」（间歇性拒图）→ 本次降级为纯数据判定")
                    data = self.parse_llm(raw)
                    res.analysis = {"shots": data.get("shots", []), "vision": True,
                                    "raw_chars": len(raw)}
                    note_txt = self._truncation_note(r, data)
                    if note_txt:
                        res.analysis["vision_note"] = note_txt
                        res.analysis["truncated"] = True
                    if note_txt or data.get("_truncated"):
                        # 残卷要靠原文才能定位（是上限太小？模型胡说？中继断流？），
                        # 落一份到工作目录，覆盖写不堆积。
                        self._dump_raw(workdir, raw)
                    # ★R-38：网格图初稿到手后，补一遍**逐镜高清复核**（治"漏写道具"）。
                    # 放在这里的原因：逐镜复核要有实测切点才知道每镜取哪几帧。
                    if per_shot:
                        self._run_per_shot(
                            res, str(p), workdir, res.analysis.get("shots") or [],
                            vision_model=vision_model,
                            vision_max_tokens=vision_max_tokens)
                    self._fill_findings(res, data)
                    return res
            except NotImplementedError:
                res.analysis = {"shots": [], "vision": False,
                                "vision_note": "当前大模型不支持看图，已降级为纯数据判定"
                                               "（不出逐镜提示词）"}
            except Exception as e:  # noqa: BLE001 - 看图失败绝不阻断，降级继续
                self._dump_raw(workdir, raw)
                res.analysis = {"shots": [], "vision": False,
                                "vision_note": f"看图反推失败，已降级为纯数据判定：{str(e)[:200]}"
                                               + (f"（已收到 {len(raw)} 字符）" if raw else "")}

        # ② 降级：纯数据判定（原行为，只出结构，不出提示词）
        raw = self.llm.complete(
            self.build_system(vision=False),
            self.build_user(res.spec, res.metrics, res.cut_candidates, note,
                            vision=False),
            temperature=0.4, max_tokens=TEXT_MAX_TOKENS,
        ).text
        data = self.parse_llm(raw)
        res.analysis = {**(res.analysis or {}), "shots": data.get("shots", []),
                        "vision": False}
        self._fill_findings(res, data)
        return res

    # ---------------------------------------------- 逐镜高清复核（★R-38）

    def extract_shot_frames(self, path: str, workdir: Path,
                            shots: List[Dict[str, Any]]) -> Dict[int, List[str]]:
        """按镜切**原始分辨率**帧（只缩不放），每镜 1-3 张，落 `shots/` 子目录。

        帧数按时长给：≥3s 取 3 帧（看得出动作与镜头内变化）、≥1.5s 取 2 帧、
        更短取 1 帧（中段单帧足够看清陈设与道具）。取点位避开剪辑点 ±0.15s，
        免得拿到转场糊帧。

        `-i` 用**绝对路径**、输出用相对名：`cwd` 设成了 workdir，两者混用会让
        ffmpeg 按 workdir 去解析相对输入路径 → 报 file not found（第一版实测踩到，
        还被 `except` 吞成了"静默漏抽"）。
        """
        out_dir = workdir / "shots"
        out_dir.mkdir(parents=True, exist_ok=True)
        src = str(Path(path).resolve())
        picked: Dict[int, List[str]] = {}
        for s in shots:
            try:
                idx = int(s.get("idx") or 0)
                start = float(s.get("start"))
                end = float(s.get("end"))
            except (TypeError, ValueError):
                continue
            dur = max(end - start, 0.0)
            if dur >= 3.0:
                ts = [start + 0.4, start + dur / 2, end - 0.4]
            elif dur >= 1.5:
                ts = [start + 0.2, end - 0.2]
            else:
                ts = [start + dur / 2]
            ts = [t for t in ts if start - 1e-6 <= t <= end + 1e-6][:PER_SHOT_MAX_FRAMES]
            files: List[str] = []
            for j, t in enumerate(ts):
                name = f"A{idx}_{j}.jpg"
                try:
                    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", src,
                         "-frames:v", "1",
                         "-vf", f"scale='min({VISION_SHOT_MAX_SIDE},iw)':-2",
                         "-q:v", "3", f"shots/{name}"],
                        cwd=str(workdir))
                except Exception:  # noqa: BLE001 - 单帧失败不影响其它帧
                    continue
                dst = out_dir / name
                if dst.exists():
                    files.append(str(dst))
            if files:
                picked[idx] = files
        return picked

    @staticmethod
    def build_shot_detail_system() -> str:
        """逐镜高清复核的系统提示：**只描述这一镜的画面事实**，不做全局判定。

        与第一遍（网格图）的分工：第一遍定时间轴与手法（切点、保留方式、有效性）、
        这一遍只干一件最容易漏的事 —— **把画面里有什么东西数清楚**。
        """
        return (
            "你是电商短视频的画面清单员。用户会给你**同一个镜头的 1-3 张原始分辨率帧**"
            "（按时间先后给出），以及该镜的时间码。\n"
            "你的唯一任务：把这一镜画面里的**可见事实**一条不落地写出来。\n"
            "重点（实测最容易漏的就是这几类）：\n"
            "1. **道具与陈设**：台面上、背景里、画面边缘的每一件物体 —— 花朵（是什么花、"
            "几朵、聚簇还是散放）、水果切片、坚果、豆子、杯盘、织物、瓶罐、装饰物；"
            "连「画面角落里的小物件」也要写。**宁可多写，不许漏**；\n"
            "2. **承载面与背景材质**：台面是什么（石质/大理石/木质/镜面）、什么颜色深浅、"
            "背景是墙面/瓷砖/石柱/纯色布，**颜色如实写**（浅色就写浅色，不要凭氛围猜成深色）；\n"
            "3. **主体**：产品（形态/瓶盖/标签可见文字/材质）与人物（可见部位/服装品类与颜色/"
            "发型/表情）；\n"
            "4. **光与雾**：光位与色温、有没有蒸汽/汽雾、从哪来、被什么照亮；\n"
            "5. **景别与运镜**：景别（中近景/近景/中景/特写…）与机位是否固定。\n"
            "只写**你在这几帧里真的看到**的东西；看不清就写「看不清」，"
            "**绝不推测**（尤其不许推断香调/成分、不许给画面里没有的东西找理由）。\n"
            "★**如果你没有收到任何图片**（附件丢失/读不到），只回一行 "
            "`NO IMAGE RECEIVED`，**不要输出 JSON、不要凭镜号与时间码猜画面**——"
            "猜出来的画面清单会被当成事实用于后续复刻，比不写更坏。\n"
            "所有字段用简体中文，`prompt` 字段用英文。只输出 JSON，不要解释文字。"
        )

    @staticmethod
    def build_shot_detail_user(shot: Dict[str, Any], spec: Dict[str, Any],
                              times_note: str) -> str:
        return (
            f"镜头：第 {shot.get('idx')} 镜，{shot.get('start')}~{shot.get('end')}s"
            f"（时长 {float(shot.get('end', 0)) - float(shot.get('start', 0)):.1f}s）。"
            f"{times_note}\n"
            f"参考片规格：{spec.get('width')}x{spec.get('height')} @ {spec.get('fps')}fps。\n\n"
            "请输出 JSON：\n"
            "{\n"
            '  "desc": "<中文：这一镜画面上有什么，按「主体 → 陈设与道具 → 承载面与背景 → '
            '光与雾」的顺序逐项写全，一件都不许漏>",\n'
            '  "result_elements": ["<中文：画面里可见的结果性元素，如白色花瓣的光泽、'
            '蒸汽被灯板照亮；没有就空数组>"],\n'
            '  "causes": ["<中文：上面每个结果性元素在画面内的成因>"],\n'
            '  "shot_size": "<中文或英文：景别>",\n'
            '  "camera": "<中文：机位与运动（无位移就写静止机位）>",\n'
            '  "light": "<中文：光位 + 色温 + 背景明暗>",\n'
            '  "prompt": "<英文：可直接投喂图生视频模型的七层提示词，层标记顺序 '
            '[SHOT] [HERO] [MOTION] [CAMERA] [LIGHT] [PHYSICS] [TEXTURE] [AUDIO] [NEG]；'
            '画面里没有的层整层省略，顺序不许变。只写真实存在的元素，'
            '不要写 consistent with the reference 这类笼统话>",\n'
            '  "prompt_zh": "<上面 prompt 的中文直译>"\n'
            "}"
        )

    def _run_per_shot(self, res: ReverseResult, path: str, workdir: Path,
                      shots: List[Dict[str, Any]], *,
                      vision_model: Optional[str] = None,
                      vision_max_tokens: Optional[int] = None) -> None:
        """逐镜高清复核的编排与结果登记（**失败绝不阻断报告**）。"""
        if not shots:
            return
        try:
            detail, notes = self.analyze_per_shot(
                path, shots, workdir, res.spec,
                vision_model=vision_model, vision_max_tokens=vision_max_tokens)
        except Exception as e:  # noqa: BLE001 - 复核是增强项，不是必选项
            res.analysis["per_shot_note"] = f"逐镜高清复核未能执行：{str(e)[:150]}"
            return
        merged = self.merge_shot_detail(shots, detail)
        res.analysis["per_shot_ok"] = len(detail)
        res.analysis["per_shot_total"] = len(shots)
        res.analysis["per_shot_merged"] = merged
        if notes:
            res.analysis["per_shot_note"] = "；".join(notes)

    def analyze_per_shot(self, path: str, shots: List[Dict[str, Any]],
                         workdir: Path, spec: Dict[str, Any], *,
                         vision_model: Optional[str] = None,
                         vision_max_tokens: Optional[int] = None,
                         ) -> tuple[Dict[int, Dict[str, Any]], List[str]]:
        """逐镜高清复核：每镜单独看原始分辨率帧，返回 `{idx: 复核结果}` + 备注。

        单镜失败**绝不阻断其他镜**（网格图初稿仍是可用下限）；失败的镜如实记进备注，
        报告里标明"这一镜的画面对账没做完"，用户才知道哪几镜是低置信度。
        """
        picked = self.extract_shot_frames(path, workdir, shots)
        detail: Dict[int, Dict[str, Any]] = {}
        notes: List[str] = []
        max_tok = int(vision_max_tokens or VISION_MAX_TOKENS)
        system = self.build_shot_detail_system()
        for s in shots:
            try:
                idx = int(s.get("idx") or 0)
            except (TypeError, ValueError):
                continue
            files = picked.get(idx) or []
            if not files:
                notes.append(f"A{idx}：抽帧失败，该镜沿用网格图初稿")
                continue
            imgs = [im for im in (_read_image_scaled(f, VISION_SHOT_MAX_SIDE)
                                  for f in files) if im is not None]
            if not imgs:
                notes.append(f"A{idx}：帧文件读取失败，该镜沿用网格图初稿")
                continue
            note = f"随消息附上 {len(imgs)} 张帧，按时间先后排列。"
            user_msg = self.build_shot_detail_user(s, spec, note)

            def _once() -> tuple[Optional[dict], str]:
                """取一次复核结果 → `(数据, 失败原因)`；成功时原因为空串。"""
                r = self.llm.complete_vision_stream(
                    system, user_msg, imgs, temperature=0.2,
                    max_tokens=min(max_tok, 3000),
                    timeout=VISION_SHOT_TIMEOUT, model=(vision_model or None),
                )
                txt = r.text or ""
                if _vision_refused(txt):
                    return None, "模型回「没收到图片」（视觉通道间歇性拒图）"
                data = self.parse_llm(txt)
                if not isinstance(data, dict) or not (data.get("desc") or data.get("prompt")):
                    return None, "输出无法解析"
                # 拒图语也可能被塞进字段里（模型一边说没看到、一边给出一份 JSON）——
                # desc 命中拒图词同样判失败：这种"清单"一旦进报告，复刻环节会照着
                # 一条其实什么也没说的清单去复刻，比漏写更坏。
                if _vision_refused(str(data.get("desc") or "")):
                    return None, "desc 里写着「没收到图片」"
                return data, ""

            data: Optional[dict] = None
            why = ""
            tries = PER_SHOT_RETRY + 1
            for _attempt in range(tries):
                try:
                    data, why = _once()
                except Exception as e:  # noqa: BLE001
                    data, why = None, f"{type(e).__name__}: {str(e)[:80]}"
                if data:
                    break
            if data:
                detail[idx] = data
            else:
                notes.append(f"A{idx}：逐镜复核失败（{why}，共尝试 {tries} 次），沿用网格图初稿")
        return detail, notes

    @staticmethod
    def merge_shot_detail(shots: List[Dict[str, Any]],
                          detail: Dict[int, Dict[str, Any]]) -> List[str]:
        """把逐镜复核结果合并进报告（**高清复核优先**），返回合并说明。

        `start` / `end` / `idx` / `retention` 保留第一遍的值 —— 时间轴是实测切点、
        保留方式是全局判定，逐镜看图无权改（否则会与时间轴约束打架）。
        `desc` / `result_elements` / `causes` / `prompt` / `prompt_zh` / `shot_size` /
        `camera` / `light` 一律被逐镜复核结果覆盖。
        """
        merged: List[str] = []
        for s in shots:
            try:
                idx = int(s.get("idx") or 0)
            except (TypeError, ValueError):
                continue
            d = detail.get(idx)
            if not d:
                continue
            changed = []
            for key in ("desc", "shot_size", "camera", "light"):
                val = str(d.get(key) or "").strip()
                if val and val != str(s.get(key) or "").strip():
                    s[key] = val
                    changed.append(key)
            for key in ("result_elements", "causes"):
                val = d.get(key)
                if isinstance(val, list) and val:
                    s[key] = val
                    changed.append(key)
            for key in ("prompt", "prompt_zh"):
                val = str(d.get(key) or "").strip()
                if val:
                    s[key] = val
                    changed.append(key)
            s["detail_source"] = "per_shot_frame"
            if changed:
                merged.append(f"A{idx}（{'/'.join(changed)}）")
        return merged

    @staticmethod
    def _fill_findings(res: ReverseResult, data: Dict[str, Any]) -> None:
        f = data.get("findings") or {}
        # adapt/reject 之外额外收一档 improve（原片做法在模型上做不到时，
        # 看图链路会在 reject 之外单独列出"该怎么改造"），没有就为空。
        res.findings = {
            "adopt": list(f.get("adopt", [])),
            "adapt": list(f.get("adapt", [])),
            "reject": list(f.get("reject", [])),
        }

    @staticmethod
    def _truncation_note(r: Any, data: Dict[str, Any]) -> str:
        """把"为什么这份报告不完整"讲清楚 —— 三种成因，三种可执行的下一步。

        以前这里只有一句笼统的"不是合法 JSON"，用户无从判断是**上限太小**、
        **中继断流**还是**模型胡说**。现在按 finish_reason 分流：
          - `length`                → 上限太小，调大设置或换更快的模型；
          - 没等到 [DONE]/finish    → 中继/网关把流掐了；
          - 能解析但标了 _truncated → 输出不完整但已救回部分。
        文案里一律带上"已收到多少字符、救回几镜"，让用户能自己判断规模。
        """
        n = len(getattr(r, "text", "") or "")
        got = len(data.get("shots") or [])
        fr = getattr(r, "finish_reason", "") or ""
        if fr == "length":
            return (f"输出被 max_tokens 上限截断（finish_reason=length，已收到 {n} 字符），"
                    f"已保留能解析的前 {got} 镜；可在「设置 → 反推设置」调大输出上限，"
                    f"或换成更快的视觉模型")
        if not getattr(r, "stream_complete", True):
            return (f"流式响应被提前掐断（未收到 [DONE]/finish_reason，已收到 {n} 字符），"
                    f"已保留能解析的前 {got} 镜；多为中继或网关侧超时，"
                    f"可换更快的视觉模型或缩短参考片")
        if data.get("_truncated"):
            return (f"输出不完整（已收到 {n} 字符），已从残卷中保留能解析的前 {got} 镜")
        return ""

    @staticmethod
    def _dump_raw(workdir: Path, raw: str) -> None:
        """把模型原始输出落一份到工作目录（覆盖写，不堆积）。

        只在**解析失败或有截断**时调用：截断成因必须看原文才能定性，
        否则用户只能拿到一句二手结论。文件里只有模型输出，不含任何密钥。
        """
        if not raw:
            return
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            (workdir / "llm_raw.txt").write_text(raw, encoding="utf-8")
        except OSError:
            pass


def _tile_count(tile: str) -> int:
    """`5x6` → 30。拼图格数要告诉模型，它才能把网格对齐到时间轴。"""
    m = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", tile or "")
    if not m:
        return 0
    return int(m.group(1)) * int(m.group(2))


def _strip_fence(text: str) -> str:
    """剥掉 markdown 围栏，**容忍未闭合**。

    模型常写 ```json … ```，但输出被截断时收尾的 ``` 根本不会出现；
    旧实现用"必须成对"的正则去匹配，截断时整个匹配失败 → 连里面的 JSON 都拿去
    json.loads → 必然报"不是合法 JSON"。改为分别剥头尾，缺尾巴也能继续。
    """
    t = (text or "").strip()
    t = _FENCE_HEAD.sub("", t)
    t = t.strip()
    if t.endswith("```"):
        t = t[:-3].rstrip()
    return t.strip()


def _balanced_objects(text: str) -> List[str]:
    """扫描出所有**括号配对完整**的 `{...}` 片段（含嵌套，按闭合先后给出）。

    只认真正的结构：字符串内部的 `{}` 会被跳过（含 `\\"` 转义处理），
    所以模型写在提示词正文里的花括号不会污染结构判定。
    被腰斩的 JSON 里，末尾那个残缺对象因括号不配对而不会被收下 —— 这正是我们要的：
    **只取已写完整的对象**。
    """
    out: List[str] = []
    stack: List[int] = []
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}":
            if stack:
                out.append(text[stack.pop():i + 1])
    return out


def _read_image(path: str) -> Optional[tuple]:
    """读拼图字节。文件缺失/读不到返回 None（调用方据此降级，不报错）。"""
    try:
        p = Path(path)
        if not p.exists():
            return None
        return (p.read_bytes(), "image/jpeg")
    except OSError:
        return None


def _read_image_scaled(path: str, max_side: int = 960) -> Optional[tuple]:
    """A：读拼图并把最长边压到 max_side 以内再返回（降低看图请求的体量与模型看图耗时）。

    压图用 ffmpeg（本就是依赖），失败一律回退原始字节 —— 缩图只是优化，绝不因它阻断反推。
    """
    raw = _read_image(path)
    if raw is None:
        return None
    data, mime = raw
    try:
        import os
        import tempfile

        from ..audio.ffmpeg import run

        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "sheet.jpg")
            dst = os.path.join(td, "sheet_small.jpg")
            Path(src).write_bytes(data)
            run([
                "ffmpeg", "-y", "-i", src,
                "-vf", f"scale='if(gt(iw,ih),{max_side},-2)':"
                       f"'if(gt(ih,iw),{max_side},-2)'",
                "-q:v", "4", dst,
            ], check=True, timeout=30)
            small = Path(dst)
            if small.exists() and small.stat().st_size > 0:
                return (small.read_bytes(), "image/jpeg")
    except Exception:  # noqa: BLE001 - 压图失败就用原图
        pass
    return raw
