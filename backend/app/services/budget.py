"""花费上限熔断（P-5）。

为什么需要它：
    出片是**非幂等的计费操作**（提交即扣钱），而批量变体、重跑单镜、
    dry-run 试算……任何一处忘了卡上限，钱就出去了。所以把判据收敛成
    **一个纯函数** `check()`，所有提交入口（单镜渲染 / 批量编排 / 队列二次守卫）
    都只调它，不各写各的。

三层上限，任一触发即拒（0 = 不限）：
    per_task  单次提交预估费用      —— "手滑保护"，最直接
    provider  该供应商累计花费      —— 一个中转站烧穿了就换别家
    project   单项目累计花费        —— 一个 campaign 的预算
    global    全局（按 period）累计 —— 月度/单次充值总额

两个容易漏的点，这里都算进去了：
  1. **在途要计**：已提交未下载的任务（`render_task` 里 submitting/submitted/running）
     钱迟早要花；`unknown` 更糟 —— 钱**可能已经花了**却查不到。
     两类的预估费用在提交时写进台账 `meta.estimated_cny`，这里一并计入。
  2. **预估要留余量**：平台报价常偏低（高峰时段、加时长、重试换 key），
     `reserve_ratio` 给一份安全垫，默认 1.0（不留）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db.models import CostRecord, Project, RenderTask

# 被熔断拦下的记录用这个 kind 落进 cost_record —— 金额恒为 0，
# 但"什么时候、因为哪一级上限、拦掉了多大一笔"都留痕，便于事后调参。
BLOCK_KIND = "budget_block"

OPEN_STATUSES = ("submitting", "submitted", "running")
UNKNOWN_STATUS = "unknown"


class BudgetError(RuntimeError):
    """预估费用会越过某一级上限。携带 `decision` 供上层原样回显。"""

    def __init__(self, decision: "BudgetDecision") -> None:
        super().__init__(decision.reason)
        self.decision = decision


@dataclass
class BudgetDecision:
    allowed: bool
    scope: str = "ok"          # ok/disabled/per_task/provider/project/global
    reason: str = ""
    raw_estimate_cny: float = 0.0
    estimate_cny: float = 0.0  # 已乘 reserve_ratio
    cap_cny: float = 0.0       # 触发的那一级上限
    committed_cny: float = 0.0  # 触发那一级的已花 + 在途
    remaining_cny: Optional[float] = None
    on_exceed: str = "block"
    warned: bool = False
    checks: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return (not self.allowed) and self.on_exceed == "block"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "blocking": self.blocking,
            "warned": self.warned,
            "scope": self.scope,
            "reason": self.reason,
            "raw_estimate_cny": round(self.raw_estimate_cny, 4),
            "estimate_cny": round(self.estimate_cny, 4),
            "cap_cny": round(self.cap_cny, 4),
            "committed_cny": round(self.committed_cny, 4),
            "remaining_cny": (None if self.remaining_cny is None
                              else round(self.remaining_cny, 4)),
            "on_exceed": self.on_exceed,
            "checks": self.checks,
        }


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

def period_start(period: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """统计窗口起点。`total` 返回 None（=有史以来）。"""
    now = now or datetime.utcnow()
    if period == "total":
        return None
    if period == "rolling_24h":
        return now - timedelta(hours=24)
    if period == "daily":
        return datetime(now.year, now.month, now.day)
    if period == "monthly":
        return datetime(now.year, now.month, 1)
    raise ValueError(f"未知 period: {period!r}")


def _cost_query(session: Session, *, since: Optional[datetime] = None,
                project_id: Optional[int] = None,
                provider_id: Optional[str] = None):
    q = session.query(CostRecord).filter(CostRecord.kind != BLOCK_KIND)
    if since is not None:
        q = q.filter(CostRecord.created_at >= since)
    if project_id is not None:
        q = q.filter(CostRecord.project_id == project_id)
    if provider_id is not None:
        q = q.filter(CostRecord.provider_id == provider_id)
    return q


def spent_cny(session: Session, *, since: Optional[datetime] = None,
              project_id: Optional[int] = None,
              provider_id: Optional[str] = None) -> float:
    """窗口内**已入账**的花费（成本台账口径）。"""
    total = _cost_query(session, since=since, project_id=project_id,
                        provider_id=provider_id).with_entities(
        func.coalesce(func.sum(CostRecord.amount_cny), 0.0)).scalar()
    return float(total or 0.0)


def pending_cny(session: Session, *, since: Optional[datetime] = None,
                project_id: Optional[int] = None,
                provider_id: Optional[str] = None) -> float:
    """**在途 + 未知**任务的预估费用。

    这些还没进 `cost_record`（尚未下载成功），但钱已经（或即将）花出去。
    金额取提交时写进台账 `meta.estimated_cny` 的预估 —— 拿不到就按 0 计，
    不猜。`unknown` 一定计入（它正是"可能已扣费"的那一类）。
    """
    q = session.query(RenderTask).filter(
        RenderTask.status.in_(OPEN_STATUSES + (UNKNOWN_STATUS,)))
    if since is not None:
        q = q.filter(RenderTask.created_at >= since)
    if project_id is not None:
        q = q.filter(RenderTask.project_id == project_id)
    if provider_id is not None:
        q = q.filter(RenderTask.provider_id == provider_id)
    total = 0.0
    for t in q.all():
        meta = t.meta or {}
        try:
            total += float(meta.get("estimated_cny") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def committed_cny(session: Session, *, count_pending: bool = True,
                  since: Optional[datetime] = None,
                  project_id: Optional[int] = None,
                  provider_id: Optional[str] = None) -> Dict[str, float]:
    """已花 / 在途 / 合计。"""
    sp = spent_cny(session, since=since, project_id=project_id,
                   provider_id=provider_id)
    pe = (pending_cny(session, since=since, project_id=project_id,
                      provider_id=provider_id) if count_pending else 0.0)
    return {"spent_cny": round(sp, 4), "pending_cny": round(pe, 4),
            "committed_cny": round(sp + pe, 4)}


# --------------------------------------------------------------------------- #
# 熔断判据
# --------------------------------------------------------------------------- #

def _provider_cap(cfg, kind: str, provider_id: Optional[str]) -> float:
    if not provider_id:
        return 0.0
    try:
        if kind == "video":
            c = cfg.video_by_id(provider_id)
        elif kind == "image":
            c = cfg.image_by_id(provider_id)
        else:
            c = cfg.llm_by_id(provider_id)
    except Exception:  # noqa: BLE001 - 配置缺 slot 时报错不该炸掉熔断
        return 0.0
    return float(getattr(c, "spend_cap_cny", 0.0) or 0.0) if c else 0.0


def _mk_check(name: str, cap: float, committed: float,
              estimate: float, label: str) -> Dict[str, Any]:
    remain = None if cap <= 0 else round(cap - committed, 4)
    over = bool(cap > 0 and committed + estimate > cap + 1e-9)
    return {
        "scope": name,
        "label": label,
        "cap_cny": round(cap, 4),
        "committed_cny": round(committed, 4),
        "estimate_cny": round(estimate, 4),
        "remaining_cny": remain,
        "would_exceed": over,
    }


def check(session: Session, cfg, *, project_id: Optional[int] = None,
          provider_id: Optional[str] = None, kind: str = "video",
          raw_estimate_cny: float = 0.0,
          now: Optional[datetime] = None) -> BudgetDecision:
    """**唯一的熔断判据**：这笔预估费用现在能不能提交。

    纯读操作，不改库；上层据此拒绝或放行（`enforce` 是它的抛异常包装）。
    """
    b = cfg.budget
    ratio = float(b.reserve_ratio or 1.0)
    est = max(0.0, float(raw_estimate_cny or 0.0)) * ratio
    d = BudgetDecision(allowed=True, raw_estimate_cny=float(raw_estimate_cny or 0.0),
                       estimate_cny=est, on_exceed=b.on_exceed)

    if not b.enabled:
        d.scope, d.reason = "disabled", "花费上限未启用"
        d.checks.append(_mk_check("disabled", 0.0, 0.0, est, "花费上限"))
        return d

    since = period_start(b.period, now)
    count_pending = bool(b.count_pending)

    g = committed_cny(session, count_pending=count_pending, since=since)
    p = (committed_cny(session, count_pending=count_pending, since=since,
                       project_id=project_id) if project_id else
         {"spent_cny": 0.0, "pending_cny": 0.0, "committed_cny": 0.0})
    v = (committed_cny(session, count_pending=count_pending, since=since,
                       provider_id=provider_id) if provider_id else
         {"spent_cny": 0.0, "pending_cny": 0.0, "committed_cny": 0.0})

    checks = [
        _mk_check("per_task", float(b.per_task_cap_cny or 0.0), 0.0, est,
                  "单次提交上限"),
        _mk_check("provider", _provider_cap(cfg, kind, provider_id),
                  v["committed_cny"], est, "该供应商累计上限"),
        _mk_check("project", float(b.project_cap_cny or 0.0),
                  p["committed_cny"], est, "单项目累计上限"),
        _mk_check("global", float(b.global_cap_cny or 0.0),
                  g["committed_cny"], est, "全局累计上限"),
    ]
    d.checks = checks

    violated = next((c for c in checks if c["would_exceed"]), None)
    if violated is None:
        # 未越界：把最紧的那一级余额报出来（谁先到头谁最有参考价值）
        tight = [c for c in checks if c["cap_cny"] > 0]
        d.scope = "ok"
        d.reason = "在花费上限内"
        if tight:
            best = min(tight, key=lambda c: c["remaining_cny"] or 0.0)
            d.cap_cny = best["cap_cny"]
            d.committed_cny = best["committed_cny"]
            d.remaining_cny = best["remaining_cny"]
        return d

    d.allowed = False
    d.scope = violated["scope"]
    d.cap_cny = violated["cap_cny"]
    d.committed_cny = violated["committed_cny"]
    d.remaining_cny = violated["remaining_cny"]
    d.reason = (
        f"{violated['label']}：本次预估 ¥{est:.2f}（含 {ratio:g}× 预留），"
        f"已用 ¥{violated['committed_cny']:.2f} / 上限 ¥{violated['cap_cny']:.2f}，"
        f"余额 ¥{max(0.0, violated['remaining_cny'] or 0.0):.2f}"
    )
    if b.on_exceed == "warn":
        d.allowed = True
        d.warned = True
        d.reason = "（仅告警，未拦截）" + d.reason
    return d


def enforce(session: Session, cfg, **kw) -> BudgetDecision:
    """`check` + 越界即抛 `BudgetError`（`on_exceed=block` 时）。"""
    d = check(session, cfg, **kw)
    if d.blocking:
        raise BudgetError(d)
    return d


def record_block(session: Session, *, project_id: Optional[int], kind: str,
                 provider_id: Optional[str], decision: BudgetDecision,
                 note: str = "") -> CostRecord:
    """把一次被拦下的提交写进成本台账。金额 0，备注写明拦在哪一级。"""
    meta: Dict[str, Any] = {
        "budget_block": True,
        "scope": decision.scope,
        "reason": decision.reason,
        "estimate_cny": round(decision.estimate_cny, 4),
        "raw_estimate_cny": round(decision.raw_estimate_cny, 4),
        "cap_cny": round(decision.cap_cny, 4),
        "committed_cny": round(decision.committed_cny, 4),
        "checks": decision.checks,
    }
    if note:
        meta["note"] = note
    rec = CostRecord(project_id=project_id, kind=BLOCK_KIND,
                     provider_id=provider_id or "", amount_cny=0.0,
                     quantity=0.0, meta=meta)
    session.add(rec)
    session.flush()
    return rec


# --------------------------------------------------------------------------- #
# 快照（给设置页 / 交付页显示）
# --------------------------------------------------------------------------- #

def _recent_blocks(session: Session, limit: int = 5) -> List[Dict[str, Any]]:
    rows = (session.query(CostRecord)
            .filter(CostRecord.kind == BLOCK_KIND)
            .order_by(CostRecord.id.desc()).limit(max(1, int(limit))).all())
    return [{
        "id": r.id,
        "project_id": r.project_id,
        "provider_id": r.provider_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "scope": (r.meta or {}).get("scope"),
        "reason": (r.meta or {}).get("reason"),
        "estimate_cny": (r.meta or {}).get("estimate_cny"),
    } for r in rows]


def _open_tasks(session: Session, limit: int = 20) -> List[Dict[str, Any]]:
    rows = (session.query(RenderTask)
            .filter(RenderTask.status.in_(OPEN_STATUSES + (UNKNOWN_STATUS,)))
            .order_by(RenderTask.id.desc()).limit(max(1, int(limit))).all())
    return [{
        "id": r.id,
        "project_id": r.project_id,
        "provider_id": r.provider_id,
        "kind": r.kind,
        "status": r.status,
        "estimated_cny": (r.meta or {}).get("estimated_cny"),
        "provider_task_id": r.provider_task_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


def snapshot(session: Session, cfg, *, project_id: Optional[int] = None,
             now: Optional[datetime] = None) -> Dict[str, Any]:
    """当前花费状况 + 各级上限。前端据此显示"还能花多少"。"""
    b = cfg.budget
    since = period_start(b.period, now)
    g = committed_cny(session, count_pending=b.count_pending, since=since)

    by_project = [
        {"project_id": pid, "name": name, **committed_cny(
            session, count_pending=b.count_pending, since=since, project_id=pid)}
        for pid, name in (session.query(Project.id, Project.name)
                          .order_by(Project.id).all())
    ]
    by_project = [r for r in by_project if r["committed_cny"] > 0]

    by_provider: Dict[str, float] = {}
    for pid, amt in (session.query(CostRecord.provider_id,
                                   func.coalesce(func.sum(CostRecord.amount_cny), 0.0))
                     .filter(CostRecord.kind != BLOCK_KIND)
                     .group_by(CostRecord.provider_id).all()):
        by_provider[pid or "-"] = round(float(amt or 0.0), 4)

    caps = {
        "llm": {c.id: float(c.spend_cap_cny or 0.0) for c in cfg.llm.list},
        "image": {c.id: float(c.spend_cap_cny or 0.0) for c in cfg.image.list},
        "video": {c.id: float(c.spend_cap_cny or 0.0) for c in cfg.video.list},
    }

    out: Dict[str, Any] = {
        "enabled": bool(b.enabled),
        "period": b.period,
        "period_start": since.isoformat() if since else None,
        "reserve_ratio": float(b.reserve_ratio or 1.0),
        "count_pending": bool(b.count_pending),
        "on_exceed": b.on_exceed,
        "per_task_cap_cny": float(b.per_task_cap_cny or 0.0),
        "project_cap_cny": float(b.project_cap_cny or 0.0),
        "global_cap_cny": float(b.global_cap_cny or 0.0),
        "provider_caps": caps,
        **g,
        "global_remaining_cny": (
            None if float(b.global_cap_cny or 0.0) <= 0
            else round(float(b.global_cap_cny) - g["committed_cny"], 4)),
        "by_project": by_project,
        "by_provider": [{"provider_id": k, "amount_cny": v}
                        for k, v in sorted(by_provider.items(),
                                           key=lambda kv: -kv[1])],
        "open_tasks": _open_tasks(session),
        "recent_blocks": _recent_blocks(session),
    }
    if project_id is not None:
        p = committed_cny(session, count_pending=b.count_pending, since=since,
                          project_id=project_id)
        cap = float(b.project_cap_cny or 0.0)
        out["project"] = {
            "project_id": project_id, **p,
            "cap_cny": cap,
            "remaining_cny": (None if cap <= 0
                              else round(cap - p["committed_cny"], 4)),
            "exceeded": bool(cap > 0 and p["committed_cny"] > cap + 1e-9),
        }
    return out
