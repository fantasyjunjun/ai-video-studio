"""并发渲染队列：提交 → 轮询 → 下载 → 记账 → 下线临时图床。

继承本项目已踩过的坑：

  - **并发受 `max_concurrency` 限制**：一次性把六镜全砸给 GPU 平台，
    换来的是排队超时与账单点，而不是更快的出片。
  - **每个 worker 独立 DB session**：SQLAlchemy Session 非线程安全，共享会串数据。
  - **状态实时落库**：前端轮询 `/api/jobs/{id}` 才有进度可看；不落库等于黑盒。
  - **出片成功就下线临时图床**：参考图不该长期挂在公网（见 ImageHostLease）。
  - **异常信息必须脱敏**：桓岚令牌/密钥可能被拼进 HTTP 错误串，入库前先替换。
  - **out_dir 带版本号**：重复渲染同一镜不覆盖旧产物，方便对比选版。
"""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import update as sql_update

from ..db import fts as fts_mod
from ..db.models import Asset, CostRecord, ImageHostLease, Project, RenderJob, Shot
from ..providers.base import JobHandle, MediaProvider, mask_secret
from ..providers.http_safety import BillingRiskError
from ..providers.media_registry import build_image_providers, build_video_providers
from . import budget as budget_mod
from . import task_ledger as ledger
from .image_host import ImageHost, StaticDirImageHost, ensure_public_urls


def _sha256(path: Path, length: int = 32) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:length]


class RenderQueue:
    def __init__(
        self,
        session_factory,
        image_providers: Dict[str, MediaProvider],
        video_providers: Dict[str, MediaProvider],
        out_dir: str | Path,
        *,
        max_concurrency: int = 3,
        poll_interval: float = 5.0,
        poll_timeout: float = 900.0,
        image_host: Optional[ImageHost] = None,
        budget_cfg=None,
    ) -> None:
        self.session_factory = session_factory
        self.image_providers = image_providers
        self.video_providers = video_providers
        self.out_dir = Path(out_dir)
        self.max_concurrency = max(1, int(max_concurrency))
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.image_host = image_host
        # 花费上限（P-5）。这里再守一道，是因为**队列不只是 API 进来的** ——
        # 批量编排（BatchOrchestrator）也直接往这里丢 job，只靠路由层预检会漏。
        self.budget_cfg = budget_cfg

        self._executor = ThreadPoolExecutor(
            max_workers=self.max_concurrency, thread_name_prefix="render"
        )
        self._futures: Dict[int, Future] = {}

    # ---------- 构造便利方法 ----------
    @classmethod
    def from_config(cls, cfg, session_factory, out_dir: str | Path,
                    image_host: Optional[ImageHost] = None) -> "RenderQueue":
        return cls(
            session_factory,
            build_image_providers(cfg),
            build_video_providers(cfg),
            out_dir,
            max_concurrency=cfg.active_video_config().max_concurrency,
            poll_interval=float(cfg.active_video_config().poll_interval),
            poll_timeout=float(cfg.active_video_config().timeout),
            image_host=image_host,
            budget_cfg=cfg,
        )

    # ---------- 提交 ----------
    def enqueue(self, job_id: int) -> Future:
        """把一条已落库的 RenderJob 丢进线程池。"""
        fut = self._executor.submit(self._run, job_id)
        self._futures[job_id] = fut
        return fut

    def wait_all(self, timeout: Optional[float] = None) -> Dict[int, str]:
        done, _ = self._wait(timeout)
        return done

    def _wait(self, timeout: Optional[float] = None) -> tuple[Dict[int, str], bool]:
        deadline = time.time() + (timeout or float("inf"))
        results: Dict[int, str] = {}
        complete = True
        for jid, fut in self._futures.items():
            remaining = max(0.0, deadline - time.time())
            try:
                fut.result(timeout=remaining if timeout else None)
                results[jid] = "done"
            except TimeoutError:
                complete = False
                results[jid] = "timeout"
            except Exception as e:  # noqa: BLE001
                results[jid] = f"error: {e}"
        return results, complete

    # ---------- 单任务主体 ----------
    def _lookup(self, s, kind: str, provider_id: str) -> MediaProvider:
        pool = self.video_providers if kind == "video" else self.image_providers
        if provider_id not in pool:
            raise KeyError(
                f"{kind} 供应商 '{provider_id}' 未登记。可用: {sorted(pool)}"
            )
        return pool[provider_id]

    @staticmethod
    def _estimate(provider: MediaProvider, job: RenderJob) -> tuple[float, str]:
        """出片前的费用预估 (金额, 说明)。供应商没实现 `estimate_cost` 时返回 0.0。

        返回 0.0 意味着"估不出来"而不是"免费"—— 预算熔断在这种情况下
        只能靠已花总额判断，不能靠本次预估，这一点在 reason 里会体现。
        """
        params = job.params or {}
        fn = getattr(provider, "estimate_cost", None)
        if not callable(fn):
            return 0.0, "该供应商未提供费用预估"
        try:
            amt, desc = fn(params.get("duration"), params.get("resolution"))
            return float(amt or 0.0), str(desc)
        except Exception as e:  # noqa: BLE001 - 预估失败不该挡住出片判定
            return 0.0, f"费用预估失败：{type(e).__name__}"

    def _sanitize(self, msg: str, provider: MediaProvider) -> str:
        """把可能泄漏的令牌换成脱敏串后再入库/回显。"""
        tok = None
        cfg = getattr(provider, "cfg", None)
        resolve = getattr(cfg, "resolve_token", None)
        if callable(resolve):
            try:
                tok = resolve()
            except Exception:  # noqa: BLE001
                tok = None
        if tok and tok in msg:
            msg = msg.replace(tok, mask_secret(tok))
        return msg[:2000]

    def _run(self, job_id: int) -> None:
        s = self.session_factory()
        host = self.image_host
        published: List[str] = []
        provider: Optional[MediaProvider] = None
        task = None
        ns = ""
        try:
            job: Optional[RenderJob] = s.get(RenderJob, job_id)
            if job is None:
                return
            params = job.params or {}
            provider = self._lookup(s, job.kind, job.provider_id)
            # 图床目录按 job 隔离：否则先完工的 job 会把还在排队的 job 的参考图一起下线，
            # 平台拿到的是已被清空的图片。代价只是多几 MB 拷贝。
            ns = f"p{job.project_id or 'misc'}/j{job.id}"

            # ---- dry-run：只估费用，不出片（省钱、也避免误花）
            if job.dry_run:
                _, desc = self._estimate(provider, job)
                job.status = "dry_run"
                job.message = f"[dry-run] {desc}"
                job.progress = 1.0
                s.commit()
                return

            # ---- 花费上限：**提交前**最后一道守卫（P-5）----
            # 路由层已预检过一次，这里是第二道：批量编排等旁路会直接进队列，
            # 只有守在这里才等于"所有出片请求都过闸"。注意它必须在发请求之前 ——
            # 出片是提交即计费，拦晚了等于没拦。
            est_amount, est_desc = self._estimate(provider, job)
            if self.budget_cfg is not None:
                decision = budget_mod.check(
                    s, self.budget_cfg, project_id=job.project_id,
                    provider_id=job.provider_id, kind=job.kind,
                    raw_estimate_cny=est_amount,
                )
                if decision.blocking:
                    budget_mod.record_block(
                        s, project_id=job.project_id, kind=job.kind,
                        provider_id=job.provider_id, decision=decision,
                        note=f"queue guard job#{job_id}",
                    )
                    job.status = "failed"
                    job.message = "已被花费上限拦下（未提交、未计费）"
                    job.error = f"[花费上限] {decision.reason}"[:2000]
                    s.commit()
                    return
                if decision.warned:
                    job.message = f"[花费告警] {decision.reason}"

            # ---- 参考图：本地路径 → 公网 URL ----
            refs = list(params.get("ref_images") or [])
            urls, lease = ensure_public_urls(refs, host, namespace=ns)
            if lease:
                published = lease[1]
                s.add(ImageHostLease(
                    host=type(host).__name__,
                    root_url=lease[0],
                    serve_root=str(getattr(host, "serve_root", "")),
                    project_id=job.project_id,
                    published=published,
                    namespace=ns,
                    note=f"auto-published for job {job_id}",
                ))
                s.commit()

            # ---- 提交（计费、非幂等）----
            # 台账必须**先于**请求落盘：崩溃时至少留下"我们发过这一单"的记录，
            # 而不是钱花了、provider_job_id 还是空、无从对账。
            task = ledger.begin_submit(s, job=job, provider_id=job.provider_id,
                                       estimated_cny=est_amount)
            s.commit()
            try:
                handle = provider.submit(params.get("prompt") or "", ref_images=urls, **{
                    k: v for k, v in params.items()
                    if k not in ("prompt", "ref_images")
                })
            except Exception as e:  # noqa: BLE001 - 区分"可能已计费"与"明确失败"
                if isinstance(e, BillingRiskError):
                    # 结果未知：厂商可能已建任务并计费 —— 标 unknown，留给用户对账
                    ledger.mark_unknown(s, task, str(e)[:1000])
                    job.status = "failed"
                    job.error = self._sanitize(f"{e}", provider)
                    s.commit()
                else:
                    ledger.mark_terminal(s, task, "failed",
                                         message=self._sanitize(f"{type(e).__name__}: {e}",
                                                                provider))
                    s.commit()
                raise

            ledger.mark_submitted(s, task, handle.job_id)
            job.provider_job_id = handle.job_id
            job.status = "running"
            job.progress = 0.05
            job.message = "已提交"
            s.commit()

            # ---- 轮询 + 下载 + 记账 ----
            self._finish(job, s, provider, handle, host=host,
                         published=published, namespace=ns)

            # ---- 台账收尾 ----
            if job.status == "succeeded":
                ledger.mark_terminal(s, task, "succeeded",
                                     cost_cny=float(job.cost_cny or 0.0),
                                     message=job.message or "已保存")
            else:
                ledger.mark_terminal(s, task, "failed",
                                     message=(job.error or job.message or "出片失败"))
            s.commit()

        except Exception as e:  # noqa: BLE001 - 任何异常都要落到 job.error 而不是炸掉线程
            msg = f"{type(e).__name__}: {e}"
            if isinstance(provider, MediaProvider):
                msg = self._sanitize(msg, provider)
            try:
                job2 = s.get(RenderJob, job_id)
                if job2 is not None and job2.status != "succeeded":
                    job2.status = "failed"
                    job2.error = msg[:2000]
                    if task is not None and task.status not in ("succeeded", "failed",
                                                                "unknown"):
                        ledger.mark_terminal(s, task, "failed", message=msg)
                    s.commit()
            except Exception:  # noqa: BLE001
                pass
            # 失败也要收图床，别让参考图挂着
            self._maybe_offline(s, job_id, host, published, namespace=ns)
        finally:
            s.close()

    # ---------- 轮询 / 下载 / 记账（提交成功后与"按 taskId 恢复"共用） ----------
    def _finish(self, job: RenderJob, s, provider: MediaProvider, handle: JobHandle,
                *, host: Optional[ImageHost] = None,
                published: Optional[List[str]] = None, namespace: str = "") -> None:
        published = published or []
        params = job.params or {}

        # ---- 轮询 ----
        deadline = time.time() + self.poll_timeout
        status = None
        while time.time() < deadline:
            status = provider.poll(handle)
            if status.state in ("succeeded", "failed"):
                # **不要在这里就把 succeeded 写进库**。下载与落盘还在后面
                # （`output_path` 要等 `download()` 之后才有），先写就会留下
                # "状态已完成、产物路径还是 None" 的中间态。轮询方（前端出图
                # 后要拿 output_path 去登记主播图）正好停在 succeeded 那一刻读，
                # 就会拿不到路径而报失败 —— 钱花了，用户看到的却是一次失败。
                # 终态统一由下面下载完成后再落库。
                break
            job.status = status.state
            job.progress = max(0.05, min(status.progress, 0.99))
            job.message = (status.message or "")[:500]
            s.commit()
            time.sleep(self.poll_interval)

        if status is None or status.state != "succeeded":
            job.status = "failed"
            job.error = ((status.message if status else "轮询超时") or "")[:2000]
            s.commit()
            self._maybe_offline(s, job.id, host, published, namespace=namespace)
            return

        # ---- 下载 ----
        shot_code = ""
        if job.shot_id:
            sh = s.get(Shot, job.shot_id)
            shot_code = (sh.code if sh else f"shot{job.shot_id}")
        dest_dir = self.out_dir / f"p{job.project_id or 'misc'}" / (shot_code or "misc")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{job.kind}-{job.provider_job_id or job.id}"

        out = provider.download(handle, dest)
        job.output_path = str(out)
        job.checksum = _sha256(Path(out))

        # ---- 计费 ----
        amount = 0.0
        est = getattr(provider, "estimate_cost", None)
        if callable(est) and job.kind == "video":
            amt, _ = est(params.get("duration"), params.get("resolution"))
            amount = float(amt)
        job.cost_cny = amount

        # ---- 回写 shot / asset / cost ----
        if job.shot_id:
            sh = s.get(Shot, job.shot_id)
            if sh is not None and job.kind == "video":
                sh.video_path = str(out)
                sh.status = "rendered"
        asset = Asset(kind=job.kind, path=str(out), checksum=job.checksum,
                      project_id=job.project_id,
                      meta={"job_id": job.id, "provider": job.provider_id,
                            "provider_job_id": job.provider_job_id})
        s.add(asset)
        s.add(CostRecord(project_id=job.project_id, kind=job.kind,
                         provider_id=job.provider_id, amount_cny=amount,
                         quantity=float(params.get("duration") or 1),
                         meta={"params": {k: v for k, v in params.items()
                                          if k != "prompt"}}))
        # ---- 成本累加必须走 SQL 层原子自增 ----
        # 反例（已实测）：`proj.cost_cny = proj.cost_cny + amount` 在多 worker 并发下
        # 是"读-改-写"，三镜并行时总账只记到一份 —— 典型的丢失更新。
        if job.project_id:
            s.execute(
                sql_update(Project)
                .where(Project.id == job.project_id)
                .values(cost_cny=Project.cost_cny + float(amount))
            )

        job.status = "succeeded"
        job.progress = 1.0
        job.message = f"已保存 {Path(out).name}"
        s.commit()

        # ---- 索引自维护 ----
        # 素材检索页与 `POST /assets/reindex` 已于 2026-09-29 移除，不再有
        # "人工重建索引"这一步可补录 —— 所以必须在登记处就写索引，
        # 否则 `GET /api/assets?q=`（MCP list_assets 在用）永远查不到新产物。
        # 索引失败**不该**影响出片结果：产物已落盘、台账已写完。
        try:
            fts_mod.index_asset(s.get_bind(), asset)
        except Exception:  # noqa: BLE001
            pass

        # ---- 出片成功即下线临时图床 ----
        self._maybe_offline(s, job.id, host, published, namespace=namespace)

    # ---------- 按厂商 taskId 恢复（P-2） ----------
    def resume_task(self, task_id: int) -> Dict[str, Any]:
        """拿台账里的 `provider_task_id` 重新接上轮询 —— **不重新提交，不重复计费**。

        轮询与下载都不计费，"提交即计费"那一步早已发生；恢复只是把这条命捡回来。
        仅对已有 task_id 且未完成的任务有意义；`submitting`（无 task_id）无法恢复，
        只能人工对账。
        """
        from ..db.models import RenderTask  # 局部导入，避免与 models 顶层循环

        s = self.session_factory()
        try:
            t: Optional[RenderTask] = s.get(RenderTask, task_id)
            if t is None:
                return {"ok": False, "error": f"台账 {task_id} 不存在"}
            if not t.provider_task_id:
                return {"ok": False,
                        "error": "该任务没有厂商 task_id（可能从未成功提交），无法按 ID 恢复；"
                                 "请到平台按时间核对"}
            if t.status == "succeeded":
                return {"ok": True, "status": "succeeded", "note": "已完成，无需恢复"}
            job: Optional[RenderJob] = s.get(RenderJob, t.job_id) if t.job_id else None
            if job is None:
                return {"ok": False, "error": "台账关联的渲染任务不存在"}
            if job.status == "succeeded" and job.output_path:
                ledger.mark_terminal(s, t, "succeeded", cost_cny=float(job.cost_cny or 0.0),
                                     message="job 已完成（无需恢复）")
                s.commit()
                return {"ok": True, "status": "succeeded", "note": "job 已完成"}

            provider = self._lookup(s, job.kind, job.provider_id)
            handle = JobHandle(provider_id=job.provider_id,
                               job_id=t.provider_task_id, raw={})
            ledger.mark_running(s, t, f"恢复中（task {t.provider_task_id}）")
            job.status = "running"
            job.message = "按 taskId 恢复轮询"
            s.commit()

            try:
                self._finish(job, s, provider, handle, host=self.image_host,
                             published=[], namespace=f"p{job.project_id or 'misc'}/j{job.id}")
            except Exception as e:  # noqa: BLE001 - 恢复失败也要如实落台账
                msg = self._sanitize(f"{type(e).__name__}: {e}", provider)
                job.status = "failed"
                job.error = msg[:2000]
                ledger.mark_terminal(s, t, "failed", message=msg)
                s.commit()
                return {"ok": False, "status": "failed", "error": msg}

            if job.status == "succeeded":
                ledger.mark_terminal(s, t, "succeeded",
                                     cost_cny=float(job.cost_cny or 0.0),
                                     message="按 taskId 恢复并下载成功")
                s.commit()
                return {"ok": True, "status": "succeeded",
                        "output_path": job.output_path}
            ledger.mark_terminal(s, t, "failed",
                                 message=(job.error or job.message or "恢复后仍失败"))
            s.commit()
            return {"ok": False, "status": "failed",
                    "error": job.error or job.message}
        finally:
            s.close()

    def _maybe_offline(self, s, job_id: int, host: Optional[ImageHost],
                       published: List[str], namespace: str = "") -> None:
        """关闭**本 job 自己那一份**图床租约。

        这里**绝不删文件**（本环境的安全钩子会在 os.remove 处中止进程），
        一律走截断覆盖；namespace 必须取自租约记录本身，否则会下线错目录 ——
        尤其在多 job 并发、各自目录隔离的情况下。
        """
        if not host or not published or not namespace:
            return
        # 按 namespace 精确匹配，而不是按文件名 —— 并发 job 的参考图常同名
        # （都叫 m02_front.jpg），按名字匹配会误关掉别人还在用的租约。
        leases = s.query(ImageHostLease).filter(
            ImageHostLease.is_offline == 0,
            ImageHostLease.namespace == namespace,
            ImageHostLease.published.isnot(None),
        ).all()
        for l in leases:
            removed: List[str] = []
            try:
                removed = host.unpublish(l.published or [], namespace=l.namespace or "")
            except Exception:  # noqa: BLE001
                removed = []
            l.is_offline = 1
            l.offline_at = datetime.utcnow()
            note = f" | unpublished-after-job-{job_id}: {removed}"
            # 上传型图床没有删除 API：这里必须**说实话**，否则"已下线"会被读成"已删除"
            if not getattr(host, "supports_delete", True):
                note += (f" | 该图床无法删除，文件由服务方按时效过期"
                         f"（{getattr(host, 'expiry_hint', '') or '时效未知'}）")
            l.note = (l.note or "") + note
        s.commit()

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
