# AI Video Studio · MCP 接入（P-7）

把本地工作室暴露给 **Claude Desktop / Cursor / 任意 MCP 客户端**，让 agent 能直接
建项目、写分镜、出片、后期、自检 —— 不用你手点十来个页面。

---

## 1. 它是什么（以及**不是什么**）

这是一个 **stdio MCP server，纯薄壳**：

```
MCP 客户端 ──stdio/JSON-RPC──> mcp_server.py ──HTTP──> 后端 (127.0.0.1:8000)
```

**它不实现任何业务流程。** 排期、重试、计费台账、预算熔断、队列调度、合规词表、
质检判据 —— 全部是后端那一份实现。本层只做三件事：参数整形、HTTP 转发、结果摘要。

为什么坚持这样：一旦 MCP 自己写一遍"生成分镜前要先查产品香调事实"这类规则，
两条路径必然分叉 —— 网页上手能跑、agent 调却漏规则，改一处忘另一处。
代价是每次工具调用多一跳本机 HTTP（微秒级），收益是**业务流程永远只有一份真相**。

**零新增依赖**：只用 `httpx` + 标准库。打包进 Electron 时不需要额外装东西。

---

## 2. 快速开始

**前提**：后端要在跑。MCP server 不会替你启动后端。

```bash
cd <backend 目录>
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

然后在 MCP 客户端里加一条 server 配置。**最省事的办法是打开软件的「设置」页，
最下面那张「AI Agent 接入（MCP）」卡片里点「复制配置」** —— 里面的解释器路径和
脚本路径都是后端算好的（指向当前真正装着依赖的那个 Python）。

手写的话长这样：

```json
{
  "mcpServers": {
    "ai-video-studio": {
      "command": "<backend 目录>/.venv/Scripts/python.exe",
      "args": ["<backend 目录>/mcp_server.py"],
      "env": {
        "STUDIO_BASE_URL": "http://127.0.0.1:8000",
        "STUDIO_MCP_ALLOW_SPEND": "0"
      }
    }
  }
}
```

- **Claude Desktop**：设置 → Developer → Edit Config，改 `claude_desktop_config.json`
- **Cursor**：项目或全局 `mcp.json`
- 改完**重启客户端**（MCP server 是客户端拉起的子进程）

Windows 下命令行验证在不在这儿（不开客户端也能测）：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python mcp_server.py
```

---

## 3. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `STUDIO_BASE_URL` | `http://127.0.0.1:8000` | 后端地址。指向非本机时**会**走系统代理 |
| `STUDIO_MCP_ALLOW_SPEND` | `0` | **是否允许花钱**。见下节 |

---

## 4. 安全模型：默认不许花钱

`render_shot` / `generate_storyboard` / `reverse_video` 会调用**付费接口**。
默认状态下调用它们会被**在本地直接拒绝**，且**不会向后端发出任何请求**：

```
未授权花费，已拒绝：render_shot 会调用付费接口，但当前 MCP 会话没有花钱权限。
· 要放开：在 MCP 客户端配置的 env 里加 STUDIO_MCP_ALLOW_SPEND=1，然后重启客户端
· 只想看费用：改用 render_shot 的 dry_run=true（免费试算）
（本次拒绝在本地完成，没有向任何供应商发出请求）
```

**为什么默认关**：agent 一旦拿到出片权，就可能在你没盯着的时候连着烧钱。
默认给"能读、能算、能自检"，花钱要你显式开一次。

另外两层护栏（都在后端，不依赖 MCP）：

- **P-5 花费上限熔断**：超过全局 / 项目 / 单次 / 供应商上限时，`render_shot` 返回
  **402** 并说明还差多少 —— MCP 会把它翻译成"先用 get_budget 看余额"。
- **P-4 合规门禁**：念白若有高危广告法违规词，`run_final` 返回 422 并点名命中词。

**没有删除类工具。** 外部 agent 能一路往前推进（建项目 → 分镜 → 出片 → 后期 → 自检），
但删不掉你的项目和资产。

---

## 5. 工具清单（17 个）

| 分组 | 工具 |
|---|---|
| 项目 | `list_projects` · `get_project`（含成本） · `create_project` |
| 分镜 | `generate_storyboard`* · `list_shots` · `update_shot` · `lint_prompt` |
| 出片 | `render_shot`* · `get_job` · `wait_for_job` |
| 质检 | `compliance_scan` · `qc_project` · `verify_delivery` |
| 后期 | `run_final` |
| 预算 | `get_budget` |
| 素材 | `list_assets` |
| 反推 | `reverse_video`* |

`*` = 计费工具（受 `STUDIO_MCP_ALLOW_SPEND` 门控）。

### 典型流水线

```
create_project  →  generate_storyboard  →  list_shots 复核 lint
  →  render_shot (先 dry_run=true 试算)  →  wait_for_job
  →  compliance_scan 扫念白  →  run_final  →  verify_delivery
```

---

## 6. 几个设计取舍

**出片是异步的。** `render_shot` 立刻返回 `job_id`，出片本身要跑几十秒到几分钟。
`wait_for_job` 在工具内轮询（`max_wait` 默认 300s、上限 900s）；超时**不算失败**，
返回里带 `_timed_out`，稍后再 `get_job` 即可。这是为了不让客户端把一次工具调用
挂死十分钟。

**`reuse` 默认 true。** 该镜已有成功产物时直接复用，不重复付费。

**`lint_prompt` 是免费的、无副作用的。** 改稿时先用它试分数，别急着 `update_shot`
落库。`update_shot` 落库后后端会自动重算 lint，返回的就是改后真实分数。

**结果超长会被截断**（40k 字符）并给出提示 —— 避免把 agent 的上下文冲爆。

**错误信息带"下一步"。** 404 会说"先用 list_projects 拿真实 id"，402 会说"先用
get_budget 看余额"。agent 能不能自己纠错，基本取决于错误信息质量。

---

## 7. 排错

**客户端里看不到工具 / 连不上**

1. 先确认后端在跑：`curl http://127.0.0.1:8000/api/health`
2. 确认 `command` 指向的解释器装了 `httpx`（用软件设置页复制的配置就不会错）
3. 手动跑一次 `echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python mcp_server.py`，
   看 stderr 有没有异常

**报 502 / 网关错误**

系统代理（`HTTP_PROXY`）拦了发往 `127.0.0.1` 的请求。本客户端**对 localhost 已关掉
`trust_env`**，所以正常不会发生；若仍出现，检查是否有全局透明代理或 `NO_PROXY` 配置
异常。（这个坑实测踩过：httpx 默认会把 localhost 也塞给代理，代理连不上就回 502，
把"后端没启动"误报成"网关错误"，排查方向完全跑偏。）

**stdout 被污染导致客户端解析失败**

**任何 `print` 都会破坏协议** —— stdio MCP 的 stdout 是协议通道，只许出现 JSON-RPC。
本实现所有日志走 stderr。改代码时请守住这条。

**中文乱码**

Windows 默认 stdout 可能是 GBK。`main()` 里已 `reconfigure(encoding="utf-8")`。

---

## 8. 加一个新工具（三步）

在 `app/mcp/tools.py` 里：

```python
def _h_my_tool(c: StudioClient, a: Dict[str, Any]) -> Any:
    pid = _need(a, "project_id", int)          # 1. 取参数（缺了会报可读错误）
    return c.post(f"/api/projects/{pid}/xxx",  # 2. 转发（别在这儿写业务逻辑）
                  json_body={"k": a.get("k")})

Tool(
    name="my_tool",
    group="质检",
    spend=False,                                # 3. 会花钱就置 True
    description="一句话说清**何时用**、**副作用**、**返回什么**。",
    schema=_obj({"project_id": {"type": "integer"}}, ["project_id"]),
    handler=_h_my_tool,
)
```

加进 `TOOLS` 列表即可，`tools/list` 会自动带上。**如果要做的事后端还没有对应端点，
先去后端加端点** —— 不要在 MCP 层拼业务流程。
