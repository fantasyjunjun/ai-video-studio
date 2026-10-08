"""项目参考图解析：把「商品图 + 主播参考图」组装成 i2v 出片用的 `ref_images`。

**为什么需要这个模块（本次修复的核心）**
    出片接口 `POST /api/shots/{id}/render` 原本只认调用方显式传入的
    `ref_image_paths`，而前端分镜页从未传过 —— 于是"图生视频"实际退化成
    纯文生视频，产品与模特根本没进画面。本模块是**唯一**的解析入口：
    单镜出片按镜解析，`GET /api/projects/{id}/refs` 用同一份数据给 UI 预览，
    避免两处各拼一遍而漂移。

**参考图来源与取舍**
    - 商品图：项目关联的每个 Product 的 `images`（上传 URL），按关联顺序。
    - 主播图：只取**能当参考图**的 kind（front / threeview / sheet / variant）；
      `poster`（氛围图）是带场景的成品图，喂进去会把背景复刻进每一个镜头 → 排除。
    - **按镜判人物**：纯产品镜（L1 只写瓶身）不带模特图，否则模型可能把人物
      塞进本该空镜的产品画面。判定用"人物锚点是否逐字出现在提示词里" + 人称词兜底。
    - 去重、限量（MAX_REFS）：参考图过多会互相稀释，平台侧也只认 ref_image_0..N。
      **但限量绝不静默** —— 每一张被丢掉的图都要报出「哪来的、为什么没用上」，
      否则用户传了 4 张、画面里只见到 1 张，只会以为是产品坏了（踩过）。
      上限值本身也不是拍的：`MAX_PRODUCT_REFS` 对齐"商品库允许传 5 张"，
      平台侧槽位上限由 `PLATFORM_MAX_REFS` 记录（★R-11 实测 `ref_image_0..7` 合法）。

**`ref_image_0` 是「首帧」，不是普通参考图（重要）**
    该工作流的语义是：`ref_image_0` = 视频起始帧，`ref_image_1..N` = 附加参考。
    所以**排在第 0 位的那张图会主导整个镜头的画面**，后面的只是弱约束。
    本模块因此必须把「实际发送顺序」算准并交给 UI 展示（`sequence_*`），
    而不是让用户对着一排缩略图猜哪张是首帧。改排序 = 改成片观感，别随手调。

本模块**只读不写、不花钱**。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..db.models import Product, Project, ProjectProduct, Shot, Talent
from . import storage

# 主播图里可作为 i2v 参考图的 kind，按优先级排列。
# ★R-16 修正：front 提到最前 —— `ref_image_0` 是**首帧**，正面单人是最好的首帧；
# 三视图（一人三份同框）当首帧会把人物画重，只配做后排附加参考。
REF_TALENT_KINDS = ("front", "sheet", "threeview", "variant")
# 上限。"商品库允许传 5 张"，所以单商品给 5 —— 让用户传多少就用多少，
# 别再出现"传了 5 张只发 3 张"的静默丢失（R-11）。
MAX_PRODUCT_REFS = 5    # 单个商品最多取几张（= 商品库的图片上限）
MAX_TALENT_REFS = 2     # 主播图最多取几张
MAX_REFS = 7            # 单镜合计上限 = MAX_TALENT_REFS + MAX_PRODUCT_REFS

# 平台侧槽位上限（★R-11 实测）：用"不可达 URL"探测（参数校验先于抓图，
# 不产生计费任务）—— `ref_image_0..7` 全部被接受，即最多 8 张。
# MAX_REFS 特意留在 7，给未来留 1 个槽位的余量。
PLATFORM_MAX_REFS = 8

UPLOAD_URL_PREFIX = "/api/uploads/"

# ---------------------------------------------------------------- 商品图角色（R-16，R-17 泛化）
# 商品图不再"张张都进参考图"。实测穿帮：一个商品混着「两张不同型号的瓶身照 +
# 两张香调信息图」，全量发给模型 → 一镜里画出两只瓶子。给每张图打用途标记，
# 只有主体图（hero）进 i2v 参考图；其余在 dropped 里**如实报出原因**。
# R-17 泛化：bottle → hero（不再香水本位，口红/面霜/食品的实物图同样是 hero）。
# 同一实物的多张角度照可以**都是 hero、全部进参考图**（多角度帮模型锁外形）；
# 不同型号/花色必须拆成独立商品 —— 这是录入规范，代码不猜。
# 兼容：老数据里的 "bottle" 读入时自动当 hero；缺省也按 hero —— 与从前行为一致。
PRODUCT_IMAGE_ROLES = ("hero", "infographic", "poster", "other")
PRODUCT_ROLE_LABELS = {
    "hero": "主体图（产品实物）",
    "infographic": "信息图/成分表",
    "poster": "海报/场景图",
    "other": "其他（不进参考图）",
}
_REF_PRODUCT_ROLES = ("hero",)
_ROLE_ALIASES = {"bottle": "hero"}          # R-16 旧值 → R-17 新值


def _norm_role(role: Any) -> str:
    """角色值收敛：别名映射 + 非法值回落 hero。单一实现，两处读法共用。"""
    r = (str(role) if role is not None else "").strip().lower()
    r = _ROLE_ALIASES.get(r, r)
    return r if r in PRODUCT_IMAGE_ROLES else "hero"


def normalize_image_meta(raw: Any, n_images: int) -> List[Dict[str, str]]:
    """把 image_meta 收敛成长度 == n_images 的 `[{...}]`，非法值回落 hero。

    assets API 与迁移兜底共用这一个实现（单一真相），refs 侧读库前也走它。
    老数据里的 "bottle" 自动映射成 hero（R-17 泛化，语义等价）。
    ★R-30 修正：显式标注只该覆盖 **role**，AI 识别出的 desc / desc_missing
    必须原样保留 —— 此前只留 role，手工改一次用途就把画面描述抹掉，
    下次生成的上下文里该图变回"暂无解读"（还触发重识别白花钱）。
    """
    raw_list = raw if isinstance(raw, list) else []
    roles: List[Dict[str, str]] = []
    for i in range(n_images):
        entry = raw_list[i] if i < len(raw_list) else None
        if isinstance(entry, dict):
            role = entry.get("role")
        elif isinstance(entry, str):
            role = entry
        else:
            role = None
        e: Dict[str, str] = {"role": _norm_role(role)}
        if isinstance(entry, dict):
            desc = (entry.get("desc") or "").strip()
            if desc:
                e["desc"] = desc
            elif entry.get("desc_missing"):
                e["desc_missing"] = True
        roles.append(e)
    return roles


def _image_role(image_meta: Any, idx: int) -> str:
    """读第 idx 张商品图的角色；越界 / 未标 / 非法一律按 hero（旧 bottle 同义）。"""
    try:
        entry = (image_meta or [])[idx]
    except (IndexError, TypeError):
        return "hero"
    if isinstance(entry, dict):
        role = entry.get("role")
    elif isinstance(entry, str):
        role = entry
    else:
        role = None
    return _norm_role(role)

# 这句要原样透传到前端 —— 用户对"为什么成片只像第一张图"的困惑，
# 根子就在 `ref_image_0` 是首帧而没人告诉他。
FIRST_FRAME_NOTE = (
    "ref_image_0 是图生视频的**首帧**：整镜画面从它开始，会主导成片观感；"
    "ref_image_1…N 只是附加参考，约束力弱得多。"
    "想换首帧，就把想要的那张图放到「商品库」图片里的第 1 张。"
)

# 兜底的人称/人物名词（锚点没命中时用）。词边界避免 "he" 命中 "the"。
_PERSON_HINTS = re.compile(
    r"\b(woman|women|man|men|girl|boy|female|male|person|model|she|her|hers|he|him|his|face)\b",
    re.IGNORECASE,
)

# 人物/身体部位词（比 _PERSON_HINTS 宽）：用于「人物镜里谁当首帧」的细分判断。
# 实测教训（R-27 对照实验）：A3 的 [SHOT] "Index finger presses... toward wrist"
# 没有woman/she 等词但有 finger/wrist —— 主体是手（人物动作），首帧仍应给主播图。
_PERSON_BODY_RE = re.compile(
    r"\b(woman|women|man|men|girl|boy|female|male|person|model|she|her|hers|he|him|his"
    r"|face|hand|hands|wrist|finger|fingers|arm|arms|palm|skin|neck|shoulder|shoulders"
    r"|body|leg|legs|eye|eyes|hair)\b",
    re.IGNORECASE,
)

# 产品词：判断画面主体是否是产品（收尾产品镜/产品开箱镜等）。
_PRODUCT_HINTS = re.compile(
    r"\b(bottle|perfume|fragrance|flacon|product|jar|tube|compact|canister|box|carton"
    r"|lipstick|palette|device|gadget|label|pack)\b",
    re.IGNORECASE,
)


def _to_disk_path(raw: str) -> Optional[str]:
    """把「上传 URL / 本机绝对路径」归一成存在的磁盘文件路径；不存在返回 None。

    绝不自己拼路径 —— 上传 URL 一律走 `storage.resolve_file`（自带越界校验）。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith(UPLOAD_URL_PREFIX):
        parts = raw[len(UPLOAD_URL_PREFIX):].split("/")
        if len(parts) != 3:
            return None
        try:
            p = storage.resolve_file(parts[0], parts[1], parts[2])
        except ValueError:
            return None
        return str(p) if p.exists() and p.is_file() else None
    p = Path(raw)
    return str(p) if p.exists() and p.is_file() else None


def _product_refs(db: Session, project_id: int,
                  codes: Optional[List[str]] = None) -> tuple:
    """按项目关联顺序取商品图。返回 `(items, dropped)`。

    `dropped` 不是"错误"，是**必须让用户看见的事实**：文件找不到、超出单商品上限。

    ★R-33 `codes`：**逐镜商品分配** —— 只取本镜用到的商品的图。多商品项目里
    把全部商品的图挂给每一镜，实测就是"两瓶同框"的根源，而且产品主导镜的首帧
    参考图会取到另一款（A1 金瓶镜挂黑瓶图，只是模型靠文本侥幸纠偏）。
    `codes` 为空 = 脚本没写逐镜分配 → 回退旧行为（挂全部商品），不阻断出片。

    ★去重：同一商品被重复关联时**只出一份图**。`project_product` 没有唯一约束
    （`role` 允许同商品挂多行），而且历史上有过"删项目没删关系行 → SQLite 复用
    id → 新项目继承旧关系"的污染（R-33 实测命中，一个项目攒到 18 行关系）。
    重复行会让同一张图被挂 N 次、白烧 `MAX_PRODUCT_REFS` 名额，所以按 product_id
    去重。注意本函数本来就不读 `lk.role`，同商品多角色也只该出一份。
    """
    items: List[dict] = []
    dropped: List[dict] = []
    want = {str(c).strip().upper() for c in (codes or []) if str(c or "").strip()}
    links = (db.query(ProjectProduct)
             .filter(ProjectProduct.project_id == project_id)
             .order_by(ProjectProduct.id.asc()).all())
    seen_pids: set = set()
    for lk in links:
        if lk.product_id in seen_pids:      # 重复关联（历史污染 / 多角色行）跳过
            continue
        seen_pids.add(lk.product_id)
        prod = db.get(Product, lk.product_id)
        if prod is None:
            continue
        label = f"{prod.code} {prod.name or ''}".strip()
        # 本镜没用到的商品：图一张都不挂，但**要说清是"这一镜用不到"**，
        # 而不是静默消失 —— 否则用户会以为是图丢了或功能坏了。
        if want and str(prod.code or "").upper() not in want:
            dropped.append({
                "path": f"（{label} 的全部图片）", "kind": "product", "label": label,
                "reason": "本镜的画面里没有这件商品（脚本的逐镜商品分配），"
                          "所以它的图不参与本镜出片",
            })
            continue
        for idx, u in enumerate(prod.images or []):
            # ★R-16：先看用途标记 —— 信息图/海报/其他根本不该进参考图，
            # 连磁盘解析都不用做（省一次 IO，也免得"文件丢了"抢走它被丢的真原因）
            role = _image_role(getattr(prod, "image_meta", None), idx)
            if role not in _REF_PRODUCT_ROLES:
                dropped.append({
                    "path": str(u), "kind": "product", "label": label,
                    "reason": f"商品图标记为「{PRODUCT_ROLE_LABELS.get(role, role)}」，"
                              f"不进参考图（如需启用请在商品库改成瓶身照）",
                })
                continue
            path = _to_disk_path(u)
            if not path:
                dropped.append({"path": str(u), "kind": "product", "label": label,
                                "reason": "文件不存在或路径无法解析，发不给模型"})
                continue
            if idx >= MAX_PRODUCT_REFS:
                dropped.append({
                    "path": path, "kind": "product", "label": label,
                    "reason": f"超出单商品参考图上限 {MAX_PRODUCT_REFS} 张"
                              f"（这张是第 {idx + 1} 张）"})
                continue
            items.append({"path": path, "kind": "product", "label": label,
                          "role": role})
    return items, dropped


def _talent_refs(db: Session, project_id: int, look: str = "") -> tuple:
    """只取**能当参考图**的 kind（不含 poster）。返回 `(items, dropped)`。

    ★R-33 `look`：本镜要求的造型（`shot.talent_look`）。主播库里的 `variant`
    图可以带 `variant_name`（如 blackgown）—— 若这一镜要求的造型与某张 variant
    同名，就把它提到最前（`ref_image_0` = 这一套造型的正面照），让"章节换装"
    真的换掉参考图，而不只是换一句提示词。找不到匹配就维持原优先级。
    """
    proj = db.get(Project, project_id)
    if proj is None or not proj.talent_id:
        return [], []
    t = db.get(Talent, proj.talent_id)
    if t is None:
        return [], []
    label = f"{t.code} {t.name or ''}".strip()
    dropped: List[dict] = []
    entries: List[dict] = []
    for im in t.images:
        if im.kind not in REF_TALENT_KINDS or not im.path:
            continue
        path = _to_disk_path(im.path)
        if path:
            entries.append({"path": path, "kind": f"talent:{im.kind}", "label": label,
                            "variant": (im.variant_name or "").strip()})
        else:
            dropped.append({"path": str(im.path), "kind": f"talent:{im.kind}",
                            "label": label,
                            "reason": "文件不存在或路径无法解析，发不给模型"})
    prio = {k: i for i, k in enumerate(REF_TALENT_KINDS)}
    entries.sort(key=lambda e: (prio.get(e["kind"].split(":", 1)[1], 99), e["path"]))
    if look:
        lt = look.lower()

        def _hit(e: dict) -> bool:
            v = (e.get("variant") or "").lower()
            if not v:
                return False
            if v in lt or lt in v:
                return True
            return any(tok in lt for tok in re.split(r"[^a-z0-9]+", v) if len(tok) > 2)

        entries.sort(key=lambda e: (0 if _hit(e) else 1))
    items = entries[:MAX_TALENT_REFS]
    for it in entries[MAX_TALENT_REFS:]:
        dropped.append({**it, "reason": f"超出主播参考图上限 {MAX_TALENT_REFS} 张"})
    return items, dropped


def _dedup_split(items: List[dict], limit: int) -> tuple:
    """去重 + 截断。返回 `(kept, skipped)`；`skipped` 里带上被丢的原因。"""
    seen, kept, skipped = set(), [], []
    for it in items:
        if it["path"] in seen:
            skipped.append({**it, "reason": "与前面的图重复（同一文件）"})
            continue
        seen.add(it["path"])
        if len(kept) >= limit:
            skipped.append({**it, "reason": f"超出单镜参考图上限 {limit} 张"})
            continue
        kept.append(it)
    return kept, skipped


def _slots(items: List[dict]) -> List[dict]:
    """给序列编上 `ref_image_N`，并标出哪张是首帧（第 0 位）。"""
    return [{**it, "slot": f"ref_image_{i}", "is_first_frame": i == 0}
            for i, it in enumerate(items)]


def _shot_layer_text(shot: Shot) -> str:
    """取 [SHOT] 层文本（画面层最核心的一句话）；没有分层标记就退回剥锚点后的全文。"""
    prompt = shot.prompt_en or ""
    m = re.search(r"^\[SHOT\]\s*(.+?)(?=^\[[A-Z]+\]|\Z)", prompt, re.S | re.M)
    if m and m.group(1).strip():
        return m.group(1).strip().lower()
    scene = re.sub(r"^\[(REF|PRODUCT)\].*?(?=^\[[A-Z]+\]|\Z)", "", prompt,
                   flags=re.S | re.M).lower()
    return scene.strip()


def _scene_is_product_led(shot: Shot) -> bool:
    """人物镜的**画面主体**是不是产品（决定首帧给产品照还是主播照）。

    ★R-27 对照实验定责：同一份提示词，ref_image_0=产品正面照时 A5 收尾镜
    干干净净；ref_image_0=主播照时模型被迫给"人+产品"凭空造环境，把湿漉漉的
    场景参考复刻进来（液体浇落+台面积水）。所以人物镜内部还要分主导：
    - 画面主体是产品（[SHOT] 层只讲产品、没有人物/身体部位词）→ 产品正面照
      当 ref_image_0（首帧锚定场景，还能锁标签文字），主播图后移保人脸一致；
    - 画面主体是人物/手部（A2 手腕、A3 按压喷头）→ 维持主播照首帧。
    判据只看 [SHOT] 层 —— 它是"这一镜的主动作"的一句话，最能代表画面主体。
    """
    text = _shot_layer_text(shot)
    if not text:
        return False
    return bool(_PRODUCT_HINTS.search(text)) and not bool(_PERSON_BODY_RE.search(text))


def shot_has_person(shot: Shot, talent: Optional[Talent]) -> bool:
    """这一镜的**画面内容**是否出现人物（决定要不要喂模特参考图）。

    ★R-16 修正：只看画面层，**不看 [REF] / [PRODUCT] 锚点行**。R-14 起骨架
    锚点被逐字强制进每一镜的 [REF]，里面必然有人称词（woman…）—— 按全文搜
    会把纯产品镜也判成人物镜，主播图永远被挂上去。剥掉这两行再搜；
    老提示词没有分层标记时（剥完为空）退回全文，行为与旧版一致。

    ★R-35 补充：**`[NEG]` 层同样必须剥掉**（同类回环）。R-35 的第六道闸门会给
    纯产品镜补 `no person, no figure, no face`，而 `_PERSON_HINTS` 里 `person`
    / `face` 是精确词 —— 否定句反过来把纯产品镜判成了人物镜，于是本镜一边声明
    "无人物"、一边挂上主播图（自相矛盾）。实测：A3/A6 剥掉 NEG 后命中词为 0。
    `[NEG]` 描述的是"不要出现什么"，永远不能用来判断画面主体。
    """
    prompt = shot.prompt_en or ""
    scene = re.sub(r"^\[(REF|PRODUCT|NEG)\].*?(?=^\[[A-Z]+\]|\Z)", "", prompt,
                   flags=re.S | re.M).lower()
    if not scene.strip():
        scene = prompt.lower()
    if talent is not None and (talent.appearance or "").strip():
        snippet = " ".join((talent.appearance or "").split()[:6]).lower()
        if snippet and snippet in scene:
            return True
    return bool(_PERSON_HINTS.search(scene))


def refs_for_shot_detail(db: Session, shot: Shot) -> dict:
    """单镜出片用的参考图 + **实际发送顺序** + 被丢弃的图（含原因）。

    顺序即语义：`ref_image_0` 是首帧。人物镜把模特图放前面（人脸一致性是最强
    约束），纯产品镜只有商品图 —— 这一点在 `why` 里如实说明，UI 直接照搬即可。

    ★R-33：商品图按**本镜的商品分配**（`shot.products`）筛，造型按
    `shot.talent_look` 挑。
    """
    proj = db.get(Project, shot.project_id)
    talent = db.get(Talent, proj.talent_id) if (proj and proj.talent_id) else None
    codes = [str(c).strip().upper() for c in (getattr(shot, "products", None) or [])
             if str(c or "").strip()]
    look = str(getattr(shot, "talent_look", "") or "")
    product_items, dropped = _product_refs(db, shot.project_id, codes=codes or None)
    want_person = shot_has_person(shot, talent)
    product_led = False
    if want_person:
        talent_items, t_dropped = _talent_refs(db, shot.project_id, look=look)
        dropped = dropped + t_dropped
        # R-27：人物镜内部再分主导 —— 画面主体是产品的镜（收尾产品镜等）
        # 产品正面照当首帧，主播图后移；其余维持"主播图在前"。
        product_led = _scene_is_product_led(shot)
        if product_led:
            ordered = product_items + talent_items
            why = ("这一镜的画面主体是产品（[SHOT] 层只讲产品）→ 产品正面照当 "
                   "ref_image_0（首帧锚定场景与标签），主播图后移保人脸一致性")
        else:
            ordered = talent_items + product_items
            why = ("这一镜的提示词里出现了人物 → 主播图排在前（ref_image_0 = 主播图，"
                   "人脸一致性是最强约束），商品图往后排")
    else:
        ordered = product_items
        why = ("提示词里没有人物 → 纯产品镜，只挂商品图，避免模型把人物塞进空镜；"
               "ref_image_0 = 商品图第 1 张")
    if codes:
        why = (f"本镜商品：{'、'.join(codes)}（脚本的逐镜商品分配，"
               f"其它商品的图一张都不挂）；" + why)
    if look:
        why += f"；本镜造型：{look}"
    kept, skipped = _dedup_split(ordered, MAX_REFS)
    return {
        "refs": kept,
        "sequence": _slots(kept),
        "dropped": dropped + skipped,
        "want_person": want_person,
        "product_led": product_led,
        "why": why,
        "products": codes,
        "talent_look": look,
        "max_refs": MAX_REFS,
        "first_frame_note": FIRST_FRAME_NOTE,
    }


def refs_for_shot(db: Session, shot: Shot) -> List[dict]:
    """单镜出片用的参考图（本地磁盘路径 + 标签）。含人物才带模特图。"""
    return refs_for_shot_detail(db, shot)["refs"]


def ref_paths_for_shot(db: Session, shot: Shot) -> List[str]:
    return [r["path"] for r in refs_for_shot(db, shot)]


def project_refs(db: Session, project_id: int) -> dict:
    """项目级参考图总览（给 UI 预览用）：商品图与主播图分开列。

    除了"有哪些图"，还要给出**两种镜型各自的实际发送顺序**与被丢弃的图 ——
    否则 UI 上一排缩略图跟真正提交的 `ref_image_N` 对不上，用户只能靠猜。
    """
    proj = db.get(Project, project_id)
    talent = db.get(Talent, proj.talent_id) if (proj and proj.talent_id) else None
    product_items, p_dropped = _product_refs(db, project_id)
    talent_items, t_dropped = _talent_refs(db, project_id)

    p_only, p_skip = _dedup_split(product_items, MAX_REFS)
    with_t, t_skip = _dedup_split(talent_items + product_items, MAX_REFS)

    # 两种镜型会重复报到同一张图，按 (path, reason) 去一次重再给用户看
    dropped, seen = [], set()
    for d in p_dropped + t_dropped + p_skip + t_skip:
        key = (d.get("path"), d.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        dropped.append(d)

    return {
        "product": product_items,
        "talent": talent_items,
        "has_talent_anchor": bool(talent and (talent.appearance or "").strip()),
        "max_refs": MAX_REFS,
        "max_product_refs": MAX_PRODUCT_REFS,
        "max_talent_refs": MAX_TALENT_REFS,
        "first_frame_note": FIRST_FRAME_NOTE,
        "sequence_product_only": _slots(p_only),
        "sequence_with_talent": _slots(with_t),
        "dropped": dropped,
        "dropped_count": len(dropped),
    }
