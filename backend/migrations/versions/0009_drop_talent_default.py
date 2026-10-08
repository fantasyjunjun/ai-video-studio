"""删除 talent.is_default（默认主播）—— R-32。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-02

背景：用户问「默认主播在功能中起什么作用」。全后端核实：`is_default` 只在
`api/assets.py` 内部读写（列表排序、星标显示、新建首位自动置默认、删除后顺位继承），
**出片 / 生文 / 出图 / 参考图选择全部走 `project.talent_id`**，从未读过该字段。
即它是一个纯装饰字段，删除以免误导用户以为"设了默认就会自动用它"。

SQLite 删列要重建表 —— Alembic 的 `batch_alter_table` 负责这件事（复制到新表 →
拷数据 → 换名）。幂等：列不在就直接返回。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = [c["name"] for c in sa.inspect(bind).get_columns("talent")]
    if "is_default" not in cols:
        return  # 幂等：新库 / 已跑过
    with op.batch_alter_table("talent") as batch:
        batch.drop_column("is_default")


def downgrade() -> None:
    bind = op.get_bind()
    cols = [c["name"] for c in sa.inspect(bind).get_columns("talent")]
    if "is_default" in cols:
        return
    with op.batch_alter_table("talent") as batch:
        batch.add_column(sa.Column("is_default", sa.Integer(),
                                   server_default=sa.text("0")))
