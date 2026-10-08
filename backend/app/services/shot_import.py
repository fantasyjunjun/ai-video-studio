"""脚本 → 项目分镜：解析并落库（按镜号幂等 upsert）。

抽成服务的理由和 render_core / compose 一样：`POST /import-script`（人工导入）
与「一键出片」编排都要做这件事。**"同名镜号覆盖还是新增""lint 用哪个闸门"
这类判据不能有两套** —— 不然人工导入和自动出片会得到不同的镜头集。

脚本来源有两个，共用这条链路：
  1. `POST /generate-script` 里 Trae 的产出（模板严格路径）；
  2. **用户粘贴的外部脚本**（格式更野，走 script_import 的宽松兜底）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session

from ..db.models import Project, Shot
from ..pipeline.lint import DEFAULT_MIN_SCORE, lint_prompt
from ..pipeline.script_import import parse_script
from .scripts import save_script


@dataclass
class ImportOut:
    imported: int = 0
    created: int = 0
    updated: int = 0
    shots: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    script_asset_id: Optional[int] = None


def _flat(s: Optional[str]) -> str:
    return " ".join((s or "").split())


def shot_preview(p, min_score: int) -> dict:
    """把解析结果压成一份摘要（既用于落库回执，也用于 dry_run 预览）。"""
    rep = lint_prompt(p.prompt_en, p.duration_sec, name=p.code)
    return {
        "code": p.code,
        "duration_sec": p.duration_sec,
        "start_sec": p.start_sec,
        "end_sec": p.end_sec,
        "target_frames": int(round(p.duration_sec * 24)),
        "lint_score": rep.total,
        "status": "lint_passed" if rep.total >= min_score else "lint_failed",
        "prompt_source": p.prompt_source,
        "prompt_head": _flat(p.prompt_en)[:160],
        "prompt_zh_head": _flat(p.prompt_zh)[:120],
        # ★R-33：逐镜商品分配与造型 —— 前端能一眼看到"这一镜用哪个商品、
        # 出片会挂谁的图"，不用等出完片才发现挂错了型号。
        "products": list(p.products or []),
        "talent_look": p.talent_look or "",
    }


def import_script_to_project(
    db: Session,
    pid: int,
    *,
    script: str,
    min_score: int = DEFAULT_MIN_SCORE,
    source: str = "import",
    save_asset: bool = True,
    dry_run: bool = False,
) -> ImportOut:
    """解析 `script` 并把镜头落库（按 code 幂等 upsert）。

    `dry_run=True`：只解析 + 打分，**不落库、不改项目状态、不抛异常**
    （解析不出镜头时返回 imported=0 + warnings），好让前端把
    「识别到几镜、有没有警告」摊给用户看。
    """
    proj: Optional[Project] = db.get(Project, pid)
    if proj is None:
        raise ValueError(f"项目不存在: {pid}")

    parsed, warnings = parse_script(script)
    if not parsed:
        return ImportOut(warnings=list(warnings))

    if dry_run:
        return ImportOut(
            imported=len(parsed),
            shots=[shot_preview(p, min_score) for p in parsed],
            warnings=list(warnings),
        )

    existing = {s.code: s for s in db.query(Shot).filter(Shot.project_id == pid).all()}
    created = updated = 0
    out: List[dict] = []
    for idx, p in enumerate(parsed):
        row = existing.get(p.code)
        if row is None:
            row = Shot(project_id=pid, code=p.code)
            db.add(row)
            created += 1
        else:
            updated += 1
        row.idx = idx
        row.prompt_en = p.prompt_en
        row.prompt_zh = p.prompt_zh
        row.duration_sec = p.duration_sec
        row.target_frames = int(round(p.duration_sec * 24))
        # ★R-33 逐镜商品分配 / 章节级造型：从脚本分镜表读回，落库供出片筛参考图。
        row.products = list(p.products or [])
        row.talent_look = p.talent_look or ""
        rep = lint_prompt(p.prompt_en, p.duration_sec, name=p.code)
        row.lint_score = rep.total
        row.lint_report = rep.to_dict()
        row.status = "lint_passed" if rep.total >= min_score else "lint_failed"
        out.append(shot_preview(p, min_score))

    db.commit()
    proj.status = "storyboarded"
    db.commit()

    # 已经导入过的镜号本轮被覆盖（同 code 幂等），给用户一句提示避免困惑
    if updated:
        warnings = list(warnings) + [f"{updated} 个同名镜号已按新脚本覆盖"]

    # 脚本留存为项目素材：它承载了镜号/时长/念白稿/音效落点，是这一版成片的
    # 事实源；一键成片也从这里取念白行。**入库失败不该让导入失败**（镜头已经
    # 落库了），所以只降级成一条 warning。
    asset_id: Optional[int] = None
    if save_asset:
        try:
            asset = save_script(db, project_id=pid, script=script,
                                source=source, language=proj.language)
            asset_id = asset.id
        except Exception as e:  # noqa: BLE001
            warnings = list(warnings) + [f"脚本素材留存失败：{type(e).__name__}: {e}"]

    return ImportOut(imported=len(out), created=created, updated=updated,
                     shots=out, warnings=list(warnings), script_asset_id=asset_id)
