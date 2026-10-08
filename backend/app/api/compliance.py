"""广告法合规自检 API（P-4）。

路由一览：
    GET  /api/compliance/rules            当前生效词表（内置 + 可选 compliance.yaml）
    POST /api/compliance/scan             扫任意文本（前端"边打字边扫"用）
    POST /api/compliance/reload           改完 compliance.yaml 后热重载（免重启）
    POST /api/projects/{pid}/compliance   扫整个项目的文案并给出分级报告

设计取舍
--------
- **扫描器是纯函数**（`pipeline/compliance.py`），本层只负责"从哪儿取文本"和"报告存哪"。
  文案来源按可靠性排序：
    1) 请求体里前端正在编辑的念白行（最新、最准）
    2) 请求体附加文本
    3) 项目标题（标题同样受广告法管，且最容易被忽略）
    4) 库里已登记的念白音轨留痕（`audio_track.meta.text`）
  —— 第 3、4 类是**兜底**：用户没传文案时也能扫，因为"发布前闸门"不能依赖前端配合。
- **画面提示词的直译（`shot.prompt_zh`）默认不进扫描**（`include_prompt_zh=False`）：
  它写的是运镜/光线/质感/表演，不是对外发布的文案。实测 `minimal affect` 直译成
  「最小表情」，会被广告法词表判成"绝对化用语"而**稳定拦死成片**。要连它一起扫需显式开启。
- **同一段文字只扫一次**：念白文案往往同时以 rows 与音轨留痕两种形式存在，
  重复扫会在报告里出现两份一模一样的违规，看起来像 bug。
- **报告可落盘**（`persist=true`）：合规自检是有法律意义的动作，
  留一份带时间戳的 JSON 并登记进资产表，才能回答"这一版到底检没检过"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import deps
from ..db.models import Asset, AudioTrack, Project, Shot
from ..db.session import get_db
from ..pipeline import compliance as comp

BASE = Path(__file__).resolve().parents[2]  # backend/

router = APIRouter(prefix="/api", tags=["compliance"])

VALID_LANGS = set(comp.LANG_LABELS)


class ScanIn(BaseModel):
    text: str
    langs: Optional[List[str]] = None
    source: str = "text"


class RowIn(BaseModel):
    start: float = 0.0
    end: float = 0.0
    text: str


class ProjectScanIn(BaseModel):
    """项目级扫描入参。全部可省 —— 什么都不传就走库里的兜底文案源。"""

    rows: List[RowIn] = Field(default_factory=list)
    texts: Dict[str, str] = Field(default_factory=dict)
    langs: Optional[List[str]] = None
    include_title: bool = True
    # 画面提示词的中文直译**不是投放文案**，默认不扫（详见 collect_sources 文档）
    include_prompt_zh: bool = False
    include_audio: bool = True
    persist: bool = False


def _check_langs(langs: Optional[List[str]]) -> Optional[List[str]]:
    if not langs:
        return None
    bad = [x for x in langs if x not in VALID_LANGS]
    if bad:
        raise HTTPException(400, f"不支持的语言 {bad}（可选：{sorted(VALID_LANGS)}）")
    return list(dict.fromkeys(langs))


def _rules():
    try:
        return deps.get_compliance_rules()
    except Exception as e:  # noqa: BLE001 - 词表写坏了要给出可读的错，而不是 500 栈
        raise HTTPException(400, f"合规词表加载失败：{e}") from e


def _pid(pid: int, db: Session) -> Project:
    p: Optional[Project] = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, f"项目 {pid} 不存在")
    return p


# ---------------------------------------------------------------- 词表


@router.get("/compliance/rules")
def get_rules():
    """当前生效的合规词表概览（不回具体正则，避免前端包袱过重）。"""
    rules, source = _rules()
    return {
        "source": source,
        "path": str(BASE / "compliance.yaml"),
        "file_exists": (BASE / "compliance.yaml").is_file(),
        **comp.rules_summary(rules),
        "rules": [
            {k: v for k, v in r.to_dict().items() if k != "patterns"}
            for r in rules
        ],
        "hint": "想停用某条内置规则或补自己的行业禁语，改 backend/compliance.yaml 后调 reload",
    }


@router.post("/compliance/reload")
def reload_rules():
    deps.get_compliance_rules.cache_clear()
    rules, source = _rules()
    return {"ok": True, "source": source, "count": len(rules)}


# ---------------------------------------------------------------- 单段文本


@router.post("/compliance/scan")
def scan_text(body: ScanIn):
    """扫一段文本。前端应在输入停止约 300ms 后调用（debounce），不要每次按键都打。"""
    rules, _ = _rules()
    res = comp.scan_text(
        body.text, source=body.source or "text",
        langs=_check_langs(body.langs), rules=rules,
    )
    return res.to_dict()


# ---------------------------------------------------------------- 项目级


def collect_sources(
    pid: int,
    db: Session,
    *,
    texts: Sequence[str] = (),
    named: Optional[Dict[str, str]] = None,
    include_title: bool = True,
    include_prompt_zh: bool = False,
    include_audio: bool = True,
) -> List[tuple]:
    """按优先级收集待扫文本，**同一段文字只保留最先出现的那个来源**。

    公开函数（不是 `_collect_sources`）：`/verify` 与 `/final` 的门禁也要用同一套
    取文本逻辑 —— 如果门禁和页面各扫各的，"页面上没报、发布时被拦"就成了玄学。

    `include_prompt_zh` **默认关**（曾经默认开，是错的）：`shot.prompt_zh` 是
    **画面 i2v 提示词的中文直译**，内容是运镜 / 光线 / 质感 / 表演，**不是投放文案**。
    拿它过广告法词表会稳定误报 —— 实测 `minimal affect` 直译成「最小表情」，
    于是每个含 minimal / maximum 这类词的脚本都会被判"绝对化用语"而**拦住成片**。
    广告法的规制对象是**对外发布的内容**（念白字幕、画面文案、商品卖点），
    不是内部提示词的翻译稿。确实要连直译一起扫时，显式传 `include_prompt_zh=True`。
    """
    ordered: List[tuple] = []
    for i, t in enumerate(texts):
        t = (t or "").strip()
        if t:
            ordered.append((f"narration:{i + 1}", t))
    for name, text in (named or {}).items():
        t = (text or "").strip()
        if t:
            ordered.append((str(name), t))

    if include_title:
        p = db.get(Project, pid)
        if p is not None and (p.name or "").strip():
            ordered.append(("project:title", p.name.strip()))

    if include_prompt_zh:
        shots = (db.query(Shot).filter(Shot.project_id == pid)
                 .order_by(Shot.idx).all())
        for s in shots:
            t = (s.prompt_zh or "").strip()
            if t:
                ordered.append((f"shot:{s.code or s.id}:zh", t))

    if include_audio:
        # 念白原文只在合成时留了前 200 字（见 post.py::narration_raw）——
        # 够用来兜底扫高危词，但不适合当唯一来源；有 rows 时以 rows 为准。
        tracks = (db.query(AudioTrack)
                  .filter(AudioTrack.project_id == pid,
                          AudioTrack.kind.in_(("voice", "voice_raw")))
                  .order_by(AudioTrack.id.desc()).limit(5).all())
        for tr in tracks:
            t = str((tr.meta or {}).get("text") or "").strip()
            if t:
                ordered.append((f"audio:{tr.kind}#{tr.id}", t))

    # 去重：同一段文字只扫一遍（保留第一个来源名）
    seen: set = set()
    uniq: List[tuple] = []
    for name, text in ordered:
        if text in seen:
            continue
        seen.add(text)
        uniq.append((name, text))
    return uniq


def scan_project(
    pid: int,
    db: Session,
    *,
    texts: Sequence[str] = (),
    named: Optional[Dict[str, str]] = None,
    langs: Optional[List[str]] = None,
    include_title: bool = True,
    include_prompt_zh: bool = False,
    include_audio: bool = True,
) -> comp.ComplianceReport:
    """项目级合规扫描（不落盘、不抛异常）——供其他路由复用的纯计算入口。

    `include_prompt_zh` 默认关：门禁若把画面提示词直译当文案扫，会稳定误报
    绝对化用语（`minimal affect` → 「最小表情」）而拦死成片。详见 `collect_sources`。
    """
    rules, source = deps.get_compliance_rules()
    srcs = collect_sources(
        pid, db, texts=texts, named=named,
        include_title=include_title, include_prompt_zh=include_prompt_zh,
        include_audio=include_audio,
    )
    return comp.scan_sources(srcs, langs=langs, rules=rules, rules_source=source)


def _sources_from_body(pid: int, body: ProjectScanIn, db: Session) -> List[tuple]:
    return collect_sources(
        pid, db,
        texts=[r.text for r in body.rows],
        named=body.texts,
        include_title=body.include_title,
        include_prompt_zh=body.include_prompt_zh,
        include_audio=body.include_audio,
    )


@router.post("/projects/{pid}/compliance")
def project_compliance(pid: int, body: ProjectScanIn,
                       db: Session = Depends(get_db)):
    """项目的广告法合规报告。`blocked=true` 表示含高危项，不应发布。"""
    _pid(pid, db)
    rules, source = _rules()
    langs = _check_langs(body.langs)
    sources = _sources_from_body(pid, body, db)

    rep = comp.scan_sources(sources, langs=langs, rules=rules, rules_source=source)
    payload = rep.to_dict()
    payload["project_id"] = pid
    payload["scanned"] = [{"source": n, "chars": len(t)} for n, t in sources]
    payload["summary"] = rep.summary_text()
    payload["langs_scanned"] = rep.langs

    if body.persist:
        out_dir = BASE / "data" / "compliance"
        out_dir.mkdir(parents=True, exist_ok=True)
        fp = out_dir / f"p{pid}.json"
        fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")
        db.add(Asset(kind="report", path=str(fp), project_id=pid,
                     meta={"kind": "compliance", "blocked": rep.blocked,
                           "counts": rep.counts, "summary": rep.summary_text()}))
        db.commit()
        payload["saved_to"] = str(fp)

    return payload
