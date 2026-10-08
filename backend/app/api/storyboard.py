"""分镜生成 + lint 门禁 API。

POST /api/projects/{id}/storyboard
    生成分镜提示词 → 跑 lint 门禁 → 不达标自动回灌重生成 → 落库

POST /api/lint
    单条提示词打分（不改库，前端分镜编辑器可实时调用）
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db.models import Product, Project, ProjectProduct, Shot, Talent
from ..db.session import get_db
from ..deps import get_prompt_engine
from ..pipeline.engine import Brief, PromptEngine, ShotSpec
from ..pipeline.lint import DEFAULT_MIN_SCORE, lint_prompt
from ..pipeline.script_import import parse_post_plan, parse_script
from ..services.scripts import latest_script, list_scripts
from ..services.shot_import import import_script_to_project
from ..services.shot_import import shot_preview

router = APIRouter(prefix="/api", tags=["storyboard"])


# ---------------------------------------------------------------- schemas


class ShotSpecIn(BaseModel):
    code: str
    duration_sec: float
    brief: str


class StoryboardIn(BaseModel):
    shots: List[ShotSpecIn]
    extra: str = ""
    min_score: int = DEFAULT_MIN_SCORE


class LintIn(BaseModel):
    prompt: str
    duration: float = 3.0


class ImportScriptIn(BaseModel):
    """把分镜脚本 markdown 导入为项目分镜。

    脚本可以来自 Trae（`/generate-script` 的产出），也可以是用户从别处
    **粘贴进来的外部脚本**（见 pipeline/script_import.py 的宽松兜底）。
    `dry_run=True` 时只解析 + 打分，不落库——给前端的「解析预览」用。

    `source` 只用于素材留存的溯源标记；`save_asset=False` 可让 dry_run 之外
    的导入也不留痕（一般不用）。
    """
    script: str
    min_score: int = DEFAULT_MIN_SCORE
    dry_run: bool = False
    source: str = "import"          # agent / paste / import
    save_asset: bool = True         # 是否把脚本留存为项目素材


class ImportScriptOut(BaseModel):
    project_id: int
    imported: int
    created: int
    updated: int
    min_score: int
    dry_run: bool = False
    shots: List[dict]
    warnings: List[str] = Field(default_factory=list)
    # 留存下来的脚本素材 id（便于前端链接到脚本详情）；未留痕时为 None
    script_asset_id: Optional[int] = None


# ---------------------------------------------------------------- 业务


class StoryboardOut(BaseModel):
    project_id: int
    rounds: int
    total_frames: int
    min_score: int
    shots: List[dict]
    # 商品事实的缺口提示（例如"关联了商品但没填卖点描述"）。
    # 旧字段名 `excluded_c_level_notes` 随铁律 15 门禁一起退役 ——
    # 现在没有"C 级事实被排除"这回事了，商品事实一律可写。
    warnings: List[str] = Field(default_factory=list)


@router.post("/projects/{pid}/storyboard", response_model=StoryboardOut)
def generate_storyboard(
    pid: int,
    body: StoryboardIn,
    db: Session = Depends(get_db),
    engine: PromptEngine = Depends(get_prompt_engine),
):
    proj: Optional[Project] = db.get(Project, pid)
    if proj is None:
        raise HTTPException(404, f"项目不存在: {pid}")

    # --- 商品事实：直接取表单里的卖点描述（对齐 ClipForge，不再做 A/B/C 分级）---
    # 旧实现只放行 A/B 级香调事实、把 C 级排除在提示词外（铁律 15）。该门禁已随
    # "字段模型完全替换"停用 —— 事实来源改为用户自己填的卖点描述。
    #
    # 但**没有卖点描述的商品仍然不进提示词**，只记一条 warning。理由不是"分级"，
    # 而是没有事实可写：把光秃秃的商品名塞进去，等于邀请模型自行编造卖点
    # （"禁止虚构商品事实"这条约束必须保住，它跟分级是两件事）。
    links = db.query(ProjectProduct).filter(ProjectProduct.project_id == pid).all()
    product_facts, warnings = [], []
    for lk in links:
        p: Optional[Product] = db.get(Product, lk.product_id)
        if p is None:
            continue
        if not (p.description or "").strip():
            warnings.append(
                f"{p.code} {p.name} 没有卖点描述，已排除在提示词之外"
                f"（避免模型自行编造商品事实）—— 去商品库补一段卖点即可")
            continue
        parts = [f"{p.name}（{p.code}，品类 {p.category}）",
                 f"卖点：{p.description}"]
        if p.price:
            parts.append(f"价格：{p.price}")
        if p.target_audience:
            parts.append(f"目标人群：{p.target_audience}")
        product_facts.append("；".join(parts))

    talent: Optional[Talent] = db.get(Talent, proj.talent_id) if proj.talent_id else None
    anchor = (talent.appearance if talent else "") or ""
    if talent is not None and not anchor:
        warnings.append(f"主播 {talent.code} 没有填外貌特征，跨镜人脸一致性无锚点可用")

    brief = Brief(
        project_name=proj.name,
        product_notes="; ".join(product_facts) or "（无可用的商品卖点事实，禁止虚构商品事实）",
        talent_anchor=anchor,
        shots=[ShotSpec(code=s.code, duration_sec=s.duration_sec, brief=s.brief)
               for s in body.shots],
        language=proj.language,
        extra=body.extra,
    )

    engine.min_score = body.min_score
    sb = engine.generate_storyboard(brief)

    # --- 落库（按 code 幂等 upsert）---
    existing = {s.code: s for s in db.query(Shot).filter(Shot.project_id == pid).all()}
    code_to_idx = {s.code: i for i, s in enumerate(body.shots)}
    for g in sb.shots:
        row = existing.get(g.code)
        if row is None:
            row = Shot(project_id=pid, code=g.code)
            db.add(row)
        row.idx = code_to_idx.get(g.code, 0)
        row.prompt_en = g.prompt_en
        row.prompt_zh = g.prompt_zh
        row.duration_sec = g.duration_sec
        row.target_frames = g.target_frames
        row.lint_score = g.lint_score
        row.lint_report = g.lint.to_dict() if g.lint else {}
        row.status = "lint_passed" if g.lint_score >= body.min_score else "lint_failed"
    db.commit()

    proj.status = "storyboarded"
    db.commit()

    return StoryboardOut(
        project_id=pid,
        rounds=sb.rounds,
        total_frames=sb.total_frames,
        min_score=sb.min_score,
        shots=[s.to_dict() for s in sb.shots],
        warnings=warnings,
    )


# 解析 + 落库逻辑已抽到 services/shot_import.py —— 「一键出片」编排走同一份实现。
# 这里保留 `_shot_preview` 这个名字，供同文件其它地方复用。
_shot_preview = shot_preview


@router.post("/projects/{pid}/import-script", response_model=ImportScriptOut)
def import_script(pid: int, body: ImportScriptIn, db: Session = Depends(get_db)):
    """把分镜脚本 markdown 解析成镜头并落库（按 code 幂等 upsert）。

    这是「生成视频提示词」→「出片」之间的桥：脚本不再只是弹窗里一段文本，
    导入后每个镜头都能出片（出片会自动带上产品图与模特图作为 i2v 参考图，
    见 services/refs.py）。

    `dry_run=True`：只解析 + 打分，**不落库、不改项目状态、不返回 400**，
    好让前端在真正导入前把「识别到几镜、有没有警告」摊给用户看。
    """
    try:
        r = import_script_to_project(
            db, pid, script=body.script, min_score=body.min_score,
            source=body.source, save_asset=body.save_asset, dry_run=body.dry_run,
        )
    except ValueError as e:
        raise HTTPException(404, str(e)) from e

    # 预览模式不下 400：用户需要看到「为什么没解析出来」，而不是一个通用报错
    if not r.imported and not body.dry_run:
        raise HTTPException(400, "脚本解析失败：" + "；".join(r.warnings))

    return ImportScriptOut(
        project_id=pid, imported=r.imported, created=r.created, updated=r.updated,
        min_score=body.min_score, dry_run=body.dry_run, shots=r.shots,
        warnings=r.warnings, script_asset_id=r.script_asset_id,
    )


@router.get("/projects/{pid}/shots")
def list_shots(pid: int, db: Session = Depends(get_db)):
    rows = (db.query(Shot).filter(Shot.project_id == pid)
            .order_by(Shot.idx, Shot.id).all())
    # 产物字段（video_path / onset_sec）必须回传：分镜页要看"哪几镜已出片"，
    # 音频页要测起点，交付页要靠它们判断成片能不能拼起来。
    return [{
        "id": r.id, "code": r.code, "idx": r.idx,
        "duration_sec": r.duration_sec, "target_frames": r.target_frames,
        "lint_score": r.lint_score, "status": r.status,
        "prompt_en": r.prompt_en, "prompt_zh": r.prompt_zh,
        "onset_sec": r.onset_sec, "video_path": r.video_path, "seed": r.seed,
        "has_video": bool(r.video_path),
    } for r in rows]


class ShotPatch(BaseModel):
    """单镜改稿。前端分镜编辑器就是打这个接口。"""

    code: Optional[str] = None
    idx: Optional[int] = None
    duration_sec: Optional[float] = None
    prompt_en: Optional[str] = None
    prompt_zh: Optional[str] = None
    status: Optional[str] = None
    seed: Optional[int] = None
    relint: bool = True


@router.patch("/shots/{sid}")
def patch_shot(sid: int, body: ShotPatch, db: Session = Depends(get_db)):
    """改完提示词**立刻重算 lint 并落库**：编辑器里看到的分必须和库里一致。"""
    s: Optional[Shot] = db.get(Shot, sid)
    if s is None:
        raise HTTPException(404, f"镜头 {sid} 不存在")
    for k in ("code", "idx", "duration_sec", "prompt_en", "prompt_zh", "status", "seed"):
        v = getattr(body, k)
        if v is not None:
            setattr(s, k, v)
    if body.relint and s.prompt_en:
        rep = lint_prompt(s.prompt_en, s.duration_sec or 3.0, name=s.code or str(sid))
        s.lint_score = rep.total
        s.lint_report = rep.to_dict()
        if s.status in ("lint_passed", "lint_failed"):
            s.status = "lint_passed" if rep.total >= DEFAULT_MIN_SCORE else "lint_failed"
    if body.duration_sec is not None:
        s.target_frames = int(round(body.duration_sec * 24))
    db.commit()
    db.refresh(s)
    return {"id": s.id, "code": s.code, "lint_score": s.lint_score,
            "lint_report": s.lint_report, "status": s.status,
            "duration_sec": s.duration_sec, "target_frames": s.target_frames}


@router.delete("/shots/{sid}")
def delete_shot(sid: int, db: Session = Depends(get_db)):
    s: Optional[Shot] = db.get(Shot, sid)
    if s is None:
        raise HTTPException(404, f"镜头 {sid} 不存在")
    db.delete(s)
    db.commit()
    return {"deleted": sid, "files_kept": True}


@router.post("/shots/{sid}/lint")
def relint_shot(sid: int, db: Session = Depends(get_db)):
    s: Optional[Shot] = db.get(Shot, sid)
    if s is None:
        raise HTTPException(404, f"镜头 {sid} 不存在")
    rep = lint_prompt(s.prompt_en or "", s.duration_sec or 3.0, name=s.code or str(sid))
    s.lint_score = rep.total
    s.lint_report = rep.to_dict()
    db.commit()
    return rep.to_dict() | {"min_score": DEFAULT_MIN_SCORE,
                            "passed": rep.total >= DEFAULT_MIN_SCORE}


@router.post("/lint")
def lint_one(body: LintIn):
    rep = lint_prompt(body.prompt, body.duration, name="inline")
    return rep.to_dict() | {"min_score": DEFAULT_MIN_SCORE,
                            "passed": rep.total >= DEFAULT_MIN_SCORE}


# ---------------------------------------------------------------- 脚本素材

@router.get("/projects/{pid}/scripts")
def list_project_scripts(pid: int, db: Session = Depends(get_db)):
    """项目的脚本素材（一版一条，按时间倒序）。

    脚本是成片的事实源：镜号 / 时长 / 念白稿 / 音效落点都在里面。
    全文用返回的 `path` 走 `/api/file?path=` 取。
    """
    if db.get(Project, pid) is None:
        raise HTTPException(404, f"项目不存在: {pid}")
    return list_scripts(db, pid)


@router.get("/projects/{pid}/post-plan")
def project_post_plan(pid: int, db: Session = Depends(get_db)):
    """最新一版脚本解析出的**后期计划**：念白行 + 音效落点。

    给「一键成片」做前置预览用 —— 用户能先看清"会念哪几句、喷头声落在第几秒"，
    再决定要不要真的跑（TTS 会联网，混音要跑 ffmpeg）。
    """
    proj: Optional[Project] = db.get(Project, pid)
    if proj is None:
        raise HTTPException(404, f"项目不存在: {pid}")
    asset = latest_script(db, pid)
    if asset is None:
        return {"project_id": pid, "has_script": False,
                "vo_rows": [], "sfx_cues": [], "warnings": ["该项目还没有脚本素材"]}
    m = asset.meta or {}
    return {
        "project_id": pid,
        "has_script": True,
        "script_asset_id": asset.id,
        "script_path": asset.path,
        "title": m.get("title"),
        "language": m.get("language"),
        "vo_rows": m.get("vo_rows") or [],
        "sfx_cues": m.get("sfx_cues") or [],
        "warnings": m.get("warnings") or [],
    }
