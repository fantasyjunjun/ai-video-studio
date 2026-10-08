"""批量变体 API（P5）：一份 manifest，跑 N 份成片。

设计取向（与技能里 `batch_render.py` 一致，但做成服务）：

  - **`plan` 永不花钱**：先给矩阵与费用，让人看清代价再决定。
  - **`run` 走后台线程**：出片是分钟级操作，HTTP 请求不该陪着等。
    立即返回 run_id，前端轮询 `GET /api/batch/{id}`。
  - **断点续跑判据落在 task 表**：做过且文件还在 → 跳过，不重花钱。
  - **dry_run 走渲染队列的试算分支**，不提交平台。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import deps
from ..db.models import BatchRun, BatchTask, Project
from ..db.session import get_db
from ..services.batch import (BatchManifest, BatchOrchestrator,
                              manifest_from_project)

router = APIRouter(prefix="/api/batch", tags=["batch"])

# 批量一次只跑一个：出片账户有并发限制，排队比并行更省（也更不容易超时）
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="batch")


class CreateIn(BaseModel):
    name: str = "batch"
    project_id: Optional[int] = None
    manifest: Dict[str, Any] = Field(default_factory=dict)


class ManifestIn(BaseModel):
    manifest: Dict[str, Any]


class RunIn(BaseModel):
    dry_run: bool = False
    only_units: List[str] = Field(default_factory=list)
    only_langs: List[str] = Field(default_factory=list)


def _validate(raw: Dict[str, Any]) -> BatchManifest:
    try:
        return BatchManifest(**(raw or {}))
    except Exception as e:  # noqa: BLE001 - pydantic 的报错已经是人话，直接回传
        raise HTTPException(400, f"manifest 不合法：{e}") from e


def _run_out(r: BatchRun) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "project_id": r.project_id,
        "status": r.status,
        "message": r.message,
        "cost_cny": round(float(r.cost_cny or 0.0), 4),
        "work_dir": r.work_dir,
        "plan": r.plan_json or {},
        "manifest": r.manifest_json or {},
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _task_out(t: BatchTask) -> dict:
    return {
        "id": t.id, "run_id": t.run_id, "kind": t.kind, "key": t.key,
        "status": t.status, "output_path": t.output_path, "message": t.message,
        "cost_cny": round(float(t.cost_cny or 0.0), 4), "meta": t.meta or {},
    }


def _get(run_id: int, db: Session) -> BatchRun:
    r: Optional[BatchRun] = db.get(BatchRun, run_id)
    if r is None:
        raise HTTPException(404, f"批量任务 {run_id} 不存在")
    return r


@router.post("")
def create(body: CreateIn, db: Session = Depends(get_db),
           orch: BatchOrchestrator = Depends(deps.get_batch)):
    """建一条批量运行。**创建即出计划**（计划不花钱）。"""
    if body.project_id is not None and db.get(Project, body.project_id) is None:
        raise HTTPException(404, f"项目 {body.project_id} 不存在")
    m = _validate(body.manifest)
    r = BatchRun(name=body.name, project_id=body.project_id,
                 manifest_json=body.manifest, status="planned")
    db.add(r)
    db.commit()
    db.refresh(r)
    r.plan_json = orch.plan(m)
    r.work_dir = str(orch.paths(r.id).root)
    db.commit()
    db.refresh(r)
    return _run_out(r)


@router.get("")
def list_runs(db: Session = Depends(get_db)):
    rows = db.query(BatchRun).order_by(BatchRun.id.desc()).all()
    return [_run_out(r) for r in rows]


@router.get("/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    """轮询入口：状态 + 逐条 task + 落盘的完整报告。

    run 在后台线程里跑，返回体送不到前端，所以报告写成 `work_dir/report.json`，
    这里读出来一并返回 —— 前端只轮询这一个端点就能拿到全部信息。
    """
    r = _get(run_id, db)
    out = _run_out(r)
    out["tasks"] = [_task_out(t) for t in
                    db.query(BatchTask).filter(BatchTask.run_id == run_id)
                    .order_by(BatchTask.id.asc()).all()]
    rp = Path(r.work_dir or "") / "report.json"
    if r.work_dir and rp.exists():
        try:
            out["report"] = json.loads(rp.read_text(encoding="utf-8"))
        except ValueError:
            out["report"] = None
    return out


@router.put("/{run_id}/manifest")
def set_manifest(run_id: int, body: ManifestIn, db: Session = Depends(get_db),
                 orch: BatchOrchestrator = Depends(deps.get_batch)):
    """改工作流源。**改完立刻重算计划**，免得拿着旧预算去跑。"""
    r = _get(run_id, db)
    m = _validate(body.manifest)
    r.manifest_json = body.manifest
    r.plan_json = orch.plan(m)
    r.status = "planned"
    db.commit()
    db.refresh(r)
    return _run_out(r)


@router.post("/{run_id}/plan")
def replan(run_id: int, db: Session = Depends(get_db),
           orch: BatchOrchestrator = Depends(deps.get_batch)):
    r = _get(run_id, db)
    r.plan_json = orch.plan(_validate(r.manifest_json or {}))
    db.commit()
    return r.plan_json


@router.post("/{run_id}/run")
def run_batch(run_id: int, body: RunIn, db: Session = Depends(get_db),
              orch: BatchOrchestrator = Depends(deps.get_batch)):
    """执行。**立即返回**，出片在后台线程跑（分钟级，HTTP 不该陪着等）。"""
    r = _get(run_id, db)
    if r.status == "running":
        raise HTTPException(409, "这个批量任务正在跑，等它结束再发起")
    m = _validate(r.manifest_json or {})
    r.status = "running"
    r.message = "已提交后台执行"
    db.commit()

    def _job():
        try:
            orch.run(run_id, m,
                     only_units=list(body.only_units) or None,
                     only_langs=list(body.only_langs) or None,
                     dry_run=body.dry_run)
        except Exception as e:  # noqa: BLE001 - 后台线程的异常不能静默
            s = orch.session_factory()
            try:
                row = s.get(BatchRun, run_id)
                if row is not None:
                    row.status = "failed"
                    row.message = f"{type(e).__name__}: {e}"[:500]
                    s.commit()
            finally:
                s.close()

    _executor.submit(_job)
    return {"run_id": run_id, "status": "running", "dry_run": body.dry_run,
            "poll": f"/api/batch/{run_id}"}


@router.delete("/{run_id}")
def delete_run(run_id: int, db: Session = Depends(get_db)):
    """只删记录，**磁盘产物保留**（与本仓库"删除只删元数据"的约定一致）。"""
    r = _get(run_id, db)
    db.query(BatchTask).filter(BatchTask.run_id == run_id).delete()
    db.delete(r)
    db.commit()
    return {"deleted": run_id, "note": "磁盘产物保留"}


@router.get("/projects/{pid}/template")
def template(pid: int, db: Session = Depends(get_db)):
    """把已跑通的项目导出成 manifest 初稿（视觉单元一个，念白待填）。"""
    if db.get(Project, pid) is None:
        raise HTTPException(404, f"项目 {pid} 不存在")
    return manifest_from_project(db, pid)
