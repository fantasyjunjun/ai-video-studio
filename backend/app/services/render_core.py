"""出片核心：把「让一个镜头出片」这件事完整做一遍。

抽出来的理由
------------
单镜出片（`POST /api/shots/{id}/render`）和**一键出片编排**（`services/produce.py`）
要做的是同一件事：定供应商 → 定时长 → 挂参考图 → 查复用 → 过预算闸门 → 建台账入队。

这两条路径**必须共用一份实现**。尤其下面两条，任何一处漏掉都会让结果与脚本脱钩：

  - **时长跟随分镜**（脚本写 3s 就得是 3s，不能落到供应商默认 5s）；
  - **自动挂参考图**（产品图 + 主播图；纯产品镜不带模特图，否则人物会进空镜）。

历史上这两条都各自漏过，所以不再允许出现第二份实现。

不 import `deps`
----------------
供应商池与配置由调用方传进来（`media_nodes` / `cfg`）。这样这一层不依赖全局单例，
离线冒烟可以直接塞假供应商，也不会有 import 环。
"""

from __future__ import annotations

import math
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from ..db.models import RenderJob, Shot
from . import budget as budget_mod
from . import refs as refs_mod

# 出片工作流（AutoDL minimax_h3_lightx2v_v5_15s）能接受的时长区间
VIDEO_MIN_SEC = 1
VIDEO_MAX_SEC = 15


class UnknownKind(ValueError):
    """`kind` 既不是 image 也不是 video。路由层翻成 400。"""


class ProviderNotReady(ValueError):
    """供应商的本地静态前提不满足（工作流文件缺失 / 占位地址 / 没令牌）。

    与 `UnknownKind` 一样属于**提交前就能确定**的错误（400 一类），但单独成类，
    因为处置动作不同：kind 拼错要改调用方，供应商没配好要去设置页换或修。
    消息里带的是 `MediaProvider.preflight()` 给出的那句可执行原因。
    """


def shot_video_duration(shot: Shot, fps: float = 24.0) -> Optional[int]:
    """分镜时长 → 出片请求时长（**整数秒**）。

    为什么需要它：出片接口只收整数秒，而 15s / 5 镜的脚本里常常是
    3.0 / 3.0 / 3.5 / 3.0 / 2.5 这种非整数。**一律向上取整** ——
    装配阶段会按 `target_frames` 精确裁剪，多出来的部分被切掉没有任何损失；
    反过来向下取整就会缺帧，只能拉伸或补帧，画面必然劣化。

    不传这个值时会落到供应商的 `default_duration`（默认 5s），
    于是"脚本写 3s、出片变 5s"—— 时长与脚本脱钩，成片配不平。
    """
    frames = shot.target_frames
    if frames:
        sec = float(frames) / (fps or 24.0)
    elif shot.duration_sec:
        sec = float(shot.duration_sec)
    else:
        return None
    # 减一个极小量：3.0 不该被浮点误差抬成 4
    return max(VIDEO_MIN_SEC, min(VIDEO_MAX_SEC, int(math.ceil(sec - 1e-6))))


def estimate_media(media_nodes: Tuple[Dict[str, Any], Dict[str, Any]],
                   kind: str, provider_id: str,
                   duration: Any, resolution: Any) -> Tuple[float, str]:
    """按供应商自己的价目表估一次费用，返回 (金额, 说明)。

    估不出来返回 **0.0**（"估不出"而不是"免费"）—— 此时预算熔断只能靠
    已花总额判断，不能靠本次预估；这一点会体现在拒绝理由里。
    """
    img_nodes, vid_nodes = media_nodes
    pool = vid_nodes if kind == "video" else img_nodes
    prov = pool.get(provider_id)
    fn = getattr(prov, "estimate_cost", None) if prov is not None else None
    if not callable(fn):
        return 0.0, "该供应商未提供费用预估"
    try:
        amt, desc = fn(duration, resolution)
        return float(amt or 0.0), str(desc)
    except Exception as e:  # noqa: BLE001 - 预估失败不该挡住出片
        return 0.0, f"费用预估失败：{type(e).__name__}"


def resolve_render_spec(shot: Shot, kind: str, cfg,
                        *, duration: Optional[int] = None,
                        resolution: Optional[str] = None,
                        provider_id: Optional[str] = None,
                        ref_image_paths: Optional[Sequence[str]] = None,
                        db: Optional[Session] = None) -> Dict[str, Any]:
    """算出这次出片的**全部有效参数**（不产生任何副作用、不花钱）。

    单独暴露出来是给「一键出片」的规划预览用的：前端要能在**没花钱之前**
    看到"每镜会请求几秒、会喂哪几张参考图"。
    """
    if kind == "video":
        resolved_provider = provider_id or cfg.video.active
        duration_source = "explicit"
        if duration:
            out_duration: Optional[int] = int(duration)
        else:
            out_duration = shot_video_duration(shot)
            duration_source = "shot" if out_duration else "provider_default"
        if not out_duration:
            out_duration = cfg.active_video_config().default_duration
        out_resolution = resolution or cfg.active_video_config().default_resolution
    elif kind == "image":
        resolved_provider = provider_id or cfg.image.active
        out_duration = None
        duration_source = "none"
        out_resolution = resolution or cfg.active_image_config().default_resolution
    else:
        raise UnknownKind(f"未知 kind: {kind}")

    # ---- 参考图：显式优先，缺省则自动按项目解析（产品图 + 模特图）----
    # 这一步是"图生视频"成立的前提：前端从未传过 ref_image_paths，
    # 不自动解析的话出片就退化成纯文生视频（产品与模特根本不进画面）。
    # 纯产品镜按镜判定，不带模特图 —— 否则模型可能把人物塞进空镜画面。
    refs = [p for p in (ref_image_paths or []) if p]
    refs_source = "explicit"
    if not refs:
        refs = refs_mod.ref_paths_for_shot(db, shot) if db is not None else []
        refs_source = "auto"

    return {
        "provider_id": resolved_provider,
        "duration": out_duration,
        "duration_source": duration_source,
        "resolution": out_resolution,
        "refs": list(refs),
        "refs_source": refs_source,
    }


def build_job_params(shot: Shot, spec: Dict[str, Any],
                     *, seed: Optional[int] = None,
                     extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """RenderJob.params —— 出片时真正喂给供应商的那份参数（也是排查问题的凭据）。"""
    params: Dict[str, Any] = {
        "prompt": shot.prompt_en or "",
        "duration": spec["duration"],
        # 时长是从哪来的：explicit（调用方指定）/ shot（跟随分镜）/ provider_default。
        # 有了它，"为什么这一镜出了 5s 而脚本写 3s" 就不用再猜。
        "duration_source": spec["duration_source"],
        "shot_duration_sec": shot.duration_sec,
        "shot_target_frames": shot.target_frames,
        "resolution": spec["resolution"],
        "ref_images": spec["refs"],
        "refs_source": spec["refs_source"],
    }
    if seed is not None:
        params["seed"] = seed
    if extra:
        params.update(extra)
    return params


def enqueue_shot_render(
    db: Session,
    shot: Shot,
    *,
    cfg,
    media_nodes: Tuple[Dict[str, Any], Dict[str, Any]],
    queue=None,
    kind: str = "video",
    duration: Optional[int] = None,
    resolution: Optional[str] = None,
    provider_id: Optional[str] = None,
    seed: Optional[int] = None,
    ref_image_paths: Optional[Sequence[str]] = None,
    dry_run: bool = False,
    reuse: bool = True,
    extra: Optional[Dict[str, Any]] = None,
) -> Tuple[RenderJob, Optional[Future]]:
    """把一个镜头排进出片队列。

    返回 `(job, future)`：`future` 为 None 表示**没有真正入队**
    （命中了复用，或者只是试算）—— 调用方别再对它 `.result()`。

    **预算闸门在提交之前**（P-5）：超限抛 `budget_mod.BudgetError`，
    由调用方决定翻成 402 还是记成一条失败步骤。
    """
    spec = resolve_render_spec(
        shot, kind, cfg, duration=duration, resolution=resolution,
        provider_id=provider_id, ref_image_paths=ref_image_paths, db=db,
    )
    params = build_job_params(shot, spec, seed=seed, extra=extra)

    # ---- 复用已有产物，避免重复付费 ----
    if reuse and not dry_run:
        prior = (
            db.query(RenderJob)
            .filter(RenderJob.shot_id == shot.id, RenderJob.kind == kind,
                    RenderJob.status == "succeeded", RenderJob.dry_run == 0)
            .order_by(RenderJob.id.desc())
            .first()
        )
        if _reusable_prior(prior, params):
            # 复用也要**回写 shot**：compose 闸门读的是 shot.video_path（shot_ready），
            # 只建 reused job 不回写，会出现"复用成功却报镜头没产物"的死锁
            # （R-24 实测：run 4 五镜全有视频，合成却说 A1/A2 缺产物）。
            if kind == "video":
                shot.video_path = str(prior.output_path)
                shot.status = "rendered"
            j = RenderJob(
                shot_id=shot.id, project_id=shot.project_id, kind=kind,
                provider_id=spec["provider_id"],
                provider_job_id=prior.provider_job_id,
                status="reused", progress=1.0,
                message=f"复用已有产物 {Path(prior.output_path).name}（未重复计费）",
                params=params, output_path=prior.output_path, dry_run=0,
            )
            db.add(j)
            db.commit()
            db.refresh(j)
            return j, None
        if prior is not None:
            # 有旧产物但内容已变：明说不复用的原因，别让人以为白花了钱
            old = prior.params if isinstance(prior.params, dict) else {}
            changed = [k for k in _REUSE_KEYS if old.get(k) != params.get(k)]
            if changed:
                print(f"[render] shot {shot.code or shot.id} 旧产物不复用"
                      f"（已变更：{', '.join(changed)}），重新出片", flush=True)

    # ---- 供应商静态预检：本地就能确定的配置问题，别排一个注定失败的队 ----
    # 放在预算闸门**之前**：配置本身就是坏的，调预算也救不回来，
    # 先报真正的原因（否则用户会去改花费上限，然后仍然失败）。
    nodes = media_nodes[0] if kind == "image" else media_nodes[1]
    node = nodes.get(spec["provider_id"])
    problem: Optional[str]
    if node is None:
        problem = "适配器未构造（type 是否拼错？）"
    else:
        try:
            problem = node.preflight()
        except Exception as e:  # noqa: BLE001 - 预检自己出错也当"不可用"，但要说清是本方异常
            problem = f"预检异常：{type(e).__name__}: {e}"
    if problem:
        raise ProviderNotReady(
            f"媒体供应商 `{spec['provider_id']}` 目前不可用：{problem}。"
            f"可在「设置 → 供应商」里换一个能用的，或按上面的提示修好它。"
        )

    # ---- 花费上限：**提交前预检**（P-5）----
    # 闸门长在提交层是为了让用户**在花钱之前**拿到明确拒绝（402 + 还差多少），
    # 而不是等任务排队跑完才发现超了。队列里还有第二道守卫兜底（防旁路）。
    est_amount, est_desc = estimate_media(
        media_nodes, kind, spec["provider_id"], spec["duration"], spec["resolution"])
    try:
        budget_mod.enforce(
            db, cfg, project_id=shot.project_id, provider_id=spec["provider_id"],
            kind=kind, raw_estimate_cny=est_amount,
        )
    except budget_mod.BudgetError as be:
        budget_mod.record_block(
            db, project_id=shot.project_id, kind=kind,
            provider_id=spec["provider_id"], decision=be.decision,
            note=f"preflight shot#{shot.id}",
        )
        db.commit()
        raise

    j = RenderJob(
        shot_id=shot.id,
        project_id=shot.project_id,
        kind=kind,
        provider_id=spec["provider_id"],
        status="queued",
        params=params,
        dry_run=1 if dry_run else 0,
        message="" if not dry_run else f"[dry-run] {est_desc}",
    )
    db.add(j)
    db.commit()
    db.refresh(j)

    fut = queue.enqueue(j.id) if queue is not None else None
    return j, fut


def job_out(j: RenderJob) -> dict:
    """任务对外表示。提示词不回显给列表接口，避免刷屏。"""
    return {
        "id": j.id,
        "shot_id": j.shot_id,
        "project_id": j.project_id,
        "kind": j.kind,
        "provider_id": j.provider_id,
        "provider_job_id": j.provider_job_id,
        "status": j.status,
        "progress": j.progress,
        "message": j.message,
        "output_path": j.output_path,
        "cost_cny": j.cost_cny,
        "dry_run": bool(j.dry_run),
        "error": j.error,
        "params": {
            k: v for k, v in (j.params or {}).items()
            if k not in ("prompt",)
        },
    }


def wait_for_job(db: Session, job_id: int, *, timeout: float) -> RenderJob:
    """等到任务落到终态（或超时）。超时抛 `TimeoutError`，由调用方判定。

    直接查库而不是等 Future：Future 只活在**入队那次调用**的进程里，
    而编排可能在别的线程/另一次请求里恢复，查库才是唯一事实来源。
    """
    import time

    deadline = time.time() + float(timeout)
    terminal = {"succeeded", "failed", "reused", "dry_run", "cancelled"}
    while True:
        db.expire_all()
        j: Optional[RenderJob] = db.get(RenderJob, job_id)
        if j is None:
            raise RuntimeError(f"任务 {job_id} 记录丢失")
        if j.status in terminal:
            return j
        if time.time() >= deadline:
            raise TimeoutError(
                f"任务 {job_id} 在 {timeout:.0f}s 内未结束（当前 {j.status}）")
        time.sleep(1.0)


def shot_ready(shot: Shot) -> bool:
    """该镜是否已有**磁盘上真实存在**的产物（光有路径不算）。"""
    return bool(shot.video_path) and Path(str(shot.video_path)).exists()


# 复用判据：不只「有过一次成功」，还要**内容没变**。提示词/参考图/时长/分辨率
# 任何一项变了，旧视频描述的就不是当前镜头了 —— 复用它就是"内容对不上"
# （R-24 实测：脚本重新生成后 A1/A2 被灌了改稿前的旧视频）。
_REUSE_KEYS = ("prompt", "ref_images", "duration", "resolution")


def _reusable_prior(prior: Optional[RenderJob], params: Dict[str, Any]) -> bool:
    """prior 成功产物是否仍与当前参数一致（文件在盘 + 关键参数相等）。"""
    if prior is None or not prior.output_path \
            or not Path(str(prior.output_path)).exists():
        return False
    old = prior.params if isinstance(prior.params, dict) else {}
    return all(old.get(k) == params.get(k) for k in _REUSE_KEYS)


def reusable_prior_of(db: Session, shot: Shot, cfg, *,
                      kind: str = "video",
                      provider_id: Optional[str] = None,
                      duration: Optional[int] = None,
                      resolution: Optional[str] = None) -> Optional[RenderJob]:
    """这一镜**现在**能不能复用旧产物（内容判据与入队时**完全同源**）。

    ★R-36：`shot_ready()` 只回答"磁盘上有没有产物"，**不回答"那个产物是不是
    当前这一镜"**。出片流程原先拿它当复用依据直接 `skipped` 掉渲染 —— 于是改了
    提示词 / 参考图之后再出片，旧画面被当成新画面**整镜复用**（不花钱，但内容
    对不上，而且表面上一切正常、没人会知道）。规划预览也用同一套粗判，所以它报的
    "待出片镜头"与实际会发生的事也对不上（预估 ￥0.06、真按内容算应是 ￥0.45）。

    所以凡是"要不要复用"的判断，一律走这里：内部 = `resolve_render_spec` +
    `build_job_params` + `_reusable_prior`，与 `enqueue_shot_render` 同一套调用，
    不会出现"规划说能复用、执行说不能"的双真相。

    返回可复用的 prior；`None` = 内容已变或没有可用产物 → **需要真出片**。
    """
    try:
        spec = resolve_render_spec(shot, kind, cfg, duration=duration,
                                   resolution=resolution,
                                   provider_id=provider_id, db=db)
        params = build_job_params(shot, spec)
    except Exception:  # noqa: BLE001 - 算不出参数就当作"不可复用"：宁可重出，不可张冠李戴
        return None
    prior = (
        db.query(RenderJob)
        .filter(RenderJob.shot_id == shot.id, RenderJob.kind == kind,
                RenderJob.status == "succeeded", RenderJob.dry_run == 0)
        .order_by(RenderJob.id.desc())
        .first()
    )
    return prior if _reusable_prior(prior, params) else None


def list_missing(db: Session, project_id: int) -> List[str]:
    """还缺**磁盘产物**的镜号（给合成闸门用：没有文件就没法合成）。

    ★R-36 边界：本函数只回答"文件在不在"，**不回答"这个产物是不是当前这一镜"**。
    要判断"能不能复用、要不要重出"，一律用 `reusable_prior_of()` —— 两者混用就会
    出现"闸门说齐了、实际画面是改稿前的"（出片流程曾经就是这么错的）。
    """
    rows = (db.query(Shot).filter(Shot.project_id == project_id)
            .order_by(Shot.idx, Shot.id).all())
    return [s.code or f"#{s.id}" for s in rows if not shot_ready(s)]
