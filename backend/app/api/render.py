"""渲染任务 API：单镜出图 / 出片 + 任务监控 + 临时图床收租。

对应 PRD/TDD 里的三条路由：
    POST /api/shots/{id}/render    单镜生图/生视频（**可只重跑某一镜**）
    GET  /api/jobs/{id}            轮询任务状态（前端进度条的数据源）
    POST /api/projects/{id}/hosts/unpublish   兜底下线图床（正常流程会自动下线）

设计要点：
  - **`reuse` 默认开**：该镜已有产物就不再花钱重出（对应既有 `--reuse` 思路）。
  - **`dry_run` 不花钱**：只返回费用预估，不提交平台 —— 用户能先看清代价再决定。
  - **密钥不回显**：`providers/media` 只返回 env 变量名，不返回值。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import deps
from ..db.models import ImageHostLease, Project, RenderJob, Shot
from ..db.session import SessionLocal, get_db
from ..services import budget as budget_mod
from ..services import refs as refs_mod
# 出片核心已抽到 services/render_core.py —— 单镜出片与「一键出片」编排
# **共用同一份实现**（时长跟随分镜 / 自动挂参考图 / 预算预检都不能有第二份）。
from ..services import task_ledger
from ..services.queue import RenderQueue
from ..services.render_core import (ProviderNotReady, UnknownKind,
                                    enqueue_shot_render,
                                    shot_video_duration)  # noqa: F401 - 兼容旧引用
from ..services.render_core import job_out as _job_out

router = APIRouter(prefix="/api", tags=["render"])


class RenderRequest(BaseModel):
    kind: str = "video"  # image / video
    provider_id: Optional[str] = None
    duration: Optional[int] = None
    resolution: Optional[str] = None
    seed: Optional[int] = None
    ref_image_paths: List[str] = Field(default_factory=list)
    dry_run: bool = False
    reuse: bool = True
    extra: Dict[str, Any] = Field(default_factory=dict)


# `_job_out` / `shot_video_duration` / 费用预估 / 时长区间常量 / 参考图解析
# 全部随出片核心移入 services/render_core.py（这里只做 re-export 兼容旧引用）。


@router.post("/shots/{shot_id}/render")
def render_shot(shot_id: int, body: RenderRequest,
                db: Session = Depends(get_db),
                queue: RenderQueue = Depends(deps.get_render_queue),
                media_nodes=Depends(deps.get_media_nodes)):
    shot: Optional[Shot] = db.get(Shot, shot_id)
    if shot is None:
        raise HTTPException(404, f"镜头 {shot_id} 不存在")

    # 供应商解析 / 时长跟随分镜 / 自动挂参考图 / 复用 / 预算预检 / 入队，
    # 全部在 services/render_core.py —— 与「一键出片」编排共用同一份实现。
    cfg = deps.get_providers_config()
    try:
        j, _fut = enqueue_shot_render(
            db, shot,
            cfg=cfg, media_nodes=media_nodes, queue=queue,
            kind=body.kind, duration=body.duration, resolution=body.resolution,
            provider_id=body.provider_id, seed=body.seed,
            ref_image_paths=body.ref_image_paths,
            dry_run=body.dry_run, reuse=body.reuse, extra=body.extra,
        )
    except UnknownKind as e:
        raise HTTPException(400, str(e)) from e
    except ProviderNotReady as e:
        # 供应商本地前提不满足（工作流文件缺失 / 占位地址 / 没令牌）：
        # 提交前就拦下，别排一个注定失败的任务让人等
        raise HTTPException(400, str(e)) from e
    except budget_mod.BudgetError as be:
        # 拦截已在 render_core 里落痕（要留档，但不能污染成片成本）
        raise HTTPException(402, f"花费上限拦截：{be.decision.reason}") from be

    out = _job_out(j)
    if body.dry_run:
        # 试算时顺带把"还能花多少"带回去 —— 用户点 dry-run 通常就是为了看这个
        out["budget"] = budget_mod.snapshot(db, cfg, project_id=shot.project_id)
        # 顺带把参考图的**实际发送顺序**与**被丢掉的图**讲清楚。用户点试算
        # 真正想确认的是"这一镜会拍成什么样"，而 ref_image_0 是首帧、
        # 排序决定观感 —— 不给这个，用户只能对着缩略图猜（踩过）。
        if (j.params or {}).get("refs_source") == "auto":
            d = refs_mod.refs_for_shot_detail(db, shot)
            out["refs"] = {
                "sequence": d["sequence"], "dropped": d["dropped"],
                "dropped_count": len(d["dropped"]), "want_person": d["want_person"],
                "why": d["why"], "max_refs": d["max_refs"],
                "first_frame_note": d["first_frame_note"],
            }
    return out

@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    j: Optional[RenderJob] = db.get(RenderJob, job_id)
    if j is None:
        raise HTTPException(404, f"任务 {job_id} 不存在")
    return _job_out(j)


@router.get("/projects/{pid}/jobs")
def list_jobs(pid: int, db: Session = Depends(get_db)):
    rows = (
        db.query(RenderJob)
        .filter(RenderJob.project_id == pid)
        .order_by(RenderJob.id.desc())
        .all()
    )
    return [_job_out(r) for r in rows]


@router.get("/projects/{pid}/refs")
def project_refs(pid: int, db: Session = Depends(get_db)):
    """项目参考图总览（产品图 + 模特图），给前端预览「出片会喂哪些图」。

    返回的是本地磁盘路径（`path`）；前端用 `/api/file?path=` 转成可预览地址。
    与出片时 `_run` 用的是同一个解析入口（services/refs.py），不会两处漂移。
    """
    if db.get(Project, pid) is None:
        raise HTTPException(404, f"项目 {pid} 不存在")
    return refs_mod.project_refs(db, pid)


def _media_item(c, node) -> dict:
    """单个媒体供应商的对外视图。

    `configured` 过去只判 `token_env or base_url`，于是一个"base_url 填了、
    但工作流文件根本不存在"的 ComfyUI 同样报"已配置"，把人引向错误的排查方向。
    现在改为问供应商自己的 `preflight()` —— **说能跑，就真的能跑**；
    说不能跑，`problem` 里带着一句可执行的原因。
    """
    problem: Optional[str]
    if node is None:
        problem = "适配器未构造（type 是否拼错？）"
    else:
        try:
            problem = node.preflight()
        except Exception as e:  # noqa: BLE001 - 预检自己出错不该把设置页整个打挂
            problem = f"预检异常：{type(e).__name__}: {e}"
    return {
        "id": c.id, "type": c.type,
        "has_base_url": bool(c.base_url),
        "token_env": c.token_env,
        "workflow": c.workflow,
        "configured": problem is None,
        "problem": problem,
    }


@router.get("/providers/media")
def list_media_providers():
    """列出图/视频插槽。**只返回 env 变量名，绝不返回值。**"""
    cfg = deps.get_providers_config()
    img, vid = deps.get_media_nodes()
    _host = deps.get_image_host()
    return {
        "image": {
            "active": cfg.image.active,
            "available": sorted(img.keys()),
            "list": [_media_item(c, img.get(c.id)) for c in cfg.image.list],
        },
        "video": {
            "active": cfg.video.active,
            "available": sorted(vid.keys()),
            "list": [
                {**_media_item(c, vid.get(c.id)),
                 "default_duration": c.default_duration,
                 "default_resolution": c.default_resolution,
                 "cost_per_sec": c.cost_per_sec,
                 "max_concurrency": c.max_concurrency}
                for c in cfg.video.list
            ],
        },
        "image_host_enabled": _host is not None,
        # 图床是"两种实现择一"，UI 得知道是哪一种才说得对话：
        # 静态目录说"用完会截断覆盖"，上传型只能说"由服务方按时效过期"。
        "image_host_mode": getattr(_host, "mode", None) if _host is not None else None,
        "image_host_can_delete": bool(getattr(_host, "supports_delete", False)) if _host else False,
        "image_host_expiry": getattr(_host, "expiry_hint", "") if _host is not None else "",
    }


# ------------------------------------------------------------------ 任务台账（P-2）

class RecoverRequest(BaseModel):
    stale_sec: int = 1800  # 卡在 submitting 超过这么久 → 判为孤儿（进程中断）


@router.get("/render/tasks")
def list_render_tasks(status: Optional[str] = None, project_id: Optional[int] = None,
                      limit: int = 100):
    """渲染任务台账：出片在厂商那边的凭据（task_id / 状态 / 费用 / 是否孤儿）。

    `status=unknown` 能一眼看出"可能已计费但我方不知结果"的孤儿任务。
    """
    return {
        "tasks": task_ledger.list_tasks(SessionLocal, status=status,
                                        project_id=project_id, limit=limit),
        "statuses": ["submitting", "submitted", "running", "succeeded", "failed", "unknown"],
    }


@router.post("/render/tasks/recover")
def recover_render_tasks(body: RecoverRequest = RecoverRequest()):
    """把卡在 `submitting`（提交已发出、未取得 task_id）的过期台账标成 `unknown`。

    **不会自动重提交** —— 那些任务厂商可能已受理并计费，重提交等于重复下单。
    这一步只把它们暴露出来，让用户到平台按时间对账。
    """
    return task_ledger.recover_orphans(SessionLocal, stale_sec=body.stale_sec)


@router.post("/render/tasks/{task_id}/resume")
def resume_render_task(task_id: int,
                       queue: RenderQueue = Depends(deps.get_render_queue)):
    """按已有 `provider_task_id` 重新接上轮询并下载 —— **不重新提交，不重复计费**。

    轮询与下载都不计费，"提交即计费"那一步早已发生；恢复只是把这条命捡回来。
    `submitting`（无 task_id）无法恢复，只能人工对账。
    """
    return queue.resume_task(task_id)


@router.post("/projects/{pid}/hosts/unpublish")
def unpublish_hosts(pid: int, db: Session = Depends(get_db)):
    """兜底：手动关闭某项目全部未下线租约（正常流程会自动下线）。

    这里不删文件，只做截断覆盖，符合"脚本永不 os.remove"的环境约定。
    上传型图床没有删除 API，此时 `note` 会如实说明"只能等过期"。
    """
    host = deps.get_image_host()
    leases = db.query(ImageHostLease).filter(
        ImageHostLease.project_id == pid, ImageHostLease.is_offline == 0
    ).all()

    closed = 0
    for l in leases:
        if host is not None and l.published:
            # 用租约自己记录的 namespace（含 job 维度），不要按项目重新拼，
            # 否则并发隔离的子目录会找不到
            host.unpublish(l.published, namespace=l.namespace or "")
        l.is_offline = 1
        l.offline_at = datetime.utcnow()
        closed += 1
    db.commit()
    note = ""
    if host is not None and not getattr(host, "supports_delete", True):
        note = (f"该图床（{getattr(host, 'mode', '')}）无删除 API：文件仍公网可读，"
                f"由服务方按时效过期（{getattr(host, 'expiry_hint', '') or '时效未知'}）。")
    return {"project_id": pid, "closed_leases": closed, "note": note}
