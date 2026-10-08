import { useCallback, useEffect, useState } from 'react';
import {
  Alert, App as AntApp, Button, Card, Select, Space, Table, Tag, Typography,
} from 'antd';
import { ReloadOutlined, SafetyCertificateOutlined, SyncOutlined } from '@ant-design/icons';
import { api, type Json } from '../api';

const STATUS_COLOR: Record<string, string> = {
  submitting: 'blue', submitted: 'geekblue', running: 'processing',
  succeeded: 'green', failed: 'red', unknown: 'orange',
};
const STATUS_LABEL: Record<string, string> = {
  submitting: '提交中', submitted: '已提交', running: '生成中',
  succeeded: '成功', failed: '失败', unknown: '未知(需对账)',
};

export default function TasksPage() {
  const { message } = AntApp.useApp();
  const [data, setData] = useState<Json>({ tasks: [], statuses: [] });
  const [status, setStatus] = useState<string | undefined>(undefined);
  const [busy, setBusy] = useState('');

  const load = useCallback(async () => {
    try { setData(await api.listRenderTasks({ status, limit: 200 })); }
    catch (e) { message.error((e as Error).message); }
  }, [message, status]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 5000);   // 轻轮询：出片是异步的
    return () => window.clearInterval(t);
  }, [load]);

  const recover = async () => {
    setBusy('recover');
    try {
      const r = await api.recoverRenderTasks(0);  // 0 = 立刻把卡住的 submitting 视为孤儿
      message.success(`巡检完成：扫描 ${r.scanned} 条，标记孤儿 ${r.marked_unknown} 条`);
      load();
    } catch (e) { message.error((e as Error).message); }
    finally { setBusy(''); }
  };

  const resume = async (id: number) => {
    setBusy(`r${id}`);
    try {
      const r = await api.resumeRenderTask(id);
      if (r.ok) message.success(`已按 taskId 恢复：${r.status}`);
      else message.error(`恢复失败：${r.error || r.status}`);
      load();
    } catch (e) { message.error((e as Error).message); }
    finally { setBusy(''); }
  };

  const orphans = (data.tasks || []).filter((t: Json) => t.status === 'unknown').length;

  const columns = [
    { title: '台账', dataIndex: 'id', width: 70 },
    { title: 'job', dataIndex: 'job_id', width: 70, render: (v: any) => v ?? '—' },
    { title: '类型', dataIndex: 'kind', width: 80, render: (v: string) => <Tag>{v}</Tag> },
    { title: '供应商', dataIndex: 'provider_id', width: 130 },
    {
      title: '厂商 task_id', dataIndex: 'provider_task_id', width: 190, ellipsis: true,
      render: (v: string) => v ? <Typography.Text code>{v}</Typography.Text>
        : <Typography.Text type="secondary">—</Typography.Text>,
    },
    {
      title: '状态', dataIndex: 'status', width: 120,
      render: (v: string) => <Tag color={STATUS_COLOR[v] || 'default'}>{STATUS_LABEL[v] || v}</Tag>,
    },
    { title: '费用(元)', dataIndex: 'cost_cny', width: 90,
      render: (v: number) => (v ? `¥${Number(v).toFixed(3)}` : '—') },
    { title: '提交时间', dataIndex: 'submitted_at', width: 180,
      render: (v: string) => v ? v.replace('T', ' ').slice(0, 19) : '—' },
    { title: '说明', dataIndex: 'message', ellipsis: true },
    {
      title: '操作', width: 100,
      render: (_: any, r: Json) => (
        <Button size="small" icon={<SyncOutlined />}
          loading={busy === `r${r.id}`}
          disabled={!r.provider_task_id || r.status === 'succeeded'}
          onClick={() => resume(r.id)}>恢复</Button>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert type="warning" showIcon icon={<SafetyCertificateOutlined />}
        message="渲染任务台账：出片的每一笔提交都记账，「提交即计费」绝不盲目重试"
        description={
          <div>
            <div>出片是<b>提交即计费</b>的非幂等操作。提交前先落 <Typography.Text code>submitting</Typography.Text>，
              拿到厂商 task_id 后落 <Typography.Text code>submitted</Typography.Text>；超时/网络错误时
              <b>不自动重试</b>（会重复下单），标为 <Typography.Text code>unknown</Typography.Text> 请你到平台对账。</div>
            <div>「恢复」= 按已有厂商 task_id 重新接上轮询并下载，<b>不会重新提交、不重复计费</b>。</div>
          </div>
        } />

      <Card size="small"
        title={`任务台账（${(data.tasks || []).length} 条${orphans ? `，其中 ${orphans} 条待对账` : ''}）`}
        extra={<Space>
          <Select allowClear placeholder="按状态筛选" style={{ width: 160 }} value={status}
            onChange={(v) => setStatus(v)}
            options={(data.statuses || []).map((s: string) => ({
              value: s, label: STATUS_LABEL[s] || s,
            }))} />
          <Button size="small" icon={<SafetyCertificateOutlined />}
            loading={busy === 'recover'} onClick={recover}>巡检孤儿</Button>
          <Button size="small" icon={<ReloadOutlined />} onClick={load}>刷新</Button>
        </Space>}>
        <Table size="small" rowKey="id" dataSource={data.tasks || []}
          pagination={{ pageSize: 20 }} columns={columns as any} scroll={{ x: 1180 }} />
      </Card>

      <Card size="small" title="说明">
        <Space direction="vertical" size={4}>
          <Typography.Text type="secondary">
            · <b>unknown</b> = 进程在"提交已发出、task_id 未取得"之间中断，钱可能已经花了却无从对账 →
            请到平台按时间核对任务列表，<b>不要在平台外重复下单</b>。
          </Typography.Text>
          <Typography.Text type="secondary">
            · 计费请求只有 HTTP 429（限流、未受理）才重试；超时 / 网络错误 / 5xx 一律停下等人工确认。
          </Typography.Text>
          <Typography.Text type="secondary">
            · 轮询与下载不计费，网络抖动会安全重试。
          </Typography.Text>
        </Space>
      </Card>
    </Space>
  );
}
