"""新增 settings KV 表（全局键值设置）—— R-32b-D。

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-02

背景：反推看图的"视觉模型覆盖"（`reverse_vision_model`）需要 UI 可配，但又不能进
providers.yaml（那是供应商"唯一真相源"，且密钥走凭据库不进文件）。所以它放进一个
独立的轻量 KV 表，由 `/api/settings` 读写，设置页直接编辑，无需改文件。

幂等：表已存在就直接返回。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # 注意：`get_table_names()` 返回的是**字符串列表**（不像 `get_columns()` 返回 dict），
    # 所以这里绝不能写 `t["name"]` —— 那会抛 TypeError 让整个 upgrade 失败、连带
    # alembic_version 停在上一版（R-32b-D 踩过）。
    tables = set(sa.inspect(bind).get_table_names())
    if "settings" in tables:
        return  # 幂等：新库 / 已跑过
    op.create_table(
        "settings",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.Text(), server_default=sa.text("''")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "settings" not in tables:
        return
    op.drop_table("settings")
