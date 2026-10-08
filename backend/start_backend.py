"""后端守护启动器：父进程死循环保活，子进程跑 uvicorn，崩了自动重启。

无代理启动（摘掉 http_proxy/https_proxy），避免后端出站上传图床时
静默继承代理导致丢请求体（R-39 复盘的硬规矩）。

用法：
  python start_backend.py            # 前台（不要直接这样跑，交给 run_in_background）
  python start_backend.py --stop     # 停掉已在跑的 8000 端口后端
"""
import subprocess
import time
import os
import sys
import signal

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
HOST, PORT = "127.0.0.1", 8000


def clean_env():
    env = dict(os.environ)
    # 只摘这 4 个标准变量还不够：本机代理若以 ALL_PROXY / all_proxy 形式存在，
    # 子进程仍会走代理，HTTPS 经本地 MITM 代理常撞 `[SSL: UNEXPECTED_EOF_WHILE_READING]`。
    # 一并摘掉，确保 uvicorn 出站直连。
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    return env


def find_and_kill():
    """停掉监听 8000 端口的进程（Windows 用 netstat + taskkill）。

    注意：沙箱环境下 netstat / taskkill 会被静默拦截（stdout=None），
    此时直接跳过清理——由下面启动 uvicorn 时的端口占用异常来兜底。
    """
    try:
        r = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=15
        )
        out = r.stdout or ""
    except Exception as e:
        print("netstat 不可用（沙箱）: %s，跳过清理" % e, flush=True)
        return False
    pids = set()
    for line in out.splitlines():
        if (":%d " % PORT) in line and "LISTENING" in line:
            parts = line.split()
            pids.add(parts[-1])
    if not pids:
        print("未发现监听 %d 的进程" % PORT, flush=True)
        return False
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
        print("已结束 PID=%s" % pid, flush=True)
    return True


def main():
    if "--stop" in sys.argv:
        find_and_kill()
        return

    # 若已经有人在 8000 上，先让位（避免端口冲突导致反复重启）
    find_and_kill()
    time.sleep(1)

    env = clean_env()
    args = [
        PY, "-m", "uvicorn", "app.main:app",
        "--host", HOST, "--port", str(PORT), "--log-level", "info",
    ]
    print("guard start, uvicorn on http://%s:%d" % (HOST, PORT), flush=True)
    restart = False
    while True:
        p = subprocess.Popen(args, env=env, cwd=BACKEND_DIR)
        if restart:
            print("uvicorn 重启, pid=%d" % p.pid, flush=True)
            restart = False
        else:
            print("uvicorn 启动, pid=%d" % p.pid, flush=True)
        rc = p.wait()
        print("uvicorn 退出 rc=%s, 2s 后重启" % rc, flush=True)
        restart = True
        time.sleep(2)


if __name__ == "__main__":
    main()
