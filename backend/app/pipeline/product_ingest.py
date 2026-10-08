"""商品页解析（纯函数，无网络）。

贴链接导入的"抽信息"这一步：从商品详情页 HTML 里抠出
**标题 / 价格 / 卖点描述 / 商品图**。

抽取优先级（照搬 ClipForge 的策略，因为它符合电商站点的现实）：
  1. **JSON-LD**（`<script type="application/ld+json">` 里的 schema.org Product）
     —— 最可靠，天猫/京东/独立站的详情页基本都埋；
  2. **OpenGraph**（`og:title` / `product:price:amount` / `og:image`）；
  3. **Twitter Card**；
  4. `<title>` + `<meta name="description">` 兜底。

**纯函数**：输入 HTML 字符串、输出 dict，不碰网络。理由是可单测 ——
解析规则一多，靠"抓个真页面看看"来验必漏边界（属性顺序反了、content 里带
单引号、`&amp;#39;` 这类双重转义）。
"""

from __future__ import annotations

import html as html_mod
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

CURRENCY_SYMBOL = {
    "USD": "$", "CNY": "¥", "RMB": "¥", "EUR": "€", "GBP": "£",
    "JPY": "¥", "HKD": "HK$", "TWD": "NT$", "KRW": "₩",
    "AUD": "A$", "CAD": "C$", "BRL": "R$",
}

# 图最多取 3 张：商品库表单本身上限 5，导入只做"够用"的初始填充
MAX_INGEST_IMAGES = 3


def decode_entities(s: str) -> str:
    """解 HTML 实体并压平空白。

    用 `html.unescape`（标准库）而不是链式 `replace`：链式会把前一步解出的
    `&` 当成下一个实体的开头 —— `&amp;#39;` 本该保留字面量 `&#39;`，
    链式替换会错误地再解成 `'`。标准库单趟解析没有这个问题。
    """
    if not s:
        return ""
    return re.sub(r"\s+", " ", html_mod.unescape(s)).strip()


def _escape_re(s: str) -> str:
    return re.sub(r"([.*+?^${}()\[\]\\|])", r"\\\1", s)


def get_meta(html: str, keys: List[str]) -> Optional[str]:
    """取 meta 标签的 content，**两种属性顺序都认**。

    content 用**反向引用**匹配自己的引号（`(["'])((?:(?!\\1).)*)\\1`）而不是
    `[^"']*`：否则 `content="Tom's Mug"` 会在撇号处被截断。
    """
    for key in keys:
        k = _escape_re(key)
        pats = [
            rf'<meta[^>]+(?:property|name)=["\']{k}["\'][^>]*content=(["\'])((?:(?!\1).)*)\1',
            rf'<meta[^>]+content=(["\'])((?:(?!\1).)*)\1[^>]*(?:property|name)=["\']{k}["\']',
        ]
        for p in pats:
            m = re.search(p, html, re.I | re.S)
            if m and m.group(2).strip():
                return decode_entities(m.group(2))
    return None


def to_absolute(url: str, base: str) -> str:
    try:
        return urljoin(base, url)
    except Exception:  # noqa: BLE001
        return url


def _format_price(price: Any, currency: Optional[str]) -> Optional[str]:
    """价格文本化。

    已带货币符号或 `USD ` 前缀的原样返回（避免 `$$19.99`）；
    0 / 负数 / 非数字视为无效（避免产出"¥0"这种没意义的报价）。
    """
    if price is None:
        return None
    raw = str(price).strip()
    if not raw:
        return None

    # **先把数值抠出来判正负，再决定要不要补货币符号。**
    # 反例（原 TS 实现在这点上是错的）：`$-5` 剥掉非数字字符后得到 `5`，
    # 数值校验通过、又因为含 `$` 被原样返回 —— 负价就这么漏到了商品卡上。
    # 所以正则里必须**带上符号**，让 `-5` 自己暴露出来。
    m = re.search(r"[-+]?\d+(?:[.,]\d+)?", raw)
    if m is None:
        return None
    numeric = float(m.group(0).replace(",", ""))
    if numeric <= 0:
        return None

    if re.search(r"[¥$€£₩]", raw) or re.match(r"^[A-Za-z]{2,3}[\s\u00a0]", raw):
        return raw
    cur = (currency or "").upper()
    sym = CURRENCY_SYMBOL.get(cur, f"{cur} " if cur else "")
    return f"{sym}{raw}"


# ------------------------------------------------------------------ JSON-LD


def _find_product_node(data: Any) -> Optional[Dict[str, Any]]:
    """在任意嵌套结构里找 `@type: Product` 的节点。

    用**显式栈**而不是递归：某些站点的 JSON-LD 嵌套极深，递归会爆栈。
    """
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
            continue
        if not isinstance(cur, dict):
            continue
        t = cur.get("@type")
        if t == "Product" or (isinstance(t, list) and "Product" in t):
            return cur
        if "@graph" in cur:
            graph = cur["@graph"]
            stack.append(graph if isinstance(graph, list) else [graph])
    return None


def extract_jsonld_product(html: str) -> Optional[Dict[str, Any]]:
    """扫所有 ld+json 块，返回第一个 Product 节点。坏 JSON 跳过不报错。"""
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S,
    ):
        try:
            node = _find_product_node(json.loads(m.group(1).strip()))
        except (ValueError, TypeError):
            continue
        if node:
            return node
    return None


def _jsonld_price(node: Dict[str, Any]) -> Optional[str]:
    offers = node.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        offers = {}
    price = offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
    return _format_price(price, offers.get("priceCurrency") or node.get("priceCurrency"))


def _jsonld_images(node: Dict[str, Any]) -> List[str]:
    img = node.get("image")
    if not img:
        return []
    arr = img if isinstance(img, list) else [img]
    out = []
    for x in arr:
        if isinstance(x, str) and x:
            out.append(x)
        elif isinstance(x, dict) and isinstance(x.get("url"), str) and x["url"]:
            out.append(x["url"])
    return out


# ------------------------------------------------------------------ 主入口


def parse_product_from_html(html: str, base_url: str) -> Dict[str, Any]:
    """解析商品页。返回 dict（不是 Pydantic 模型 —— 这里只做抽取，不做校验）。"""
    ld = extract_jsonld_product(html)

    title_meta = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = (
        (ld and ld.get("name") and decode_entities(str(ld["name"])))
        or get_meta(html, ["og:title", "twitter:title"])
        or (title_meta and decode_entities(title_meta.group(1)))
        or ""
    )

    og_price = get_meta(html, ["product:price:amount", "og:price:amount"])
    og_cur = get_meta(html, ["product:price:currency", "og:price:currency"])
    price_text = (ld and _jsonld_price(ld)) or _format_price(og_price, og_cur)

    description = (
        (ld and ld.get("description") and decode_entities(str(ld["description"])))
        or get_meta(html, ["og:description", "twitter:description", "description"])
    )

    raw_images: List[str] = []
    if ld:
        raw_images.extend(_jsonld_images(ld))
    for keys in (["og:image", "og:image:secure_url"],
                 ["twitter:image", "twitter:image:src"]):
        v = get_meta(html, keys)
        if v:
            raw_images.append(v)

    seen = set()
    images: List[str] = []
    for u in raw_images:
        if not u:
            continue
        absu = to_absolute(decode_entities(u), base_url)
        if not re.match(r"^https?://", absu, re.I):
            continue
        if absu in seen:
            continue
        seen.add(absu)
        images.append(absu)

    return {
        "title": (title or "").strip(),
        "price_text": price_text,
        "description": description,
        "images": images,
        "source_url": base_url,
    }


def pick_ingest_images(images: List[str], limit: int = MAX_INGEST_IMAGES) -> List[str]:
    """挑要下载的图。前 limit 张，保持原顺序（首图通常是主图）。"""
    return list(images or [])[:limit]
