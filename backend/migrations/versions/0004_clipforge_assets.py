"""资产模块按 ClipForge 1:1 重构（商品库 + 主播库）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28

**这是一次有意的破坏性变更，不是 bug。**

商品表：
  + category / description / images / price / target_audience
  - notes_level / notes_card_path / notes_json
  - 连带停用"香调事实分级（铁律 15）"门禁 —— 用户明确选择按 ClipForge 的
    字段模型"完全替换"，商品事实不再分级、卖点描述一律可写。
    代价是失去"无来源事实拦截"，改由表单人工把关。**不要擅自改回去。**

主播表：
  ~ anchor_md → appearance（同一职责，语义收敛为"外貌特征"）
  + description / voice_style / is_default
  改名走"先加列 → 搬数据 → 再删旧列"，**已填的锚点不会丢**。

幂等：每个变更前先查当前列集合，缺啥补啥、多啥删啥；老库、新库、半迁移库
重复跑都收敛到同一形态。SQLite 走 batch_alter_table（重建表）来支持删列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

PRODUCT_ADD = [
    ("category", sa.String(), "other"),
    ("description", sa.Text(), None),
    ("images", sa.JSON(), None),
    ("price", sa.String(), None),
    ("target_audience", sa.String(), None),
]
PRODUCT_DROP = ["notes_level", "notes_card_path", "notes_json"]

TALENT_ADD = [
    ("description", sa.String(), None),
    ("voice_style", sa.String(), None),
    ("is_default", sa.Integer(), "0"),
]


def _cols(bind, table: str) -> set:
    insp = sa.inspect(bind)
    if table not in set(insp.get_table_names()):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # ---------------- 商品表 ----------------
    have = _cols(bind, "product")
    if have:
        with op.batch_alter_table("product") as b:
            for name, type_, default in PRODUCT_ADD:
                if name not in have:
                    b.add_column(sa.Column(name, type_, server_default=default))
            for name in PRODUCT_DROP:
                if name in have:
                    b.drop_column(name)
        # 老库回填：商品图缺省为空列表，避免前端拿到 NULL 崩在 .length 上
        op.execute("UPDATE product SET images = '[]' WHERE images IS NULL")
        op.execute("UPDATE product SET category = 'other' WHERE category IS NULL")

    # ---------------- 主播表 ----------------
    have = _cols(bind, "talent")
    if have:
        with op.batch_alter_table("talent") as b:
            for name, type_, default in TALENT_ADD:
                if name not in have:
                    b.add_column(sa.Column(name, type_, server_default=default))
            if "appearance" not in have:
                b.add_column(sa.Column("appearance", sa.Text()))

        # 搬数据：anchor_md 里已填的锚点必须落到 appearance，改名不是清空
        if "anchor_md" in have:
            op.execute(
                "UPDATE talent SET appearance = anchor_md "
                "WHERE (appearance IS NULL OR appearance = '') "
                "AND anchor_md IS NOT NULL"
            )
            with op.batch_alter_table("talent") as b:
                b.drop_column("anchor_md")

        op.execute("UPDATE talent SET is_default = 0 WHERE is_default IS NULL")

        # 老库回填默认主播：重构前没有 is_default 这个概念，迁移后全部是 0，
        # 前端"默认主播"星标会全空、也失去兜底选角。按**最早一位**补上，口径与 API
        # 的删除顺位逻辑一致（都是 id 升序）。只在"一个默认都没有"时才补，
        # 不覆盖用户已有的选择。
        has_default = bind.execute(
            sa.text("SELECT id FROM talent WHERE is_default = 1 LIMIT 1")).scalar()
        if has_default is None:
            first_id = bind.execute(sa.text("SELECT MIN(id) FROM talent")).scalar()
            if first_id is not None:
                bind.execute(sa.text("UPDATE talent SET is_default = 1 WHERE id = :i"),
                             {"i": first_id})


def downgrade() -> None:
    bind = op.get_bind()

    have = _cols(bind, "product")
    if have:
        with op.batch_alter_table("product") as b:
            if "notes_level" not in have:
                b.add_column(sa.Column("notes_level", sa.String(), server_default="C"))
            if "notes_card_path" not in have:
                b.add_column(sa.Column("notes_card_path", sa.String()))
            if "notes_json" not in have:
                b.add_column(sa.Column("notes_json", sa.JSON()))
            for name, _, _ in PRODUCT_ADD:
                if name in have:
                    b.drop_column(name)

    have = _cols(bind, "talent")
    if have:
        with op.batch_alter_table("talent") as b:
            if "anchor_md" not in have:
                b.add_column(sa.Column("anchor_md", sa.Text()))
        if "anchor_md" in have:
            op.execute("UPDATE talent SET anchor_md = appearance WHERE anchor_md IS NULL")
        with op.batch_alter_table("talent") as b:
            for name, _, _ in TALENT_ADD:
                if name in have:
                    b.drop_column(name)
            if "appearance" in have:
                b.drop_column("appearance")
