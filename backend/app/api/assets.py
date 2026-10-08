"""资产管理 API：商品库 / 主播库。

按 ClipForge 的资产模块 1:1 重构（用户 2026-09-28 定案）：

    商品库  `/api/products`  名称 / 品类 / 卖点描述 / 多图 / 价格 / 目标人群
    主播库  `/api/talents`   基本信息 → 形象提示词 → 参考图

**主播的产出链条是三段**（2026-09-29 重构）：

    1. 基本信息（必填：名称 / 年龄 / 性别 / 国籍；选填：外貌补充 / 声线）
    2. 形象提示词 `appearance` —— 调**文本大模型**把基本信息扩写成一段可画的
       人物描述；不满意可手改，或带上一版再生成（会明确要求换一套外貌）。
    3. 参考图 —— 拿提示词调**文生图**生成正面图 / 人物三视图；已有模特图的
       走上传域直接导入。产物一律留在主播库，可复用。

第 2 步与第 3 步分离是有意的：**生图要花钱，扩写几乎不花钱**。让人在便宜的
那一步反复试到满意，再按下花钱的按钮。

几条硬约束：

  - **图片走上传域**（`app/api/uploads.py`），商品/主播记录里存的是
    `/api/uploads/...` 形式的 URL，不是"用户手填的本机绝对路径"。
    这是与旧版的根本差异：路径靠人打必然出错，而且一换机器全断。
  - **大文件只存路径/URL + 校验和**，绝不把图读进库。
  - **删除只删元数据**：磁盘素材一律保留（托管环境禁删，也防误删）。
  - **`code` 服务端自动生成**（P01/M01…），它是提示词里的产品锚点，
    不作为表单项暴露给用户。

注意：铁律 15（香调事实分级 A/B/C）的 `notes_level` / `notes_card_path` /
`notes_json` / `writable` 已按用户要求随本次重构**移除**，商品事实不再分级。
详见 `app/db/models.py::Product` 的说明与迁移 0004。

**素材检索模块已于 2026-09-29 按用户要求移除**：页面、侧边栏入口、以及
`POST /assets`（手动登记）、`POST /assets/scan`（扫描目录）、
`POST /assets/reindex`（重建索引）、`GET /fs/list`（目录浏览）四个接口全部删除。
保留 `GET /assets` 是因为 MCP 的 `list_assets` 工具在用它（agent 侧检索产物）。
**`Asset` 表与 FTS 索引不能删** —— `services/queue.py` 与 `services/post.py`
在每次出片完成后自动登记产物，项目详情页也读它回显成片列表。
索引改为**由流水线自维护**（登记 Asset 时同步 `fts_mod.index_asset`），
不再依赖已删除的人工重建入口。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import deps
from ..db import fts as fts_mod
from ..db.models import (
    Asset, Product, Project, RenderJob, Shot, Talent, TalentImage,
)
from ..db.session import engine, get_db
from ..pipeline.sheet import (
    REGISTERABLE_TALENT_IMAGE_KINDS,
    TALENT_IMAGE_KINDS, build_appearance_gen_messages, build_talent_image_prompt,
    build_talent_image_prompt_parts, normalize_appearance, polish_or_fallback,
)
from ..pipeline import talent_spec as spec_mod
from ..providers.base import LLMProvider
from ..services.refs import normalize_image_meta

router = APIRouter(prefix="/api", tags=["assets"])

# 性别 / 国籍的候选值定义在 `pipeline/talent_spec.py`，这里**转出去**而不是重抄 ——
# 拼装提示词时要用国籍的英文写法（`NATIONALITY_EN`），枚举与映射必须同源才不会漂。
GENDERS = spec_mod.GENDERS
GENDER_LABELS = spec_mod.GENDER_LABELS
NATIONALITIES = spec_mod.NATIONALITIES

# 品类枚举。**唯一事实来源**：前端下拉、badge 配色、校验都照这里来，
# 别在前端再抄一份（改一处漏一处是这类枚举的经典问题）。
CATEGORIES = ("beauty", "perfume", "food", "home", "fashion", "tech", "other")
MAX_PRODUCT_IMAGES = 5


def checksum_of(path: str) -> str:
    """算 sha256 前 16 位。大文件也要流式读，别一次性 read()。

    主播参考图登记（`POST /api/talents/{id}/images`）在用它 —— 素材检索
    模块删掉后它并未成为死代码，别顺手删。
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        return ""
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _next_code(db: Session, model, prefix: str) -> str:
    """生成 P01 / M01 这样的短编号。

    取现有最大号 +1 而不是 count()+1 —— 删掉中间一条后 count 会重号，
    撞上 `code` 的唯一索引直接 500。
    """
    top = 0
    for (c,) in db.query(model.code).all():
        if c and c.startswith(prefix):
            try:
                top = max(top, int(c[len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{top + 1:02d}"


# ------------------------------------------------------------------ schemas


class ProductIn(BaseModel):
    """商品入参。`code` 不在其中 —— 服务端生成，用户不需要关心。

    `image_meta` 与 images 下标对齐，标记每张图的用途（主体图/信息图/海报/其他）；
    只有主体图会进 i2v 参考图（R-16/R-17）。**通常不用传** —— 不传时由视觉大模型
    自动识别（R-18）；显式传了则尊重手工标注（覆盖通道）。
    """

    name: str
    category: str = "other"
    description: str = ""
    images: List[str] = Field(default_factory=list)
    image_meta: Optional[List[Dict[str, Any]]] = None
    price: str = ""
    target_audience: str = ""
    brand: str = ""
    tags: str = ""


class TalentIn(BaseModel):
    """主播入参。

    `age` / `gender` / `nationality` 是**选角的三个硬维度**，缺任何一个，
    文本大模型就只能从"女性"两个字里瞎猜族裔和年龄，之后每一版提示词都会漂。
    所以这三项与 `name` 一样必填（年龄还限了合理区间）。
    `appearance` 允许为空 —— 它可以先建人、后生成提示词。
    """

    name: str
    age: Optional[int] = None
    gender: str = ""
    nationality: str = ""
    description: str = ""
    appearance: str = ""
    appearance_source: str = ""     # manual / llm
    # 十六个维度的选中值。**层的划分由运行时按 talent_spec 解释**，这里不拆 ——
    # 以后词表调整层的归属，接口不用动。
    spec: Dict[str, str] = Field(default_factory=dict)
    preset: str = ""                # 最后套用的人设预设
    voice_style: str = ""


class TalentImageIn(BaseModel):
    kind: str = "front"            # front / threeview / sheet / variant / poster
    variant_name: str = ""
    path: str


class AppearancePreviewIn(BaseModel):
    """生成形象提示词入参（**不入库**，只回候选文案给用户确认）。

    `spec` 里同时放造型层与画面层；服务端拼装时会按 `reference_safe` 把画面层
    剔掉，所以这里可以无脑全传。
    """

    name: str = ""
    age: Optional[int] = None
    gender: str = ""
    nationality: str = ""
    appearance: str = ""            # 用户已手写的补充/上一版，作为参考
    hint: str = ""                  # 额外要求，例如"看起来更干练一些"
    lang: str = "en"                # en / zh
    provider_id: Optional[str] = None
    spec: Dict[str, str] = Field(default_factory=dict)   # 全部维度的选择
    preset: str = ""                # 若传了，先用它铺满再用 spec 覆盖
    vary: str = ""                  # face / full —— 在上面结果基础上"换一版"
    seed: Optional[int] = None      # vary 的随机种子（不传则真随机）


class TalentRenderIn(BaseModel):
    """主播参考图生成入参。**会花钱** —— 这是真实的文生图调用。"""

    kind: str = "front"             # front / threeview / sheet / poster
    provider_id: Optional[str] = None
    resolution: Optional[str] = None
    seed: Optional[int] = None
    dry_run: bool = False
    # 本次出图临时改的画面层（仅 poster 读取）。不传则用主播身上存的那份 ——
    # "这次换个光线试试"不该要求用户先把主播改掉。
    render_spec: Optional[Dict[str, str]] = None


class SheetIn(BaseModel):
    """（兼容旧调用）2x2 四视图定妆照生成入参。新代码请用 TalentRenderIn。"""

    provider_id: Optional[str] = None
    resolution: Optional[str] = None
    seed: Optional[int] = None
    dry_run: bool = False


# ------------------------------------------------------------------ helpers


def _validate_category(cat: str) -> str:
    c = (cat or "other").strip().lower()
    if c not in CATEGORIES:
        raise HTTPException(400, f"未知商品品类：{cat}（可选：{'/'.join(CATEGORIES)}）")
    return c


def _product_out(p: Product) -> dict:
    imgs = [u for u in (p.images or []) if u]
    raw_meta = getattr(p, "image_meta", None)
    return {
        "id": p.id, "code": p.code, "name": p.name,
        "category": (p.category or "other").lower(),
        "description": p.description or "",
        "images": imgs,
        # 与 images 下标对齐的用途标记（归一过，前端可直接渲染下拉）
        "image_meta": normalize_image_meta(raw_meta, len(imgs)),
        # R-20：识别在生成脚本时做 —— 有图但库里还没标注时，前端提示
        # 「生成脚本时自动识别」（判断用原始值，normalize 会把空填成全 hero）
        "image_meta_pending": bool(imgs) and not raw_meta,
        "cover": imgs[0] if imgs else None,
        "price": p.price or "",
        "target_audience": p.target_audience or "",
        "brand": p.brand or "", "tags": p.tags or "",
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


UPLOAD_URL_PREFIX = "/api/uploads/"


def _normalize_image_path(raw: str) -> Path:
    """把「上传 URL / 本机绝对路径」归一成存在的磁盘路径。

    上传 URL 走 `storage.resolve_file` 反解（它自带目录白名单与穿越校验），
    绝不在这里自己拼路径 —— 手拼就是下一处 `../../` 漏洞。
    """
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(400, "参考图路径为空")
    if raw.startswith(UPLOAD_URL_PREFIX):
        parts = raw[len(UPLOAD_URL_PREFIX):].split("/")
        if len(parts) != 3:
            raise HTTPException(400, f"上传 URL 格式不对：{raw}")
        from ..services import storage
        try:
            p = storage.resolve_file(parts[0], parts[1], parts[2])
        except ValueError as e:
            raise HTTPException(400, f"上传素材不可读：{e}") from e
        if not p.exists() or not p.is_file():
            raise HTTPException(400, f"上传素材不存在：{raw}")
        return p
    p = Path(raw)
    if not p.exists() or not p.is_file():
        raise HTTPException(400, f"参考图不存在（本地路径）：{raw}")
    return p


def _display_url(path: Optional[str]) -> Optional[str]:
    """把磁盘路径 / 上传 URL 统一成浏览器能直接喂给 `<img src>` 的地址。

    两种来源：
      - `/api/uploads/...` —— 上传域的持久 URL，原样返回；
      - 出片落盘的**本机绝对路径** —— 必须走 `/api/file?path=...`。

    第二条是旧版主播卡片缩略图裂图的根因：把 `C:\\…\\xxx.png` 直接塞进
    `img src`，浏览器按相对路径去请求 `http://host/C:/…`，永远 404。
    """
    if not path:
        return None
    if path.startswith(UPLOAD_URL_PREFIX):
        return path
    return f"/api/file?path={quote(path)}"


def _talent_out(t: Talent) -> dict:
    images = [
        {"id": i.id, "kind": i.kind, "variant_name": i.variant_name,
         "path": i.path, "url": _display_url(i.path), "checksum": i.checksum,
         "exists": Path(i.path).exists() if i.path else False}
        for i in t.images
    ]

    def pick(kind: str) -> Optional[dict]:
        return next((i for i in images if i["kind"] == kind), None)

    front, three, sheet = pick("front"), pick("threeview"), pick("sheet")
    poster = pick("poster")
    # 卡片缩略图优先级：正面图（一眼认人）> 三视图 > 定妆照 > 任意参考图
    # （poster 是带场景的成品图，不当参考图用，故不进 cover）
    cover = front or three or sheet or (images[0] if images else None)
    return {
        "id": t.id, "code": t.code, "name": t.name,
        "profile": {"age": t.age, "gender": t.gender or "",
                    "nationality": t.nationality or ""},
        "description": t.description or "",
        "appearance": t.appearance or "",
        "appearance_source": t.appearance_source or "",
        # 老行可能是 NULL（迁移兜的是 '{}'，手工改过库则仍是 NULL），归一成 dict，
        # 免得前端 `Object.keys(null)` 直接炸。空 dict 表示"还没选"，是合法状态。
        "spec": t.spec or {},
        "preset": t.preset or "",
        "voice_style": t.voice_style or "",
        "meta": t.meta or {},
        "images": images,
        "cover_path": cover["path"] if cover else None,
        "cover_url": cover["url"] if cover else None,
        "front_path": front["path"] if front else None,
        "front_url": front["url"] if front else None,
        "threeview_path": three["path"] if three else None,
        "threeview_url": three["url"] if three else None,
        "sheet_path": sheet["path"] if sheet else None,
        "sheet_url": sheet["url"] if sheet else None,
        "poster_path": poster["path"] if poster else None,
        "poster_url": poster["url"] if poster else None,
        "has_sheet": sheet is not None,
        "has_front": front is not None,
        "has_threeview": three is not None,
        "has_poster": poster is not None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


# ------------------------------------------------------------------ 商品库


@router.get("/products/categories")
def list_categories():
    """品类枚举（前端下拉 + badge 配色都读它）。"""
    return list(CATEGORIES)


@router.get("/products")
def list_products(category: str = "", db: Session = Depends(get_db)):
    q = db.query(Product)
    if category:
        q = q.filter(Product.category == _validate_category(category))
    rows = q.order_by(Product.id.desc()).all()
    return [_product_out(r) for r in rows]


def _resolve_image_meta(name: str, desc: str, images: List[str],
                        explicit: Optional[Any]) -> Tuple[Any, List[str]]:
    """决定 image_meta（R-20 起**不再自动调大模型**）。

    显式给了手工标注 → 照用；没给 → 返回空（待识别）。
    识别挪到了**生成视频脚本**的入口（`services.product_vision.
    ensure_product_visuals`）：上传即识别既拖慢保存、又在用户还没决定用时
    提前花钱。返回值保留 `(meta, warnings)` 形状以兼容既有调用。
    """
    if explicit is not None:
        return normalize_image_meta(explicit, len(images)), []
    return [], []


@router.post("/products")
def create_product(body: ProductIn, db: Session = Depends(get_db)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "商品名称必填")
    images = [u for u in (body.images or []) if u]
    if len(images) > MAX_PRODUCT_IMAGES:
        raise HTTPException(400, f"商品图最多 {MAX_PRODUCT_IMAGES} 张")
    meta, warns = _resolve_image_meta(name, body.description, images,
                                      body.image_meta)

    p = Product(
        code=_next_code(db, Product, "P"),
        name=name,
        category=_validate_category(body.category),
        description=(body.description or "").strip(),
        images=images,
        image_meta=meta,
        price=(body.price or "").strip(),
        target_audience=(body.target_audience or "").strip(),
        brand=(body.brand or "").strip(),
        tags=(body.tags or "").strip(),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    out = _product_out(p)
    if warns:
        out["image_meta_warnings"] = warns
    return out


@router.patch("/products/{pid}")
def update_product(pid: int, body: Dict[str, Any], db: Session = Depends(get_db)):
    p: Optional[Product] = db.get(Product, pid)
    if p is None:
        raise HTTPException(404, f"商品不存在：{pid}")

    # 白名单赋值：直接 setattr 任意字段会让调用方改掉 code/id（提示词锚点当场失效）
    if "name" in body:
        name = (body["name"] or "").strip()
        if not name:
            raise HTTPException(400, "商品名称必填")
        p.name = name
    if "category" in body:
        p.category = _validate_category(body["category"])
    if "description" in body:
        p.description = (body["description"] or "").strip()
    images_changed = False
    old_image_meta = p.image_meta if isinstance(p.image_meta, list) else []
    if "images" in body:
        images = [u for u in (body["images"] or []) if u]
        if len(images) > MAX_PRODUCT_IMAGES:
            raise HTTPException(400, f"商品图最多 {MAX_PRODUCT_IMAGES} 张")
        images_changed = set(p.images or []) != set(images)
        p.images = images
    if "image_meta" in body:
        # 手工覆盖通道：显式给的就尊重（跟随 images 长度归一，保证下标对齐）
        explicit = normalize_image_meta(body["image_meta"], len(p.images or []))
        if images_changed:
            # ★R-30 优先级修正：**图换了，旧图的 desc 一律失效**（旧描述说的
            # 是旧图内容，留着就是"金瓶配粉色瓶描述"的事实错位——P01 实测）。
            # 显式 meta 只保留手工 role 意图；desc 等下次生成时重识别补上。
            p.image_meta = explicit
        else:
            # ★R-30 合并修正：前端只传 role 数组（不带 desc）——显式 role
            # 覆盖，AI 的 desc / desc_missing 从旧 meta 按下标补回，**手工改
            # 用途不丢识别成果**（此前整组覆盖，改一次下拉描述就没了）。
            merged: list = []
            for i, e in enumerate(explicit):
                old = old_image_meta[i] if i < len(old_image_meta) \
                    and isinstance(old_image_meta[i], dict) else {}
                if not e.get("desc"):
                    d = (old.get("desc") or "").strip()
                    if d:
                        e["desc"] = d
                    elif old.get("desc_missing"):
                        e["desc_missing"] = True
                merged.append(e)
            p.image_meta = merged
    elif images_changed:
        # 图变了且没给手工标注 → **清空旧标注**（R-20：识别在生成脚本时做，
        # 带着旧图的缓存描述写脚本是事实错位）；只改文字不动图 → 保留旧标注不重识别
        p.image_meta = []
    for field in ("price", "target_audience", "brand", "tags"):
        if field in body:
            setattr(p, field, (body[field] or "").strip())

    db.commit()
    db.refresh(p)
    return _product_out(p)


@router.delete("/products/{pid}")
def delete_product(pid: int, db: Session = Depends(get_db)):
    """只删元数据。**磁盘上的商品图保留** —— 素材丢了没法找回来。"""
    p: Optional[Product] = db.get(Product, pid)
    if p is None:
        raise HTTPException(404, f"商品不存在：{pid}")
    db.delete(p)
    db.commit()
    return {"deleted": pid, "files_kept": True}


# ------------------------------------------------------------------ 主播库


@router.get("/talents")
def list_talents(db: Session = Depends(get_db)):
    # 按新建时间倒序。**不再有"默认主播"排序** —— R-32 用户核实：`is_default`
    # 在出片/生文/出图链路里从未被读取（全部走 `project.talent_id`），
    # 只影响列表排序与星标显示，属于纯装饰，已整体删除。
    rows = db.query(Talent).order_by(Talent.id.desc()).all()
    return [_talent_out(r) for r in rows]


def _validate_profile(name: str, age: Optional[int], gender: str,
                      nationality: str) -> None:
    """基本信息校验。**年龄/性别/国籍与名称一样必填** —— 这三个维度缺一个，
    扩写出来的形象提示词就只能靠猜，之后每一版都在漂。"""
    if not (name or "").strip():
        raise HTTPException(400, "主播名称必填")
    if age is None:
        raise HTTPException(400, "年龄必填：年龄段决定脸与体态的生成先验")
    if not (1 <= int(age) <= 120):
        raise HTTPException(400, f"年龄需在 1–120 之间：{age}")
    if not (gender or "").strip():
        raise HTTPException(400, "性别必填")
    if (gender or "").strip().lower() not in GENDERS:
        raise HTTPException(400, f"性别只能是 {'/'.join(GENDERS)}：{gender}")
    if not (nationality or "").strip():
        raise HTTPException(400, "国籍必填：族裔气质必须跨镜一致")


@router.get("/talents/options")
def talent_options():
    """主播相关的一切下拉候选。**全部从 `pipeline/talent_spec.py` 转出**。

    前端不再抄任何一份词表 —— 十六个维度的选项、六个人设预设、图的类型，
    都是同一处的真相。改一次两边同时生效，不会出现"界面上有的选项后端不认"。
    """
    return {
        "genders": [{"value": g, "label": GENDER_LABELS.get(g, g)}
                    for g in GENDERS],
        "nationalities": list(NATIONALITIES),
        "image_kinds": [{"value": k, "label": v[1], "allows_render": v[2],
                         # 第一列是 None 表示"没有提示词构造函数" → 只能导入不能生成。
                         # 前端据此决定要不要给"生成"按钮，不用自己维护一份差集。
                         "generatable": v[0] is not None}
                        for k, v in REGISTERABLE_TALENT_IMAGE_KINDS.items()],
        "layers": spec_mod.layers_payload(),
        "dims": spec_mod.dims_payload(),
        "presets": spec_mod.presets_payload(),
    }


@router.post("/talents")
def create_talent(body: TalentIn, db: Session = Depends(get_db)):
    _validate_profile(body.name, body.age, body.gender, body.nationality)
    appearance = (body.appearance or "").strip()
    t = Talent(
        code=_next_code(db, Talent, "M"),
        name=(body.name or "").strip(),
        age=int(body.age or 0),
        gender=(body.gender or "").strip().lower(),
        nationality=(body.nationality or "").strip(),
        description=(body.description or "").strip(),
        appearance=appearance,
        # 没传就按"有没有提示词"推断：写了就是手写的，没写等着生成
        appearance_source=(body.appearance_source or "").strip()
        or ("manual" if appearance else ""),
        # 归一化后再落库：非法值（旧选项已被删除、前端传错）在这里静默丢掉，
        # 而不是带着脏数据进库，等到拼提示词时才炸。
        spec=spec_mod.normalize_spec(body.spec),
        preset=(body.preset or "").strip(),
        voice_style=(body.voice_style or "").strip(),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return _talent_out(t)


@router.patch("/talents/{tid}")
def update_talent(tid: int, body: Dict[str, Any], db: Session = Depends(get_db)):
    t: Optional[Talent] = db.get(Talent, tid)
    if t is None:
        raise HTTPException(404, f"主播不存在：{tid}")

    if "name" in body:
        name = (body["name"] or "").strip()
        if not name:
            raise HTTPException(400, "主播名称必填")
        t.name = name
    if "age" in body:
        age = body["age"]
        if age is not None and not (1 <= int(age) <= 120):
            raise HTTPException(400, f"年龄需在 1–120 之间：{age}")
        t.age = None if age is None else int(age)
    for field in ("gender", "nationality"):
        if field in body:
            setattr(t, field, (body[field] or "").strip())
    if "gender" in body and (t.gender or "").lower() not in GENDERS:
        raise HTTPException(400, f"性别只能是 {'/'.join(GENDERS)}：{t.gender}")
    for field in ("description", "appearance", "voice_style", "appearance_source"):
        if field in body:
            setattr(t, field, (body[field] or "").strip())
    # 整体替换（不做深度合并）：前端每次都会把完整的一份回传，深度合并反而
    # 会让"取消某个选项"这件事永远做不到。
    if "spec" in body:
        t.spec = spec_mod.normalize_spec(body["spec"])
    if "preset" in body:
        t.preset = (body["preset"] or "").strip()
        # 套预设时顺带把缺失的维度补齐，前端回显才有东西可显示
        if t.preset:
            t.spec = spec_mod.normalize_spec(
                spec_mod.apply_preset(t.preset, body.get("spec") or t.spec))
    # 改了提示词却没声明来源 → 一律算手改（用户在编辑框里动过就是手改）
    if "appearance" in body and not (body.get("appearance_source") or "").strip():
        t.appearance_source = "manual"

    db.commit()
    db.refresh(t)
    return _talent_out(t)


@router.delete("/talents/{tid}")
def delete_talent(tid: int, db: Session = Depends(get_db)):
    t: Optional[Talent] = db.get(Talent, tid)
    if t is None:
        raise HTTPException(404, f"主播不存在：{tid}")
    db.delete(t)  # cascade 删掉 talent_image 行，磁盘文件保留
    db.commit()
    return {"deleted": tid, "files_kept": True}


@router.post("/talents/{tid}/images")
def add_talent_image(tid: int, body: TalentImageIn,
                     db: Session = Depends(get_db)):
    """登记主播参考图。

    `kind=sheet` 用于定妆照：本接口不生成图，只登记**已经存在**的文件路径 ——
    生成由 `POST /api/talents/{tid}/render` 派任务，前端轮询成功后拿
    `output_path` 回来调这里。这样"生成"和"登记"各管一段，图不会重复产。

    `path` 两种写法都收：
      - 上传域 URL `/api/uploads/characters/<owner>/<name>`（"我已有模特图，直接导入"）；
      - 本机绝对路径（生成产物 / 手工登记）。
    两者都**归一成磁盘绝对路径存库** —— 库里只存一种形态，下游出片拿它当
    参考图、前端靠 `_display_url` 转预览地址，不会两套路径对不上。
    """
    t: Optional[Talent] = db.get(Talent, tid)
    if t is None:
        raise HTTPException(404, f"主播不存在：{tid}")
    if body.kind not in REGISTERABLE_TALENT_IMAGE_KINDS:
        # 白名单只认 `REGISTERABLE_TALENT_IMAGE_KINDS` 这一份。这里有两次前车之鉴：
        #   ① 曾经手写死一个元组，漏了 `poster` → 氛围图**生成得出来、却永远登记不了**，
        #      前端出图成功 -> 登记被 400 拒 -> has_poster 永远是 false，产物成孤儿文件；
        #   ② 修 ① 时改成直接用 `TALENT_IMAGE_KINDS`，又把只导入不生成的 `variant`
        #      （下拉里的"其他参考图"、`refs.py` 的 `REF_TALENT_KINDS` 成员）挡在门外
        #      -> 用户一选它就 400。**"能生成"和"能登记"不是同一张表。**
        raise HTTPException(
            400,
            f"kind 只能是 {' / '.join(REGISTERABLE_TALENT_IMAGE_KINDS)}：{body.kind}")
    p = _normalize_image_path(body.path)
    row = TalentImage(talent_id=tid, kind=body.kind,
                      variant_name=body.variant_name, path=str(p),
                      checksum=checksum_of(str(p)))
    db.add(row)
    db.commit()
    db.refresh(row)
    return _talent_out(t)


@router.delete("/talent-images/{iid}")
def delete_talent_image(iid: int, db: Session = Depends(get_db)):
    row: Optional[TalentImage] = db.get(TalentImage, iid)
    if row is None:
        raise HTTPException(404, f"参考图记录不存在：{iid}")
    tid = row.talent_id
    db.delete(row)
    db.commit()
    t: Optional[Talent] = db.get(Talent, tid)
    return _talent_out(t) if t else {"deleted": iid}


def _enqueue_talent_image(tid: int, kind: str, provider_id: Optional[str],
                          resolution: Optional[str], seed: Optional[int],
                          dry_run: bool, db: Session, queue,
                          render_override: Optional[Dict[str, str]] = None) -> dict:
    """派一个主播生图任务（**会花钱**）。

    走与出片完全相同的通道（RenderJob → 渲染队列），所以自动获得：
    预算熔断、任务台账、进度轮询、失败留痕。写一个"直接同步生图"的旁路
    会绕过上面全部 —— 尤其是预算闸门，绝不能绕。
    """
    t: Optional[Talent] = db.get(Talent, tid)
    if t is None:
        raise HTTPException(404, f"主播不存在：{tid}")
    if kind not in TALENT_IMAGE_KINDS:
        raise HTTPException(
            400, f"图类型只能是 {'/'.join(TALENT_IMAGE_KINDS)}：{kind}")
    if not (t.appearance or "").strip():
        raise HTTPException(
            400, "还没有形象提示词——先生成或手写一段，否则出来的不是同一个人")

    cfg = deps.get_providers_config()
    provider_id = provider_id or cfg.image.active
    img_nodes, _ = deps.get_media_nodes()
    if provider_id not in img_nodes:
        raise HTTPException(400, f"图片供应商不存在：{provider_id}")
    provider = img_nodes[provider_id]
    # 提交前先过一遍供应商自己的静态预检：工作流文件缺失 / base_url 还是占位值 /
    # 没令牌 —— 这些都是本地就能确定的。早点拒掉，别排一个注定失败的队：
    # 否则用户看到的是"过了一会儿，任务失败：FileNotFoundError: 工作流文件不存在"，
    # 既看不出是哪个供应商，也不知道能怎么办。
    problem = provider.preflight()
    if problem:
        raise HTTPException(
            400,
            f"图片供应商 `{provider_id}` 目前不可用：{problem}。"
            f"可在「设置 → 供应商」里换一个能用的，或按上面的提示修好它。")
    # 取**本次实际用的那个**供应商的默认分辨率，不是 `active` 那个 ——
    # 调用方显式传了 provider_id 时，两者并不相同。
    # 兜底要连 `default_size` 一起看：`openai_image` 那一类走的是 `size`，
    # 配 `default_size: 1024x1536` 时 `default_resolution` 是空的 ——
    # 只看它会让回传的 `resolution` 变成 null，前端显示成"没设置"，
    # 而实际上这一单是按 1024×1536 出的。宁可回传真实生效值。
    res_cfg = cfg.image_by_id(provider_id)
    resolution = resolution or (
        (res_cfg.default_resolution or res_cfg.default_size) if res_cfg else None)

    prompt = build_talent_image_prompt(kind, t.appearance, t.name)

    # 画面层只有当次出图是 poster（氛围图）时才读。参考图上的浅灰棚拍是硬约束，
    # 用户选的场景/布光不会被丢掉（仍存在主播身上），只是这一次不写进提示词。
    prompt, negative = build_talent_image_prompt_parts(
        kind, t.appearance, t.name,
        spec_mod.merge_spec(t.spec, render_override))

    from ..services import budget as budget_mod

    est_amount, est_desc = 0.0, "该供应商未提供费用预估"
    est = getattr(provider, "estimate_cost", None)
    if callable(est):
        try:
            amt, desc = est(None, resolution)
            est_amount, est_desc = float(amt or 0.0), str(desc)
        except Exception as e:  # noqa: BLE001 - 预估失败不该挡住出图
            est_desc = f"费用预估失败：{type(e).__name__}"

    try:
        budget_mod.enforce(db, cfg, project_id=None, provider_id=provider_id,
                           kind="image", raw_estimate_cny=est_amount)
    except budget_mod.BudgetError as be:
        budget_mod.record_block(db, project_id=None, kind="image",
                                provider_id=provider_id, decision=be.decision,
                                note=f"talent {kind} #{tid}")
        db.commit()
        raise HTTPException(402, f"花费上限拦截：{be.decision.reason}") from be

    params: Dict[str, Any] = {
        "prompt": prompt,
        # 走 provider 的专用 negative 通道，避免"不要出现水印"被当成画面内容去生成。
        # 供应商不声明 supports_negative_prompt 时会被静默忽略 —— 那时正文里的
        # "硬性要求"仍然兜得住，所以这不是单点依赖。
        "negative_prompt": negative,
        "ref_images": [],
        "resolution": resolution,
        # 队列靠 purpose 区分"这次生图是为了什么"，也是排障时唯一的线索
        "purpose": f"talent_{kind}",
        "talent_id": tid,
        "talent_image_kind": kind,
    }
    if seed is not None:
        params["seed"] = seed

    job = RenderJob(
        shot_id=None, project_id=None, kind="image", provider_id=provider_id,
        status="queued", params=params, dry_run=1 if dry_run else 0,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    queue.enqueue(job.id)

    out = {"job_id": job.id, "provider_id": provider_id, "kind": kind,
           "resolution": resolution, "prompt": prompt,
           "estimate": {"amount_cny": est_amount, "desc": est_desc}}
    if dry_run:
        out["message"] = f"[dry-run] {est_desc}"
    return out


@router.post("/talents/{tid}/render")
def render_talent_image(tid: int, body: TalentRenderIn = TalentRenderIn(),
                        db: Session = Depends(get_db),
                        queue=Depends(deps.get_render_queue)):
    """生成主播参考图：正面图 / 人物三视图 / 2x2 四视图定妆照 / 氛围图（**会花钱**）。

    返回 `job_id` 交给前端轮询 `GET /api/jobs/{id}`；成功后拿 `output_path`
    调 `POST /api/talents/{tid}/images {kind:"front"|"threeview"|"sheet"|"poster"}`
    登记。

    注意 **poster 与其他三种不是一回事**：前三种是给下游当 i2v 参考图用的，必须
    中性棚拍；poster 是能落地的成品图，只有它会采用画面层（场景/光线/构图/色调）。
    """
    return _enqueue_talent_image(tid, (body.kind or "front").strip().lower(),
                                 body.provider_id, body.resolution, body.seed,
                                 body.dry_run, db, queue, body.render_spec)


@router.post("/talents/{tid}/sheet")
def generate_talent_sheet(tid: int, body: SheetIn = SheetIn(),
                          db: Session = Depends(get_db),
                          queue=Depends(deps.get_render_queue)):
    """（兼容旧调用）2x2 四视图定妆照。等价于 `render {kind:"sheet"}`。"""
    return _enqueue_talent_image(tid, "sheet", body.provider_id,
                                 body.resolution, body.seed, body.dry_run,
                                 db, queue)


@router.post("/talents/appearance-preview")
def preview_talent_appearance(body: AppearancePreviewIn,
                              llm: LLMProvider = Depends(deps.get_active_llm)):
    """生成形象提示词：**先按所选维度拼装，再让文本大模型润色**。

    **不入库、不生图** —— 只回一段候选文案给用户看。原因很实在：生图要花钱，
    拼装与润色几乎不要钱，让人在便宜的那一步反复试到满意，再按花钱的按钮。

    顺序是刻意的：

        ① `spec_mod` 把用户勾选的十六个维度拼成一段话（确定性，选了就一定出现）
        ② 文本大模型只做行文润色，**不许增删特征**
        ③ `polish_or_fallback` 按覆盖率复查：润色稿丢了特征就退回 ① 的原文

    所以这里**没有任何一步依赖模型的创造力** —— 模型的不可控性被限制在"怎么说"
    而不是"说什么"。这也是 `temperature` 保持低值的原因：要的是稳定的编辑，
    不是灵感。（旧实现用 0.9，那是给"自由扩写"用的，在这里只会增加删改的概率。）

    `vary` 用来"再生成一版"：它换掉的是**真实的组合成分**（见 `vary_spec` 的
    设计说明），而不是让模型把同一张脸换个讲法。
    """
    _validate_profile(body.name or "主播", body.age, body.gender, body.nationality)

    lang = (body.lang or "en").lower()
    profile = {"name": body.name, "age": body.age,
               "gender": body.gender, "nationality": body.nationality}

    # ① 拼装 —— 预设在前，用户逐项的选择在后覆盖
    spec = spec_mod.apply_preset(body.preset, body.spec) if body.preset \
        else spec_mod.normalize_spec(body.spec)
    vary = (body.vary or "").strip().lower()
    if vary in spec_mod.VARY_LAYERS:
        spec = spec_mod.vary_spec(spec, vary, seed=body.seed)

    drafted = spec_mod.build_spec_sentence(profile, spec, lang)
    if not drafted:
        raise HTTPException(400, "没有可用于拼装的信息：至少要有年龄/性别/国籍")

    system, user = build_appearance_gen_messages(
        profile, lang=lang, hint=body.hint,
        previous=body.appearance, drafted=drafted)

    if body.provider_id:
        nodes = deps.get_llm_nodes()
        if body.provider_id not in nodes:
            raise HTTPException(400, f"文本供应商不存在：{body.provider_id}")
        llm = nodes[body.provider_id]

    note = ""
    try:
        res = llm.complete(system, user, temperature=0.3)
    except Exception as e:  # noqa: BLE001 - 供应商报错要原样告诉用户
        # 润色失败不该让用户卡住 —— 拼装结果本身就已经能用了
        return {"appearance": drafted, "lang": lang, "model": "", "prompt": user,
                "spec": spec, "polished": False,
                "warning": f"文本大模型不可用（{type(e).__name__}: {e}），"
                           f"已返回未经润色的拼装结果"}
    # ③ 护栏
    text, note = polish_or_fallback(drafted, res.text, lang)
    if not text:
        raise HTTPException(502, "文本大模型没有返回可用的形象描述，请重试")
    return {
        "appearance": text, "lang": lang,
        "model": getattr(res, "model", "") or "", "prompt": user,
        # spec 必须回传：前端要靠它把十六个下拉框回填到所选状态，
        # 尤其是 vary 过的版本 —— 用户看到的分析必须与实际存下来的一致
        "spec": spec, "drafted": drafted, "polished": text != drafted, "note": note,
    }


# ------------------------------------------------------------------ 素材


def _asset_out(r: Any, *, rank: Optional[float] = None,
               snippet: str = "", engine_name: str = "like") -> dict:
    p = Path(r.path) if r.path else None
    meta = r.meta or {}
    if isinstance(meta, str):  # FTS 走原始 SQL，JSON 列不会被反序列化
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    # FTS 走原始 SQL，created_at 是 SQLite 返回的 ISO 字符串；ORM 路径则是 datetime
    ca = r.created_at
    if isinstance(ca, str):
        created_at = ca
    elif ca is not None:
        created_at = ca.isoformat()
    else:
        created_at = None
    return {
        "id": r.id, "kind": r.kind, "path": r.path,
        "project_id": r.project_id, "meta": meta,
        "exists": bool(p and p.exists()),
        "size": p.stat().st_size if p and p.exists() else 0,
        "created_at": created_at,
        "rank": rank, "snippet": snippet, "engine": engine_name,
    }


@router.get("/assets")
def list_assets(q: str = "", kind: str = "", project_id: Optional[int] = None,
                limit: int = Query(200, ge=1, le=2000),
                db: Session = Depends(get_db)):
    """资产全文检索（前端入口已移除，现由 MCP 的 `list_assets` 工具使用）。

    **有关键词先走 FTS5**（bm25 相关度排序 + 高亮片段）；FTS5 不可用、表达式
    不合法、或**零命中**时静默回退 LIKE —— 检索能力降级可以接受，接口 500 不行。
    索引由流水线在登记资产时自维护（`services/queue.py`、`services/post.py`），
    不再依赖已删除的人工重建入口。
    """
    if q:
        hits = fts_mod.search(engine, q, limit=limit, kind=kind,
                              project_id=project_id)
        # FTS 可用但**零命中**时也回退 LIKE。索引由流水线尽力维护（登记 Asset
        # 时同步写入），历史数据或外部改过的库都可能漏条 —— 漏条不该表现为
        # "搜不到"。检索降级可以接受，查不出来不行。
        if hits:
            return [_asset_out(SimpleNamespace(**h), rank=h.get("rank"),
                               snippet=h.get("snippet") or "", engine_name="fts5")
                    for h in hits]

    query = db.query(Asset)
    if kind:
        query = query.filter(Asset.kind == kind)
    if project_id is not None:
        query = query.filter(Asset.project_id == project_id)
    if q:
        like = f"%{q}%"
        query = query.filter(Asset.path.like(like))
    rows = query.order_by(Asset.id.desc()).limit(limit).all()
    return [_asset_out(r) for r in rows]


# ------------------------------------------------------------------ 概览


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    """工作台首页用的一屏概览。"""
    def count(model) -> int:
        return db.query(model).count()

    products = db.query(Product).all()
    return {
        "projects": count(Project),
        "shots": count(Shot),
        "products": len(products),
        # 建好卡片但一张图都没有的商品 = 出片时没参考图可用，值得单独提示
        "products_with_images": sum(1 for p in products if p.images),
        "talents": count(Talent),
        "assets": count(Asset),
    }


@router.get("/env")
def env_status():
    """哪些环境变量已设置（**只回 True/False，绝不回值**）。"""
    names = ["OPENAI_API_KEY", "AUTODL_TOKEN", "AVS_IMAGE_HOST_ROOT",
             "AVS_IMAGE_HOST_URL", "AVS_IMAGE_HOST_UPLOAD", "DATABASE_URL"]
    return {n: bool(os.environ.get(n)) for n in names}
