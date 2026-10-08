import { useCallback, useEffect, useRef, useState } from 'react';
import {
  App as AntApp, Button, Card, Col, Descriptions, Modal, Row, Select, Space,
  Statistic, Table, Tag, Typography, Input,
} from 'antd';
import { api, fileUrl, type Json } from '../api';

/** 状态色：让"正在跑"一眼可见。 */
const STATUS_COLOR: Record<string, string> = {
  planned: 'default', running: 'processing', done: 'success', failed: 'error',
};

export default function BatchPage() {
  const { message } = AntApp.useApp();
  const [runs, setRuns] = useState<Json[]>([]);
  const [cur, setCur] = useState<Json | null>(null);       // 当前选中的批量运行
  const [projects, setProjects] = useState<Json[]>([]);
  const [mkOpen, setMkOpen] = useState(false);
  const [mkPid, setMkPid] = useState<number | undefined>();
  const [manifestText, setManifestText] = useState('');
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const pollRef = useRef<number | null>(null);

  const loadRuns = useCallback(async () => {
    try { setRuns(await api.listBatch()); } catch (e) { message.error((e as Error).message); }
  }, [message]);

  const loadRun = useCallback(async (id: number) => {
    try {
      const r = await api.getBatch(id);
      setCur(r);
      setManifestText(JSON.stringify(r.manifest || {}, null, 2));
    } catch (e) { message.error((e as Error).message); }
  }, [message]);

  useEffect(() => { loadRuns(); }, [loadRuns]);
  useEffect(() => {
    if (!cur) return;
    if (cur.status === 'running') {
      if (pollRef.current == null) {
        pollRef.current = window.setInterval(async () => {
          try {
            const r = await api.getBatch(cur.id);
            setCur(r);
            if (r.status !== 'running') {
              if (pollRef.current != null) window.clearInterval(pollRef.current);
              pollRef.current = null;
            }
          } catch { /* 轮询失败静默，下次再试 */ }
        }, 2000);
      }
    } else if (pollRef.current != null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current != null) window.clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [cur?.id, cur?.status]);

  const openMk = async () => {
    try { setProjects(await api.listProjects()); } catch (e) { message.error((e as Error).message); }
    setMkOpen(true);
  };

  const createFromProject = async () => {
    if (!mkPid) { message.warning('先选一个项目'); return; }
    setSaving(true);
    try {
      const tpl = await api.batchTemplate(mkPid);
      const r = await api.createBatch({ name: tpl.name, project_id: mkPid, manifest: tpl });
      setMkOpen(false);
      await loadRuns();
      await loadRun(r.id);
      message.success('已按项目导出 manifest 初稿（视觉单元 1 个，念白待填）');
    } catch (e) { message.error((e as Error).message); }
    finally { setSaving(false); }
  };

  const saveManifest = async () => {
    if (!cur) return;
    let parsed: Json;
    try { parsed = JSON.parse(manifestText); }
    catch { message.error('JSON 不合法，先修好再保存'); return; }
    setSaving(true);
    try {
      await api.setBatchManifest(cur.id, parsed);
      message.success('已保存并重新计算计划');
      await loadRun(cur.id);
    } catch (e) { message.error((e as Error).message); }
    finally { setSaving(false); }
  };

  const runIt = async (dry: boolean) => {
    if (!cur) return;
    setRunning(true);
    try {
      await api.runBatch(cur.id, { dry_run: dry });
      message[dry ? 'info' : 'success'](dry ? '试算已提交（不花钱）' : '出片已提交，后台执行中');
      await loadRun(cur.id);
    } catch (e) { message.error((e as Error).message); }
    finally { setRunning(false); }
  };

  const del = async (id: number) => {
    try {
      await api.deleteBatch(id);
      message.success('已删除（磁盘产物保留）');
      if (cur?.id === id) setCur(null);
      await loadRuns();
    } catch (e) { message.error((e as Error).message); }
  };

  const plan = cur?.plan || {} as Json;
  const tasks = (cur?.tasks || []) as Json[];
  const films = ((cur?.report || {}) as Json).films || [];

  const taskColumns = [
    { title: '类型', dataIndex: 'kind', key: 'kind',
      render: (k: string) => <Tag>{k}</Tag> },
    { title: '键', dataIndex: 'key', key: 'key' },
    { title: '状态', dataIndex: 'status', key: 'status',
      render: (s: string) => <Tag color={s === 'succeeded' ? 'success' : s === 'failed' ? 'error' : 'default'}>{s}</Tag> },
    { title: '消息', dataIndex: 'message', key: 'message', ellipsis: true },
  ];
  const filmColumns = [
    { title: '视觉单元', dataIndex: 'unit', key: 'unit' },
    { title: '语言', dataIndex: 'lang', key: 'lang' },
    { title: '状态', dataIndex: 'status', key: 'status',
      render: (s: string) => <Tag color={s === 'succeeded' ? 'success' : 'error'}>{s}</Tag> },
    { title: '成片', dataIndex: 'path', key: 'path',
      render: (p: string, row: Json) =>
        row.status === 'succeeded' && p
          ? <a href={fileUrl(p)} target="_blank" rel="noreferrer">下载 {p.split(/[\\/]/).pop()}</a>
          : <Typography.Text type="secondary">—</Typography.Text> },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card size="small" title="批量变体编排"
        extra={<Button type="primary" size="small" onClick={openMk}>从项目新建</Button>}>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
          一份 manifest 跑 N 份成片。<b>视觉层（产品/模特/比例）每个单元只出片一次</b>，
          语言变体只换念白重新混流 —— 矩阵条数涨、出片次数不涨。<b>计划阶段不花钱</b>，
          看清代价再决定要不要真出片。
        </Typography.Paragraph>
        <Table rowKey="id" size="small" dataSource={runs} pagination={false}
          onRow={(r) => ({ onClick: () => loadRun(r.id), style: { cursor: 'pointer' } })}
          columns={[
            { title: '#', dataIndex: 'id', key: 'id', width: 60 },
            { title: '名称', dataIndex: 'name', key: 'name' },
            { title: '状态', dataIndex: 'status', key: 'status',
              render: (s: string) => <Tag color={STATUS_COLOR[s] || 'default'}>{s}</Tag> },
            { title: '出片', key: 'rc',
              render: (_: any, r: Json) => (r.plan?.render_calls ?? '—') },
            { title: '成片', key: 'mc',
              render: (_: any, r: Json) => (r.plan?.mux_calls ?? '—') },
            { title: '成本', dataIndex: 'cost_cny', key: 'cost_cny',
              render: (c: number) => `¥${(c || 0).toFixed(2)}` },
            { title: '', key: 'op', width: 80,
              render: (_: any, r: Json) =>
                <Button size="small" danger type="link" onClick={(e) => { e.stopPropagation(); del(r.id); }}>删</Button> },
          ]} />
      </Card>

      {cur && (
        <>
          <Card size="small" title={`计划矩阵 · #${cur.id} ${cur.name}`}>
            <Row gutter={12}>
              <Col span={6}><Statistic title="出片次数（真花钱）" value={plan.render_calls ?? 0} /></Col>
              <Col span={6}><Statistic title="念白次数（按语言去重）" value={plan.narration_calls ?? 0} /></Col>
              <Col span={6}><Statistic title="成片条数（× 矩阵）" value={plan.final_films ?? 0} /></Col>
              <Col span={6}><Statistic title="费用预估" prefix="¥" value={(plan.cost_estimate_cny ?? 0)} precision={2} /></Col>
            </Row>
            <Typography.Paragraph style={{ marginTop: 8, fontSize: 12 }}>
              朴素做法要出 <b>{plan.naive_render_calls ?? 0}</b> 次；本次去重后省下{' '}
              <b style={{ color: '#cf1322' }}>{plan.saved_render_calls ?? 0}</b> 次出片。
            </Typography.Paragraph>
            {(plan.notes || []).map((n: string, i: number) => (
              <Tag key={i} color="warning" style={{ marginTop: 4 }}>{n}</Tag>
            ))}
          </Card>

          <Card size="small" title="工作流源（manifest）"
            extra={<Space>
              <Button size="small" loading={saving} onClick={saveManifest}>保存并重算</Button>
              <Button size="small" onClick={() => runIt(true)} loading={running}>试算（不花钱）</Button>
              <Button size="small" type="primary" onClick={() => runIt(false)} loading={running}>正式出片</Button>
            </Space>}>
            <Input.TextArea value={manifestText} onChange={(e) => setManifestText(e.target.value)}
              autoSize={{ minRows: 10, maxRows: 26 }} style={{ fontFamily: 'monospace', fontSize: 12 }}
              placeholder="JSON：film / visual_units / languages / audio" />
          </Card>

          <Card size="small" title={`执行状态：${cur.status}`}
            extra={<Tag color={STATUS_COLOR[cur.status] || 'default'}>{cur.status}</Tag>}>
            <Table rowKey="id" size="small" dataSource={tasks} pagination={false}
              columns={taskColumns} />
          </Card>

          {films.length > 0 && (
            <Card size="small" title="成片结果">
              <Table rowKey={(r) => `${r.unit}-${r.lang}`} size="small" dataSource={films}
                pagination={false} columns={filmColumns} />
            </Card>
          )}
        </>
      )}

      <Modal title="从项目新建批量" open={mkOpen} onOk={createFromProject}
        confirmLoading={saving} onCancel={() => setMkOpen(false)}>
        <Select style={{ width: '100%' }} placeholder="选择已跑通的项目"
          value={mkPid} onChange={setMkPid}
          options={projects.map((p) => ({ value: p.id, label: `${p.id} · ${p.name}（${p.language}）` }))} />
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
          会导出一份 manifest 初稿（视觉单元 1 个、镜头提示词已带、念白文案待你填）。
          要加产品/礼服变体 = 复制一个 visual_unit，它会独立出片一次，而不是重跑整套。
        </Typography.Paragraph>
      </Modal>
    </Space>
  );
}
