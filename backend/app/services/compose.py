"""一键成片核心：脚本念白 + 逐镜产物 → 一条可交付的 15s 广告。

抽出来的理由
------------
`POST /api/projects/{pid}/compose`（人工点一下）与 `services/produce.py`
（脚本→出片→成片一条龙）要跑的是同一件事。**规划与执行都只留一份实现** ——
"帧数怎么配平""念白从哪来""没有念白时拒还是放行"这些判据一旦分叉，
两条路径就会产出不同的片子，而用户完全无从察觉。

设计约束
--------
- `build_plan()` **不产出任何文件、不联网、不花钱**：前端要靠它做"出片前确认"。
- 闸门（缺镜头 / 没念白 / 合规高危）**全部在花钱与联网之前**，
  并以 `ComposeBlocked` 抛出（带 HTTP 语义的状态码），
  由 API 层翻成 4xx、由编排层记成一条可读的失败步骤。
- 不 import `deps`：`post` 由调用方注入，这一层不认识任何全局单例。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..db.models import AudioTrack, Project, Shot
from .post import FullRequest, PostProduction
from .scripts import latest_script

# 语言 → 默认音色。**同一语言族内才降级**，跨语言降级是灾难（见 audio/voice.py）。
_DEFAULT_VOICE_BY_LANG = {
    "zh-CN": "zh-CN-XiaoxiaoNeural",
    "pt-BR": "pt-BR-FranciscaNeural",
    "es-MX": "es-MX-DaliaNeural",
}


def voice_for_lang(lang: str) -> str:
    s = (lang or "").strip()
    if s in _DEFAULT_VOICE_BY_LANG:
        return _DEFAULT_VOICE_BY_LANG[s]
    low = s.lower()
    for k, v in _DEFAULT_VOICE_BY_LANG.items():
        if low.startswith(k.split("-")[0].lower()):
            return v
    return "pt-BR-FranciscaNeural"


class ComposeBlocked(Exception):
    """成片被闸门拦下。`status` 是给 HTTP 层用的语义码（400 / 422）。"""

    def __init__(self, status: int, message: str,
                 detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail or {}


@dataclass
class ComposeRequest:
    """一键成片入参。**入参几乎全自动**：只要项目号，其余从库里取。

    - 镜头片段 = 该项目所有**已有产物**的 shot，按 idx 排序、按 target_frames 配平；
    - 念白行   = 最新一版脚本素材的念白稿（`use_script_plan`，可被 rows 覆盖）；
    - 原始语音 = 整条一次 TTS（音色按项目语言挑），或 `reuse_raw` 复用已有音轨；
    - 音效落点 = 脚本的「音效落位」表（`use_script_sfx`），没有才退回 onset 推导；
    - 垫乐     = 本地合成，长度自动取成片实际时长。

    **无念白时默认拒绝**（`allow_silent=True` 才放行）：用户反馈过"视频里只有背景音、
    没有旁白"，静默产出一条没念白的片子正是那个故障现象 —— 宁可明确拒绝并说清原因。
    """

    rows: List[Tuple[float, float, str]] = field(default_factory=list)
    use_script_plan: bool = True
    use_script_sfx: bool = True
    raw_text: Optional[str] = None
    voice: Optional[str] = None
    reuse_raw: Optional[str] = None
    retries: int = 10
    fallback_after: int = 4
    fps: float = 24.0
    # 是否把每镜开头的模型前摇裁掉。默认关：源片通常只比目标长 0.5s，
    # 裁掉近 1s 的前摇会不够用（装配阶段会警告并缩短）。
    trim_onset: bool = False
    sfx_kind: str = "mist"
    bgm_root: str = "F3"
    bgm_level_db: float = -20.0
    bgm_split: Optional[float] = None
    bgm_root2: Optional[str] = None
    out_name: str = "final.mp4"
    allow_silent: bool = False
    enforce_compliance: bool = True
    force: bool = False


def _ready(s: Shot) -> bool:
    return bool(s.video_path) and Path(str(s.video_path)).exists()


def _load_shots(db: Session, pid: int) -> List[Shot]:
    return (db.query(Shot).filter(Shot.project_id == pid)
            .order_by(Shot.idx, Shot.id).all())


def build_plan(db: Session, pid: int, req: ComposeRequest) -> Dict[str, Any]:
    """规划与预算：**不产出文件、不联网、不花钱**。

    返回体同时也是 `/compose` 的回执（`compose` 字段），
    让"这次到底按什么规格拼的"有据可查。
    """
    proj: Optional[Project] = db.get(Project, pid)
    if proj is None:
        raise ComposeBlocked(404, f"项目 {pid} 不存在")

    fps = float(req.fps or 24.0)
    shots = _load_shots(db, pid)
    if not shots:
        raise ComposeBlocked(
            400, "项目还没有分镜 —— 先用「生成视频提示词」产出脚本，"
                 "或在弹窗里粘贴脚本导入")

    missing = [s.code or f"#{s.id}" for s in shots if not _ready(s)]

    # ---- 规格：按帧配平（帧数优先，秒数相约会攒出偏差）----
    specs: List[str] = []
    total_frames = 0
    segs: List[Dict[str, Any]] = []
    for s in shots:
        if not _ready(s):
            continue
        frames = int(s.target_frames or round((s.duration_sec or 0.0) * fps))
        if frames <= 0:
            missing.append(s.code or f"#{s.id}")
            continue
        start = float(s.onset_sec or 0.0) if req.trim_onset else 0.0
        spec = f"{s.video_path}:{frames / fps:.6f}"
        if start > 0:
            spec += f"@{start:.6f}"
        specs.append(spec)
        total_frames += frames
        segs.append({"code": s.code, "shot_id": s.id, "frames": frames,
                     "start": round(start, 3),
                     "duration_sec": round(frames / fps, 3)})

    # ---- 念白行 / 音效落点：显式入参优先，否则取最新脚本素材 ----
    asset = latest_script(db, pid)
    plan_meta: Dict[str, Any] = dict(asset.meta or {}) if asset is not None else {}
    script_rows = plan_meta.get("vo_rows") or []
    script_cues = plan_meta.get("sfx_cues") or []

    rows: List[Tuple[float, float, str]] = [
        (float(a), float(b), t) for a, b, t in req.rows if str(t).strip()
    ]
    rows_source = "explicit"
    if not rows and req.use_script_plan and script_rows:
        rows = [(float(x["start"]), float(x["end"]), str(x["text"]))
                for x in script_rows if str(x.get("text", "")).strip()]
        rows_source = "script"
    elif not rows:
        rows_source = "none"

    sfx_plan: List[Tuple[str, float]] = []
    sfx_source = "onset"
    if req.use_script_sfx and script_cues:
        sfx_plan = [(str(c.get("kind") or req.sfx_kind), float(c.get("at") or 0.0))
                    for c in script_cues]
        sfx_source = "script"

    warnings: List[str] = []
    if missing:
        warnings.append(f"{len(missing)} 个镜头还没有出片产物：" + "、".join(missing))
    if not rows:
        warnings.append(
            "没有念白行：最新脚本素材里没有念白稿，也没显式传 rows。"
            "脚本可能是外部粘贴的、或念白稿整段留白 —— 可在音频页补念白稿后重试")
    if req.trim_onset:
        warnings.append(
            "已按 onset 裁剪每镜开头：源片通常只比目标长不到 1s，"
            "缺帧会自动缩短（见装配步骤的 warnings）")
    if asset is None:
        warnings.append("该项目还没有脚本素材，念白/音效只能靠显式入参或 onset 推导")

    return {
        "project_id": pid,
        "shots_total": len(shots),
        "shots_ready": len(specs),
        "missing": missing,
        "fps": fps,
        "total_frames": total_frames,
        "total_sec": round(total_frames / fps, 3),
        "segments": segs,
        "rows": [{"start": a, "end": b, "text": t} for a, b, t in rows],
        "rows_source": rows_source,
        "sfx_plan": [{"kind": k, "at": a} for k, a in sfx_plan],
        "sfx_source": sfx_source,
        "script_asset_id": asset.id if asset is not None else None,
        "language": proj.language,
        "voice": req.voice or voice_for_lang(proj.language),
        "warnings": warnings,
    }


def run_compose(db: Session, post: PostProduction, pid: int,
                req: Optional[ComposeRequest] = None) -> Dict[str, Any]:
    """真跑一遍成片：装配 → TTS → 念白落位 → BGM/音效 → 混音 → 自检。"""
    req = req or ComposeRequest()
    plan = build_plan(db, pid, req)
    missing = list(plan["missing"])
    rows = [(r["start"], r["end"], r["text"]) for r in plan["rows"]]
    sfx_plan = [(c["kind"], c["at"]) for c in plan["sfx_plan"]]

    # `build_plan` 的 segments 只带镜号与帧数，**不带产物路径**（那不该出现在规划里）。
    # 装配要的是 `路径:时长[@起始]`，所以这里重算一次（纯内存，零成本、零副作用）。
    fps = float(plan["fps"])
    specs = []
    total_frames = 0
    for s in _load_shots(db, pid):
        if not _ready(s):
            continue
        frames = int(s.target_frames or round((s.duration_sec or 0.0) * fps))
        if frames <= 0:
            continue
        start = float(s.onset_sec or 0.0) if req.trim_onset else 0.0
        spec = f"{s.video_path}:{frames / fps:.6f}"
        if start > 0:
            spec += f"@{start:.6f}"
        specs.append(spec)
        total_frames += frames

    # ---- 闸门（都放在"花钱/联网"之前）----
    if missing:
        raise ComposeBlocked(400, (
            f"还有 {len(missing)} 个镜头没有出片产物：" + "、".join(missing)
            + "。请先对它们出片（已有片子的镜次会自动复用，不会重复计费）。"
        ), {"missing": missing})
    if not rows and not req.allow_silent:
        raise ComposeBlocked(400, (
            "没有可用的念白行，拒绝产出一条没有旁白的成片。"
            "请① 在「生成视频提示词」里重新生成脚本（模板会带念白稿），或 "
            "② 到音频页手工补念白稿，或 ③ 传 rows 显式指定；"
            "确实要做无旁白版本时传 allow_silent=true。"
        ))

    compliance_info: Optional[Dict[str, Any]] = None
    if req.enforce_compliance:
        # 懒导入：合规层在 api/ 下，放在函数内避免 import 顺序问题
        from ..api.compliance import scan_project

        rep = scan_project(pid, db, texts=[t for _, _, t in rows])
        compliance_info = rep.to_dict()
        if rep.blocked and not req.force:
            hits = [f"『{f.matched}』（{f.category_label}·{f.source}）"
                    for f in rep.findings if f.severity == "high"][:5]
            raise ComposeBlocked(422, (
                "广告法合规自检未通过，已阻止成片：高危用语 " + "、".join(hits)
                + f"（共 {rep.counts['high']} 项高危）。请改写文案后重试；"
                "确认无风险可传 force=true 强制继续。"
            ), {"compliance": compliance_info})

    # ---- 原始语音：整条一次 TTS（音色在开头一次性锁定）----
    raw_wav = req.reuse_raw
    requested_voice = req.voice or voice_for_lang(_lang_of(db, pid))
    voice_used = requested_voice
    if rows and not raw_wav:
        from ..audio.voice import default_params_for, lang_of, synth_raw

        text = (req.raw_text or " ".join(t for _, _, t in rows)).strip()
        if not text:
            raise ComposeBlocked(400, "念白行内容为空，无法合成语音")
        raw_path = str(post.paths(pid).audio_dir / "raw_voice.mp3")
        rate, pitch = default_params_for(lang_of(requested_voice))
        used = synth_raw(text, requested_voice, rate, pitch, raw_path,
                         retries=req.retries, fallback_after=req.fallback_after)
        if used is None:
            raise ComposeBlocked(
                502, "TTS 全部重试失败（端点超时？先清掉代理环境变量再试）")
        voice_used = used
        raw_wav = raw_path
        # 记「实际用的音色」+「是否兜底」——记锁定音色等于撒谎
        db.add(AudioTrack(project_id=pid, kind="voice_raw", path=raw_wav,
                          meta={"voice": used,
                                "voice_is_fallback": used != requested_voice,
                                "text": text[:200]}))
        db.commit()

    full = FullRequest(
        specs=specs, total_frames=total_frames, fps=fps,
        rows=rows, raw_wav=raw_wav, voice=voice_used,
        bgm_root=req.bgm_root, bgm_level_db=req.bgm_level_db,
        bgm_split=req.bgm_split, bgm_root2=req.bgm_root2,
        sfx_kind=req.sfx_kind, auto_sfx=not sfx_plan, sfx_plan=sfx_plan,
        out_name=req.out_name,
    )
    res = post.run_full(pid, full)
    if isinstance(res, dict):
        res["compose"] = plan | {"dry_run": False}
        res["raw_voice"] = raw_wav
        res["voice_used"] = voice_used
        if compliance_info is not None:
            res["compliance"] = compliance_info
    return res


def compose_final_path(res: Dict[str, Any]) -> Optional[str]:
    """从分步报告里取成片路径（混音那一步的产物）。"""
    for s in res.get("steps") or []:
        if s.get("step") == "mix" and isinstance(s.get("result"), dict):
            p = s["result"].get("path")
            if p:
                return str(p)
    return None


def _lang_of(db: Session, pid: int) -> str:
    proj: Optional[Project] = db.get(Project, pid)
    return (proj.language if proj is not None else "") or "pt-BR"
