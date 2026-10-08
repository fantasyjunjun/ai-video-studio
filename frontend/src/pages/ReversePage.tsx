import { useCallback, useEffect, useState } from 'react';
import {
  App as AntApp, Alert, Button, Card, Col, Descriptions, Input, InputNumber, Row,
  Space, Switch, Table, Tag, Typography, Upload,
} from 'antd';
import { UploadOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { api, fileUrl, type Json } from '../api';
import { Sparkline } from '../components';

export default function ReversePage() {
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const [path, setPath] = useState('');
  const [sample, setSample] = useState(2);
  const [note, setNote] = useState('');
  const [useLlm, setUseLlm] = useState(true);
  /** 看图反推（R-32 默认开）：把抽帧拼图交给视觉通道，才能反推出**逐镜提示词**；
   *  关掉只在"只想看实测数据"或当前模型不支持看图时用。 */
  const [useVision, setUseVision] = useState(true);
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<Json | null>(null);
  const [reports, setReports] = useState<Json[]>([]);
  const [revModel, setRevModel] = useState('');

  const load = useCallback(async () => {
    try { setReports(await api.reverseReports()); }
    catch (e) { message.error((e as Error).message); }
  }, [message]);

  useEffect(() => {
    load();
    api.settings().then((s) => setRevModel((s.reverse_vision_model as string) || ''))
      .catch(() => {});
  }, [load]);

  const run = async (upload?: File) => {
    setBusy(true);
    try {
      const r = upload
        ? await api.reverseUpload(upload,
            { sample_fps: sample, note, use_llm: useLlm, use_vision: useVision })
        : await api.reverse({ path, sample_fps: sample, note, use_llm: useLlm,
            use_vision: useVision });
      setRes(r);
      message.success(`反推完成（报告 #${r.report_id}）`);
      load();
    } catch (e) { message.error((e as Error).message); }
    finally { setBusy(false); }
  };

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card size="small" title="参考片反推"
        extra={<Typography.Text type="secondary">
          本地量测零成本；关掉 LLM 就只出数据与切点。看图反推会多看到画面，
          才能反推出逐镜提示词
        </Typography.Text>}>
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap>
            <Input style={{ width: 480 }} placeholder="本机参考片绝对路径"
              value={path} onChange={(e) => setPath(e.target.value)} />
            <InputNumber addonBefore="采样(fps)" min={0.5} max={10} step={0.5} value={sample}
              onChange={(v) => setSample(Number(v))} />
            <Space size={4}>
              <Typography.Text>LLM 判定</Typography.Text>
              <Switch checked={useLlm} onChange={setUseLlm} />
            </Space>
            <Space size={4}>
              <Typography.Text>看图反推</Typography.Text>
              <Switch checked={useVision} disabled={!useLlm} onChange={setUseVision} />
            </Space>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              视觉模型：{revModel || '当前 LLM 模型'}（
              <Typography.Link onClick={() => navigate('/settings#reverse-settings')}>
                设置 → 反推设置
              </Typography.Link>
              可覆盖为更快的视觉模型、并调大输出上限）
            </Typography.Text>
            <Button type="primary" loading={busy} disabled={!path} onClick={() => run()}>
              开始反推
            </Button>
            <Upload beforeUpload={(f) => { run(f); return false; }} showUploadList={false}
              disabled={busy}>
              <Button icon={<UploadOutlined />} loading={busy}>
                {busy ? '反推中…（约十几秒）' : '上传参考片'}
              </Button>
            </Upload>
          </Space>
          <Input.TextArea rows={2} placeholder="补充说明（例：这条是夜戏，关注喷香水那一段）"
            value={note} onChange={(e) => setNote(e.target.value)} />
        </Space>
      </Card>

      {res && (
        <>
          <Card size="small" title="实测规格">
            <Descriptions size="small" column={4} bordered>
              <Descriptions.Item label="分辨率">{res.spec?.width}x{res.spec?.height}</Descriptions.Item>
              <Descriptions.Item label="fps">{res.spec?.fps}</Descriptions.Item>
              <Descriptions.Item label="时长">{res.spec?.duration}s</Descriptions.Item>
              <Descriptions.Item label="帧数">{res.spec?.nb_frames}</Descriptions.Item>
            </Descriptions>
          </Card>

          <Row gutter={16}>
            <Col span={8}>
              <Card size="small" title="抽帧拼图">
                {res.sheet_path
                  ? <img src={fileUrl(res.sheet_path)} alt="sheet" style={{ width: '100%' }} />
                  : <Typography.Text type="secondary">未生成</Typography.Text>}
              </Card>
            </Col>
            <Col span={16}>
              <Card size="small" title="逐帧亮度（红虚线=疑似切点）">
                <Sparkline values={(res.metrics || []).map((m: Json) => m.yavg)}
                  marks={(res.cut_candidates || []).map((c: number) =>
                    c / Math.max(0.001, Number(res.spec?.duration || 1)))} />
                <Typography.Text type="secondary">
                  亮度 {(res.metrics || []).length} 点 · 切点 {(res.cut_candidates || []).length} 个
                </Typography.Text>
                <div style={{ marginTop: 8 }}>
                  {(res.cut_candidates || []).map((c: number, i: number) => (
                    <Tag key={i} color="volcano">{Number(c).toFixed(2)}s</Tag>))}
                </div>
              </Card>
            </Col>
          </Row>

          {useLlm && (
            <Card size="small" title="三档判定（铁律 23：照单全收必翻车）">
              <Row gutter={16}>
                {[
                  ['可直接采纳', res.findings?.adopt || [], 'green'],
                  ['须改造', res.findings?.adapt || [], 'gold'],
                  ['不可采纳', res.findings?.reject || [], 'red'],
                ].map(([title, list, color]) => (
                  <Col span={8} key={String(title)}>
                    <Card size="small" title={<Typography.Text style={{ color }}>
                      {title}（{list.length}）
                    </Typography.Text>}>
                      {(list as string[]).length
                        ? (list as string[]).map((s, i) => <div key={i}>· {s}</div>)
                        : <Typography.Text type="secondary">无</Typography.Text>}
                    </Card>
                  </Col>
                ))}
              </Row>
            </Card>
          )}

          <Card size="small" title="逐镜拆解与逆推提示词"
            extra={res.analysis?.vision ? (
              <Button size="small" onClick={() => {
                const all = (res.analysis?.shots || [])
                  .map((s: Json) => `# ${s.idx} ${s.start}~${s.end}s\n${s.prompt || ''}`)
                  .join('\n\n');
                void navigator.clipboard.writeText(all);
                message.success('已复制全部逐镜提示词');
              }}>复制全部提示词</Button>
            ) : null}>
            {res.analysis?.vision_note && (
              <Typography.Text type="warning" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                {res.analysis.vision_note}
              </Typography.Text>
            )}
            {res.analysis?.truncated && (res.analysis?.shots || []).length > 0 && (
              <Alert type="warning" showIcon style={{ marginBottom: 8 }}
                message="提示词不完整（已救回能解析的部分）"
                description={<span>
                  模型输出在写到一半时被截断，下面只有已写完的镜头。
                  去<Button type="link" size="small" style={{ padding: '0 4px' }}
                    onClick={() => navigate('/settings#reverse-settings')}>设置 → 反推设置</Button>
                  调大「单次输出上限」（如 12000~16000）或换更快的视觉模型后重跑即可拿全。
                </span>} />
            )}
            {!res.analysis?.vision && !res.analysis?.vision_note && (
              <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                这份结果没有逐镜提示词 —— 打开上面的「看图反推」重新拆解即可拿到（模型能真的看到画面）。
              </Typography.Text>
            )}
            {(res.analysis?.shots || []).length > 0 && (
              <div style={{ display: 'grid', gap: 10 }}>
                {(res.analysis?.shots || []).map((s: Json, i: number) => (
                  <div key={i} className="cf-card" style={{ padding: 12 }}>
                    <Space size={8} wrap style={{ marginBottom: 6 }}>
                      <Tag color="blue">#{s.idx ?? i + 1}</Tag>
                      <Typography.Text style={{ fontSize: 12 }}>{s.start}~{s.end}s</Typography.Text>
                      {s.shot_size && <Tag>{s.shot_size}</Tag>}
                      {s.camera && <Tag>{s.camera}</Tag>}
                      {s.light && <Tag>{s.light}</Tag>}
                      {s.retention && (
                        <Tag color={RETENTION_COLOR[s.retention as string] || 'default'}>
                          {s.retention}
                        </Tag>
                      )}
                    </Space>
                    {s.desc && (
                      <Typography.Paragraph style={{ fontSize: 12, marginBottom: 4 }}>
                        画面：{s.desc}
                      </Typography.Paragraph>
                    )}
                    {(s.result_elements || []).length > 0 && (
                      <Typography.Paragraph style={{ fontSize: 12, marginBottom: 4 }}>
                        结果性元素：{(s.result_elements || []).map((x: string, k: number) =>
                          <Tag key={k}>{x}</Tag>)}{' '}
                        成因：{(s.causes || []).length
                          ? (s.causes || []).map((x: string, k: number) =>
                              <Tag key={k} color="blue">{x}</Tag>)
                          : <Typography.Text type="warning">缺失</Typography.Text>}
                      </Typography.Paragraph>
                    )}
                    {s.why_effective && (
                      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 4 }}>
                        为什么有效：{s.why_effective}
                      </Typography.Paragraph>
                    )}
                    {s.prompt ? (
                      <>
                        <pre style={preStyle}>{s.prompt}</pre>
                        <Space size={8}>
                          <Button size="small" type="link" style={{ paddingLeft: 0 }}
                            onClick={() => {
                              void navigator.clipboard.writeText(String(s.prompt));
                              message.success('已复制');
                            }}>复制这一镜</Button>
                        </Space>
                        {s.prompt_zh && (
                          <Typography.Paragraph type="secondary"
                            style={{ fontSize: 12, whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                            中文直译：{s.prompt_zh}
                          </Typography.Paragraph>
                        )}
                      </>
                    ) : (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        这一镜没有反推出提示词
                      </Typography.Text>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>
        </>
      )}

      <Card size="small" title="历史报告">
        <Table size="small" rowKey="id" dataSource={reports} pagination={false}
          columns={[
            { title: 'ID', dataIndex: 'id', width: 60 },
            { title: '来源', dataIndex: 'source_path', ellipsis: true },
            { title: '时长', width: 90, render: (_: any, r: Json) => `${r.spec?.duration ?? '-'}s` },
            { title: '反推方式', width: 150, render: (_: any, r: Json) => (
              r.vision
                ? (<Tag color="green">看图（{r.shot_count || 0} 镜提示词）</Tag>)
                : <Tag>仅结构</Tag>) },
            { title: '完整度', width: 90, render: (_: any, r: Json) => (
              r.vision && r.truncated
                ? <Tag color="orange">不完整</Tag>
                : (r.vision ? <Tag color="blue">完整</Tag> : <Tag>-</Tag>)) },
            { title: '结论', dataIndex: 'findings', ellipsis: true },
            { title: '操作', width: 90, render: (_: any, r: Json) => (
              <Button size="small" onClick={() => setRes({
                // 报告详情统一挂在 analysis 下 —— 渲染统一读 res.analysis.*，
                // 这里若不包一层，载入历史报告会是一片空白（旧实现就是直接摊平）。
                analysis: { ...(r.structure || {}) },
                spec: r.spec, findings: parseFindings(r.findings),
              })}>载入</Button>) },
          ]} />
      </Card>
    </Space>
  );
}

/** 报告里 findings 是三档拼成的纯文本，载入时拆回去便于分栏展示。 */
function parseFindings(text: string) {
  const out = { adopt: [] as string[], adapt: [] as string[], reject: [] as string[] };
  for (const line of (text || '').split('\n')) {
    if (line.startsWith('[采纳]')) out.adopt.push(line.slice(4).trim());
    else if (line.startsWith('[改造]')) out.adapt.push(line.slice(4).trim());
    else if (line.startsWith('[不可采纳]')) out.reject.push(line.slice(7).trim());
  }
  return out;
}

/** 保留方式四档 → 颜色（与官方 `retention_analysis` 标记同名，R-32）。 */
const RETENTION_COLOR: Record<string, string> = {
  fully_preserved: 'green',
  partially_preserved: 'cyan',
  attribute_transfer: 'gold',
  weak_reference: 'default',
};

/** 提示词本体按原文展示（不折行截断）—— 反推的目的就是拿到可复用的原文。 */
const preStyle: React.CSSProperties = {
  background: 'rgba(0,0,0,0.28)', border: '1px solid var(--cf-border)',
  borderRadius: 8, padding: 10, fontSize: 12, lineHeight: 1.55,
  whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: '0 0 4px',
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  color: 'var(--cf-text)',
};
