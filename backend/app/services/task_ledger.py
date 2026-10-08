"""渲染任务台账的读写（P-2）。

纯 DB 操作，不认识任何厂商。状态机：

    submitting --(拿到厂商 task_id)--> submitted --> running --> succeeded
        |                                              └-----> failed
        └--(进程中断，无法对账)------------------------> unknown

"提交即落库"的关键在 `begin_submit`：**发请求之前**先把 `submitting` 行写下去，
这样即使进程在"请求已发出、响应还没回来"之间崩溃，也留下一笔"我们确实发过这一单"
的记录，重启后可以被 `recover_orphans` 找出来提示人工对账，而不是静默丢失。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..db.models import RenderTask

TERMINAL = ("succeeded", "failed")
# 仍在"厂商那边可能有活"的状态 —— 恢复/巡检都盯这些
OPEN = ("submitting", "submitted", "running")


def new_client_ref() -> str:
    """本地关联串：提交前就能生成，用于审计"我们确实发过这一单"。"""
    return "avs-" + uuid.uuid4().hex[:12]


def begin_submit(s: Session, *, job, provider_id: str,
                 estimated_cny: Optional[float] = None) -> RenderTask:
    """**提交之前**落一行 submitting。调用方随后 commit。

    `estimated_cny` 一并存进 `meta`：这笔在途的钱还没进 `cost_record`
    （要等下载成功才入账），但预算熔断（P-5）必须把它算进"已花"，
    否则连点三次提交就能绕过上限。拿不到预估就留空，不猜。
    """
    meta: Dict[str, Any] = {}
    if estimated_cny is not None:
        meta["estimated_cny"] = round(float(estimated_cny), 4)
    t = RenderTask(
        job_id=getattr(job, "id", None),
        project_id=getattr(job, "project_id", None),
        kind=getattr(job, "kind", ""),
        provider_id=provider_id,
        client_ref=new_client_ref(),
        status="submitting",
        attempt=1,
        message="提交中（未取得 task_id）",
        meta=meta,
    )
    s.add(t)
    s.flush()  # 拿到 id，但不提交事务 —— 由调用方与 job 一起 commit
    return t


def mark_submitted(s: Session, t: RenderTask, provider_task_id: str,
                   message: str = "已提交") -> None:
    t.provider_task_id = str(provider_task_id or "")
    t.status = "submitted"
    t.submitted_at = datetime.utcnow()
    t.message = message
    s.flush()


def mark_running(s: Session, t: RenderTask, message: str = "") -> None:
    if t.status != "running":
        t.status = "running"
    if message:
        t.message = message[:1000]
    s.flush()


def mark_terminal(s: Session, t: RenderTask, status: str, *,
                  cost_cny: Optional[float] = None, message: str = "") -> None:
    t.status = status
    if cost_cny is not None:
        t.cost_cny = float(cost_cny)
    if message:
        t.message = message[:1000]
    t.resolved_at = datetime.utcnow()
    s.flush()


def mark_unknown(s: Session, t: RenderTask, message: str) -> None:
    t.status = "unknown"
    t.message = message[:1000]
    t.resolved_at = datetime.utcnow()
    s.flush()


def task_out(t: RenderTask) -> Dict[str, Any]:
    return {
        "id": t.id,
        "job_id": t.job_id,
        "project_id": t.project_id,
        "kind": t.kind,
        "provider_id": t.provider_id,
        "client_ref": t.client_ref,
        "provider_task_id": t.provider_task_id,
        "status": t.status,
        "attempt": t.attempt,
        "cost_cny": t.cost_cny,
        "submitted_at": t.submitted_at.isoformat() if t.submitted_at else None,
        "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
        "message": t.message,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


# --------------------------------------------------------------------------- #
# 会话工厂级助手（供 API / 启动巡检调用）
# --------------------------------------------------------------------------- #

def list_tasks(session_factory, *, status: Optional[str] = None,
               project_id: Optional[int] = None,
               limit: int = 100) -> List[Dict[str, Any]]:
    s = session_factory()
    try:
        q = s.query(RenderTask)
        if status:
            q = q.filter(RenderTask.status == status)
        if project_id is not None:
            q = q.filter(RenderTask.project_id == project_id)
        rows = q.order_by(RenderTask.id.desc()).limit(max(1, int(limit))).all()
        return [task_out(t) for t in rows]
    finally:
        s.close()


def recover_orphans(session_factory, *, stale_sec: int = 1800) -> Dict[str, Any]:
    """把**卡在 `submitting` 且已过期**的台账标成 `unknown`。

    这些行意味着：进程在"提交已发出、task_id 未取得"之间中断了。
    厂商那边可能建了任务（并计了费），我们却无从得知 —— 所以**不重提交**，
    只标记出来让用户到平台核对。`submitted`/`running` 有 task_id，不算孤儿，
    可走 `resume_task` 恢复轮询。
    """
    s = session_factory()
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=int(stale_sec))
        rows = (s.query(RenderTask)
                .filter(RenderTask.status == "submitting").all())
        marked = 0
        for t in rows:
            ts = t.updated_at or t.created_at
            if ts is not None and ts > cutoff:
                continue
            mark_unknown(
                s, t,
                "进程中断：提交已发出但未取得厂商 task_id，无法判断是否已计费，"
                "请到平台按时间核对任务列表",
            )
            marked += 1
        s.commit()
        return {"scanned": len(rows), "marked_unknown": marked,
                "stale_sec": int(stale_sec)}
    finally:
        s.close()
