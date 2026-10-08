"""工具表：MCP 工具 ↔ 工作室 REST 的映射。

每个工具只回答三件事：**收哪些参数**、**转发到哪条路径**、**是否花钱**。
任何"流程"（先查 A 再决定 B、失败重试、落库事务、词表判定）一律不在这里实现
—— 那是后端 `app/services` 与 `app/pipeline` 的职责，本层重复一遍必然分叉。

**不暴露破坏性操作**：删除项目 / 删除镜头 / 删除资产一律不给工具。外部 agent
可以一路往前推进（建项目 → 分镜 → 出片 → 后期 → 自检），但删不掉你的东西。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .client import StudioClient, StudioError

# 任务终态（与 app/services/task_ledger.py 的状态机一致）
TERMINAL_JOB_STATUS = {"succeeded", "failed", "reused", "dry_run", "canceled",
                       "unknown"}

SPEND_ENV = "STUDIO_MCP_ALLOW_SPEND"


def allow_spend() -> bool:
    """是否授权 MCP 触发**计费**动作。默认 **否**。

    外部 agent 一旦拿到出片权，就可能在你没盯着的时候连着烧钱 —— 所以默认
    是"能读、能算、能自检，但不能花钱"。要放开，在客户端配置里加
    `STUDIO_MCP_ALLOW_SPEND=1`，或在软件「设置」里打开同名开关。
    """
    return os.environ.get(SPEND_ENV, "0").strip().lower() in (
        "1", "true", "yes", "on", "y")


# ---------------------------------------------------------------- 工具模型


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: Dict[str, Any]
    handler: Callable[[StudioClient, Dict[str, Any]], Any]
    spend: bool = False          # 会花钱 → 受 allow_spend() 门控
    timeout: float = 30.0        # HTTP 超时（长任务另计）
    group: str = ""

    def spec(self) -> Dict[str, Any]:
        desc = self.description.strip()
        if self.spend:
            desc += ("\n\n**会花钱**：需后端设置页或客户端环境变量 "
                     f"`{SPEND_ENV}=1` 明确授权；未授权时本工具直接拒绝、"
                     "不会发出任何请求。先用 `dry_run=true` 可以免费试算。")
        return {"name": self.name, "description": desc, "inputSchema": self.schema}


def _obj(props: Dict[str, Any], required: Optional[List[str]] = None,
         **extra: Any) -> Dict[str, Any]:
    return {"type": "object", "properties": props,
            "required": required or [], "additionalProperties": False, **extra}


def _need(args: Dict[str, Any], key: str, cast: Optional[Callable] = None) -> Any:
    v = args.get(key)
    if v is None or v == "":
        raise StudioError(f"缺少必填参数 {key!r}")
    if cast is not None:
        try:
            return cast(v)
        except (TypeError, ValueError) as e:
            raise StudioError(f"参数 {key!r} 类型不对（收到 {v!r}）") from e
    return v


def _drop_none(args: Dict[str, Any], keys) -> Dict[str, Any]:
    return {k: args[k] for k in keys if args.get(k) is not None}


# ---------------------------------------------------------------- 项目


def _h_list_projects(c: StudioClient, a: Dict[str, Any]) -> Any:
    return c.get("/api/projects")


def _h_get_project(c: StudioClient, a: Dict[str, Any]) -> Any:
    pid = _need(a, "project_id", int)
    out = c.get(f"/api/projects/{pid}")
    # 成本随详情一起回：agent 看项目时几乎总要知道"这个花了多少"
    try:
        out["cost"] = c.get(f"/api/projects/{pid}/cost")
    except StudioError as e:
        out["cost_error"] = e.describe()
    return out


def _h_create_project(c: StudioClient, a: Dict[str, Any]) -> Any:
    body = {"name": _need(a, "name")}
    body.update(_drop_none(a, ("language", "talent_id", "llm_provider",
                               "image_provider", "video_provider", "products")))
    return c.post("/api/projects", json_body=body)


# ---------------------------------------------------------------- 分镜


def _h_generate_storyboard(c: StudioClient, a: Dict[str, Any]) -> Any:
    pid = _need(a, "project_id", int)
    shots = a.get("shots")
    if not shots:
        raise StudioError("缺少必填参数 'shots'：至少给一个镜头规格 "
                          "[{code, duration_sec, brief}]")
    body = {"shots": shots, "extra": a.get("extra") or "",
            "min_score": a.get("min_score") or 8}
    return c.post(f"/api/projects/{pid}/storyboard", json_body=body, timeout=600.0)


def _h_list_shots(c: StudioClient, a: Dict[str, Any]) -> Any:
    pid = _need(a, "project_id", int)
    return c.get(f"/api/projects/{pid}/shots")


def _h_update_shot(c: StudioClient, a: Dict[str, Any]) -> Any:
    sid = _need(a, "shot_id", int)
    body = _drop_none(a, ("code", "idx", "duration_sec", "prompt_en",
                          "prompt_zh", "status", "seed"))
    body["relint"] = bool(a.get("relint", True))
    if len(body) == 1:  # 只有 relint，等于什么都没改
        raise StudioError("没给要改的字段：至少给 prompt_en / duration_sec 之一")
    return c.patch(f"/api/shots/{sid}", json_body=body)


def _h_lint_prompt(c: StudioClient, a: Dict[str, Any]) -> Any:
    """纯本地打分，免费、无副作用 —— agent 改稿后应该先跑它再落库。"""
    return c.post("/api/lint", json_body={
        "prompt": _need(a, "prompt"),
        "duration": float(a.get("duration") or 3.0),
    })


# ---------------------------------------------------------------- 出片


def _h_render_shot(c: StudioClient, a: Dict[str, Any]) -> Any:
    sid = _need(a, "shot_id", int)
    body = {"kind": a.get("kind") or "video",
            "dry_run": bool(a.get("dry_run", False)),
            "reuse": bool(a.get("reuse", True))}
    body.update(_drop_none(a, ("provider_id", "duration", "resolution", "seed")))
    if a.get("ref_image_paths"):
        body["ref_image_paths"] = a["ref_image_paths"]
    if a.get("extra"):
        body["extra"] = a["extra"]
    # 异步：提交完就回，出片进度走 get_job / wait_for_job
    return c.post(f"/api/shots/{sid}/render", json_body=body, timeout=120.0)


def _h_get_job(c: StudioClient, a: Dict[str, Any]) -> Any:
    return c.get(f"/api/jobs/{_need(a, 'job_id', int)}")


def _h_wait_for_job(c: StudioClient, a: Dict[str, Any]) -> Any:
    """轮询到终态。**MCP 工具内阻塞**，所以 max_wait 有硬上限。"""
    jid = _need(a, "job_id", int)
    interval = max(1.0, float(a.get("interval") or 5.0))
    max_wait = min(900.0, max(5.0, float(a.get("max_wait") or 300.0)))
    deadline = time.time() + max_wait
    polls = 0
    last: Dict[str, Any] = {}
    while True:
        last = c.get(f"/api/jobs/{jid}")
        polls += 1
        if last.get("status") in TERMINAL_JOB_STATUS:
            last["_polls"] = polls
            last["_waited_sec"] = round(max_wait - (deadline - time.time()), 1)
            return last
        if time.time() >= deadline:
            last["_timed_out"] = True
            last["_polls"] = polls
            last["_hint"] = (f"超过 max_wait={max_wait:.0f}s 仍未完成；任务还在跑，"
                             "可稍后再 get_job 查询，或用 list_render_tasks 看台账。")
            return last
        time.sleep(interval)


# ---------------------------------------------------------------- 质检 / 合规


def _h_compliance_scan(c: StudioClient, a: Dict[str, Any]) -> Any:
    body: Dict[str, Any] = {"text": _need(a, "text")}
    if a.get("langs"):
        body["langs"] = a["langs"]
    if a.get("source"):
        body["source"] = a["source"]
    return c.post("/api/compliance/scan", json_body=body)


def _h_qc_project(c: StudioClient, a: Dict[str, Any]) -> Any:
    pid = _need(a, "project_id", int)
    body: Dict[str, Any] = {}
    body.update(_drop_none(a, ("roi", "prompt")))
    if a.get("shot_ids"):
        body["shot_ids"] = a["shot_ids"]
    if a.get("persist") is not None:
        body["persist"] = bool(a["persist"])
    # 逐镜解帧，慢
    return c.post(f"/api/projects/{pid}/qc", json_body=body, timeout=900.0)


def _h_verify_delivery(c: StudioClient, a: Dict[str, Any]) -> Any:
    pid = _need(a, "project_id", int)
    params: Dict[str, Any] = {}
    params.update(_drop_none(a, ("path", "expected_frames", "fps")))
    params["qc"] = bool(a.get("qc", False))
    if a.get("qc_roi"):
        params["qc_roi"] = a["qc_roi"]
    return c.post(f"/api/projects/{pid}/verify", params=params, timeout=900.0)


# ---------------------------------------------------------------- 后期


def _h_run_final(c: StudioClient, a: Dict[str, Any]) -> Any:
    """一键后期：念白 / BGM / 音效 / 混音全在本地 ffmpeg 完成，**不产生外部费用**。"""
    pid = _need(a, "project_id", int)
    body: Dict[str, Any] = {}
    for k in ("specs", "video_path", "total_frames", "fps", "rows", "raw_wav",
              "voice", "bgm_duration", "bgm_split", "bgm_root2", "sfx_kind",
              "auto_sfx", "out_name", "force", "enforce_compliance"):
        if a.get(k) is not None:
            body[k] = a[k]
    return c.post(f"/api/projects/{pid}/final", json_body=body, timeout=1200.0)


# ---------------------------------------------------------------- 预算 / 素材 / 反推


def _h_get_budget(c: StudioClient, a: Dict[str, Any]) -> Any:
    params = _drop_none(a, ("project_id",))
    return c.get("/api/budget", params=params or None)


def _h_list_assets(c: StudioClient, a: Dict[str, Any]) -> Any:
    params = {k: v for k, v in _drop_none(
        a, ("q", "kind", "project_id", "limit")).items()}
    return c.get("/api/assets", params=params or None)


def _h_reverse_video(c: StudioClient, a: Dict[str, Any]) -> Any:
    body: Dict[str, Any] = {"path": _need(a, "path")}
    body.update({k: v for k, v in a.items() if k != "path" and v is not None})
    return c.post("/api/reverse", json_body=body, timeout=600.0)


# ---------------------------------------------------------------- 注册表


TOOLS: List[Tool] = [
    # ---- 项目 ----
    Tool(
        name="list_projects",
        group="项目",
        description="列出工作室里所有项目（id / 名称 / 语言 / 状态 / 花费）。\n"
                    "**任何不知道 project_id 的时候，第一步都应该调它。**",
        schema=_obj({}),
        handler=_h_list_projects,
    ),
    Tool(
        name="get_project",
        group="项目",
        description="看单个项目的完整详情（含成本拆分与当时所用的供应商组合）。\n"
                    "返回里的 `cost` 是按供应商 / 按镜头拆好的花费，回答"
                    "「这个项目花了多少」用这一个工具就够，不要另找成本接口。",
        schema=_obj({"project_id": {"type": "integer",
                                    "description": "项目 id（从 list_projects 取）"}},
                    ["project_id"]),
        handler=_h_get_project,
    ),
    Tool(
        name="create_project",
        group="项目",
        description="新建项目。`language` 决定后续念白文案的语言（如 pt-BR / es-MX / zh-CN）。\n"
                    "`products` 用来把产品库里的产品挂到项目上，格式 "
                    "`[{\"product_id\": 1, \"role\": \"hero\"}]`；"
                    "**分镜生成会读产品的香调事实，C 级事实不会被写进提示词。**\n"
                    "新建后一般接 generate_storyboard。",
        schema=_obj({
            "name": {"type": "string", "description": "项目名"},
            "language": {"type": "string", "description": "投放语言，默认 pt-BR"},
            "talent_id": {"type": "integer", "description": "绑定模特 id（可选）"},
            "products": {"type": "array", "description": "[{product_id, role}]",
                         "items": {"type": "object"}},
            "llm_provider": {"type": "string"},
            "image_provider": {"type": "string"},
            "video_provider": {"type": "string"},
        }, ["name"]),
        handler=_h_create_project,
    ),
    # ---- 分镜 ----
    Tool(
        name="generate_storyboard",
        group="分镜",
        spend=True,
        timeout=600.0,
        description="**调 LLM 生成分镜提示词并跑 lint 门禁**（不达标签自动回灌重生成），"
                    "结果落库。\n"
                    "入参 `shots` 是镜头规格清单：`[{\"code\":\"A1\","
                    "\"duration_sec\":3,\"brief\":\"开场：晨光里的梳妆台\"}]`。"
                    "**brief 写意图，不要写最终提示词** —— 提示词由引擎按 v2 模板生成。\n"
                    "返回每个镜头的 prompt_en / prompt_zh / lint 分数。"
                    "生成后建议用 list_shots 复核。",
        schema=_obj({
            "project_id": {"type": "integer"},
            "shots": {"type": "array", "description": "镜头规格清单",
                      "items": {"type": "object",
                                "properties": {
                                    "code": {"type": "string"},
                                    "duration_sec": {"type": "number"},
                                    "brief": {"type": "string"}},
                                "required": ["code", "duration_sec", "brief"]}},
            "extra": {"type": "string", "description": "追加给引擎的额外要求（可选）"},
            "min_score": {"type": "integer", "description": "lint 及格线，默认 8（满分 14）"},
        }, ["project_id", "shots"]),
        handler=_h_generate_storyboard,
    ),
    Tool(
        name="list_shots",
        group="分镜",
        description="列出项目的全部镜头，含 `prompt_en`（出片实际用的）、`prompt_zh`"
                    "（中文直译）、`lint_score`、`duration_sec`、`id`。\n"
                    "**出片前必须先拿这里的 shot id。**",
        schema=_obj({"project_id": {"type": "integer"}}, ["project_id"]),
        handler=_h_list_shots,
    ),
    Tool(
        name="update_shot",
        group="分镜",
        description="改单个镜头的提示词或时长。**改完后端会自动重算 lint 并把分数落库**，"
                    "所以返回值里的 `lint_score` 就是改后真实分数。\n"
                    "常见用法：list_shots 看到某镜 lint 低 → 改 prompt_en → 看新分数。",
        schema=_obj({
            "shot_id": {"type": "integer"},
            "prompt_en": {"type": "string", "description": "出片用的英文提示词"},
            "prompt_zh": {"type": "string"},
            "duration_sec": {"type": "number"},
            "code": {"type": "string"},
            "seed": {"type": "integer"},
            "relint": {"type": "boolean", "description": "改完是否重算 lint，默认 true"},
        }, ["shot_id"]),
        handler=_h_update_shot,
    ),
    Tool(
        name="lint_prompt",
        group="分镜",
        description="给任意一条提示词打分（**纯本地、免费、无副作用、不落库**）。\n"
                    "改稿时先用它试，别急着 update_shot。返回总分（满分 14）与逐项扣分理由。",
        schema=_obj({
            "prompt": {"type": "string"},
            "duration": {"type": "number", "description": "该镜时长秒数，默认 3"},
        }, ["prompt"]),
        handler=_h_lint_prompt,
    ),
    # ---- 出片 ----
    Tool(
        name="render_shot",
        group="出片",
        spend=True,
        timeout=120.0,
        description="**提交单个镜头的出片任务（异步）。** 立刻返回 `job_id` 与状态，"
                    "出片本身要跑几十秒到几分钟 —— 拿 job_id 后用 `wait_for_job` 跟进。\n"
                    "`kind=video` 出视频、`kind=image` 出静帧。\n"
                    "**先试算**：`dry_run=true` 只估费用、不提交、不花钱，返回里带预算快照。\n"
                    "**不重复付费**：`reuse=true`（默认）时若该镜已有成功产物，会直接复用旧结果。",
        schema=_obj({
            "shot_id": {"type": "integer"},
            "kind": {"type": "string", "enum": ["video", "image"],
                     "description": "默认 video"},
            "dry_run": {"type": "boolean", "description": "只估费用不提交，默认 false"},
            "reuse": {"type": "boolean", "description": "复用已有产物，默认 true"},
            "duration": {"type": "integer", "description": "视频秒数（仅 video）"},
            "resolution": {"type": "string",
                           "description": "如 480p竖 / 480p横 / 768p竖 / 768p横"},
            "seed": {"type": "integer"},
            "provider_id": {"type": "string", "description": "覆盖当前生效的供应商"},
            "ref_image_paths": {"type": "array", "items": {"type": "string"},
                                "description": "参考图本地绝对路径；不传则用镜头已绑定的"},
            "extra": {"type": "object", "description": "供应商私有参数直传"},
        }, ["shot_id"]),
        handler=_h_render_shot,
    ),
    Tool(
        name="get_job",
        group="出片",
        description="查一次出片任务的状态（`status` / `progress` / `output_path` / "
                    "`cost_cny` / `error`）。不阻塞，适合轮询或事后回看。",
        schema=_obj({"job_id": {"type": "integer"}}, ["job_id"]),
        handler=_h_get_job,
    ),
    Tool(
        name="wait_for_job",
        group="出片",
        description="阻塞等待出片任务到终态（succeeded / failed / reused / dry_run）后返回。\n"
                    "出片通常 30s–3min。**`max_wait` 默认 300s、上限 900s**；超时不算失败，"
                    "任务还在跑，返回里会带 `_timed_out`，稍后再 get_job 即可。",
        schema=_obj({
            "job_id": {"type": "integer"},
            "interval": {"type": "number", "description": "轮询间隔秒，默认 5"},
            "max_wait": {"type": "number", "description": "最长等待秒，默认 300，上限 900"},
        }, ["job_id"]),
        handler=_h_wait_for_job,
        timeout=60.0,
    ),
    # ---- 质检 / 合规 ----
    Tool(
        name="compliance_scan",
        group="质检",
        description="扫一段文案的**广告法风险**（内置 zh / pt-BR / es-MX 三套词表，"
                    "含绝对化用语、医疗功效、虚假紧迫等）。返回命中词、**精确字符区间**、"
                    "分级（high 阻断发布 / medium 需举证 / low 提示）与改写建议。\n"
                    "写念白稿之后、发布之前跑一次；high 级命中会被 `/final` 门禁拦下。",
        schema=_obj({
            "text": {"type": "string", "description": "要扫的文案"},
            "langs": {"type": "array", "items": {"type": "string"},
                      "description": "限定语言，如 [\"pt\"]；不传则用全部"},
            "source": {"type": "string", "description": "来源标注（便于阅读报告）"},
        }, ["text"]),
        handler=_h_compliance_scan,
    ),
    Tool(
        name="qc_project",
        group="质检",
        timeout=900.0,
        description="**逐镜质量门**（解帧分析，慢）：检出黑场 / 冻结帧 / 整镜无运动 / "
                    "亮度跳变 / 重复帧 / **声明了雾却没出现（喷雾无来源）** / "
                    "ROI 内物件数量突变（瓶盖消失）等出片缺陷。\n"
                    "**必须逐镜判** —— 整片平均会把「某一镜根本没动」抹平。\n"
                    "自动判据只用来圈重点，结论要看拼图，所以配合返回的 `sheet` 路径人工复核。",
        schema=_obj({
            "project_id": {"type": "integer"},
            "roi": {"type": "string",
                    "description": "关注区域 x,y,w,h（像素），用于物件数量判据"},
            "prompt": {"type": "string", "description": "声明性元素（如 mist），用于「声明了却没出现」判据"},
            "shot_ids": {"type": "array", "items": {"type": "integer"},
                         "description": "只扫这些镜头；不传则全扫"},
            "persist": {"type": "boolean", "description": "报告是否留档为资产"},
        }, ["project_id"]),
        handler=_h_qc_project,
    ),
    Tool(
        name="verify_delivery",
        group="质检",
        timeout=900.0,
        description="**交付前一次性自检成片**：帧数 / 时长 / 分辨率 / 有无音轨 / 平均电平，"
                    "**并顺带跑广告法合规**（高危则 `ok=false`）。\n"
                    "`qc=true` 会额外逐镜质检（慢很多）。`ok` 为 false 时不要交付，"
                    "按 `issues` 逐条修。",
        schema=_obj({
            "project_id": {"type": "integer"},
            "path": {"type": "string", "description": "成片路径；不传则取最近一次成片"},
            "expected_frames": {"type": "integer", "description": "期望总帧数"},
            "fps": {"type": "number", "description": "默认 24"},
            "qc": {"type": "boolean", "description": "是否附逐镜质检（慢），默认 false"},
            "qc_roi": {"type": "string"},
        }, ["project_id"]),
        handler=_h_verify_delivery,
    ),
    # ---- 后期 ----
    Tool(
        name="run_final",
        group="后期",
        timeout=1200.0,
        description="**一键后期装配**：念白 + BGM + 镜头音效 + 混音全部在本地 ffmpeg 完成，"
                    "**不产生任何外部费用**。缺什么就不做什么（不给 `raw_wav` 就不做念白）。\n"
                    "**带发布前合规门禁**：念白若有高危违规词，会被 422 拦下并点名命中词；"
                    "确要发布可传 `force=true`（会被记进日志，慎用）。",
        schema=_obj({
            "project_id": {"type": "integer"},
            "video_path": {"type": "string", "description": "已拼接的无音轨成片路径"},
            "total_frames": {"type": "integer"},
            "fps": {"type": "number"},
            "rows": {"type": "array", "description": "念白时间轴 [{start,end,text}]",
                     "items": {"type": "object"}},
            "raw_wav": {"type": "string", "description": "已合成的念白音轨路径"},
            "voice": {"type": "string"},
            "specs": {"type": "array", "items": {"type": "string"},
                      "description": "镜头规格（时长/起始），用于音效落位"},
            "sfx_kind": {"type": "string", "description": "默认 mist"},
            "bgm_duration": {"type": "number"},
            "bgm_split": {"type": "number"},
            "bgm_root2": {"type": "string"},
            "auto_sfx": {"type": "boolean"},
            "out_name": {"type": "string"},
            "force": {"type": "boolean", "description": "绕过合规门禁（慎用）"},
        }, ["project_id"]),
        handler=_h_run_final,
    ),
    # ---- 预算 ----
    Tool(
        name="get_budget",
        group="预算",
        description="看**花费上限与余额**的快照：全局 / 项目 / 单次各级上限、周期、"
                    "已花、在途（已提交未入账，**连点提交就能绕过上限，所以必须计入**）、"
                    "以及剩余额度。\n"
                    "**准备调用 render_shot 之前先看它**，避免撞 402。",
        schema=_obj({"project_id": {"type": "integer",
                                    "description": "看该项目维度的余额（可选）"}}),
        handler=_h_get_budget,
    ),
    # ---- 素材 ----
    Tool(
        name="list_assets",
        group="素材",
        description="检索素材库（模特图 / 产品图 / 三视图 / 成片 / 报告）。支持关键词 `q`、"
                    "类型 `kind` 与 `project_id` 过滤。\n"
                    "找「用哪张参考图出片」时用它 —— 拿到路径后传给 render_shot 的 "
                    "`ref_image_paths`。",
        schema=_obj({
            "q": {"type": "string", "description": "关键词（全文检索）"},
            "kind": {"type": "string", "description": "如 image / video / report"},
            "project_id": {"type": "integer"},
            "limit": {"type": "integer"},
        }),
        handler=_h_list_assets,
    ),
    # ---- 反推 ----
    Tool(
        name="reverse_video",
        group="反推",
        spend=True,
        timeout=600.0,
        description="**拆解一条参考片**：本地量测（镜头切分 / 节奏 / 运动）+ LLM 三档判定"
                    "（可直接采纳 / 须改造 / 不可采纳），产出反推分镜与提示词。\n"
                    "入参 `path` 是本地视频绝对路径。用于「照着爆款复刻」的起点。",
        schema=_obj({
            "path": {"type": "string", "description": "本地视频文件绝对路径"},
            "project_id": {"type": "integer", "description": "把结果挂到某项目（可选）"},
        }, ["path"]),
        handler=_h_reverse_video,
    ),
]

TOOLS_BY_NAME: Dict[str, Tool] = {t.name: t for t in TOOLS}

# 分组顺序（写进 initialize 的 instructions，让 agent 一眼看清流水线）
GROUP_ORDER = ["项目", "分镜", "出片", "质检", "后期", "预算", "素材", "反推"]
