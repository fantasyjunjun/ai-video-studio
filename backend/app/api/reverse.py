"""参考片反推 API。

    POST /api/reverse          对本地已有文件做反推
    POST /api/reverse/upload   上传参考片后反推（需要 python-multipart）
    GET  /api/reverse/reports  历史报告
    GET  /api/reverse/{rid}    单份报告

`use_llm=false` 时只做本地量测（规格/亮度/帧差/切点），**不联网、不花钱**。
这既是离线测试通道，也是"只想看点数据"时的快速路径。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import deps
from ..audio.ffmpeg import FFmpegError
from ..db.models import Asset, ReverseReport
from ..db.session import get_db
from ..services import settings as settings_svc
from ..services.reverse import VISION_MAX_TOKENS

router = APIRouter(prefix="/api/reverse", tags=["reverse"])

UPLOAD_DIR = deps.BASE / "data" / "reverse" / "_uploads"


class ReverseIn(BaseModel):
    path: str
    project_id: Optional[int] = None
    sample_fps: float = 2.0
    note: str = ""
    use_llm: bool = True
    # R-32：默认看图反推（把抽帧拼图交给视觉通道），这样才有逐镜"逆推提示词"；
    # 关掉或通道不支持视觉时自动降级为纯数据判定（只出结构不出提示词）。
    use_vision: bool = True
    tile: str = "5x6"


def _report_out(r: ReverseReport) -> dict:
    st = r.structure_json or {}
    return {
        "id": r.id, "source_path": r.source_path,
        "spec": r.spec_json or {}, "structure": st,
        "findings": r.findings or "",
        # 前端要据此说明"这份报告为什么没有逐镜提示词"（旧报告 / 纯数据降级）
        "vision": bool(st.get("vision")),
        "vision_note": st.get("vision_note") or "",
        # 有提示词但**不完整**（撞输出上限 / 中继断流），残卷已保留可解析的镜头
        "truncated": bool(st.get("truncated")),
        "raw_chars": int(st.get("raw_chars") or 0),
        # ★R-38：逐镜高清复核（治"漏写道具"）的执行情况
        "per_shot_ok": int(st.get("per_shot_ok") or 0),
        "per_shot_total": int(st.get("per_shot_total") or 0),
        "per_shot_note": st.get("per_shot_note") or "",
        "shot_count": len(st.get("shots") or []),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.post("")
def reverse(body: ReverseIn, db: Session = Depends(get_db),
            engine=Depends(deps.get_reverse_engine)):
    slug = f"{int(time.time() * 1000)}"
    # 反推看图的视觉模型覆盖（C）：设置页填，留空=当前 active LLM 模型。
    # 复用 active LLM 的 base_url 与同一把密钥，只覆盖模型名。
    vision_model = settings_svc.get_setting(db, "reverse_vision_model", "") or None
    # 输出上限（逐镜长提示词极易撞上限→ JSON 被腰斩，故做成可调）
    vision_max_tokens = settings_svc.get_int_setting(
        db, "reverse_vision_max_tokens", VISION_MAX_TOKENS)
    try:
        res = engine.analyze(body.path, sample_fps=body.sample_fps,
                             note=body.note, use_llm=body.use_llm,
                             use_vision=body.use_vision,
                             tile=body.tile, slug=slug,
                             vision_model=vision_model,
                             vision_max_tokens=vision_max_tokens)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        raise HTTPException(502, f"LLM 输出无法解析：{str(e)[:300]}") from e
    except FFmpegError as e:
        # 视频/坏文件/上传截断：本地量测就失败了，别让 500 裸奔
        raise HTTPException(400, f"视频无法解析（文件损坏或不是有效视频）：{str(e)[:300]}") from e
    except RuntimeError as e:
        # LLM 调用失败（额度不足/网络/超时）：把供应商真实原因透给前端
        raise HTTPException(502, str(e)) from e

    row = ReverseReport(
        source_path=res.source_path,
        spec_json=res.spec,
        structure_json={
            "sheet_path": res.sheet_path,
            "metrics": [m.to_dict() for m in res.metrics],
            "cut_candidates": res.cut_candidates,
            "shots": res.analysis.get("shots", []),
            "findings": res.findings,
            # 看图 / 纯数据降级 —— 报告自己说清有没有逐镜提示词（R-32）
            "vision": bool(res.analysis.get("vision")),
            "vision_note": res.analysis.get("vision_note", ""),
            # 有提示词但不完整（撞输出上限 / 中继断流）：残卷抢救后的报告要标注出来，
            # 否则用户会以为"原片就这么多镜头"
            "truncated": bool(res.analysis.get("truncated")),
            "raw_chars": int(res.analysis.get("raw_chars") or 0),
            # ★R-38 逐镜高清复核：网格图初稿之后补的一遍"逐镜单独看原始分辨率帧"，
            # 治的是"道具/陈设成批漏写"。ok/total 让用户知道哪几镜的对账做完了。
            "per_shot_ok": int(res.analysis.get("per_shot_ok") or 0),
            "per_shot_total": int(res.analysis.get("per_shot_total") or 0),
            "per_shot_merged": res.analysis.get("per_shot_merged") or [],
            "per_shot_note": res.analysis.get("per_shot_note", ""),
        },
        findings="\n".join(
            [f"[采纳] {x}" for x in res.findings.get("adopt", [])]
            + [f"[改造] {x}" for x in res.findings.get("adapt", [])]
            + [f"[不可采纳] {x}" for x in res.findings.get("reject", [])]
        ),
    )
    db.add(row)
    if res.sheet_path:
        db.add(Asset(kind="image", path=res.sheet_path,
                     project_id=body.project_id,
                     meta={"stage": "reverse-sheet"}))
    db.commit()
    db.refresh(row)
    return {"report_id": row.id, **res.to_dict()}


@router.post("/upload")
async def reverse_upload(
    file: UploadFile = File(...),
    # 注意：这几个必须声明成 `Form(...)` —— FastAPI 里只有 `File` 是表单字段，
    # 其它标量默认走 **query**，前端把 note / use_llm 放在 multipart 里会被静默忽略
    # （实测：上传线上传参考片时补充说明与看图开关一直没生效）。R-32 修正。
    project_id: Optional[int] = Form(None),
    sample_fps: float = Form(2.0),
    note: str = Form(""),
    use_llm: bool = Form(True),
    use_vision: bool = Form(True),
    db: Session = Depends(get_db),
    engine=Depends(deps.get_reverse_engine),
):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # 文件名带时间戳，避免同名覆盖（覆盖=截断写，会把别人的素材清空）
    dest = UPLOAD_DIR / f"{int(time.time() * 1000)}_{file.filename or 'ref.mp4'}"
    with open(dest, "wb") as f:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    return reverse(ReverseIn(path=str(dest), project_id=project_id,
                             sample_fps=sample_fps, note=note, use_llm=use_llm,
                             use_vision=use_vision),
                   db=db, engine=engine)


@router.get("/reports")
def list_reports(limit: int = 50, db: Session = Depends(get_db)):
    rows = (db.query(ReverseReport).order_by(ReverseReport.id.desc())
            .limit(limit).all())
    return [_report_out(r) for r in rows]


@router.get("/{rid}")
def get_report(rid: int, db: Session = Depends(get_db)):
    r: Optional[ReverseReport] = db.get(ReverseReport, rid)
    if r is None:
        raise HTTPException(404, f"报告不存在：{rid}")
    return _report_out(r)
