"""主播库重构：基本信息（年龄/性别/国籍）+ 形象提示词来源。

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-29

主播的产出链条改成三段「基本信息 → 形象提示词 → 参考图」，因此补上选角的
三个硬维度与提示词来源：

    + age                Integer  必填。年龄段决定脸与体态的生成先验，
                                  缺了它模型只能从"女性"两个字里瞎猜。
    + gender             String   必填。female / male / neutral。
    + nationality        String   必填。族裔气质，跨镜必须一致。
    + appearance_source  String   manual / llm —— 提示词是手写的还是模型生成的。

**只加列、不删列**，所以这是可安全前进的变更；downgrade 把四列删掉即可，
`appearance` 本身不动（那是既有资产，丢不得）。

幂等：先查列集合，缺啥补啥；老库、新库重复跑都收敛到同一形态。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# 字符串默认值必须写成 `sa.text("''")`。**直接传 `"''"` 会被当字面量**，
# 落库后字段值就是两个单引号字符（实测：真实库里 gender 变成了 `''`），
# 前端于是显示 `''` 而不是空白。整型不受影响（`"0"` 是合法 SQL 字面量）。
TALENT_ADD = [
    ("age", sa.Integer(), None),
    ("gender", sa.String(), sa.text("''")),
    ("nationality", sa.String(), sa.text("''")),
    ("appearance_source", sa.String(), sa.text("''")),
]


def _cols(bind, table: str) -> set:
    insp = sa.inspect(bind)
    if table not in set(insp.get_table_names()):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    have = _cols(bind, "talent")
    if not have:
        return
    with op.batch_alter_table("talent") as b:
        for name, type_, default in TALENT_ADD:
            if name not in have:
                b.add_column(sa.Column(name, type_, server_default=default))
    # 老主播没有基本信息，接口层会提示补齐；这里只保证不是 NULL，前端不会崩。
    # 顺带把"值就是两个单引号"的坏数据清掉（本迁移早期版本写错过默认值，
    # 已升级过的库需要靠这一句自愈，否则下拉框里永远是 `''`）
    op.execute("UPDATE talent SET gender = '' WHERE gender IS NULL OR gender = \"''\"")
    op.execute("UPDATE talent SET nationality = '' "
               "WHERE nationality IS NULL OR nationality = \"''\"")
    op.execute("UPDATE talent SET appearance_source = '' WHERE appearance_source = \"''\"")
    op.execute("UPDATE talent SET appearance_source = "
               "CASE WHEN appearance IS NOT NULL AND appearance <> '' "
               "THEN 'manual' ELSE '' END "
               "WHERE appearance_source IS NULL OR appearance_source = ''")


def downgrade() -> None:
    bind = op.get_bind()
    have = _cols(bind, "talent")
    if not have:
        return
    with op.batch_alter_table("talent") as b:
        for name, _, _ in TALENT_ADD:
            if name in have:
                b.drop_column(name)
