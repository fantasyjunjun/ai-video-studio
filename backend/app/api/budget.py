"""花费上限 API（P-5）。

三个端点：
    GET  /api/budget          当前花费快照（已花 / 在途 / 各级余额 / 最近拦截）
    PUT  /api/budget          改上限（ruamel 保注释写回 providers.yaml 并热重载）
    POST /api/budget/check    纯试算：这笔如果现在提交，会不会被拦（不改库、不花钱）

设计取向与 P-1 一致：**配置改在页面上，不靠手改文档**；写回仍落在服务端
`providers.yaml`（`budget:` 段），不引入第二个配置文件。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import deps
from ..config import BudgetConfig, ProvidersConfig
from ..db.session import get_db
from ..services import budget as budget_mod
from .providers import write_budget_section

router = APIRouter(prefix="/api/budget", tags=["budget"])


class BudgetPatch(BaseModel):
    """部分更新：**省略 = 不动**；显式 `null` = 删除该键（回到缺省）。

    与 P-1b 的密钥语义区分开：这里没有"空串=删除"的坑，
    因为上限 0 本身就是有意义的取值（= 不限）。
    """

    model_config = {"extra": "allow"}

    enabled: Optional[bool] = None
    period: Optional[str] = None
    per_task_cap_cny: Optional[float] = None
    project_cap_cny: Optional[float] = None
    global_cap_cny: Optional[float] = None
    reserve_ratio: Optional[float] = None
    count_pending: Optional[bool] = None
    on_exceed: Optional[str] = None


class CheckIn(BaseModel):
    project_id: Optional[int] = None
    provider_id: Optional[str] = None
    kind: str = "video"
    estimate_cny: float = Field(default=0.0, ge=0.0)


def _cfg() -> ProvidersConfig:
    return deps.get_providers_config()


@router.get("")
def get_budget(project_id: Optional[int] = None, db: Session = Depends(get_db)):
    """花费快照。`global_remaining_cny=None` 表示未设全局上限。"""
    return budget_mod.snapshot(db, _cfg(), project_id=project_id)


@router.put("")
def put_budget(body: BudgetPatch, project_id: Optional[int] = None,
               db: Session = Depends(get_db)):
    """改上限。写回前先合并 + 校验，**非法配置绝不落盘**。"""
    patch: Dict[str, Any] = body.model_dump(exclude_unset=True)
    if not patch:
        return budget_mod.snapshot(db, _cfg(), project_id=project_id)

    merged = _cfg().budget.model_dump()
    for k, v in patch.items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    try:
        BudgetConfig(**merged)  # 构造即校验（period/on_exceed/reserve_ratio）
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"预算配置非法，未写入：{e}") from e

    write_budget_section(patch)
    return budget_mod.snapshot(db, _cfg(), project_id=project_id)


@router.post("/check")
def check_budget(body: CheckIn, db: Session = Depends(get_db)):
    """纯试算：这笔费用现在提交会不会被拦。不改库、不发请求、不花钱。"""
    d = budget_mod.check(
        db, _cfg(), project_id=body.project_id, provider_id=body.provider_id,
        kind=body.kind, raw_estimate_cny=body.estimate_cny,
    )
    return d.to_dict()
