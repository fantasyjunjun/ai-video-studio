import { useCallback, useEffect, useState } from 'react';
import {
  Alert, App as AntApp, AutoComplete, Button, Card, Collapse, Drawer, Form, Input, InputNumber,
  Modal, Popconfirm, Space, Table, Tabs, Tag, Typography,
} from 'antd';
import {
  CheckCircleOutlined, DeleteOutlined, EditOutlined, PlusOutlined,
} from '@ant-design/icons';
import { api, type Json } from '../api';
import { BudgetPanel, McpPanel } from '../components';
import {
  DEFAULT_TYPE, FIELDS, GROUP_META, RUNTIME_ONLY, SLOT_KNOWN_KEYS,
  fieldVisible, type FieldDef,
} from '../providerFields';

const SLOT_TITLE: Record<string, string> = {
  llm: '文本大模型（提示词 / 反推 / 文案）',
  image: '文生图',
  video: '图生视频',
};

export default function SettingsPage() {
  const { message } = AntApp.useApp();
  const [data, setData] = useState<Json>({});
  const [yaml, setYaml] = useState('');
  const [drawer, setDrawer] = useState(false);
  const [testing, setTesting] = useState('');

  const [modal, setModal] = useState(false);
  const [editing, setEditing] = useState<{ slot: string; pid: string | null }>({ slot: 'llm', pid: null });
  const [extraJson, setExtraJson] = useState('{}');
  const [form] = Form.useForm();

  // 反推设置：看图的视觉模型覆盖（留空=当前 LLM 模型）+ 单次输出上限
  const [revModel, setRevModel] = useState('');
  const [revMax, setRevMax] = useState<number>(8000);
  const [revSaving, setRevSaving] = useState(false);
  // 当前表单里的接入方式 —— 决定哪些字段该显示（ComfyUI 与中转站的字段集不同，
  // 全摊出来正是"无用字段太多"的根源）。用 state 而非 useWatch：Modal 带
  // destroyOnClose，表单卸载后 useWatch 会取不到值。
  const [formType, setFormType] = useState<string>(DEFAULT_TYPE.llm);

  const load = useCallback(async () => {
    try { setData(await api.providers()); }
    catch (e) { message.error((e as Error).message); }
  }, [message]);

  const loadSettings = useCallback(async () => {
    try {
      const s = await api.settings();
      setRevModel((s.reverse_vision_model as string) || '');
      setRevMax(Number(s.reverse_vision_max_tokens) || 8000);
    } catch (e) { /* 设置是增强项，失败不影响主流程 */ }
  }, []);

  useEffect(() => { load(); loadSettings(); }, [load, loadSettings]);

  const slot = (k: string) => data[k] || { active: '', list: [] };

  const activate = async (s: string, id: string) => {
    try { await api.providersSwitch(s, id); message.success(`${s} 已切到 ${id}`); load(); }
    catch (e) { message.error((e as Error).message); }
  };

  const test = async (s: string, id: string) => {
    setTesting(id);
    try {
      const r = await api.providersTest(s, id);
      if (r.ok) message.success(`${id}：连通${r.reply ? ` · ${String(r.reply).slice(0, 40)}` : ''}`);
      else message.error(`${id}：${r.error || (r.issues || []).join('；') || '不通'}`);
    } catch (e) { message.error((e as Error).message); }
    finally { setTesting(''); }
  };

  const openAdd = (s: string) => {
    setEditing({ slot: s, pid: null });
    const t = DEFAULT_TYPE[s];
    setFormType(t);
    form.resetFields();
    form.setFieldsValue({ type: t });
    setExtraJson('{}');
    setModal(true);
  };

  const openEdit = (s: string, rec: Json) => {
    setEditing({ slot: s, pid: rec.id });
    const known = SLOT_KNOWN_KEYS[s] || new Set<string>();
    const defs = FIELDS[s] || [];
    const init: Json = {};
    for (const f of defs) {
      if (f.group === 'hidden') continue;
      const val = rec[f.key];
      if (val === undefined || val === null) continue;
      init[f.key] = f.type === 'json' && typeof val === 'object' ? JSON.stringify(val, null, 2) : val;
    }
    init.id = rec.id;
    setFormType(String(rec.type || DEFAULT_TYPE[s]));
    form.resetFields();
    form.setFieldsValue(init);

    // 只有落在"本插槽已知字段"之外的，才算用户自定义字段。
    // 少了这道过滤，image 的 image_path / edit_path 会被回填进 video 的 JSON
    // 并随保存写回 video 配置（跨插槽污染）。
    // 空值也一并跳过：后端为保证字段齐全会回传一批 null / {} / []，
    // 照单收下会让用户在一个本应干净的 JSON 框里看到一大片无意义的 null。
    const isEmpty = (v: unknown) =>
      v === null || v === undefined
      || (Array.isArray(v) && v.length === 0)
      || (typeof v === 'object' && !Array.isArray(v) && Object.keys(v as object).length === 0);
    const extra: Json = {};
    for (const [k, v] of Object.entries(rec)) {
      if (known.has(k) || RUNTIME_ONLY.has(k) || isEmpty(v)) continue;
      extra[k] = v;
    }
    setExtraJson(JSON.stringify(extra, null, 2));
    setModal(true);
  };

  const submit = async () => {
    try { await form.validateFields(); } catch { return; }
    const s = editing.slot;
    const v = form.getFieldsValue(true);
    const body: Json = {};
    // 只提交"当前接入方式下可见"的字段：换过 type 的条目往往残留着另一套字段的值，
    // 照单全收会把无关字段写进配置。
    const visible = (FIELDS[s] || []).filter(
      (f) => f.group !== 'hidden' && fieldVisible(f, v.type));
    for (const f of visible) {
      let val = v[f.key];
      if (f.key === 'id' && editing.pid) continue;
      if (val === undefined || val === '') continue;
      if (f.type === 'number') {
        val = Number(val);
        if (Number.isNaN(val)) continue;
      }
      if (f.type === 'json') {
        try { val = JSON.parse(val as string); }
        catch { message.error(`${f.label} 不是合法 JSON`); return; }
      }
      body[f.key] = val;
    }
    // llm 的 type 不在表单里（只有一种适配器），但也不能丢 —— 新增时要显式带上
    if (s === 'llm' && !editing.pid) body.type = DEFAULT_TYPE.llm;
    try {
      const extra = JSON.parse(extraJson || '{}');
      if (extra && typeof extra === 'object') Object.assign(body, extra);
    } catch { message.error('其他字段不是合法 JSON'); return; }

    try {
      if (editing.pid) await api.providersUpdate(s, editing.pid, body);
      else await api.providersCreate(s, body);
      message.success(editing.pid ? '已更新' : '已新增');
      setModal(false); load();
    } catch (e) { message.error((e as Error).message); }
  };

  const remove = async (s: string, id: string) => {
    try { await api.providersDelete(s, id); message.success(`已删除 ${id}`); load(); }
    catch (e) { message.error((e as Error).message); }
  };

  const renderField = (f: FieldDef) => {
    if (f.type === 'number') return <InputNumber style={{ width: '100%' }} placeholder={f.placeholder} />;
    if (f.type === 'password') return <Input.Password placeholder={f.placeholder} />;
    if (f.type === 'type') return (
      <AutoComplete
        options={(f.options || []).map((o) => ({ value: o }))}
        placeholder={f.placeholder}
        filterOption={(input, option) => String(option?.value ?? '').toLowerCase().includes(input.toLowerCase())}
      />
    );
    if (f.type === 'json') return (
      <Input.TextArea rows={3} placeholder={f.placeholder}
        style={{ fontFamily: 'ui-monospace, Menlo, Consolas, monospace', fontSize: 12 }} />
    );
    return <Input placeholder={f.placeholder} />;
  };

  /** 一项的说明：用途 + 怎么配。写在 Form.Item 的 extra 里，紧贴输入框。 */
  const fieldHint = (f: FieldDef) => (
    <span style={{ fontSize: 12, lineHeight: 1.7 }}>
      <Typography.Text type="secondary">{f.purpose}</Typography.Text>
      {f.howto && (
        <>
          <br />
          <Typography.Text type="secondary" style={{ opacity: 0.75 }}>怎么配：{f.howto}</Typography.Text>
        </>
      )}
      {f.fallback && (
        <>
          <br />
          <Typography.Text type="secondary" style={{ opacity: 0.75 }}>留空时用 {f.fallback}。</Typography.Text>
        </>
      )}
    </span>
  );

  const renderItem = (f: FieldDef) => (
    <Form.Item key={f.key} name={f.key} label={f.label} extra={fieldHint(f)}
      rules={f.key === 'id' && !editing.pid ? [{ required: true, message: 'id 必填' }] : undefined}>
      {renderField(f)}
    </Form.Item>
  );

  const columns = (s: string) => [
    {
      title: '供应商', dataIndex: 'id', width: 170,
      render: (val: string, r: Json) => (
        <Space size={4}>
          <Typography.Text strong>{val}</Typography.Text>
          {val === slot(s).active && <Tag color="green">启用中</Tag>}
        </Space>
      ),
    },
    { title: '类型', dataIndex: 'type', width: 150, render: (val: string) => <Tag>{val}</Tag> },
    {
      title: '端点', dataIndex: 'base_url', ellipsis: true,
      render: (val: string, r: Json) => (
        <Space size={4}>
          <Typography.Text code>{val || '—'}</Typography.Text>
          {r.base_url_is_placeholder && <Tag color="orange">占位值</Tag>}
        </Space>
      ),
    },
    { title: '模型/工作流', dataIndex: 'model', width: 180, render: (val: string, r: Json) => val || r.workflow || '—' },
    {
      title: '凭据', width: 230,
      render: (_: any, r: Json) => {
        if (s === 'llm') {
          const names = [r.api_key_env, ...(r.key_rotation || [])].filter(Boolean);
          const ready = Object.values(r.env_ready || {}) as boolean[];
          return (
            <Space size={4} wrap>
              {r.api_key_set && <Tag color="geekblue">密钥已存</Tag>}
              {names.map((n: string) => <Tag key={n} color={r.env_ready?.[n] ? 'green' : 'red'}>{n}</Tag>)}
              {!names.length && !r.api_key_set && <Typography.Text type="secondary">无需 key</Typography.Text>}
              {ready.filter(Boolean).length > 1 && <Tag color="blue">多 key 轮换</Tag>}
            </Space>
          );
        }
        return (
          <Space size={4} wrap>
            {r.token_set && <Tag color="geekblue">令牌已存</Tag>}
            {r.token_env
              ? <Tag color={r.token_ready ? 'green' : 'red'}>{r.token_env}</Tag>
              : !r.token_set && !(r.key_rotation || []).length
                && <Typography.Text type="secondary">—</Typography.Text>}
            {(r.key_rotation || []).map((n: string) => <Tag key={n} color="purple">{n}</Tag>)}
            {(r.key_rotation || []).length > 0 && <Tag color="blue">多 key 轮换</Tag>}
          </Space>
        );
      },
    },
    {
      title: '降级', dataIndex: 'fallback_provider', width: 130,
      render: (val?: string) => val ? <Tag color="purple">{val}</Tag> : '—',
    },
    {
      title: '操作', width: 250,
      render: (_: any, r: Json) => (
        <Space size={4} wrap>
          <Button size="small" type={r.id === slot(s).active ? 'default' : 'primary'}
            disabled={r.id === slot(s).active} onClick={() => activate(s, r.id)}>启用</Button>
          <Button size="small" icon={<CheckCircleOutlined />} loading={testing === r.id}
            onClick={() => test(s, r.id)}>测试</Button>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(s, r)}>编辑</Button>
          <Popconfirm title={`删除 ${r.id}？`} description="删除后不可恢复"
            onConfirm={() => remove(s, r.id)} okText="删除" cancelText="取消"
            okButtonProps={{ danger: true }}>
            <Button size="small" danger icon={<DeleteOutlined />}
              disabled={r.id === slot(s).active}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  // 表单按三档渲染：必填 / 常用 平铺，进阶 折叠。
  const slotDefs = FIELDS[editing.slot] || [];
  const shown = slotDefs.filter((f) => f.group !== 'hidden' && fieldVisible(f, formType));
  const groupOf = (g: string) => shown.filter((f) => f.group === g);
  const manyField = shown.find((f) => f.key === 'type');

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert type="info" showIcon
        message="供应商配置：页面表单直接新增 / 编辑 / 删除，无需改文件"
        description={
          <div>
            <div>每个输入框下面都写了<b>这一项是干什么的、该怎么填</b>。表单只列必需与常用项，
              其余收在「进阶设置」里 —— 那些默认值已经够用，只有对接非标准服务端时才需要动。</div>
            <div>密钥可直接填在「API Key / 令牌」里 —— <b>不会明文写入配置文件</b>，而是存进
              <Typography.Text code>OS 凭据库</Typography.Text>（Windows 凭据管理器 / macOS Keychain / Linux 密钥环）；
              无可用的系统凭据库时自动回退到本地加密文件。前端只显示脱敏标记，不回显、不落明文。</div>
            <div>任意中转站：把「接入方式 type」改成 <Typography.Text code>openai_image</Typography.Text> /
              <Typography.Text code>openai_video</Typography.Text>，填上对方地址与令牌即可；路径或字段名不匹配时，
              用「进阶设置」里的路径与映射项覆盖，代码无需改动。</div>
          </div>
        } />

      <Card size="small" id="reverse-settings"
        title="反推设置（参考片反推 · 看图视觉模型 + 输出上限）">
        <Alert type="info" showIcon style={{ marginBottom: 12 }}
          message="看图反推默认用当前 LLM 模型；若你的中转站上有更快的视觉模型，可在此覆盖"
          description="填模型名（如 gpt-4o / claude-sonnet-4 / gemini-2.0-flash）即可加速看图反推并降低触发 Cloudflare 100s 超时（524）的概率。留空=使用当前 LLM 模型。注意：它复用当前 LLM 的接入地址与同一把密钥，只是换模型名——不需要、也不应在供应商列表里再建一条同地址的条目（那条会因查不到凭据库密钥而鉴权失败）。" />
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <div>
            <Typography.Text strong>看图视觉模型</Typography.Text>
            <div>
              <Input
                placeholder="留空 = 使用当前 LLM 模型"
                value={revModel}
                onChange={(e) => setRevModel(e.target.value)}
                style={{ maxWidth: 480, marginTop: 4 }}
                allowClear
              />
            </div>
          </div>
          <div>
            <Typography.Text strong>单次输出上限（tokens）</Typography.Text>
            <div>
              <InputNumber min={1000} max={32000} step={1000}
                value={revMax} onChange={(v) => setRevMax(Number(v) || 8000)}
                style={{ width: 220, marginTop: 4 }} />
            </div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              逐镜要写「英文七层提示词 + 中文直译」，一份 10~15 镜的输出很长。
              上限太小会让 JSON 在数组中途被截断，报出那句迷惑的「输出不是合法 JSON」。
              默认 8000；若仍被截断可调到 12000~16000。
            </Typography.Text>
          </div>
          <Button type="primary" loading={revSaving} onClick={async () => {
            setRevSaving(true);
            try {
              await api.saveSettings({
                reverse_vision_model: revModel.trim(),
                reverse_vision_max_tokens: String(revMax || 8000),
              });
              message.success('已保存反推设置');
            } catch (e) { message.error((e as Error).message); }
            finally { setRevSaving(false); }
          }}>保存反推设置</Button>
        </Space>
      </Card>

      <Card size="small"
        title="供应商配置（唯一真相源：backend/providers.yaml，页面编辑会保注释写回）"
        extra={<Space>
          <Button size="small" onClick={async () => {
            try { const r = await api.providersYaml(); setYaml(r.text); setDrawer(true); }
            catch (e) { message.error((e as Error).message); }
          }}>原始 YAML（兜底）</Button>
          <Button size="small" onClick={load}>刷新</Button>
        </Space>}>
        <Tabs items={(['llm', 'image', 'video'] as const).map((s) => ({
          key: s,
          label: `${SLOT_TITLE[s]}（启用：${slot(s).active || '—'}）`,
          children: (
            <div>
              <Space style={{ marginBottom: 12 }}>
                <Button size="small" type="primary" icon={<PlusOutlined />}
                  onClick={() => openAdd(s)}>新增供应商</Button>
              </Space>
              <Table size="small" rowKey="id" dataSource={slot(s).list || []}
                pagination={false} columns={columns(s) as any} scroll={{ x: 980 }} />
            </div>
          ),
        }))} />
      </Card>

      <BudgetPanel editable title="花费上限（熔断 · 写回 providers.yaml 的 budget 段）" />

      <McpPanel />

      <Card size="small" title="说明">
        <Space direction="vertical" size={4}>
          <Typography.Text type="secondary">
            · 密钥存储后端：{(data.secret_backend as string) || '—'}（OS 凭据库优先，不可用时回退本地加密文件，绝不明文落盘）
          </Typography.Text>
          {(data.notes || []).map((n: string, i: number) => (
            <Typography.Text key={i} type="secondary">· {n}</Typography.Text>))}
          <Typography.Text type="secondary">
            · 图/视频插槽<b>刻意不做降级</b>：静默换供应商会让同一批镜头出现两种画风，不一致比失败更糟。
          </Typography.Text>
          <Typography.Text type="secondary">
            · 视频插槽的"测试"只检查令牌与地址，<b>不会真的提交任务</b>（提交即计费）。
          </Typography.Text>
        </Space>
      </Card>

      <Modal title={editing.pid ? `编辑供应商 · ${editing.pid}` : `新增供应商 · ${SLOT_TITLE[editing.slot]}`}
        open={modal} onOk={submit} onCancel={() => setModal(false)} width={760}
        okText="保存" cancelText="取消" destroyOnClose>
        <Form form={form} layout="vertical" preserve={false}
          onValuesChange={(chg) => { if (chg.type !== undefined) setFormType(String(chg.type)); }}>
          {groupOf('basic').map(renderItem)}

          {groupOf('common').length > 0 && (
            <div style={{ marginTop: 8, marginBottom: 4 }}>
              <Typography.Text strong style={{ fontSize: 13 }}>{GROUP_META.common.title}</Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                {GROUP_META.common.hint}
              </Typography.Text>
            </div>
          )}
          {groupOf('common').map(renderItem)}

          {groupOf('advanced').length > 0 && (
            <Collapse ghost style={{ marginTop: 8 }} items={[{
              key: 'adv',
              label: (
                <span>
                  <Typography.Text strong style={{ fontSize: 13 }}>{GROUP_META.advanced.title}</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                    {GROUP_META.advanced.hint}（{groupOf('advanced').length} 项）
                  </Typography.Text>
                </span>
              ),
              children: <div style={{ paddingTop: 4 }}>{groupOf('advanced').map(renderItem)}</div>,
            }]} />
          )}

          {manyField && (
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 4 }}>
              当前接入方式：<Typography.Text code>{formType}</Typography.Text>
              —— 上面列出的字段已按它适配。切换「接入方式」会更换后续字段。
            </Typography.Paragraph>
          )}

          <Collapse ghost style={{ marginTop: 4 }} items={[{
            key: 'extra',
            label: (
              <span>
                <Typography.Text strong style={{ fontSize: 13 }}>其他字段（JSON）</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                  仅当服务端有我们没列出的参数时才用
                </Typography.Text>
              </span>
            ),
            children: (
              <>
                <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
                  写在这里的键值对会原样进入该供应商的配置，不被任何适配器限制。
                  上面的表单不认识的字段也会自动出现在这里 —— 留空即 <Typography.Text code>{'{}'}</Typography.Text>。
                </Typography.Paragraph>
                <Input.TextArea rows={4} value={extraJson} onChange={(e) => setExtraJson(e.target.value)}
                  placeholder='{"price_map": {"480p竖": [0.03, 0.02]}}'
                  style={{ fontFamily: 'ui-monospace, Menlo, Consolas, monospace', fontSize: 12 }} />
              </>
            ),
          }]} />
        </Form>
      </Modal>

      <Drawer title="providers.yaml（兜底编辑）" width={760} open={drawer} onClose={() => setDrawer(false)}
        extra={<Button type="primary" onClick={async () => {
          try { await api.providersSave(yaml); message.success('已保存并热重载'); setDrawer(false); load(); }
          catch (e) { message.error((e as Error).message); }
        }}>保存</Button>}>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          保存前会先解析校验：active 必须指向已登记的 id，否则整个应用起不来。
          表单之外的高级用法（例如给某家配一份独一无二的请求体）也可以直接在这里改。
        </Typography.Paragraph>
        <Input.TextArea rows={30} value={yaml} onChange={(e) => setYaml(e.target.value)}
          style={{ fontFamily: 'ui-monospace, Menlo, Consolas, monospace', fontSize: 12 }} />
      </Drawer>
    </Space>
  );
}
