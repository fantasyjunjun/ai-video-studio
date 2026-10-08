"""后期编排：把"出好的镜头"变成"能交付的成片"。

一条流水线，七步有序，**每一步都可单独重跑**（这是 P3 最重要的工程性质 ——
改个念白字数不该重出处 TI）：

    1. detect_onset   实测每镜的雾/汽雾起点（换提示词必须重测）
    2. assemble       帧网格精确裁剪 + 按帧配平 + 丢源音轨
    3. plan_sfx       由成片时间轴 + 实测起点推导音效落位
    4. narration      整条一次 TTS + 单一全局 atempo
    5. bgm            本地合成（昼夜分段）
    6. sfx            本地合成（喷香水默认 mist）
    7. mix            闪避 + limiter + -14 LUFS，并**回读校验**

为什么每步都能单独重跑：出片要花钱且慢，念白/BGM/音效全是本地零成本。
实践中真正的返工几乎都落在念白字数和音效落点上，所以这两步必须能绕开出片。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..audio import bgm as bgm_mod
from ..audio import mix as mix_mod
from ..audio import sfx as sfx_mod
from ..audio.ffmpeg import measure_loudness, probe_duration, probe_video
from ..audio.onset import detect_onset as _detect_onset
from ..audio.voice import VoiceRow, plan_voice, render_track
from ..db import fts as fts_mod
from ..db.models import Asset, AudioTrack, Shot
from ..pipeline.assembly import AssemblePlan, AssembleResult, assemble, build_plan


@dataclass
class PostPaths:
    """一个项目的后期工作目录布局。"""

    root: Path

    @property
    def dir(self) -> Path:
        return self.root

    @property
    def video_dir(self) -> Path:
        return self.root / "video"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    @property
    def report_dir(self) -> Path:
        return self.root / "reports"

    def ensure(self) -> "PostPaths":
        for p in (self.video_dir, self.audio_dir, self.tmp_dir, self.report_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self


@dataclass
class FullRequest:
    """一键后期的入参。**每一段都可缺省**，缺什么就不做什么或用现有产物。"""

    specs: List[str] = field(default_factory=list)        # 镜头片段（路径[:时长][@起始]）
    total_frames: Optional[int] = None
    fps: float = 24.0
    width: int = 480
    height: int = 864

    video_path: Optional[str] = None                       # 已装配好的底片（跳过第 2 步）

    rows: List[Tuple[float, float, str]] = field(default_factory=list)
    voice: str = "pt-BR-FranciscaNeural"
    rate: str = "+6%"
    pitch: str = "-3Hz"
    raw_wav: Optional[str] = None                          # 复用已合成原始音轨（不联网）

    bgm_duration: Optional[float] = None
    bgm_root: str = "F3"
    bgm_level_db: float = -20.0
    bgm_split: Optional[float] = None
    bgm_root2: Optional[str] = None
    bgm_minor2: bool = True
    bgm_xfade: float = 1.4

    sfx_kind: str = "mist"
    auto_sfx: bool = True
    # 显式音效落点 [(kind, at秒)]。给了就用它，**不再**走 onset 推导 ——
    # 脚本里已经写明"喷头声 mist @3.2s"，比按实测起点猜更准（那是画面侧的前摇，
    # 而脚本的落位是导演刻意安排的）。
    sfx_plan: List[Tuple[str, float]] = field(default_factory=list)

    out_name: str = "final.mp4"


class PostProduction:
    """后期流水线。**不持有 session**，每次调用都新建一个，避免跨线程共享。"""

    def __init__(self, session_factory, work_root: Path) -> None:
        self.session_factory = session_factory
        self.work_root = Path(work_root)

    # ---------------------------------------------------------- 目录

    def paths(self, project_id: int) -> PostPaths:
        return PostPaths(self.work_root / f"p{project_id}").ensure()

    # ---------------------------------------------------------- 1. onset

    def detect_onset(self, path: str, *, roi=None, fps: float = 24.0,
                     shot_id: Optional[int] = None) -> dict:
        r = _detect_onset(path, roi=roi or (0.55, 0.22, 0.95, 0.58), fps=fps)
        d = r.to_dict()
        d["note"] = r.note
        if shot_id is not None:
            s = self.session_factory()
            try:
                row: Optional[Shot] = s.get(Shot, shot_id)
                if row is not None:
                    row.onset_sec = r.onset_sec
                    s.commit()
            finally:
                s.close()
        return d

    # ---------------------------------------------------------- 2. 装配

    def plan_assemble(self, specs: Sequence[str], *, fps: float = 24.0,
                      width: int = 480, height: int = 864,
                      total_frames: Optional[int] = None,
                      codes: Optional[Sequence[str]] = None) -> AssemblePlan:
        return build_plan(list(specs), fps=fps, width=width, height=height,
                          total_frames=total_frames, codes=codes)

    def assemble(self, specs: Sequence[str], out: str, *, fps: float = 24.0,
                 width: int = 480, height: int = 864,
                 total_frames: Optional[int] = None,
                 codes: Optional[Sequence[str]] = None,
                 tmp_root: Optional[str] = None,
                 project_id: Optional[int] = None) -> AssembleResult:
        plan = self.plan_assemble(specs, fps=fps, width=width, height=height,
                                  total_frames=total_frames, codes=codes)
        tmp = tmp_root or str((Path(out).parent / "tmp"))
        os.makedirs(tmp, exist_ok=True)
        return assemble(plan, out, tmp_root=tmp)

    # ---------------------------------------------------------- 3. 音效落位计划

    @staticmethod
    def plan_sfx(shots: Sequence[Shot], trims: Optional[Dict[int, float]] = None,
                 fps: float = 24.0, total: Optional[float] = None) -> List[Dict[str, object]]:
        """由"成片时间轴 + 实测起点"推导音效落位。

        绝对时刻 = 该镜在成片里的起点 + (实测起点 − 本镜裁剪起点)。
        裁剪是为了切掉模型前摇，所以**必须减掉**，否则音效会落在画面还在发呆的地方。
        """
        trims = trims or {}
        out: List[Dict[str, object]] = []
        pos_frames = 0
        for s in sorted(shots, key=lambda x: (x.idx or 0, x.id or 0)):
            frames = int(s.target_frames or round((s.duration_sec or 0) * fps))
            start = pos_frames / fps
            pos_frames += frames
            if s.onset_sec is None:
                continue
            trim = float(trims.get(s.id, 0.0) or 0.0)
            local = max(0.0, float(s.onset_sec) - trim)
            at = start + local
            if total and at > total:
                continue
            out.append({"shot_id": s.id, "code": s.code, "kind": "mist",
                        "at": round(at, 3), "onset_in_shot": round(float(s.onset_sec), 3),
                        "trimmed_by": round(trim, 3)})
        return out

    # ---------------------------------------------------------- 4. 念白

    def narration(self, rows: Sequence[VoiceRow], total: float, out: str,
                  *, raw_wav: Optional[str] = None, workdir: Optional[str] = None,
                  voice_used: Optional[str] = None, **plan_kwargs) -> Tuple[str, dict]:
        if not raw_wav:
            raise ValueError(
                "缺少原始语音 raw_wav。联网合成请用 audio.voice.synth_raw() 先产出，"
                "或传入已有音轨复用（调时间轴时不必重跑 TTS）。")
        wd = workdir or str(Path(out).parent / "_vo")
        path, plan = render_track(raw_wav, rows, total, out, wd,
                                  voice_used=voice_used, voice_locked=voice_used,
                                  **plan_kwargs)
        return path, plan.to_dict()

    def preview_plan(self, rows: Sequence[VoiceRow], total: float, raw_dur: float,
                     **plan_kwargs) -> dict:
        """只算计划不产出 —— 前端改文案时能先看 atempo 会不会超标。**不花钱不联网**。"""
        return plan_voice(rows, total, raw_dur, **plan_kwargs).to_dict()

    # ---------------------------------------------------------- 5. 垫乐

    def bgm(self, out: str, duration: float, *, root: str = "F3",
            level_db: float = -20.0, split: Optional[float] = None,
            root2: Optional[str] = None, minor2: bool = True,
            xfade: float = 1.4) -> dict:
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        meta = bgm_mod.render_bgm(out, duration, root, level_db,
                                  split=split, root2=root2,
                                  minor2=minor2, xfade=xfade)
        if split and not root2:
            raise ValueError("给了分段点却没给第二段根音")
        return meta

    # ---------------------------------------------------------- 6. 音效

    def sfx(self, kind: str, out: str, **kw) -> dict:
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        return sfx_mod.render_sfx(kind, out, **kw)

    # ---------------------------------------------------------- 7. 混音

    def mix(self, video: str, voice: str, out: str, *, bgm: Optional[str] = None,
            sfx_cues: Sequence[str] = (), bgm_gain: float = 0.30,
            sfx_gain: float = 0.90, lufs: float = -14.0):
        return mix_mod.mix_final(video, voice, out, bgm=bgm, sfx=list(sfx_cues),
                                 bgm_gain=bgm_gain, sfx_gain=sfx_gain, lufs=lufs)

    # ---------------------------------------------------------- 回读校验

    def verify(self, path: str, *, expected_frames: Optional[int] = None,
               fps: float = 24.0) -> Dict[str, object]:
        """交付前自检。**不加 -v error**（响度结果会被吞）。"""
        v = probe_video(path)
        loud = measure_loudness(path)
        res: Dict[str, object] = {
            "path": path,
            "nb_frames": v.nb_frames,
            "duration": round(v.duration, 3),
            "fps": v.fps,
            "size": [v.width, v.height],
            "has_audio": v.has_audio,
            "loudness": loud,
            "ok": True,
            "issues": [],
        }
        issues: List[str] = res["issues"]  # type: ignore[assignment]
        if expected_frames and v.nb_frames != expected_frames:
            issues.append(f"帧数 {v.nb_frames} ≠ 期望 {expected_frames}")
        if not v.has_audio:
            issues.append("成品没有音轨")
        mean = loud.get("mean_volume_db")
        if mean is None:
            issues.append("测不出平均电平（是否被 -v error 吞掉？）")
        elif mean < -40:
            issues.append(f"平均电平 {mean:.1f}dB，近乎无声")
        res["ok"] = not issues
        return res

    # ---------------------------------------------------------- 一键

    def run_full(self, project_id: int, req: FullRequest) -> Dict[str, object]:
        """串起整条后期。每一步的产物路径都写进返回，便于逐步排查。"""
        p = self.paths(project_id)
        report: Dict[str, object] = {"project_id": project_id, "steps": []}

        def step(name: str, payload: object) -> None:
            report["steps"].append({"step": name, "result": payload})  # type: ignore[union-attr]

        # 2. 装配
        video = req.video_path
        if not video:
            out_video = p.video_dir / "assembled.mp4"
            res = self.assemble(req.specs, str(out_video), fps=req.fps,
                                width=req.width, height=req.height,
                                total_frames=req.total_frames,
                                tmp_root=str(p.tmp_dir), project_id=project_id)
            video = str(out_video)
            step("assemble", res.to_dict())
        total_sec = probe_duration(video)

        # 4. 念白
        voice_track = None
        if req.rows and req.raw_wav:
            rows = [VoiceRow(a, b, c) for a, b, c in req.rows]
            voice_track = p.audio_dir / "narration.wav"
            path, plan = self.narration(
                rows, total_sec, str(voice_track), raw_wav=req.raw_wav,
                workdir=str(p.audio_dir / "_vo"), voice_used=req.voice)
            step("narration", {"path": path, **plan})
            voice_track = path

        # 5. 垫乐
        bgm_track = None
        if req.bgm_duration or total_sec:
            bgm_track = p.audio_dir / "bgm.wav"
            meta = self.bgm(str(bgm_track), req.bgm_duration or total_sec,
                            root=req.bgm_root, level_db=req.bgm_level_db,
                            split=req.bgm_split, root2=req.bgm_root2,
                            minor2=req.bgm_minor2, xfade=req.bgm_xfade)
            step("bgm", meta)

        # 3+6. 音效
        cues: List[str] = []
        s = self.session_factory()
        try:
            shots = (s.query(Shot).filter(Shot.project_id == project_id)
                     .order_by(Shot.idx).all())
            if req.sfx_plan:
                # 脚本已给定落点：按 kind 各合成一条音效，再按 at 逐条落位
                ats_by_kind: Dict[str, List[float]] = {}
                for kind, at in req.sfx_plan:
                    ats_by_kind.setdefault(kind, []).append(float(at))
                for kind, ats in ats_by_kind.items():
                    sfx_file = p.audio_dir / f"{kind}.wav"
                    sfx_meta = self.sfx(kind, str(sfx_file))
                    step("sfx", {"kind": kind, **sfx_meta})
                    cues.extend(f"{sfx_file}@{round(a, 3)}" for a in ats)
                report["sfx_plan"] = [
                    {"kind": k, "at": round(float(a), 3)} for k, a in req.sfx_plan
                ]
            elif req.auto_sfx and shots:
                plan_sfx = self.plan_sfx(shots, fps=req.fps, total=total_sec)
                sfx_file = p.audio_dir / f"{req.sfx_kind}.wav"
                sfx_meta = self.sfx(req.sfx_kind, str(sfx_file))
                step("sfx", sfx_meta)
                cues = [f"{sfx_file}@{c['at']}" for c in plan_sfx]
                report["sfx_plan"] = plan_sfx
            report["shots"] = [
                {"id": x.id, "code": x.code, "frames": x.target_frames,
                 "onset_sec": x.onset_sec}
                for x in shots
            ]
        finally:
            s.close()

        # 7. 混音
        final_path = str(p.dir / req.out_name)
        if not voice_track:
            report["steps"].append({  # type: ignore[union-attr]
                "step": "mix", "skipped": "没有念白音轨，未混音"})
            report["video"] = video
            return report

        mres = self.mix(video, voice_track, final_path,
                        bgm=str(bgm_track) if bgm_track else None, sfx_cues=cues)
        step("mix", mres.to_dict())
        report["final"] = self.verify(final_path)

        # 落/assets
        s = self.session_factory()
        try:
            s.add(AudioTrack(project_id=project_id, kind="final", path=final_path,
                             meta={"sfx_cues": cues, "duration": mres.duration}))
            final_asset = Asset(kind="video", path=final_path,
                                project_id=project_id, meta={"stage": "final"})
            s.add(final_asset)
            s.commit()
            # 索引自维护（素材检索页已移除，不再有"重建索引"人工入口）。
            # 索引失败不该让成片登记失败 —— 文件与台账都已就绪。
            try:
                fts_mod.index_asset(s.get_bind(), final_asset)
            except Exception:  # noqa: BLE001
                pass
        finally:
            s.close()

        report["final_path"] = final_path
        return report


def write_report(report: Dict[str, object], path: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path
