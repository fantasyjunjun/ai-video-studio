"""资产全文检索（SQLite FTS5）。

为什么单开一层：
  - 资产表只存**路径 + 校验和**，正文（文件名、标签、meta）才是检索对象，
    用 `LIKE '%关键词%'` 在几千行时还行，上万行就全表扫、还没有相关度排序。
  - FTS5 能给 bm25 相关度 + 高亮片段（snippet），检索体验才有"排序"可言。

两个必须处理的现实问题：
  1. **FTS5 不是一定编译进 SQLite 的**（某些发行版裁剪掉了）→
     先探测，没有就老实回退 LIKE，绝不因为缺扩展而让检索接口 500。
  2. **中文没有空格**：unicode61 会把"黑礼服夜戏"当成一个整 token，
     查"夜戏"命中不了。索引时把 CJK **逐字切开**再入库，
     查询串同样处理 —— 用一点索引体积换中文可检索性。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine

FTS_TABLE = "asset_fts"

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]")


def _split_cjk(s: str) -> str:
    """把连续中文/日文切成单字，英文与数字保持原样。"""
    return "".join((f" {c} " if _CJK.match(c) else c) for c in s)


def _doc_of(asset: Any) -> str:
    """一条资产被检索的"正文"：路径 + kind + meta 里的可读文本。"""
    parts = [str(getattr(asset, "path", "") or ""),
             str(getattr(asset, "kind", "") or "")]
    meta = getattr(asset, "meta", None) or {}
    if isinstance(meta, dict):
        for v in meta.values():
            if isinstance(v, (str, int, float)):
                parts.append(str(v))
    return _split_cjk(" ".join(parts))


def _is_engine(x: Any) -> bool:
    """Engine 有 connect()，Connection 没有 —— 这决定要不要自己开事务。"""
    return hasattr(x, "connect")


def _exec(target: Any, sql: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """在 Engine 或 Connection 上执行。

    Alembic 迁移里拿到的是 **Connection**（已经在事务中），再 `begin()` 会抛
    "a transaction is already begun" —— 所以两种入参都得支持，不能只认 Engine。
    """
    if _is_engine(target):
        with target.begin() as conn:
            return conn.execute(text(sql), params or {})
    return target.execute(text(sql), params or {})


def has_fts5(engine: Engine) -> bool:
    """探测 FTS5 是否可用。

    ⚠️ **必须幂等**：探测用的 `temp._fts5_probe` 是**连接级**临时表，而 SQLAlchemy
    的连接池会把不同调用派到不同连接。若某连接上残留了上次没清干净的探针表，
    这次 `CREATE` 就报"已存在" → 误判成"不可用"。表现为同一函数时灵时不灵
    （smoke 里出现过 `has_fts5` 返 False 但 `search` 同一连接却命中）。

    所以探测前先 `DROP IF EXISTS` 清残留，用后再清 —— 不依赖连接是否"干净"。
    """
    try:
        _exec(engine, "DROP TABLE IF EXISTS temp._fts5_probe")
        _exec(engine, "CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
        _exec(engine, "DROP TABLE IF EXISTS temp._fts5_probe")
        return True
    except Exception:  # noqa: BLE001 - 缺扩展 / 只读库，都当作不可用
        return False


def ensure_fts(engine: Engine) -> bool:
    """建 FTS5 虚表（幂等）。返回是否真的可用。"""
    if not has_fts5(engine):
        return False
    _exec(engine, f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE}
        USING fts5(asset_id UNINDEXED, path, kind, doc,
                   tokenize='unicode61 remove_diacritics 2')
    """)
    return True


def drop_fts(engine: Engine) -> None:
    _exec(engine, f"DROP TABLE IF EXISTS {FTS_TABLE}")


def index_asset(engine: Engine, asset: Any) -> None:
    """单条资产入索引（已存在则先按 asset_id 清掉旧的）。"""
    aid = int(getattr(asset, "id") or 0)
    if not aid:
        return
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {FTS_TABLE} WHERE asset_id = :a"),
                     {"a": aid})
        conn.execute(
            text(f"INSERT INTO {FTS_TABLE}(rowid, asset_id, path, kind, doc) "
                 f"VALUES(:r, :a, :p, :k, :d)"),
            {"r": aid, "a": aid,
             "p": str(getattr(asset, "path", "") or ""),
             "k": str(getattr(asset, "kind", "") or ""),
             "d": _doc_of(asset)},
        )


def unindex_asset(engine: Engine, asset_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {FTS_TABLE} WHERE asset_id = :a"),
                     {"a": int(asset_id)})


def reindex(engine: Engine, assets: Sequence[Any]) -> int:
    """全量重建。**先清空再灌**，保证删掉的资产不会留幽灵条目。"""
    if not has_fts5(engine):
        return 0
    ensure_fts(engine)
    n = 0
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {FTS_TABLE}"))
    for a in assets:
        try:
            index_asset(engine, a)
            n += 1
        except Exception:  # noqa: BLE001 - 单条失败不该中断整批
            continue
    return n


def _match_query(q: str) -> str:
    """把用户输入转成 FTS5 MATCH 表达式。

    空格分隔的词视为 AND；每个词再切 CJK。带引号的短语原样保留。
    """
    q = (q or "").strip()
    if not q:
        return ""
    if q.startswith('"') and q.endswith('"') and len(q) > 2:
        return f'"{_split_cjk(q[1:-1]).strip()}"'
    toks = [t for t in re.split(r"\s+", _split_cjk(q)) if t]
    return " AND ".join(f'"{t}"' for t in toks)


def search(engine: Engine, q: str, limit: int = 50, *,
           kind: str = "", project_id: Optional[int] = None
           ) -> Optional[List[Dict[str, Any]]]:
    """FTS5 检索。**返回 None 表示 FTS5 不可用或表达式不合法，调用方回退 LIKE**。

    排序用 bm25：值越小越相关（负数更相关），所以升序。
    """
    if not q or not q.strip():
        return None
    if not has_fts5(engine):
        return None
    ensure_fts(engine)
    expr = _match_query(q)
    if not expr:
        return None
    # 过滤条件写成"空值即不过滤"，同一条 SQL 兼顾各种组合，避免拼字符串
    sql = f"""
        SELECT a.id AS id, a.path AS path, a.kind AS kind,
               a.checksum AS checksum, a.project_id AS project_id,
               a.meta AS meta, a.created_at AS created_at,
               bm25({FTS_TABLE}) AS rank,
               snippet({FTS_TABLE}, 3, '<mark>', '</mark>', '…', 12) AS snippet
        FROM {FTS_TABLE} f
        JOIN asset a ON a.id = f.asset_id
        WHERE {FTS_TABLE} MATCH :m
          AND (:kind = '' OR a.kind = :kind)
          AND (:pid < 0 OR a.project_id = :pid)
        ORDER BY rank
        LIMIT :n
    """
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text(sql),
                {"m": expr, "n": int(limit), "kind": kind or "",
                 "pid": int(project_id) if project_id is not None else -1},
            ).mappings().all()
    except Exception:  # noqa: BLE001 - 用户输入了裸引号之类不合法表达式时回退
        return None
    return [dict(r) for r in rows]
