"""一键出片 API：脚本 → 落镜 → 逐镜出片 → 15s 成片。

三条端点，各自对应一个**必须存在**的产品动作：

    POST /api/projects/{pid}/produce          正式出片（后台线程跑，立即返 run_id）
                                              或 `dry_run=true` 只出规划
    POST /api/projects/{pid}/produce/plan     只规划：几镜 / 几秒 / 多少钱 / 缺什么
    GET  /api/projects/{pid}/produce          该项目的历史运行（列表）
    GET  /api/projects/{pid}/produce/{run_id} 轮询：状态 + 逐步骤 + 报告 + 成片路径
    POST /api/projects/{pid}/produce/{run_id}/retry
                                              重试：**只补没出片的镜头**，已有的复用不重复计费

设计取向（与 `api/batch.py` 一致）：

  - **规划永不花钱**：`plan` 不落库、不联网、不产出。先让人看清代价再决定。
  - **执行走后台线程**：出片是分钟级操作，HTTP 请求不该陪着等。立即返回 run_id，
    前端轮询 `GET .../produce/{run_id}`。
  - **断点续跑判据落在磁盘**：镜头已有产物且文件还在 → 跳过，不重花钱
    （`reuse=True` 是默认值，也正是「重试只补缺的那几镜」的实现方式）。
  - **重试复用同一条 run**：前端不必换 id，失败补齐后轮询的还是原来那条。

路由注册顺序
------------
`plan` / `retry` 这些**静态后缀必须注册在 `{run_id}` 之前** —— 否则
`/produce/plan` 会被 `{run_id}` 吞掉（`run_id: int` 解析 "plan" 失败 → 422）。
本文件刻意在源码里把静态路径排前面。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import deps
from ..db.models import ProduceRun, Project
from ..db.session import get_db
from ..pipeline.lint import DEFAULT_MIN_SCORE
from ..services.produce import (ProduceOrchestrator, ProduceRequest,
                                read_report, write_report)

router = APIRouter(prefix="/api/projects", tags=["produce"])

# 一键出片一次只跑一个：出片账户有并发限制，且一次出片本身就要花钱，
# 排队比并行更省（也更不容易超时）
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="produce")


class ProduceIn(BaseModel):
    """出片入参。`script` 为空 = 用项目里**已有**的分镜（重试就走这条）。"""

    script: Optional[str] = None
    source: str = "agent"           # agent / paste —— 脚本素材溯源
    title: Optional[str] = None
    min_score: int = DEFAULT_MIN_SCORE
    language: Optional[str] = None  # 不传则用项目语言
    allow_silent: bool = False      # 无念白默认拒绝，勾了才放行
    force: bool = False             # 放行合规高危（默认拦）
    fps: float = 24.0
    out_name: str = "final.mp4"
    render_timeout_sec: float = 1800.0
    reuse: bool = True              # 已有产物的镜头是否复用（关掉 = 全部重出）
    dry_run: bool = False           # 只规划，不落库不花钱


def _to_req(body: ProduceIn, *, script: Optional[str] = "") -> ProduceRequest:
    return ProduceRequest(
        script=script if script is not None else body.script,
        source=body.source, title=body.title, min_score=body.min_score,
        language=body.language, allow_silent=body.allow_silent, force=body.force,
        fps=body.fps, out_name=body.out_name,
        render_timeout_sec=body.render_timeout_sec, reuse=body.reuse,
    )


def _run_out(r: ProduceRun) -> Dict[str, Any]:
    rep = r.report or {}
    return {
        "id": r.id,
        "project_id": r.project_id,
        "status": r.status,
        "stage": r.stage,
        "message": r.message or "",
        "script_asset_id": r.script_asset_id,
        "plan": r.plan or {},
        "steps": r.steps or [],
        "report": rep,
        # 成片路径从报告里提上来 —— 前端轮询时最需要的就是它
        "final_path": rep.get("final_path"),
        "final": rep.get("final") or {},
        "cost_estimate_cny": round(float(r.cost_estimate_cny or 0.0), 4),
        "cost_cny": round(float(r.cost_cny or 0.0), 4),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _project(pid: int, db: Session) -> Project:
    p: Optional[Project] = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, f"项目 {pid} 不存在")
    return p


def _get_run(pid: int, run_id: int, db: Session) -> ProduceRun:
    r: Optional[ProduceRun] = db.get(ProduceRun, run_id)
    if r is None or r.project_id != pid:
        raise HTTPException(404, f"出片运行 {run_id} 不存在")
    return r


def _submit(orch: ProduceOrchestrator, run_id: int, req: ProduceRequest) -> None:
    """把 `orch.run` 丢进后台线程，并把报告落盘。

    报告必须写盘：后台线程的返回值送不到前端，且 `report` 可能很大
    （逐镜产出 + 分步耗时），不适合每次轮询都从 DB JSON 里整坨读。
    """

    def _job() -> None:
        try:
            report = orch.run(run_id, req)
            write_report(orch.work_root, run_id, report or {})
        except Exception as e:  # noqa: BLE001 - 后台线程的异常不能静默
            s = orch.session_factory()
            try:
                row = s.get(ProduceRun, run_id)
                if row is not None:
                    row.status = "failed"
                    row.stage = "done"
                    row.message = f"{type(e).__name__}: {e}"[:500]
                    s.commit()
            finally:
                s.close()

    _executor.submit(_job)


# --------------------------------------------------------------------------- #
# 只规划（静态路径，必须排在 `{run_id}` 之前）
# --------------------------------------------------------------------------- #

@router.post("/{pid}/produce/plan")
def plan_project(pid: int, body: ProduceIn, db: Session = Depends(get_db),
                 orch: ProduceOrchestrator = Depends(deps.get_produce)):
    """**不花钱、不联网、不落库**：这次出片会出几镜、每镜几秒、大概多少钱、缺什么。"""
    _project(pid, db)
    try:
        out = orch.plan(db, pid, _to_req(body))
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return {**out, "dry_run": True}


@router.post("/{pid}/produce/{run_id}/retry")
def retry_run(pid: int, run_id: int, body: Optional[ProduceIn] = None,
              db: Session = Depends(get_db),
              orch: ProduceOrchestrator = Depends(deps.get_produce)):
    """重试：**复用同一条 run**，只补没出片的镜头。

    为什么不需要重传脚本：上一次已经落镜了，镜头就在库里；`reuse=True` 会让
    已有产物的镜头直接跳过 —— 于是"重试"天然只补缺的那几镜，不重复花钱。
    上一次出片时记录的入参（fps / 时长上限 / 合规放行…）从 `plan.request` 里读回。
    """
    r = _get_run(pid, run_id, db)
    if r.status == "running":
        raise HTTPException(409, "这条出片正在跑，等它结束再重试")

    prev = ((r.plan or {}).get("request") or {})
    body = body or ProduceIn()
    req = ProduceRequest(
        script=None,  # 走库里已有的镜头
        source=prev.get("source", body.source),
        title=prev.get("title", body.title),
        min_score=int(prev.get("min_score", body.min_score)),
        language=prev.get("language", body.language),
        allow_silent=bool(prev.get("allow_silent", body.allow_silent)),
        force=bool(prev.get("force", body.force)),
        fps=float(prev.get("fps", body.fps)),
        out_name=prev.get("out_name", body.out_name),
        render_timeout_sec=float(prev.get("render_timeout_sec",
                                          body.render_timeout_sec)),
        reuse=True,  # 重试**总是**复用已有产物：这是"只补缺的"的实现方式
    )

    # 清掉上一轮的步骤与报告，让前端看到一条干净的进度
    r.steps = []
    r.report = {}
    r.status = "queued"
    r.stage = "script"
    r.message = "已重新提交"
    db.commit()

    _submit(orch, run_id, req)
    db.refresh(r)
    return {**_run_out(r), "poll": f"/api/projects/{pid}/produce/{run_id}"}


# --------------------------------------------------------------------------- #
# 正式出片
# --------------------------------------------------------------------------- #

@router.post("/{pid}/produce")
def start_produce(pid: int, body: ProduceIn, db: Session = Depends(get_db),
                  orch: ProduceOrchestrator = Depends(deps.get_produce)):
    """出片。`dry_run=true` 只返回规划；否则建一条运行记录并**立即返回** run_id。"""
    _project(pid, db)

    if body.dry_run:
        try:
            out = orch.plan(db, pid, _to_req(body))
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        return {**out, "dry_run": True}

    # 同一项目同时只允许一条在跑：两次点击 = 两笔钱
    busy = (db.query(ProduceRun)
            .filter(ProduceRun.project_id == pid,
                    ProduceRun.status.in_(("queued", "running")))
            .order_by(ProduceRun.id.desc()).first())
    if busy is not None:
        raise HTTPException(409, f"项目 {pid} 已有出片在跑（run {busy.id}），等它结束")

    req = _to_req(body)
    r = ProduceRun(project_id=pid, status="queued", stage="script",
                   message="已提交后台执行")
    db.add(r)
    db.commit()
    db.refresh(r)

    _submit(orch, r.id, req)
    return {**_run_out(r), "poll": f"/api/projects/{pid}/produce/{r.id}"}


@router.get("/{pid}/produce")
def list_runs(pid: int, limit: int = 20, db: Session = Depends(get_db)):
    """该项目的历史出片运行（最新在前）。"""
    _project(pid, db)
    rows = (db.query(ProduceRun).filter(ProduceRun.project_id == pid)
            .order_by(ProduceRun.id.desc()).limit(max(1, min(limit, 100))).all())
    return [_run_out(r) for r in rows]


@router.get("/{pid}/produce/{run_id}")
def get_run(pid: int, run_id: int, db: Session = Depends(get_db),
            orch: ProduceOrchestrator = Depends(deps.get_produce)):
    """轮询入口：状态 + 逐步骤 + 成片路径 + 落盘的完整报告。

    报告优先读磁盘（后台线程写的），读不到再退回 DB 里那份 —— 两者内容一致，
    但磁盘那份不会因为 DB JSON 体积上限被截断。
    """
    r = _get_run(pid, run_id, db)
    out = _run_out(r)
    disk = read_report(orch.work_root, run_id)
    if disk:
        out["report"] = disk
        out["final_path"] = disk.get("final_path") or out["final_path"]
        out["final"] = disk.get("final") or out["final"]
    return out
