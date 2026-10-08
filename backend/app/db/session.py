"""数据库会话。

SQLite 单文件起步；切 PostgreSQL 只需改 DATABASE_URL（SQLAlchemy 方言无关）。
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

BASE = Path(__file__).resolve().parents[2]  # backend/
DATA_DIR = BASE / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, future=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _alembic_config():
    """拼 Alembic 配置。**路径全用绝对路径** —— 否则换个工作目录就找不到迁移。"""
    from alembic.config import Config

    cfg = Config(str(BASE / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE / "migrations"))
    # configparser 把 % 当插值符号，连接串里出现要先转义
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
    return cfg


def init_db() -> None:
    """建库 / 演进。

    以前这里是 `create_all` + 一段 ALTER 自愈；那段自愈已经搬进
    **Alembic 的基线迁移**（0001），所以新库和老库现在走同一条路：
    `alembic upgrade head`。

    Alembic 缺失或迁移目录损坏时才降级为 `create_all` —— 能跑起来，
    但以后再加字段不会自动补，所以降级会打警告而不是悄悄过去。
    """
    from .models import Base  # 局部导入避免循环

    try:
        from alembic import command

        command.upgrade(_alembic_config(), "head")
        return
    except Exception as e:  # noqa: BLE001
        print(f"[init_db] Alembic 迁移不可用（{type(e).__name__}: {e}），"
              f"降级为 create_all（新增字段不会自动补列）", flush=True)

    Base.metadata.create_all(engine)


def get_db():
    """FastAPI 依赖。"""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
