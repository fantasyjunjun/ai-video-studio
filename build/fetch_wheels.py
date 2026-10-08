# -*- coding: utf-8 -*-
"""为便携版构建拉取 PyInstaller 及其依赖的 wheel。

**为什么不用 pip**：本机 pip 访问任何 PyPI 镜像一律失败（`from versions: none`），
curl 用浏览器 UA 也 403 —— 但 curl 用 **pip 的完整 UA** 就是 200。所以走
「curl 风格 urllib + pip UA 取索引 → 挑版本 → 校验 → pip --no-index 离线装」。

三条踩过的坑，这里都规避了：
  ① 版本挑错：纯数值比较会把 `1.5.0a0` 当成比 `1.3.0` 新 → 装了预发布版。
     本脚本**先剔除预发布标记**（a/b/rc/dev/post/alpha/beta）再比数值。
  ② 假成功：索引 href 打到错地址会拿回 70 字节的代理错误页并伪装成下载成功。
     所以每个 wheel 都过 `zipfile.is_zipfile()`（校验 PK 魔数）。
  ③ 代理污染：本机 `http_proxy=127.0.0.1:52720` 会被 urllib 继承，
     大请求被重置 → 脚本开头先把代理变量摘掉。

用法：
    python build/fetch_wheels.py            # 拉默认包
    python build/fetch_wheels.py --dest DIR # 指定落盘目录
"""

from __future__ import annotations

import os

# ① 必须在 import urllib 之前摘掉代理
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import argparse
import re
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urljoin

PIP_UA = (
    "pip/24.0 {\"ci\":null,\"cpu\":\"x86_64\",\"distro\":{\"name\":\"Windows\","
    "\"version\":\"10\",\"id\":\"10\",\"libc\":{\"name\":\"\",\"version\":\"\"}},"
    "\"implementation\":{\"name\":\"CPython\",\"version\":\"3.13.12\"},"
    "\"installer\":{\"name\":\"pip\",\"version\":\"24.0\"},"
    "\"openssl_version\":\"3.0\",\"python_version\":\"3.13.12\","
    "\"setuptools_version\":\"75.0.0\","
    "\"system\":{\"name\":\"Windows\",\"release\":\"10\"},\"wheel_version\":\"0.45.1\"}"
)

# 清华镜像实测 200（官方 pypi.org 也行，但镜像更快）
INDEX_TMPL = "https://pypi.tuna.tsinghua.edu.cn/simple/{pkg}/"

# PyInstaller 及其运行时依赖。顺序无关，pip 离线装会自己解依赖。
# 注意 `--no-deps` 意味着**依赖必须在这里列全**：漏一个就是运行期 ModuleNotFoundError
# （实测漏了 pefile / pyinstaller-hooks-contrib，装成功但一跑就炸）。
PACKAGES = [
    "pyinstaller",
    "pefile",                  # PyInstaller 读 exe 版本信息用（Windows 必需）
    "pyinstaller-hooks-contrib",  # 各框架的 hook 扩展
    "altgraph",
    "packaging",
    "pywin32-ctypes",
    "setuptools",
]

# 预发布标记：出现任一即视为 alpha/beta/rc，**不参与最新版本评选**
PRE_RELEASE = re.compile(r"(a|b|rc|dev|post|alpha|beta)\d*$", re.I)

# 可接受的 wheel 标签：win_amd64 或 universal(any)
OK_TAG = re.compile(r"-(py3|py2\.py3|cp3\d+)-(none|any|manylinux\d[^-]*)-win_amd64\.whl$"
                    r"|-(py2\.py3|py3)-(none|any)\.whl$")


def _fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": PIP_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _versions(name: str) -> list[tuple[tuple[int, ...], str, str]]:
    """返回 [(版本元组, 文件名, 绝对URL)]，已剔除预发布。"""
    html = _fetch(INDEX_TMPL.format(pkg=name)).decode("utf-8", "replace")
    out: list[tuple[tuple[int, ...], str, str]] = []
    for href in re.findall(r'href="([^"]+)"', html):
        url = urljoin(INDEX_TMPL.format(pkg=name), href)
        fname = url.split("#", 1)[0].rsplit("/", 1)[-1]
        if not fname.endswith(".whl"):
            continue
        stem = fname[: -len(".whl")]
        parts = stem.split("-")
        if len(parts) < 3:
            continue
        # name-version-...：版本永远是第 2 段（包名不含 '-'）
        ver = parts[1]
        if PRE_RELEASE.search(ver):
            continue
        nums = tuple(int(x) for x in re.findall(r"\d+", ver))
        if not nums:
            continue
        out.append((nums, fname, url.split("#", 1)[0]))
    return out


def pick(name: str) -> tuple[str, str]:
    cands = _versions(name)
    if not cands:
        raise SystemExit(f"[fail] {name}: 索引里没有可用的非预发布 wheel")
    # 同一版本可能多平台，优先 win_amd64，其次 any
    for tag_pat in (r"win_amd64\.whl$", r"(none|any)\.whl$"):
        best = None
        for nums, fname, url in cands:
            if not re.search(tag_pat, fname):
                continue
            if best is None or nums > best[0]:
                best = (nums, fname, url)
        if best:
            return best[1], best[2]
    raise SystemExit(f"[fail] {name}: 没有 win_amd64 / universal 轮子")


def fetch_all(dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    got: list[Path] = []
    for name in PACKAGES:
        fname, url = pick(name)
        out = dest / fname
        if out.exists() and zipfile.is_zipfile(out):
            print(f"[skip] {name} -> {fname}（已存在且合法）", flush=True)
            got.append(out)
            continue
        print(f"[get ] {name} -> {fname}", flush=True)
        blob = _fetch(url, timeout=180)
        out.write_bytes(blob)
        # ② 校验 PK 魔数，别让代理错误页伪装成 wheel
        if not zipfile.is_zipfile(out):
            raise SystemExit(f"[fail] {fname} 不是合法 wheel（拿到 {len(blob)} 字节，"
                             f"疑似代理错误页）")
        got.append(out)
    return got


def install(wheels: list[Path]) -> None:
    """离线安装。

    必须传**包名**而不是 wheel 文件名：pip 收到形如 `foo-1.0-py3-none-any.whl`
    的 requirement 时会按**当前工作目录**去找文件，而不是按 `--find-links` 解析
    （实测报 `looks like a filename, but the file does not exist`）。包名才会走
    `--find-links` 的索引。落盘目录里每个包只有一个轮子，所以不指定版本也确定。
    """
    names = sorted({w.name.split("-")[0].replace("_", "-").lower() for w in wheels})
    cmd = [sys.executable, "-m", "pip", "install", "--no-index",
           "--no-deps", "--find-links", str(wheels[0].parent)] + names
    print("[pip ]", " ".join(names), flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout[-2000:], flush=True)
    if r.returncode != 0:
        print(r.stderr[-2000:], file=sys.stderr, flush=True)
        raise SystemExit(f"[fail] pip 离线安装返回 {r.returncode}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=str(Path(__file__).resolve().parent / "_wheels"))
    ap.add_argument("--no-install", action="store_true", help="只下载不安装")
    a = ap.parse_args()
    wheels = fetch_all(Path(a.dest))
    print(f"[ok  ] 下载 {len(wheels)} 个 wheel -> {a.dest}", flush=True)
    if not a.no_install:
        install(wheels)
        r = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                           capture_output=True, text=True)
        print("[ver ]", (r.stdout or r.stderr).strip(), flush=True)


if __name__ == "__main__":
    main()
