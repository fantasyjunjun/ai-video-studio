"""基线：把库**收敛**到当前 ORM metadata。

Revision ID: 0001
Revises:
Create Date: 2026-09-28

为什么这版是"收敛式"而不是"演进式"：
项目早期 schema 变动频繁，而且已经有一批本地库在跑 —— 此前靠
`init_db()` 里的一段 ALTER 自愈补列（比如 `image_host_lease.namespace`）。
那段自愈逻辑就搬到这里：缺的表建出来，已有表缺的列补上，**幂等**。

基线跑完之后，后续 schema 变更才写成标准增量迁移（见 0002）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())

    # 1) 缺的表建出来（create_all 自带 checkfirst，已存在的会跳过）
    Base.metadata.create_all(bind=bind)

    # 2) 已存在的表补列 —— create_all **不会**给已有表加列，这部分必须手写
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            op.add_column(table.name, sa.Column(col.name, col.type, nullable=True))


def downgrade() -> None:
    """基线不回滚：它只是"补齐到当前"，回滚没有明确语义。"""
    pass
