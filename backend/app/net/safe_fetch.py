"""SSRF 防护 + 安全的取数入口（贴链接导入用）。

**为什么必须有这一层**：贴链接导入让"用户输入的 URL"直接变成"服务端发起的
请求"。不拦的话，一个 `http://169.254.169.254/` 就能读到云上的实例元数据
（拿到临时凭据），`http://127.0.0.1:6379/` 就能探内网服务。

三层防护，缺一不可：
  1. **协议白名单**：只放 http / https，`file://` / `gopher://` 一律拒。
  2. **逐跳校验**：关掉自动跳转，自己跟着 Location 走，**每一跳都重新解析
     DNS 并检查**。只查第一跳是常见漏洞 —— 公网域名 302 到内网就绕过去了。
  3. **解析后判 IP**：不看域名字面，看这个域名**实际解析到**的每个地址是否
     落在环回 / 私网 / 链路本地 / 保留段。

已知局限（写在明处，不装作没有）：若本机配了 HTTP 代理，请求实际由代理发起，
本层校验的是本地 DNS 结果、与代理侧解析结果可能不一致。所以它防的是"服务端
被诱导去打内网"，不防"代理本身被滥用"。单人本地应用按此取舍。
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Dict, Optional
from urllib.parse import urljoin

import httpx

UA = "Mozilla/5.0 (compatible; AI-Video-Studio/1.0)"
MAX_REDIRECTS = 4
DEFAULT_TIMEOUT = 15.0

# 198.18.0.0/15（RFC 2544 基准测试段）**不拦**：Cloudflare WARP 等透明代理
# 会把它当转发地址用，拦了会让正常用户打不开网页。
_ALLOW_CGNAT = False


def is_blocked_ip(ip: str) -> bool:
    """该地址是否属于环回 / 私网 / 链路本地 / 保留段。纯函数，可单测。

    用 `ipaddress` 标准库而不是手写前缀表：手写版一定会漏掉 IPv6 的
    IPv4-mapped 写法（`::ffff:127.0.0.1`）和 `fc00::/7` 这类段。
    """
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])  # 去掉 IPv6 scope id
    except ValueError:
        return True  # 解析不了就当作不安全

    # IPv6 里嵌了 IPv4（::ffff:127.0.0.1）——拆出来按 IPv4 规则再判一次
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None:
            return is_blocked_ip(str(mapped))

    if addr.is_loopback or addr.is_private or addr.is_link_local:
        return True
    if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return True
    if _ALLOW_CGNAT and addr in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return False


def resolve_ips(host: str) -> list[str]:
    """解析主机名到全部地址；本机已排除方括号。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise ValueError(f"无法解析主机：{host}（{e}）") from e
    ips = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


def assert_public_url(url: str) -> None:
    """URL 必须是 http/https，且解析出的每个地址都是公网地址。"""
    parts = httpx.URL(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 链接，收到 {parts.scheme!r}")
    host = parts.host
    if not host:
        raise ValueError("链接缺少主机名")

    host = host.strip("[]")  # URL.host 对 IPv6 字面量会带方括号
    try:
        literal = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        ips = resolve_ips(host)
    else:
        ips = [str(literal)]

    if not ips:
        raise ValueError("无法解析主机")
    for ip in ips:
        if is_blocked_ip(ip):
            raise ValueError(f"目标地址被拒绝（内网/保留地址 {ip}）")


def safe_fetch(url: str, *, headers: Optional[Dict[str, str]] = None,
               timeout: float = DEFAULT_TIMEOUT,
               max_redirects: int = MAX_REDIRECTS) -> httpx.Response:
    """带 SSRF 校验的 GET。

    关掉 httpx 的自动跳转，自己跟 Location 并**逐跳重新校验**。
    重定向目标可能是相对路径，用 `urljoin` 还原成绝对 URL 再校验。
    """
    hdrs = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        hdrs.update(headers)

    current = url
    # trust_env 交给 httpx 默认（读系统代理）：抓的是公网商品页，走代理是本机
    # 网络的常态。本机回环地址已在上面被拒，不会出现"代理打自己"的情况。
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        for _ in range(max_redirects + 1):
            assert_public_url(current)
            resp = client.get(current, headers=hdrs)
            if 300 <= resp.status_code < 400:
                loc = resp.headers.get("location")
                if not loc:
                    return resp
                current = urljoin(current, loc)
                continue
            return resp
    raise ValueError(f"重定向次数过多（>{max_redirects}）")
