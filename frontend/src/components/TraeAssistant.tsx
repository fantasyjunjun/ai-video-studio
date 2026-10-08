/** Trae 助手（R-19 MVP）：全局悬浮按钮 + 对话抽屉。
 *
 *  - 一条消息 = 后端一次独立的 Trae agent 运行（无状态），历史由本组件保存
 *    并随请求内联 —— 刷新即清空（MVP 不落库）。
 *  - 单条消息可能要等 1-3 分钟（agent 要走几步 LLM），按钮上有耗时提示；
 *    等待期间禁用发送，避免并发把通道打满。
 *  - 走文本大模型通道按 token 计费（与生文同通道），抽屉里明示。
 *  - 在项目详情页内打开时自动带上 project_id，Trae 能看到项目与商品上下文。
 */
import { useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { App as AntApp, Button, Drawer, Input, Modal, Space, Switch, Tag, Typography } from 'antd';
import { RobotOutlined, SendOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { api } from '../api';

type Msg = { role: 'user' | 'assistant'; content: string; warning?: string };

const SUGGESTIONS = [
  '帮我把上一版念白改得更口语化一点',
  '解释一下什么是首帧参考图，为什么它影响一致性',
  '我的视频里出现油珠穿帮，一般是什么原因？',
];

const AUTO_RISKS = [
  '自动化模式会让 Trae 获得 bash 执行能力：它可以运行任意命令（写代码、跑脚本、处理文件）。',
  '⚠️ 命令以运行本软件的当前用户身份直接执行，没有沙箱隔离 —— 工作目录只是软约束，理论上可访问本机任何该用户可及的文件。',
  '⚠️ bash 可发起网络请求，绕过软件内部的预算闸门（shell 里的请求不计入花费台账）。',
  '⚠️ 任务一次性跑完，没有逐步确认；步数与超时上限是唯一的失控兜底。',
  '后端已通过提示词约束它禁止出工作目录、禁止联网下载、禁止删除操作，但这些是软约束，不是硬隔离。',
  '建议：只在让它处理软件工作目录内任务时开启；敏感操作（删除、批量改动）请先自行备份。',
];

export default function TraeAssistant() {
  const loc = useLocation();
  const { message: antMsg } = AntApp.useApp();
  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [auto, setAuto] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const pendingAutoRef = useRef<string | null>(null);

  // 项目详情页 → 自动带 project_id（/projects/:id/...）
  const projectId = Number(loc.pathname.match(/^\/projects\/(\d+)/)?.[1]) || null;

  useEffect(() => {
    if (!busy) {
      setElapsed(0);
      return;
    }
    const t = window.setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => window.clearInterval(t);
  }, [busy]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' });
  }, [msgs, busy]);

  const send = async (text?: string) => {
    const content = (text ?? input).trim();
    if (!content || busy) return;
    const useAuto = auto;
    const history = msgs.map((m) => ({ role: m.role, content: m.content }));
    setMsgs((prev) => [...prev, { role: 'user', content: useAuto ? `⚡[自动化] ${content}` : content }]);
    setInput('');
    setBusy(true);
    try {
      const r = await api.traeChat({
        project_id: projectId,
        message: content,
        history,
        auto: useAuto,
      });
      setMsgs((prev) => [...prev, {
        role: 'assistant', content: r.reply || '（空回答）',
        warning: r.warning || '',
      }]);
    } catch (e) {
      setMsgs((prev) => [...prev, {
        role: 'assistant',
        content: `⚠️ 调用失败：${(e as Error).message}`,
      }]);
      antMsg.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // 自动化模式发送前先弹风险确认（每次都确认，不记住选择）
  const sendGuarded = (text?: string) => {
    const content = (text ?? input).trim();
    if (!content || busy) return;
    if (!auto) {
      void send(content);
      return;
    }
    pendingAutoRef.current = content;
    Modal.confirm({
      title: '开启自动化执行？',
      width: 520,
      okText: '我已了解风险，执行',
      okButtonProps: { danger: true },
      cancelText: '取消',
      content: (
        <div style={{ fontSize: 13, lineHeight: 1.8, maxHeight: 320, overflowY: 'auto' }}>
          {AUTO_RISKS.map((r, i) => <p key={i} style={{ marginBottom: 6 }}>{r}</p>)}
          <p style={{ marginTop: 8 }}><b>本次任务：</b>{content}</p>
        </div>
      ),
      onOk: () => { const c = pendingAutoRef.current || content; pendingAutoRef.current = null; void send(c); },
      onCancel: () => { pendingAutoRef.current = null; },
    });
  };

  const bubbles = (m: Msg, i: number) => (
    <div key={i} style={{ display: 'flex', justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start' }}>
      <div style={{
        maxWidth: '86%', padding: '8px 12px', borderRadius: 10, fontSize: 13,
        lineHeight: 1.65, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        background: m.role === 'user' ? 'rgba(22,119,255,0.12)' : 'var(--cf-card, rgba(255,255,255,0.04))',
        border: '1px solid var(--cf-border, rgba(255,255,255,0.08))',
      }}>
        {m.content}
        {m.warning && (
          <div style={{ marginTop: 6, fontSize: 12, color: '#faad14' }}>⚠ {m.warning}</div>
        )}
      </div>
    </div>
  );

  return (
    <>
      {!open && (
        <Button
          type="primary" shape="circle" size="large" icon={<RobotOutlined />}
          onClick={() => setOpen(true)}
          title="Trae 助手"
          style={{ position: 'fixed', right: 24, bottom: 24, zIndex: 900, width: 48, height: 48 }}
        />
      )}
      <Drawer
        title={<Space><RobotOutlined /> Trae 助手 {projectId && <Tag color="blue">项目 #{projectId}</Tag>}</Space>}
        width={420}
        open={open}
        onClose={() => setOpen(false)}
        styles={{ body: { display: 'flex', flexDirection: 'column', padding: '12px 16px' } }}
        extra={
          <Space size={6}>
            <ThunderboltOutlined style={{ color: auto ? '#faad14' : undefined }} />
            <span style={{ fontSize: 12 }}>自动化</span>
            <Switch size="small" checked={auto} onChange={setAuto} />
          </Space>
        }
      >
        <Typography.Text type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
          每条消息由 Trae 智能体独立完成，约 1-3 分钟；按 token 计费（与生文同通道）。
          {auto && (
            <span style={{ color: '#faad14' }}>
              自动化模式已开启：Trae 将获得 bash 执行能力（无沙箱隔离），每条消息发送前都会先弹出风险确认。
            </span>
          )}
        </Typography.Text>
        <div ref={listRef} style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 10, paddingBottom: 8 }}>
          {msgs.length === 0 && (
            <div style={{ color: 'var(--cf-text-secondary, #999)', fontSize: 13, lineHeight: 1.8 }}>
              问问它关于脚本、提示词、穿帮排查的问题。试试：
              <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
                {SUGGESTIONS.map((s) => (
                  <Button key={s} size="small" type="dashed" style={{ textAlign: 'left', whiteSpace: 'normal', height: 'auto' }}
                    onClick={() => sendGuarded(s)}>{s}</Button>
                ))}
              </div>
            </div>
          )}
          {msgs.map(bubbles)}
          {busy && (
            <div style={{ color: 'var(--cf-text-secondary, #999)', fontSize: 13 }}>
              Trae 正在工作…（已 {elapsed}s，可在任务台账看进度）
            </div>
          )}
        </div>
        <div style={{ display: 'flex', gap: 8, paddingTop: 8, borderTop: '1px solid var(--cf-border)' }}>
          <Input.TextArea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={auto ? '自动化模式：描述要执行的任务，发送前需确认风险' : '输入消息，Ctrl+Enter 发送'}
            autoSize={{ minRows: 1, maxRows: 4 }}
            disabled={busy}
            onPressEnter={(e) => {
              if (e.ctrlKey || e.metaKey) sendGuarded();
            }}
          />
          <Button type="primary" icon={<SendOutlined />} loading={busy}
            disabled={!input.trim()} onClick={() => sendGuarded()} />
        </div>
      </Drawer>
    </>
  );
}
