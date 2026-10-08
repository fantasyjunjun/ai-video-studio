# -*- coding: utf-8 -*-
"""一键打出便携版 zip。

    python build/make_portable.py              # 构建 + 复制 ffmpeg + 打 zip
    python build/make_portable.py --no-zip     # 只出文件夹（更快，便于调试）
    python build/make_portable.py --clean      # 先清掉上次的产物

产物结构（解压即用，**不要**把 exe 单独拷出来）：

    avs/
      avs.exe                 启动器（双击 → 起后端 → 开浏览器）
      ffmpeg/                 随包二进制，启动器会把它加进 PATH
        ffmpeg.exe  ffprobe.exe
      使用说明.txt
      _internal/              Python 运行时 + 依赖 + **你的数据**
        providers.yaml        （在这里改供应商，改完重启生效）
        kb/  migrations/  frontend/dist/
        data/                 素材 / 成片 / app.db ← 你的东西都在这
        .secrets.enc          加密后的 API key

⚠️ `_internal/` 装的是程序依赖**和你的数据**（见 launcher.spec 的说明）：
删掉它等于删库。升级前先备份这个目录。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

ROOT = Path(__file__).resolve().parent.parent      # ai-video-studio/
BACKEND = ROOT / "backend"
DIST = ROOT / "dist_portable"
WORK = ROOT / "build" / "_work"
APP_NAME = "avs"

# ★分发**绝不能**用 backend/providers.yaml —— 那是开发者本机的配置。
# 它带着开发者的服务商 base_url 打进包里，而**密钥永远不跟着包走**
#（在原开发者的操作系统凭据库里）。结果：用户机器上会拿着别人的地址去打请求，
# 服务商返回 `403 insufficient_user_quota 剩余额度 $0`，
# 而原文完全不提"你还没配密钥" —— 用户会去充值，钱花了仍然不通（R-42 真实踩中）。
# 所以一律用 build/providers.dist.yaml：三个 slot 的 active 都是待填写的中性占位。
PROVIDERS_SRC = ROOT / "build" / "providers.dist.yaml"

README = """AI 视频生成工作室（便携版）
================================

【★第一次使用必读：先配供应商，否则任何生成都会失败】
  这个包**不包含任何密钥**（也不会把你的密钥发给别人）。
  里面的 _internal\\providers.yaml 是**中性占位模板**，出图槽位默认是
  active: none —— 也就是说它不会去联系任何服务商。

  请进「设置」页填写（至少要填第 1 项，否则连分镜脚本都生成不出来）：
    1) 文本大模型：中转站地址或 OpenAI 官方 + API key
       （不知道填什么？看 _internal\\providers.yaml 里 my-llm 那条的注释）
    2) 出图：任意 OpenAI 兼容中转站都能出图，填 base_url + key 即可
    3) 出片：AutoDL 的 endpoint + token（或设环境变量 AUTODL_TOKEN）

  ★如果看到报错「供应商账号额度不足，剩余额度 $0」：
    这是**服务商那边这个账号没钱了**，不是软件坏了。二选一：
      ① 到该服务商后台给账号充值；
      ② 在「设置」里换成自己有余额的供应商（换完需重启软件）。
    拿这个包的新电脑尤其常见 —— 因为它带着原开发者的服务商配置，
    而你的密钥并没有跟着包走。

【怎么启动】
  双击 avs.exe。会自动启动服务并自动打开浏览器（http://127.0.0.1:8000）。
  首次启动可能需要 10~30 秒（要解包运行时），请耐心等窗口里出现
  「后端就绪，打开浏览器」。

【关闭】
  关掉 avs.exe 的窗口即可。再次双击能正常重启。

【数据在哪】
  所有数据（素材、成片、数据库、配置）都在 avs\\_internal\\ 目录下：
    _internal\\data\\        素材图、成片、app.db（SQLite 数据库）
    _internal\\providers.yaml 供应商配置（可以直接用记事本改）
  ★ 备份就是备份整个 _internal 目录。删掉它等于删库。

【常见问题】
  Q: 杀毒软件报毒 / 提示未知发布者？
  A: 本程序没有购买商业代码签名证书，Windows SmartScreen 可能拦截。
     选「更多信息」→「仍要运行」即可。这是未签名软件的正常表现。

  Q: 提示 ffmpeg 缺失或出片失败？
  A: 确认 avs\\ffmpeg\\ 目录下有 ffmpeg.exe 和 ffprobe.exe。
     不要只拷 avs.exe，ffmpeg 目录必须一起带走。

  Q: 8000 端口被占用？
  A: 关掉占用程序后重试；或设环境变量 AVS_PORT=8010 再启动。

  Q: 能装到 Program Files 吗？
  A: 不建议。本便携版需要往 _internal 目录写数据，Program Files 有写保护。
     请解压到 D:\\avideos 或 C:\\Users\\你的用户名\\ 下。
"""


def _run(cmd: list[str], what: str) -> None:
    print(f"\n=== {what} ===", flush=True)
    t0 = time.time()
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"[fail] {what} 返回 {r.returncode}")
    print(f"--- {what} 完成（{time.time() - t0:.0f}s）", flush=True)


def find_ffmpeg() -> tuple[Path | None, Path | None]:
    """找 ffmpeg/ffprobe。

    优先级：
      ① `build/ffmpeg/`（我们自备的轻量版，推荐）
      ② PATH 上的（注意 WinGet 装的是 **full_build，单个 ffmpeg.exe 222MB**，
         两个加起来 444MB，便携包会变 600MB —— 所以命中它要警告）
    """
    staged = ROOT / "build" / "ffmpeg"
    if (staged / "ffmpeg.exe").exists() and (staged / "ffprobe.exe").exists():
        return staged / "ffmpeg.exe", staged / "ffprobe.exe"

    ff = shutil.which("ffmpeg")
    fp = shutil.which("ffprobe")
    if not (ff and fp):
        # 再找 WinGet 包目录（PATH 上的可能只是链接）
        links = Path(os.environ.get("LOCALAPPDATA", "")) / \
            "Microsoft" / "WinGet" / "Links"
        for p in links.glob("ffmpeg"):
            cand = p.resolve()
            if cand.exists():
                ff = str(cand)
                fp = str(cand.with_name("ffprobe.exe"))
                break
    if not (ff and fp and Path(ff).exists() and Path(fp).exists()):
        return None, None
    return Path(ff), Path(fp)


# full_build 的单个 exe 就有 222MB； essentials 约 30MB。
FFMPEG_BUDGET_MB = 80.0


def copy_ffmpeg(app_dir: Path) -> None:
    ff, fp = find_ffmpeg()
    dst = app_dir / "ffmpeg"
    dst.mkdir(parents=True, exist_ok=True)
    if not ff:
        (dst / "放这里.txt").write_text(
            "请把 ffmpeg.exe 和 ffprobe.exe 放到本目录。\n"
            "下载地址：https://www.gyan.dev/ffmpeg/builds/ （选 essentials 即可，约 30MB）\n"
            "注意不要用 full_build（单个 exe 就有 222MB，便携包会膨胀到 600MB）。\n",
            encoding="utf-8")
        print("[warn] 没找到 ffmpeg，已留占位说明。", flush=True)
        return
    shutil.copy2(ff, dst / "ffmpeg.exe")
    shutil.copy2(fp, dst / "ffprobe.exe")
    sz = ((dst / "ffmpeg.exe").stat().st_size
          + (dst / "ffprobe.exe").stat().st_size) / 1e6
    tag = "" if sz <= FFMPEG_BUDGET_MB * 2 else "  ← 偏大，建议换 essentials 版"
    print(f"[ffmpeg] 已复制（合计 {sz:.0f} MB）{tag}", flush=True)
    if sz > FFMPEG_BUDGET_MB * 2:
        print("[warn] ffmpeg 体积异常大，很可能是 full_build。"
              "便携包会因此涨到 600MB 以上。", flush=True)
        print("[warn] 换法：下载 https://www.gyan.dev/ffmpeg/builds/ 的 "
              "ffmpeg-release-essentials.zip，解压后把两个 exe 放到 "
              "build/ffmpeg/ 再重跑本脚本。", flush=True)


def dir_size(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6


def main() -> None:
    args = set(sys.argv[1:])
    if "--clean" in args:
        for d in (DIST, WORK):
            shutil.rmtree(d, ignore_errors=True)
        print("[clean] 已清理旧产物", flush=True)

    # 1) 构建
    _run([sys.executable, "-m", "PyInstaller", "build/launcher.spec",
          "--noconfirm", "--distpath", str(DIST), "--workpath", str(WORK)],
         "PyInstaller 构建")

    app_dir = DIST / APP_NAME
    if not (app_dir / f"{APP_NAME}.exe").exists():
        raise SystemExit(f"[fail] 没找到产物：{app_dir / (APP_NAME + '.exe')}")

    # 2) ffmpeg + 说明
    copy_ffmpeg(app_dir)

    # ★强制覆盖成中性分发模板（R-42）：即使 spec 把开发者本机的 providers.yaml
    # 打进去了，这一步也会用占位版替换掉它，避免把别人的服务商地址发给用户。
    dst_cfg = app_dir / "_internal" / "providers.yaml"
    if not PROVIDERS_SRC.exists():
        raise SystemExit(f"[fail] 缺少分发配置模板：{PROVIDERS_SRC}")
    shutil.copy2(PROVIDERS_SRC, dst_cfg)
    leaked = [ln for ln in dst_cfg.read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.strip().startswith("#")
              and ln.rstrip().endswith(("api_key:", "token:"))
              and "api_key_env" not in ln and "token_env" not in ln]
    if leaked:
        raise SystemExit(f"[fail] 分发配置里疑似出现明文密钥，已中止：{leaked}")
    print(f"[cfg  ] 已写入中性分发模板（{PROVIDERS_SRC.name}）", flush=True)

    (app_dir / "使用说明.txt").write_text(README, encoding="utf-8")

    total = dir_size(app_dir)
    print(f"\n[ok] 产物：{app_dir}", flush=True)
    print(f"[ok] 体积：{total:.0f} MB（未压缩）", flush=True)

    # 3) zip
    if "--no-zip" not in args:
        zp = ROOT / f"{APP_NAME}-portable.zip"
        print(f"\n=== 打包 zip：{zp.name} ===", flush=True)
        t0 = time.time()
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for f in app_dir.rglob("*"):
                if f.is_file():
                    z.write(f, Path(APP_NAME) / f.relative_to(app_dir))
        print(f"--- 完成（{time.time() - t0:.0f}s）", flush=True)
        print(f"[ok] {zp.name}  {zp.stat().st_size / 1e6:.0f} MB", flush=True)


if __name__ == "__main__":
    main()
