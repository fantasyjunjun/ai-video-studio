"""主播形象选项化：spec / preset。

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29

背景：之前「生成形象提示词」的输入只有年龄 / 性别 / 国籍三项，剩下的全由文本大模型
自由扩写 —— 于是服装、场景、光线这些决定画面观感的变量要么被系统提示词明确禁掉，
要么被硬编码在模板里，用户无从选择，出图质量全凭运气。

这次把形象选项化（词表见 `app/pipeline/talent_spec.py`）：

    + spec     JSON   十六个维度的选中值 —— 服装 / 妆容 / 发型 / 配饰 / 场景 /
                      光线 / 姿势 / 构图 / 色调 / 风格 / 脸型 / 肤色 / 身材 / 气质 …
                      它是 `appearance` 那串生成结果的**来源**。
    + preset   String 最后套过的人设预设，仅用于把界面回填到用户上次的选择。

**为什么是一个 JSON 列而不是按层拆三列**：层（identity / styling / render）是词表的
组织方式，不是数据的固有属性。哪天才发现"表情其实该归画面层"，拆成列就得再迁移一次。
存一份完整的 `spec`，让层划分留给运行时代码去解释。

两条 column 都**只加不删**，`appearance` 那列原封不动（它是既有资产，也是下游
跨镜复用的锚点），所以这是可安全前进的变更。

幂等：先查列集合，缺啥补啥；老库新库重复跑都收敛到同一形态。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# 字符串默认值必须写成 `sa.text("''")`。**直接传 `"''"` 会被当字面量**，落库后
# 字段值就是两个单引号字符（`0005` 早期版本踩过，下拉框里全是 `''`）。
TALENT_ADD = [
    ("spec", sa.JSON(), sa.text("'{}'")),
    ("preset", sa.String(), sa.text("''")),
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

    # 老主播没有这些数据。历史上可能存在 NULL（手工改过库），这里统一兜成 {}，
    # 免得前端 `Object.keys(null)` 直接炸 —— 空 dict 表示"这一层没选"，是合法状态。
    op.execute("UPDATE talent SET spec = '{}' WHERE spec IS NULL OR spec = ''")
    op.execute("UPDATE talent SET preset = '' WHERE preset IS NULL OR preset = \"''\"")


def downgrade() -> None:
    bind = op.get_bind()
    have = _cols(bind, "talent")
    if not have:
        return
    with op.batch_alter_table("talent") as b:
        for name, _, _ in TALENT_ADD:
            if name in have:
                b.drop_column(name)
