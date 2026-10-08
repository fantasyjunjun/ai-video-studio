"""FastAPI 入口。

单端口服务：API + 前端静态产物。
开发时前端跑 Vite(5173) 走代理；生产把构建好的 `frontend/dist` 挂到 `/`，
这样用户只需要记一个地址。
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.assets import router as assets_router
from .api.batch import router as batch_router
from .api.budget import router as budget_router
from .api.compliance import router as compliance_router
from .api.examples import router as examples_router
from .api.files import router as files_router
from .api.ingest import router as ingest_router
from .api.mcp import router as mcp_router
from .api.post import router as post_router
from .api.produce import router as produce_router
from .api.projects import router as projects_router
from .api.providers import router as providers_router
from .api.qc import router as qc_router
from .api.render import router as render_router
from .api.reverse import router as reverse_router
from .api.settings import router as settings_router
from .api.storyboard import router as storyboard_router
from .api.trae import router as trae_router
from .api.uploads import router as uploads_router
from .db.session import SessionLocal, init_db
from .services import task_ledger
from .services.produce import recover_orphan_runs

app = FastAPI(
    title="AI 视频生成工作室",
    version="0.1.0",
    description="电商香水/美妆短视频一站式生成：提示词引擎 + lint 门禁 + 可插拔供应商",
)


# ------------------------------------------------------------------ 兜底异常处理（R-42）
# 为什么必须有：未捕获异常时 Starlette 返回的是**纯文本** `Internal Server Error`，
# 于是前端 `api.ts` 的 `res.json()` 解析失败 → 回落到 `res.statusText` →
# 用户只看到「500 Internal Server Error」，**真实原因（类型/消息/traceback）
# 全被丢掉**。实测便携版首次出片就撞到这个：真因完全不可见，只能靠猜。
#
# 这里做两件事：
#   ① 完整 traceback 落 `data/error.log`（排障时能拿到真因）；
#   ② 返回 JSON `detail`，且**去掉密钥样式**后再出网（R-39 硬规矩）。
@app.exception_handler(Exception)
async def _unhandled(request, exc: Exception):  # noqa: ANN001
    import traceback as _tb
    tb = _tb.format_exc()
    try:
        from .providers.base import mask_secret
        safe = mask_secret(tb)
    except Exception:  # noqa: BLE001 -脱敏失败也要给原文
        safe = tb
    try:
        log_dir = Path(__file__).resolve().parents[1] / "data"
        log_dir.mkdir(exist_ok=True)
        with open(log_dir / "error.log", "a", encoding="utf-8") as f:
            f.write(f"\n===== {request.method} {request.url.path} "
                    f"@ {datetime.utcnow().isoformat()}Z =====\n{safe}\n")
    except Exception:  # noqa: BLE001 - 落盘失败不能反过来再炸
        pass
    return JSONResponse(
        status_code=500,
        content={"detail": (
            f"{type(exc).__name__}: {exc}\n"
            f"（完整错误已写入 data/error.log；"
            f"若是「未配置 API 密钥」类问题，请到「设置」页填写对应供应商的密钥）"
        )[:1200]},
    )

# 本地开发：允许 Vite dev server 跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173",
                   "http://localhost:4173", "http://127.0.0.1:4173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(storyboard_router)
app.include_router(render_router)
app.include_router(post_router)
app.include_router(projects_router)
app.include_router(assets_router)
# 素材上传 / 静态读取（商品图、主播参考图、示例图）
app.include_router(uploads_router)
# 贴链接导入（抓商品页 → 解析 → 图片落盘，**不入库**）
app.include_router(ingest_router)
# 内置示例商品清单
app.include_router(examples_router)
app.include_router(providers_router)
app.include_router(reverse_router)
app.include_router(settings_router)
app.include_router(files_router)
app.include_router(batch_router)
# 一键出片（R-9）：`/api/projects/{pid}/produce` 系列。
# **必须在 `_mount_frontend()` 的 SPA 兜底路由之前注册** —— 否则未知 GET 会被
# `/{full_path:path}` 接走并返回 index.html（200），看起来像"接口挂了"却不是 404。
app.include_router(produce_router)
app.include_router(compliance_router)
# 预算路由挂在 /api/budget（无路径参数），与 /api/providers 无先后之争
app.include_router(budget_router)
# 质量门（P-6）：/api/projects/{id}/qc、/api/shots/{id}/qc、/api/qc/sheet
app.include_router(qc_router)
# MCP 接入信息（P-7）：只读，无路径参数，注册顺序无要求
app.include_router(mcp_router)
# Trae Agent 接入：从网页一键用 fragrance-ecom-video 技能生成脚本分镜提示词
app.include_router(trae_router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    # 启动即巡检渲染台账：上一进程若在"提交已发出、task_id 未取得"之间挂掉，
    # 会留下 submitting 行。本进程刚起，不可能有自己的在途提交 ——
    # 所以此刻所有 submitting 行都是**上一进程的孤儿**，直接标 unknown 提示对账
    # （绝不自动重提交：那些任务可能在厂商侧已计费）。
    try:
        r = task_ledger.recover_orphans(SessionLocal, stale_sec=0)
        if r.get("marked_unknown"):
            print(f"[startup] 发现 {r['marked_unknown']} 个孤儿提交（可能已计费），"
                  f"已标 unknown，请到平台核对", flush=True)
    except Exception as e:  # noqa: BLE001 - 巡检失败不该挡住启动
        print(f"[startup] 台账巡检跳过（{type(e).__name__}: {e}）", flush=True)

    # 同理收尸一键出片 run：编排器是进程内线程，上一进程留下的
    # queued/running 都是孤儿 —— 不标 failed 会把项目永久锁死在
    # 「已有出片在跑」的 409 上（无取消端点，重试又拒绝 running）。
    try:
        n = recover_orphan_runs(SessionLocal())
        if n:
            print(f"[startup] 发现 {n} 个中断的出片 run，已标 failed"
                  f"（重试可复用已完成镜头）", flush=True)
    except Exception as e:  # noqa: BLE001 - 收尸失败不该挡住启动
        print(f"[startup] 出片 run 收尸跳过（{type(e).__name__}: {e}）", flush=True)


# ---------------------------------------------------------------- 代码指纹

# 本进程的启动时刻。**必须在本模块导入时取一次** —— 它就代表"当前进程加载的是
# 哪一版代码"。下面的漂移判据靠它对比源码 mtime。
_STARTED_AT = time.time()
_APP_DIR = Path(__file__).resolve().parent


def _stale_modules() -> list[str]:
    """启动之后又被改动过的源码文件 —— 非空即说明当前进程在跑旧代码。

    为什么要这个判据：本项目后端**不带 `--reload`**，改完必须重启才生效；
    而沙箱里 `tasklist`/`wmic`/`Get-NetTCPConnection` 全被拦或静默无输出，
    想确认"进程是不是旧的"只能让接口自述。别再靠猜。
    """
    out: list[str] = []
    for p in _APP_DIR.rglob("*.py"):
        try:
            if p.stat().st_mtime > _STARTED_AT:
                out.append(str(p.relative_to(_APP_DIR.parent)))
        except OSError:  # 文件被并发删改就跳过，不影响健康检查本身
            continue
    return sorted(out)


@app.get("/api/health")
def health():
    stale = _stale_modules()
    return {
        "status": "ok",
        "version": "0.1.0",
        "started_at": datetime.fromtimestamp(_STARTED_AT).isoformat(timespec="seconds"),
        "code_current": not stale,
        "stale_modules": stale,
        "ambient_proxy": _ambient_proxy(),
    }


def _ambient_proxy() -> list[str]:
    """本进程蹭到的 HTTP 代理环境变量（**正常应为空**）。

    加这个字段是因为踩过一次真事故：后端从带 `http_proxy` 的 shell 里启动，
    于是静默继承了它。症状是**图床偶发上传失败**，而报错完全指不到代理 ——
    表现为 uguu 回 `HTTP 400 "No input file(s)"`（大包 multipart 被代理丢掉请求体）
    或 `ConnectionResetError`，看起来像图床服务方自己挂了。
    实测同一张 2MB 参考图：经代理 39.5s 且整批复现同一个 400；直连 5.6s、9/9 全成功。

    代理不是"绝对不能有"，但**必须是显式配置的**，不能从启动环境里悄悄继承。
    出片用的 AutoDL、图床、LLM 都是直连语义（实测直连均可通）。
    """
    return [f"{k}={os.environ[k]}" for k in
            ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
            if os.environ.get(k)]


# ------------------------------------------------------------------ 前端静态托管

# 定位构建产物。**这里必须同时试两个层级**，其余 11 处路径都不需要（它们在开发态与
# 打包态恰好同指 `_internal`/backend，只有前端这一处会跨目录）：
#   开发态：backend/app/main.py → parents[2] = ai-video-studio/ → frontend/dist ✓
#   打包态：_internal/app/main.py → parents[2] = dist/avs/ ✗，但 parents[1] = _internal ✓
# 原先只写死 parents[2] → 便携版启动后首页 404（表现为 `/` 返回 {"detail":"Not Found"}），
# 而 `/api/*` 全部正常，很容易被误判成"前端没打包"。
def _find_frontend_dist() -> Path | None:
    here = Path(__file__).resolve()
    for base in (here.parents[2], here.parents[1]):
        cand = base / "frontend" / "dist"
        if (cand / "index.html").exists():
            return cand
    return None


FRONTEND_DIST = _find_frontend_dist()


def _mount_frontend() -> bool:
    """构建产物存在才挂载（没构建过就只提供 API，方便纯后端开发）。"""
    dist = FRONTEND_DIST
    if dist is None or not (dist / "index.html").exists():
        return False

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/", include_in_schema=False)
    def _index():
        return FileResponse(str(dist / "index.html"))

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa(full_path: str):
        """SPA 回退：**未知路径一律给 index.html**，交给前端路由处理。

        不能返回 404 —— 用户在 /projects/3 刷新时后端没有这条路由，
        返 404 会让整个应用打不开。
        """
        target = dist / full_path
        if full_path and target.is_file():
            return FileResponse(str(target))
        return FileResponse(str(dist / "index.html"))

    return True


FRONTEND_MOUNTED = _mount_frontend()
