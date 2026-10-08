"""渲染任务台账 render_task（P-2）。

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28

建表 + 补列都做**幂等**处理：老库（已经跑过 0001/0002）只缺这一张表；
万一库里已经有半张表（手工建过），也把缺的列补上，而不是报错中断。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.models import RenderTask

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existed = "render_task" in set(insp.get_table_names())

    # 缺表就建（checkfirst 幂等）；已有表则跳过
    RenderTask.__table__.create(bind=bind, checkfirst=True)

    # 已有半张表（少见）时补缺失的列 —— create 不会给已存在的表加列
    if existed:
        have = {c["name"] for c in insp.get_columns("render_task")}
        for col in RenderTask.__table__.columns:
            if col.name in have:
                continue
            op.add_column("render_task", sa.Column(col.name, col.type, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "render_task" in set(insp.get_table_names()):
        op.drop_table("render_task")
