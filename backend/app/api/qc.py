"""出片质量门 API（P-6）。

三个端点：
    POST /api/projects/{pid}/qc   扫该项目全部已出片的镜头（+ 可选成片）
    POST /api/shots/{sid}/qc      只扫某一镜
    POST /api/qc/sheet            对任意视频出逐格拼图（目视复核用）

为什么要有项目级而不是只挂在 `/verify` 上：
    成片是几镜拼出来的，**单看整片查不出"某一镜自己没动/尾部冻帧"** ——
    整片的帧差会被别的镜头平均掉。质量门必须在**镜级**跑，成片级只做补充。

判据全部来自 `pipeline/qc.py`（纯函数、纯读）。这里只负责取路径、聚合、
可选留档 —— 不重复实现任何一条检测。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import deps
from ..db.models import Asset, AudioTrack, Project, Shot
from ..db.session import get_db
from ..pipeline import qc as qc_mod

router = APIRouter(prefix="/api", tags=["qc"])


class QcIn(BaseModel):
    include_shots: bool = True
    include_final: bool = True
    # 留空 = 自动从提示词里探测"声明了哪些结果性元素"（mist/smoke/splash…）
    elements: Optional[List[str]] = None
    roi: Optional[List[float]] = None          # 元素检出的 ROI（0–1 相对坐标）
    count_roi: Optional[List[float]] = None    # 物件数量突变检查的 ROI（瓶盖/桌面）
    sheet: bool = False                        # 是否出逐格拼图
    sheet_at: Optional[List[float]] = None     # 指定时刻抽帧（如 onset 前后）
    save_report: bool = False                  # 留档成 Asset(kind=report)
    fps: float = 24.0
    max_shots: int = 60


class SheetIn(BaseModel):
    path: str
    cols: int = 4
    rows: int = 4
    cell_width: int = 240
    seconds: Optional[List[float]] = None
    out: Optional[str] = None


def _pid(pid: int, db: Session) -> Project:
    p = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, f"项目 {pid} 不存在")
    return p


def _tolist(v) -> Optional[tuple]:
    if not v:
        return None
    if len(v) != 4:
        raise HTTPException(400, "ROI 需要 4 个数：x0,y0,x1,y1")
    vals = tuple(float(x) for x in v)
    if not all(0.0 <= x <= 1.0 for x in vals) or vals[2] <= vals[0] or vals[3] <= vals[1]:
        raise HTTPException(400, "ROI 需满足 0<=x0<x1<=1 且 0<=y0<y1<=1")
    return vals


def _sheet_dir(pid: Optional[int]) -> Path:
    base = deps.BASE / "data" / "qc"
    return base / (f"p{pid}" if pid else "misc")


def _run_one(path: str, *, prompt: str = "", recorded_onset: Optional[float] = None,
             body: QcIn, sheet_out: Optional[Path] = None,
             expect_silent: bool = True) -> Dict[str, Any]:
    rep = qc_mod.analyze(
        path, fps=body.fps, roi=_tolist(body.roi), prompt=prompt,
        expect_elements=body.elements, recorded_onset_sec=recorded_onset,
        expect_silent=expect_silent, count_roi=_tolist(body.count_roi),
    )
    out = rep.to_dict()
    if body.sheet and sheet_out is not None:
        try:
            at = body.sheet_at
            if not at:
                # 默认抽"出事的那一下"：起点前后 + 首末
                base = out["metrics"].get("element_onset_sec")
                at = ([max(0.0, base - 0.2), base, base + 0.2] if base else [])
                at += [0.0, max(0.0, out["duration"] - 0.05)]
            out["sheet"] = qc_mod.contact_sheet(path, str(sheet_out),
                                                seconds=at or None)
        except Exception as e:  # noqa: BLE001 - 拼图失败不该毁整份报告
            out["sheet_error"] = f"{type(e).__name__}: {e}"
    return out


def _summarize(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {"high": 0, "medium": 0, "low": 0, "info": 0}
    for it in items:
        for k, v in (it.get("qc", {}).get("counts") or {}).items():
            counts[k] = counts.get(k, 0) + int(v or 0)
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "blocked": counts["high"] > 0,
        "checks": len(items),
    }


@router.post("/projects/{pid}/qc")
def project_qc(pid: int, body: QcIn, db: Session = Depends(get_db)):
    """镜级质量门。**每镜单独判**：整片会互相平均掉彼此的缺陷。"""
    _pid(pid, db)
    out: Dict[str, Any] = {"project_id": pid, "items": []}

    if body.include_shots:
        shots = (db.query(Shot).filter(Shot.project_id == pid)
                 .order_by(Shot.idx).limit(max(1, body.max_shots)).all())
        for s in shots:
            if not s.video_path or not Path(s.video_path).exists():
                continue
            sheet = (_sheet_dir(pid) / f"shot{s.id}_{s.code or 'x'}_sheet.png")
            q = _run_one(str(s.video_path), prompt=s.prompt_zh or s.prompt_en or "",
                         recorded_onset=s.onset_sec, body=body, sheet_out=sheet)
            out["items"].append({
                "scope": "shot", "shot_id": s.id, "code": s.code,
                "path": str(s.video_path), "qc": q,
            })

    if body.include_final:
        last = (db.query(AudioTrack)
                .filter(AudioTrack.project_id == pid, AudioTrack.kind == "final")
                .order_by(AudioTrack.id.desc()).first())
        if last and last.path and Path(last.path).exists():
            sheet = _sheet_dir(pid) / "final_sheet.png"
            # 成片**应当**有音轨（念白/BGM/SFX 都在里面），所以不查残留音轨
            q = _run_one(str(last.path), body=body, sheet_out=sheet,
                         expect_silent=False)
            out["items"].append({
                "scope": "final", "shot_id": None, "code": "final",
                "path": str(last.path), "qc": q,
            })

    out.update(_summarize(out["items"]))
    out["sheets"] = [it["qc"].get("sheet") for it in out["items"]
                     if it["qc"].get("sheet")]

    if body.save_report:
        try:
            d = _sheet_dir(pid)
            d.mkdir(parents=True, exist_ok=True)
            fp = d / "qc-report.json"
            payload = dict(out)
            payload["generated_at"] = datetime.utcnow().isoformat()
            fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
            db.add(Asset(kind="report", path=str(fp), project_id=pid,
                         meta={"report": "qc", "blocked": out["blocked"],
                               "counts": out["counts"]}))
            db.commit()
            out["saved_to"] = str(fp)
        except Exception as e:  # noqa: BLE001
            out["save_error"] = f"{type(e).__name__}: {e}"
    return out


@router.post("/shots/{shot_id}/qc")
def shot_qc(shot_id: int, body: QcIn, db: Session = Depends(get_db)):
    s: Optional[Shot] = db.get(Shot, shot_id)
    if s is None:
        raise HTTPException(404, f"镜头 {shot_id} 不存在")
    if not s.video_path or not Path(s.video_path).exists():
        raise HTTPException(400, f"镜头 {shot_id} 还没有可质检的产物")
    sheet = _sheet_dir(s.project_id) / f"shot{s.id}_{s.code or 'x'}_sheet.png"
    q = _run_one(str(s.video_path), prompt=s.prompt_zh or s.prompt_en or "",
                 recorded_onset=s.onset_sec, body=body, sheet_out=sheet)
    return {"scope": "shot", "shot_id": s.id, "code": s.code,
            "path": str(s.video_path), "qc": q,
            "blocked": q["blocked"], "counts": q["counts"]}


@router.post("/qc/sheet")
def make_sheet(body: SheetIn):
    """对任意视频出逐格拼图。自动判据只圈重点，**结论看这张图**。"""
    if not Path(body.path).exists():
        raise HTTPException(404, f"视频不存在：{body.path}")
    out = body.out or str(_sheet_dir(None) /
                          (Path(body.path).stem + "_sheet.png"))
    try:
        p = qc_mod.contact_sheet(body.path, out, cols=body.cols, rows=body.rows,
                                 cell_width=body.cell_width,
                                 seconds=body.seconds)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"拼图失败：{type(e).__name__}: {e}") from e
    return {"ok": True, "sheet": p}
