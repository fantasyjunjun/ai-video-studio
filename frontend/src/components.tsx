import { useCallback, useEffect, useState, type ReactNode } from 'react';
import {
  Alert, App as AntApp, Button, Card, Form, InputNumber, Select, Space, Statistic,
  Switch, Table, Tag, Tooltip, Typography,
} from 'antd';
import { api, fileUrl, type Json } from './api';
import { categoryMeta } from './categories';

export const MIN_SCORE = 8;

/** 一键出片的**规划预览**：花钱前把代价摊开 —— 几镜、几秒、多少钱、缺什么、
 *  哪些镜会被复用（复用 = 不重复计费）。
 *
 *  成片页与项目工作台的出片确认框共用这一份：两边各写一个"预估花费长什么样"，
 *  迟早会有一边漏掉某个警告。
 */
export function PlanPreview({ plan }: { plan: Json }) {
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Space size={16} wrap>
        <Statistic title="镜头" value={`${plan.shots_ready}/${plan.shots_total}`} />
        <Statistic title="本次要出" value={plan.render_calls ?? 0} suffix="镜" />
        <Statistic title="总时长" value={plan.total_sec} suffix="s" />
        <Statistic title="总帧数" value={plan.total_frames ?? '-'} />
        <Statistic title="念白行" value={plan.vo_row_count ?? 0} />
        <Statistic title="预估花费" value={plan.estimate_cny ?? 0} precision={2} prefix="¥" />
        <Tag>音色 {plan.voice || '-'}</Tag>
        <Tag>语言 {plan.language || '-'}</Tag>
      </Space>

      {(plan.to_render || []).length > 0 && (
        <Table size="small" rowKey="code" dataSource={plan.to_render} pagination={false}
          columns={[
            { title: '要出的镜', dataIndex: 'code', width: 100 },
            { title: '分镜时长', dataIndex: 'duration_sec', width: 96,
              render: (v: number) => `${v}s` },
            { title: '将请求', dataIndex: 'request_sec', width: 90,
              render: (v: number) => `${v}s` },
            { title: '说明', render: () => (
              <Typography.Text type="secondary">
                时长跟随分镜，向上取整后由装配精确裁剪
              </Typography.Text>) },
          ]} />
      )}

      {(plan.rows || []).length > 0 && (
        <Table size="small" rowKey={(_, i) => String(i)} dataSource={plan.rows} pagination={false}
          title={() => <Typography.Text type="secondary">
            念白稿（来源：{plan.rows_source === 'script' ? '脚本素材' : '无'}）
          </Typography.Text>}
          columns={[
            { title: '念白窗口', width: 120,
              render: (_: any, r: Json) => `${r.start}~${r.end}s` },
            { title: '文案', dataIndex: 'text' },
          ]} />
      )}

      {(plan.warnings || []).map((s: string, i: number) => (
        <Alert key={i} type="warning" showIcon message={s} />
      ))}
    </Space>
  );
}

/** lint 分颜色：**低于闸门必须显眼** —— 这是"能不能发片"的硬指标，不能只当数字看。 */
export function LintTag({ score, min = MIN_SCORE }: { score?: number | null; min?: number }) {
  if (score === undefined || score === null) return <Tag>未评分</Tag>;
  const color = score >= 12 ? 'green' : score >= min ? 'gold' : 'red';
  return (
    <Tooltip title={`满分 14，闸门 ${min}。lint 只管提示词结构，管不了时序与构图漂移`}>
      <Tag color={color}>{score} / 14</Tag>
    </Tooltip>
  );
}

/** 商品品类 badge。
 *
 *  取代旧的 `NotesLevelTag`：随"字段模型完全替换"，商品事实不再分 A/B/C 级，
 *  卡片上要区分的是**品类**（用于一眼扫出这是什么货），不是事实来源可信度。
 */
export function CategoryTag({ category }: { category?: string | null }) {
  const m = categoryMeta(category);
  return (
    <span style={{
      display: 'inline-block', padding: '1px 8px', borderRadius: 10,
      background: m.bg, color: m.color, fontSize: 12, lineHeight: '18px',
      border: `1px solid ${m.color}33`, whiteSpace: 'nowrap',
    }}>{m.label}</span>
  );
}

export function StatusTag({ status }: { status?: string | null }) {
  const map: Record<string, [string, string]> = {
    draft: ['default', '草稿'],
    storyboarded: ['blue', '已分镜'],
    lint_passed: ['green', 'lint 通过'],
    lint_failed: ['red', 'lint 未过'],
    queued: ['default', '排队中'],
    running: ['processing', '生成中'],
    succeeded: ['success', '成功'],
    failed: ['error', '失败'],
    reused: ['cyan', '复用'],
    done: ['success', '完成'],
  };
  const [color, text] = map[status || ''] || ['default', status || '-'];
  return <Tag color={color}>{text}</Tag>;
}

/** 极简折线（不引图表库）：亮度/帧差趋势够用了。
 *  `marks` 是 **0~1 的相对位置**（不是数组下标），调用方自己把秒数换算成比例。 */
export function Sparkline({
  values, marks = [], height = 90, color = '#b37f4b',
}: { values: number[]; marks?: number[]; height?: number; color?: string }) {
  const W = 640;
  if (!values.length) return <Typography.Text type="secondary">无数据</Typography.Text>;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = W / Math.max(1, values.length - 1);
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(height - ((v - min) / span) * (height - 10) - 5).toFixed(1)}`);
  return (
    <svg viewBox={`0 0 ${W} ${height}`} width="100%" height={height} preserveAspectRatio="none">
      <polyline points={pts.join(' ')} fill="none" stroke={color} strokeWidth={1.5} />
      {marks.map((m, i) => {
        const x = Math.min(1, Math.max(0, m)) * W;
        return <line key={i} x1={x} y1={0} x2={x} y2={height} stroke="#d4380d" strokeWidth={1} strokeDasharray="3 3" />;
      })}
    </svg>
  );
}

// ---------------------------------------------------------------- 合规（P-4）

/** 三级严重度配色。high 才阻断发布，medium/low 只是"该确认一下"。 */
const SEV_META: Record<string, { color: string; bg: string; label: string }> = {
  high: { color: 'red', bg: '#ffccc7', label: '高危' },
  medium: { color: 'orange', bg: '#ffe7ba', label: '高风险' },
  low: { color: 'gold', bg: '#fff1b8', label: '提示' },
};

export function SeverityTag({ severity, label }: { severity?: string; label?: string }) {
  const m = SEV_META[severity || ''] || { color: 'default', bg: '#f0f0f0', label: severity || '-' };
  return <Tag color={m.color}>{label || m.label}</Tag>;
}

/** 把命中处标红渲染。跨规则可能交叠，所以用 `pos` 单调推进，避免重复输出文字。 */
export function Highlighted({ text, findings }: { text?: string; findings?: Json[] }) {
  if (!text) return null;
  const sorted = [...(findings || [])].sort((a, b) => a.start - b.start || a.end - b.end);
  const parts: ReactNode[] = [];
  let pos = 0;
  sorted.forEach((f: Json, i: number) => {
    const s = Math.max(pos, f.start);
    const e = Math.max(s, Math.min(f.end, text.length));
    if (s > pos) parts.push(<span key={`t${i}`}>{text.slice(pos, s)}</span>);
    if (e > s) {
      const m = SEV_META[f.severity] || SEV_META.low;
      parts.push(
        <Tooltip key={`m${i}`} title={`${f.category_label}｜${f.law || '—'}`}>
          <mark style={{
            background: m.bg, color: 'inherit', padding: '0 2px', borderRadius: 3,
            borderBottom: `1px solid ${m.color}`, cursor: 'help',
          }}>
            {text.slice(s, e)}
          </mark>
        </Tooltip>,
      );
      pos = e;
    }
  });
  if (pos < text.length) parts.push(<span key="tail">{text.slice(pos)}</span>);
  return <Typography.Paragraph style={{ marginBottom: 0 }}>{parts}</Typography.Paragraph>;
}

export type ComplianceRow = { start: number; end: number; text: string };

/** 广告法合规自检面板。
 *
 *  `rows` 传"页面上正在编辑的念白稿"，会与后端兜底文案源（标题 / 镜头中文 /
 *  念白音轨留痕）合并扫描 —— 合并而不是替换，因为标题与镜头文案同样是发布内容。
 */
export function CompliancePanel({ pid, rows, title = '广告法合规自检' }: {
  pid: number;
  rows?: ComplianceRow[];
  title?: string;
}) {
  const { message } = AntApp.useApp();
  const [rep, setRep] = useState<Json | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (persist: boolean) => {
    setBusy(true);
    try {
      const payload: Json = { persist };
      const clean = (rows || [])
        .map((r) => ({ start: r.start || 0, end: r.end || 0, text: (r.text || '').trim() }))
        .filter((r) => r.text);
      if (clean.length) payload.rows = clean;
      const r = await api.compliance(pid, payload);
      setRep(r);
      if (r.blocked) message.error(`含 ${r.counts.high} 项高危用语，不应发布`);
      else if (r.total) message.warning(`发现 ${r.total} 处需确认的表述`);
      else message.success('合规自检通过');
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const items: Json[] = rep?.items || [];
  const flat: Json[] = items.flatMap((it: Json) =>
    (it.findings || []).map((f: Json) => ({ ...f, key: `${it.source}-${f.start}-${f.rule_id}` })));
  const c = rep?.counts || {};

  return (
    <Card size="small" title={title} extra={
      <Space>
        <Button size="small" loading={busy} onClick={() => run(false)}>自检</Button>
        <Button size="small" type="primary" loading={busy} onClick={() => run(true)}>自检并留档</Button>
      </Space>
    }>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Typography.Text type="secondary">
          扫描绝对化用语 / 医疗功效 / 虚假紧迫 / 绝对承诺等禁语；高危项会被一键后期
          （<Typography.Text code>/final</Typography.Text>）直接拦下。词表可编辑
          <Typography.Text code>backend/compliance.yaml</Typography.Text> 后热重载。
        </Typography.Text>

        {rep && (
          <>
            <Alert
              showIcon
              type={rep.blocked ? 'error' : rep.total ? 'warning' : 'success'}
              message={rep.blocked
                ? `不应发布：命中 ${rep.counts.high} 项高危用语`
                : rep.total ? `有 ${rep.total} 处建议改写（无高危项，可发布）`
                  : '合规自检通过：未命中任何禁语'}
              description={rep.total ? rep.summary : undefined}
            />
            <Space size={24}>
              <Statistic title="高危" value={c.high ?? 0} valueStyle={{ color: '#cf1322' }} />
              <Statistic title="高风险" value={c.medium ?? 0} valueStyle={{ color: '#d46b08' }} />
              <Statistic title="提示" value={c.low ?? 0} valueStyle={{ color: '#d4b106' }} />
              <Statistic title="已扫文本" value={(rep.scanned || []).length} suffix="段" />
              <Statistic title="词表" value={rep.rule_count} suffix="条规则" />
            </Space>
            <Table
              size="small" rowKey="key" dataSource={flat} pagination={flat.length > 12 ? { pageSize: 12 } : false}
              columns={[
                { title: '级别', dataIndex: 'severity', width: 84,
                  render: (v: string, r: Json) => <SeverityTag severity={v} label={r.severity_label} /> },
                { title: '类别', dataIndex: 'category_label', width: 130 },
                { title: '命中', dataIndex: 'matched', width: 130,
                  render: (v: string) => <Typography.Text strong>{v}</Typography.Text> },
                { title: '出处', dataIndex: 'source', width: 150, ellipsis: true },
                { title: '依据', dataIndex: 'law', width: 210, ellipsis: true },
                { title: '建议改写', dataIndex: 'suggestion', ellipsis: true },
              ]}
            />
            {items.filter((it: Json) => it.text).map((it: Json) => (
              <div key={it.source}>
                <Typography.Text type="secondary">{it.source}</Typography.Text>
                <Highlighted text={it.text} findings={it.findings} />
              </div>
            ))}
            {rep.saved_to && (
              <Typography.Text type="secondary">报告已留档：{rep.saved_to}</Typography.Text>
            )}
          </>
        )}
      </Space>
    </Card>
  );
}

// ---------------------------------------------------------------- 花费上限（P-5）

const PERIODS = [
  { value: 'total', label: '有史以来（total）' },
  { value: 'daily', label: '当天（daily）' },
  { value: 'monthly', label: '当月（monthly）' },
  { value: 'rolling_24h', label: '滚动 24 小时（rolling_24h）' },
];

/** 花费上限面板。
 *
 *  `editable` 为真时给可编辑表单（放设置页）；否则只读展示（放交付页）。
 *  判据全在后端 `services/budget.py::check` —— 这里只是把它的结论显示出来，
 *  页面上的"还剩多少"与服务端拦截**必然一致**（同一函数）。
 */
export function BudgetPanel({ px, editable = true, title = '花费上限（熔断）' }: {
  px?: number; editable?: boolean; title?: string;
}) {
  const { message } = AntApp.useApp();
  const [b, setB] = useState<Json | null>(null);
  const [busy, setBusy] = useState(false);
  const [form] = Form.useForm();

  const load = useCallback(async () => {
    try {
      const r = await api.budget(px);
      setB(r);
      form.setFieldsValue({
        enabled: r.enabled, period: r.period,
        per_task_cap_cny: r.per_task_cap_cny, project_cap_cny: r.project_cap_cny,
        global_cap_cny: r.global_cap_cny, reserve_ratio: r.reserve_ratio,
        count_pending: r.count_pending, on_exceed: r.on_exceed,
      });
    } catch (e) { message.error((e as Error).message); }
  }, [px, form, message]);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setBusy(true);
    try {
      await api.budgetSave(form.getFieldsValue(true), px);
      message.success('已写回 providers.yaml 并热重载');
      load();
    } catch (e) { message.error((e as Error).message); }
    finally { setBusy(false); }
  };

  if (!b) return <Card size="small" title={title} loading />;
  const over = b.global_cap_cny > 0 && b.committed_cny > b.global_cap_cny;

  return (
    <Card size="small" title={title} extra={<Button size="small" onClick={load}>刷新</Button>}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Alert
          type={!b.enabled ? 'warning' : over ? 'error' : 'success'}
          showIcon
          message={!b.enabled ? '花费上限已关闭（不拦截任何提交）'
            : over ? '已超出全局上限，新的出片提交会被拒绝'
              : `在花费上限内${b.on_exceed === 'warn' ? '（当前为"仅告警"模式，不会拦截）' : ''}`}
          description={`统计窗口：${b.period}${b.period_start ? `（自 ${String(b.period_start).slice(0, 19)}）` : ''}；在途任务${b.count_pending ? '计入' : '不计入'}已花。`}
        />
        <Space size={24} wrap>
          <Statistic title="已入账" value={b.spent_cny} precision={2} prefix="¥" />
          <Statistic title="在途/未知" value={b.pending_cny} precision={2} prefix="¥"
            valueStyle={{ color: '#d46b08' }} />
          <Statistic title="合计占用" value={b.committed_cny} precision={2} prefix="¥" />
          <Statistic title="全局上限" value={b.global_cap_cny > 0 ? b.global_cap_cny : '不限'}
            {...(b.global_cap_cny > 0 ? { precision: 2, prefix: '¥' } : {})} />
          <Statistic title="全局余额"
            value={b.global_remaining_cny == null ? '不限' : b.global_remaining_cny}
            {...(b.global_remaining_cny == null ? {} : { precision: 2, prefix: '¥' })}
            valueStyle={{ color: over ? '#cf1322' : '#3f8600' }} />
        </Space>

        {editable && (
          <Form form={form} layout="inline" size="small" style={{ rowGap: 8 }}>
            <Form.Item name="enabled" label="启用" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="period" label="统计窗口">
              <Select style={{ width: 190 }} options={PERIODS} />
            </Form.Item>
            <Form.Item name="on_exceed" label="超限行为">
              <Select style={{ width: 150 }} options={[
                { value: 'block', label: '拦截（block）' },
                { value: 'warn', label: '仅告警（warn）' },
              ]} />
            </Form.Item>
            <Form.Item name="per_task_cap_cny" label="单次上限 ¥">
              <InputNumber min={0} />
            </Form.Item>
            <Form.Item name="project_cap_cny" label="单项目上限 ¥">
              <InputNumber min={0} />
            </Form.Item>
            <Form.Item name="global_cap_cny" label="全局上限 ¥">
              <InputNumber min={0} />
            </Form.Item>
            <Form.Item name="reserve_ratio" label="预估预留倍数">
              <InputNumber min={0.1} step={0.1} />
            </Form.Item>
            <Form.Item name="count_pending" label="在途计入" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item>
              <Button type="primary" size="small" loading={busy} onClick={save}>保存上限</Button>
            </Form.Item>
          </Form>
        )}

        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          0 = 不限。出片是"提交即计费"的非幂等操作，闸门在<b>发请求之前</b>生效：
          超限直接拒绝并留痕，不会先花钱再说。供应商级上限在各供应商表单的
          <Typography.Text code>spend_cap_cny</Typography.Text> 字段里配。
        </Typography.Text>

        {(b.recent_blocks || []).length > 0 && (
          <Table size="small" rowKey="id" pagination={false}
            dataSource={b.recent_blocks as Json[]}
            columns={[
              { title: '时间', dataIndex: 'created_at', width: 190,
                render: (v: string) => String(v || '').slice(0, 19) },
              { title: '拦截级别', dataIndex: 'scope', width: 100,
                render: (v: string) => <Tag color="red">{v}</Tag> },
              { title: '预估', dataIndex: 'estimate_cny', width: 90,
                render: (v: number) => `¥${Number(v || 0).toFixed(2)}` },
              { title: '原因', dataIndex: 'reason', ellipsis: true },
            ]} />
        )}

        {(b.open_tasks || []).length > 0 && (
          <>
            <Typography.Text type="secondary">
              在途 / 未知任务（钱已发出或可能已扣，无法确定结果 → 请到平台对账）
            </Typography.Text>
            <Table size="small" rowKey="id" pagination={false}
              dataSource={b.open_tasks as Json[]}
              columns={[
                { title: 'ID', dataIndex: 'id', width: 60 },
                { title: '供应商', dataIndex: 'provider_id', width: 160 },
                { title: '状态', dataIndex: 'status', width: 100,
                  render: (v: string) => <Tag color={v === 'unknown' ? 'red' : 'processing'}>{v}</Tag> },
                { title: '厂商 task', dataIndex: 'provider_task_id', ellipsis: true },
                { title: '预估', dataIndex: 'estimated_cny', width: 90,
                  render: (v: number) => (v == null ? '—' : `¥${Number(v).toFixed(2)}`) },
              ]} />
          </>
        )}
      </Space>
    </Card>
  );
}

// ---------------------------------------------------------------- 出片质量门（P-6）

const QC_META: Record<string, { color: string; label: string }> = {
  high: { color: 'red', label: '高危' },
  medium: { color: 'orange', label: '需复核' },
  low: { color: 'gold', label: '提示' },
  info: { color: 'default', label: '信息' },
};

/** 出片质量门面板。
 *
 *  与合规面板互补：合规管**文案**，这里管**画面结果**。lint 满分也挡不住
 *  「某镜根本没动」「雾没出来」「尾巴冻住」—— 只有真解帧才看得见。
 *  自动判据只**圈重点**，结论请点开逐格拼图目视（本项目的既定方法论）。
 */
export function QualityPanel({ pid, title = '出片质量门（逐镜）' }: {
  pid: number; title?: string;
}) {
  const { message } = AntApp.useApp();
  const [rep, setRep] = useState<Json | null>(null);
  const [busy, setBusy] = useState(false);
  const [sheet, setSheet] = useState(false);
  const [countRoi, setCountRoi] = useState(false);

  const run = async () => {
    setBusy(true);
    try {
      const r = await api.projectQc(pid, {
        sheet,
        count_roi: countRoi ? [0.2, 0.55, 0.8, 0.95] : undefined,
        save_report: true,
      });
      setRep(r);
      if (r.blocked) message.error(`有 ${r.counts.high} 项高危质检问题`);
      else if (r.total) message.warning(`有 ${r.total} 项需复核`);
      else message.success('出片质量门通过');
    } catch (e) { message.error((e as Error).message); }
    finally { setBusy(false); }
  };

  const flat: Json[] = (rep?.items || []).flatMap((it: Json) =>
    (it.qc?.findings || []).map((f: Json) => ({
      ...f, key: `${it.scope}-${it.code}-${f.code}-${f.at_sec}`, scope: it.code,
    })));

  return (
    <Card size="small" title={title} extra={
      <Space size={8}>
        <Tooltip title="额外生成逐格拼图（自动判据只圈重点，结论看图）">
          <Space size={4}>
            <Switch size="small" checked={sheet} onChange={setSheet} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>出拼图</Typography.Text>
          </Space>
        </Tooltip>
        <Tooltip title="在画面下半部（桌面 ROI）数亮物件，检测「瓶盖消失 / 同物复制」">
          <Space size={4}>
            <Switch size="small" checked={countRoi} onChange={setCountRoi} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>物件数量</Typography.Text>
          </Space>
        </Tooltip>
        <Button size="small" type="primary" loading={busy} onClick={run}>逐镜质检</Button>
      </Space>
    }>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Typography.Text type="secondary">
          黑场 / 冻结帧 / 整镜无运动 / 亮度跳变 / 重复帧 / <b>声明了雾却没出现</b> /
          起点与台账不符 / 源音轨残留。<b>逐镜判</b>而不是只看整片 —— 整片会把单镜的缺陷平均掉。
        </Typography.Text>

        {rep && (
          <>
            <Alert
              showIcon
              type={rep.blocked ? 'error' : rep.total ? 'warning' : 'success'}
              message={rep.blocked
                ? `建议重出：${rep.counts.high} 项高危质检问题`
                : rep.total ? `${rep.total} 项需复核（无高危）` : '逐镜质检通过'}
              description={`共检查 ${rep.checks} 项（${(rep.items || []).filter((x: Json) => x.scope === 'shot').length} 镜 + 成片）；高危 ${rep.counts.high} / 需复核 ${rep.counts.medium}`}
            />
            <Space size={24}>
              <Statistic title="高危" value={rep.counts.high ?? 0} valueStyle={{ color: '#cf1322' }} />
              <Statistic title="需复核" value={rep.counts.medium ?? 0} valueStyle={{ color: '#d46b08' }} />
              <Statistic title="提示" value={rep.counts.low ?? 0} valueStyle={{ color: '#d4b106' }} />
              <Statistic title="已检" value={rep.checks ?? 0} suffix="项" />
            </Space>
            <Table size="small" rowKey="key" dataSource={flat}
              pagination={flat.length > 12 ? { pageSize: 12 } : false}
              columns={[
                { title: '镜头', dataIndex: 'scope', width: 90 },
                { title: '级别', dataIndex: 'severity', width: 88,
                  render: (v: string) => {
                    const m = QC_META[v] || QC_META.info;
                    return <Tag color={m.color}>{m.label}</Tag>;
                  } },
                { title: '问题', dataIndex: 'label', width: 210 },
                { title: '时刻', dataIndex: 'at_sec', width: 80,
                  render: (v: number) => (v == null ? '—' : `${Number(v).toFixed(2)}s`) },
                { title: '详情', dataIndex: 'detail', ellipsis: true },
                { title: '处置建议', dataIndex: 'suggestion', ellipsis: true },
              ]} />
            {(rep.sheets || []).length > 0 && (
              <Space wrap>
                {(rep.sheets as string[]).map((s) => (
                  <a key={s} href={fileUrl(s)} target="_blank" rel="noreferrer">
                    <img src={fileUrl(s)} alt="逐格拼图" style={{ width: 260, border: '1px solid #eee' }} />
                  </a>
                ))}
              </Space>
            )}
            {rep.saved_to && (
              <Typography.Text type="secondary">报告已留档：{rep.saved_to}</Typography.Text>
            )}
          </>
        )}
      </Space>
    </Card>
  );
}

/** 把「客户端该填什么」原样展示出来 —— 路径由后端算好，避免手抄出错。 */
export function McpPanel({ title = 'AI Agent 接入（MCP）' }: { title?: string }) {
  const { message } = AntApp.useApp();
  const [info, setInfo] = useState<Json | null>(null);

  const load = useCallback(async () => {
    try {
      setInfo(await api.mcpInfo());
    } catch (e) {
      message.error((e as Error).message);
    }
  }, [message]);

  useEffect(() => { void load(); }, [load]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(String(info?.config_json || ''));
      message.success('配置已复制，粘到客户端的 mcpServers 里即可');
    } catch {
      message.warning('自动复制被浏览器拦了，请手动选中文本复制');
    }
  };

  if (!info) return <Card size="small" title={title} loading />;

  const groups = (info.by_group || {}) as Record<string, string[]>;
  const spend = (info.spend_tools || []) as string[];

  return (
    <Card size="small" title={title}
      extra={<Button size="small" onClick={load}>刷新</Button>}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Alert type="info" showIcon
          message={`这个工作室可以被 Claude Desktop / Cursor 等 agent 直接驱动（共 ${info.tool_count} 个工具）`}
          description={String(info.note || '')} />
        <Space size={28} wrap>
          <Statistic title="工具数" value={Number(info.tool_count || 0)} />
          <Statistic title="计费工具" value={spend.length}
            suffix={spend.length ? '默认禁用' : ''} />
          <Statistic title="入口脚本" value={info.entry_exists ? '就绪' : '缺失'}
            valueStyle={{ color: info.entry_exists ? '#3f8600' : '#cf1322' }} />
          <Statistic title="出片权（客户端 env）" value={info.allow_spend ? '已开启' : '默认关闭'}
            valueStyle={{ color: info.allow_spend ? '#cf1322' : '#3f8600' }} />
        </Space>
        <div>
          {Object.entries(groups).map(([g, names]) => (
            <div key={g} style={{ marginBottom: 4 }}>
              <Tag>{g}</Tag>
              {(names || []).map((n) => (
                <Tag key={n} color={spend.includes(n) ? 'red' : undefined}>{n}</Tag>
              ))}
            </div>
          ))}
        </div>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          入口：{String(info.entry)}（用 {String(info.python)} 启动）
        </Typography.Text>
        <pre style={{
          margin: 0, padding: 12, borderRadius: 6, maxHeight: 280, overflow: 'auto',
          background: '#fafafa', border: '1px solid #f0f0f0',
          fontFamily: 'monospace', fontSize: 12, lineHeight: 1.5,
        }}>{String(info.config_json)}</pre>
        <Space>
          <Button type="primary" size="small" onClick={copy}>复制配置</Button>
          <Typography.Text type="secondary">
            粘到客户端的 mcpServers 后重启客户端；后端需保持运行。
          </Typography.Text>
        </Space>
      </Space>
    </Card>
  );
}
