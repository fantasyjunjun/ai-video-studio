# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir = 便携版）。

★ 关键设计：**不改一行业务代码路径**，靠目录层级的巧合对上。

后端有 12 处 `Path(__file__).resolve().parents[N]` 定位（deps.py 用 parents[1]，
db/session.py、services/storage.py、security/secret_store.py 等用 parents[2]），
全都指向 `backend/`。而 PyInstaller onedir 打出来的结构是：

    dist/avs/
      avs.exe
      _internal/            ← sys._MEIPASS
        app/deps.py ...     ← 打包后的模块

于是 `app/deps.py` 的 `parents[1]` = `_internal`，`app/db/session.py` 的 `parents[2]`
也 = `_internal`。所以只要把数据用 `--add-data` 打到 `_internal/` 下的**同名位置**，
那 12 处路径**自动全部正确** —— 零改动。

    providers.yaml  →  .              （deps.py 读 _internal/providers.yaml）
    kb/             →  kb            （deps.py 读 _internal/kb）
    migrations/     →  migrations    （session.py 的 alembic script_location）
    frontend/dist/  →  frontend/dist （main.py 的 FRONTEND_DIST）

**代价**：用户数据（data/、.secrets.enc）与程序依赖混在 `_internal/` 下。
解压目录可写，所以功能没问题；但用户若删 `_internal/` 会连素材一起丢。
以后要彻底分离，得给路径加 env 覆盖（AVS_BASE）并改那 12 处 —— 本次不做。

`data/` **不打进包**：里面是 `_r16_tmp_*` / `_e2e` / `_qc` 等开发临时目录，
而 `DATA_DIR.mkdir()` 与 `scope_dir(mkdir=True)` 会在运行时自建，干净。
"""

from pathlib import Path

# SPECPATH 由 PyInstaller 注入，指向本 spec 所在目录（build/）
ROOT = Path(SPECPATH).resolve().parent          # ai-video-studio/
BACKEND = ROOT / "backend"
FRONTEND_DIST = ROOT / "frontend" / "dist"

for _req in (BACKEND / "alembic.ini", BACKEND / "kb", BACKEND / "migrations",
             FRONTEND_DIST / "index.html", ROOT / "build" / "providers.dist.yaml"):
    if not _req.exists():
        raise SystemExit(f"[spec] 缺少打包输入：{_req}\n"
                         f"        前端先跑 `npm run build`，或检查路径是否变了。")

# ---- 数据文件：目标路径就是让 parents[N] 落到 _internal/ 之后的那个位置 ----
# ★providers.yaml 用**中性分发模板**，不用 backend/providers.yaml。
# 后者是我本机配置：带着我的中转站 base_url，而密钥在**本机凭据库**里、不跟着包走
# → 用户机器上会拿着别人的地址打请求 → 403 insufficient_user_quota（R-42 真实事故）。
# 这里连源头都换掉；make_portable.py 打包后还会再覆盖一次（双保险）。
#
# ⚠️ 目标目录写 "." 不等于改名：PyInstaller 的 datas **只保留源文件名**
# （`("a/providers.dist.yaml", ".")` → 落成 `_internal/providers.dist.yaml`），
# 而代码只认 `providers.yaml` → 启动即 500。必须先在 build/ 下生成同名文件。
_PROV_SRC = ROOT / "build" / "providers.dist.yaml"
_PROV_STAGED = ROOT / "build" / "_stage_providers" / "providers.yaml"
_PROV_STAGED.parent.mkdir(exist_ok=True)
_PROV_STAGED.write_bytes(_PROV_SRC.read_bytes())

datas = [
    (str(_PROV_STAGED), "."),                   # → _internal/providers.yaml（名字必须对）
    (str(BACKEND / "compliance.yaml"), "."),
    (str(BACKEND / "alembic.ini"), "."),
    (str(BACKEND / "kb"), "kb"),
    (str(BACKEND / "migrations"), "migrations"),
    (str(FRONTEND_DIST), "frontend/dist"),
]

# ---- 动态导入补全：漏一个就是运行期 ModuleNotFoundError ----
# uvicorn 的 loop/protocol/lifespan 都是按字符串选后端，必须显式点名；
# keyring 后端在 Windows 上是运行时按 availability 挑的；
# cryptography 的 ffi 后端同理。
hiddenimports = [
    # uvicorn：静态分析看不见字符串拼出来的这些
    "uvicorn.logging",
    "uvicorn.loops.auto", "uvicorn.loops.asyncio", "uvicorn.loops.none",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    # 音频后期：onset/sfx/mix 的数值与音频 IO
    "scipy.signal", "scipy.fft", "scipy.io.wavfile", "numpy",
    "edge_tts",
    # 凭据与加密：secret_store 的两条路径（keyring / fernet 回退）
    "cryptography", "cryptography.fernet", "cryptography.hazmat.backends.openssl",
    "keyring", "keyring.backends", "keyring.backends.Windows",
    "win32api", "win32event",
    # DB / 迁移 / 配置
    "sqlalchemy.dialects.sqlite", "alembic", "alembic.runtime.migration",
    "yaml",
]

# ---- 排除用不到的重包（venv 里混了 gradio 之类，不能让它进包）----
excludes = [
    "torch", "torchvision", "torchaudio",
    "matplotlib", "pandas", "IPython", "jupyter",
    "pytest", "tkinter", "gradio", "gradio_client",
    "notebook", "IPykernel", "sphinx",
]

block_cipher = None


a = Analysis(
    [str(ROOT / "build" / "launcher.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="avs",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX 会触发部分杀软误报，宁可大一点
    console=True,            # 带控制台：出错时用户能看到原因（也方便我们远程排障）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="avs",
)
