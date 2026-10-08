# -*- coding: utf-8 -*-
"""便携版启动器：起后端 → 等健康 → 开浏览器。

**为什么需要它**：本项目是「单端口 8000 + 后端同域托管前端」的架构（见
`app/main.py::_mount_frontend`），所以桌面化**不需要 Electron/Tauri 重写前端** ——
启动器只要把 uvicorn 跑起来、等 `/api/health` 通了再打开浏览器即可。

三件在打包环境里必须显式做的事（开发态靠环境碰巧是对的）：

1. **摘掉代理**：本机 `http_proxy=127.0.0.1:52720` 会被子进程继承，导致图床上传
   丢请求体（uguu 回 `No input file(s)`）。实测有代理 11 次失败 2 次、
   无代理 9/9 全成。开发态靠 `env -u` 命令行兜着，打包后没人兜了。
2. **ffmpeg 进 PATH**：`app/audio/ffmpeg.py::require()` 用 `shutil.which("ffmpeg")`，
   只认 PATH。所以把随包的 `ffmpeg/` 目录塞进 PATH 就够，**不用改业务代码**。
3. **数据目录可写**：打包后 `data/`、`providers.yaml` 落在 exe 同级的 `_internal/`
   （由 spec 的 `--add-data` 决定），解压目录可写，不需要改任何路径代码。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------- 1. 环境准备

# ① 必须在任何网络库初始化之前摘代理（否则 socket 层会挂上代理）
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

# 开发态：把 backend/ 放进 sys.path 才能 import app.*；打包态 app 已在包内
if not getattr(sys, "frozen", False):
    _BUILD = Path(__file__).resolve().parent
    sys.path.insert(0, str(_BUILD.parent / "backend"))

# exe 所在目录（便携版所有可写数据与 ffmpeg 都以它为根）
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent.parent

# ② ffmpeg 随包目录 → PATH（代码用 shutil.which 找，所以只需改 PATH）
_ffdir = ROOT / "ffmpeg"
if _ffdir.is_dir():
    os.environ["PATH"] = str(_ffdir) + os.pathsep + os.environ.get("PATH", "")

HOST = "127.0.0.1"
PORT = int(os.environ.get("AVS_PORT") or 8000)
URL = f"http://{HOST}:{PORT}/"

# 控制台按 UTF-8 输出：默认跟随 locale(936/GBK) 时，中文日志在重定向到文件或
# 非中文 Windows 上就是一团乱码（实测 `[avs] 根目录` → `[avs] ��Ŀ¼`），
# 而这行恰恰是排障时最该看清的信息。errors=replace 保证再怪也不抛异常。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 同时落一份 UTF-8 日志：用户双击报错时可以直接把 avs.log 发给我们
try:
    _LOG = open(ROOT / "avs.log", "a", encoding="utf-8", buffering=1)
except Exception:
    _LOG = None


def log(msg: str) -> None:
    line = f"[avs] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    if _LOG is not None:
        try:
            _LOG.write(line + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------- 2. 端口占用

def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def pick_port(preferred: int) -> int:
    """端口被占用时，优先**直接复用**已在跑的那个实例（重复双击不叠加进程），
    找不到可用的就顺延，最多试 20 个端口。"""
    for p in range(preferred, preferred + 20):
        if _port_free(p):
            return p
    raise SystemExit(f"[avs] {preferred}..{preferred + 19} 都被占用了，请先关掉占用的程序")


# ---------------------------------------------------------------- 3. 等健康

def wait_healthy(port: int, timeout: float = 90.0) -> bool:
    """轮询 `/api/health`。

    判据用**接口本身**而不是「端口能连」—— 端口可连但 app 还在 import，
    打开浏览器会看到白屏。本地起后端冷启动要几秒（cryptography/keyring 首次
    初始化更慢），所以超时给到 90s。
    """
    url = f"http://{HOST}:{port}/api/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------- 4. 主流程

def main() -> int:
    port = pick_port(PORT)
    log(f"根目录 {ROOT}")
    log(f"监听 {HOST}:{port}")

    # 端口已被同款实例占着 → 直接开浏览器，不要再起第二个后端
    if not _port_free(port):
        log("检测到已有实例在运行，直接打开浏览器")
        webbrowser.open(URL if port == PORT else f"http://{HOST}:{port}/")
        return 0

    from app.main import app
    import uvicorn

    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="avs-backend", daemon=True)
    t.start()

    if not wait_healthy(port):
        log("后端启动超时（90s）。常见原因：ffmpeg 缺失 / 端口被占用 / 依赖没打全")
        return 1

    log("后端就绪，打开浏览器")
    webbrowser.open(f"http://{HOST}:{port}/")

    # 守着后端线程直到它退出（Ctrl+C 或后端崩了）
    try:
        while t.is_alive():
            t.join(0.5)
    except KeyboardInterrupt:
        log("正在关闭…")
        server.should_exit = True
        t.join(5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
