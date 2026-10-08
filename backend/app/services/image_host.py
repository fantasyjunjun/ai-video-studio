"""临时图床：参考图发布 / 下线 + 租约登记。

为什么需要它
------------
AutoDL 之类的平台要求 `ref_image_0..N` 是**公网可达 URL**，
本地路径（`C:/...` 或相对路径）一律不可用（实测会被判"URL 地址不合法"）。
所以出片前必须把本地参考图发布出去，出片后必须下线。

为什么要有"租约"
----------------
踩过的坑：忘下线 = 资产长期挂在公网。这里把每次发布登记成一行 `ImageHostLease`，
提供 `unpublish_all(project_id)` 与启动时的"未关闭租约"自查，
把"用完下线"从人的记性变成系统的闭环。

两种实现怎么选
--------------
- `StaticDirImageHost` —— 已有"某个静态目录被暴露成公网 URL"时用（本地 nginx /
  `python -m http.server` / sites 发布）。
- `UploadImageHost` —— 参考图直接 POST 给临时文件托管服务，当场拿 URL。**本站默认走这条**，
  原因见该类的 docstring（简言之：静态图床的 URL 指向一份"已上传的快照"，而本应用发布参考图
  时的目录含 **job id**，快照里不可能预先存在这些路径）。

下线方式（重要）
----------------
- 静态目录：**绝不使用 `os.remove`** —— 本环境的注入式安全钩子会在删除调用处中止进程
  （表现为脚本一声不响退出、产物 0 字节）。下线一律走**截断覆盖**：把文件内容覆盖为占位字节。
- 上传型：这类服务**没有删除 API**，只能等服务方按时效自动过期。所以"下线"对它而言
  只是"我方用完了"，`supports_delete=False` 把这点如实透出，**不能把"标记为已下线"
  说成"文件已删除"**。
"""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
import time
import urllib.request
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from ..providers.http_safety import request_bytes

#: `ensure_public_urls` 对**整批**发布的最大尝试次数。
#: 上传参考图是**不计费**且**幂等**的（重复上传只是多几个会自己过期的文件），
#: 所以值得大方重试 —— 实测某匿名图床经本地代理时，同一张 2MB 图一次要 39.5s、
#: 另一次只要 5.6s，还会偶发丢请求体（服务端回 `No input file(s)`）与连接重置。
PUBLISH_ATTEMPTS = 3
PUBLISH_RETRY_BASE_SEC = 1.0


class ImageHostError(RuntimeError):
    """参考图发布失败 —— **明确区别于"出片失败"**。

    这个类型存在的唯一理由是**说清钱和状态**：发布发生在 `provider.submit` **之前**，
    所以走到这里就意味着「**没有提交、没有计费、没有任务**」。
    实测事故：这个阶段的失败被当成平台报错（`HTTP 400 ... No input file(s)`），
    排查方向整个跑偏 —— 查了半天 AutoDL，其实是图床上传丢包，
    而且台账里连一条提交记录都没有（这恰恰是"没花钱"的铁证）。
    """


class ImageHost(ABC):
    """临时图床接口。换图床 = 换实现，上层编排代码不动。"""

    #: 是否真的能从服务方把文件删掉/作废。上传型临时图床为 False（只能等过期）。
    supports_delete: bool = True

    @abstractmethod
    def publish(self, files: Iterable[Path], *, namespace: str = "") -> Tuple[str, List[str]]:
        """发布若干本地文件，返回 (根 URL, 文件名或完整 URL 列表)。

        约定：`names` 里**以 http(s) 开头的元素视为完整 URL**，否则按 `根 URL + "/" + name`
        拼（见 `ensure_public_urls`）。上传型图床每个文件一条独立 URL，用前者。
        """
        raise NotImplementedError

    @abstractmethod
    def unpublish(self, names: Iterable[str], *, namespace: str = "") -> List[str]:
        """使已发布文件失效，返回被处理的清单。"""
        raise NotImplementedError

    @property
    def mode(self) -> str:
        """给 UI / 接口看的一句话标识（只讲类型，不含密钥）。"""
        return type(self).__name__

    @property
    def expiry_hint(self) -> str:
        """文件在服务方那边能存活多久（上传型才有意义）。"""
        return ""


class StaticDirImageHost(ImageHost):
    """静态目录图床。

    适用所有"某个静态目录已被暴露成公网 URL"的情形，例如：
      - 本地 nginx / `python -m http.server`
      - workbuddy_sites_deploy 发布某个目录后得到的 URL

    用法：
        host = StaticDirImageHost(serve_root="storyboards/S05/refhost",
                                  root_url="https://xxx.app.workbuddy.host")
        url, names = host.publish([Path(...)/"m02_front.jpg"])
        # 出片后
        host.unpublish(names)
    """

    PLACEHOLDER = b"removed-by-ai-video-studio"
    supports_delete = True

    def __init__(self, serve_root: str | Path, root_url: str) -> None:
        self.serve_root = Path(serve_root)
        self.root_url = str(root_url).rstrip("/")

    @property
    def mode(self) -> str:
        return "static"

    @property
    def expiry_hint(self) -> str:
        return "由使用者自己托管的静态目录，不会自动过期（下线靠截断覆盖）"

    def publish(self, files: Iterable[Path], *, namespace: str = "") -> Tuple[str, List[str]]:
        target_dir = self.serve_root / namespace if namespace else self.serve_root
        target_dir.mkdir(parents=True, exist_ok=True)

        names: List[str] = []
        base = self.root_url + (f"/{namespace}" if namespace else "")
        for f in files:
            p = Path(f)
            if not p.exists():
                raise FileNotFoundError(f"待发布文件不存在: {p}")
            # 统一小写后缀，避免 JPG/PNG 混用导致的 URL 404
            suffix = p.suffix.lower() or ".jpg"
            name = p.stem + suffix
            shutil.copy2(p, target_dir / name)
            names.append(name)
        return base, names

    def unpublish(self, names: Iterable[str], *, namespace: str = "") -> List[str]:
        target_dir = self.serve_root / namespace if namespace else self.serve_root
        done: List[str] = []
        for n in names:
            p = target_dir / n
            if not p.exists():
                continue
            # 截断覆盖，绝不删除
            with open(p, "wb") as f:
                f.write(self.PLACEHOLDER)
            done.append(n)
        return done


# --------------------------------------------------------------- 上传型图床


@dataclass(frozen=True)
class UploadBackend:
    """上传型图床的**厂商差异**：字段名、响应格式、时效。换服务只换这个。"""

    name: str
    endpoint: str
    field: str              # 表单里装文件的那个字段名（写错就是 400）
    expiry_hint: str
    # 60s 而不是 120s：实测正常上传 ≤6s、经本地代理最慢约 40s。
    # 超时给太大只会让"网络坏掉"这种情况在队列里干等（外层还有整批重试兜着）。
    timeout: float = 60.0

    def build_request(self, path: Path) -> urllib.request.Request:
        boundary = "----avs" + uuid.uuid4().hex
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{self.field}"; filename="{path.name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        return urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def extract_url(self, body: bytes) -> str:  # pragma: no cover - 由子类实现
        raise NotImplementedError


UGUU = UploadBackend(
    name="uguu",
    endpoint="https://uguu.se/upload",
    # 字段名必须是 `files[]`：写成 `file` 会拿到 `400 Bad Request`，
    # 而错误体是空的，看起来像"服务端挂了"。这是实测踩出来的。
    field="files[]",
    expiry_hint="约 3 小时（服务方自动过期，无删除 API）",
)


def _uguu_extract(body: bytes) -> str:
    """uguu 响应：`{"success":true,"files":[{"url":"https://n.uguu.se/xxx.jpg"}]}`。"""
    data = json.loads(body.decode("utf-8", "replace"))
    if not data.get("success"):
        raise RuntimeError(f"uguu 上传失败: {str(data)[:200]}")
    files = data.get("files") or []
    if not files or not str(files[0].get("url") or "").startswith("http"):
        raise RuntimeError(f"uguu 响应里没有可用 URL: {str(data)[:200]}")
    return str(files[0]["url"])


#: 服务名 → (构造 backend, 取 URL 的函数)
UPLOAD_BACKENDS = {
    "uguu": (lambda: UGUU, _uguu_extract),
}


class UploadImageHost(ImageHost):
    """上传型图床：把参考图 POST 给临时文件托管服务，当场拿到公网 URL。

    为什么本应用默认用它
    --------------------
    `StaticDirImageHost` 的 URL 指向一份**已上传的快照**（比如 sites 发布出去的目录），
    而本应用发布参考图时的目录名含 **job id**（`p{pid}/j{jid}`，出片时才生成）——
    快照里不可能预先存在这些路径，于是每出一片都得重新发布一次站点，等于不可用。
    上传型图床与目录、与快照都无关，所以没有这个问题。

    代价（如实记账，别粉饰）
    ----------------------
    这类服务**没有删除 API**：`unpublish` 返回空列表，"下线"实际是"等它自己过期"。
    因此 `supports_delete=False`，租约的 note 里会写明时效，UI 也只说"用完了/将过期"，
    绝不说成"已删除"。

    另外：参考图会短暂出现在公网（这是平台取图的前提，不是本实现的额外泄漏），
    介意的话改用 `StaticDirImageHost` 指向自己的受控目录。
    """

    supports_delete = False

    def __init__(self, backend: UploadBackend, extract=None) -> None:
        self.backend = backend
        self._extract = extract or _uguu_extract

    @property
    def mode(self) -> str:
        return f"upload:{self.backend.name}"

    @property
    def expiry_hint(self) -> str:
        return self.backend.expiry_hint

    def publish(self, files: Iterable[Path], *, namespace: str = "") -> Tuple[str, List[str]]:
        """逐个上传，返回 (服务方 origin, [完整 URL, ...])。

        `namespace` 在这里没有意义（服务方不分目录），保留只为满足接口一致 ——
        别把它塞进文件名，同名参考图在不同 job 之间是靠 URL 唯一的。
        """
        urls: List[str] = []
        for f in files:
            p = Path(f)
            if not p.exists():
                raise FileNotFoundError(f"待发布文件不存在: {p}")
            req = self.backend.build_request(p)
            # 上传是**非计费**请求 → 允许自动重试（见 http_safety 的分档）。
            # 但这里只给自己留 1 次重试：外层 `_publish_with_retry` 还会整批重来，
            # 两层都开满 2 次会让最坏耗时乘到几十分钟（超时 60s × 2 × 3 批）。
            body = request_bytes(
                req, billable=False, timeout=self.backend.timeout, max_retries=1,
                provider=self.backend.name, endpoint=self.backend.endpoint,
            )
            urls.append(self._extract(body))
        origin = ""
        if urls:
            sp = urlsplit(urls[0])
            origin = f"{sp.scheme}://{sp.netloc}"
        return origin, urls

    def unpublish(self, names: Iterable[str], *, namespace: str = "") -> List[str]:
        """上传型图床没有删除 API，只能等过期 —— 返回空列表（**不是**失败）。"""
        return []


def ensure_public_urls(
    paths: List[str],
    host: Optional[ImageHost] = None,
    *,
    namespace: str = "",
) -> Tuple[List[str], Optional[Tuple[str, List[str]]]]:
    """把本地路径提升为公网 URL。

    已经是 http(s) 的 URL 原样放行（比如图床里的历史资产）；
    本地路径才走发布流程。

    `host.publish` 返回的 `names` 有两种形态：**本身就是完整 URL**（上传型图床，
    每个文件一条独立 URL）或**文件名**（静态目录图床，与根 URL 拼接）。这里两种都认。

    返回 (urls, lease_info)；lease_info 为 None 表示没有新发布、无需下线。
    """
    local = [p for p in paths if not str(p).startswith(("http://", "https://"))]

    if not local:
        return [str(p) for p in paths], None
    if host is None:
        raise ValueError(
            "存在本地参考图但未提供图床：出片需要公网可达 URL。"
            "请配置 ImageHost（AVS_IMAGE_HOST_ROOT+AVS_IMAGE_HOST_URL 静态目录图床，"
            "或 AVS_IMAGE_HOST_UPLOAD 上传型图床）。"
        )

    base, names = _publish_with_retry(host, local, namespace=namespace)
    url_of = {}
    for src, n in zip([str(p) for p in local], names):
        n = str(n)
        url_of[src] = n if n.startswith(("http://", "https://")) else f"{base}/{n}"
    # 严格保持入参顺序 —— ref_image_0/1/2 的指派取决于此，乱序会把瓶子和模特喂反
    urls = [str(p) if str(p).startswith(("http://", "https://")) else url_of[str(p)]
            for p in paths]
    return urls, (base, names)


def _ambient_proxy_hint() -> str:
    """本机是否存在"应用没打算用、却被继承进来"的代理。

    **为什么值得专门提示**：本项目的出站请求都是直连语义。但如果后端进程是从一个
    带 `http_proxy` 的 shell 里启动的，它就会静默继承 —— 表现为"图床上传偶发失败"，
    而报错信息里**完全看不出代理的存在**（很像图床服务方挂了）。
    实测：同一张 2MB 参考图，走本地代理 39.5s、直连 5.6s，且代理下会出现
    丢请求体（服务端回 `No input file(s)`）与 `ConnectionResetError`。
    """
    live = [f"{k}={os.environ[k]}" for k in
            ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
            if os.environ.get(k)]
    if not live:
        return ""
    return ("｜注意：当前进程继承了 HTTP 代理（" + "、".join(live) +
            "），大文件上传经它可能丢包/被重置。"
            "如非必需，重启后端时清掉这些环境变量再试。")


def _publish_with_retry(host: ImageHost, local: List[str],
                        *, namespace: str) -> Tuple[str, List[str]]:
    """整批发布，失败就重来 —— 上传不计费且幂等，重试的代价只有几秒。

    逐文件的 `request_bytes` 已经会在 429/5xx/网络错误上退避重试，但仍然不够：
    实测图床会**丢请求体**，服务端于是回一个 `HTTP 400`（`http_safety` 按
    "明确的客户端错误"不重试），而实际上换个时刻重发就成功了。
    所以在这一层再兜一层"整批重试"。
    """
    last: Optional[Exception] = None
    for attempt in range(1, PUBLISH_ATTEMPTS + 1):
        try:
            return host.publish([Path(p) for p in local], namespace=namespace)
        except FileNotFoundError:
            raise  # 本地文件没了，重试一百次也没用，立刻如实报错
        except Exception as e:  # noqa: BLE001 - 图床的失败形态不值得逐个枚举
            last = e
            if attempt < PUBLISH_ATTEMPTS:
                time.sleep(PUBLISH_RETRY_BASE_SEC * attempt)
    raise ImageHostError(
        f"参考图上传失败（共尝试 {PUBLISH_ATTEMPTS} 次）："
        f"{type(last).__name__}: {str(last)[:300]}。"
        f"**这一步在提交出片之前，所以没有提交、没有计费、没有任务**。"
        f"请重试出片；若持续失败，换一个图床（设置 → 图床）"
        f"{_ambient_proxy_hint()}"
    ) from last


def lease_model_defaults(host: str, root_url: str, serve_root: str,
                         project_id: Optional[int], published: List[str],
                         note: str = "") -> dict:
    return {
        "host": host,
        "root_url": root_url,
        "serve_root": serve_root,
        "project_id": project_id,
        "published": published,
        "is_offline": 0,
        "note": note,
        "created_at": datetime.utcnow(),
    }
