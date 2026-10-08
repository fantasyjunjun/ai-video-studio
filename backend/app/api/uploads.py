"""素材上传与静态读取（商品图 / 主播参考图 / 示例商品图）。

对齐 ClipForge 的 `/api/products/upload` + `/api/files/...` 一对：

    POST /api/uploads/{scope}/{owner}       多文件上传 → 返回持久 URL 列表
    GET  /api/uploads/{scope}/{owner}/{name} 把磁盘上的图喂给 <img src>

三条硬约束：
  - **只收图片**（扩展名 + MIME 双重白名单），单图 ≤20MB，一次最多 5 张
    （商品图上限就是 5，上传接口比表单更早拦住更省事）；
  - **文件名由服务端生成**，绝不用用户传来的原始名（防路径穿越 + 防覆盖）；
  - **删除一律不做**：磁盘素材只增不减，与"脚本永不删除文件"的环境约定一致。
    删商品/主播只删元数据（见 assets.py），图还在。
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..services import storage

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

ALLOWED_MIME = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "image/svg+xml", "image/bmp",
}
MAX_FILES_PER_REQUEST = 5


def _ext_of(name: str, content_type: str) -> str:
    """扩展名优先看文件名，其次按 MIME 兜底；两者都不认识就拒绝。"""
    import os
    ext = os.path.splitext(name or "")[1].lower()
    if ext in storage.IMAGE_EXT:
        return ext
    guess = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/gif": ".gif", "image/svg+xml": ".svg", "image/bmp": ".bmp",
    }.get((content_type or "").split(";")[0].strip().lower())
    if guess:
        return guess
    raise HTTPException(400, f"不支持的文件类型：{name or '(无名)'} / {content_type}")


@router.post("/{scope}/{owner}")
async def upload_images(scope: str, owner: str,
                        files: List[UploadFile] = File(...)):
    """上传一组图片，返回 `{urls: [...], paths: [...]}`。

    `urls` 是给浏览器直接用的（`<img src>` / 存进商品记录）；
    `paths` 是磁盘绝对路径（出片时当参考图喂给供应商，也便于排障）。
    """
    try:
        target = storage.scope_dir(scope, owner)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if not files:
        raise HTTPException(400, "请至少上传一张图片")
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(400, f"一次最多上传 {MAX_FILES_PER_REQUEST} 张图片")

    urls: List[str] = []
    paths: List[str] = []
    for f in files:
        ext = _ext_of(f.filename or "", f.content_type or "")
        raw = await f.read()
        if not raw:
            raise HTTPException(400, f"空文件：{f.filename or '(无名)'}")
        if len(raw) > storage.MAX_IMAGE_BYTES:
            raise HTTPException(
                400,
                f"{f.filename or '文件'} 超过 "
                f"{storage.MAX_IMAGE_BYTES // (1024 * 1024)}MB 大小限制",
            )
        name = storage.new_filename(ext)
        dest = target / name
        # 同名理论上不可能（随机 16 hex），真撞上就**原地覆盖** ——
        # 环境约定禁止 os.remove / unlink，所以不写"先删后建"
        with dest.open("wb") as fh:
            fh.write(raw)
        urls.append(storage.url_for(scope, owner, name))
        paths.append(str(dest))

    return {"scope": scope, "owner": owner, "urls": urls, "paths": paths,
            "count": len(urls)}


@router.get("/{scope}/{owner}/{name}")
def get_image(scope: str, owner: str, name: str):
    """读取已上传的图片。

    单人本地应用，但仍然做目录白名单 + 穿越校验：素材 URL 会写进商品记录并
    被前端直接渲染，一旦能被拼成 `../../` 就是任意文件读取。
    """
    try:
        p = storage.resolve_file(scope, owner, name)
    except ValueError as e:
        raise HTTPException(403, str(e)) from e
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "素材不存在")
    ext = p.suffix.lower()
    if ext not in storage.MIME_BY_EXT:
        raise HTTPException(403, "只允许读取图片素材")
    return FileResponse(
        str(p),
        media_type=storage.MIME_BY_EXT[ext],
        # 文件名由内容决定且不复用，可以放心长缓存
        headers={"Cache-Control": "public, max-age=31536000"},
    )
