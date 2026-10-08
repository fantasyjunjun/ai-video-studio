/**
 * 供应商配置字段定义（单一事实来源）。
 *
 * 设计取向照 ClipForge：**必填项平铺、常用项少量、进阶项折叠**，
 * 而不是把 pydantic 模型的每个字段都摊在表单上。
 *
 * 为什么把字段定义从页面里抽出来：
 *  - 页面只负责渲染，字段增删改只动这一个文件；
 *  - `SLOT_KNOWN_KEYS` 是"哪些键属于这个插槽"的唯一判据，
 *    编辑时落在它之外的键才当作"用户自定义字段"回填 JSON ——
 *    否则 image 的 `image_path` 会被回填进 video 的 JSON 并写回配置（跨插槽污染）。
 *
 * 三类分组：
 *  - `basic`    不填就跑不起来，平铺显示
 *  - `common`   有合理默认值，但常需要按实际情况调，平铺显示
 *  - `advanced` 默认值够用，只在对接非标准服务端时改，折叠显示
 *  - `hidden`   后端有默认值且几乎不该改，不渲染（但要登记进 SLOT_KNOWN_KEYS，
 *               免得它被当成"自定义字段"塞回 JSON）
 */

export type FieldType = 'text' | 'number' | 'password' | 'json' | 'type';
export type FieldGroup = 'basic' | 'common' | 'advanced' | 'hidden';

export interface FieldDef {
  key: string;
  /** 中文短名，直接作为表单 label */
  label: string;
  type: FieldType;
  group: FieldGroup;
  /** 用途：这个字段控制什么行为 */
  purpose: string;
  /** 配置方法：怎么填、去哪拿、填错的后果 */
  howto?: string;
  placeholder?: string;
  /** type='type' 时的候选适配器（可自由输入，兜底任意服务端） */
  options?: string[];
  /** 只在适配器类型落在这组里时显示（留空 = 所有类型都显示） */
  onlyTypes?: string[];
  /** 留空时的生效值，渲染在说明里 */
  fallback?: string;
}

// ---------------------------------------------------------------- 适配器类型

/** 自建 ComfyUI（走工作流注入） */
export const COMFY_IMAGE_TYPES = ['comfyui', 'comfyui_api'];
/** AutoDL 托管工作流 */
export const AUTODL_VIDEO_TYPES = ['comfyui_autodl', 'autodl'];
/** 任意 OpenAI 兼容中转站 —— 与后端 openai_media.py 的常量保持一致 */
export const OPENAI_IMAGE_TYPES = ['openai_image', 'openai_compatible', 'openai', 'images_generations'];
export const OPENAI_VIDEO_TYPES = ['openai_video', 'openai_compatible', 'openai', 'videos_generations'];

const norm = (t?: string) => (t || '').trim().toLowerCase();
const oneOf = (t: string | undefined, set: string[]) => set.includes(norm(t));

export const isOpenAIImage = (t?: string) => oneOf(t, OPENAI_IMAGE_TYPES);
export const isOpenAIVideo = (t?: string) => oneOf(t, OPENAI_VIDEO_TYPES);
export const isComfyImage = (t?: string) => oneOf(t, COMFY_IMAGE_TYPES);
export const isAutodlVideo = (t?: string) => oneOf(t, AUTODL_VIDEO_TYPES);

/** 字段是否该在当前适配器类型下显示：未声明 onlyTypes 的恒显示；
 *  声明了但类型填得不在已知集合里（自定义适配器）→ 也显示，宁可多给也别让用户配不了。 */
export function fieldVisible(f: FieldDef, adapterType: string | undefined): boolean {
  if (!f.onlyTypes) return true;
  const t = norm(adapterType);
  if (!t) return true;
  if (oneOf(t, f.onlyTypes)) return true;
  const known =
    oneOf(t, COMFY_IMAGE_TYPES) || oneOf(t, OPENAI_IMAGE_TYPES) ||
    oneOf(t, AUTODL_VIDEO_TYPES) || oneOf(t, OPENAI_VIDEO_TYPES);
  return !known;
}

// ---------------------------------------------------------------- 文本大模型

const LLM_FIELDS: FieldDef[] = [
  {
    key: 'id', label: '标识 id', type: 'text', group: 'basic',
    purpose: '这个供应商的唯一名字。启用（active）、降级（fallback_provider）都靠它引用。',
    howto: '只能字母、数字、短横线，例如 relay-a、local-ollama。建好后不可修改，改名请新建一条再删旧的。',
    placeholder: 'relay-a',
  },
  {
    key: 'base_url', label: 'API 地址 base_url', type: 'text', group: 'basic',
    purpose: '请求发往哪里。同一个适配器靠改这个地址就能接官方 / 中转站 / 本地模型，不需要改代码。',
    howto: '官方填 https://api.openai.com/v1；中转站填对方给的地址（多数也要带 /v1）；本地 Ollama 填 http://localhost:11434/v1。地址结尾不要多写 /chat/completions。',
    placeholder: 'https://api.openai.com/v1',
  },
  {
    key: 'api_key', label: 'API Key', type: 'password', group: 'basic',
    purpose: '调用鉴权凭证。',
    howto: '直接粘贴。不会明文写进 providers.yaml，而是存进 OS 凭据库（Windows 凭据管理器 / macOS Keychain），前端只显示"密钥已存"。编辑时留空表示不修改，清空表示删除。',
    placeholder: 'sk-...',
  },
  {
    key: 'model', label: '模型名 model', type: 'text', group: 'basic',
    purpose: '用哪个模型生成提示词 / 反推 / 文案。',
    howto: '按服务商的命名填，例如 gpt-4o、deepseek-chat。名字写错会直接 404；本地 Ollama 要带标签（qwen2.5:14b）。',
    placeholder: 'gpt-4o',
  },
  {
    key: 'timeout', label: '超时（秒）', type: 'number', group: 'common',
    purpose: '单次请求的最长等待。超时会触发重试。',
    howto: '默认 120。生成整篇文案较慢时可调到 180～300；中转站经常排队的话别设太小。',
    fallback: '120',
  },
  {
    key: 'knowledge_base', label: '知识库目录', type: 'text', group: 'common',
    purpose: '提示词引擎从这里读铁律、模板、评分卡、文案规范。改这个目录 = 换一套写作规范。',
    howto: '相对 backend 目录的路径，默认 kb/。多套规范可复制一个目录再在这里切换。',
    placeholder: 'kb/',
    fallback: 'kb/',
  },
  {
    key: 'api_key_env', label: '密钥环境变量名', type: 'text', group: 'advanced',
    purpose: '不把 Key 交给本机凭据库，改为从环境变量读取（服务器 / CI 部署用）。',
    howto: '只填变量名不填值，例如 OPENAI_API_KEY。与上面的 API Key 可以同时存在，运行时按 内联 > 凭据库 > 环境变量 的顺序取。',
  },
  {
    key: 'key_rotation', label: '多 Key 轮换（JSON 数组）', type: 'json', group: 'advanced',
    purpose: '一个 Key 的额度用完或被限流时，自动换下一个重试。',
    howto: '填**环境变量名**的数组，不是 Key 本身：["RELAY_KEY_1","RELAY_KEY_2"]。变量要真的在系统环境里设好。',
    placeholder: '["RELAY_A_KEY_1","RELAY_A_KEY_2"]',
  },
  {
    key: 'extra_headers', label: '自定义请求头（JSON）', type: 'json', group: 'advanced',
    purpose: '中转站常要求带上渠道标识或专属鉴权头，否则报 401 / 无权限。',
    howto: '按中转站文档填键值对。值不会回显（只回键名）。',
    placeholder: '{"X-Channel":"brand-video"}',
  },
  {
    key: 'temperature', label: '温度 temperature', type: 'number', group: 'advanced',
    purpose: '输出的随机性。这里的值只是缺省，提示词引擎/反推自己会按任务传参。',
    howto: '0～2，默认 0.7。要稳定可复现就调低。',
    fallback: '0.7',
  },
  {
    key: 'max_tokens', label: '单次最大 token', type: 'number', group: 'advanced',
    purpose: '单次回复的长度上限，防止超长输出被截断或烧钱。',
    howto: '默认 4096。分镜脚本较长时可提到 8192。',
    fallback: '4096',
  },
  {
    key: 'retries', label: '重试次数', type: 'number', group: 'advanced',
    purpose: '失败后自动重试几次（含换 Key 轮换）。',
    howto: '默认 3。中转站不稳定可加到 5。',
    fallback: '3',
  },
  {
    key: 'fallback_provider', label: '降级到哪个供应商 id', type: 'text', group: 'advanced',
    purpose: '全部重试都失败后，自动改用另一个供应商继续，避免整条流水线中断。',
    howto: '填本页另一个条目的 id，例如 local-ollama。不要填自己，也不要不存在的 id。',
    placeholder: 'local-ollama',
  },
  {
    key: 'spend_cap_cny', label: '该供应商累计花费上限（¥）', type: 'number', group: 'advanced',
    purpose: '这一家烧到这个金额就拒绝再提交，用于"烧穿了就换别家"。',
    howto: '0 = 不限。全局上限在页面下方的「花费上限」面板里设，这里只管单个供应商。',
    fallback: '0（不限）',
  },
  // llm 目前只有一种适配器，不暴露给用户改，但登记为已知字段以免被当自定义字段回填
  { key: 'type', label: '适配器类型', type: 'text', group: 'hidden', purpose: '固定 openai_compatible' },
];

// ---------------------------------------------------------------- 文生图

const IMAGE_FIELDS: FieldDef[] = [
  {
    key: 'id', label: '标识 id', type: 'text', group: 'basic',
    purpose: '这个图像供应商的唯一名字，切换启用时用它引用。',
    howto: '字母数字短横线，例如 comfyui-sdxl、relay-image。建好后不可改。',
    placeholder: 'comfyui-sdxl',
  },
  {
    key: 'type', label: '接入方式 type', type: 'type', group: 'basic',
    purpose: '决定用哪个适配器。改这个值会切换下面显示的字段。',
    howto: '自建 ComfyUI 填 comfyui；任一中转站填 openai_image（按 OpenAI 的 /images/generations 协议）。',
    options: ['comfyui', 'openai_image'],
  },
  {
    key: 'base_url', label: '服务地址 base_url', type: 'text', group: 'basic',
    purpose: '出图请求发往哪里。',
    howto: 'ComfyUI 填自己的实例地址（含端口），例如 http://127.0.0.1:8188；中转站填对方给的地址，多数要带 /v1。',
    placeholder: 'http://127.0.0.1:8188',
  },
  {
    key: 'token', label: '令牌 Token', type: 'password', group: 'basic',
    purpose: '中转站鉴权用。',
    howto: '直接粘贴，存进 OS 凭据库，不明文落盘。**本地 ComfyUI 不需要填，留空即可。** 编辑时留空 = 不修改，清空 = 删除。不想把令牌交给本机凭据库时，改用「进阶设置」里的环境变量名方式。',
  },
  {
    key: 'workflow', label: '工作流文件 workflow', type: 'text', group: 'basic',
    purpose: 'ComfyUI 按这个工作流文件出图，是"出什么图"的真正定义。',
    howto: '填相对 backend 的路径，例如 workflows/t2i_sdxl.json。必须是 ComfyUI 导出的 **API format**（不是 UI 界面导出的那份），否则节点编号对不上。',
    placeholder: 'workflows/t2i_sdxl.json',
    onlyTypes: COMFY_IMAGE_TYPES,
  },
  {
    key: 'inject', label: '提示词注入点（JSON）', type: 'json', group: 'basic',
    purpose: '告诉系统"提示词要写进工作流的哪个节点"，这是接 ComfyUI 最关键的一处配置。',
    howto: '打开工作流 JSON 找到吃正向提示词的节点，按 "节点号.inputs.字段名" 填，例如 {"positive":"6.inputs.text","seed":"3.inputs.seed"}。换工作流必须同步改这里。',
    placeholder: '{"positive":"6.inputs.text","negative":"7.inputs.text"}',
    onlyTypes: COMFY_IMAGE_TYPES,
  },
  {
    key: 'model', label: '模型名 model', type: 'text', group: 'basic',
    purpose: '中转站用哪个模型出图。',
    howto: '按中转站命名填，例如 dall-e-3、flux、seedream。ComfyUI 场景留空（模型由工作流决定）。',
    placeholder: 'dall-e-3',
    onlyTypes: OPENAI_IMAGE_TYPES,
  },
  {
    key: 'default_resolution', label: '默认分辨率', type: 'text', group: 'common',
    purpose: 'ComfyUI 出图的宽高。成片页没单独指定时用这个。',
    howto: '写成 宽x高，例如 768x1344（竖图）、1344x768（横图）。必须与工作流支持的档位对得上。',
    placeholder: '768x1344',
    onlyTypes: COMFY_IMAGE_TYPES,
  },
  {
    key: 'default_size', label: '默认尺寸 size', type: 'text', group: 'common',
    purpose: '中转站出图的尺寸参数。',
    howto: '按对方文档填，例如 1024x1024、1024x1536。留空则不下发该参数，听服务端默认。',
    placeholder: '1024x1024',
    onlyTypes: OPENAI_IMAGE_TYPES,
  },
  {
    key: 'timeout', label: '超时（秒）', type: 'number', group: 'common',
    purpose: '单张图生成的最长等待。',
    howto: '默认 300。高清图或排队严重时调大到 600。',
    fallback: '300',
  },
  {
    key: 'ref_image_field', label: '参考图字段名', type: 'text', group: 'advanced',
    purpose: '图生图 / 换装变体时，参考图放在请求体的哪个字段。',
    howto: '默认 image。对方文档写的是 img / reference_image 之类时改成实际字段名。',
    placeholder: 'image',
    fallback: 'image',
    onlyTypes: OPENAI_IMAGE_TYPES,
  },
  {
    key: 'ref_image_mode', label: '参考图传输方式', type: 'text', group: 'advanced',
    purpose: '参考图是给一个 URL，还是把图片字节转成 base64 内联发送。',
    howto: '默认 url（最快、省带宽）。对方不接受外链、或图片在内网拉不到时改 base64。',
    placeholder: 'url',
    fallback: 'url',
    onlyTypes: OPENAI_IMAGE_TYPES,
  },
  {
    key: 'response_format', label: '响应格式', type: 'text', group: 'advanced',
    purpose: '要服务端直接返回图片 URL 还是内联 base64。',
    howto: '填 url 或 b64_json。**留空最省事**（不下发该参数，听服务端默认）；只有当对方默认返回的格式我们解析不了时才填。',
    placeholder: '留空 = 服务端默认',
    onlyTypes: OPENAI_IMAGE_TYPES,
  },
  {
    key: 'image_path', label: '出图路径', type: 'text', group: 'advanced',
    purpose: '文生图接口路径。中转站路径与 OpenAI 不一致时覆盖。',
    howto: '默认 /images/generations，会拼在 base_url 后面。',
    placeholder: '/images/generations',
    fallback: '/images/generations',
    onlyTypes: OPENAI_IMAGE_TYPES,
  },
  {
    key: 'edit_path', label: '图生图路径', type: 'text', group: 'advanced',
    purpose: '图生图 / 换装接口路径。',
    howto: '默认 /images/edits。只有用到换装变体功能时才需要与对方对齐。',
    fallback: '/images/edits',
    onlyTypes: OPENAI_IMAGE_TYPES,
  },
  {
    key: 'poll_path', label: '轮询路径', type: 'text', group: 'advanced',
    purpose: '少数中转站不是同步返回，而是先给 task id 再轮询取结果。',
    howto: '默认 /images/generations/{id}，{id} 会替换成提交返回的任务号。同步返回的服务端可忽略此项。',
    fallback: '/images/generations/{id}',
    onlyTypes: OPENAI_IMAGE_TYPES,
  },
  {
    key: 'extract', label: '响应提取路径（JSON）', type: 'json', group: 'advanced',
    purpose: '中转站返回的 JSON 结构与 OpenAI 不同时，告诉系统去哪取任务号和图片地址。',
    howto: '写"内部语义 → 响应里的点号路径"，支持数组下标：{"task_id":"data.task_id","result_url":"data.0.url"}。留空则按常见键名自动探测。',
    placeholder: '{"result_url":"data.0.url"}',
    onlyTypes: OPENAI_IMAGE_TYPES,
  },
  {
    key: 'body_extra', label: '自定义请求体（JSON）', type: 'json', group: 'advanced',
    purpose: '给请求体加上服务商特有的参数，例如画风、水印开关、加速档。',
    howto: '写键值对，会被合并进请求体：{"style":"vivid"}。与内置字段冲突时以这里为准。',
    placeholder: '{"style":"vivid"}',
    onlyTypes: OPENAI_IMAGE_TYPES,
  },
  {
    key: 'token_env', label: '令牌环境变量名', type: 'text', group: 'advanced',
    purpose: '不把令牌交给本机凭据库，改为从系统环境变量读取（服务器 / CI 部署，或想用脚本统一注入密钥）。',
    howto: '只填变量名不填值，例如 AUTODL_TOKEN、RELAY_IMAGE_KEY。与上面的「令牌」可并存，运行时按 内联 > 凭据库 > 环境变量 的顺序取。',
    placeholder: 'AUTODL_TOKEN',
  },
  {
    key: 'extra_headers', label: '自定义请求头（JSON）', type: 'json', group: 'advanced',
    purpose: '中转站要求渠道标识头时用。',
    howto: '填键值对，值不回显。',
    placeholder: '{"X-Channel":"brand-video"}',
  },
  {
    key: 'key_rotation', label: '多 Key 轮换（JSON 数组）', type: 'json', group: 'advanced',
    purpose: '多把令牌轮换用，规避单 Key 限流。',
    howto: '填**环境变量名**数组：["RELAY_IMG_KEY_1","RELAY_IMG_KEY_2"]。',
    placeholder: '["RELAY_IMG_KEY_1","RELAY_IMG_KEY_2"]',
  },
  {
    key: 'retries', label: '重试次数', type: 'number', group: 'advanced',
    purpose: '失败后自动重试次数。',
    howto: '默认 3。出图贵，别设太大。',
    fallback: '3',
  },
  {
    key: 'spend_cap_cny', label: '该供应商累计花费上限（¥）', type: 'number', group: 'advanced',
    purpose: '这一家烧到限额就拒绝提交。',
    howto: '0 = 不限。',
    fallback: '0（不限）',
  },
  // ComfyUI 标准路径，后端有默认值且几乎不会改
  { key: 'view_path', label: 'view 路径', type: 'text', group: 'hidden', purpose: 'ComfyUI 固定 /view' },
  { key: 'history_path', label: 'history 路径', type: 'text', group: 'hidden', purpose: 'ComfyUI 固定 /history' },
  { key: 'n', label: '每次出图张数', type: 'number', group: 'hidden', purpose: '固定 1' },
];

// ---------------------------------------------------------------- 图生视频

const VIDEO_FIELDS: FieldDef[] = [
  {
    key: 'id', label: '标识 id', type: 'text', group: 'basic',
    purpose: '这个视频供应商的唯一名字，切换启用时用它引用。',
    howto: '字母数字短横线，例如 autodl-minimax、relay-video。建好后不可改。',
    placeholder: 'autodl-minimax',
  },
  {
    key: 'type', label: '接入方式 type', type: 'type', group: 'basic',
    purpose: '决定用哪个适配器。改这个值会切换下面显示的字段。',
    howto: 'AutoDL 托管填 comfyui_autodl；任一中转站填 openai_video。',
    options: ['comfyui_autodl', 'openai_video'],
  },
  {
    key: 'base_url', label: '服务地址 base_url', type: 'text', group: 'basic',
    purpose: '出片请求发往哪里。留空则用平台官方地址，要接私有实例/自建转发就填这里。',
    howto: 'AutoDL 留空即可（填 your-xxx 这类占位值会被自动识别并回落官方地址）；中转站填对方给的地址，多数要带 /v1。不要写成开关按钮以外的路径。',
    placeholder: '留空 = AutoDL 官方地址',
  },
  {
    key: 'token', label: '令牌 Token', type: 'password', group: 'basic',
    purpose: '平台鉴权用。',
    howto: '直接粘贴，存进 OS 凭据库，不明文落盘。编辑时留空 = 不修改，清空 = 删除。**出片是提交即计费的操作，令牌没配好会直接失败。** 不想把令牌交给本机凭据库时，改用「进阶设置」里的环境变量名方式。',
  },
  {
    key: 'workflow', label: '工作流 workflow', type: 'text', group: 'basic',
    purpose: '用平台的哪个工作流出片。不同工作流的可用时长、是否支持反向提示词都不同。',
    howto: '填平台上的工作流 id，例如 minimax_h3_lightx2v_v5_15s。不确定就用当前默认值。',
    placeholder: 'minimax_h3_lightx2v_v5_15s',
    onlyTypes: AUTODL_VIDEO_TYPES,
  },
  {
    key: 'model', label: '模型名 model', type: 'text', group: 'basic',
    purpose: '中转站用哪个模型出片。',
    howto: '按中转站命名填，例如 kling-v1、sora-2、veo-3。',
    placeholder: 'kling-v1',
    onlyTypes: OPENAI_VIDEO_TYPES,
  },
  {
    key: 'default_duration', label: '默认时长（秒）', type: 'number', group: 'common',
    purpose: '每个镜头默认出多少秒。成片页没单独指定时用这个。',
    howto: '按工作流支持的档位填。AutoDL 这条工作流上限 15 秒；填超了会被服务端拒绝。',
    fallback: '5',
  },
  {
    key: 'default_resolution', label: '默认分辨率', type: 'text', group: 'common',
    purpose: '默认出片档位，直接影响单价。',
    howto: 'AutoDL 用 480p_vertical / 480p_horizontal / 768p_vertical / 768p_horizontal（系统自动映射成平台的"480p竖"等档位）；中转站按对方文档填。',
    placeholder: '480p_vertical',
    fallback: '480p_vertical',
  },
  {
    key: 'cost_per_sec', label: '单价（元/秒）', type: 'number', group: 'common',
    purpose: '用于成本预估与花费熔断。算错会导致"以为没超预算其实超了"。',
    howto: '填平台实际报价。AutoDL 默认 0.03。更精细的分档报价走下面的价目表。',
    fallback: '0.03',
  },
  {
    key: 'max_concurrency', label: '并发上限', type: 'number', group: 'common',
    purpose: '同时提交几个出片任务。',
    howto: '默认 3。别把六个镜头一次性砸给 GPU 平台 —— 排队反而更慢，也更容易整体失败。',
    fallback: '3',
  },
  {
    key: 'poll_interval', label: '轮询间隔（秒）', type: 'number', group: 'common',
    purpose: '多久问一次任务进度。',
    howto: '默认 5。平台限流严格时调大。',
    fallback: '5',
  },
  {
    key: 'timeout', label: '超时（秒）', type: 'number', group: 'advanced',
    purpose: '单个任务的最长等待时间。',
    howto: '默认 900（15 分钟）。出长镜头可调大。',
    fallback: '900',
  },
  {
    key: 'submit_path', label: '提交路径', type: 'text', group: 'advanced',
    purpose: '提交出片任务的接口路径。',
    howto: '默认 /videos/generations，拼在 base_url 后面。与对方文档不一致时覆盖。',
    fallback: '/videos/generations',
    onlyTypes: OPENAI_VIDEO_TYPES,
  },
  {
    key: 'poll_path', label: '轮询路径', type: 'text', group: 'advanced',
    purpose: '查询任务状态的路径。',
    howto: '默认 /videos/generations/{id}，{id} 替换成提交返回的任务号。',
    fallback: '/videos/generations/{id}',
    onlyTypes: OPENAI_VIDEO_TYPES,
  },
  {
    key: 'ref_image_field', label: '参考图字段名', type: 'text', group: 'advanced',
    purpose: '图生视频时，首帧/参考图放在请求体的哪个字段。',
    howto: '默认 image。对方写 first_frame / image_url 时改成实际字段名。',
    fallback: 'image',
    onlyTypes: OPENAI_VIDEO_TYPES,
  },
  {
    key: 'ref_image_mode', label: '参考图传输方式', type: 'text', group: 'advanced',
    purpose: '参考图给 URL 还是转 base64 内联。',
    howto: '默认 url。对方不接受外链时改 base64。',
    fallback: 'url',
    onlyTypes: OPENAI_VIDEO_TYPES,
  },
  {
    key: 'field_map', label: '请求字段名映射（JSON）', type: 'json', group: 'advanced',
    purpose: '对方用的字段名与我们不同时，做一次重命名，代码无需改动。',
    howto: '写"我们的语义 → 对方的字段名"：{"ref_image":"image","resolution":"size"}。没映射的字段用我们的原名。',
    placeholder: '{"ref_image":"image","resolution":"size"}',
    onlyTypes: OPENAI_VIDEO_TYPES,
  },
  {
    key: 'extract', label: '响应提取路径（JSON）', type: 'json', group: 'advanced',
    purpose: '视频领域没有统一标准，用这个把对方的返回结构对到我们的字段上。',
    howto: '{"task_id":"data.task_id","status":"data.status","result_url":"data.video_url"}，支持数组下标如 data.0.url。留空则自动探测常见键名。',
    placeholder: '{"task_id":"data.task_id","result_url":"data.video_url"}',
    onlyTypes: OPENAI_VIDEO_TYPES,
  },
  {
    key: 'status_map', label: '状态词表（JSON）', type: 'json', group: 'advanced',
    purpose: '把对方的状态字符串翻成我们的内部状态（成功/失败/进行中）。',
    howto: '{"succeeded":["SUCCESS"],"failed":["FAILED"],"running":["PROCESSING"]}。只覆盖对不上的那几个即可。',
    placeholder: '{"succeeded":["SUCCESS"],"failed":["FAILED"]}',
    onlyTypes: OPENAI_VIDEO_TYPES,
  },
  {
    key: 'body_extra', label: '自定义请求体（JSON）', type: 'json', group: 'advanced',
    purpose: '加对方特有的参数，例如画质档、运动强度。',
    howto: '{"mode":"pro"}。与内置字段冲突时以这里为准。',
    onlyTypes: OPENAI_VIDEO_TYPES,
  },
  {
    key: 'price_map', label: '价目表（JSON）', type: 'json', group: 'advanced',
    purpose: '分档报价，用于按档位精确预估成本（高峰/空闲价格不同）。',
    howto: '{"480p竖":[0.030,0.020],"768p竖":[0.040,0.030]}，数组是 [高峰单价, 空闲单价]。只想给个统一价就用上面的 cost_per_sec。',
    onlyTypes: AUTODL_VIDEO_TYPES,
  },
  {
    key: 'token_env', label: '令牌环境变量名', type: 'text', group: 'advanced',
    purpose: '不把令牌交给本机凭据库，改为从系统环境变量读取（服务器 / CI 部署，或想用脚本统一注入密钥）。',
    howto: '只填变量名不填值，例如 AUTODL_TOKEN。与上面的「令牌」可并存，运行时按 内联 > 凭据库 > 环境变量 的顺序取。',
    placeholder: 'AUTODL_TOKEN',
  },
  {
    key: 'extra_headers', label: '自定义请求头（JSON）', type: 'json', group: 'advanced',
    purpose: '中转站要求渠道标识头时用。',
    howto: '填键值对，值不回显。',
    placeholder: '{"X-Channel":"brand-video"}',
  },
  {
    key: 'key_rotation', label: '多 Key 轮换（JSON 数组）', type: 'json', group: 'advanced',
    purpose: '多把令牌轮换，规避单 Key 限流。',
    howto: '填**环境变量名**数组：["RELAY_VID_KEY_1","RELAY_VID_KEY_2"]。',
  },
  {
    key: 'spend_cap_cny', label: '该供应商累计花费上限（¥）', type: 'number', group: 'advanced',
    purpose: '这一家烧到限额就拒绝提交。',
    howto: '0 = 不限。',
    fallback: '0（不限）',
  },
  // 后端已给默认值、几乎不会改的字段
  { key: 'min_duration', label: '最短时长', type: 'number', group: 'hidden', purpose: '固定 1' },
  { key: 'max_duration', label: '最长时长', type: 'number', group: 'hidden', purpose: '固定 15' },
  { key: 'supports_negative_prompt', label: '支持反向提示词', type: 'text', group: 'hidden', purpose: '按工作流判定' },
  { key: 'no_negative_prompt_workflows', label: '不支持反向提示词的工作流', type: 'json', group: 'hidden', purpose: '后端内置清单' },
  // 与 base_url 语义重复（适配器按 `api_base or base_url` 取用），已由上面的 base_url 承接；
  // 保留为已知字段，旧配置里若填了它不会被当成"自定义字段"塞进 JSON，也不会被误删。
  { key: 'api_base', label: '私有实例 API 前缀', type: 'text', group: 'hidden', purpose: '已被 base_url 取代' },
];

// ---------------------------------------------------------------- 导出

export const FIELDS: Record<string, FieldDef[]> = {
  llm: LLM_FIELDS,
  image: IMAGE_FIELDS,
  video: VIDEO_FIELDS,
};

export const GROUP_META: Record<string, { title: string; hint: string }> = {
  basic: { title: '必填', hint: '这几项不填就跑不起来' },
  common: { title: '常用设置', hint: '有默认值，但常需按实际情况调整' },
  advanced: { title: '进阶设置', hint: '默认值够用；只在对接非标准服务端时才改' },
};

/** 该插槽认识的全部键（含 hidden）——落在它之外的才算"用户自定义字段"。
 *  这是防跨插槽污染的唯一判据：image 的 image_path 不该被回填进 video 的 JSON。 */
export const SLOT_KNOWN_KEYS: Record<string, Set<string>> = Object.fromEntries(
  Object.entries(FIELDS).map(([slot, defs]) => [slot, new Set(defs.map((f) => f.key))]),
);

/** 新增时的默认适配器类型 */
export const DEFAULT_TYPE: Record<string, string> = {
  llm: 'openai_compatible',
  image: 'comfyui',
  video: 'comfyui_autodl',
};

/** 后端回传的运行时状态字段，不参与编辑、也不进"自定义字段" */
export const RUNTIME_ONLY = new Set([
  'api_key_set', 'token_set', 'base_url_is_placeholder', 'token_ready',
  'env_ready', 'keys_resolved',
]);
