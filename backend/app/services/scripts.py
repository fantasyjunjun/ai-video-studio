"""项目脚本素材：把"生成/导入的分镜脚本"落盘并登记成项目资产。

**为什么要留**：脚本是这一版成片的"事实源" —— 镜号、时长、念白稿、音效落点
全在里面。此前它只在弹窗里显示一次、关掉即失；用户回头想复核"当时脚本怎么写的"
或者想换语言重做一版念白，就无从下手。而且一键成片要靠它自动驱动 TTS 与混音。

设计要点：
  - **一版一条**，按 id 倒序即"最新"。同内容（md5 相同）重复保存会复用上一条，
    避免用户连点两次生成就多出一条一模一样的记录。
  - 大文件照例**只存路径**：markdown 写到 `data/scripts/p{pid}/`，库里存 Asset。
  - 解析出的**念白行与音效落点直接塞进 `Asset.meta`**：一键成片据此工作，
    不必再去重新解析一遍，也不必给 Shot 加列做迁移。
  - **只写不删**：本环境的安全钩子会在 `os.remove` 处中止进程，所以文件一律
    新建（带时间戳），绝不覆盖或删除既有版本。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from ..db import fts as fts_mod
from ..db.models import Asset
from ..pipeline.script_import import parse_post_plan, parse_script

# backend/data/scripts/p{pid}/<时间戳>-<来源>.md
SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "data" / "scripts"

_TITLE_RE = re.compile(r"^#\s+(?P<t>.+?)\s*$", re.MULTILINE)
_SAFE_RE = re.compile(r"[^0-9A-Za-z_\-]+")

# 来源标记。agent = Trae 生成；paste = 用户粘贴外部脚本；import = 其它导入路径
SOURCE_LABELS = {
    "agent": "Agent 生成",
    "paste": "粘贴外部脚本",
    "import": "导入",
    "manual": "手工录入",
}


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _guess_title(text: str) -> str:
    m = _TITLE_RE.search(text or "")
    return m.group("t").strip() if m else ""


def _next_path(pid: int, source: str) -> Path:
    """给新版本挑一个不冲突的文件名。**不覆盖、不删除**既有的任何版本。"""
    d = SCRIPTS_ROOT / f"p{pid}"
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{ts}-{_SAFE_RE.sub('', source) or 'script'}"
    p = d / f"{stem}.md"
    i = 2
    while p.exists():
        p = d / f"{stem}-{i}.md"
        i += 1
    return p


def latest_script(db: Session, project_id: int) -> Optional[Asset]:
    """该项目最近一版脚本（没有则 None）。"""
    return (db.query(Asset)
            .filter(Asset.project_id == project_id, Asset.kind == "script")
            .order_by(Asset.id.desc()).first())


def read_script(asset: Asset) -> str:
    """读回脚本文本。**读不出来时返回空串，而不是抛异常**。

    这里必须捕**所有**异常，不能只捕 `OSError`：脚本内容是用户/模型产出的，
    编码不一定是 UTF-8 —— 实测 GBK 编码的脚本会让 `read_text(encoding="utf-8")`
    抛 `UnicodeDecodeError`，而它**不是** OSError 的子类，于是穿透到
    `/produce` 接口变成 500（便携版首次出片就撞到，用户只看到
    「500 Internal Server Error」，真因完全不可见）。
    UTF-8 带 BOM（`utf-8-sig`）也要能读 —— 记事本另存为的常见形态。
    """
    try:
        raw = Path(asset.path).read_bytes()
    except Exception:  # noqa: BLE001 - 文件不存在/无权限/路径怪 → 一律当"没脚本"
        return ""
    # 先按 UTF-8 试；带 BOM 用 utf-8-sig 能吃下；实在不是 UTF-8 再兜底 gbk。
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def save_script(db: Session, *, project_id: int, script: str,
                source: str = "import", title: str = "",
                language: str = "", dedupe: bool = True,
                extra_meta: Optional[Dict] = None) -> Asset:
    """落盘 + 登记。返回 Asset 行。

    `dedupe=True` 时，若与**最近一版内容完全相同**则原样返回那一条 ——
    用户连点两次「导入」不该在资产里留下两条一模一样的脚本。
    """
    text = script or ""
    if not text.strip():
        raise ValueError("脚本内容为空，不登记为素材")

    digest = _md5(text)
    if dedupe:
        last = latest_script(db, project_id)
        if last is not None and (last.checksum or "") == digest:
            return last

    path = _next_path(project_id, source)
    # **新建**写入：本环境禁止删除/覆盖，安全钩子会在 os.remove 处中止进程
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    parsed, warnings = parse_script(text)
    plan = parse_post_plan(text)

    asset = Asset(
        kind="script",
        path=str(path),
        checksum=digest,
        project_id=project_id,
        meta={
            "source": source,
            "source_label": SOURCE_LABELS.get(source, source),
            "title": title or _guess_title(text),
            "language": language,
            "chars": len(text),
            "shot_count": len(parsed),
            "total_sec": round(sum(s.duration_sec for s in parsed), 3),
            "shots": [
                {"code": s.code, "duration_sec": s.duration_sec,
                 "start_sec": s.start_sec, "end_sec": s.end_sec}
                for s in parsed
            ],
            # 一键成片直接读这两个字段，不再重复解析
            "vo_rows": plan.vo_rows,
            "sfx_cues": plan.sfx_cues,
            "warnings": list(warnings) + list(plan.warnings),
            **(extra_meta or {}),
        },
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)

    # 索引自维护：素材检索页已移除，不再有"重建索引"的人工入口。
    # 索引失败不该让脚本登记失败 —— 文件与台账都已就绪。
    try:
        fts_mod.index_asset(db.get_bind(), asset)
    except Exception:  # noqa: BLE001
        pass
    return asset


def script_out(asset: Asset, *, preview_chars: int = 240) -> Dict[str, object]:
    """给前端的摘要（**不含全文**；全文走 `/api/file?path=`）。"""
    m = asset.meta or {}
    text = read_script(asset)
    rows = m.get("vo_rows") or []
    cues = m.get("sfx_cues") or []
    return {
        "id": asset.id,
        "project_id": asset.project_id,
        "path": asset.path,
        "created_at": asset.created_at.isoformat() if asset.created_at else None,
        "source": m.get("source"),
        "source_label": m.get("source_label"),
        "title": m.get("title"),
        "language": m.get("language"),
        "chars": m.get("chars"),
        "shot_count": m.get("shot_count"),
        "total_sec": m.get("total_sec"),
        "vo_row_count": len(rows),
        "sfx_cue_count": len(cues),
        "warnings": m.get("warnings") or [],
        "preview": " ".join(text.split())[:preview_chars],
    }


def list_scripts(db: Session, project_id: int) -> List[Dict[str, object]]:
    rows = (db.query(Asset)
            .filter(Asset.project_id == project_id, Asset.kind == "script")
            .order_by(Asset.id.desc()).all())
    return [script_out(r) for r in rows]
