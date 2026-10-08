"""供应商配置 API：查看 / 连接测试 / 切换 active / 编辑 YAML。

三条安全底线（照抄 providers 层的约定）：
  - **只暴露环境变量名**，绝不回显密钥值；
  - **自定义请求头只回键名**，值一律打码（渠道 token 常写在 header 里）；
  - **图/视频插槽不做事先花钱的探活**：AutoDL 提交即计费，探活只检查令牌与地址。
"""

from __future__ import annotations

import os
import re
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from sqlalchemy.orm import Session

from .. import deps
from ..config import (
    ImageProviderConfig, LLMProviderConfig, ProvidersConfig, VideoProviderConfig,
)
from ..db.session import get_db
from ..providers.http_safety import default_headers
from ..providers.openai_media import OPENAI_IMAGE_TYPES, OPENAI_VIDEO_TYPES
from ..providers.video_autodl import looks_like_placeholder
from ..security import secret_store as sec_store

router = APIRouter(prefix="/api/providers", tags=["providers"])

# 三个插槽对应的 pydantic 模型（用于结构化校验 + 任意中转站自定义字段透传）
_SLOT_MODELS = {
    "llm": LLMProviderConfig,
    "image": ImageProviderConfig,
    "video": VideoProviderConfig,
}

SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+[A-Za-z0-9_\-\.]{6,})")


def scrub(text: str) -> str:
    """异常信息里可能夹带密钥，回前端前一律打码。"""
    return SECRET_RE.sub(lambda m: m.group(0)[:3] + "…" + m.group(0)[-4:], text or "")


def _yaml_path() -> Path:
    return deps.BASE / "providers.yaml"


def _env_ready(names: List[Optional[str]]) -> Dict[str, bool]:
    return {n: bool(os.environ.get(n)) for n in names if n}


# ------------------------------------------------------------------ schemas


class SwitchIn(BaseModel):
    slot: str  # llm / image / video
    id: str


class TestIn(BaseModel):
    slot: str
    id: str
    timeout: float = 20.0


class YamlIn(BaseModel):
    text: str


# ------------------------------------------------------------------ 查看


def _llm_out(cfg) -> dict:
    keys = [cfg.api_key_env] + list(cfg.key_rotation or [])
    return {
        "id": cfg.id, "type": cfg.type,
        "base_url": cfg.base_url,
        "base_url_is_placeholder": looks_like_placeholder(cfg.base_url),
        "model": cfg.model,
        "api_key_env": cfg.api_key_env,
        # 仅标记是否已设密钥（内联或 OS 凭据库/本地加密文件），绝不回显值
        "api_key_set": bool(cfg.api_key) or sec_store.has_secret("llm", cfg.id),
        "key_rotation": list(cfg.key_rotation or []),
        "keys_resolved": len(cfg.resolve_keys()),
        "env_ready": _env_ready(keys),
        "extra_headers": {k: "***" for k in (cfg.extra_headers or {})},
        "timeout": cfg.timeout, "retries": cfg.retries,
        "temperature": cfg.temperature, "max_tokens": cfg.max_tokens,
        "fallback_provider": cfg.fallback_provider,
        "knowledge_base": cfg.knowledge_base,
    }


# 图 / 视频两个插槽的字段集并不相同（image 有 image_path，video 有 submit_path）。
# 早先两个插槽共用一份回传清单，于是编辑 video 时前端会收到一堆 image 专属字段的
# null —— 它们会被当成"用户自定义字段"回填进 JSON 框，并随保存写回 video 配置。
# 按插槽分开声明，是防跨插槽污染的源头治理。
_MEDIA_FIELDS = {
    "image": (
        "model", "workflow",
        "default_resolution", "default_size",
        "timeout", "retries", "spend_cap_cny",
        "ref_image_field", "ref_image_mode", "response_format",
        "image_path", "edit_path", "poll_path",
        "inject", "view_path", "history_path", "n",
        "key_rotation", "extract", "body_extra",
    ),
    "video": (
        "model", "workflow",
        "default_duration", "default_resolution",
        "cost_per_sec", "max_concurrency",
        "timeout", "poll_interval", "spend_cap_cny",
        "submit_path", "poll_path",
        "ref_image_field", "ref_image_mode",
        "field_map", "extract", "status_map", "body_extra",
        "price_map", "min_duration", "max_duration",
        "supports_negative_prompt", "no_negative_prompt_workflows",
        "key_rotation", "api_base",
    ),
}


def _media_out(cfg, slot: str) -> dict:
    out: dict = {
        "id": cfg.id, "type": cfg.type,
        "base_url": cfg.base_url,
        "base_url_is_placeholder": looks_like_placeholder(cfg.base_url),
        "token_env": cfg.token_env,
        # 仅标记是否已设令牌（内联或 OS 凭据库/本地加密文件），绝不回显值
        "token_set": bool(cfg.token) or sec_store.has_secret(slot, cfg.id),
        # resolve_token 已包含"内联 > 凭据库 > 环境变量"三级，直接据此判断是否可用
        "token_ready": bool(cfg.resolve_token()),
        # 自定义请求头只回键名，值一律打码（渠道 token 常写在 header 里）
        "extra_headers": {k: "***" for k in (cfg.extra_headers or {})},
    }
    for k in _MEDIA_FIELDS.get(slot, ()):
        # 一律用 getattr 兜底，别让"某个插槽少个字段"把整个设置页打挂
        out[k] = getattr(cfg, k, None)
    return out


@router.get("")
def list_providers():
    cfg = deps.get_providers_config()
    _host = deps.get_image_host()
    return {
        "llm": {"active": cfg.llm.active, "list": [_llm_out(c) for c in cfg.llm.list]},
        "image": {"active": cfg.image.active, "list": [_media_out(c, "image") for c in cfg.image.list]},
        "video": {"active": cfg.video.active, "list": [_media_out(c, "video") for c in cfg.video.list]},
        "image_host": {
            "enabled": _host is not None,
            "mode": getattr(_host, "mode", None) if _host is not None else None,
            "can_delete": bool(getattr(_host, "supports_delete", False)) if _host else False,
            "expiry": getattr(_host, "expiry_hint", "") if _host is not None else "",
            "env": {k: bool(os.environ.get(k))
                    for k in ("AVS_IMAGE_HOST_ROOT", "AVS_IMAGE_HOST_URL",
                              "AVS_IMAGE_HOST_UPLOAD")},
        },
        "secret_backend": sec_store.secret_backend_name(),
        "notes": [
            "内联密钥存入 OS 凭据库（Windows 凭据管理器 / macOS Keychain / Linux 密钥环）"
            "或本地加密文件，绝不明文落盘 providers.yaml；密钥值从不回显；header 只回键名。",
            "base_url_is_placeholder=true 的配置会被适配器自动回落默认地址。",
            "图/视频也可指向任意 OpenAI 兼容中转站：type 填 openai_image / openai_video，"
            "路径与字段名不匹配时用 submit_path / poll_path / field_map / extract / status_map 覆盖。",
            "图/视频插槽刻意不做降级 —— 画风不一致比失败更糟。",
        ],
    }


@router.get("/yaml")
def get_yaml():
    return {"path": str(_yaml_path()), "text": _yaml_path().read_text(encoding="utf-8")}


@router.post("/yaml")
def save_yaml(body: YamlIn):
    """整份保存。**先解析校验再落盘** —— 写坏了整个应用起不来。"""
    try:
        data = yaml.safe_load(body.text) or {}
    except yaml.YAMLError as e:
        raise HTTPException(400, f"YAML 解析失败：{e}") from e
    if "providers" not in data:
        raise HTTPException(400, "缺少顶层 `providers:` 键")
    try:
        cfg = ProvidersConfig(**data["providers"])
    except Exception as e:  # noqa: BLE001 - pydantic 的校验错误种类多，统一成 400
        raise HTTPException(400, f"配置校验失败：{scrub(str(e))[:400]}") from e
    # active 必须指向已登记的 id，否则启动即 KeyError
    for slot in ("llm", "image", "video"):
        sub = getattr(cfg, slot)
        if not any(c.id == sub.active for c in sub.list):
            raise HTTPException(400, f"{slot}.active='{sub.active}' 未在 list 中登记")

    # 截断覆盖（托管环境禁止 os.remove）
    with open(_yaml_path(), "w", encoding="utf-8") as f:
        f.write(body.text)
    reload()
    return {"saved": True, "path": str(_yaml_path())}


@router.post("/reload")
def reload():
    for fn in (deps.get_providers_config, deps.get_kb, deps.get_llm_nodes,
               deps.get_prompt_engine, deps.get_media_nodes,
               deps.get_render_queue, deps.get_image_host, deps.get_batch,
               deps.get_post, deps.get_compliance_rules):
        try:
            fn.cache_clear()
        except AttributeError:  # 未缓存的函数跳过
            pass
    return {"reloaded": True}


# ------------------------------------------------------------------ 结构化写回（保注释）

def _rt_yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 1_000_000  # 不自动折行，避免长 base_url 被截断
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _load_doc():
    y = _rt_yaml()
    return y, y.load(_yaml_path().read_text(encoding="utf-8"))


def _to_commented(obj):
    """把普通 dict/list 递归转成 ruamel 的 Commented 结构，写回时保留整体注释。"""
    if isinstance(obj, dict):
        cm = CommentedMap()
        for k, v in obj.items():
            cm[k] = _to_commented(v)
        return cm
    if isinstance(obj, (list, tuple)):
        cs = CommentedSeq()
        for v in obj:
            cs.append(_to_commented(v))
        return cs
    return obj


def _persist(y: YAML, doc) -> None:
    buf = StringIO()
    y.dump(doc, buf)
    # 截断覆盖（托管环境禁止 os.remove）
    _yaml_path().write_text(buf.getvalue(), encoding="utf-8")


def _validate_slot(slot: str) -> None:
    if slot not in _SLOT_MODELS:
        raise HTTPException(400, "slot 只能是 llm / image / video")


def _whole_file_ok(y: YAML, doc) -> None:
    """写回后整体校验一次：确保 active 仍指向合法 id，否则整个应用起不来。"""
    buf = StringIO()
    y.dump(doc, buf)
    try:
        ProvidersConfig(**yaml.safe_load(buf.getvalue())["providers"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"写回后整体校验失败：{scrub(str(e))[:300]}") from e


def write_budget_section(patch: Dict[str, Any]) -> Dict[str, Any]:
    """把 `budget:` 段写回 providers.yaml（保注释），随后 reload。

    为什么复用 providers 的写回器而不是另起一个文件：预算也是"配置"的一部分，
    落两个文件会让"当前生效的到底是哪一版"变得难查；`budget` 段缺省即"不限"，
    所以老配置文件不写这一段也能跑。

    `patch` 里值为 **None 表示删除该键**（回到缺省），其余原样写入。
    """
    y, doc = _load_doc()
    if "providers" not in doc:
        raise HTTPException(500, "providers.yaml 缺少 `providers:` 顶层键")
    prov = doc["providers"]
    cur = prov.get("budget")
    if cur is None:
        cur = CommentedMap()
        prov["budget"] = cur
    for k, v in patch.items():
        if v is None:
            if k in cur:
                del cur[k]
            continue
        cur[k] = v
    _whole_file_ok(y, doc)
    _persist(y, doc)
    reload()
    return deps.get_providers_config().budget.model_dump()


# ------------------------------------------------------------------ 新增 / 更新 / 删除
# 注意：这三个路由挂在 `/{slot}` 下，**会吞掉同名静态路径**（POST /switch、POST /test）。
# FastAPI 按注册顺序匹配，所以它们必须放在 `/switch`、`/test` 之后 —— 见文件末尾。

class _AnyProvider(BaseModel):
    body: Dict[str, Any]


# ------------------------------------------------------------------ 历史内联密钥迁移（P-1b）

def migrate_inline_secrets() -> bool:
    """把 providers.yaml 里残存的内联明文密钥迁移到 OS 凭据库/加密文件，并剥离 yaml。幂等。

    只在确实存在内联密钥时才写盘，避免每次启动都重写配置文件。
    """
    try:
        y, doc = _load_doc()
    except Exception:
        return False
    changed = False
    for slot in ("llm", "image", "video"):
        prov = doc["providers"][slot]
        for item in prov["list"]:
            pid = item.get("id")
            if not pid:
                continue
            for secret_key in ("api_key", "token"):
                val = item.get(secret_key)
                if val:
                    sec_store.set_secret(slot, pid, val)
                    del item[secret_key]
                    changed = True
    if changed:
        try:
            _whole_file_ok(y, doc)
            _persist(y, doc)
        except Exception:
            return False
    return changed


# 启动期把历史内联明文密钥搬进凭据库（幂等；无残留则不写盘）
try:
    migrate_inline_secrets()
except Exception:
    pass


# ------------------------------------------------------------------ 切换


def _set_active_in_text(text: str, slot: str, pid: str) -> str:
    """文本级替换 active，**保留注释与格式**（PyYAML dump 会把注释全丢掉）。

    只在目标 slot 的缩进块内替换一次，避免误改别的插槽。
    """
    lines = text.splitlines(keepends=True)
    n = len(lines)
    i = 0
    while i < n and lines[i].rstrip() != f"  {slot}:":
        i += 1
    if i >= n:
        raise HTTPException(400, f"YAML 里找不到 `{slot}:` 段")
    i += 1
    while i < n:
        raw = lines[i]
        if raw.strip() and not raw.startswith("   "):  # 缩进回到 ≤2 = 出了该段
            break
        # 允许行尾注释（`active: xxx  # 出厂默认`），并且替换时要把它留回去 ——
        # 配置文件里的注释往往是"为什么这么选"，丢了就只剩下一串 id
        m = re.match(r"^(\s*)active:\s*(\S+)\s*(#.*)?$", raw)
        if m:
            tail = f" {m.group(3).strip()}" if m.group(3) else ""
            lines[i] = f"{m.group(1)}active: {pid}{tail}\n"
            return "".join(lines)
        i += 1
    raise HTTPException(400, f"`{slot}` 段内没有 active 字段")


@router.post("/switch")
def switch_active(body: SwitchIn):
    cfg = deps.get_providers_config()
    if body.slot not in ("llm", "image", "video"):
        raise HTTPException(400, "slot 只能是 llm / image / video")
    sub = getattr(cfg, body.slot)
    if not any(c.id == body.id for c in sub.list):
        raise HTTPException(404, f"`{body.slot}` 里没有供应商 {body.id}")

    path = _yaml_path()
    text = path.read_text(encoding="utf-8")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_set_active_in_text(text, body.slot, body.id))
    reload()
    return {"slot": body.slot, "active": body.id}


# ------------------------------------------------------------------ 连接测试


def _test_llm(pid: str, timeout: float) -> dict:
    nodes = deps.get_llm_nodes()
    node = nodes.get(pid)
    if node is None:
        raise HTTPException(404, f"LLM 供应商未加载：{pid}")
    try:
        r = node.complete("You are a connectivity probe.",
                          "Reply with the single word: ok",
                          temperature=0, max_tokens=8)
        text = (r.text or "").strip()[:40]
        return {"ok": True, "model": r.model, "reply": text,
                "usage": r.usage or {}}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": scrub(f"{type(e).__name__}: {e}")[:400]}


def _test_image(pid: str, timeout: float) -> dict:
    cfg = deps.get_providers_config().image_by_id(pid)
    if cfg is None:
        raise HTTPException(404, f"图像供应商未登记：{pid}")
    if looks_like_placeholder(cfg.base_url):
        return {"ok": False, "error": "base_url 是占位值，先去设置里填真实地址"}
    # OpenAI 兼容中转站：GET {base}/models 探活（不计费、幂等）
    if (cfg.type or "").lower() in OPENAI_IMAGE_TYPES:
        return _probe_openai_models(cfg, timeout)
    url = (cfg.base_url or "").rstrip("/") + "/system_stats"
    try:
        import urllib.request

        req = urllib.request.Request(
            url, headers=default_headers(cfg.extra_headers or {})
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read(400).decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": scrub(f"{type(e).__name__}: {e}")[:400]}
    return {"ok": True, "url": url, "reply": body[:200]}


def _probe_openai_models(cfg, timeout: float) -> dict:
    """对 OpenAI 兼容端点做一次 GET /models 探活。

    **不产生任何费用**（不提交生成任务），只验证 base_url 可达 + 令牌鉴权通过。
    """
    import urllib.request

    url = (cfg.base_url or "").rstrip("/") + "/models"
    headers = default_headers(cfg.extra_headers or {})
    tok = cfg.resolve_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        # default_headers 保证带了正常 UA：缺 UA 会被 WAF 按 bot 拦掉（403 + HTML），
        # 报错里完全看不出是 UA 的问题。
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read(600).decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": url, "error": scrub(f"{type(e).__name__}: {e}")[:400]}
    return {"ok": True, "url": url, "reply": body[:300],
            "note": "GET /models 探活通过（未提交任何生成任务，零费用）"}


def _test_video(pid: str, timeout: float) -> dict:
    """**不提交任务** —— 提交即计费。只检查令牌与地址是否具备可用的样子。"""
    cfg = deps.get_providers_config().video_by_id(pid)
    if cfg is None:
        raise HTTPException(404, f"视频供应商未登记：{pid}")
    token = cfg.resolve_token()
    issues = []
    if not token:
        hint = f"环境变量 {cfg.token_env}" if cfg.token_env else "内联令牌/环境变量"
        issues.append(f"{hint} 未设置（令牌缺失）")
    if looks_like_placeholder(cfg.base_url or ""):
        issues.append("base_url 是占位值，将回落官方默认地址")

    # OpenAI 兼容中转站：有 base_url + 令牌时，用 GET /models 做零费用探活
    if (cfg.type or "").lower() in OPENAI_VIDEO_TYPES and not issues:
        probe = _probe_openai_models(cfg, timeout)
        if not probe.get("ok"):
            return {"ok": False, "issues": [probe.get("error", "探活失败")],
                    "url": probe.get("url")}
        return {"ok": True, "issues": [],
                "note": "GET /models 探活通过（未提交出片任务，零费用）",
                "reply": probe.get("reply")}

    return {"ok": not issues, "issues": issues,
            "note": "视频插槽不做探活请求：提交任务即计费，需真正出片时再验证"}


@router.post("/test")
def test_provider(body: TestIn):
    try:
        if body.slot == "llm":
            return _test_llm(body.id, body.timeout)
        if body.slot == "image":
            return _test_image(body.id, body.timeout)
        if body.slot == "video":
            return _test_video(body.id, body.timeout)
    except HTTPException:
        raise
    raise HTTPException(400, "slot 只能是 llm / image / video")


# ------------------------------------------------------------------ 新增 / 更新 / 删除（结构化写回）
#
# ⚠️ 必须放在文件的**最末尾**：这些路由挂在 `/{slot}` 与 `/{slot}/{pid}` 下，
# FastAPI 按注册顺序匹配 —— 若放在 `POST /switch`、`POST /test` 之前，
# 那两个静态路径会被 `/{slot}` 抢先匹配（"switch"/"test" 被当成 slot），
# 表现为"启用"和"连接测试"按钮全部 422。踩过一次，勿再移动。

@router.post("/{slot}")
def create_provider(slot: str, payload: _AnyProvider):
    _validate_slot(slot)
    raw = payload.body
    if not isinstance(raw.get("id"), str) or not raw["id"].strip():
        raise HTTPException(400, "id 必填且为字符串")
    # 空字符串一律视为"未填"，避免落盘成 api_key: '' 之类
    body = {k: v for k, v in raw.items()
            if not (isinstance(v, str) and v == "")}
    model = _SLOT_MODELS[slot]
    try:
        cfg = model.model_validate(body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"配置校验失败：{scrub(str(e))[:400]}") from e

    y, doc = _load_doc()
    prov = doc["providers"][slot]
    for item in prov["list"]:
        if item["id"] == cfg.id:
            raise HTTPException(409, f"id '{cfg.id}' 在该插槽已存在")

    # P-1b：内联密钥不落盘，改存 OS 凭据库 / 本地加密文件
    # exclude_defaults：只写用户真正填过的字段。否则一次"保存"就会把二十多个
    # 带默认值的字段（min_duration / supports_negative_prompt / view_path …）
    # 全灌进 providers.yaml，把配置文件变成一坨没人敢读的转储。
    dumped = cfg.model_dump(exclude_none=True, exclude_defaults=True)
    if slot == "llm" and isinstance(raw.get("api_key"), str) and raw["api_key"]:
        sec_store.set_secret("llm", cfg.id, raw["api_key"])
        dumped.pop("api_key", None)
    if slot in ("image", "video") and isinstance(raw.get("token"), str) and raw["token"]:
        sec_store.set_secret(slot, cfg.id, raw["token"])
        dumped.pop("token", None)

    prov["list"].append(_to_commented(dumped))
    _whole_file_ok(y, doc)
    _persist(y, doc)
    reload()
    return {"ok": True, "id": cfg.id}


@router.put("/{slot}/{pid}")
def update_provider(slot: str, pid: str, payload: _AnyProvider):
    _validate_slot(slot)
    body = payload.body
    model = _SLOT_MODELS[slot]

    y, doc = _load_doc()
    prov = doc["providers"][slot]
    lst = prov["list"]
    target = next((it for it in lst if it["id"] == pid), None)
    if target is None:
        raise HTTPException(404, f"{slot} 里没有供应商 {pid}")

    # P-1b：密钥字段不写进 yaml，统一交给凭据库。
    #  - api_key/token 显式给了非空值  -> set（并删除 yaml 内联残留）
    #  - api_key/token 显式给了空串/None -> delete（清掉凭据库条目）
    #  - 完全没给该字段             -> 不动（保留凭据库现有条目）
    # 其余字段按"现有条目 + body 覆盖"合并；内联 api_key/token 一律不进 yaml。
    secret_action = {"api_key": None, "token": None}  # None=未提供 | "set" | "delete"
    merged: Dict[str, Any] = {k: v for k, v in target.items()
                              if k not in ("api_key", "token")}
    for k, v in body.items():
        if k in ("api_key", "token"):
            secret_action[k] = "delete" if (v is None or v == "") else "set"
            continue
        merged[k] = v
    merged["id"] = pid  # id 不可改

    try:
        cfg = model.model_validate(merged)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"配置校验失败：{scrub(str(e))[:400]}") from e

    # 原地改写，保留该条目自身的行内注释；确保 yaml 里没有内联密钥残留
    # exclude_defaults 同 create：不把默认值写进配置文件。
    # 下面"clean 里没有的键就删掉"因此会顺手清掉历史遗留的默认值字段 ——
    # 语义不变（缺省即默认），但从此只留下真正被配置过的项。
    clean = cfg.model_dump(exclude_none=True, exclude_defaults=True)
    clean.pop("api_key", None)
    clean.pop("token", None)
    for k in list(target.keys()):
        if k not in clean:
            del target[k]
    for k, v in clean.items():
        target[k] = _to_commented(v)

    # 处理密钥存储（在写盘之后，确保即便下面异常也已落盘配置）
    if secret_action["api_key"] == "set":
        sec_store.set_secret("llm", pid, body["api_key"])
    elif secret_action["api_key"] == "delete":
        sec_store.delete_secret("llm", pid)
    if secret_action["token"] == "set":
        sec_store.set_secret(slot, pid, body["token"])
    elif secret_action["token"] == "delete":
        sec_store.delete_secret(slot, pid)

    _whole_file_ok(y, doc)
    _persist(y, doc)
    reload()
    return {"ok": True, "id": pid}


@router.delete("/{slot}/{pid}")
def delete_provider(slot: str, pid: str):
    _validate_slot(slot)
    y, doc = _load_doc()
    prov = doc["providers"][slot]
    if prov["active"] == pid:
        raise HTTPException(400, f"{pid} 是当前启用中的供应商，请先切到别的再删除")
    lst = prov["list"]
    idx = next((i for i, it in enumerate(lst) if it["id"] == pid), None)
    if idx is None:
        raise HTTPException(404, f"{slot} 里没有供应商 {pid}")
    del lst[idx]  # 原地删除，保留其余条目的注释
    sec_store.delete_secret(slot, pid)  # 同时清掉凭据库里的密钥
    _whole_file_ok(y, doc)
    _persist(y, doc)
    reload()
    return {"ok": True, "id": pid}
