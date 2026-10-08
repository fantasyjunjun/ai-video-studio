"""Alembic 环境。

与常规项目的差别只有一处：**连接串不写在 ini 里**，而是复用应用自己的
`app.db.session.DATABASE_URL`（也就是同一个 `DATABASE_URL` 环境变量）。
否则"应用连 A 库、迁移跑到 B 库"这种错位迟早会发生。
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BASE = Path(__file__).resolve().parents[1]  # backend/
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from app.db.models import Base  # noqa: E402
from app.db.session import DATABASE_URL  # noqa: E402

config = context.config

# 连接串的优先级：**调用方显式设置的 > 应用的环境变量**。
# 早先这里是"无条件用 app 的 DATABASE_URL"，结果 `command.upgrade(cfg)` 里
# 指定的 url 被静默覆盖 —— 迁移跑到了另一个库上，老库一列没补，还查不出原因。
_INI_DEFAULT = "sqlite:///./data/app.db"
_url = config.get_main_option("sqlalchemy.url")
if not _url or _url == _INI_DEFAULT:
    _url = DATABASE_URL

# configparser 会把 % 当插值符号，路径里出现就炸，所以先转义
config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
