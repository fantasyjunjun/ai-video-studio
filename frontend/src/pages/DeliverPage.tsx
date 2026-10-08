/** 成片页 —— 项目里**唯一**的工作页。
 *
 * 产品形态改成「脚本生成 → 直接一键出 15s 成片」之后，中间那次"人工生成分镜、
 * 逐镜出片"的停顿就取消掉了。原来分散在四页（分镜 / 任务 / 成片 / 音频）的东西
 * 按"还需要人管"的程度重新分成四块：
 *
 *   ① 出片运行面板  一键出片 + 规划预览 + 逐阶段进度 + 成片播放器 + 失败重试
 *   ② 镜头与提示词  出问题了才需要看：镜头表 + 单镜重出 + 编辑提示词 + 参考图
 *   ③ 自检与合规    交付前把关：帧数/音轨/电平 + 出片质检 + 广告法
 *   ④ 高级          对账与排查用：成本 / 脚本素材 / 音轨 / 任务明细 / 音频后期
 *
 * 为什么"分镜→出片"这个人工步骤能删：脚本里已经写了镜号、时长、念白稿与音效落点，
 * 而时长与参考图在出片链路里是**自动跟随**的（见 services/render_core.py）。
 * 人工那一步的实际作用只剩"再点一下"，却让人以为不点它就不会出片。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import {
  Alert, App as AntApp, Button, Card, Col, Collapse, Descriptions, Drawer, Input,
  InputNumber, Progress, Row, Space, Statistic, Switch, Table, Tag, Typography,
} from 'antd';
import {
  ReloadOutlined, ThunderboltOutlined,
} from '@ant-design/icons';
import { api, fileUrl, type Json } from '../api';
import {
  BudgetPanel, CompliancePanel, LintTag, MIN_SCORE, PlanPreview, QualityPanel,
  StatusTag,
} from '../components';

/** 出片运行/任务的"还在跑"状态集合。 */
const ACTIVE = new Set(['queued', 'running']);

/** 出片阶段 → 人话。顺序也用于进度条。 */
const STAGES: [string, string][] = [
  ['script', '脚本落镜'],
  ['render', '逐镜出片'],
  ['compose', '合成成片'],
  ['done', '完成'],
];

const stageLabel = (s?: string) =>
  STAGES.find(([k]) => k === s)?.[1] || s || '-';

export default function DeliverPage() {
  const { id } = useParams();
  const pid = Number(id);
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const { message, modal } = AntApp.useApp();

  // ---------------- 出片运行 ----------------
  const [runId, setRunId] = useState<number | null>(null);
  const [run, setRun] = useState<Json | null>(null);
  const [runs, setRuns] = useState<Json[]>([]);
  const [plan, setPlan] = useState<Json | null>(null);
  const [planBusy, setPlanBusy] = useState(false);
  const [allowSilent, setAllowSilent] = useState(false);
  const [force, setForce] = useState(false);

  // ---------------- 项目 / 镜头 ----------------
  const [proj, setProj] = useState<Json>({});
  const [shots, setShots] = useState<Json[]>([]);
  const [tl, setTl] = useState<Json>({});
  const [refs, setRefs] = useState<Json>({});
  const [media, setMedia] = useState<Json>({});
  const [cost, setCost] = useState<Json>({});
  const [tracks, setTracks] = useState<Json[]>([]);
  const [scripts, setScripts] = useState<Json[]>([]);

  // ---------------- 单镜编辑 ----------------
  const [drawer, setDrawer] = useState<Json | null>(null);
  const [text, setText] = useState('');
  const [lint, setLint] = useState<Json | null>(null);
  const [saving, setSaving] = useState(false);
  const [shotBusy, setShotBusy] = useState<number | null>(null);

  // ---------------- 交付自检 ----------------
  const [verifyPath, setVerifyPath] = useState('');
  const [frames, setFrames] = useState<number | null>(null);
  const [withQc, setWithQc] = useState(true);
  const [verifyRes, setVerifyRes] = useState<Json | null>(null);

  const pollRef = useRef<number | null>(null);

  // 从 `?run=123` 恢复：出片提交后跳过来，刷新页面也不会丢进度
  useEffect(() => {
    const r = Number(params.get('run'));
    if (r) setRunId(r);
  }, [params]);

  const loadShots = useCallback(async () => {
    try {
      const [p, s, t, rf] = await Promise.all([
        api.getProject(pid), api.listShots(pid), api.timeline(pid),
        api.projectRefs(pid),
      ]);
      setProj(p); setShots(s); setTl(t); setRefs(rf || {});
      setFrames((f) => f ?? (t.total_frames || null));
      const fin = (await api.tracks(pid)).find((x: Json) => x.kind === 'final');
      if (fin) setVerifyPath((v) => v || fin.path);
    } catch (e) { message.error((e as Error).message); }
  }, [pid, message]);

  const loadAdvanced = useCallback(async () => {
    try {
      const [c, m, tr, sc, rs] = await Promise.all([
        api.cost(pid), api.mediaProviders(), api.tracks(pid),
        api.projectScripts(pid), api.listProduceRuns(pid),
      ]);
      setCost(c); setMedia(m); setTracks(tr); setScripts(sc || []); setRuns(rs || []);
    } catch (e) { message.error((e as Error).message); }
  }, [pid, message]);

  useEffect(() => { void loadShots(); void loadAdvanced(); }, [loadShots, loadAdvanced]);

  // ---------------- 轮询出片运行 ----------------
  const pollRun = useCallback(async (rid: number) => {
    try {
      const r = await api.getProduceRun(pid, rid);
      setRun(r);
      if (r.final_path) setVerifyPath((v) => v || r.final_path);
      if (ACTIVE.has(r.status)) {
        pollRef.current = window.setTimeout(() => { void pollRun(rid); }, 3000);
      } else {
        setPlan(r.plan || null);
        // 跑完刷新镜头与台账：产物/花费都变了
        void loadShots(); void loadAdvanced();
        message[r.status === 'done' ? 'success' : 'warning'](r.message || '已结束');
      }
    } catch (e) {
      message.error((e as Error).message);
    }
  }, [pid, message, loadShots, loadAdvanced]);

  useEffect(() => {
    if (pollRef.current) { window.clearTimeout(pollRef.current); pollRef.current = null; }
    if (!runId) { setRun(null); return; }
    void pollRun(runId);
    return () => {
      if (pollRef.current) { window.clearTimeout(pollRef.current); pollRef.current = null; }
    };
  }, [runId, pollRun]);

  // ---------------- 出片：规划 → 确认 → 提交 ----------------
  const startProduce = async (script?: string, source = 'agent') => {
    setPlanBusy(true);
    try {
      const body: Json = { allow_silent: allowSilent, force };
      if (script) { body.script = script; body.source = source; }
      const p = await api.producePlan(pid, body);
      setPlan(p);

      if (!p.shots_total) {
        message.warning(p.warnings?.[0] || '没有镜头可出 —— 先生成或导入脚本');
        return;
      }
      modal.confirm({
        title: '确认出片？',
        width: 660,
        okText: p.estimate_cny > 0
          ? `确认出片（预估 ¥${Number(p.estimate_cny).toFixed(2)}）`
          : '确认出片',
        cancelText: '再想想',
        content: <PlanPreview plan={p} />,
        onOk: async () => {
          try {
            const r = await api.startProduce(pid, body);
            setRunId(r.id);
            setParams({ run: String(r.id) }, { replace: true });
            message.success(`已提交出片 #${r.id}，正在后台执行`);
          } catch (e) { message.error((e as Error).message); }
        },
      });
    } catch (e) { message.error((e as Error).message); }
    finally { setPlanBusy(false); }
  };

  const retry = async () => {
    if (!runId) return;
    try {
      const r = await api.retryProduce(pid, runId, {});
      setRun(r);
      void pollRun(runId);
      message.success('已重新提交：只补没出片的镜头，已有的直接复用');
    } catch (e) { message.error((e as Error).message); }
  };

  // ---------------- 单镜：编辑 / 重出 ----------------
  const openShot = (r: Json) => { setDrawer(r); setText(r.prompt_en || ''); setLint(r.lint_report || null); };

  const relint = async () => {
    try {
      const r = await api.lintPrompt(text, Number(drawer?.duration_sec || 3));
      setLint(r);
      message[r.passed ? 'success' : 'warning'](`得分 ${r.total}/14（闸门 ${r.min_score}）`);
    } catch (e) { message.error((e as Error).message); }
  };

  const saveShot = async () => {
    setSaving(true);
    try {
      if (!drawer) return;
      await api.patchShot(drawer.id, { prompt_en: text });
      message.success('已保存并重算 lint');
      setDrawer(null); void loadShots();
    } catch (e) { message.error((e as Error).message); }
    finally { setSaving(false); }
  };

  /** 单镜出片。真出片要轮询这一条 job 到终态 —— 不轮询用户就不知道跑完没有。 */
  const renderShot = async (shotId: number, dry: boolean) => {
    setShotBusy(shotId);
    try {
      const r = await api.renderShot(shotId, { kind: 'video', dry_run: dry, reuse: true });
      const n = (r?.params?.ref_images || []).length;
      const d = r?.params?.duration;
      const src = r?.params?.duration_source;
      const durTxt = d
        ? `，时长 ${d}s（${src === 'shot' ? '跟随分镜' : src === 'explicit' ? '指定' : '供应商默认'}）`
        : '';
      if (dry) {
        // 试算顺手把"首帧是哪张、有没有图被丢掉"说清楚 —— 用户点试算想确认的
        // 就是"这一镜会拍成什么样"，而 ref_image_0 是首帧、排序决定观感。
        const rf: Json = r?.refs || {};
        const first = (rf.sequence || [])[0];
        const warn = rf.dropped_count
          ? `；⚠️ ${rf.dropped_count} 张图因限额不会发送` : '';
        message.success(
          `试算任务 #${r.id}（不花钱，参考图 ${n} 张${durTxt}）`
          + (first ? `；首帧 ref_image_0 = ${first.label || first.kind}` : '')
          + warn);
        return;
      }
      message.success(`已提交 #${r.id}（参考图 ${n} 张${durTxt}），等待出片…`);
      for (let i = 0; i < 400; i += 1) {
        await new Promise((res) => window.setTimeout(res, 3000));
        const j = await api.getJob(r.id);
        if (!ACTIVE.has(j.status)) {
          if (j.status === 'succeeded' || j.status === 'reused') {
            message.success(`镜头 ${j.shot_id} 完成：${j.status === 'reused' ? '复用已有产物' : '已出片'}`);
          } else {
            message.error(`镜头 ${j.shot_id} 失败：${j.error || j.message || j.status}`);
          }
          break;
        }
      }
      void loadShots(); void loadAdvanced();
    } catch (e) { message.error((e as Error).message); }
    finally { setShotBusy(null); }
  };

  const verify = async () => {
    try {
      const r = await api.verify(pid, {
        path: verifyPath || undefined,
        expected_frames: frames ?? undefined,
        fps: tl.fps ?? 24,
        qc: withQc || undefined,
      });
      setVerifyRes(r);
      message[r.ok ? 'success' : 'warning'](r.ok ? '交付自检通过' : '自检有问题，见下方');
    } catch (e) { message.error((e as Error).message); }
  };

  const finalPath = run?.final_path || tracks.find((t) => t.kind === 'final')?.path || '';
  const ready = shots.filter((s) => s.has_video || s.video_path).length;
  const running = !!run && ACTIVE.has(run.status);
  const stageIdx = Math.max(0, STAGES.findIndex(([k]) => k === run?.stage));

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {/* ============================ ① 出片运行面板 ============================ */}
      <Card
        size="small"
        title={`一键出片 · #${pid} ${proj.name || ''}`}
        extra={
          <Space size={8}>
            <Tag color={ready === shots.length && shots.length ? 'green' : 'default'}>
              已出片 {ready}/{shots.length} 镜
            </Tag>
            <Button size="small" icon={<ReloadOutlined />}
              onClick={() => { void loadShots(); void loadAdvanced(); }}>刷新</Button>
          </Space>
        }
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            type="info" showIcon
            message="出片 = 脚本自动落镜 → 逐镜出片（时长跟随分镜、自动挂商品图与主播图）→ 念白/音效/垫乐混成 15s 成片"
            description="念白整条一次 TTS（音色开头锁定，不逐句漂移）；垫乐与音效本地合成、零版权。已有产物的镜头**自动复用、不重复计费**，所以重试只会补没出的那几镜。"
          />

          {/* 图床状态：没有公网 URL，带参考图的出片会被拒 */}
          {media.image_host_enabled ? (
            <Alert type="success" showIcon
              message={`临时图床已启用（${media.image_host_mode}）——参考图会在出片前发布成公网 URL`}
              description={media.image_host_can_delete
                ? '出片结束后自动下线（截断覆盖，不删文件）。'
                : `该图床没有删除 API：出片结束后我方只是"用完了"，文件仍会公网可读到服务方过期（${media.image_host_expiry || '时效未知'}）。介意的话请改用自控的静态目录图床（AVS_IMAGE_HOST_ROOT + AVS_IMAGE_HOST_URL）。`} />
          ) : (
            <Alert type="warning" showIcon
              message="未启用临时图床：出片需要公网可达的参考图 URL"
              description="默认应走上传型图床（uguu），现在被显式关掉了。删掉 AVS_IMAGE_HOST_UPLOAD=off，或改配静态目录图床：AVS_IMAGE_HOST_ROOT（静态目录）+ AVS_IMAGE_HOST_URL（对外地址），改完重启后端。否则带参考图的出片请求会被拒绝。" />
          )}

          <Space wrap size={16} align="center">
            <Button type="primary" icon={<ThunderboltOutlined />} loading={planBusy}
              disabled={running} onClick={() => startProduce()}>
              一键出片
            </Button>
            <Button loading={planBusy} disabled={running}
              onClick={async () => {
                try { setPlan(await api.producePlan(pid, {})); }
                catch (e) { message.error((e as Error).message); }
              }}>
              只看规划（不花钱）
            </Button>
            {run && !running && (
              <Button onClick={retry}>重试（只补缺失镜头）</Button>
            )}
            <Space size={4}>
              <Switch size="small" checked={allowSilent} onChange={setAllowSilent} />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                允许无念白（默认拒绝：没旁白就产片，正是"只有背景音没有念白"的故障来源）
              </Typography.Text>
            </Space>
            <Space size={4}>
              <Switch size="small" checked={force} onChange={setForce} />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                放行合规高危（默认拦）
              </Typography.Text>
            </Space>
          </Space>

          {shots.length === 0 && scripts.length === 0 && (
            <Alert type="warning" showIcon
              message="项目里还没有脚本，也没有镜头"
              description={
                <span>
                  先到「项目工作台」用 <b>生成视频提示词</b>（Trae Agent 读技能产出脚本）或
                  <b>粘贴脚本导入</b>，脚本里带镜号/时长/念白稿/音效落点，出片会直接用。
                  <Button type="link" size="small" onClick={() => nav('/')}>去项目工作台</Button>
                </span>
              } />
          )}

          {/* 规划预览 */}
          {plan && !running && (
            <Card size="small" title="规划预览（未产出任何文件、未花钱）">
              <PlanPreview plan={plan} />
            </Card>
          )}

          {/* 运行进度 */}
          {run && (
            <Card size="small"
              title={<Space size={8}>
                <span>出片运行 #{run.id}</span>
                <StatusTag status={run.status} />
                <Tag>{stageLabel(run.stage)}</Tag>
              </Space>}
              extra={<Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {run.updated_at?.replace('T', ' ').slice(0, 19)}
              </Typography.Text>}>
              <Space direction="vertical" size={10} style={{ width: '100%' }}>
                <Progress
                  percent={Math.round(
                    (Math.max(stageIdx, 0) + (run.stage === 'done' ? 1 : 0))
                    / STAGES.length * 100)}
                  size="small" status={run.status === 'failed' ? 'exception'
                    : running ? 'active' : 'success'}
                />
                <Space size={16} wrap>
                  <Statistic title="预估花费" value={run.cost_estimate_cny ?? 0} precision={2} prefix="¥" />
                  <Statistic title="实际花费" value={run.cost_cny ?? 0} precision={2} prefix="¥" />
                  <Statistic title="镜头"
                    value={`${(run.report?.shots || []).filter(
                      (x: Json) => x.status === 'succeeded').length} 出新 / ${
                      (run.report?.shots || []).filter(
                        (x: Json) => x.status === 'reused').length} 复用 / ${
                      (run.report?.shots || []).filter(
                        (x: Json) => x.status === 'failed').length} 失败`} />
                </Space>

                {(run.steps || []).length > 0 && (
                  <Table size="small" rowKey={(r: Json) => `${r.key}-${r.at}`}
                    dataSource={run.steps} pagination={false}
                    columns={[
                      { title: '步骤', dataIndex: 'label', width: 210 },
                      { title: '状态', dataIndex: 'status', width: 100,
                        render: (v: string) => <StatusTag status={v} /> },
                      { title: '说明', dataIndex: 'message', ellipsis: true },
                      { title: '时间', dataIndex: 'at', width: 170,
                        render: (v: string) => (v || '').replace('T', ' ') },
                    ]} />
                )}

                {(run.report?.errors || []).length > 0 && (
                  <Alert type="error" showIcon message="失败项"
                    description={<ul style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
                      {(run.report.errors as string[]).map((s, i) => <li key={i}>{s}</li>)}
                    </ul>} />
                )}
                {(run.report?.compose_blocked) && (
                  <Alert type="warning" showIcon
                    message={`成片被拦下（${run.report.compose_blocked.status}）`}
                    description={run.report.compose_blocked.message} />
                )}

                {/* 成片播放器 */}
                {run.final_path ? (
                  <Space direction="vertical" size={8} style={{ width: '100%' }}>
                    <Space size={16} wrap>
                      <Tag color="green">成片已产出</Tag>
                      <span>时长 {run.final?.duration ?? '-'}s</span>
                      <span>音轨层 {(run.final?.layers || []).join(' + ') || '-'}</span>
                      <span>平均电平 {run.final?.loudness?.mean_volume_db?.toFixed?.(1) ?? '-'} dB</span>
                    </Space>
                    <video src={fileUrl(run.final_path)} controls
                      style={{ width: 320, background: '#000' }} />
                    <Typography.Text copyable style={{ fontSize: 12 }}>
                      {run.final_path}
                    </Typography.Text>
                  </Space>
                ) : null}
              </Space>
            </Card>
          )}

          {/* 成片（非本页发起的也显示） */}
          {!run && finalPath && (
            <Card size="small" title="最近一次成片">
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                <video src={fileUrl(finalPath)} controls
                  style={{ width: 320, background: '#000' }} />
                <Typography.Text copyable style={{ fontSize: 12 }}>{finalPath}</Typography.Text>
              </Space>
            </Card>
          )}

          {/* 历史运行 */}
          {runs.length > 0 && (
            <Collapse ghost size="small" items={[{
              key: 'runs', label: `出片运行历史（${runs.length} 条）`,
              children: (
                <Table size="small" rowKey="id" dataSource={runs} pagination={false}
                  columns={[
                    { title: 'ID', dataIndex: 'id', width: 60 },
                    { title: '状态', dataIndex: 'status', width: 100,
                      render: (v: string) => <StatusTag status={v} /> },
                    { title: '阶段', dataIndex: 'stage', width: 100,
                      render: (v: string) => <Tag>{stageLabel(v)}</Tag> },
                    { title: '说明', dataIndex: 'message', ellipsis: true },
                    { title: '预估', dataIndex: 'cost_estimate_cny', width: 90,
                      render: (v: number) => `¥${(v || 0).toFixed(2)}` },
                    { title: '实际', dataIndex: 'cost_cny', width: 90,
                      render: (v: number) => `¥${(v || 0).toFixed(2)}` },
                    { title: '时间', dataIndex: 'created_at', width: 170,
                      render: (v: string) => (v || '').replace('T', ' ') },
                    { title: '', width: 70, render: (_: any, r: Json) => (
                      <Button size="small" type="link" onClick={() => {
                        setRunId(r.id); setParams({ run: String(r.id) }, { replace: true });
                      }}>查看</Button>) },
                  ]} />
              ),
            }]} />
          )}
        </Space>
      </Card>

      {/* ============================ ② 镜头与提示词 ============================ */}
      <Card size="small" title="镜头与提示词"
        extra={<Typography.Text type="secondary" style={{ fontSize: 12 }}>
          正常情况下不用管它 —— 只有某一镜跑偏时才需要逐镜看/改
        </Typography.Text>}>
        <Collapse ghost items={[{
          key: 'shots',
          label: `镜头明细（${shots.length} 镜，已出片 ${ready}）`,
          children: (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Table size="small" rowKey="id" dataSource={shots} pagination={false}
                columns={[
                  { title: '镜号', dataIndex: 'code', width: 80 },
                  { title: '时长', dataIndex: 'duration_sec', width: 76,
                    render: (v: number) => `${v}s` },
                  { title: '帧数', dataIndex: 'target_frames', width: 76 },
                  { title: 'lint', dataIndex: 'lint_score', width: 90,
                    render: (v: number) => <LintTag score={v} /> },
                  { title: '起点(onset)', dataIndex: 'onset_sec', width: 100,
                    render: (v?: number) => (v === undefined || v === null)
                      ? <Typography.Text type="secondary">未实测</Typography.Text>
                      : `${v.toFixed(3)}s` },
                  { title: '状态', dataIndex: 'status', width: 110,
                    render: (v: string) => <StatusTag status={v} /> },
                  { title: '产物', dataIndex: 'video_path', ellipsis: true,
                    render: (v?: string) => (v
                      ? <a href={fileUrl(v)} target="_blank" rel="noreferrer">打开</a>
                      : <Typography.Text type="secondary">-</Typography.Text>) },
                  { title: '操作', width: 270, render: (_: any, r: Json) => (
                    <Space size={4} wrap>
                      <Button size="small" onClick={() => openShot(r)}>编辑提示词</Button>
                      <Button size="small" loading={shotBusy === r.id}
                        onClick={() => renderShot(r.id, true)}>试算</Button>
                      <Button size="small" type="primary" loading={shotBusy === r.id}
                        onClick={() => renderShot(r.id, false)}>重出</Button>
                      <Button size="small" danger onClick={() => {
                        modal.confirm({
                          title: `删除镜头 ${r.code}？`,
                          content: '只删分镜记录，磁盘上的产物保留。',
                          okButtonProps: { danger: true },
                          onOk: async () => {
                            try { await api.deleteShot(r.id); void loadShots(); }
                            catch (e) { message.error((e as Error).message); }
                          },
                        });
                      }}>删除</Button>
                    </Space>) },
                ]} />

              {/* 参考图：**按实际发送顺序**展示。一排缩略图看不出谁先谁后，
                  而 ref_image_0 是首帧、会主导整镜画面 —— 必须标出来。 */}
              <Card size="small" title="i2v 参考图（按实际发送顺序）"
                extra={<Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  出片时自动作为 ref_image_N；纯产品镜不带主播图，避免人物入空镜
                </Typography.Text>}>
                {(() => {
                  const hasAny = (refs.product || []).length + (refs.talent || []).length > 0;
                  if (!hasAny) {
                    return (
                      <Alert type="warning" showIcon message="未找到可用的参考图"
                        description="商品图来自「商品库」，主播图来自「主播库」的参考图（正面 / 三视图 / 定妆照 / 换装）。缺失时出片会退化为纯文生视频。" />
                    );
                  }
                  const seqBlock = (title: string, seq: Json[] | undefined) => {
                    if (!seq || !seq.length) return null;
                    return (
                      <div key={title} style={{ marginBottom: 10 }}>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {title}
                        </Typography.Text>
                        <Space wrap size={10} style={{ marginTop: 6 }}>
                          {seq.map((r: Json, i: number) => (
                            <div key={i} style={{ textAlign: 'center', width: 104 }}>
                              <a href={fileUrl(r.path)} target="_blank" rel="noreferrer">
                                <img src={fileUrl(r.path)} alt={r.label}
                                  style={{
                                    width: 96, height: 96, objectFit: 'cover', borderRadius: 6,
                                    border: r.is_first_frame
                                      ? '2px solid var(--cf-accent, #1677ff)'
                                      : '1px solid var(--cf-border)',
                                  }} />
                              </a>
                              <div style={{ marginTop: 2, fontSize: 11 }}>
                                <Tag color={r.is_first_frame ? 'blue' : 'default'}
                                  style={{ marginInlineEnd: 0, fontSize: 10, lineHeight: '16px' }}>
                                  {r.slot}{r.is_first_frame ? '（首帧）' : ''}
                                </Tag>
                              </div>
                              <div style={{
                                fontSize: 11, color: 'var(--cf-text-dim)',
                                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                              }}>{r.label || r.kind}</div>
                            </div>
                          ))}
                        </Space>
                      </div>
                    );
                  };
                  return (
                    <>
                      {refs.first_frame_note && (
                        <Alert type="info" showIcon style={{ marginBottom: 10 }}
                          message="「首帧」是哪个，由顺序决定"
                          description={refs.first_frame_note} />
                      )}
                      {(refs.dropped_count ?? 0) > 0 && (
                        <Alert type="warning" showIcon style={{ marginBottom: 10 }}
                          message={`有 ${refs.dropped_count} 张图不会发给模型`}
                          description={
                            <ul style={{ margin: 0, paddingLeft: 18 }}>
                              {(refs.dropped || []).map((d: Json, i: number) => (
                                <li key={i}>{d.label || d.kind}：{d.reason}</li>
                              ))}
                            </ul>
                          } />
                      )}
                      {seqBlock('纯产品镜（提示词里没有人物）',
                        refs.sequence_product_only)}
                      {(refs.talent || []).length > 0 && seqBlock(
                        '人物镜（提示词里出现人物）',
                        refs.sequence_with_talent)}
                    </>
                  );
                })()}
              </Card>
            </Space>
          ),
        }]} />
      </Card>

      {/* ============================ ③ 自检与合规 ============================ */}
      <Card size="small" title="交付前自检（帧数 / 音轨 / 电平 / 出片质检）">
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap>
            <Input style={{ width: 420 }} addonBefore="成片"
              placeholder="留空则取最近一次成片"
              value={verifyPath} onChange={(e) => setVerifyPath(e.target.value)} />
            <InputNumber addonBefore="期望帧数" value={frames ?? undefined}
              onChange={(v) => setFrames(v ?? null)} />
            <Button type="primary" onClick={verify}>自检</Button>
            <Space size={4}>
              <Switch size="small" checked={withQc} onChange={setWithQc} />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                含出片质检（解帧，稍慢；查黑场/冻结/无运动/元素未出现）
              </Typography.Text>
            </Space>
          </Space>
          {verifyRes && (
            <>
              <Descriptions size="small" column={3} bordered>
                <Descriptions.Item label="帧数">{verifyRes.nb_frames}
                  {frames && verifyRes.nb_frames !== frames ? ' ✗' : ' ✓'}</Descriptions.Item>
                <Descriptions.Item label="时长">{verifyRes.duration}s</Descriptions.Item>
                <Descriptions.Item label="fps">{verifyRes.fps}</Descriptions.Item>
                <Descriptions.Item label="分辨率">
                  {(verifyRes.size || []).join('x')}</Descriptions.Item>
                <Descriptions.Item label="有音轨">
                  {verifyRes.has_audio ? '是' : '否'}</Descriptions.Item>
                <Descriptions.Item label="平均电平">
                  {verifyRes.loudness?.mean_volume_db?.toFixed?.(1)} dB
                </Descriptions.Item>
              </Descriptions>
              {verifyRes.qc?.counts && (
                <Space size={16}>
                  <Typography.Text type="secondary">出片质检：</Typography.Text>
                  <Tag color="red">高危 {verifyRes.qc.counts.high}</Tag>
                  <Tag color="orange">需复核 {verifyRes.qc.counts.medium}</Tag>
                  <Tag color="gold">提示 {verifyRes.qc.counts.low}</Tag>
                </Space>
              )}
              {(verifyRes.issues || []).map((s: string, i: number) => (
                <Typography.Text key={i} type="danger"><div>· {s}</div></Typography.Text>))}
              {verifyPath && (
                <video src={fileUrl(verifyPath)} controls
                  style={{ width: 320, background: '#000' }} />
              )}
            </>
          )}
        </Space>
      </Card>

      <QualityPanel pid={pid} />

      <CompliancePanel pid={pid} />

      {/* ============================ ④ 高级：对账与排查 ============================ */}
      <Card size="small" title="高级（对账 / 排查）">
        <Collapse
          items={[
            {
              key: 'cost', label: '成本',
              children: <Space direction="vertical" size={12} style={{ width: '100%' }}>
                <Row gutter={16}>
                  <Col span={4}><Statistic title="总成本" value={cost.project_cost_cny ?? 0}
                    precision={2} prefix="¥" /></Col>
                  <Col span={4}><Statistic title="任务数" value={cost.jobs ?? 0} /></Col>
                  <Col span={4}><Statistic title="成功" value={cost.jobs_succeeded ?? 0} /></Col>
                  <Col span={4}><Statistic title="复用（省钱）" value={cost.jobs_reused ?? 0} /></Col>
                  <Col span={4}><Statistic title="失败" value={cost.jobs_failed ?? 0} /></Col>
                  <Col span={4}><Statistic title="试算" value={cost.jobs_dry_run ?? 0} /></Col>
                </Row>
                <Table size="small" rowKey="kind" dataSource={cost.by_kind || []} pagination={false}
                  columns={[
                    { title: '类型', dataIndex: 'kind', render: (v: string) => <Tag>{v}</Tag> },
                    { title: '金额', dataIndex: 'amount_cny',
                      render: (v: number) => `¥${(v || 0).toFixed(4)}` },
                    { title: '用量（秒/token）', dataIndex: 'quantity' },
                  ]} />
                <BudgetPanel px={pid} editable={false} title="花费与上限" />
              </Space>,
            },
            {
              key: 'scripts',
              label: `脚本素材（${scripts.length} 版 —— 脚本是成片的事实来源）`,
              children: (
                <Table size="small" rowKey="id" dataSource={scripts} pagination={false}
                  locale={{ emptyText: '还没有脚本素材 —— 用「生成视频提示词」产出或粘贴导入后会自动留存' }}
                  columns={[
                    { title: 'ID', dataIndex: 'id', width: 60 },
                    { title: '来源', dataIndex: 'source_label', width: 110,
                      render: (v: string) => <Tag>{v || '—'}</Tag> },
                    { title: '标题', dataIndex: 'title', ellipsis: true },
                    { title: '镜数', dataIndex: 'shot_count', width: 70 },
                    { title: '时长', dataIndex: 'total_sec', width: 80,
                      render: (v: number) => (v ? `${v}s` : '—') },
                    { title: '念白行', dataIndex: 'vo_row_count', width: 80 },
                    { title: '时间', dataIndex: 'created_at', width: 170,
                      render: (v: string) => (v ? v.replace('T', ' ').slice(0, 19) : '') },
                    { title: '全文', width: 70, render: (_: any, r: Json) => (
                      <a href={fileUrl(r.path)} target="_blank" rel="noreferrer">打开</a>) },
                  ]} />
              ),
            },
            {
              key: 'tracks', label: `音轨与产物（${tracks.length}）`,
              children: (
                <Table size="small" rowKey="id" dataSource={tracks} pagination={false}
                  columns={[
                    { title: 'ID', dataIndex: 'id', width: 60 },
                    { title: '类型', dataIndex: 'kind', width: 110,
                      render: (v: string) => <Tag>{v}</Tag> },
                    { title: '路径', dataIndex: 'path', ellipsis: true },
                    { title: '预览', width: 70, render: (_: any, r: Json) => (
                      <a href={fileUrl(r.path)} target="_blank" rel="noreferrer">打开</a>) },
                  ]} />
              ),
            },
            {
              key: 'jobs', label: '任务明细（逐笔出片）',
              children: (
                <Table size="small" rowKey="id" dataSource={cost.detail || []}
                  pagination={{ pageSize: 10 }}
                  columns={[
                    { title: 'ID', dataIndex: 'id', width: 60 },
                    { title: '类型', dataIndex: 'kind', width: 80 },
                    { title: '供应商', dataIndex: 'provider_id', width: 170 },
                    { title: '状态', dataIndex: 'status', width: 100,
                      render: (v: string) => <StatusTag status={v} /> },
                    { title: '费用', dataIndex: 'cost_cny', width: 100,
                      render: (v: number, r: Json) => (r.dry_run
                        ? <Tag>试算</Tag> : `¥${(v || 0).toFixed(3)}`) },
                    { title: '时间', dataIndex: 'created_at', width: 180 },
                  ]} />
              ),
            },
            {
              key: 'more', label: '更多入口',
              children: (
                <Space wrap>
                  <Button size="small" onClick={() => nav('/tasks')}>
                    任务台账（计费安全 · 全系统）
                  </Button>
                  <Button size="small" onClick={() => nav(`/projects/${pid}/audio`)}>
                    音频后期（手工调念白 / BGM / 音效）
                  </Button>
                  <Button size="small" onClick={async () => {
                    try {
                      const r = await api.unpublishHosts(pid);
                      message.success(`关闭租约 ${r.closed_leases} 条`);
                    } catch (e) { message.error((e as Error).message); }
                  }}>收图床（关闭参考图公网租约）</Button>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      {/* ============================ 编辑提示词抽屉 ============================ */}
      <Drawer width={760} open={!!drawer} title={`编辑提示词 · ${drawer?.code || ''}`}
        onClose={() => setDrawer(null)}
        extra={<Space>
          <Button size="small" onClick={relint}>实时 lint</Button>
          <Button size="small" type="primary" loading={saving} onClick={saveShot}>保存</Button>
        </Space>}>
        {drawer && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Typography.Text type="secondary">
              分层顺序：[SHOT][HERO][MOTION][CAMERA][LIGHT][PHYSICS][TEXTURE][REF][NEG]
              —— 视频模型对开头 token 权重更高，运动写前面。
            </Typography.Text>
            <Input.TextArea rows={18} value={text} onChange={(e) => setText(e.target.value)}
              style={{ fontFamily: 'ui-monospace, Menlo, Consolas, monospace', fontSize: 12 }} />
            {lint && (
              <Card size="small" title={`lint ${lint.total ?? '?'}/14（闸门 ${lint.min_score ?? MIN_SCORE}）`}>
                <Space direction="vertical" size={4} style={{ width: '100%' }}>
                  {(lint.issues || []).map((s: string, i: number) => (
                    <Typography.Text key={i} type="danger">· {s}</Typography.Text>))}
                  {(lint.notes || []).map((s: string, i: number) => (
                    <Typography.Text key={i} type="secondary">· {s}</Typography.Text>))}
                  {!(lint.issues || []).length && !(lint.notes || []).length &&
                    <Typography.Text type="success">无扣分点</Typography.Text>}
                </Space>
              </Card>
            )}
            <Typography.Text type="secondary">
              中文直译：{drawer.prompt_zh || '（无）'}
            </Typography.Text>
          </Space>
        )}
      </Drawer>
    </Space>
  );
}
