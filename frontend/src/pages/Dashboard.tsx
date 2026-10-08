import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Alert, App as AntApp, Button, Card, Col, Form, Input, InputNumber, Modal, Row, Select,
  Space, Statistic, Table, Tabs, Tag, Typography,
} from 'antd';
import { PlusOutlined, ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { api, type Json } from '../api';
import { PlanPreview, StatusTag } from '../components';
import { categoryLabel } from '../categories';

const LANGS = [
  { value: 'pt-BR', label: 'pt-BR 巴西葡语（Francisca）' },
  { value: 'es-MX', label: 'es-MX 拉美西语（Dalia）' },
  { value: 'zh-CN', label: 'zh-CN 中文（Xiaoxiao）' },
];

/** 粘贴外部脚本时的格式示例 —— 用户常从别的工具搬脚本过来，格式五花八门，
 *  这里给一份「最省事」的骨架，解析器对它的各种变体都能吃下。 */
const SCRIPT_SAMPLE = `### S1（0.0-3.0s｜钩子）

\`\`\`text
A crystal-clear rectangular perfume bottle with amber liquid on a dark marble surface,
macro lens, slow dolly in about 3% of the frame width, warm side light from camera left.
\`\`\`

### S2（3.0-6.0s｜主体）

\`\`\`text
A 27-year-old woman holds the bottle about 12 cm from the side of her neck, her index
finger presses the nozzle once; a short fine burst of mist drifts toward her neck.
\`\`\`
`;

const SOURCE_LABEL: Record<string, string> = {
  fence: '代码块', label: '提示词标签', body: '整段正文',
};

export default function Dashboard() {
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const { message, modal } = AntApp.useApp();
  const [stats, setStats] = useState<Json>({});
  const [rows, setRows] = useState<Json[]>([]);
  const [talents, setTalents] = useState<Json[]>([]);
  const [products, setProducts] = useState<Json[]>([]);
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);

  // Trae 生成脚本弹窗状态
  const [genOpen, setGenOpen] = useState(false);
  const [genPid, setGenPid] = useState<number | null>(null);
  const [genLang, setGenLang] = useState('pt-BR');
  const [genShots, setGenShots] = useState(5);
  const [genDuration, setGenDuration] = useState(15);
  const [genInstruction, setGenInstruction] = useState('');
  const [genLoading, setGenLoading] = useState(false);
  const [genResult, setGenResult] = useState('');
  const [genLog, setGenLog] = useState('');
  // 后端在「超时但有部分产出」时会回 warning（而不是直接报错），这里要显式提示用户
  const [genWarning, setGenWarning] = useState('');
  // Trae 是 agent 循环，可能要跑几分钟。没有计时器时界面看起来像卡死，
  // 用户会反复点击（重跑会重复烧钱），所以给一个可见的秒数。
  const [genElapsed, setGenElapsed] = useState(0);
  // 「导入为项目分镜」的进行中状态
  const [importBusy, setImportBusy] = useState(false);
  // 「生成 15s 成片」的规划中状态
  const [planBusy, setPlanBusy] = useState(false);
  // 弹窗两个 Tab：agent 生成 / 粘贴外部脚本
  const [genTab, setGenTab] = useState('agent');
  const [pasteText, setPasteText] = useState('');
  const [pastePreview, setPastePreview] = useState<Json | null>(null);
  const [pasteBusy, setPasteBusy] = useState(false);

  useEffect(() => {
    if (!genLoading) return;
    setGenElapsed(0);
    const t = window.setInterval(() => setGenElapsed((s) => s + 1), 1000);
    return () => window.clearInterval(t);
  }, [genLoading]);

  const openGen = (id: number, lang: string, tab = 'agent') => {
    setGenPid(id);
    setGenLang(lang || 'pt-BR');
    setGenShots(5);
    setGenDuration(15);
    setGenInstruction('');
    setGenResult('');
    setGenLog('');
    setGenWarning('');
    setGenTab(tab);
    setPasteText('');
    setPastePreview(null);
    setGenOpen(true);
  };

  const runGen = async () => {
    if (genPid == null || genLoading) return;
    setGenLoading(true);
    setGenResult('');
    setGenLog('');
    setGenWarning('');
    try {
      const r = await api.generateScript(genPid, {
        language: genLang,
        shots: genShots,
        duration_sec: genDuration,
        instruction: genInstruction,
      });
      setGenResult(r.script || '（未产出内容，见下方日志）');
      setGenLog(r.log || '');
      setGenWarning(r.warning || '');
      if (r.warning) message.warning('生成未完全成功，下面是已产出的部分');
      else message.success('脚本已生成');
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setGenLoading(false);
    }
  };

  const copyScript = async () => {
    try {
      await navigator.clipboard.writeText(genResult);
      message.success('已复制到剪贴板');
    } catch {
      message.warning('复制失败，请手动选中文本复制');
    }
  };

  const downloadScript = () => {
    const blob = new Blob([genResult], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `script-p${genPid}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  /** **生成 15s 成片**（会花钱）：先要一份规划，把「几镜 / 几秒 / 预估花费 / 念白行」
   *  摊在确认框里，点确认才提交。规划本身不落库、不联网、不花钱。
   *
   *  为什么中间要停一下：出片是 N 笔真实的出片费用，且脚本可能有 lint 不达标、
   *  念白缺失、镜数不对等问题 —— 让人在**付钱之前**看到代价，比事后解释便宜得多。 */
  const produceFromScript = async (script: string, source: string) => {
    if (genPid == null || !script.trim() || planBusy) return;
    setPlanBusy(true);
    try {
      const p = await api.producePlan(genPid, { script, source });
      if (!p.shots_total) {
        message.warning(p.warnings?.[0] || '没有识别到镜头，先看看脚本格式');
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
            const r = await api.startProduce(genPid, { script, source });
            setGenOpen(false);
            message.success(`已提交出片 #${r.id}，正在后台执行`);
            nav(`/projects/${genPid}/deliver?run=${r.id}`);
          } catch (e) { message.error((e as Error).message); }
        },
      });
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setPlanBusy(false);
    }
  };

  /** 只落镜、不出片（**不花钱**）：想先逐镜看/改提示词时走这条。
   *  出片会自动带上商品图 + 主播参考图作为 i2v 参考图。 */
  const importOnly = async (script: string, source: string) => {
    if (genPid == null || !script.trim() || importBusy) return;
    setImportBusy(true);
    try {
      const r = await api.importScript(genPid, { script, source });
      (r.warnings || []).forEach((w: string) => message.warning(w, 6));
      message.success(`已导入 ${r.imported} 个镜头（未出片，不花钱）`);
      setGenOpen(false);
      nav(`/projects/${genPid}/deliver`);
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setImportBusy(false);
    }
  };

  /** 粘贴外部脚本的「解析预览」：dry_run 只解析 + lint，不落库。
   *  先给用户看一眼「识别到几镜、时长对不对、有没有警告」，再决定要不要导入。 */
  const previewPaste = async () => {
    if (genPid == null || !pasteText.trim() || pasteBusy) return;
    setPasteBusy(true);
    setPastePreview(null);
    try {
      const r = await api.importScript(genPid, { script: pasteText, dry_run: true });
      setPastePreview(r);
      if (!r.imported) message.warning(r.warnings?.[0] || '没有识别到镜头');
      else message.success(`识别到 ${r.imported} 个镜头`);
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setPasteBusy(false);
    }
  };

  const load = useCallback(async () => {
    try {
      const [s, p, t, pr] = await Promise.all([
        api.stats(), api.listProjects(), api.listTalents(), api.listProducts(),
      ]);
      setStats(s); setRows(p); setTalents(t); setProducts(pr);
    } catch (e) {
      message.error((e as Error).message);
    }
  }, [message]);

  useEffect(() => { load(); }, [load]);

  /** 商品库的「做视频」把商品 id 带过来（`/?productId=3`）—— 自动开表单并预选。
   *
   *  必须**等商品列表加载完**再预选：`Select` 的 options 还没到就设值，
   *  显示层会找不到 label 而渲染成裸数字。
   *  预选完成后立刻把 query 清掉，否则用户关掉弹窗、再点新建又会被重新填一次。
   */
  useEffect(() => {
    const pid = Number(params.get('productId'));
    if (!pid || !products.length) return;
    setOpen(true);
    form.setFieldValue('product_ids', [pid]);
    setParams({}, { replace: true });
  }, [params, products, form, setParams]);

  const create = async () => {
    const v = await form.validateFields();
    setBusy(true);
    try {
      const p = await api.createProject({
        name: v.name,
        language: v.language,
        talent_id: v.talent_id ?? null,
        products: (v.product_ids || []).map((id: number) => ({ product_id: id, role: '' })),
      });
      message.success(`项目已创建 #${p.id}`);
      setOpen(false);
      form.resetFields();
      load();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = (id: number) => {
    modal.confirm({
      title: `删除项目 #${id}？`,
      content: '只删元数据与分镜记录，磁盘上的出片/音轨一律保留。',
      okButtonProps: { danger: true },
      onOk: async () => {
        try { await api.deleteProject(id); message.success('已删除'); load(); }
        catch (e) { message.error((e as Error).message); }
      },
    });
  };

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Row gutter={16}>
        {[
          ['项目', stats.projects ?? 0], ['镜头', stats.shots ?? 0],
          ['商品（带图）', `${stats.products_with_images ?? 0} / ${stats.products ?? 0}`],
          ['主播', stats.talents ?? 0], ['素材', stats.assets ?? 0],
        ].map(([t, v]) => (
          <Col key={String(t)} span={4}>
            <Card size="small"><Statistic title={t as string} value={v as any} /></Card>
          </Col>
        ))}
        <Col span={4}>
          <Card size="small">
            <Space direction="vertical" size={4}>
              <Button icon={<ReloadOutlined />} size="small" onClick={load}>刷新</Button>
              <Button type="primary" icon={<PlusOutlined />} size="small" onClick={() => setOpen(true)}>
                新建项目
              </Button>
            </Space>
          </Card>
        </Col>
      </Row>

      <Card size="small" title="项目列表">
        <Table
          rowKey="id"
          dataSource={rows}
          pagination={false}
          columns={[
            { title: 'ID', dataIndex: 'id', width: 60 },
            { title: '名称', dataIndex: 'name' },
            { title: '语言', dataIndex: 'language', width: 90, render: (v: string) => <Tag>{v}</Tag> },
            { title: '状态', dataIndex: 'status', width: 110, render: (v: string) => <StatusTag status={v} /> },
            { title: '镜头', width: 90, render: (_: any, r: Json) => `${r.shots_ready}/${r.shots}` },
            { title: '主播', dataIndex: 'talent_code', width: 80, render: (v: string) => v || '-' },
            {
              title: '商品', dataIndex: 'products', width: 190,
              render: (ps: Json[]) => ps?.length
                ? ps.map((p) => `${p.code}·${categoryLabel(p.category)}`).join('、')
                : <Typography.Text type="secondary">未关联</Typography.Text>,
            },
            { title: '成本', dataIndex: 'cost_cny', width: 90, render: (v: number) => `¥${(v || 0).toFixed(2)}` },
            {
              title: '操作', width: 400,
              render: (_: any, r: Json) => (
                <Space size={4} wrap>
                  <Button size="small" type="primary"
                    onClick={() => openGen(r.id, r.language, 'agent')}>生成视频提示词</Button>
                  <Button size="small" onClick={() => openGen(r.id, r.language, 'paste')}>粘贴脚本导入</Button>
                  <Button size="small" onClick={() => nav(`/projects/${r.id}/deliver`)}>成片</Button>
                  <Button size="small" danger onClick={() => remove(r.id)}>删除</Button>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal title="新建项目" open={open} onOk={create} confirmLoading={busy}
        onCancel={() => setOpen(false)} okText="创建" cancelText="取消">
        <Form form={form} layout="vertical" initialValues={{ language: 'pt-BR' }}>
          <Form.Item name="name" label="项目名称" rules={[{ required: true, message: '必填' }]}>
            <Input placeholder="例：S06 双型号 15 秒 夜戏" />
          </Form.Item>
          <Form.Item name="language" label="投放语言（决定念白音色与文案语言）">
            <Select options={LANGS} />
          </Form.Item>
          <Form.Item name="talent_id" label="主播（外貌锚点会注入提示词）">
            <Select allowClear options={talents.map((t) => ({ value: t.id, label: `${t.code} ${t.name || ''}` }))} />
          </Form.Item>
          <Form.Item name="product_ids" label="商品"
            extra="商品的卖点描述会作为事实来源写进提示词；缺卖点描述的商品只能靠分镜简述推断">
            <Select mode="multiple" allowClear
              options={products.map((p) => ({
                value: p.id,
                label: `${p.code} ${p.name || ''}（${categoryLabel(p.category)}）`,
              }))} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title="生成视频提示词" open={genOpen} onCancel={() => setGenOpen(false)}
        footer={null} width={860} destroyOnClose>
        <Tabs activeKey={genTab} onChange={setGenTab} items={[
          {
            key: 'agent',
            label: '① 生成脚本',
            children: (
              <>
                <Alert type="info" showIcon style={{ marginBottom: 12 }}
                  message="按 fragrance-ecom-video 技能规范生成：先定全片骨架（统一锚点 / 光线走向 / 每镜动作与念白），再逐镜并发展开，最后合并成分镜表、逐镜 i2v 提示词与念白稿。"
                  description="生成后点「生成 15s 成片」→ 先给你一份规划（几镜/几秒/预估花费）确认，确认后才逐镜出片并混成成片；全过程进度在成片页可看、可重试。想先逐镜改提示词就点「只导入镜头」。生成通常 1–3 分钟（骨架 1 轮 + 逐镜并发；通道偶发卡顿时更久）。" />
                {genWarning ? (
                  <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={genWarning} />
                ) : null}
                <Space direction="vertical" size={12} style={{ width: '100%' }}>
                  <Space size={12} wrap>
                    <div>
                      <div style={{ fontSize: 12, color: 'var(--cf-text-secondary)' }}>投放语言</div>
                      <Select style={{ width: 220 }} value={genLang} onChange={setGenLang} options={LANGS} />
                    </div>
                    <div>
                      <div style={{ fontSize: 12, color: 'var(--cf-text-secondary)' }}>镜头数</div>
                      <InputNumber min={1} max={12} value={genShots} onChange={(v) => setGenShots(v ?? 5)} />
                    </div>
                    <div>
                      <div style={{ fontSize: 12, color: 'var(--cf-text-secondary)' }}>时长（秒）</div>
                      <InputNumber min={5} max={60} value={genDuration} onChange={(v) => setGenDuration(v ?? 15)} />
                    </div>
                  </Space>
                  <Input.TextArea rows={2} placeholder="附加要求（可选）：如「走夜戏调性」「双型号同框」「强化木质尾调」"
                    value={genInstruction} onChange={(e) => setGenInstruction(e.target.value)} />
                  <Space wrap>
                    <Button type="primary" loading={genLoading} onClick={runGen}>
                      {genLoading ? `生成中… 已用 ${genElapsed}s` : '开始生成'}
                    </Button>
                    <Button disabled={!genResult} onClick={copyScript}>复制</Button>
                    <Button disabled={!genResult} onClick={downloadScript}>下载 .md</Button>
                    <Button type="primary" danger icon={<ThunderboltOutlined />}
                      disabled={!genResult} loading={planBusy}
                      onClick={() => produceFromScript(genResult, 'agent')}>
                      生成 15s 成片
                    </Button>
                    <Button disabled={!genResult} loading={importBusy}
                      onClick={() => importOnly(genResult, 'agent')}>
                      只导入镜头（不出片）
                    </Button>
                  </Space>
                  {genResult ? (
                    <Input.TextArea readOnly value={genResult} rows={16}
                      style={{ fontFamily: 'monospace', fontSize: 12 }} />
                  ) : (
                    <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
                      {genLoading
                        ? 'Trae 正在按技能工作流产出脚本（读技能 → 逐镜写入），请勿重复点击…'
                        : '点击「开始生成」后，结果会显示在这里。'}
                    </Typography.Paragraph>
                  )}
                  {genLog ? (
                    <details>
                      <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--cf-text-secondary)' }}>
                        Trae 运行日志（调试用）
                      </summary>
                      <pre style={{ maxHeight: 220, overflow: 'auto', fontSize: 11, whiteSpace: 'pre-wrap' }}>{genLog}</pre>
                    </details>
                  ) : null}
                </Space>
              </>
            ),
          },
          {
            key: 'paste',
            label: '② 粘贴外部脚本',
            children: (
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                <Alert type="info" showIcon
                  message="把别处生成的分镜脚本直接粘进来，解析成项目镜头后即可一键出片"
                  description="出片时会自动带上本项目的商品图与主播参考图作为 i2v 参考图。建议先点「解析预览」确认识别结果，再点「生成 15s 成片」（会先给规划确认，才花钱）。" />
                <Input.TextArea rows={12} value={pasteText}
                  onChange={(e) => { setPasteText(e.target.value); setPastePreview(null); }}
                  placeholder="在此粘贴分镜脚本（markdown 即可，无需严格遵循模板）…"
                  style={{ fontFamily: 'monospace', fontSize: 12 }} />
                <Space wrap>
                  <Button type="primary" loading={pasteBusy} disabled={!pasteText.trim()}
                    onClick={previewPaste}>解析预览</Button>
                  <Button type="primary" danger icon={<ThunderboltOutlined />}
                    disabled={!pasteText.trim()} loading={planBusy}
                    onClick={() => produceFromScript(pasteText, 'paste')}>
                    生成 15s 成片
                  </Button>
                  <Button disabled={!pasteText.trim()} loading={importBusy}
                    onClick={() => importOnly(pasteText, 'paste')}>
                    只导入镜头（不出片）
                  </Button>
                  <Button disabled={!pasteText}
                    onClick={() => { setPasteText(''); setPastePreview(null); }}>清空</Button>
                  <Button type="link"
                    onClick={() => { setPasteText(SCRIPT_SAMPLE); setPastePreview(null); }}>填入格式示例</Button>
                </Space>

                {pastePreview ? (
                  pastePreview.imported ? (
                    <>
                      <Alert type="success" showIcon
                        message={`识别到 ${pastePreview.imported} 个镜头，共 ${pastePreview.shots.reduce(
                          (a: number, s: Json) => a + (s.duration_sec || 0), 0,
                        ).toFixed(1)} 秒`}
                        description="点「生成 15s 成片」写入项目并出片（同名镜号会按新脚本覆盖）；想先逐镜改就点「只导入镜头」。" />
                      <Table size="small" pagination={false} rowKey="code"
                        dataSource={pastePreview.shots}
                        columns={[
                          { title: '镜号', dataIndex: 'code', width: 64 },
                          { title: '时长(s)', dataIndex: 'duration_sec', width: 78 },
                          { title: '帧', dataIndex: 'target_frames', width: 56 },
                          {
                            title: 'lint', dataIndex: 'lint_score', width: 58,
                            render: (v: number) => <Tag color={v >= 8 ? 'green' : 'orange'}>{v}</Tag>,
                          },
                          {
                            title: '取词来源', dataIndex: 'prompt_source', width: 92,
                            render: (v: string) => SOURCE_LABEL[v] || v,
                          },
                          { title: '提示词开头', dataIndex: 'prompt_head', ellipsis: true },
                        ]} />
                      {pastePreview.warnings?.length ? (
                        <Alert type="warning" showIcon message="解析提示"
                          description={<ul style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
                            {pastePreview.warnings.map((w: string, i: number) => <li key={i}>{w}</li>)}
                          </ul>} />
                      ) : null}
                    </>
                  ) : (
                    <Alert type="error" showIcon message="没有识别到镜头"
                      description={<ul style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
                        {(pastePreview.warnings || []).map((w: string, i: number) => <li key={i}>{w}</li>)}
                      </ul>} />
                  )
                ) : null}

                <details>
                  <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--cf-text-secondary)' }}>
                    支持的格式（点开看判定规则）
                  </summary>
                  <div style={{ fontSize: 12, color: 'var(--cf-text-secondary)', marginTop: 6, lineHeight: 1.9 }}>
                    只要两样东西：<b>镜头标题</b>（含镜号或时间码）+ <b>提示词</b>。
                    <ul style={{ margin: '4px 0', paddingLeft: 18 }}>
                      <li>标题：<code>### S1（0.0-3.0s）</code>、<code>## Shot 1</code>、
                        <code>### 镜头1</code> 都能认；没写镜号会自动编 S1..Sn，没写时间码按 3.0s 计
                        （导入后可在成片页的「镜头与提示词」里逐镜改）</li>
                      <li>提示词：优先取 <code>```text</code> 代码块；没有代码块时取
                        <code>**提示词**：</code> 标签行；再没有就把整段正文当提示词（要求以英文为主）</li>
                      <li><b>表格不支持</b>：表格列的含义各家不同，且模板里那张「分镜表」的中文列是
                        画面简述与念白、并不是 i2v 提示词，硬解析只会产出跑偏的镜头</li>
                      <li>可选的 <code>**中文直译**：</code> 行会被识别并一起入库</li>
                    </ul>
                  </div>
                </details>
              </Space>
            ),
          },
        ]} />
      </Modal>
    </Space>
  );
}
