"""商品图用途标记 product.image_meta（R-16）。

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-30

背景：实测穿帮 —— 一个商品混着「两张不同型号瓶身照 + 两张香调信息图」，
`refs._product_refs` 全量发给模型，一镜里画出两只瓶子。给每张商品图打用途
标记（瓶身照 / 信息图 / 海报 / 其他），只有瓶身照进 i2v 参考图。

    + image_meta   JSON    与 images 下标对齐的 [{"role": …}]
                           缺省按 bottle（老数据行为不变）

纯新增列，不动任何既有列，可安全前进；幂等（列已存在就跳过）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = [c["name"] for c in sa.inspect(bind).get_columns("product")]
    if "image_meta" in cols:
        return  # 幂等：老库重复跑也收敛到同一形态
    op.add_column(
        "product",
        sa.Column("image_meta", sa.JSON(), server_default=sa.text("'[]'"),
                  nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    cols = [c["name"] for c in sa.inspect(bind).get_columns("product")]
    if "image_meta" not in cols:
        return
    op.drop_column("product", "image_meta")
