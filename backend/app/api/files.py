"""本地文件预览（成片播放 / 抽帧图查看）。

单人本地应用，所以允许按绝对路径取文件；但加一道可选白名单：
设了 `AVS_FILE_ROOTS`（`; ` 分隔）就只允许这些前缀，防止哪天把服务暴露出去变成任意文件读取。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api", tags=["files"])

MIME = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp", ".wav": "audio/wav",
    ".mp3": "audio/mpeg", ".json": "application/json", ".txt": "text/plain",
}


def allowed(path: Path) -> bool:
    raw = os.environ.get("AVS_FILE_ROOTS", "").strip()
    if not raw:
        return True
    roots = [r.strip() for r in raw.replace("\n", ";").split(";") if r.strip()]
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(str(resolved).lower().startswith(str(Path(r).resolve()).lower())
               for r in roots)


@router.get("/file")
def serve_file(path: str):
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, f"文件不存在：{path}")
    if not allowed(p):
        raise HTTPException(403, "路径不在 AVS_FILE_ROOTS 白名单内")
    return FileResponse(str(p), media_type=MIME.get(p.suffix.lower()))


@router.get("/file/head")
def file_head(path: str):
    """只要元信息（大小/是否存在），不搬字节。前端列表页用它标红"文件已被移走"。"""
    p = Path(path)
    return {"path": str(p), "exists": p.exists() and p.is_file(),
            "size": p.stat().st_size if p.exists() and p.is_file() else 0,
            "readable": allowed(p) if p.exists() else False}
