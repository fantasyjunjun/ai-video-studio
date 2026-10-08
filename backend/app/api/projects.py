"""项目 CRUD + 成本报告 + 时间轴。

项目表里刻意记了 `llm_provider / image_provider / video_provider`：
同一份分镜在不同供应商下出片差异很大，**记不住当时用的谁，就没法解释"为什么这版和上一版不一样"**。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import deps
from ..db.models import (
    Asset,
    AudioTrack,
    CostRecord,
    Product,
    Project,
    ProjectProduct,
    RenderJob,
    Shot,
    Talent,
)
from ..db.session import get_db

router = APIRouter(prefix="/api", tags=["projects"])


class ProjectIn(BaseModel):
    name: str
    language: str = "pt-BR"
    talent_id: Optional[int] = None
    llm_provider: Optional[str] = None
    image_provider: Optional[str] = None
    video_provider: Optional[str] = None
    products: List[Dict[str, Any]] = Field(default_factory=list)  # [{product_id, role}]


def _project_out(p: Project, db: Session) -> dict:
    links = db.query(ProjectProduct).filter(ProjectProduct.project_id == p.id).all()
    products = []
    for lk in links:
        prod: Optional[Product] = db.get(Product, lk.product_id)
        if prod is None:
            continue
        products.append({
            "product_id": prod.id, "code": prod.code, "name": prod.name,
            "category": (prod.category or "other").lower(),
            "cover": (prod.images or [None])[0] if prod.images else None,
            "role": lk.role or "",
        })
    shots = db.query(Shot).filter(Shot.project_id == p.id).all()
    talent: Optional[Talent] = db.get(Talent, p.talent_id) if p.talent_id else None
    return {
        "id": p.id, "name": p.name, "language": p.language,
        "status": p.status, "cost_cny": round(p.cost_cny or 0.0, 4),
        "llm_provider": p.llm_provider, "image_provider": p.image_provider,
        "video_provider": p.video_provider,
        "talent_id": p.talent_id, "talent_code": talent.code if talent else None,
        "products": products,
        "shots": len(shots),
        "shots_ready": sum(1 for s in shots if s.video_path),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


@router.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    rows = db.query(Project).order_by(Project.id.desc()).all()
    return [_project_out(r, db) for r in rows]


@router.post("/projects")
def create_project(body: ProjectIn, db: Session = Depends(get_db)):
    cfg = deps.get_providers_config()
    p = Project(
        name=body.name, language=body.language, talent_id=body.talent_id,
        llm_provider=body.llm_provider or cfg.llm.active,
        image_provider=body.image_provider or cfg.image.active,
        video_provider=body.video_provider or cfg.video.active,
        status="draft",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    for item in body.products:
        pid = int(item.get("product_id"))
        prod: Optional[Product] = db.get(Product, pid)
        if prod is None:
            continue
        db.add(ProjectProduct(project_id=p.id, product_id=pid,
                              role=str(item.get("role", ""))))
    db.commit()
    return _project_out(p, db)


@router.get("/projects/{pid}")
def get_project(pid: int, db: Session = Depends(get_db)):
    p: Optional[Project] = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, f"项目不存在：{pid}")
    out = _project_out(p, db)
    out["recent_jobs"] = [
        {"id": j.id, "kind": j.kind, "status": j.status, "cost_cny": j.cost_cny}
        for j in db.query(RenderJob).filter(RenderJob.project_id == pid)
        .order_by(RenderJob.id.desc()).limit(10).all()
    ]
    out["audio_tracks"] = [
        {"id": a.id, "kind": a.kind, "path": a.path}
        for a in db.query(AudioTrack).filter(AudioTrack.project_id == pid)
        .order_by(AudioTrack.id.desc()).limit(10).all()
    ]
    finals = db.query(Asset).filter(Asset.project_id == pid,
                                    Asset.kind == "video").all()
    out["assets"] = [{"id": a.id, "path": a.path, "meta": a.meta} for a in finals]
    return out


@router.patch("/projects/{pid}")
def update_project(pid: int, body: Dict[str, Any], db: Session = Depends(get_db)):
    p: Optional[Project] = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, f"项目不存在：{pid}")

    products = body.pop("products", None)
    for k, v in body.items():
        if hasattr(p, k) and k not in ("id", "created_at", "cost_cny"):
            setattr(p, k, v)
    p.updated_at = datetime.utcnow()

    if products is not None:
        db.query(ProjectProduct).filter(ProjectProduct.project_id == pid).delete()
        for item in products:
            db.add(ProjectProduct(project_id=pid,
                                  product_id=int(item["product_id"]),
                                  role=str(item.get("role", ""))))
    db.commit()
    db.refresh(p)
    return _project_out(p, db)


@router.delete("/projects/{pid}")
def delete_project(pid: int, db: Session = Depends(get_db)):
    """删库连带清关系；**磁盘产物一律保留**（出片很贵，删了没法找回）。"""
    p: Optional[Project] = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, f"项目不存在：{pid}")
    db.query(ProjectProduct).filter(ProjectProduct.project_id == pid).delete()
    db.delete(p)
    db.commit()
    return {"deleted": pid, "files_kept": True}


# ------------------------------------------------------------------ 成本


@router.get("/projects/{pid}/cost")
def cost_report(pid: int, db: Session = Depends(get_db)):
    p: Optional[Project] = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, f"项目不存在：{pid}")

    rows = (db.query(CostRecord.kind, func.sum(CostRecord.amount_cny),
                     func.sum(CostRecord.quantity))
            .filter(CostRecord.project_id == pid)
            .group_by(CostRecord.kind).all())
    by_kind = [{"kind": k, "amount_cny": round(a or 0.0, 4),
                "quantity": round(q or 0.0, 3)} for k, a, q in rows]

    jobs = db.query(RenderJob).filter(RenderJob.project_id == pid).all()
    return {
        "project_id": pid,
        "project_cost_cny": round(p.cost_cny or 0.0, 4),
        "by_kind": by_kind,
        "jobs": len(jobs),
        "jobs_succeeded": sum(1 for j in jobs if j.status == "succeeded"),
        "jobs_reused": sum(1 for j in jobs if j.status == "reused"),
        "jobs_failed": sum(1 for j in jobs if j.status == "failed"),
        "jobs_dry_run": sum(1 for j in jobs if bool(j.dry_run)),
        "detail": [
            {"id": j.id, "kind": j.kind, "provider_id": j.provider_id,
             "status": j.status, "cost_cny": round(j.cost_cny or 0.0, 4),
             "dry_run": bool(j.dry_run),
             "created_at": j.created_at.isoformat() if j.created_at else None}
            for j in sorted(jobs, key=lambda x: x.id)
        ],
    }


# ------------------------------------------------------------------ 时间轴


@router.get("/projects/{pid}/timeline")
def timeline(pid: int, fps: float = 24.0, db: Session = Depends(get_db)):
    """逐镜起止秒 + 帧数。**UI 上的一切时间点都以这里为准**，不要前端自己累加。"""
    p: Optional[Project] = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, f"项目不存在：{pid}")
    shots = db.query(Shot).filter(Shot.project_id == pid).order_by(Shot.idx).all()

    rows, pos_frames = [], 0
    for s in shots:
        frames = int(s.target_frames or round((s.duration_sec or 0) * fps))
        rows.append({
            "id": s.id, "code": s.code, "idx": s.idx,
            "duration_sec": s.duration_sec,
            "frames": frames,
            "start_sec": round(pos_frames / fps, 4),
            "end_sec": round((pos_frames + frames) / fps, 4),
            "onset_sec": s.onset_sec,
            "video_path": s.video_path,
            "status": s.status,
            "lint_score": s.lint_score,
        })
        pos_frames += frames
    return {"project_id": pid, "fps": fps,
            "total_frames": pos_frames,
            "total_sec": round(pos_frames / fps, 4),
            "shots": rows}
