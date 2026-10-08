"""贴链接导入：粘贴商品详情页 URL → 抓取解析 → 图片落盘 → 回填表单核对。

对齐 ClipForge 的 `/api/ingest/product`（`libraryProductId` 模式）。

**这一步绝不入库。** 它只回一份"提议"：名称/价格/卖点/图片全都可以是错的
（页面改版、提取到的是推荐位文案等），所以前端把结果填进**同一个可编辑表单**，
用户核对并点保存之后才真正进商品库。这个 review gate 是刻意的，
不要图省事在接口里顺手插一条商品记录。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..net import safe_fetch as sf
from ..pipeline import product_ingest as ing
from ..services import storage

router = APIRouter(prefix="/api", tags=["ingest"])

MAX_HTML_BYTES = 3 * 1024 * 1024
MAX_IMAGE_BYTES = storage.MAX_IMAGE_BYTES

_EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp",
}


class IngestIn(BaseModel):
    url: str
    # 图片落盘归属：前端打开表单时生成的随机串（此时商品还没入库、没有主键）
    draft_id: str = Field(default="", alias="draftId")

    model_config = {"populate_by_name": True}


def _download_image(url: str, scope: str, owner: str, idx: int) -> Optional[str]:
    """下载一张商品图并落盘，返回可持久访问的 URL；失败返回 None。

    单张失败**不中止整个导入** —— 商品页里常有装饰图/统计像素，抽不回来很正常，
    为一张图让用户白等一次抓取不划算。全部失败才算失败。
    """
    try:
        resp = sf.safe_fetch(url, timeout=20.0)
    except Exception:  # noqa: BLE001 - 含 SSRF 拒绝与网络错误
        return None
    if resp.status_code != 200:
        return None

    mime = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = _EXT_BY_MIME.get(mime)
    if ext is None:
        # content-type 不可信（很多 CDN 返回 octet-stream），退回按 URL 后缀猜
        guess = url.split("?")[0].rsplit(".", 1)
        cand = f".{guess[1].lower()}" if len(guess) == 2 else ""
        ext = cand if cand in storage.IMAGE_EXT else None
    if ext is None:
        return None

    raw = resp.content
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        return None

    target = storage.scope_dir(scope, owner)
    name = storage.new_filename(ext)
    with (target / name).open("wb") as fh:  # 原地覆盖，不删除
        fh.write(raw)
    return storage.url_for(scope, owner, name)


@router.post("/ingest/product")
def ingest_product(body: IngestIn) -> Dict[str, Any]:
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(400, "请填写商品链接")
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "请填写合法的商品链接（http/https）")

    # ---- 抓页面（15s 超时 + 3MB 上限 + 逐跳 SSRF 校验）----
    try:
        resp = sf.safe_fetch(
            url, headers={"Accept": "text/html,application/xhtml+xml,*/*"},
        )
    except ValueError as e:
        raise HTTPException(400, f"链接被拒绝：{e}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"抓取商品页失败：{type(e).__name__}: {e}") from e

    if resp.status_code != 200:
        raise HTTPException(502, f"抓取商品页失败：HTTP {resp.status_code}")

    ctype = (resp.headers.get("content-type") or "").lower()
    if "text/html" not in ctype and "xhtml" not in ctype:
        raise HTTPException(415, "该链接不是网页（非 HTML），无法解析")

    html = resp.content[:MAX_HTML_BYTES].decode("utf-8", "ignore")
    parsed = ing.parse_product_from_html(html, url)

    if not parsed["title"] and not parsed["images"]:
        raise HTTPException(
            422,
            "没能从该链接解析出商品信息，请改用手动填写",
        )

    # ---- 图片落盘（最多 3 张）----
    images: List[str] = []
    if body.draft_id:
        try:
            storage.safe_key(body.draft_id)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        for i, img in enumerate(ing.pick_ingest_images(parsed["images"])):
            u = _download_image(img, "products", body.draft_id, i)
            if u:
                images.append(u)

    return {
        "product": {
            "title": parsed["title"],
            "price_text": parsed["price_text"],
            "description": parsed["description"],
        },
        "images": images,
        "source_url": url,
        # 抽到的图总数可能多于已下载的张数，把这个差值透出去便于排障
        "images_found": len(parsed["images"]),
    }
