# -*- coding: utf-8 -*-
"""商品图自动解读（R-18 建，R-20 改为脚本生成时识别）。

用户自由传图，**由视觉大模型判断每张图的用途与画面内容**，不再要求用户在
前端逐张标注：

- hero        主体图 —— 商品本体的清晰照片（多角度特写都算，全部进参考图）
- infographic 信息图 —— 成分表 / 香调金字塔 / 卖点排版图
- poster      海报 —— 场景氛围图（人物出镜、强后期）
- other       其他 —— 截图、无关水印图、与商品无关的图

**识别时机（R-20 起改）**：保存商品时**不再调用大模型**（上传即识别既拖慢
保存、又在用户还没决定用时提前花钱）。识别挪到**生成视频脚本**的入口：
`ensure_product_visuals` 对项目关联商品做一次「role + 画面描述」识别，结果
（含 desc）落库 `product.image_meta` 缓存；图片未变时后续生成直接复用，
**不重复花钱**。只有 hero 进 i2v 参考图（`refs._product_refs` 消费）。

识别失败（无视觉模型 / 网络 / 超时 / 解析失败）→ **全部回落 hero**，行为与
从前一致；绝不因识别失败阻断脚本生成。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .refs import PRODUCT_IMAGE_ROLES, _to_disk_path

# base64 后约 5.3MB，超过多数中转站的 5MB 上限 → 这张跳过识别（回落 hero）
MAX_IMAGE_BYTES = 4 * 1024 * 1024
# 保存请求不能被识别卡死：超时即回落
VISION_TIMEOUT = 30

SYSTEM = (
    "你是电商素材审核员。用户给出同一个商品的多张图片，请判断每张图的用途，"
    "并用一句中文描述每张图里真实可见的内容。"
    '只输出一个 JSON 数组（如 [{"role":"hero","desc":"白色磨砂瓶身，金色瓶盖，正面照"}]），'
    "数组长度必须与图片张数一致，不要输出任何解释文字或 markdown 代码块。"
)

USER_TMPL = """商品名称：{name}
商品描述：{desc}

按顺序判断上面 {n} 张图，每张输出一个对象，只含两个字段：
- role（用途，只能取）：
  - hero：商品本体的清晰照片（正面/侧面/背面/细节特写都算，多张都判 hero）
  - infographic：成分表、香调卡、卖点排版等信息图（图里有大量文字排版或示意图）
  - poster：海报或场景图（人物出镜、强后期、氛围渲染为主）
  - other：截图、带无关水印、模糊或与该商品无关的图
- desc（画面描述，一句中文，≤60字）：只写**图中真实可见**的内容 —— 容器形态、
  颜色、材质、瓶盖/包装结构、图上印的文字要点；**不要推测气味、功效、成分**。

输出 JSON 数组，长度 {n}。"""


def _guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "image/jpeg"


def _shrink_with_ffmpeg(data: bytes, mime: str) -> Optional[Tuple[bytes, str]]:
    """超大图用 ffmpeg 转码压缩（零 Python 依赖：本机装有 ffmpeg 即可）。

    背景：中转站对请求体约 5MB 上限（base64 后），原图 >4MB 只能跳过识别——
    但被跳过的往往恰是最大的**正面主体图**（P01 实测 5.1MB PNG），丢了它
    参考图与画面描述全空。压缩顺序：先原尺寸转 JPEG（q=4），还大就再缩到
    1600px 宽。任何一步失败（无 ffmpeg / 转码出错 / 仍超大）返回 None，
    调用方按旧逻辑跳过——绝不让压缩失败阻断识别。
    """
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    suffix = ".png" if "png" in (mime or "") else ".jpg"
    try:
        with tempfile.TemporaryDirectory(prefix="avs_shrink_") as td:
            src = Path(td) / f"src{suffix}"
            src.write_bytes(data)
            out = Path(td) / "out.jpg"
            # 第一档：原尺寸转 JPEG q=4（无缩放，保细节）
            r = subprocess.run(
                [exe, "-y", "-loglevel", "error", "-i", str(src), "-q:v", "4",
                 str(out)],
                capture_output=True, timeout=30,
            )
            if r.returncode != 0 or not out.exists():
                return None
            if out.stat().st_size > MAX_IMAGE_BYTES:
                # 第二档：缩到 1600px 宽（竖图 2:3 依旧清晰）
                out2 = Path(td) / "out2.jpg"
                r = subprocess.run(
                    [exe, "-y", "-loglevel", "error", "-i", str(src),
                     "-vf", "scale='min(1600,iw)':-2", "-q:v", "5", str(out2)],
                    capture_output=True, timeout=30,
                )
                if r.returncode != 0 or not out2.exists():
                    return None
                if out2.stat().st_size > MAX_IMAGE_BYTES:
                    return None
                return out2.read_bytes(), "image/jpeg"
            return out.read_bytes(), "image/jpeg"
    except Exception:  # noqa: BLE001 - 压缩失败按"跳过"处理，不阻断
        return None


def _read_images(
        image_urls: List[str]) -> Tuple[List[Tuple[int, bytes, str]], List[int]]:
    """把 URL 列表读成 `(原下标, bytes, mime)`；读不了/超大的记入跳过下标。

    ★R-24 修正：必须带**原下标**。早前版本丢弃槽位、在结果**末尾**补 hero ——
    跳过的图在中间时，后面所有图的 role/desc 会整体**错位一格**
    （把香调卡的描述安到海报头上这种）。识别结果必须按原下标回填。
    ★R-26 修正：超大图先尝试 ffmpeg 压缩（R-18 起沙箱装不上 Pillow）——
    被跳过的往往是最重要的正面主体图；压缩失败才跳过。
    """
    imgs: List[Tuple[int, bytes, str]] = []
    skipped: List[int] = []
    for i, u in enumerate(image_urls):
        p = _to_disk_path(u)
        if not p:
            skipped.append(i)
            continue
        try:
            data = Path(p).read_bytes()
        except OSError:
            skipped.append(i)
            continue
        if not data:
            skipped.append(i)
            continue
        mime = _guess_mime(p)
        if len(data) > MAX_IMAGE_BYTES:
            shrunk = _shrink_with_ffmpeg(data, mime)
            if shrunk is None:
                skipped.append(i)
                continue
            data, mime = shrunk
        imgs.append((i, data, mime))
    return imgs, skipped


def _parse_roles(text: str, n: int) -> List[Dict[str, str]]:
    """从模型输出里抠出长度为 n 的合法数组；不合格抛 ValueError。

    兼容两种输出：R-20 的 `[{"role":…,"desc":…}]` 对象数组，以及
    R-18 时代的纯字符串数组（老桩测试 / 模型偶发简化输出，desc 置空）。
    """
    raw = (text or "").strip()
    # 容错：剥掉 markdown 代码块围栏
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M).strip()
    arr = json.loads(raw)
    if not isinstance(arr, list) or len(arr) != n:
        raise ValueError(f"期望长度 {n} 的数组，得到：{str(arr)[:120]}")
    out: List[Dict[str, str]] = []
    for v in arr:
        desc = ""
        if isinstance(v, dict):
            r = str(v.get("role") or "").strip().lower()
            desc = str(v.get("desc") or "").strip()[:200]
        else:
            r = str(v or "").strip().lower()
        # 兼容旧词：bottle == hero（R-17 改名前的历史输出）
        if r == "bottle":
            r = "hero"
        out.append({"role": r if r in PRODUCT_IMAGE_ROLES else "hero",
                    "desc": desc})
    return out


def analyze_product_images(
    llm: Any,
    name: str,
    desc: str,
    image_urls: List[str],
) -> Tuple[List[Dict[str, str]], List[str], bool]:
    """让视觉模型判断每张商品图的用途 + **一句话画面描述**（R-20）。

    一次调用两用：role 供出片参考图分流（refs._product_refs 消费），
    desc 供脚本生成上下文（「商品图解读」—— 图只影响外观事实，不影响香调推断）。
    结果由调用方落库 `product.image_meta`（`[{"role":…,"desc":…}]`）做缓存，
    图片未变时后续生成**不再重复花钱**。

    返回 `(items, warnings, tagged)`：items 与 image_urls **等长**（每项含
    role 与 desc，被跳过的图 desc 为空、role 兜底 hero）；tagged=False 表示
    没识别成（调用方拿到的是全 hero 兜底），warnings 里给原因。
    """
    n = len(image_urls)
    default = [{"role": "hero", "desc": ""} for _ in range(n)]
    warnings: List[str] = []
    if n == 0:
        return [], [], False
    if llm is None:
        return default, ["未配置文本大模型，商品图未识别（全部按主体图处理）"], False

    imgs, skipped_idx = _read_images(image_urls)
    if not imgs:
        return default, (["图片读不出来或超过 4MB，未识别（全部按主体图处理）"]
                         if skipped_idx else []), False
    if skipped_idx:
        warnings.append(f"{len(skipped_idx)} 张图读不出来/超过 4MB，跳过识别（按主体图处理）")

    user = USER_TMPL.format(name=name or "（未命名）", desc=desc or "（无）", n=len(imgs))
    try:
        result = llm.complete_vision(
            SYSTEM, user, [(data, mime) for _i, data, mime in imgs],
            temperature=0, max_tokens=600,
            timeout=VISION_TIMEOUT,
        )
        got = _parse_roles(result.text, len(imgs))
    except NotImplementedError:
        return default, warnings + [
            "当前大模型不支持看图，商品图未识别（全部按主体图处理）"], False
    except Exception as e:  # noqa: BLE001 - 识别失败绝不阻断主流程
        return default, warnings + [
            f"商品图自动识别失败，已全部按主体图处理：{str(e)[:160]}"], False

    # 按**原下标**回填；被跳过的图在自己槽位上补 hero 占位（desc 空 →
    # ensure 落库时标 desc_missing；R-30 起 desc_missing 不再豁免缓存，
    # 下次生成会重试识别）
    items = [{"role": "hero", "desc": ""} for _ in range(n)]
    for (idx, _data, _mime), it in zip(imgs, got):
        items[idx] = it
    return items, warnings, True


def classify_product_images(
    llm: Any,
    name: str,
    desc: str,
    image_urls: List[str],
) -> Tuple[List[Dict[str, str]], List[str], bool]:
    """旧接口（R-18）：只返回 `[{"role":…}]`。内部复用 analyze，desc 被丢弃。"""
    items, warnings, tagged = analyze_product_images(llm, name, desc, image_urls)
    return [{"role": it["role"]} for it in items], warnings, tagged


def meta_has_visual(meta: Any, n_images: int) -> bool:
    """image_meta 缓存是否「够用」：每张图**角色已定 且 有 desc**。

    R-18 只存 role 的老数据 → 不够用，下次脚本生成重识别一次补 desc。
    ★R-24 修正：此前要求"每图都有 desc"，一张 >4MB 的大图就能让缓存**永远
    不命中** —— 每次生成脚本都重新识别、role 每轮随模型输出翻转（实测 P01
    的参考图内容两轮三变）→ 当时让 `desc_missing` 豁免。
    ★R-30 修正：desc_missing **不再豁免**——P02 实测：识别没产出 desc 的图
    被永久冻结（骨架只拿到"商品图暂无解读"，模型凭商品名编外观）。desc 空
    = 缓存不够用，下次生成**重试识别**（识别成功即自愈；真读不出的图每轮
    多花一次识别钱，属可接受代价——比"永远没事实"好）。
    """
    if not isinstance(meta, list) or len(meta) < n_images or n_images == 0:
        return False
    for i in range(n_images):
        e = meta[i] if i < len(meta) else None
        if not isinstance(e, dict) or not (e.get("role") or "").strip():
            return False
        if not (e.get("desc") or "").strip():
            return False
    return True


def visual_notes(meta: Any, n_images: int) -> List[str]:
    """从 image_meta 里读出每张图的画面描述（无 desc 的图返回空串占位）。"""
    out: List[str] = []
    for i in range(n_images):
        e = meta[i] if isinstance(meta, list) and i < len(meta) else None
        out.append((e.get("desc") or "").strip() if isinstance(e, dict) else "")
    return out


def ensure_product_visuals(db: Any, products: List[Any]) -> Tuple[List[dict], List[str]]:
    """脚本生成入口的商品图识别（R-20 建，R-24 改为**合并式**识别）。

    `products` 是 Product ORM 对象列表。对每个商品：
      - 缓存够用（`meta_has_visual`）→ 直接读出，**不花钱**；
      - 没图 → 跳过；
      - 缺缓存 → 调一次 vision，结果与旧 meta **按图合并**后落库。

    ★R-24 合并原则：**已定的角色永不覆盖**（旧条目有 role 就保留旧 role，
    只补 desc）—— 视觉模型两次输出并不总一致，整个重识别会让参考图集合
    每轮漂移（实测 P01 两轮三变，首帧从瓶身照跳到海报图）。想让某张图
    改用途，走商品编辑的手工标注（显式 meta 优先，比 AI 判定可信）。

    返回 `(visuals, warnings)`：visuals 与 products 一一对应，每项
    `{"product": prod, "notes": [每张图的中文描述, 无描述为 ""]}`。
    识别失败不抛异常 —— warnings 带原因，notes 全空，脚本生成照常进行。
    """
    from ..deps import get_active_llm  # noqa: PLC0415 - 延迟导入避免环形依赖

    visuals: List[dict] = []
    warnings: List[str] = []
    llm_cache: dict = {}
    for prod in products:
        images = list(prod.images or [])
        old_meta = getattr(prod, "image_meta", None)
        old_meta = old_meta if isinstance(old_meta, list) else []
        if not images:
            visuals.append({"product": prod, "notes": []})
            continue
        if meta_has_visual(old_meta, len(images)):
            visuals.append({"product": prod,
                            "notes": visual_notes(old_meta, len(images))})
            continue
        # 需要识别：LLM 拿不到就整体回落（连尝试都不做，不给生成添等待）
        try:
            llm = llm_cache.setdefault("llm", get_active_llm())
        except Exception:  # noqa: BLE001
            llm = None
        items, warns, _tagged = analyze_product_images(
            llm, prod.name or "", prod.description or "", images)
        warnings.extend(f"[{prod.code}] {w}" for w in warns)
        # ---- 合并：旧角色优先，desc 缺了才认新的；仍缺 → 标 desc_missing ----
        merged: List[dict] = []
        for i in range(len(images)):
            old = old_meta[i] if i < len(old_meta) and isinstance(old_meta[i], dict) else {}
            new = items[i] if i < len(items) and isinstance(items[i], dict) else {}
            role = (old.get("role") or "").strip() or (new.get("role") or "").strip() \
                or "hero"
            desc = (old.get("desc") or "").strip() or (new.get("desc") or "").strip()
            e: Dict[str, Any] = {"role": role, "desc": desc}
            if not desc:
                e["desc_missing"] = True
            merged.append(e)
        try:
            # 落库缓存。normalize 只认 role、会丢 desc —— 所以这里直接存
            # 合并结果；refs 侧读 role 走 _image_role 不受影响。
            prod.image_meta = merged
            db.commit()
        except Exception as e:  # noqa: BLE001 - 缓存写失败只影响下次，不阻断本次
            db.rollback()
            warnings.append(f"[{prod.code}] 识别结果缓存失败（本次仍可用）：{type(e).__name__}")
        visuals.append({"product": prod,
                        "notes": [it.get("desc") or "" for it in merged]})
    return visuals, warnings


def visual_context_text(visuals: List[dict]) -> str:
    """把识别结果拼成可注入脚本上下文的「商品图解读」文本块。

    空描述的图不输出；整个商品都没描述 → 输出占位说明（让模型知道不是漏了）。
    前置一条事实纪律：图内可见 = 外观事实来源；**图看不出来 = 不存在**，
    禁止从图推断香调 / 功效 / 成分（与 C 级规则同向）。
    """
    blocks: List[str] = []
    for v in visuals:
        prod = v["product"]
        notes = [n for n in (v.get("notes") or []) if n]
        if not notes:
            blocks.append(f"- [{prod.code}] {prod.name or ''}：（商品图暂无解读）")
            continue
        blocks.append(
            f"- [{prod.code}] {prod.name or ''} 商品图解读（仅描述图中可见内容）：\n"
            + "\n".join(f"  - {n}" for n in notes)
        )
    if not blocks:
        return ""
    return (
        "### 商品图解读（视觉大模型识别，供画面与文案参考）\n"
        "**事实纪律**：只把解读中真实写出的内容当作外观事实（形态/颜色/材质/"
        "包装/图上印字）；解读没提的一律当不存在，**禁止从图推断香调、功效、成分**。\n"
        + "\n".join(blocks)
    )
