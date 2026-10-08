"""全局键值设置服务。

为什么单独一张 KV 表、而不是塞进 providers.yaml：
  - providers.yaml 是"供应商唯一真相源"，且**密钥走 OS 凭据库、不进文件**；
  - 反推视觉模型这类"一个值、全局生效、UI 可改"的小配置，放 YAML 既强迫用户改文件，
    又和"配置不进明文"的约定混在一起，容易踩坑。
  - 所以这类设置走 `/api/settings`（SQLite KV），设置页直接编辑，零文件改动。

DEFAULTS 是已知键的默认值；未登记的键按 get_setting 的 default 参数返回。
"""

from __future__ import annotations

from typing import Dict, Optional

from sqlalchemy.orm import Session

from ..db.models import Setting

# 已知设置键及其默认值（前端/后端共享语义）
DEFAULTS: Dict[str, str] = {
    # 反推看图的视觉模型覆盖：留空（""）表示使用当前 active LLM 的模型；
    # 填上中转站上的更快视觉模型名（如 gpt-4o / claude-sonnet-*）可加速看图反推。
    # 注意：它复用当前 active LLM 的 base_url 与同一把密钥，只覆盖模型名——
    # 不需要（也不应该）单独建一条供应商条目（那条会查不到凭据库里的密钥）。
    "reverse_vision_model": "",
    # 反推看图单次输出的 token 上限。逐镜要写「英文七层提示词 + 中文直译」，
    # 一份 10~15 镜的输出很长；上限太小会让 JSON 在数组中途被腰斩，
    # 表现就是那句迷惑的"LLM 输出不是合法 JSON"。流式（D）落地后已不受
    # Cloudflare 100s 约束，所以这里给足 8000，仍可再调大。
    "reverse_vision_max_tokens": "8000",
}


def get_setting(db: Session, key: str, default: Optional[str] = None) -> Optional[str]:
    row = db.get(Setting, key)
    if row is None:
        return DEFAULTS.get(key, default)
    return row.value


def get_int_setting(db: Session, key: str, default: int) -> int:
    """取整数设置：非法/非正数一律回退 default（配置脏了不能把功能带崩）。"""
    raw = get_setting(db, key, str(default))
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def get_all_settings(db: Session) -> Dict[str, str]:
    out: Dict[str, str] = dict(DEFAULTS)
    for row in db.query(Setting).all():
        out[row.key] = row.value
    return out


def set_setting(db: Session, key: str, value: str) -> Setting:
    row = db.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()
    return row


def set_settings(db: Session, updates: Dict[str, str]) -> Dict[str, str]:
    """批量更新；只接受 DEFAULTS 里登记过的键，未知键忽略（防止误写脏数据）。"""
    for key, value in updates.items():
        if key not in DEFAULTS:
            continue
        set_setting(db, key, "" if value is None else str(value))
    return get_all_settings(db)
