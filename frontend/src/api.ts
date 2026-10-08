/** 后端 API 封装。
 *
 * 约定：
 *  - 开发走 Vite 代理（`vite.config.ts`），生产是 FastAPI 单端口，所以 BASE 恒为空、
 *    全部用相对路径 —— 换部署地址不用改代码。
 *  - 出错时把 FastAPI 的 `detail` 原样抛出来，别让前端自己编一句"请求失败"。
 */

export type Json = Record<string, any>;

async function request<T = any>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    });
  } catch (e) {
    throw new Error(`连不上后端（${path}）：${(e as Error).message}`);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j?.detail === 'string' ? j.detail : JSON.stringify(j).slice(0, 300);
    } catch {
      /* 非 JSON 响应就用状态文本 */
    }
    throw new Error(`${res.status} ${detail}`);
  }
  const text = await res.text();
  if (!text) return undefined as unknown as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    return text as unknown as T;
  }
}

const get = <T = any>(p: string) => request<T>(p);
const post = <T = any>(p: string, body?: unknown) =>
  request<T>(p, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });
const patch = <T = any>(p: string, body?: unknown) =>
  request<T>(p, { method: 'PATCH', body: body === undefined ? undefined : JSON.stringify(body) });
const put = <T = any>(p: string, body?: unknown) =>
  request<T>(p, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) });
const del = <T = any>(p: string) => request<T>(p, { method: 'DELETE' });

/** 本地文件预览：只拼 URL，不搬字节（视频/抽帧图都走它）。 */
export const fileUrl = (path: string) => `/api/file?path=${encodeURIComponent(path)}`;

export const api = {
  // ---------------- 概览 / 项目
  health: () => get('/api/health'),
  stats: () => get('/api/stats'),
  listProjects: () => get('/api/projects'),
  createProject: (body: Json) => post('/api/projects', body),
  getProject: (id: number) => get(`/api/projects/${id}`),
  updateProject: (id: number, body: Json) => patch(`/api/projects/${id}`, body),
  deleteProject: (id: number) => del(`/api/projects/${id}`),
  timeline: (id: number, fps = 24) => get(`/api/projects/${id}/timeline?fps=${fps}`),
  cost: (id: number) => get(`/api/projects/${id}/cost`),
  tracks: (id: number) => get(`/api/projects/${id}/audio-tracks`),

  // ---------------- 分镜
  listShots: (pid: number) => get(`/api/projects/${pid}/shots`),
  patchShot: (sid: number, body: Json) => patch(`/api/shots/${sid}`, body),
  deleteShot: (sid: number) => del(`/api/shots/${sid}`),
  relintShot: (sid: number) => post(`/api/shots/${sid}/lint`),
  lintPrompt: (prompt: string, duration: number) =>
    post('/api/lint', { prompt, duration }),
  storyboard: (pid: number, body: Json) => post(`/api/projects/${pid}/storyboard`, body),

  // ---------------- Trae Agent 生成脚本分镜提示词
  generateScript: (pid: number, body: Json) => post(`/api/projects/${pid}/generate-script`, body),

  // ---------------- Trae 助手（R-19：内嵌对话面板）
  // 无状态：历史由前端保存并随请求带上；单条消息可能跑 1-3 分钟（agent 多步）。
  traeChat: (body: Json) => post('/api/trae/chat', body),
  /** 把分镜脚本 markdown 解析并导入为项目分镜（按镜号幂等 upsert）。
   *
   *  脚本既可以是 Trae 的产出，也可以是用户**从外部粘贴**的脚本
   *  （解析器对 ## 标题 / Shot N / 无围栏等写法有宽松兜底）。
   *  传 `dry_run: true` 时只解析 + 打分不落库，用于导入前的「解析预览」。 */
  importScript: (pid: number, body: Json) => post(`/api/projects/${pid}/import-script`, body),
  /** 项目的脚本素材（一版一条，按时间倒序）。脚本是成片的事实源：
   *  镜号 / 时长 / 念白稿 / 音效落点都在里面，全文用返回的 path 走 /api/file?path=。 */
  projectScripts: (pid: number) => get(`/api/projects/${pid}/scripts`),
  /** 最新脚本解析出的后期计划（念白行 + 音效落点），给「一键成片」做前置预览。 */
  projectPostPlan: (pid: number) => get(`/api/projects/${pid}/post-plan`),

  // ---------------- 渲染
  renderShot: (sid: number, body: Json) => post(`/api/shots/${sid}/render`, body),
  getJob: (jid: number) => get(`/api/jobs/${jid}`),
  listJobs: (pid: number) => get(`/api/projects/${pid}/jobs`),
  /** 项目 i2v 参考图（产品图 + 模特图）：预览"出片会喂哪些图"。 */
  projectRefs: (pid: number) => get(`/api/projects/${pid}/refs`),
  mediaProviders: () => get('/api/providers/media'),
  unpublishHosts: (pid: number) => post(`/api/projects/${pid}/hosts/unpublish`),

  // ---------------- 渲染任务台账（P-2：计费安全）
  listRenderTasks: (params?: { status?: string; project_id?: number; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.status) qs.set('status', params.status);
    if (params?.project_id != null) qs.set('project_id', String(params.project_id));
    if (params?.limit != null) qs.set('limit', String(params.limit));
    const q = qs.toString();
    return get(`/api/render/tasks${q ? `?${q}` : ''}`);
  },
  recoverRenderTasks: (stale_sec = 1800) =>
    post('/api/render/tasks/recover', { stale_sec }),
  resumeRenderTask: (tid: number) => post(`/api/render/tasks/${tid}/resume`),

  // ---------------- 后期
  onset: (sid: number, body: Json = {}) => post(`/api/shots/${sid}/onset`, body),
  narrationRaw: (pid: number, body: Json) =>
    post(`/api/projects/${pid}/narration/raw`, body),
  narrationPlan: (pid: number, body: Json) =>
    post(`/api/projects/${pid}/narration/plan`, body),
  narration: (pid: number, body: Json) => post(`/api/projects/${pid}/narration`, body),
  bgm: (pid: number, body: Json) => post(`/api/projects/${pid}/bgm`, body),
  sfx: (pid: number, body: Json) => post(`/api/projects/${pid}/sfx`, body),
  sfxPlan: (pid: number, body: Json = {}) => post(`/api/projects/${pid}/sfx-plan`, body),
  mix: (pid: number, body: Json) => post(`/api/projects/${pid}/mix`, body),
  verify: (pid: number, params: Json = {}) => {
    const qs = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
      .join('&');
    return post(`/api/projects/${pid}/verify${qs ? `?${qs}` : ''}`);
  },
  runFinal: (pid: number, body: Json) => post(`/api/projects/${pid}/final`, body),

  // ---------------- 一键出片（R-9）：脚本 → 落镜 → 逐镜出片 → 15s 成片
  /** **只规划，不花钱不落库**：会出几镜、每镜几秒、预估多少钱、缺什么、念白几行。
   *  传 `script` 时用脚本预估（还没落库），不传则用项目里已有的分镜。 */
  producePlan: (pid: number, body: Json = {}) =>
    post(`/api/projects/${pid}/produce/plan`, body),
  /** **正式出片**（会花钱）：立即返回 `{id, status:'queued'}`，用 getProduceRun 轮询。
   *  传 `dry_run: true` 只返回规划、不建运行记录。同一项目同时只允许一条在跑（否则 409）。 */
  startProduce: (pid: number, body: Json = {}) =>
    post(`/api/projects/${pid}/produce`, body),
  /** 该项目的出片运行（最新在前）。 */
  listProduceRuns: (pid: number) => get(`/api/projects/${pid}/produce`),
  /** 轮询一条出片运行：状态 + `stage` + 逐步骤 `steps` + 报告 + `final_path`。 */
  getProduceRun: (pid: number, runId: number) =>
    get(`/api/projects/${pid}/produce/${runId}`),
  /** 重试：复用同一条 run，**只补没出片的镜头**（已有的复用，不重复计费）。 */
  retryProduce: (pid: number, runId: number, body: Json = {}) =>
    post(`/api/projects/${pid}/produce/${runId}/retry`, body),

  /** **一键成片（15s）**：只要项目号，其余从库里取 ——
   *  镜头片段（已有产物的 shot，按 target_frames 配平）、念白行（最新脚本素材的念白稿）、
   *  原始语音（整条一次 TTS）、音效落点（脚本的「音效落位」表）、垫乐（本地合成）。
   *  传 `dry_run: true` 只看规划（齐备性/帧数/念白/音效落点），不出东西、不联网。 */
  compose: (pid: number, body: Json = {}) => post(`/api/projects/${pid}/compose`, body),

  // ---------------- 资产：商品库 ----------------
  listProducts: (category?: string) =>
    get(`/api/products${category ? `?category=${encodeURIComponent(category)}` : ''}`),
  productCategories: () => get('/api/products/categories'),
  createProduct: (body: Json) => post('/api/products', body),
  updateProduct: (id: number, body: Json) => patch(`/api/products/${id}`, body),
  deleteProduct: (id: number) => del(`/api/products/${id}`),

  // ---------------- 资产：主播库 ----------------
  listTalents: () => get('/api/talents'),
  createTalent: (body: Json) => post('/api/talents', body),
  updateTalent: (id: number, body: Json) => patch(`/api/talents/${id}`, body),
  deleteTalent: (id: number) => del(`/api/talents/${id}`),
  addTalentImage: (tid: number, body: Json) => post(`/api/talents/${tid}/images`, body),
  deleteTalentImage: (iid: number) => del(`/api/talent-images/${iid}`),
  /** 性别 / 国籍 / 图类型的下拉候选（后端是唯一事实来源，前端不抄枚举）。 */
  talentOptions: () => get('/api/talents/options'),
  /** 用**文本大模型**把基本信息扩写成形象提示词。不入库、不花钱生图。
   *  传 `appearance`（上一版）即"再生成一版"。 */
  previewTalentAppearance: (body: Json) => post('/api/talents/appearance-preview', body),
  /** 生成主播参考图（**会花钱**）：kind = front 正面图 / threeview 三视图 / sheet 四视图。
   *  返回 job_id，用 getJob 轮询。 */
  renderTalentImage: (tid: number, body: Json) => post(`/api/talents/${tid}/render`, body),
  /** 生成 2x2 四视图定妆照（**会花钱**）。等价于 render {kind:'sheet'}。 */
  talentSheet: (tid: number, body: Json = {}) => post(`/api/talents/${tid}/sheet`, body),

  // ---------------- 素材上传 / 示例 / 贴链接导入 ----------------
  /** 上传一组图片，落 data/uploads/{scope}/{owner}/。`owner` 用前端生成的 draftId。 */
  uploadImages: (scope: string, owner: string, files: File[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append('files', f));
    return request<{ urls: string[]; paths: string[]; count: number }>(
      `/api/uploads/${scope}/${owner}`,
      { method: 'POST', body: fd, headers: {} as Record<string, string> },
    );
  },
  /** 贴链接导入：抓商品页 → 解析 → 图片落盘。**不入库**，结果回填表单核对。 */
  importProductLink: (url: string, draftId: string) =>
    post('/api/ingest/product', { url, draftId }),
  exampleProducts: () => get('/api/examples/products'),

  envStatus: () => get('/api/env'),

  // ---------------- 供应商
  providers: () => get('/api/providers'),
  providersYaml: () => get('/api/providers/yaml'),
  providersSave: (text: string) => post('/api/providers/yaml', { text }),
  providersSwitch: (slot: string, id: string) => post('/api/providers/switch', { slot, id }),
  providersTest: (slot: string, id: string) => post('/api/providers/test', { slot, id }),
  providersCreate: (slot: string, body: Json) => post(`/api/providers/${slot}`, { body }),
  providersUpdate: (slot: string, id: string, body: Json) => put(`/api/providers/${slot}/${id}`, { body }),
  providersDelete: (slot: string, id: string) => del(`/api/providers/${slot}/${id}`),
  providersReload: () => post('/api/providers/reload'),

  // ---------------- 反推
  reverse: (body: Json) => post('/api/reverse', body),
  reverseUpload: (file: File, extra: Json = {}) => {
    const fd = new FormData();
    fd.append('file', file);
    Object.entries(extra).forEach(([k, v]) => fd.append(k, String(v)));
    return request('/api/reverse/upload', { method: 'POST', body: fd,
      headers: {} as Record<string, string> });
  },
  reverseReports: () => get('/api/reverse/reports'),
  reverseReport: (id: number) => get(`/api/reverse/${id}`),

  // ---------------- 批量变体
  createBatch: (body: Json) => post('/api/batch', body),
  listBatch: () => get('/api/batch'),
  getBatch: (id: number) => get(`/api/batch/${id}`),
  setBatchManifest: (id: number, manifest: Json) =>
    patch(`/api/batch/${id}/manifest`, { manifest }),
  replanBatch: (id: number) => post(`/api/batch/${id}/plan`),
  runBatch: (id: number, body: Json = {}) => post(`/api/batch/${id}/run`, body),
  deleteBatch: (id: number) => del(`/api/batch/${id}`),
  batchTemplate: (pid: number) => get(`/api/batch/projects/${pid}/template`),

  // ---------------- 广告法合规自检（P-4）
  complianceRules: () => get('/api/compliance/rules'),
  complianceReload: () => post('/api/compliance/reload'),
  complianceScan: (body: Json) => post('/api/compliance/scan', body),
  compliance: (pid: number, body: Json = {}) =>
    post(`/api/projects/${pid}/compliance`, body),

  // ---------------- 花费上限熔断（P-5）
  budget: (project_id?: number) =>
    get(`/api/budget${project_id != null ? `?project_id=${project_id}` : ''}`),
  budgetSave: (patch: Json, project_id?: number) =>
    put(`/api/budget${project_id != null ? `?project_id=${project_id}` : ''}`, patch),
  budgetCheck: (body: Json) => post('/api/budget/check', body),

  // ---------------- 全局设置（KV：如反推看图模型覆盖）
  settings: () => get('/api/settings'),
  saveSettings: (body: Json) => put('/api/settings', body),

  // ---------------- 出片质量门（P-6）
  projectQc: (pid: number, body: Json = {}) => post(`/api/projects/${pid}/qc`, body),
  shotQc: (sid: number, body: Json = {}) => post(`/api/shots/${sid}/qc`, body),
  contactSheet: (body: Json) => post('/api/qc/sheet', body),

  // ---------------- AI Agent 接入（P-7）
  mcpInfo: () => get('/api/mcp/info'),
};
