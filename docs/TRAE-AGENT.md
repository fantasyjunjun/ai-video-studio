# Trae Agent 集成（ai-video-studio）

把字节开源的 [Trae Agent](https://github.com/bytedance/trae-agent) 接进本地视频工作室：
用自然语言指挥它调用本项目的 MCP 工具，串起"建项目 → 写分镜 → lint → 渲染 → QC"整条流水线。

```
┌──────────────┐   OpenAI 兼容    ┌─────────────────┐
│   你的一句话  │ ───────────────> │   Trae Agent    │
└──────────────┘                  │  (trae-cli)     │
                                  └────────┬────────┘
                                           │ stdio / JSON-RPC
                                           ▼
                                  ┌─────────────────┐
                                  │ backend/        │   http://127.0.0.1:8000
                                  │ mcp_server.py   │ ──────────────> FastAPI 后端
                                  │  (17 个工具)     │                 （唯一业务真相）
                                  └─────────────────┘
```

**MCP server 故意做成薄壳**：它不实现任何业务规则，只做参数整形 + HTTP 转发。
讽刺的是这正是它能放心给 agent 用的原因 —— 业务流程在后端只有一份实现，
网页手点和 agent 调用不会分叉。

---

## 1. 安装

### 1.1 必须用 Python 3.12（不是 3.13）

依赖里有 `tree-sitter-languages==1.10.2` 这种钉死版本的包，在 3.13 上常拿不到 wheel。
我们实测 3.12.10 一次装过。

```bash
C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe -m venv \
    C:/Users/Administrator/.workbuddy/binaries/python/envs/trae312
```

### 1.2 源码获取：**PyPI 上没有这个包**

README 里写了 "Simple pip-based installation"，但实测 `pip install trae-agent` 返回
`No matching distribution found` —— 它没发布到 PyPI，只能装源码。

```bash
pip install -i https://mirrors.aliyun.com/pypi/simple/ \
    -e C:/Users/Administrator/.workbuddy/third_party/trae-agent/trae-agent-main
```

> **装 pip 包时不要挂代理。** 本机环境变量里的 `http_proxy=127.0.0.1:xxxxx` 经常是失效的，
> pip 构建隔离子进程会连不上。实测**直连反而通**，而阿里云镜像走代理会失败。
> 遇到 `ProxyError / WinError 10061` 先 `env -u http_proxy -u https_proxy ...` 试一次。

### 1.3 Windows 必打的补丁

源码 `trae_agent/agent/docker_manager.py` 在**模块顶层**硬 import `docker` 与 `pexpect`，
而这两个在 pyproject 里属于 optional 依赖。后果是：即使你完全不用 Docker 功能，
`import trae_agent`（连带 `trae-cli`）也会直接 ImportError。

更麻烦的是 `pexpect` 在 Windows 上**装了也 import 不了**（依赖 Unix 的 `pty` 模块），
所以这不是"再装个包"能解决的。

已打的补丁存档在：

```
~/.workbuddy/third_party/trae-agent/patches/0001-windows-no-docker-import.patch
```

做法是把 import 软化（导不进来就置 None），不改 Docker 模式下的任何行为；
真用到 Docker 时在 `__init__` 里给出明确报错，而不是诡异的 `AttributeError`。
**重新拉取上游源码后要重打这个 patch。**

### 1.4 还需要 `docker` 包本身

软化之后仍建议 `pip install docker`：DockerManager 类定义依赖它存在与否的分支判断，
装上能让代码路径更接近上游。

---

## 2. 生成配置

```bash
cd ai-video-studio/backend
python scripts/gen_trae_config.py            # 用托管 venv 的 python
python scripts/gen_trae_config.py --dry-run  # 先看内容
```

脚本会从 `providers.yaml`（文本大模型槽位的 active 供应商）+ Secret Store 读出
endpoint / 模型 / 密钥，生成 `<trae home>/trae_config.yaml`。
该路径已被上游 `.gitignore` 忽略。

**密钥来自 Secret Store 的唯一好处**：换模型、换 key 都在软件的「设置 → 供应商」里做，
然后重跑脚本。不要手改 yaml —— 它是可以随手再生的产物。

### 2.1 为什么 `provider` 要写 `doubao`（不是 `openai`）

这是最容易浪费半天时间的坑：

| 配置 | 实际走的 Client | API 形态 | 中转站能用吗 |
|---|---|---|---|
| `provider: openai` | `OpenAIClient` | **Responses API** `/v1/responses` | 普遍不行，报 404/400 且信息含糊 |
| `provider: doubao` | `DoubaoClient` | chat/completions `/v1/chat/completions` | ✅ |

而且 `DoubaoProvider.create_client()` **直接使用配置里的 base_url，不会硬编码覆盖成火山方舟** ——
所以名字叫 doubao，实质是通用的 OpenAI 兼容客户端。想接任意中转站/ollama 都用它。

### 2.2 默认关闭的东西

- **`allow_mcp_servers` 白名单**：yaml 里必须显式列出要启用的 server，
  不在名单里的**不会生效且没有任何报错**。生成脚本已经写好了。
- **Lakeview**（步骤摘要）：会额外配一个模型付费做摘要，中转站未必支持它的调用形态，默认关。
- **花钱动作**：MCP 侧 `STUDIO_MCP_ALLOW_SPEND=0`，渲染 / 最终混流默认拒绝。
  要 agent 真出片就重跑脚本加 `--allow-spend`。
- **`max_steps: 40`**：防止失控跑钱，按需调。

---

## 3. 使用

```bash
# 先确保 ai-video-studio 后端在跑（MCP server 不会替你启动）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

trae-cli run "列出所有项目，并告诉我每个项目有几个分镜" \
    --config-file "C:/Users/Administrator/.workbuddy/third_party/trae-agent/trae-agent-main/trae_config.yaml" \
    --working-dir "C:/Users/Administrator/WorkBuddy/图生视频skill/ai-video-studio"
```

按你选的权限档，`--working-dir` **限定在 ai-video-studio 目录内**，agent 有 bash 和文件编辑能力。
给它一个工作区边界，它就不该到别的地方折腾。

---

## 4. 验证

不依赖任何 API key，只验证配置和管道：

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/trae312/Scripts/python.exe \
    scripts/smoke_trae_agent.py
```

覆盖三类问题：配置 schema 能否被 trae-agent 解析 / 白名单有没有漏 /
能不能真的拉起 `mcp_server.py` 并调用只读工具。当前实测 **12 项全绿、17 个工具**。

> 工具数以 smoke 实测为准，不要相信文档里的旧数字。

---

## 5. 已知限制

- **Windows 上不能用 Docker 模式**（`--docker-image` 等）：pexpect 依赖 Unix pty。
- **HTTP / WebSocket 传输未实现**：trae-agent 的 `MCPClient` 只支持 stdio，
  配 `http_url` 会直接 `NotImplementedError`。
- **完整流水线（MCP 通道）的真实 LLM 调用尚未端到端验证**：本机没配任何可用的 key
  （Secret Store 四个 llm 供应商都为空，也没跑 ollama）。补上 key 后按第 3 节跑一次真实任务即完成闭环。
  > 注：下面的「生文专用通道」（第 6 节）**已端到端验证**——185s 返回 200，产出 10989 字合规脚本。

---

## 6. 网页一键生成视频脚本（生文专用通道）

R5 新增：在「项目工作台」(`/`，即项目列表) **每行最左侧**放了「生成视频提示词」按钮（位于「分镜」按钮左侧）。
点击后弹窗可选 语言 / 镜头数 / 时长 / 附加要求，提交即调用：

```
POST /api/projects/{project_id}/generate-script
```

后端（`backend/app/api/trae.py`）流程：

1. 取项目 + 关联商品（**卖点描述作为事实源**，不得编造香调/功效/成分）+ 主播锚点（外观/规格）；
2. 把 `fragrance-ecom-video` 技能的 `SKILL.md` + `references/` + `assets/templates/` 拷进沙箱
   `backend/.trae_runs/<ts>-p<id>/`（**不拷 `scripts/`**，纯生文用不到，也避免 agent 误跑去出片）；
3. 写 `context.md`（任务 + 必读 5 文件清单 + 输出格式 + 工作纪律）；
4. 用**纯生文专用配置** `trae_config.scriptgen.yaml` 拉起 `trae-cli run --file context.md`
   （**不挂 MCP、不允许花钱**）；
5. 读回 `./output/script.md` 返回前端弹窗（可复制 / 下载 .md，**不入库**）。

前端入口：`frontend/src/pages/Dashboard.tsx`（按钮 + 配置/结果弹窗）+ `frontend/src/api.ts::generateScript`。

**从脚本到成片（可出片的桥）**：结果弹窗里点「导入为项目分镜」→ `POST /api/projects/{id}/import-script`
把逐镜小节的标题 + 提示词围栏块解析成 `Shot` 落库（按镜号幂等 upsert，见 `pipeline/script_import.py`）
→ 跳转分镜页逐镜试算 / 出片。出片时若未显式传 `ref_image_paths`，后端会**自动**按项目解析
**商品图 + 主播参考图**作为 i2v 参考图（`services/refs.py`；纯产品镜不带主播图，避免人物入空镜）
—— 否则"图生视频"会退化成纯文生视频。分镜页有「i2v 参考图」预览卡显示会喂哪些图。

同一个弹窗里还有**第二条入口**（不开 Agent、直接粘贴别人的脚本），见 §6.4。

### 6.1 生文任务的步数与超时预算

`trae.py` 顶部常量是**实测校准**的（不是拍脑袋），并被代码注释引用为本节。
R-13 把这一整块按实测数据重排过一次（用户反馈"15 分钟还没生成，在线大模型只要几秒"）：

| 常量 | 值 | 理由 |
|---|---|---|
| `TIMEOUT_SEC` | `1200`（20 分钟） | 最后一道保险。R-13 起单步超时由 `SCRIPTGEN_LLM_TIMEOUT` 兜住，正常不会再走到这里。且超时**不丢产出**（见下）。 |
| `MAX_STEPS` | `8` | R-13 前是 45（要留出"读 5 个文件 + 逐镜追加"的余量）。资料改为内联后，典型只用 2-4 步；留 8 既是充裕余量，也能在模型陷入反复重写时及时止损，而不是白烧几十轮请求。 |
| `SCRIPTGEN_TOOLS` | `["str_replace_based_edit_tool", "task_done"]` | **刻意不给 `bash`**：实测 bash 让 agent 把 4 步浪费在 `cd` 与 `chcp 65001`（Windows 代码页）上，纯生文毫无必要；不给 `sequentialthinking`（实测吃掉 7/18 步做长篇自问自答）。只剩"文件读写 + 收尾"，彻底杜绝误碰文件系统。 |
| `SCRIPTGEN_MAX_TOKENS_FLOOR` | `8192` | 5 镜分镜 + 每镜 i2v 提示词 + 念白动辄数千 token，供应商若只给 4096 会被截断，抬下限。 |
| `SCRIPTGEN_MAX_RETRIES` | `1` | 外层 `retry_with` 次数（失败后随机 sleep 3-30s 再试）。从默认 10 压下来：单次请求动辄要吐数千 token，一次抖动就是几分钟，10 次重试只会把"卡 5 分钟"拖成"卡 15 分钟以上"。**与 `SCRIPTGEN_LLM_TIMEOUT` 是乘算关系**，调大前先算总账。 |
| `SCRIPTGEN_LLM_TIMEOUT` | `300`（秒） | 注入上游 client 的单次请求超时。流式下它的含义是**两个分片之间的最大间隔**，而非总时长。 |
| `TRAE_LLM_STREAM` | `1`（生文任务注入） | **见下节 —— 这是长输出能否成功的分水岭。** |

**为什么必须开流式（R-13 实测结论，别回退这个开关）**

本项目用的中转站 `your-relay.example.com` 前面挡着 Cloudflare，其 **Proxy Read Timeout 硬上限
= 120 秒**。非流式请求要等源站把整段响应生成完才回数据，一旦超线就被 CF 掐断，实测原文：

```
524 - 'The origin web server did not return a complete response within the
120-second Proxy Read Timeout window.'   zone: your-relay.example.com | retry_after: 120
```

观测到的现象与它完全吻合：某次"输出 5,919 token"的请求用了 116s（踩线侥幸通过），紧接着
"追加剩余镜头"那一步超线立刻 524。**这与通道快慢无关** —— 哪怕通道有 100 tok/s，
输出 12,000 token 也要 120s，非流式照样必挂。流式下分片持续流动，CF 收到首个分片就开始
转发，不会走到 524，**这是解锁长输出的唯一办法**。

实现落在上游 `openai_compatible_base.py`（`patches/0003-llm-streaming.patch`）：
`_create_response` 按 `TRAE_LLM_STREAM` 分流到 `_create_response_streamed()`，
把分片攒成一个**与非流式等价**的 `ChatCompletion` 返回，所以 `chat()` 及其下游一行都不用改。
另外**上游默认不设 timeout**（`read=600/write=600/pool=600`）**且 SDK 自己还会重试 2 次**，
与外层重试叠乘后一次抖动就能拖成十几分钟 —— `patches/0002-llm-request-timeout.patch`
让 `doubao_client` 从环境变量读这两个值，**不设时完全保持上游行为**。

**超时不丢产出**：`context.md` 要求 agent "分次落盘、单次不写太多"（单次输出越长，请求越慢
越容易卡）。`_run_trae` 在 `asyncio.wait_for` 超时后 `proc.kill()`，但**先读盘**
`./output/script.md`；只要有部分产出就返回（带 `warning` 字段提示完整性），
只有一点产出都没有才报 504。

**资料是内联的，不是让 agent 自己读**：5 份必读资料（`SKILL.md` / `storyboard-15s.md` /
`prompt-layering.md` / `copywriting-style.md` / `storyboard-template.md`，合计 65KB）
由 `_read_inline_docs()` 整份写进 `context.md` 末尾，`context.md` 里明确要求
**不要再读任何文件**。从前"让 agent 自己读这 5 个文件"= 5-7 次串行请求，每步输入带着
2.8 万 token 的历史、输出只有 50-90 token（纯为决定"下一个读哪个文件"），
而且每步都多一次撞长尾的机会。`./skill/` 目录仍会拷进沙箱（供人工核对），但**只是备查**。

### 6.2 与「完整流水线」通道的区别

| 维度 | 第 3 节（完整流水线） | 第 6 节（生文专用） |
|---|---|---|
| 用途 | agent 串起建项目→写分镜→lint→渲染→QC | 只出**脚本分镜 + 逐镜 i2v 提示词**文本 |
| 配置 | 用户手维护的 `trae_config.yaml`（可能挂 MCP） | 后端按需生成的 `trae_config.scriptgen.yaml`（不挂 MCP） |
| 花钱 | 可选开（`--allow-spend`） | 永远 `allow_spend=False`，不触发出图/出片 |
| 入口 | 命令行 `trae-cli run` | 网页按钮 + `POST /api/projects/{id}/generate-script` |

### 6.3 生文通道专属坑

- **后端没重启 → 405**：后端 `main.py` 的 `@app.get("/{full_path:path}")` 只接受 GET，未注册的 POST
  会落到它身上返回 **405（不是 404）**——这是路由没生效的信号。先确认后端跑的是新代码
  （`GET /openapi.json` 路径里应含 `/api/projects/{project_id}/generate-script`）。
- **密钥前置 → 400**：文本大模型没配密钥，`_ensure_config` 读 `providers.yaml` active llm 的 key 会直接 400。
- **清代理**：启动时清掉本机失效的 `http_proxy`/`https_proxy` 等（LLM 直连避 502）。
- **运行目录清理**：`.trae_runs/` 里 3 天前的沙箱目录会被 `_prune_old_runs` 自动删（只删本工具自己写的）。

### 6.4 粘贴外部脚本：不走 Agent 的第二条入口

需求：「生成视频提示词的页面允许用户粘贴外部生成的脚本导入为分镜，在 agent 生成脚本之外多开一个口子」。

弹窗拆成两个 Tab，**共用同一条导入链路**（`POST /api/projects/{id}/import-script`）：

| Tab | 入口 | 脚本从哪来 |
|---|---|---|
| ① 用 Agent 生成脚本 | 行内按钮「生成视频提示词」 | trae-cli 读技能现写（§6） |
| ② 粘贴外部脚本 | 行内按钮「粘贴脚本导入」 | 用户从别处复制进来，**不落盘、直接解析入库** |

**`dry_run` 预览**（`ImportScriptIn.dry_run`）：只解析 + 跑 lint，**不落库、不改项目状态**，
且**解析不出镜头时也回 200 + `warnings`**（普通导入是 400）。理由：用户需要看到「为什么没解析出来」，
而不是一个通用报错。响应每镜带 `prompt_source`（`fence`/`label`/`body`）与 `prompt_head`，
前端用表格把「识别到几镜、时长对不对、lint 几分」摊开给用户确认后再导。

**解析器要同时吃下两种来源**，所以 `parse_script` 分成「模板严格路径」+「外部宽松兜底」：

- 标题：`##`/`###`/`####` 都算小节；`Shot 1` / `镜头1` / `S1` 都能取号；裸数字补成 `S1`；
  没写镜号按小节序号自动编（并发 warning）；时间码兼容**全角** `～`(U+FF5E) / `〜`(U+301C) / `－`(U+FF0D)。
- 提示词优先级：` ```text ` 围栏块 > `**提示词**：`标签行 > 整段正文兜底。
- 两条**反向守卫**（都是实测踩出来的）：
  1. **只认「像镜头」的小节** —— 标题带镜号 / 带时间码 / 正文里有围栏块，三者有其一。
     Trae 模板在 5 个镜头之后还有 `## 念白稿` `## 音效落位` `## 自检评分` `## 关键决策说明`
     四个中文章节，正文兜底会把它们**当成镜头导入**（实测真实脚本从 5 镜变 7 镜、时长 15s 变 20.5s）。
  2. **正文兜底要求「以英文为主」**（ASCII 占比 ≥ 70%）。中文散文小节的占比实测 32% / 62%，
     英文提示词 ~100%，一刀切得干净。
- **刻意不解析 markdown 表格**：列的顺序与含义各家不同，且模板里那张「分镜表」的中文列是
  画面简述与念白、**不是** i2v 提示词，硬解析只会产出跑偏的镜头。检测到疑似提示词表就只给
  一条「请改成分节格式」的提示。

**实测**（临时项目、未花钱）：外部格式脚本（`## Shot 1` + `Prompt:` 标签 + 无围栏正文 + 中文 `镜头3（7～11s）`）
→ 识别 3 镜 / 264 帧 = 11.0s，dry_run 零写入，正式导入 3 新建，二次导入 3 覆盖（幂等）。
lint 只给 4/2/4 —— **外部短提示词通常过不了 v2 结构门禁（≥8）**，这是正常的：
`lint_failed` 徽标正是在告诉用户「能出片，但镜头语言偏薄」，可在分镜页逐镜改。
