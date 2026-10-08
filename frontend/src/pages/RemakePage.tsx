import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  App as AntApp, Alert, Button, Card, Col, Descriptions, Input, InputNumber, Row, Select,
  Space, Steps, Tag, Tooltip, Typography, Upload,
} from 'antd';
import {
  ExperimentOutlined, LoadingOutlined, RocketOutlined, UploadOutlined,
} from '@ant-design/icons';
import { api, type Json } from '../api';

/** 爆款复刻（技能工作流 C）。
 *
 *  技能的复刻六步是：**拆解 → 归因 → 反推 → 分类 → 替换 → 重组**。
 *  这个页面把它做成三步可操作流程，每一步都对应已有能力，不另起一套：
 *
 *    ① 拆解参考片    → 反推工作台那套引擎（本地量测 + 看图反推逐镜提示词）
 *    ② 选目标项目    → 项目里已关联的商品与主播就是"替换"后的主体
 *    ③ 生成复刻脚本  → 同一个生文接口，带上 `remake_report_id`
 *
 *  关键原则（技能原文）：**保留原片的拍摄手法与情绪曲线，替换人物、产品与场景**。
 *  所以第 ③ 步的产物不是"另一个分镜"，而是"原片结构的换人换片版本"，脚本里
 *  会额外输出一张「本片镜号 ↔ 原片镜号 ↔ 保留方式」对照表。
 *
 *  成本口径：① 反推要调一次大模型（看图模式 token 更多）；③ 生成脚本走文本模型；
 *  只有到出片页才会真正按 GPU 时长花钱。
 */

const RETENTION_LABEL: Record<string, { text: string; color: string }> = {
  fully_preserved: { text: '完整保留手法', color: 'green' },
  partially_preserved: { text: '部分保留', color: 'cyan' },
  attribute_transfer: { text: '特征迁移（换人换品）', color: 'gold' },
  weak_reference: { text: '仅宽泛相似', color: 'default' },
};

export default function RemakePage() {
  const { message } = AntApp.useApp();
  const nav = useNavigate();

  const [reports, setReports] = useState<Json[]>([]);
  const [reportId, setReportId] = useState<number | null>(null);
  const [report, setReport] = useState<Json | null>(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  const [projects, setProjects] = useState<Json[]>([]);
  const [pid, setPid] = useState<number | null>(null);
  const [lang, setLang] = useState<string>('');
  const [shots, setShots] = useState(5);
  const [dur, setDur] = useState(15);
  const [instruction, setInstruction] = useState('');

  const [genning, setGenning] = useState(false);
  const [result, setResult] = useState<Json | null>(null);

  const loadReports = useCallback(async () => {
    try { setReports(await api.reverseReports()); }
    catch (e) { message.error((e as Error).message); }
  }, [message]);

  const loadProjects = useCallback(async () => {
    try { setProjects(await api.listProjects()); }
    catch (e) { message.error((e as Error).message); }
  }, [message]);

  useEffect(() => { void loadReports(); void loadProjects(); }, [loadReports, loadProjects]);

  /** 载入一份报告的完整内容（含逐镜逆推提示词）。 */
  const openReport = async (id: number) => {
    setReportId(id);
    setReport(null);
    setResult(null);
    try { setReport(await api.reverseReport(id)); }
    catch (e) { message.error((e as Error).message); }
  };

  /** 上传参考片并直接反推（看图模式，产出逐镜提示词）。 */
  const upload = async (f: File) => {
    setBusy(true);
    try {
      const r = await api.reverseUpload(f, { note, use_llm: true, use_vision: true });
      message.success(`拆解完成（报告 #${r.report_id}）`);
      await loadReports();
      await openReport(r.report_id);
    } catch (e) { message.error((e as Error).message); }
    finally { setBusy(false); }
  };

  const project = projects.find((p) => p.id === pid);

  const generate = async () => {
    if (!reportId || !pid) return;
    setGenning(true);
    setResult(null);
    try {
      const r = await api.generateScript(pid, {
        language: lang || undefined,
        shots, duration_sec: dur,
        instruction, remake_report_id: reportId,
      });
      setResult(r);
      message.success('复刻脚本已生成并存入项目');
    } catch (e) { message.error((e as Error).message); }
    finally { setGenning(false); }
  };

  const shotsOfReport: Json[] = report?.structure?.shots || [];
  const vision = !!report?.vision;

  // ★R-33：参考片实测剪辑点 = 本片的镜数与逐镜时间码。复刻模式下请求里填的
  // 镜数/时长不再生效（后端会按这份时间轴强制对齐），所以这里直接把输入框锁掉，
  // 免得用户以为改得动 —— 上一版正是"报告 6 镜 vs 请求 5 镜"打架丢了节拍。
  const timeline = shotsOfReport
    .map((s) => ({ start: Number(s.start), end: Number(s.end) }))
    .filter((t) => Number.isFinite(t.start) && Number.isFinite(t.end)
      && t.end - t.start >= 0.5);
  const timelineOn = timeline.length > 0;
  const timelineTotal = timelineOn ? timeline[timeline.length - 1].end : 0;

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card size="small">
        <Typography.Title level={4} style={{ marginTop: 0 }}>爆款复刻</Typography.Title>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 13 }}>
          拆解参考片 → 反推逐镜提示词 → <b>保留原片的镜头序列、节奏、运镜与光影结构</b>，
          只把人物、产品与场景换成你自己的。三步走完，脚本直接进项目出片。
        </Typography.Paragraph>
      </Card>

      <Steps size="small" current={result ? 3 : report ? 2 : 1} items={[
        { title: '拆解参考片' }, { title: '选目标项目' }, { title: '生成复刻脚本' },
      ]} />

      {/* ---------------- ① 拆解参考片 ---------------- */}
      <Card size="small" title="① 拆解参考片（出逐镜提示词）"
        extra={<Typography.Text type="secondary" style={{ fontSize: 12 }}>
          看图反推会调用一次大模型；关掉 LLM 的纯量测模式在反推工作台
        </Typography.Text>}>
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap>
            <Select style={{ width: 460 }} placeholder="选择一份已拆解的反推报告"
              value={reportId ?? undefined} loading={busy}
              onChange={(v) => void openReport(v)}
              options={reports.map((r) => ({
                value: r.id as number,
                label: `#${r.id}　${String(r.source_path || '').split(/[\\/]/).pop()}`
                  + `　${r.spec?.duration ?? '-'}s　${r.vision ? '逐镜提示词' : '仅结构'}`
                  + `　${r.shot_count || 0} 镜`,
              }))} />
            <Upload beforeUpload={(f) => { void upload(f); return false; }} showUploadList={false}
              disabled={busy}>
              <Button icon={busy ? <LoadingOutlined /> : <UploadOutlined />} loading={busy}>
                {busy ? '拆解中…（看图约 1-3 分钟）' : '上传新参考片并拆解'}
              </Button>
            </Upload>
          </Space>
          <Input placeholder="补充说明（选填，例：这条是浴室的夜戏，重点复刻喷雾那一段）"
            value={note} onChange={(e) => setNote(e.target.value)} />

          {report && (
            <>
              <Descriptions size="small" column={4} bordered>
                <Descriptions.Item label="报告">#{report.id}</Descriptions.Item>
                <Descriptions.Item label="镜头数">{shotsOfReport.length}</Descriptions.Item>
                <Descriptions.Item label="反推方式">
                  {vision ? '看图反推（含逐镜提示词）' : '纯数据（仅结构）'}
                </Descriptions.Item>
                <Descriptions.Item label="来源">
                  {String(report.source_path || '').split(/[\\/]/).pop()}
                </Descriptions.Item>
              </Descriptions>

              {!!report.vision_note && (
                <Typography.Text type="warning" style={{ fontSize: 12 }}>
                  {report.vision_note}
                </Typography.Text>
              )}

              <div>
                <Typography.Text strong style={{ fontSize: 13 }}>
                  逐镜逆推提示词（{shotsOfReport.length} 镜，可整段复制去复用）
                </Typography.Text>
                {!vision && (
                  <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                    这份报告没有提示词 —— 在反推工作台重新反推（看图模式）即可得到
                  </Typography.Text>
                )}
                <div style={{ marginTop: 8, display: 'grid', gap: 10 }}>
                  {shotsOfReport.map((s: Json, i: number) => {
                    const rt = RETENTION_LABEL[s.retention as string];
                    return (
                      <div key={i} className="cf-card" style={{ padding: 12 }}>
                        <Space size={8} wrap style={{ marginBottom: 6 }}>
                          <Tag color="blue">#{s.idx ?? i + 1}</Tag>
                          <Typography.Text style={{ fontSize: 12 }}>
                            {s.start}~{s.end}s
                          </Typography.Text>
                          {s.shot_size && <Tag>{s.shot_size}</Tag>}
                          {s.camera && <Tag>{s.camera}</Tag>}
                          {s.light && <Tag>{s.light}</Tag>}
                          {rt && <Tag color={rt.color}>{rt.text}</Tag>}
                        </Space>
                        {s.desc && (
                          <Typography.Paragraph style={{ fontSize: 12, marginBottom: 4 }}>
                            画面：{s.desc}
                          </Typography.Paragraph>
                        )}
                        {s.why_effective && (
                          <Typography.Paragraph type="secondary"
                            style={{ fontSize: 12, marginBottom: 4 }}>
                            为什么有效：{s.why_effective}
                          </Typography.Paragraph>
                        )}
                        {s.prompt ? (
                          <>
                            <pre style={preStyle}>{s.prompt}</pre>
                            <Button size="small" type="link" style={{ paddingLeft: 0 }}
                              onClick={() => {
                                void navigator.clipboard.writeText(String(s.prompt));
                                message.success('已复制该镜提示词');
                              }}>
                              复制这一镜
                            </Button>
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
                    );
                  })}
                </div>
              </div>
            </>
          )}
        </Space>
      </Card>

      {/* ---------------- ② 选目标项目 ---------------- */}
      <Card size="small" title="② 选目标项目（决定换成谁、换什么产品）">
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Select style={{ width: 460 }} placeholder="选择要套用这套手法的项目"
            value={pid ?? undefined} onChange={setPid} showSearch optionFilterProp="label"
            options={projects.map((p) => ({
              value: p.id as number,
              label: `#${p.id}　${p.name}`,
            }))} />
          {project && (
            <Descriptions size="small" column={3} bordered>
              <Descriptions.Item label="商品">
                {(project.products || []).length
                  ? (project.products || []).map((x: Json) =>
                      <Tag key={x.product_id}>{x.name}</Tag>)
                  : <Typography.Text type="warning">未关联商品（锚点会缺失）</Typography.Text>}
              </Descriptions.Item>
              <Descriptions.Item label="主播">
                {project.talent_code
                  ? <Tag>{project.talent_code}</Tag>
                  : <Typography.Text type="warning">未选主播</Typography.Text>}
              </Descriptions.Item>
              <Descriptions.Item label="已有分镜">{project.shots}</Descriptions.Item>
            </Descriptions>
          )}
          {project && !(project.products || []).length && (
            <Typography.Text type="warning" style={{ fontSize: 12 }}>
              复刻的"替换"必须知道换成什么：请先在项目里关联商品并为项目选好主播，再来复刻。
            </Typography.Text>
          )}
        </Space>
      </Card>

      {/* ---------------- ③ 生成复刻脚本 ---------------- */}
      <Card size="small" title="③ 生成复刻脚本（保留手法 → 替换主体）">
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {timelineOn && (
            <Alert type="info" showIcon
              message={`镜数与时长为硬约束：按参考片的 ${timeline.length} 个实测剪辑点生成`
                + `（总长 ${timelineTotal}s），下面两项不可手改`}
              description={'复刻片必须与参考片等长：合并 / 拆分镜头或改任一时间码都会'
                + '破坏原片的节奏，后端会强制对齐到实测剪辑点。想换镜数请另做一支非复刻的片子。'} />
          )}
          <Space wrap>
            <InputNumber addonBefore="镜数" min={1} max={12} value={shots}
              disabled={timelineOn}
              onChange={(v) => setShots(Number(v))} />
            <InputNumber addonBefore="时长(s)" min={3} max={60} value={dur}
              disabled={timelineOn}
              onChange={(v) => setDur(Number(v))} />
            <Input style={{ width: 200 }} placeholder="投放语言（留空=项目默认）"
              value={lang} onChange={(e) => setLang(e.target.value)} />
            <Tooltip title="例：原片的浴室场景换成暖调客厅；念白主打留香">
              <Button type="primary" icon={<RocketOutlined />}
                loading={genning} disabled={!reportId || !pid}
                onClick={() => void generate()}>
                {genning ? '生成中…（约 1-3 分钟）' : '生成复刻脚本'}
              </Button>
            </Tooltip>
          </Space>
          <Input.TextArea rows={2}
            placeholder="附加要求（选填）：比如「把浴室场景换成暖调客厅」「喷雾落点用喷空中走入汽雾」"
            value={instruction} onChange={(e) => setInstruction(e.target.value)} />

          {result && (
            <>
              <Space size={8} wrap>
                <Button type="primary" ghost onClick={() => nav(`/projects/${pid}/deliver`)}>
                  去出片页（下一步：参考图 → 出片）
                </Button>
                <Button onClick={() => {
                  void navigator.clipboard.writeText(String(result.script || ''));
                  message.success('已复制整份脚本');
                }}>复制脚本</Button>
              </Space>
              {result.warning && (
                <Typography.Text type="warning" style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>
                  {result.warning}
                </Typography.Text>
              )}
              {result.log && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {result.log}
                </Typography.Text>
              )}
              <Row gutter={16}>
                <Col span={14}>
                  <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
                    复刻脚本（含「复刻对照」表）
                  </div>
                  <pre style={{ ...preStyle, maxHeight: 520, overflow: 'auto' }}>
                    {String(result.script || '')}
                  </pre>
                </Col>
                <Col span={10}>
                  <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
                    逐镜提示词
                  </div>
                  <div style={{ maxHeight: 520, overflow: 'auto' }}>
                    {(result.skeleton?.shots || []).map((s: Json, i: number) => {
                      const rt = RETENTION_LABEL[s.retention as string];
                      return (
                        <div key={i} className="cf-card"
                          style={{ padding: 10, marginBottom: 8 }}>
                          <Space size={6} wrap>
                            <Tag color="blue">{s.code}</Tag>
                            <Typography.Text style={{ fontSize: 12 }}>
                              {s.start_sec}-{s.end_sec}s
                            </Typography.Text>
                            {s.source_shot ? <Tag>原片 #{s.source_shot}</Tag> : null}
                            {rt && <Tag color={rt.color}>{rt.text}</Tag>}
                            {Array.isArray(s.products) && (s.products as Json[]).length
                              ? (s.products as Json[]).map((c) =>
                                  <Tag key={String(c)} color="purple">{String(c)}</Tag>)
                              : null}
                          </Space>
                          {s.talent_look && (
                            <div style={{ fontSize: 12, marginTop: 4 }}>
                              本镜造型：{s.talent_look}
                            </div>
                          )}
                          {s.visual_proof && s.visual_proof !== 'none' && (
                            <div style={{ fontSize: 12, marginTop: 4 }}>
                              卖点可视化：{s.visual_proof}
                            </div>
                          )}
                        </div>
                      );
                    })}
                    {!(result.skeleton?.shots || []).length && (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        骨架未回传（脚本正文里有完整分镜）
                      </Typography.Text>
                    )}
                  </div>
                </Col>
              </Row>
            </>
          )}
          {!result && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              <ExperimentOutlined /> 生成走的是与「生成脚本」同一个链路（骨架 → 逐镜并发 → 合并），
              额外注入原片的逐镜序列、有效性归因与保留方式四档标记。
            </Typography.Text>
          )}
        </Space>
      </Card>
    </Space>
  );
}

const preStyle: React.CSSProperties = {
  background: 'rgba(0,0,0,0.28)', border: '1px solid var(--cf-border)',
  borderRadius: 8, padding: 10, fontSize: 12, lineHeight: 1.55,
  whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: '0 0 4px',
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  color: 'var(--cf-text)',
};
