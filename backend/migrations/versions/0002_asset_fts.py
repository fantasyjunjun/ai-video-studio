"""资产全文索引（FTS5 虚表）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28

建表 SQL 复用 `app.db.fts`，不在这里抄一份 —— 否则两边迟早不一致。
**FTS5 不是每个 SQLite 都编译进去了**，建不上就跳过：检索会回退到 LIKE，
这是能力降级，好过因为缺扩展让整个库起不来。
"""

from __future__ import annotations

from alembic import op

from app.db.fts import ensure_fts, has_fts5

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if has_fts5(conn):
        ensure_fts(conn)


def downgrade() -> None:
    from app.db.fts import drop_fts

    drop_fts(op.get_bind())
