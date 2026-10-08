"""shot 表新增「逐镜商品分配」与「章节级造型」两列 —— R-33。

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-02

背景（复刻失败复盘的两个根因）

  `products`（JSON，字符串数组）
      多商品项目里，出片阶段把**项目全部商品**的图都挂给了每一镜 ——
      A1 明明是金瓶静物，参考图里却带着黑瓶；提示词里每镜也都写着"两瓶"。
      模型只能靠文本自己消歧，运气好纠偏、运气不好就两瓶同框。
      现在骨架必须给出「这一镜用哪个商品」，落到 shot 表，`refs.py` 按它筛图。

  `talent_look`（String）
      全片每镜都用同一位主播的同一套造型锚点，于是原片"白天 / 夜晚"章节在
      服装层的对比被抹平。允许按镜覆盖服装/发型（身份锚点仍由参考图承载）。

幂等：列已存在就跳过。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _cols(bind) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns("shot")}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _cols(bind)
    if "products" not in existing:
        op.add_column("shot", sa.Column("products", sa.JSON(), nullable=True))
    if "talent_look" not in existing:
        op.add_column("shot", sa.Column("talent_look", sa.String(),
                                        server_default=sa.text("''")))


def downgrade() -> None:
    bind = op.get_bind()
    existing = _cols(bind)
    if "talent_look" in existing:
        op.drop_column("shot", "talent_look")
    if "products" in existing:
        op.drop_column("shot", "products")
