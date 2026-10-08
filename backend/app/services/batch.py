"""批量变体编排器（P5）：一份 manifest，跑 N 份成片。

从技能里的 `scripts/batch_render.py` 移植，但改成**库**而不是脚本：

  - 视觉层 = (产品, 模特, 比例)：每个 `visual_unit` 只出片一次 —— 出片贵，
    **语言变体绝不重新出片**，只换音频重新混流。
  - 念白层 = 语言：每种语言只合成一次，**跨视觉单元缓存**。
  - BGM / SFX：本地 numpy 合成，全局一次，成本 ≈ 0。
  - 混流层 = (视觉单元 × 语言)：最廉价的本地 ffmpeg mux，按矩阵倍增。

所以 `plan()` 会明确给出三件事：**出片次数 / 念白次数 / 成片条数**，
并和"不去重的朴素做法"对比，让用户一眼看出省在哪。

另一条工程性质：**每一步都是独立 task 并落库**，断点续跑时先看 task 表里
有没有 succeeded 且文件还在 —— 有就跳过，不重花钱。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field
from sqlalchemy import update as sql_update

from ..audio.voice import VoiceRow, synth_raw
from ..db.models import BatchRun, BatchTask, RenderJob, Shot


# --------------------------------------------------------------------------- #
# manifest schema（可编辑、可重跑的"工作流源"）
# --------------------------------------------------------------------------- #

class BatchShot(BaseModel):
    name: str
    dur: float = 4.0                      # 提交给平台的**请求时长**（前摇随时长等比增长，要留余量）
    trim: Optional[str] = None            # "3.7500@0.5833333333" = 真实裁剪 时长@起始（吸附帧网格）
    prompt: Optional[str] = None
    prompt_file: Optional[str] = None     # 或直接读磁盘上的提示词文件
    refs: List[str] = Field(default_factory=list)
    spray: bool = False                   # 喷头镜 → 自动落 mist 音效
    seed: Optional[int] = None


class BatchUnit(BaseModel):
    id: str
    label: str = ""
    shots: List[BatchShot] = Field(default_factory=list)


class BatchLanguage(BaseModel):
    code: str
    voice: str = "pt-BR-FranciscaNeural"
    rate: str = "+6%"
    pitch: str = "-3Hz"
    timeline: List[List[Any]] = Field(default_factory=list)  # [start, end, text]
    wav: Optional[str] = None   # 已合成的原始语音 → 直接复用（不联网、也避开每次重合成的抖动）


class BatchManifest(BaseModel):
    name: str = "batch"
    film: Dict[str, Any] = Field(default_factory=lambda: {
        "duration": 15.0, "fps": 24, "width": 480, "height": 864,
        "resolution": "480p_vertical",
    })
    visual_units: List[BatchUnit] = Field(default_factory=list)
    languages: List[BatchLanguage] = Field(default_factory=list)
    audio: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def parse_trim(spec: Optional[str], fallback: float) -> Tuple[float, float]:
    """'3.7500@0.5833333333' -> (时长, 起始)。没有 @ 则起始为 0。"""
    if not spec:
        return float(fallback), 0.0
    start = 0.0
    s = spec
    if "@" in s:
        s, _, tail = s.rpartition("@")
        try:
            start = float(tail)
        except ValueError:
            start = 0.0
    try:
        dur = float(s)
    except ValueError:
        dur = float(fallback)
    return dur, start


def resolve_prompt(sh: BatchShot) -> str:
    if sh.prompt:
        return sh.prompt.strip()
    if sh.prompt_file:
        p = Path(sh.prompt_file)
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return ""


def compute_sfx_cues(unit: BatchUnit, audio: Dict[str, Any],
                     files: Dict[str, str]) -> List[str]:
    """喷头镜在 (镜起点 + spray_lag) 落 mist；末镜起点 + glass_offset 落 glass。

    ⚠️ `spray_lag` 是**实测前摇**，不是常数：同一工作流同一时长，换提示词就会变。
    manifest 里写死的 0.49s 只对当初那版提示词成立（见 TDD §9）。
    """
    lag = float(audio.get("spray_lag", 0.49))
    goff = float(audio.get("glass_offset", 0.05))
    cues: List[str] = []
    pos = 0.0
    last_start = 0.0
    for sh in unit.shots:
        dur, _ = parse_trim(sh.trim, sh.dur)
        shot_start = pos
        if sh.spray and files.get("mist"):
            cues.append(f"{files['mist']}@{round(shot_start + lag, 3)}")
        last_start = shot_start
        pos += dur
    if files.get("glass"):
        cues.append(f"{files['glass']}@{round(last_start + goff, 3)}")
    return cues


# --------------------------------------------------------------------------- #
# 编排器
# --------------------------------------------------------------------------- #

@dataclass
class BatchPaths:
    root: Path

    @property
    def shots(self) -> Path:
        return self.root / "shots"

    @property
    def silent(self) -> Path:
        return self.root / "silent"

    @property
    def audio(self) -> Path:
        return self.root / "audio"

    @property
    def out(self) -> Path:
        return self.root / "out"

    def ensure(self) -> "BatchPaths":
        for p in (self.shots, self.silent, self.audio, self.out, self.root / "tmp"):
            p.mkdir(parents=True, exist_ok=True)
        return self


class BatchOrchestrator:
    """manifest → 计划 → 执行。

    `queue` / `post` 都由 deps 注入，编排器自己不认识任何厂商。
    """

    def __init__(self, session_factory, queue, post, work_root: Path, cfg) -> None:
        self.session_factory = session_factory
        self.queue = queue
        self.post = post
        self.work_root = Path(work_root)
        self.cfg = cfg

    def paths(self, run_id: int) -> BatchPaths:
        return BatchPaths(self.work_root / f"r{run_id}").ensure()

    # ------------------------------------------------------------------ 计划

    def plan(self, m: BatchManifest) -> Dict[str, Any]:
        """只算不算账：**不联网、不出片、不花钱**。"""
        film = m.film or {}
        fps = float(film.get("fps", 24))
        duration = float(film.get("duration", 0)) or 0.0
        res = film.get("resolution", "480p_vertical")

        units = []
        render_calls = 0
        request_sec = 0.0
        trim_sec = 0.0
        for u in m.visual_units:
            n = len(u.shots)
            req = sum(float(s.dur) for s in u.shots)
            tr = sum(parse_trim(s.trim, s.dur)[0] for s in u.shots)
            render_calls += n
            request_sec += req
            trim_sec += tr
            units.append({
                "id": u.id, "label": u.label, "shots": n,
                "request_sec": round(req, 3), "trim_sec": round(tr, 3),
                "spray_shots": sum(1 for s in u.shots if s.spray),
            })

        langs = [{
            "code": lg.code, "voice": lg.voice,
            "rate": lg.rate, "pitch": lg.pitch,
            "lines": len(lg.timeline),
            "reuse_wav": bool(lg.wav),
        } for lg in m.languages]

        nu, nl = len(m.visual_units), len(m.languages)
        mux_calls = nu * nl
        naive = render_calls * max(1, nl)  # 不去重：每个语言都重出一遍画面

        # 费用：优先问供应商自己（更准），问不到才用配置里的单价
        price = self._price_per_sec(res)
        cost = round(request_sec * price, 2)

        notes: List[str] = []
        if any(s.spray for u in m.visual_units for s in u.shots):
            notes.append(
                f"喷头镜的音效落点用 audio.spray_lag="
                f"{m.audio.get('spray_lag', 0.49)}s；这是实测值，换提示词必须重测（TDD §9）")
        if any(s.refs for u in m.visual_units for s in u.shots):
            notes.append("参考图必须公网可达（AVS_IMAGE_HOST_*），出片后会自动下线")
        if duration and trim_sec and abs(trim_sec - duration) > 0.05:
            notes.append(
                f"各镜裁剪合计 {trim_sec:.3f}s 与 film.duration {duration:.3f}s 不一致，"
                f"装配会按 film.duration 配平帧数（{int(round(duration * fps))} 帧）")
        notes.append("--plan 不花钱；想看清代价先跑 dry_run，再决定要不要真出片")

        return {
            "name": m.name,
            "units": units,
            "languages": langs,
            "unit_count": nu,
            "language_count": nl,
            "render_calls": render_calls,          # 真正会花钱的次数
            "narration_calls": nl,                 # 念白按语言去重
            "mux_calls": mux_calls,                # 混流按矩阵倍增
            "final_films": mux_calls,
            "naive_render_calls": naive,           # 不去重的对照
            "saved_render_calls": naive - render_calls,
            "request_sec": round(request_sec, 3),
            "trim_sec": round(trim_sec, 3),
            "duration": duration,
            "fps": fps,
            "resolution": res,
            "price_per_sec": price,
            "cost_estimate_cny": cost,
            "notes": notes,
        }

    def _price_per_sec(self, resolution: str) -> float:
        prov = None
        try:
            prov = self.queue.video_providers.get(self.cfg.video.active)
        except Exception:  # noqa: BLE001 - 没配供应商也要能出计划
            prov = None
        est = getattr(prov, "estimate_cost", None)
        if callable(est):
            try:
                amt, _ = est(1, resolution)   # 问"1 秒多少钱"，比读配置准
                if amt:
                    return float(amt)
            except Exception:  # noqa: BLE001
                pass
        try:
            return float(self.cfg.active_video_config().cost_per_sec or 0.03)
        except Exception:  # noqa: BLE001
            return 0.03

    # ------------------------------------------------------------------ 执行

    def run(self, run_id: int, m: BatchManifest, *,
            only_units: Optional[Sequence[str]] = None,
            only_langs: Optional[Sequence[str]] = None,
            dry_run: bool = False) -> Dict[str, Any]:
        film = m.film or {}
        fps = float(film.get("fps", 24))
        duration = float(film.get("duration", 0)) or None
        width = int(film.get("width", 480))
        height = int(film.get("height", 864))
        res = film.get("resolution", "480p_vertical")
        total_frames = int(round(duration * fps)) if duration else None

        p = self.paths(run_id)
        self._set_run(run_id, status="running", message="执行中")

        units = [u for u in m.visual_units
                 if not only_units or u.id in set(only_units)]
        langs = [lg for lg in m.languages
                 if not only_langs or lg.code in set(only_langs)]

        report: Dict[str, Any] = {
            "run_id": run_id, "dry_run": dry_run,
            "units": [], "languages": [], "films": [], "errors": [],
        }

        # ---------- 1. 视觉层：每个视觉单元出片一次 ----------
        silent_map: Dict[str, str] = {}
        for u in units:
            ok = True
            for sh in u.shots:
                out, err = self._ensure_shot(run_id, u, sh, p, res, dry_run)
                if err:
                    report["errors"].append(err)
                    ok = False
            if dry_run or not ok:
                report["units"].append({"id": u.id, "silent": None,
                                        "status": "dry_run" if dry_run else "failed"})
                continue
            silent, err = self._ensure_silent(run_id, u, p, fps, width, height,
                                              total_frames)
            if err:
                report["errors"].append(err)
                report["units"].append({"id": u.id, "silent": None, "status": "failed"})
                continue
            silent_map[u.id] = silent
            report["units"].append({"id": u.id, "silent": silent, "status": "succeeded"})

        # ---------- 2. 念白层：每种语言一次，跨视觉单元缓存 ----------
        voice_map: Dict[str, str] = {}
        for lg in langs:
            wav, err = self._ensure_narration(run_id, lg, p, duration or 0.0)
            if err:
                report["errors"].append(err)
                report["languages"].append({"code": lg.code, "status": "failed",
                                            "error": err})
                continue
            voice_map[lg.code] = wav
            report["languages"].append({"code": lg.code, "wav": wav,
                                        "status": "succeeded"})

        # ---------- 3. BGM / SFX：全局一次，本地合成 ≈ 0 ----------
        bgm_path: Optional[str] = None
        sfx_files: Dict[str, str] = {}
        if not dry_run:
            bgm_path, bgm_err = self._ensure_bgm(run_id, m, p, duration or 0.0)
            if bgm_err:
                report["errors"].append(bgm_err)
            sfx_files, sfx_err = self._ensure_sfx(run_id, m, p)
            if sfx_err:
                report["errors"].append(sfx_err)

        # ---------- 4. 混流层：视觉单元 × 语言 ----------
        audio_cfg = m.audio or {}
        for u in units:
            silent = silent_map.get(u.id)
            if not silent:
                report["films"].append({"unit": u.id, "lang": "*", "status": "skipped",
                                        "reason": "静片缺失"})
                continue
            cues = compute_sfx_cues(u, audio_cfg, sfx_files)
            for lg in langs:
                wav = voice_map.get(lg.code)
                if not wav:
                    report["films"].append({"unit": u.id, "lang": lg.code,
                                            "status": "skipped",
                                            "reason": "念白缺失"})
                    continue
                out = p.out / f"{u.id}-{lg.code}.mp4"
                path, err = self._ensure_mux(run_id, u.id, lg.code, silent, wav,
                                             bgm_path, cues, str(out), audio_cfg)
                report["films"].append({
                    "unit": u.id, "lang": lg.code,
                    "status": "succeeded" if path else "failed",
                    "path": path, "error": err,
                })
                if err:
                    report["errors"].append(err)

        ok = sum(1 for f in report["films"] if f["status"] == "succeeded")

        # 报告落盘（**先写，再置状态**）：run 在后台线程，返回体送不到前端，
        # 轮询端靠 work_dir/report.json 拿完整结果。若先置 done 再写文件，
        # 轮询端会在文件还没落盘时就先看到 done，读到空 report —— 这是实打实的竞态。
        try:
            import json

            (p.root / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            report["report_path"] = str(p.root / "report.json")
        except Exception:  # noqa: BLE001 - 报告写不出来不该判定执行失败
            pass

        status = "done" if not report["errors"] else "failed"
        self._set_run(run_id, status=status, message=f"成片 {ok} 条")
        return report

    # --------------------------------------------------- 各层的具体实现

    def _ensure_shot(self, run_id: int, u: BatchUnit, sh: BatchShot,
                     p: BatchPaths, res: str, dry_run: bool
                     ) -> Tuple[Optional[str], Optional[str]]:
        key = f"{u.id}/{sh.name}"
        prior = self._find_task(run_id, "render", key)
        if prior and prior.status == "succeeded" and prior.output_path \
                and Path(prior.output_path).exists():
            return prior.output_path, None

        prompt = resolve_prompt(sh)
        if not prompt:
            self._upsert_task(run_id, "render", key, "failed",
                              message="没有提示词（prompt 或 prompt_file 至少要有一个）")
            return None, f"[{key}] 没有提示词"

        job = RenderJob(
            project_id=self._run_project(run_id),
            shot_id=None,                      # 批量镜不一定挂在 shot 表上
            kind="video",
            provider_id=self.cfg.video.active,
            status="queued",
            params={
                "prompt": prompt,
                "duration": int(math.ceil(sh.dur)),
                "resolution": res,
                "ref_images": list(sh.refs),
                **({"seed": sh.seed} if sh.seed is not None else {}),
                "batch_run_id": run_id,
                "batch_key": key,
            },
            dry_run=1 if dry_run else 0,
        )
        s = self.session_factory()
        try:
            s.add(job)
            s.commit()
            s.refresh(job)
            job_id = job.id
        finally:
            s.close()

        fut = self.queue.enqueue(job_id)
        try:
            fut.result(timeout=float(self.queue.poll_timeout) + 120)
        except Exception as e:  # noqa: BLE001
            self._upsert_task(run_id, "render", key, "failed",
                              message=f"{type(e).__name__}: {e}"[:500])
            return None, f"[{key}] 出片异常 {e}"

        s = self.session_factory()
        try:
            j = s.get(RenderJob, job_id)
            if j is None:
                return None, f"[{key}] 任务记录丢失"
            if j.status == "dry_run":
                self._upsert_task(run_id, "render", key, "skipped",
                                  message=j.message or "试算（未出片）")
                return None, None
            if j.status != "succeeded" or not j.output_path:
                self._upsert_task(run_id, "render", key, "failed",
                                  message=(j.error or j.message or "出片失败")[:500])
                return None, f"[{key}] 出片失败：{(j.error or j.message)[:200]}"
            self._add_cost(run_id, float(j.cost_cny or 0.0))
            self._upsert_task(run_id, "render", key, "succeeded",
                              output_path=j.output_path,
                              cost_cny=float(j.cost_cny or 0.0),
                              message=f"job {job_id} · {j.provider_id}")
            return j.output_path, None
        finally:
            s.close()

    def _ensure_silent(self, run_id: int, u: BatchUnit, p: BatchPaths,
                       fps: float, width: int, height: int,
                       total_frames: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
        prior = self._find_task(run_id, "silent", u.id)
        if prior and prior.status == "succeeded" and prior.output_path \
                and Path(prior.output_path).exists():
            return prior.output_path, None

        s = self.session_factory()
        specs: List[str] = []
        codes: List[str] = []
        try:
            for sh in u.shots:
                t = self._find_task(run_id, "render", f"{u.id}/{sh.name}")
                if not t or not t.output_path or not Path(t.output_path).exists():
                    return None, f"[{u.id}/{sh.name}] 镜头产物缺失，无法拼静片"
                dur, start = parse_trim(sh.trim, sh.dur)
                spec = f"{t.output_path}:{dur:.4f}"
                if start:
                    spec += f"@{start:.10f}"
                specs.append(spec)
                codes.append(sh.name)
        finally:
            s.close()

        out = p.silent / f"{u.id}.mp4"
        try:
            r = self.post.assemble(specs, str(out), fps=fps, width=width,
                                   height=height, total_frames=total_frames,
                                   codes=codes, tmp_root=str(p.root / "tmp"))
        except Exception as e:  # noqa: BLE001
            self._upsert_task(run_id, "silent", u.id, "failed", message=str(e)[:500])
            return None, f"[{u.id}] 拼接失败：{e}"
        if not r.ok or not Path(out).exists():
            self._upsert_task(run_id, "silent", u.id, "failed",
                              message="; ".join(r.warnings)[:500])
            return None, f"[{u.id}] 拼接失败：{'; '.join(r.warnings)}"
        self._upsert_task(run_id, "silent", u.id, "succeeded", output_path=str(out),
                          message=f"{r.nb_frames} 帧 / {r.duration:.3f}s",
                          meta={"warnings": r.warnings})
        return str(out), None

    def _ensure_narration(self, run_id: int, lg: BatchLanguage, p: BatchPaths,
                          total: float) -> Tuple[Optional[str], Optional[str]]:
        prior = self._find_task(run_id, "narration", lg.code)
        if prior and prior.status == "succeeded" and prior.output_path \
                and Path(prior.output_path).exists():
            return prior.output_path, None

        rows = [VoiceRow(start=float(r[0]), end=float(r[1]), text=str(r[2]))
                for r in lg.timeline if len(r) >= 3]
        adir = p.audio / lg.code
        adir.mkdir(parents=True, exist_ok=True)
        out = adir / "narration.wav"

        raw = lg.wav
        if not raw:
            if not rows:
                self._upsert_task(run_id, "narration", lg.code, "failed",
                                  message="既没有 wav 也没有 timeline，无法合成念白")
                return None, f"[{lg.code}] 缺少念白来源"
            text = " ".join(r.text for r in rows).strip()
            mp3 = adir / "raw.mp3"
            used = synth_raw(text, lg.voice, lg.rate, lg.pitch, str(mp3))
            if not used:
                self._upsert_task(run_id, "narration", lg.code, "failed",
                                  message="TTS 合成失败（端点超时？）")
                return None, f"[{lg.code}] TTS 失败"
            raw = str(mp3)
        elif not Path(raw).exists():
            self._upsert_task(run_id, "narration", lg.code, "failed",
                              message=f"指定的 wav 不存在: {raw}")
            return None, f"[{lg.code}] wav 不存在: {raw}"

        if not rows:
            # 没有时间轴 = 用户给的已经是成品念白，原样使用
            self._upsert_task(run_id, "narration", lg.code, "succeeded",
                              output_path=raw, message="复用成品念白（未做时间轴落位）")
            return raw, None

        try:
            path, plan = self.post.narration(
                rows, total, str(out), raw_wav=raw,
                workdir=str(adir / "_vo"), voice_used=lg.voice)
        except Exception as e:  # noqa: BLE001
            self._upsert_task(run_id, "narration", lg.code, "failed",
                              message=str(e)[:500])
            return None, f"[{lg.code}] 念白落位失败：{e}"
        self._upsert_task(run_id, "narration", lg.code, "succeeded",
                          output_path=path, message=f"atempo {plan.get('tempo', 1)}",
                          meta={"plan": plan})
        return path, None

    def _ensure_bgm(self, run_id: int, m: BatchManifest, p: BatchPaths,
                    duration: float) -> Tuple[Optional[str], Optional[str]]:
        prior = self._find_task(run_id, "bgm", "bgm")
        if prior and prior.status == "succeeded" and prior.output_path \
                and Path(prior.output_path).exists():
            return prior.output_path, None
        b = (m.audio or {}).get("bgm")
        if not b or not duration:
            return None, None
        out = p.audio / "bgm.wav"
        try:
            self.post.bgm(str(out), duration,
                          root=b.get("root", "F3"),
                          level_db=float(b.get("level_db", -20)),
                          split=b.get("split"), root2=b.get("root2"),
                          minor2=bool(b.get("minor2", True)),
                          xfade=float(b.get("xfade", 1.4)))
        except Exception as e:  # noqa: BLE001
            self._upsert_task(run_id, "bgm", "bgm", "failed", message=str(e)[:500])
            return None, f"垫乐合成失败：{e}"
        self._upsert_task(run_id, "bgm", "bgm", "succeeded", output_path=str(out))
        return str(out), None

    def _ensure_sfx(self, run_id: int, m: BatchManifest, p: BatchPaths
                    ) -> Tuple[Dict[str, str], Optional[str]]:
        cfg = (m.audio or {}).get("sfx") or {}
        kinds = cfg.get("kinds") or ["mist", "glass"]
        files: Dict[str, str] = {}
        for kind in kinds:
            prior = self._find_task(run_id, "sfx", kind)
            if prior and prior.status == "succeeded" and prior.output_path \
                    and Path(prior.output_path).exists():
                files[kind] = prior.output_path
                continue
            out = p.audio / f"{kind}.wav"
            try:
                self.post.sfx(kind, str(out))
            except Exception as e:  # noqa: BLE001
                self._upsert_task(run_id, "sfx", kind, "failed", message=str(e)[:500])
                return files, f"音效 {kind} 合成失败：{e}"
            self._upsert_task(run_id, "sfx", kind, "succeeded", output_path=str(out))
            files[kind] = str(out)
        return files, None

    def _ensure_mux(self, run_id: int, uid: str, code: str, silent: str,
                    voice: str, bgm: Optional[str], cues: Sequence[str],
                    out: str, audio_cfg: Dict[str, Any]
                    ) -> Tuple[Optional[str], Optional[str]]:
        key = f"{uid}×{code}"
        prior = self._find_task(run_id, "mux", key)
        if prior and prior.status == "succeeded" and prior.output_path \
                and Path(prior.output_path).exists():
            return prior.output_path, None
        try:
            self.post.mix(silent, voice, out, bgm=bgm, sfx_cues=list(cues),
                          bgm_gain=float(audio_cfg.get("bgm_gain", 0.30)),
                          sfx_gain=float(audio_cfg.get("sfx_gain", 0.90)),
                          lufs=float(audio_cfg.get("lufs", -14)))
        except Exception as e:  # noqa: BLE001
            self._upsert_task(run_id, "mux", key, "failed", message=str(e)[:500])
            return None, f"[{key}] 混音失败：{e}"
        if not Path(out).exists():
            self._upsert_task(run_id, "mux", key, "failed", message="混音未产出文件")
            return None, f"[{key}] 混音未产出文件"
        self._upsert_task(run_id, "mux", key, "succeeded", output_path=out,
                          message=f"cues {len(cues)}")
        return out, None

    # ------------------------------------------------------------------ 记账

    def _find_task(self, run_id: int, kind: str, key: str) -> Optional[BatchTask]:
        s = self.session_factory()
        try:
            return (s.query(BatchTask)
                    .filter(BatchTask.run_id == run_id,
                            BatchTask.kind == kind,
                            BatchTask.key == key)
                    .order_by(BatchTask.id.desc()).first())
        finally:
            s.close()

    def _upsert_task(self, run_id: int, kind: str, key: str, status: str, *,
                     output_path: str = "", message: str = "",
                     cost_cny: float = 0.0, meta: Optional[dict] = None) -> None:
        s = self.session_factory()
        try:
            t = (s.query(BatchTask)
                 .filter(BatchTask.run_id == run_id, BatchTask.kind == kind,
                         BatchTask.key == key)
                 .order_by(BatchTask.id.desc()).first())
            if t is None:
                t = BatchTask(run_id=run_id, kind=kind, key=key)
                s.add(t)
            t.status = status
            t.output_path = output_path or t.output_path
            t.message = message[:1000]
            t.cost_cny = cost_cny
            if meta:
                t.meta = meta
            s.commit()
        finally:
            s.close()

    def _add_cost(self, run_id: int, amount: float) -> None:
        if not amount:
            return
        s = self.session_factory()
        try:
            # 与 project.cost_cny 同理：必须走 SQL 层原子自增，不能读-改-写
            s.execute(sql_update(BatchRun)
                      .where(BatchRun.id == run_id)
                      .values(cost_cny=BatchRun.cost_cny + float(amount)))
            s.commit()
        finally:
            s.close()

    def _run_project(self, run_id: int) -> Optional[int]:
        s = self.session_factory()
        try:
            r = s.get(BatchRun, run_id)
            return r.project_id if r else None
        finally:
            s.close()

    def _set_run(self, run_id: int, *, status: str, message: str = "") -> None:
        s = self.session_factory()
        try:
            r = s.get(BatchRun, run_id)
            if r is not None:
                r.status = status
                if message:
                    r.message = message
                s.commit()
        finally:
            s.close()


# --------------------------------------------------------------------------- #
# 从现有项目生成 manifest 初稿
# --------------------------------------------------------------------------- #

def manifest_from_project(db, project_id: int, *, resolution: str = "480p_vertical",
                          fps: float = 24.0) -> Dict[str, Any]:
    """把已跑通的项目导出成"可编辑、可重跑的工作流源"。

    视觉单元默认只有一个（当前项目）；想加产品/礼服变体 = 复制一份改 refs 与 label ——
    它会**独立出片一次**，而不是重跑整套。
    """
    shots = (db.query(Shot).filter(Shot.project_id == project_id)
             .order_by(Shot.idx.asc(), Shot.id.asc()).all())
    from ..db.models import Project  # 局部导入避免与上层循环

    proj = db.get(Project, project_id)
    total = sum(float(s.duration_sec or 0) for s in shots) or 15.0

    unit = {
        "id": "VP01",
        "label": (proj.name if proj else f"p{project_id}") or "主版本",
        "shots": [{
            "name": s.code or f"S{i + 1}",
            "dur": round(float(s.duration_sec or 3) * 1.25 + 0.5, 2),  # 给前摇留余量
            "trim": f"{float(s.duration_sec or 3):.4f}",
            "prompt": s.prompt_en or "",
            "spray": bool(s.onset_sec),
        } for i, s in enumerate(shots)],
    }
    lang = {
        "code": (proj.language if proj else "pt-BR") or "pt-BR",
        "voice": "pt-BR-FranciscaNeural",
        "rate": "+6%", "pitch": "-3Hz",
        "timeline": [],   # 念白文案不在 shot 表里，导出后由人填（或复用已有 wav）
    }
    return {
        "name": f"{(proj.name if proj else 'project') or 'project'}-batch",
        "film": {"duration": round(total, 3), "fps": fps,
                 "width": 480, "height": 864, "resolution": resolution},
        "visual_units": [unit],
        "languages": [lang],
        "audio": {"bgm": {"root": "F3", "level_db": -20},
                  "sfx": {"kinds": ["mist", "glass"]},
                  "spray_lag": 0.49, "glass_offset": 0.05},
    }
