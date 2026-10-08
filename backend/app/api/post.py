"""后期 API（P3）：onset 实测 / 帧精确装配 / 念白 / 垫乐 / 音效 / 混音 / 校验。

设计原则：**七个步骤都能单独调用**。
出片要花钱且慢，而念白、垫乐、音效全是本地零成本；真实返工几乎都落在
"念白字数"和"音效落点"两件事上，所以这两个绝不能被"必须重出视频"绑住。

路由一览：
    POST /api/shots/{id}/onset                  实测该镜雾/汽雾起点（写回 shot.onset_sec）
    POST /api/projects/{pid}/narration/plan     只算 atempo 计划，**不联网不产出**
    POST /api/projects/{pid}/narration          念白落位出音轨
    POST /api/projects/{pid}/bgm                本地合成垫乐（可昼夜分段）
    POST /api/projects/{pid}/sfx                本地合成音效（默认 mist）
    POST /api/projects/{pid}/sfx-plan           由时间轴+实测起点推导音效落位
    POST /api/projects/{pid}/mix                四轨混音出成片
    POST /api/projects/{pid}/final              一键串起整条后期
    GET  /api/projects/{pid}/audio-tracks       该项目音轨清单
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import deps
from ..audio.ffmpeg import FFmpegError
from ..audio.voice import VoiceRow
from ..db.models import AudioTrack, Project, Shot
from ..db.session import get_db
from ..services.compose import (ComposeBlocked, ComposeRequest, build_plan,
                                run_compose)
from ..services.post import FullRequest, PostProduction
from .compliance import scan_project

router = APIRouter(prefix="/api", tags=["post"])


def _parse_roi(text: Optional[str]):
    """把 "x0,y0,x1,y1" 解析成 4 元组；空/非法一律返回 None（用默认 ROI）。"""
    if not text:
        return None
    parts = [p for p in str(text).replace(" ", "").split(",") if p]
    if len(parts) != 4:
        raise HTTPException(400, "ROI 需要 4 个数：x0,y0,x1,y1（0–1 相对坐标）")
    try:
        vals = tuple(float(p) for p in parts)
    except ValueError as e:
        raise HTTPException(400, f"ROI 里有非数字：{text}") from e
    if not all(0.0 <= v <= 1.0 for v in vals):
        raise HTTPException(400, "ROI 各值必须在 0–1 之间（相对坐标）")
    if vals[2] <= vals[0] or vals[3] <= vals[1]:
        raise HTTPException(400, "ROI 需要 x1>x0 且 y1>y0")
    return vals


class OnsetIn(BaseModel):
    roi: Optional[List[float]] = None
    fps: float = 24.0


class NarrationRow(BaseModel):
    start: float
    end: float
    text: str


class NarrationIn(BaseModel):
    rows: List[NarrationRow]
    total: float
    raw_wav: Optional[str] = None      # 复用已合成原始音轨（**不联网**）
    voice: str = "pt-BR-FranciscaNeural"
    out: str = "narration.wav"
    # plan 参数
    lead: float = 0.12
    tail: float = 0.30
    start: Optional[float] = None
    cuts: Optional[List[float]] = None
    fill: bool = False
    max_stretch: float = 1.12
    max_tempo: float = 1.30


class RawIn(BaseModel):
    """原始念白音轨（**会联网**）。

    整条一次 TTS，音色在开头锁定。调时间轴时不要重跑这一步 ——
    用 narration 的 `raw_wav` 复用即可（音色零漂移、也省一次网络往返）。
    """

    text: str
    voice: str = "pt-BR-FranciscaNeural"
    rate: str = "+6%"
    pitch: str = "-3Hz"
    out: str = "raw_voice.mp3"
    retries: int = 10
    fallback_after: int = 4


class PlanIn(BaseModel):
    rows: List[NarrationRow]
    total: float
    raw_dur: float
    lead: float = 0.12
    tail: float = 0.30
    start: Optional[float] = None
    cuts: Optional[List[float]] = None
    fill: bool = False
    max_stretch: float = 1.12
    max_tempo: float = 1.30


class BgmIn(BaseModel):
    duration: float
    out: str = "bgm.wav"
    root: str = "F3"
    level_db: float = -20.0
    split: Optional[float] = None
    root2: Optional[str] = None
    minor2: bool = True
    xfade: float = 1.4


class SfxIn(BaseModel):
    kind: str = "mist"
    out: str = "mist.wav"
    seed: int = 20260927
    dur: Optional[float] = None
    peak_db: Optional[float] = None


class SfxPlanIn(BaseModel):
    trims: Dict[int, float] = Field(default_factory=dict)
    fps: float = 24.0
    total: Optional[float] = None


class MixIn(BaseModel):
    video: str
    voice: str
    out: str = "final.mp4"
    bgm: Optional[str] = None
    sfx: List[str] = Field(default_factory=list)   # "路径@秒数"
    bgm_gain: float = 0.30
    sfx_gain: float = 0.90
    lufs: float = -14.0


class FinalIn(BaseModel):
    specs: List[str] = Field(default_factory=list)
    video_path: Optional[str] = None
    total_frames: Optional[int] = None
    fps: float = 24.0
    rows: List[NarrationRow] = Field(default_factory=list)
    raw_wav: Optional[str] = None
    voice: str = "pt-BR-FranciscaNeural"
    bgm_duration: Optional[float] = None
    bgm_split: Optional[float] = None
    bgm_root2: Optional[str] = None
    sfx_kind: str = "mist"
    auto_sfx: bool = True
    out_name: str = "final.mp4"
    # 广告法合规门禁（P-4）：高危禁语默认拦下；确认无风险可用 force 放行。
    enforce_compliance: bool = True
    force: bool = False


def _pid(pid: int, db: Session) -> Project:
    p: Optional[Project] = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, f"项目 {pid} 不存在")
    return p


def _ok(fn):
    """把 ffmpeg/依赖类异常翻译成 400/500，别把 traceback 甩给前端。"""
    try:
        return fn()
    except FFmpegError as e:
        raise HTTPException(500, f"ffmpeg 执行失败：{str(e)[:400]}") from e
    except FileNotFoundError as e:
        raise HTTPException(400, f"文件不存在：{e}") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/shots/{shot_id}/onset")
def shot_onset(shot_id: int, body: OnsetIn, db: Session = Depends(get_db),
               post: PostProduction = Depends(deps.get_post)):
    """实测结果性元素起点。**换提示词后必须重测** —— 前摇不是常数。"""
    shot: Optional[Shot] = db.get(Shot, shot_id)
    if shot is None:
        raise HTTPException(404, f"镜头 {shot_id} 不存在")
    path = shot.video_path
    if not path:
        raise HTTPException(400,
                            f"镜头 {shot.code or shot_id} 还没有出片产物，无法实测起点")
    roi = tuple(body.roi) if body.roi and len(body.roi) == 4 else None  # type: ignore[arg-type]
    return _ok(lambda: post.detect_onset(path, roi=roi, fps=body.fps, shot_id=shot_id))


@router.post("/projects/{pid}/narration/raw")
def narration_raw(pid: int, body: RawIn, db: Session = Depends(get_db),
                  post: PostProduction = Depends(deps.get_post)):
    """合成原始语音。**唯一会联网的音频步骤**。"""
    _pid(pid, db)
    p = post.paths(pid)
    out_path = body.out if "/" in body.out or "\\" in body.out \
        else str(p.audio_dir / body.out)

    def _run():
        from ..audio.ffmpeg import probe_duration
        from ..audio.voice import synth_raw

        used = synth_raw(body.text, body.voice, body.rate, body.pitch,
                         out_path, retries=body.retries,
                         fallback_after=body.fallback_after)
        if used is None:
            raise HTTPException(502, "TTS 全部重试失败（端点超时？检查代理环境变量）")
        dur = probe_duration(out_path)
        db.add(AudioTrack(project_id=pid, kind="voice_raw", path=out_path,
                          meta={"voice": used, "voice_is_fallback": used != body.voice,
                                "duration": dur, "text": body.text[:200]}))
        db.commit()
        return {"path": out_path, "voice": used,
                "voice_is_fallback": used != body.voice, "duration": dur}

    return _ok(_run)


@router.post("/projects/{pid}/narration/plan")
def narration_plan(pid: int, body: PlanIn, db: Session = Depends(get_db),
                   post: PostProduction = Depends(deps.get_post)):
    """只算计划：**不联网、不产出**。前端改文案时先看 atempo 会不会超 1.02。"""
    _pid(pid, db)
    rows = [VoiceRow(r.start, r.end, r.text) for r in body.rows]
    return post.preview_plan(rows, body.total, body.raw_dur,
                             lead=body.lead, tail=body.tail, start=body.start,
                             cuts=body.cuts, fill=body.fill,
                             max_stretch=body.max_stretch, max_tempo=body.max_tempo)


@router.post("/projects/{pid}/narration")
def narration(pid: int, body: NarrationIn, db: Session = Depends(get_db),
              post: PostProduction = Depends(deps.get_post)):
    p = _pid(pid, db)
    rows = [VoiceRow(r.start, r.end, r.text) for r in body.rows]
    out_path = body.out if "/" in body.out or "\\" in body.out \
        else str(post.paths(pid).audio_dir / body.out)

    def _run():
        path, plan = post.narration(
            rows, body.total, out_path, raw_wav=body.raw_wav, voice_used=body.voice,
            lead=body.lead, tail=body.tail, start=body.start, cuts=body.cuts,
            fill=body.fill, max_stretch=body.max_stretch, max_tempo=body.max_tempo)
        db.add(AudioTrack(project_id=pid, kind="voice", path=path, meta=plan))
        db.commit()
        return {"path": path, "project_id": pid, **plan}

    return _ok(_run)


@router.post("/projects/{pid}/bgm")
def bgm(pid: int, body: BgmIn, db: Session = Depends(get_db),
        post: PostProduction = Depends(deps.get_post)):
    _pid(pid, db)
    out_path = body.out if "/" in body.out or "\\" in body.out \
        else str(post.paths(pid).audio_dir / body.out)

    def _run():
        meta = post.bgm(out_path, body.duration, root=body.root,
                        level_db=body.level_db, split=body.split,
                        root2=body.root2, minor2=body.minor2, xfade=body.xfade)
        db.add(AudioTrack(project_id=pid, kind="bgm", path=out_path, meta=meta))
        db.commit()
        return {"path": out_path, "project_id": pid, **meta}

    return _ok(_run)


@router.post("/projects/{pid}/sfx")
def sfx(pid: int, body: SfxIn, db: Session = Depends(get_db),
        post: PostProduction = Depends(deps.get_post)):
    _pid(pid, db)
    out_path = body.out if "/" in body.out or "\\" in body.out \
        else str(post.paths(pid).audio_dir / body.out)

    def _run():
        meta = post.sfx(body.kind, out_path, seed=body.seed,
                        dur=body.dur, peak_db=body.peak_db)
        db.add(AudioTrack(project_id=pid, kind="sfx", path=out_path, meta=meta))
        db.commit()
        return {"path": out_path, "project_id": pid, **meta}

    return _ok(_run)


@router.post("/projects/{pid}/sfx-plan")
def sfx_plan(pid: int, body: SfxPlanIn, db: Session = Depends(get_db),
             post: PostProduction = Depends(deps.get_post)):
    """推导音效落位：该镜在成片里的起点 + (实测起点 − 本镜裁剪起点)。"""
    _pid(pid, db)
    shots = db.query(Shot).filter(Shot.project_id == pid).order_by(Shot.idx).all()
    plan = post.plan_sfx(shots, trims=body.trims, fps=body.fps, total=body.total)
    missing = [s.code or s.id for s in shots if s.onset_sec is None]
    return {"project_id": pid, "cues": plan,
            "no_onset": missing,
            "hint": "没有 onset 的镜头不会挂音效 —— 跑 /shots/{id}/onset 实测后再来"}


@router.post("/projects/{pid}/mix")
def mix_track(pid: int, body: MixIn, db: Session = Depends(get_db),
              post: PostProduction = Depends(deps.get_post)):
    _pid(pid, db)
    out_path = body.out if "/" in body.out or "\\" in body.out \
        else str(post.paths(pid).dir / body.out)

    def _run():
        res = post.mix(body.video, body.voice, out_path, bgm=body.bgm,
                       sfx_cues=body.sfx, bgm_gain=body.bgm_gain,
                       sfx_gain=body.sfx_gain, lufs=body.lufs)
        db.add(AudioTrack(project_id=pid, kind="final", path=out_path,
                          meta=res.to_dict()))
        db.commit()
        return {"path": out_path, "project_id": pid, **res.to_dict()}

    return _ok(_run)


@router.get("/projects/{pid}/audio-tracks")
def list_tracks(pid: int, db: Session = Depends(get_db)):
    rows = (db.query(AudioTrack).filter(AudioTrack.project_id == pid)
            .order_by(AudioTrack.id.desc()).all())
    return [{"id": r.id, "kind": r.kind, "path": r.path, "meta": r.meta,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows]


@router.post("/projects/{pid}/verify")
def verify(pid: int, expected_frames: Optional[int] = None,
           path: Optional[str] = None, fps: float = 24.0,
           qc: bool = False, qc_sheet: bool = False,
           qc_roi: Optional[str] = None,
           db: Session = Depends(get_db),
           post: PostProduction = Depends(deps.get_post)):
    """交付前自检：帧数 / 有无音轨 / 电平 / 广告法合规 / **出片质量门（可选）**。

    合规项扫的是**库里能拿到的文案**（镜头中文直译 + 念白音轨留痕 + 项目名）。
    前端正在编辑、还没落库的念白稿，请用
    `POST /api/projects/{pid}/compliance` 显式传 rows 扫描。

    `qc=true` 时额外跑**结果级**质检（P-6）：黑场 / 冻结帧 / 整镜无运动 /
    亮度跳变 / 重复帧 / 结果性元素未出现 / 起点与台账不符。它要真解帧，
    所以默认关；但它是"lint 满分却出片翻车"的唯一兜底，建议交付前开一次。
    """
    _pid(pid, db)
    from ..db.models import AudioTrack as _AT

    target = path
    if not target:
        last = (db.query(_AT).filter(_AT.project_id == pid, _AT.kind == "final")
                .order_by(_AT.id.desc()).first())
        target = last.path if last else None
    if not target:
        raise HTTPException(400, "没有成片路径，也没出过成片")

    res = post.verify(target, expected_frames=expected_frames, fps=fps)

    # 出片质量门（P-6）：high 级直接让 self-check 不通过。
    if qc:
        from ..pipeline import qc as qc_mod

        # ROI 先解析：非法参数要**明确 400**，不能被下面的 try 吞成"质检出错"
        roi_tuple = _parse_roi(qc_roi)
        try:
            rep = qc_mod.analyze(
                target, fps=fps, roi=roi_tuple,
                expect_silent=False,
            )
            res["qc"] = rep.to_dict()
            res["issues"].extend(f"出片质检：{s}" for s in rep.issues)
            if rep.blocked:
                res["ok"] = False
            if qc_sheet:
                sheet = qc_mod.contact_sheet(
                    target, str(Path(target).with_suffix("")) + "_sheet.png")
                res["qc"]["sheet"] = sheet
        except Exception as e:  # noqa: BLE001 - 质检坏掉不该让帧数自检一起 500
            res["qc"] = {"error": f"{type(e).__name__}: {e}"}

    # 帧数对了但文案违法，片子一样不能发 —— 所以合规并入同一次自检，
    # 而不是让用户记得"还要再点一个按钮"。
    try:
        rep = scan_project(pid, db)
        res["compliance"] = rep.to_dict()
        if rep.blocked:
            res["issues"].append("广告法合规自检：" + rep.summary_text())
            res["ok"] = False
    except Exception as e:  # noqa: BLE001 - 词表坏掉不该让帧数自检一起 500
        res["compliance"] = {"error": f"{type(e).__name__}: {e}"}
    return res


@router.post("/projects/{pid}/final")
def run_final(pid: int, body: FinalIn, db: Session = Depends(get_db),
              post: PostProduction = Depends(deps.get_post)):
    """一键后期。缺什么就不做什么（例如没给 raw_wav 就不做念白）。

    带**广告法合规门禁**：高危禁语默认 422 拦下，避免"文案违法但音画完美"的
    片子被一键产出并直接投放。门禁长在服务端，是因为 `/final` 会被脚本与批量
    编排直接调用 —— 只在页面上提示等于没有门禁。
    """
    _pid(pid, db)

    compliance_info: Optional[Dict[str, Any]] = None
    if body.enforce_compliance:
        rep = scan_project(pid, db, texts=[r.text for r in body.rows])
        compliance_info = rep.to_dict()
        if rep.blocked and not body.force:
            hits = [f"『{f.matched}』（{f.category_label}·{f.source}）"
                    for f in rep.findings if f.severity == "high"][:5]
            raise HTTPException(422, (
                "广告法合规自检未通过，已阻止一键后期：高危用语 "
                + "、".join(hits)
                + f"（共 {rep.counts['high']} 项高危）。"
                "请改写文案后重试；确认无风险可传 force=true 强制继续。"
            ))

    req = FullRequest(
        specs=body.specs, total_frames=body.total_frames, fps=body.fps,
        video_path=body.video_path,
        rows=[(r.start, r.end, r.text) for r in body.rows],
        raw_wav=body.raw_wav, voice=body.voice,
        bgm_duration=body.bgm_duration, bgm_split=body.bgm_split,
        bgm_root2=body.bgm_root2, sfx_kind=body.sfx_kind,
        auto_sfx=body.auto_sfx, out_name=body.out_name,
    )
    res = _ok(lambda: post.run_full(pid, req))
    if compliance_info is not None and isinstance(res, dict):
        res["compliance"] = compliance_info
    return res


# ------------------------------------------------------------------ 一键成片（15s）

# 语言 → 音色的映射、规划与执行，全部在 `services/compose.py`
# （人工点一下的 /compose 与「一键出片」编排必须跑同一份实现）。


class ComposeIn(BaseModel):
    """一键成片：把「逐镜产物 + 脚本念白稿 + 音效落点」拼成一条可交付成片。

    与 `/final` 的区别：**入参几乎全自动**。`/final` 需要调用方自己算好
    specs / rows / raw_wav；这里只要一个项目号，其余从库里取：

      - 镜头片段 = 该项目所有**已有产物**的 shot，按 idx 排序、按 target_frames 配平；
      - 念白行   = 最新一版**脚本素材**里的念白稿（`use_script_plan`，可被 rows 覆盖）；
      - 原始语音 = 整条一次 TTS（音色按项目语言挑），或 `reuse_raw` 复用已有音轨；
      - 音效落点 = 脚本的「音效落位」表（`use_script_sfx`），没有才退回 onset 实测推导；
      - 垫乐     = 本地合成，长度自动取成片实际时长。

    **无念白时默认拒绝**（`allow_silent=true` 才放行）：用户反馈过"视频里只有背景音、
    没有旁白"，静默产出一条没念白的片子正是那个故障现象 —— 宁可明确拒绝并说清原因。
    """

    use_script_plan: bool = True
    rows: List[NarrationRow] = Field(default_factory=list)   # 显式念白，覆盖脚本
    raw_text: Optional[str] = None                            # 自定义 TTS 文本
    voice: Optional[str] = None                               # 不传则按项目语言挑
    reuse_raw: Optional[str] = None                           # 复用已合成音轨（不联网）
    retries: int = 10
    fallback_after: int = 4
    fps: float = 24.0
    # 是否把每镜开头的模型前摇裁掉。默认关：源片通常只比目标长 0.5s，
    # 裁掉近 1s 的前摇会不够用（装配阶段会警告并缩短），要看效果得先把出片时长调长。
    trim_onset: bool = False
    use_script_sfx: bool = True
    sfx_kind: str = "mist"
    bgm_root: str = "F3"
    bgm_level_db: float = -20.0
    bgm_split: Optional[float] = None
    bgm_root2: Optional[str] = None
    out_name: str = "final.mp4"
    allow_silent: bool = False
    enforce_compliance: bool = True
    force: bool = False
    dry_run: bool = False


@router.post("/projects/{pid}/compose")
def compose(pid: int, body: ComposeIn, db: Session = Depends(get_db),
            post: PostProduction = Depends(deps.get_post)):
    """一键成片（15s 广告）。`dry_run=true` 只看规划：齐备性 / 帧数 / 念白 / 音效落点。

    规划与执行的核心都在 `services/compose.py`：这里是人工点一下的入口，
    `services/produce.py` 的「一键出片」编排走的是同一份实现 —— 两条路径
    若各写一份，"什么算齐备""没有念白时拒还是放行"必然分叉。
    """
    req = ComposeRequest(
        rows=[(r.start, r.end, r.text) for r in body.rows],
        use_script_plan=body.use_script_plan,
        use_script_sfx=body.use_script_sfx,
        raw_text=body.raw_text,
        voice=body.voice,
        reuse_raw=body.reuse_raw,
        retries=body.retries,
        fallback_after=body.fallback_after,
        fps=body.fps,
        trim_onset=body.trim_onset,
        sfx_kind=body.sfx_kind,
        bgm_root=body.bgm_root,
        bgm_level_db=body.bgm_level_db,
        bgm_split=body.bgm_split,
        bgm_root2=body.bgm_root2,
        out_name=body.out_name,
        allow_silent=body.allow_silent,
        enforce_compliance=body.enforce_compliance,
        force=body.force,
    )
    try:
        if body.dry_run:
            return build_plan(db, pid, req) | {"dry_run": True}
        return _ok(lambda: run_compose(db, post, pid, req))
    except ComposeBlocked as e:
        # 闸门语义码由 compose 层给出（400 缺料 / 422 合规 / 502 TTS）
        raise HTTPException(e.status, e.message) from e
