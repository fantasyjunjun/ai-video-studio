import { useEffect, useState } from 'react';
import {
  Routes, Route, Navigate, useLocation, useNavigate, useParams,
} from 'react-router-dom';
import { App as AntApp, Badge, Layout, Menu, Space, Tag, Typography } from 'antd';
import {
  AppstoreOutlined, FundProjectionScreenOutlined,
  NodeIndexOutlined, PictureOutlined, SafetyCertificateOutlined,
  SettingOutlined, TeamOutlined, ThunderboltOutlined, UserOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons';
import Dashboard from './pages/Dashboard';
import ProductsPage from './pages/ProductsPage';
import PresentersPage from './pages/PresentersPage';
import RemakePage from './pages/RemakePage';
import ReversePage from './pages/ReversePage';
import AudioPage from './pages/AudioPage';
import DeliverPage from './pages/DeliverPage';
import SettingsPage from './pages/SettingsPage';
import BatchPage from './pages/BatchPage';
import TasksPage from './pages/TasksPage';
import { api } from './api';
import TraeAssistant from './components/TraeAssistant';

const { Sider, Content } = Layout;

/** 路由 → 侧边栏高亮项。**顺序有意义**：`/products` 必须排在 `/` 之前判断，
 *  否则一切路径都会被 `/` 吃掉（前缀匹配的经典坑）。 */
const ROUTES: [string, string][] = [
  ['/products', '/products'],
  ['/presenters', '/presenters'],
  ['/remake', '/remake'],
  ['/reverse', '/reverse'],
  ['/batch', '/batch'],
  ['/tasks', '/tasks'],
  ['/settings', '/settings'],
];

/** 把带 `:id` 的旧地址重定向到新地址（保留参数）。
 *
 *  「分镜」与「任务」两个页面已并入成片页（见 DeliverPage 顶部说明），
 *  但旧书签 / 浏览器历史里的地址还要能用 —— 直接 404 会让人以为片子没了。 */
function RedirectTo({ to }: { to: string }) {
  const { id } = useParams();
  return <Navigate to={to.replace(':id', String(id))} replace />;
}

function BackendStatus() {
  const [ok, setOk] = useState<boolean | null>(null);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        await api.health();
        if (alive) setOk(true);
      } catch {
        if (alive) setOk(false);
      }
    };
    void tick();
    const t = window.setInterval(tick, 15000);
    return () => { alive = false; window.clearInterval(t); };
  }, []);
  return (
    <Space size={6}>
      <Badge status={ok === null ? 'processing' : ok ? 'success' : 'error'} />
      <Typography.Text type={ok === false ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>
        {ok === null ? '连接中' : ok ? '后端在线' : '后端未响应'}
      </Typography.Text>
    </Space>
  );
}

export default function App() {
  const loc = useLocation();
  const nav = useNavigate();

  const selected = ROUTES.find(([p]) => loc.pathname.startsWith(p))?.[1] || '/';

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider width={212} breakpoint="lg" collapsedWidth={64}
        style={{ background: '#0e0e14', borderRight: '1px solid var(--cf-border)' }}>
        <div style={{
          padding: '18px 16px 12px', fontWeight: 600, fontSize: 15,
          display: 'flex', alignItems: 'center', gap: 8,
        }}>
          <span className="cf-brand-text" style={{ fontSize: 18 }}><VideoCameraOutlined /></span>
          <span className="cf-brand-text">AI 视频工作室</span>
        </div>
        <Menu
          mode="inline"
          theme="dark"
          selectedKeys={[selected]}
          onClick={({ key }) => nav(key)}
          style={{ background: 'transparent', borderInlineEnd: 0 }}
          items={[
            {
              type: 'group', label: '创作',
              children: [
                { key: '/', icon: <AppstoreOutlined />, label: '项目工作台' },
                { key: '/remake', icon: <ThunderboltOutlined />, label: '爆款复刻' },
                { key: '/reverse', icon: <FundProjectionScreenOutlined />, label: '反推工作台' },
                { key: '/batch', icon: <NodeIndexOutlined />, label: '批量变体' },
              ],
            },
            {
              type: 'group', label: '资产',
              children: [
                { key: '/products', icon: <PictureOutlined />, label: '商品库' },
                { key: '/presenters', icon: <UserOutlined />, label: '主播库' },
              ],
            },
            {
              type: 'group', label: '系统',
              children: [
                { key: '/tasks', icon: <SafetyCertificateOutlined />, label: '任务台账' },
                { key: '/settings', icon: <SettingOutlined />, label: '供应商设置' },
              ],
            },
          ]}
        />
      </Sider>

      <Layout style={{ background: 'var(--cf-bg)' }}>
        <div style={{
          height: 56, display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '0 20px', borderBottom: '1px solid var(--cf-border)',
          background: 'rgba(255,255,255,0.015)',
        }}>
          <Space>
            <Typography.Text strong>电商短视频一站式生成</Typography.Text>
            <Tag color="orange">提示词 lint 门禁</Tag>
            <Tag color="blue">供应商可插拔</Tag>
            <Tag icon={<TeamOutlined />} color="purple">商品库 / 主播库</Tag>
          </Space>
          <BackendStatus />
        </div>
        <Content style={{ minHeight: 0 }}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/products" element={<ProductsPage />} />
            <Route path="/presenters" element={<PresentersPage />} />
            <Route path="/reverse" element={<ReversePage />} />
            <Route path="/remake" element={<RemakePage />} />
            <Route path="/batch" element={<BatchPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/projects/:id/deliver" element={<DeliverPage />} />
            {/* 音频后期保留，但入口收进成片页的「高级」——它是排查工具，不是主流程 */}
            <Route path="/projects/:id/audio" element={<AudioPage />} />
            {/* 分镜 / 任务两页已并入成片页，旧地址重定向（不 404） */}
            <Route path="/projects/:id/storyboard"
              element={<RedirectTo to="/projects/:id/deliver" />} />
            <Route path="/projects/:id/jobs"
              element={<RedirectTo to="/projects/:id/deliver" />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Content>
        {/* Trae 助手：全局悬浮按钮 + 对话抽屉（R-19） */}
        <TraeAssistant />
      </Layout>
    </Layout>
  );
}
