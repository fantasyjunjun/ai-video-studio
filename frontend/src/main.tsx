import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App as AntApp, ConfigProvider, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import 'antd/dist/reset.css';
import './styles.css';
import App from './App';

/** 暗色主题（对齐 ClipForge 的观感，用 AntD 主题算法实现）。
 *
 *  主色沿用项目原有的暖棕 `#c08b52` 而不是照搬 ClipForge 的紫：
 *  暖棕在暗底上足够跳、且与香水/美妆的调性一致；换紫色会让整套已调过的
 *  视觉稿（图表、badge、状态色）全部失去参照。
 *  `colorBgBase` 必须显式给，否则 darkAlgorithm 会用它自己的 #141414，
 *  和我们 CSS 里的 #0b0b10 对不上、页面会出现两层底色。
 */
createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.darkAlgorithm,
        token: {
          colorPrimary: '#c08b52',
          colorBgBase: '#0b0b10',
          colorBgContainer: '#15151d',
          colorBgElevated: '#1b1b24',
          colorBorder: 'rgba(255,255,255,0.12)',
          colorBorderSecondary: 'rgba(255,255,255,0.08)',
          colorText: '#e8e8ee',
          colorTextSecondary: '#9a9aa8',
          borderRadius: 6,
        },
      }}
    >
      <AntApp>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>,
);
