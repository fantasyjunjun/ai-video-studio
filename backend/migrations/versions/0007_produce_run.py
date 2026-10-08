"""一键出片运行表 produce_run。

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-30

背景：产品形态改成「脚本生成 → 直接一键出 15s 成片」之后，中间不再有人工停顿。
但这条链路要花 N 笔出片的钱、耗时可达十几分钟，**没有一条运行记录，界面就无法给出
进度，失败也无从定位**（哪一镜挂了？卡在出片还是混音？钱花在哪？）。

所以新增 `produce_run`：

    + status             queued / running / done / failed
    + stage              script / render / compose / done（当前在哪个阶段）
    + steps      JSON    逐步骤状态 [{key,label,status,message,at}]
    + plan       JSON    花钱前那份规划快照（镜数 / 帧数 / 念白行 / 预估花费）
    + report     JSON    分步产出报告 + final_path
    + script_asset_id    这一版成片用的是哪份脚本素材

纯新增表，不动任何既有列，可安全前进；幂等（表已存在就跳过）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _tables(bind) -> set:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    if "produce_run" in _tables(bind):
        return  # 幂等：老库重复跑也收敛到同一形态
    op.create_table(
        "produce_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(),
                  sa.ForeignKey("project.id"), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'queued'")),
        sa.Column("stage", sa.String(), server_default=sa.text("'script'")),
        sa.Column("message", sa.Text(), server_default=sa.text("''")),
        sa.Column("script_asset_id", sa.Integer(),
                  sa.ForeignKey("asset.id"), nullable=True),
        sa.Column("plan", sa.JSON(), server_default=sa.text("'{}'")),
        sa.Column("steps", sa.JSON(), server_default=sa.text("'[]'")),
        sa.Column("report", sa.JSON(), server_default=sa.text("'{}'")),
        sa.Column("cost_estimate_cny", sa.Float(), server_default=sa.text("0")),
        sa.Column("cost_cny", sa.Float(), server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_produce_run_project", "produce_run", ["project_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if "produce_run" not in _tables(bind):
        return
    op.drop_table("produce_run")
