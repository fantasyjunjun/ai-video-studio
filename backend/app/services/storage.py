"""素材落盘位置与路径安全（商品库 / 主播库 / 示例商品共用）。

为什么单独一个模块：上传、贴链接导入、示例商品三处都要往同一个根目录写，
各自拼一遍 `Path(...)/"uploads"/...` 迟早会漂。路径穿越校验也只该有一份实现。

目录约定（对齐 ClipForge 的 `data/uploads/<scope>/<owner>/`）：

    backend/data/uploads/products/<draftId>/  商品图（手工上传 + 贴链接下载）
    backend/data/uploads/characters/<talentId>/ 主播参考图
    backend/data/uploads/examples/default/    内置示例商品图

**`<draftId>` 不是商品主键**：商品/主播在我们这里是自增整数 id，而表单打开时
还没入库，拿不到 id。所以由前端在打开表单时生成一个随机 draftId，图片先按它
落盘；保存时把返回的 URL 原样写进记录。这与 ClipForge 用 uuid 预生成、
"图片先落盘、确认后才入库"的两阶段做法一致。
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

# 与商品/主播/示例三种用途对齐；白名单化，避免 scope 被拼成 `../`
SCOPES = ("products", "characters", "examples")

# draftId / ownerId 只允许字母数字和连字符（UUID 也在此列）
_SAFE_KEY = re.compile(r"^[a-zA-Z0-9\-_]{1,64}$")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".bmp"}

MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
}

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 单图 20MB

# backend/app/services/storage.py → backend/
BASE = Path(__file__).resolve().parents[2]
UPLOAD_ROOT = BASE / "data" / "uploads"


def safe_key(key: str) -> str:
    """校验并规范化 owner key；不合法直接抛错，绝不"清洗后照用"。"""
    k = (key or "").strip()
    if not _SAFE_KEY.match(k) or ".." in k:
        raise ValueError(f"非法的素材归属 id：{key!r}")
    return k


def scope_dir(scope: str, owner: str, *, mkdir: bool = True) -> Path:
    """`data/uploads/<scope>/<owner>/`，并保证它落在根目录之内。"""
    if scope not in SCOPES:
        raise ValueError(f"未知素材域：{scope!r}")
    root = UPLOAD_ROOT.resolve()
    d = (root / safe_key(scope) / safe_key(owner)).resolve()
    # 双保险：resolve 之后再确认前缀，堵死符号链接绕行
    if d != root and root not in d.parents:
        raise ValueError("素材目录越界")
    if mkdir:
        d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_file(scope: str, owner: str, name: str) -> Path:
    """把 URL 里的三个片段还原成磁盘路径；任何越界一律拒绝。"""
    base = scope_dir(scope, owner, mkdir=False)
    # 文件名只许是**单个纯文件名**：不允许分隔符、不允许 `..`。
    # 只用 Path(name).name 是不够的 —— 在 POSIX 上 `..\\app.db` 里的反斜杠
    # 不是分隔符，`.name` 会原样返回它，于是这条 Windows 写法就成了漏网之鱼。
    # 所以显式把两种分隔符和 `..` 都拒掉，不依赖平台语义。
    fn = Path(name).name
    if (not fn or fn != name or ".." in fn
            or "/" in name or "\\" in name):
        raise ValueError("非法文件名")
    p = (base / fn).resolve()
    if base not in p.parents:
        raise ValueError("素材路径越界")
    return p


def new_filename(ext: str) -> str:
    """服务端生成文件名 —— 不使用用户原始文件名（安全 + 避免重名覆盖）。"""
    e = ext if ext.startswith(".") else f".{ext}"
    if e.lower() not in IMAGE_EXT:
        e = ".jpg"
    return f"{secrets.token_hex(8)}{e.lower()}"


def url_for(scope: str, owner: str, name: str) -> str:
    """素材的对外 URL。持久有效，可跨刷新/跨页面直接塞进 <img src>。"""
    return f"/api/uploads/{scope}/{owner}/{name}"
