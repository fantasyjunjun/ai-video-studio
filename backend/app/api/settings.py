"""全局设置 API：GET 读取 / PUT 更新（KV 形式）。

目前唯一用途：反推看图的视觉模型覆盖 `reverse_vision_model`。
后续其它"全局、UI 可改、非敏感"的小配置都走这里，无需动 providers.yaml。
"""

from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..services import settings as settings_svc

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def get_settings(db: Session = Depends(get_db)) -> Dict[str, str]:
    return settings_svc.get_all_settings(db)


@router.put("")
def put_settings(body: Dict[str, str], db: Session = Depends(get_db)) -> Dict[str, str]:
    return settings_svc.set_settings(db, body or {})
