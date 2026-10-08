import { useCallback, useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  App as AntApp, Alert, Button, Card, Col, Collapse, Descriptions, Input,
  InputNumber, Row, Select, Space, Table, Tag, Typography,
} from 'antd';
import { api, type Json } from '../api';
import { StatusTag, CompliancePanel } from '../components';

interface Row { start: number; end: number; text: string }

const VOICES = [
  { value: 'pt-BR-FranciscaNeural', label: 'pt-BR Francisca（暖调优雅）' },
  { value: 'es-MX-DaliaNeural', label: 'es-MX Dalia（拉美西语）' },
  { value: 'zh-CN-XiaoxiaoNeural', label: 'zh-CN Xiaoxiao（中文）' },
];

export default function AudioPage() {
  const { id } = useParams();
  const pid = Number(id);
  const nav = useNavigate();
  const { message } = AntApp.useApp();

  const [tl, setTl] = useState<Json>({});
  const [shots, setShots] = useState<Json[]>([]);
  const [tracks, setTracks] = useState<Json[]>([]);

  // 念白
  const [rows, setRows] = useState<Row[]>([
    { start: 0.0, end: 3.0, text: '' },
  ]);
  const [rawText, setRawText] = useState('');
  const [voice, setVoice] = useState('pt-BR-FranciscaNeural');
  const [rawPath, setRawPath] = useState('');
  const [rawDur, setRawDur] = useState<number>(0);
  const [plan, setPlan] = useState<Json | null>(null);

  // BGM / SFX
  const [bgm, setBgm] = useState<Json>({ root: 'F3', level_db: -20, split: undefined, root2: 'F3', minor2: true });
  const [sfxKind, setSfxKind] = useState('mist');
  const [sfxPath, setSfxPath] = useState('');
  const [cues, setCues] = useState('');

  // 混音
  const [videoPath, setVideoPath] = useState('');
  const [voicePath, setVoicePath] = useState('');
  const [bgmPath, setBgmPath] = useState('');
  const [verifyPath, setVerifyPath] = useState('');
  const [expectFrames, setExpectFrames] = useState<number | null>(null);
  const [verifyRes, setVerifyRes] = useState<Json | null>(null);

  const load = useCallback(async () => {
    try {
      const [t, s, tr] = await Promise.all([
        api.timeline(pid), api.listShots(pid), api.tracks(pid),
      ]);
      setTl(t); setShots(s); setTracks(tr);
      const v = tr.find((x: Json) => x.kind === 'voice');
      const vr = tr.find((x: Json) => x.kind === 'voice_raw');
      const b = tr.find((x: Json) => x.kind === 'bgm');
      const f = tr.find((x: Json) => x.kind === 'final');
      if (v) setVoicePath(v.path);
      if (vr) { setRawPath(vr.path); setRawDur(vr.meta?.duration || 0); }
      if (b) setBgmPath(b.path);
      if (f) setVerifyPath(f.path);
    } catch (e) { message.error((e as Error).message); }
  }, [pid, message]);

  useEffect(() => { load(); }, [load]);

  const total = Number(tl.total_sec || 0);

  const doRaw = async () => {
    try {
      const r = await api.narrationRaw(pid, { text: rawText, voice });
      setRawPath(r.path); setRawDur(r.duration);
      message.success(`原始音轨已生成（音色 ${r.voice}${r.voice_is_fallback ? '，已兜底' : ''}）`);
      load();
    } catch (e) { message.error((e as Error).message); }
  };

  const doPlan = async () => {
    try {
      const r = await api.narrationPlan(pid, { rows, total, raw_dur: rawDur });
      setPlan(r);
      const tempo = Number(r.tempo ?? r.atempo ?? 1);
      message[tempo > 1.02 ? 'warning' : 'success'](
        tempo > 1.02 ? `atempo ${tempo.toFixed(3)} > 1.02：念白会发赶，加字数而不是加放慢倍率`
          : `atempo ${tempo.toFixed(3)} 正常`);
    } catch (e) { message.error((e as Error).message); }
  };

  const doNarration = async () => {
    try {
      const r = await api.narration(pid, { rows, total, raw_wav: rawPath || undefined, voice });
      setVoicePath(r.path);
      message.success('念白音轨已落位');
      load();
    } catch (e) { message.error((e as Error).message); }
  };

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card size="small" title={`音频后期 · #${pid}`}
        extra={<Space>
          <Button size="small" type="primary"
            onClick={() => nav(`/projects/${pid}/deliver`)}>返回成片页</Button>
        </Space>}>
        <Descriptions size="small" column={4} bordered>
          <Descriptions.Item label="总帧数">{tl.total_frames ?? 0}</Descriptions.Item>
          <Descriptions.Item label="总时长">{total.toFixed(3)}s</Descriptions.Item>
          <Descriptions.Item label="镜头数">{(tl.shots || []).length}</Descriptions.Item>
          <Descriptions.Item label="fps">{tl.fps ?? 24}</Descriptions.Item>
        </Descriptions>
      </Card>

      <Collapse defaultActiveKey={['voice']} items={[
        {
          key: 'voice',
          label: '① 念白（整条一次 TTS + 单一全局 atempo）',
          children: (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Alert type="info" showIcon
                message="atempo 是全局系数：一行超窗会把整条念白拖快"
                description="判断标准：atempo > 1.02 就是没配好。此时应加文案字数（贴片变长），而不是调音色或加放慢倍率。" />
              <Table size="small" rowKey={(_, i) => String(i)} dataSource={rows} pagination={false}
                columns={[
                  { title: '起(s)', dataIndex: 'start', width: 110, render: (v: number, _r: Row, i: number) => (
                    <InputNumber size="small" step={0.1} min={0} value={v}
                      onChange={(n) => { const c = [...rows]; c[i].start = Number(n); setRows(c); }} />) },
                  { title: '止(s)', dataIndex: 'end', width: 110, render: (v: number, _r: Row, i: number) => (
                    <InputNumber size="small" step={0.1} min={0} value={v}
                      onChange={(n) => { const c = [...rows]; c[i].end = Number(n); setRows(c); }} />) },
                  { title: '文案（按投放市场语言写，不做逐字互译）', dataIndex: 'text',
                    render: (v: string, _r: Row, i: number) => (
                      <Input size="small" value={v}
                        onChange={(e) => { const c = [...rows]; c[i].text = e.target.value; setRows(c); }} />) },
                  { title: '', width: 60, render: (_: any, __r: Row, i: number) => (
                    <Button size="small" danger onClick={() => setRows(rows.filter((_, j) => j !== i))}>删</Button>) },
                ]} />
              <Space wrap>
                <Button size="small" onClick={() => setRows([...rows, { start: total > 0 ? rows.length * 3 : 0, end: 0, text: '' }])}>加一行</Button>
                <Button size="small" onClick={() => setRawText(rows.map((r) => r.text).filter(Boolean).join(' '))}>
                  用文案生成原始文本
                </Button>
                <Select style={{ width: 240 }} value={voice} onChange={setVoice} options={VOICES} />
              </Space>
              <CompliancePanel pid={pid} rows={rows} title="念白文案合规自检（广告法）" />
              <Input.TextArea rows={3} value={rawText} onChange={(e) => setRawText(e.target.value)}
                placeholder="整条念白文本（一次 TTS 请求，句间停顿靠静音插入，音色零漂移）" />
              <Space wrap>
                <Button type="primary" onClick={doRaw}>合成原始音轨（联网 TTS）</Button>
                <Input style={{ width: 320 }} placeholder="或复用已有原始音轨路径 raw_wav"
                  value={rawPath} onChange={(e) => setRawPath(e.target.value)} />
                <InputNumber addonBefore="原始时长(s)" step={0.1} value={rawDur || undefined}
                  onChange={(v) => setRawDur(Number(v))} />
              </Space>
              <Space wrap>
                <Button onClick={doPlan}>算 atempo 计划（不联网不产出）</Button>
                <Button type="primary" onClick={doNarration}>生成念白音轨</Button>
                <Typography.Text type="secondary">当前念白轨：{voicePath || '—'}</Typography.Text>
              </Space>
              {plan && (
                <Card size="small" title={`atempo ${Number(plan.tempo ?? plan.atempo ?? 1).toFixed(3)} · 模式 ${plan.mode ?? '-'}`}>
                  <Space direction="vertical" size={4} style={{ width: '100%' }}>
                    {(plan.warnings || []).map((s: string, i: number) => (
                      <Typography.Text key={i} type="danger">· {s}</Typography.Text>))}
                    {(plan.rows || []).length > 0 && (
                      <Table size="small" rowKey="line" dataSource={plan.rows} pagination={false}
                        columns={[
                          { title: '行', dataIndex: 'line', width: 50 },
                          { title: '窗口', width: 120,
                            render: (_: any, r: Json) => `${r.start}~${r.end}s（可用 ${r.window ?? '-'}）` },
                          { title: '预计占用', dataIndex: 'est_sec', width: 100,
                            render: (v: number, r: Json) => v != null ? `${v}s` : `${r.placed_sec ?? '-'}s` },
                          { title: '超出', dataIndex: 'over_by', width: 80,
                            render: (v: number) => v ? <Tag color="red">+{v}s</Tag> : '-' },
                          { title: '提示', dataIndex: 'flag', ellipsis: true },
                        ]} />
                    )}
                  </Space>
                </Card>
              )}
            </Space>
          ),
        },
        {
          key: 'bgm',
          label: '② 垫乐（本地合成，昼夜可分段）',
          children: (
            <Space wrap>
              <Input addonBefore="根音" style={{ width: 150 }} value={String(bgm.root)}
                onChange={(e) => setBgm({ ...bgm, root: e.target.value })} />
              <InputNumber addonBefore="电平(dB)" step={1} value={bgm.level_db}
                onChange={(v) => setBgm({ ...bgm, level_db: Number(v) })} />
              <InputNumber addonBefore="分段点(s)" step={0.5} value={bgm.split as any}
                onChange={(v) => setBgm({ ...bgm, split: v ?? undefined })}
                placeholder="昼夜交界" />
              <Input addonBefore="第二段根音" style={{ width: 180 }} value={String(bgm.root2)}
                onChange={(e) => setBgm({ ...bgm, root2: e.target.value })} />
              <Select value={bgm.minor2 ? 1 : 0} style={{ width: 150 }}
                onChange={(v) => setBgm({ ...bgm, minor2: v === 1 })}
                options={[{ value: 1, label: '第二段小调' }, { value: 0, label: '第二段大调' }]} />
              <Button type="primary" onClick={async () => {
                try {
                  const r = await api.bgm(pid, { duration: total, ...bgm });
                  setBgmPath(r.path); message.success('垫乐已生成'); load();
                } catch (e) { message.error((e as Error).message); }
              }}>生成垫乐（{total.toFixed(2)}s）</Button>
              <Typography.Text type="secondary">当前：{bgmPath || '—'}</Typography.Text>
            </Space>
          ),
        },
        {
          key: 'sfx',
          label: '③ 音效（onset 实测落位）',
          children: (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Alert type="warning" showIcon
                message="换提示词后必须重测起点"
                description="模型对雾/飞溅有前摇，且前摇随提示词变化（实测 0.72s ~ 1.10s）。沿用旧数会让音效落在画面还在发呆的地方。" />
              <Table size="small" rowKey="id" dataSource={shots} pagination={false}
                columns={[
                  { title: '镜号', dataIndex: 'code', width: 90 },
                  { title: 'onset', dataIndex: 'onset_sec', width: 110,
                    render: (v?: number) => v == null
                      ? <Typography.Text type="secondary">未实测</Typography.Text>
                      : <Tag color="blue">{Number(v).toFixed(3)}s</Tag> },
                  { title: '产物', dataIndex: 'video_path', ellipsis: true },
                  { title: '操作', width: 120, render: (_: any, r: Json) => (
                    <Button size="small" disabled={!r.video_path} onClick={async () => {
                      try {
                        const res = await api.onset(r.id, { fps: tl.fps ?? 24 });
                        message.success(`${r.code} 起点 ${res.onset_sec ?? '未检出'}s`);
                        load();
                      } catch (e) { message.error((e as Error).message); }
                    }}>实测起点</Button>) },
                ]} />
              <Space wrap>
                <Select value={sfxKind} onChange={setSfxKind} style={{ width: 160 }}
                  options={[
                    { value: 'mist', label: 'mist 纯水汽（默认）' },
                    { value: 'click', label: 'click 咔哒' },
                    { value: 'glass', label: 'glass 玻璃轻碰' },
                    { value: 'breath', label: 'breath 吸气' },
                  ]} />
                <Button onClick={async () => {
                  try {
                    const r = await api.sfx(pid, { kind: sfxKind });
                    setSfxPath(r.path); message.success('音效已合成'); load();
                  } catch (e) { message.error((e as Error).message); }
                }}>合成音效</Button>
                <Button onClick={async () => {
                  try {
                    const r = await api.sfxPlan(pid, { fps: tl.fps ?? 24, total });
                    setCues((r.cues || []).map((c: Json) => `${sfxPath || '<sfx路径>'}@${c.at}`).join('\n'));
                    message.success(r.no_onset?.length ? `有 ${r.no_onset.length} 镜未测起点` : '已推导落位');
                  } catch (e) { message.error((e as Error).message); }
                }}>推导落位</Button>
              </Space>
              <Input.TextArea rows={3} value={cues} onChange={(e) => setCues(e.target.value)}
                placeholder="每行一条：音效路径@秒数，例如 C:/…/mist.wav@3.33" />
            </Space>
          ),
        },
        {
          key: 'mix',
          label: '④ 混音与校验（闪避 + limiter + -14 LUFS）',
          children: (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Row gutter={8}>
                <Col span={12}><Input addonBefore="底片" placeholder="装配后的视频路径"
                  value={videoPath} onChange={(e) => setVideoPath(e.target.value)} /></Col>
                <Col span={12}><Input addonBefore="念白" placeholder="voice.wav"
                  value={voicePath} onChange={(e) => setVoicePath(e.target.value)} /></Col>
              </Row>
              <Row gutter={8}>
                <Col span={12}><Input addonBefore="垫乐" placeholder="bgm.wav（可空）"
                  value={bgmPath} onChange={(e) => setBgmPath(e.target.value)} /></Col>
                <Col span={12}><InputNumber addonBefore="期望帧数" style={{ width: '100%' }}
                  value={expectFrames ?? undefined} onChange={(v) => setExpectFrames(v ?? null)} /></Col>
              </Row>
              <Space wrap>
                <Button type="primary" onClick={async () => {
                  try {
                    const r = await api.mix(pid, {
                      video: videoPath, voice: voicePath, bgm: bgmPath || undefined,
                      sfx: cues.split('\n').map((s) => s.trim()).filter(Boolean),
                    });
                    setVerifyPath(r.path); message.success(`成片已输出：${r.path}`); load();
                  } catch (e) { message.error((e as Error).message); }
                }}>混音出成片</Button>
                <Button onClick={async () => {
                  try {
                    const r = await api.verify(pid, {
                      path: verifyPath || undefined,
                      expected_frames: expectFrames ?? undefined,
                      fps: tl.fps ?? 24,
                    });
                    setVerifyRes(r);
                    message[r.ok ? 'success' : 'warning'](r.ok ? '交付自检通过' : '自检有问题');
                  } catch (e) { message.error((e as Error).message); }
                }}>交付自检</Button>
                <Button onClick={async () => {
                  try {
                    const r = await api.runFinal(pid, {
                      total_frames: expectFrames ?? tl.total_frames,
                      fps: tl.fps ?? 24,
                      rows: rows.filter((x) => x.text),
                      raw_wav: rawPath || undefined,
                      voice,
                      bgm_duration: total,
                      bgm_split: bgm.split ?? undefined,
                      bgm_root2: bgm.root2 ?? undefined,
                      sfx_kind: sfxKind,
                      auto_sfx: true,
                    });
                    message.success('一键后期完成');
                    setVerifyPath(r.final_path || verifyPath);
                    load();
                  } catch (e) { message.error((e as Error).message); }
                }}>一键跑通整条后期</Button>
              </Space>
              {verifyRes && (
                <Card size="small" title={verifyRes.ok ? '自检通过' : '自检未通过'}>
                  <Descriptions size="small" column={3} bordered>
                    <Descriptions.Item label="帧数">{verifyRes.nb_frames}</Descriptions.Item>
                    <Descriptions.Item label="时长">{verifyRes.duration}s</Descriptions.Item>
                    <Descriptions.Item label="分辨率">{(verifyRes.size || []).join('x')}</Descriptions.Item>
                    <Descriptions.Item label="有音轨">{verifyRes.has_audio ? '是' : '否'}</Descriptions.Item>
                    <Descriptions.Item label="平均电平">
                      {verifyRes.loudness?.mean_volume_db?.toFixed?.(1)} dB
                    </Descriptions.Item>
                    <Descriptions.Item label="响度">
                      {verifyRes.loudness?.integrated?.toFixed?.(1)} LUFS
                    </Descriptions.Item>
                  </Descriptions>
                  {(verifyRes.issues || []).map((s: string, i: number) => (
                    <Typography.Text key={i} type="danger"><div>· {s}</div></Typography.Text>))}
                </Card>
              )}
            </Space>
          ),
        },
        {
          key: 'tracks',
          label: '⑤ 音轨清单',
          children: (
            <Table size="small" rowKey="id" dataSource={tracks} pagination={false}
              columns={[
                { title: 'ID', dataIndex: 'id', width: 60 },
                { title: '类型', dataIndex: 'kind', width: 110, render: (v: string) => <Tag>{v}</Tag> },
                { title: '路径', dataIndex: 'path', ellipsis: true },
                { title: '声音/参数', dataIndex: 'meta', width: 220,
                  render: (m: Json) => m?.voice ? `${m.voice}${m.voice_is_fallback ? '(兜底)' : ''}` : '' },
              ]} />
          ),
        },
      ]} />
    </Space>
  );
}
