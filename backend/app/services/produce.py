"""一键出片编排：脚本 → 落镜 → 逐镜出片 → 15s 成片。

它做的事只有**串联**，不重复实现任何一步：

    剧本解析落镜   → services/shot_import.py
    单镜出片       → services/render_core.py
    成片（TTS/混音）→ services/compose.py

为什么要有这层
--------------
产品形态是「脚本生成后直接一键出成片」，中间没有人再点第二次。但这条链路
要花 N 笔钱、耗时可达十几分钟，所以必须有：

  - **花钱前的规划**（`plan()`）：会出几镜、每镜几秒、预估多少钱、缺什么。
  - **可轮询的运行记录**（`ProduceRun`）：现在跑到哪一步、哪一镜挂了。
  - **断点续跑**：已有产物的镜头**不重出**（`reuse=True`），重试只补缺的那几镜。

失败策略
--------
某一镜出片失败时**继续跑其余镜头**，而不是立刻中止。理由：中止并不能省下钱
（那几镜迟早要出），但会把已经成功的部分白白丢掉，重试时全部重来 —— 反而更贵。
失败镜头在最终报告里列清楚，重试只补它们。

`run()` 只由后台线程调用，**自己开 session**（不持有调用方的 session）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from ..db.models import ProduceRun, Project, RenderJob, Shot
from ..pipeline.lint import DEFAULT_MIN_SCORE
from ..pipeline.script_import import parse_post_plan, parse_script
from . import budget as budget_mod
from .compose import ComposeBlocked, ComposeRequest, compose_final_path, run_compose
from .compose import voice_for_lang
from .render_core import (enqueue_shot_render, estimate_media,
                          reusable_prior_of, shot_video_duration, wait_for_job)
from .scripts import latest_script, read_script, save_script
from .shot_import import import_script_to_project, shot_preview


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def recover_orphan_runs(db: Session) -> int:
    """启动自愈：把上一进程留下的 running/queued 出片 run 标记为 failed。

    编排器是**进程内后台线程**——本进程刚启动时不可能有自己的在途 run，
    所以此刻所有非终态（queued/running）的 ProduceRun 都是**上一进程的孤儿**
    （进程被 kill / 重启时内存线程随之消失，但库里的状态没人收尸）。不收尸
    的后果是「同项目只允许一条出片」护栏把该项目永久锁死：没有取消端点，
    重试接口又拒绝 running 状态的 run。

    标 failed 而不是删除：已成功镜头的产物都在，`retry` 会按「复用已有产物，
    只补缺失镜」续跑，钱不会白花。
    """
    orphans = (db.query(ProduceRun)
               .filter(ProduceRun.status.in_(["queued", "running"])).all())
    for r in orphans:
        old = r.status
        r.status = "failed"
        r.message = (f"进程重启导致中断（启动自愈标记）。原状态 {old}；"
                     "已完成的镜头产物已保留，点「重试」只补缺失的镜头。")
    if orphans:
        db.commit()
    return len(orphans)


@dataclass
class ProduceRequest:
    """一键出片的入参。`script` 为空表示"用项目里已有的分镜"。"""

    script: Optional[str] = None
    source: str = "agent"           # agent / paste —— 只用于脚本素材溯源
    title: Optional[str] = None
    min_score: int = DEFAULT_MIN_SCORE
    language: Optional[str] = None  # 不传则用项目语言
    allow_silent: bool = False
    force: bool = False             # 放行合规高危（默认拦）
    fps: float = 24.0
    out_name: str = "final.mp4"
    # 单镜等待上限。出片是分钟级，5 镜串行可能要十几分钟
    render_timeout_sec: float = 1800.0
    # 已有产物的镜头是否复用（默认复用 = 不重复花钱）。关掉 = 全部重出。
    reuse: bool = True


class ProduceOrchestrator:
    def __init__(self, session_factory, queue, post, cfg,
                 media_nodes: Callable[[], Tuple[Dict[str, Any], Dict[str, Any]]],
                 work_root: Path) -> None:
        self.session_factory = session_factory
        self.queue = queue
        self.post = post
        self.cfg = cfg
        self.media_nodes = media_nodes
        self.work_root = Path(work_root)

    # ------------------------------------------------------------- 规划

    def plan(self, db: Session, pid: int, req: ProduceRequest) -> Dict[str, Any]:
        """**不花钱、不联网、不落库**：给出"这次出片会发生什么"。"""
        proj: Optional[Project] = db.get(Project, pid)
        if proj is None:
            raise ValueError(f"项目不存在: {pid}")

        warnings: List[str] = []
        parsed_preview: List[dict] = []
        rows: List[Dict[str, Any]] = []
        sfx_cues: List[Dict[str, Any]] = []
        rows_source = "none"
        script_from_request = bool((req.script or "").strip())

        # 没传脚本、库里也还没落镜，但存在「最新脚本素材」：预览就按"自动落镜"处理，
        # run() 会真正调 import_script_to_project 落镜。这样"脚本生成 → 一键出片"
        # 中间不必再有人点一次"导入分镜"（产品形态要求脚本自动落镜）。
        auto_from_asset = False
        asset_script = ""
        if not script_from_request:
            have_shots = db.query(Shot).filter(Shot.project_id == pid).first() is not None
            if not have_shots:
                asset = latest_script(db, pid)
                if asset is not None:
                    asset_script = read_script(asset)
                    if asset_script.strip():
                        auto_from_asset = True

        if script_from_request or auto_from_asset:
            text = req.script if script_from_request else asset_script
            parsed, pw = parse_script(text)
            warnings.extend(pw)
            parsed_preview = [shot_preview(p, req.min_score) for p in parsed]
            if not parsed:
                warnings.append("脚本没解析出任何镜头，无法出片")
            # `parse_post_plan` 返回的是 ParsedPostPlan 数据类，**不是二元组**
            post_plan = parse_post_plan(text)
            rows = list(post_plan.vo_rows or [])
            sfx_cues = list(post_plan.sfx_cues or [])
            warnings.extend(post_plan.warnings or [])
            rows_source = "script" if rows else "none"
        else:
            asset = latest_script(db, pid)
            meta = dict(asset.meta or {}) if asset is not None else {}
            rows = list(meta.get("vo_rows") or [])
            sfx_cues = list(meta.get("sfx_cues") or [])
            rows_source = "script" if rows else "none"
            if asset is None:
                warnings.append("项目里还没有脚本素材 —— 先在弹窗里生成脚本或粘贴导入")

        # ---- 镜头与齐备性 ----
        if script_from_request or auto_from_asset:
            # 脚本还没落库，用解析结果估算规格（真实落库在 run() 里做）
            total_frames = sum(int(round(p["duration_sec"] * req.fps))
                               for p in parsed_preview)
            shots_total = len(parsed_preview)
            # 同名镜次可能**已经出过片**（重复点了一次出片，或改了文案又重跑）。
            # ★R-36：算"复用"必须用**内容判据**（reusable_prior_of），不能只看
            # "磁盘上有产物" —— 后者会让规划把"改了提示词的镜"也报成可复用，
            # 预估花费随之虚低（实测：真按内容算 ￥0.45，粗判只报 ￥0.06）。
            # 这份规划是拿去对账的，不能虚报。
            existing = {s.code: s for s in
                        db.query(Shot).filter(Shot.project_id == pid).all()
                        if s.code}
            ready_codes = {p["code"] for p in parsed_preview
                           if p["code"] in existing
                           and reusable_prior_of(db, existing[p["code"]],
                                                 self.cfg) is not None}
            to_render: List[Dict[str, Any]] = [
                {"code": p["code"], "duration_sec": p["duration_sec"],
                 "request_sec": self._request_sec(p["duration_sec"], req.fps)}
                for p in parsed_preview if p["code"] not in ready_codes
            ]
            missing = [p["code"] for p in parsed_preview
                       if p["code"] not in ready_codes]
        else:
            shots = (db.query(Shot).filter(Shot.project_id == pid)
                     .order_by(Shot.idx, Shot.id).all())
            shots_total = len(shots)
            total_frames = sum(int(s.target_frames
                                   or round((s.duration_sec or 0.0) * req.fps))
                               for s in shots)
            # ★R-36：同样用内容判据 —— 见上面 ready_codes 处的说明。
            reusable = {s.id for s in shots
                        if reusable_prior_of(db, s, self.cfg) is not None}
            missing = [s.code or f"#{s.id}" for s in shots if s.id not in reusable]
            to_render = [
                {"code": s.code, "duration_sec": s.duration_sec,
                 "request_sec": shot_video_duration(s, req.fps)
                 or self.cfg.active_video_config().default_duration}
                for s in shots if s.id not in reusable
            ]
        shots_ready = shots_total - len(to_render)

        if shots_total == 0:
            warnings.append("没有镜头可出 —— 先给项目生成或导入脚本")

        # ---- 预估花费：逐镜按实际请求时长问供应商价目表 ----
        resolution = self.cfg.active_video_config().default_resolution
        est_total = 0.0
        est_desc = ""
        for item in to_render:
            amt, desc = estimate_media(self.media_nodes(), "video",
                                       self.cfg.video.active,
                                       item["request_sec"], resolution)
            est_total += float(amt or 0.0)
            est_desc = desc or est_desc
        rows_ready = sum(1 for r in rows if str(r.get("text", "")).strip())
        if not rows_ready:
            warnings.append(
                "脚本里没有念白稿 —— 成片会缺旁白。默认会被拒绝；"
                "确实要做无旁白版本时勾选「允许无念白」")
        if req.reuse and shots_ready:
            if to_render:
                warnings.append(
                    f"{shots_ready} 个镜头内容未变会被复用，不重复计费")
            else:
                warnings.append(
                    "所有镜头都已有产物 —— 本次只会重新合成成片，"
                    "不会产生出片花费")

        return {
            "project_id": pid,
            # 入参快照（**不含脚本正文**）：重试要靠它还原 fps / 时长上限 / 合规放行
            "request": {
                "source": req.source, "title": req.title,
                "min_score": req.min_score, "language": req.language,
                "allow_silent": req.allow_silent, "force": req.force,
                "fps": req.fps, "out_name": req.out_name,
                "render_timeout_sec": req.render_timeout_sec, "reuse": req.reuse,
            },
            "script_from_request": script_from_request,
            "script_preview": parsed_preview,
            "shots_total": shots_total,
            "shots_ready": shots_total - len(to_render),
            "to_render": to_render,
            "missing": missing,
            "fps": req.fps,
            "total_frames": total_frames,
            "total_sec": round(total_frames / req.fps, 3),
            "rows": rows,
            "rows_source": rows_source,
            "vo_row_count": rows_ready,
            "sfx_cues": sfx_cues,
            "language": req.language or proj.language,
            "voice": voice_for_lang(req.language or proj.language),
            "render_calls": len(to_render),
            "estimate_cny": round(est_total, 4),
            "estimate_detail": est_desc,
            "warnings": warnings,
        }

    def _request_sec(self, duration_sec: Any, fps: float) -> int:
        try:
            sec = float(duration_sec or 0.0)
        except (TypeError, ValueError):
            sec = 0.0
        return max(1, min(15, int(math.ceil(sec - 1e-6)))) if sec else \
            self.cfg.active_video_config().default_duration

    # ------------------------------------------------------------- 执行

    def run(self, run_id: int, req: ProduceRequest) -> Dict[str, Any]:
        steps: List[Dict[str, Any]] = []
        report: Dict[str, Any] = {"run_id": run_id, "shots": [], "errors": []}

        def flush(**patch: Any) -> None:
            s = self.session_factory()
            try:
                r = s.get(ProduceRun, run_id)
                if r is None:
                    return
                r.steps = list(steps)
                for k, v in patch.items():
                    setattr(r, k, v)
                s.commit()
            finally:
                s.close()

        def step(key: str, label: str, status: str, message: str = "") -> None:
            steps.append({"key": key, "label": label, "status": status,
                          "message": message[:500], "at": _now()})
            flush()

        flush(status="running", stage="script", message="开始")

        # ---------- ① 脚本落镜 ----------
        pid = self._project_of(run_id)
        if pid is None:
            step("script", "脚本落镜", "failed", "运行记录丢了项目号")
            flush(status="failed", stage="done", message="运行记录异常", report=report)
            return report

        plan: Dict[str, Any] = {}
        # 没传脚本时：若库里还没有镜头、却有"最新脚本素材"，自动落镜
        # （产品形态是"脚本生成 → 一键出片"，中间不再需要人点"导入分镜"）。
        # 已有镜头则直接复用，不覆盖人工改动。
        script_to_import = (req.script or "").strip()
        script_source = req.source
        auto_asset_id = None
        if not script_to_import:
            s0 = self.session_factory()
            try:
                have_shots = s0.query(Shot).filter(Shot.project_id == pid).first() is not None
                if not have_shots:
                    asset = latest_script(s0, pid)
                    if asset is not None:
                        txt = read_script(asset)
                        if txt.strip():
                            script_to_import = txt
                            script_source = (asset.meta or {}).get("source") or "import"
                            auto_asset_id = asset.id
            finally:
                s0.close()

        if script_to_import:
            s = self.session_factory()
            try:
                out = import_script_to_project(
                    s, pid, script=script_to_import, min_score=req.min_score,
                    source=script_source, save_asset=False, dry_run=False,
                )
                if not out.imported:
                    step("script", "脚本落镜", "failed",
                         "；".join(out.warnings) or "没解析出镜头")
                    flush(status="failed", stage="done",
                          message="脚本解析失败", report=report)
                    return report
                report["script"] = {"imported": out.imported,
                                    "created": out.created, "updated": out.updated,
                                    "script_asset_id": auto_asset_id or out.script_asset_id}
                flush(script_asset_id=report["script"]["script_asset_id"])
            finally:
                s.close()
            step("script", f"脚本落镜（{report['script']['imported']} 镜）", "succeeded",
                 f"新增 {report['script']['created']} / 覆盖 {report['script']['updated']}")
        else:
            step("script", "使用项目已有分镜", "skipped", "未传脚本")

        # ---------- 规划（真正的落库后版本）----------
        s = self.session_factory()
        try:
            plan = self.plan(s, pid, req)
            flush(plan=plan, cost_estimate_cny=float(plan.get("estimate_cny") or 0.0))
        finally:
            s.close()

        # ---------- ② 逐镜出片 ----------
        flush(stage="render", message="逐镜出片")
        shots = self._shots(pid)
        rendered = 0
        for shot in shots:
            label = f"出片 {shot.code or shot.id}"
            # ★R-36：这里**不再**用 `shot_ready` 前置跳过。它只回答"磁盘上有没有
            # 产物"，不回答"那个产物是不是当前这一镜" —— 拿它当复用依据，等于
            # 改了提示词/参考图之后出片时，旧画面被整镜复用（不花钱，但内容对不上，
            # 且表面上一切正常）。复用与否一律交给 `_render_one` →
            # `enqueue_shot_render` → `_reusable_prior`（内容判据），口径与规划一致。
            ok, msg, job = self._render_one(pid, shot.id, req)
            if ok:
                reused = bool(job is not None and job.status == "reused")
                if not reused:
                    rendered += 1
                report["shots"].append({"code": shot.code,
                                        "status": "reused" if reused else "succeeded",
                                        "job_id": job.id if job else None,
                                        "output_path": job.output_path if job else None,
                                        "cost_cny": float(job.cost_cny or 0.0) if job else 0.0})
                step(label, label, "skipped" if reused else "succeeded",
                     ("已有产物且内容未变，复用（未重复计费）" if reused
                      else f"{msg} · job {job.id if job else '?'}"))
            else:
                report["shots"].append({"code": shot.code, "status": "failed",
                                        "error": msg})
                report["errors"].append(f"[{shot.code}] {msg}")
                step(label, label, "failed", msg)
            # 单镜失败也继续跑其余镜头：中止省不下钱，却会丢掉已成功的部分
        self._add_cost(run_id, report)

        missing = [x["code"] for x in report["shots"] if x["status"] == "failed"]
        if missing:
            step("render", "逐镜出片", "failed",
                 f"{len(missing)} 镜未出片：" + "、".join(str(c) for c in missing))

        # ---------- ③ 成片 ----------
        flush(stage="compose", message="合成 15s 成片")
        creq = ComposeRequest(
            fps=req.fps, out_name=req.out_name, allow_silent=req.allow_silent,
            force=req.force, voice=req.language and voice_for_lang(req.language) or None,
        )
        s = self.session_factory()
        try:
            res = run_compose(s, self.post, pid, creq)
            report["compose"] = res.get("compose") or {}
            final_path = compose_final_path(res)
            report["final_path"] = final_path
            report["final"] = self._final_meta(res)
            if final_path:
                step("compose", "合成 15s 成片", "succeeded", final_path)
            else:
                step("compose", "合成 15s 成片", "failed", "没有混音产物")
                report["errors"].append("合成阶段没有产出成片")
        except ComposeBlocked as e:
            report["errors"].append(e.message)
            report["compose_blocked"] = {"status": e.status, "message": e.message}
            step("compose", "合成 15s 成片", "failed", e.message)
        except Exception as e:  # noqa: BLE001 - 后台线程的异常不能静默
            report["errors"].append(f"{type(e).__name__}: {e}")
            step("compose", "合成 15s 成片", "failed", f"{type(e).__name__}: {e}")
        finally:
            s.close()

        ok = bool(report.get("final_path")) and not report["errors"]
        status = "done" if ok else "failed"
        msg = ("成片已产出" if report.get("final_path") else "未产出成片")
        if report["errors"]:
            msg += f"；{len(report['errors'])} 个问题"
        flush(status=status, stage="done", message=msg, report=report)
        return report

    # ------------------------------------------------------------- 内部

    def _project_of(self, run_id: int) -> Optional[int]:
        s = self.session_factory()
        try:
            r = s.get(ProduceRun, run_id)
            return r.project_id if r else None
        finally:
            s.close()

    def _shots(self, pid: int) -> List[Shot]:
        s = self.session_factory()
        try:
            return (s.query(Shot).filter(Shot.project_id == pid)
                    .order_by(Shot.idx, Shot.id).all())
        finally:
            s.close()

    def _render_one(self, pid: int, shot_id: int,
                    req: ProduceRequest) -> Tuple[bool, str, Optional[RenderJob]]:
        """出一镜。异常一律翻成可读结果，**不向外抛**（后台线程里抛出去就静默了）。"""
        s = self.session_factory()
        try:
            shot = s.get(Shot, shot_id)
            if shot is None:
                return False, "镜头记录丢失", None
            try:
                job, _fut = enqueue_shot_render(
                    s, shot, cfg=self.cfg, media_nodes=self.media_nodes(),
                    queue=self.queue, kind="video", reuse=req.reuse,
                )
            except budget_mod.BudgetError as be:
                return False, f"花费上限拦截：{be.decision.reason}", None
            except Exception as e:  # noqa: BLE001
                return False, f"提交失败 {type(e).__name__}: {e}", None

            if job.status == "reused":
                return True, "复用已有产物", job
            try:
                done = wait_for_job(s, job.id, timeout=req.render_timeout_sec)
            except TimeoutError as e:
                return False, str(e), job
            if done.status == "succeeded" and done.output_path:
                return True, f"{done.provider_id}", done
            return False, (done.error or done.message or "出片失败")[:300], done
        finally:
            s.close()

    def _final_meta(self, res: Dict[str, Any]) -> Dict[str, Any]:
        for s in res.get("steps") or []:
            if s.get("step") == "mix" and isinstance(s.get("result"), dict):
                r = s["result"]
                return {"ok": True, "duration": r.get("duration"),
                        "layers": r.get("layers"),
                        "loudness": r.get("loudness")}
        return {}

    def _add_cost(self, run_id: int, report: Dict[str, Any]) -> None:
        total = sum(float(x.get("cost_cny") or 0.0)
                    for x in report.get("shots") or [])
        if not total:
            return
        s = self.session_factory()
        try:
            r = s.get(ProduceRun, run_id)
            if r is not None:
                r.cost_cny = float(r.cost_cny or 0.0) + total
                s.commit()
        finally:
            s.close()


# --------------------------------------------------------------------------- #
# 报告落盘（与 batch 同理：后台线程的返回值送不到前端）
# --------------------------------------------------------------------------- #

def write_report(work_root: Path, run_id: int, report: Dict[str, Any]) -> str:
    import json

    root = Path(work_root) / f"r{run_id}"
    root.mkdir(parents=True, exist_ok=True)
    fp = root / "report.json"
    fp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(fp)


def read_report(work_root: Path, run_id: int) -> Optional[Dict[str, Any]]:
    import json

    fp = Path(work_root) / f"r{run_id}" / "report.json"
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except ValueError:
        return None
