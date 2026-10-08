# 便携版打包（Windows）

把整个软件打成**一个文件夹 + 一个 zip**，用户解压后双击 `avs.exe` 就能用，
**不需要装 Python、不需要装 ffmpeg、不需要代码签名、不需要安装器**。

---

## 一、快速出包

```bash
# 用带全部依赖的解释器（不是托管版 python！见下方「环境」）
PY=~/.workbuddy/binaries/python/envs/default/Scripts/python.exe

$PY build/make_portable.py            # 构建 + 拷 ffmpeg + 打 zip
$PY build/make_portable.py --no-zip   # 只出文件夹（调试更快）
```

产物：`dist_portable/avs/`（文件夹）与 `dist_portable/avs-portable.zip`。

---

## 二、四个脚本各干什么

| 文件 | 作用 |
|---|---|
| `build/fetch_wheels.py` | 装 PyInstaller 及其依赖。**本机 pip 装不了任何东西**（索引不通），这个脚本用 pip UA 走 urllib 取 wheel + 离线装 |
| `build/launcher.py` | 启动器：摘代理 → ffmpeg 进 PATH → 起 uvicorn → 等 health → 开浏览器 |
| `build/launcher.spec` | 打包配置：数据文件打到 `_internal/`，hiddenimports 补全动态导入 |
| `build/make_portable.py` | 一键串联：构建 → 拷 ffmpeg → 写说明 → 打 zip |

---

## 三、★关键设计：为什么不用改一行业务代码

后端有 **12 处** `Path(__file__).resolve().parents[N]` 定位本地文件。
PyInstaller **onedir** 打出的结构恰好让它们全部对上：

```
dist_portable/avs/
  avs.exe
  _internal/            ← sys._MEIPASS
    app/deps.py ...     ← 打包后的模块
```

- `app/deps.py`（app/ 下）→ `parents[1]` = `_internal`
- `app/db/session.py`（app/db/ 下）→ `parents[2]` = `_internal`

两者**都等于 `_internal`**，所以只要 `--add-data` 把数据打到 `_internal/` 下的同名位置，
那 12 处路径**自动全部正确**。

已实测：`app.db` 生成在 `_internal/data/app.db`、`/api/providers` 正常读出
`_internal/providers.yaml`、前端 `index.html` 与 js/css 全部 200。

### 唯一例外（踩过）

`app/main.py` 的 `FRONTEND_DIST` 原先写死 `parents[2]`，因为**前端在仓库根而不在 backend 下**：

- 开发态 `parents[2]` = `ai-video-studio/` ✓
- 打包态 `parents[2]` = `avs/` ✗，正确的是 `parents[1]` = `_internal/`

症状很有欺骗性：**`/api/*` 全部正常，只有首页 404**（`/` 返回 `{"detail":"Not Found"}`），
极易误判成「前端没打包」。已修为 `_find_frontend_dist()` 依次试两个层级取存在者。

### 代价

用户数据（`data/`、`.secrets.enc`）与程序依赖混在 `_internal/` 下。解压目录可写所以功能正常，
但**删掉 `_internal/` 等于删库**。说明文件里已写警告。要彻底分离得给路径加 `AVS_BASE`
环境变量覆盖并改那 12 处——本次不做。

---

## 四、⚠️ ffmpeg 体积问题（分发前必读）

**代码确实需要 ffprobe**（`app/audio/ffmpeg.py` 用它探时长与流信息），ffmpeg + ffprobe 都得带。

本机 WinGet 装的是 **full_build，单个 exe 222MB**，两个合计 444MB
→ 便携包 **579MB（zip 243MB）**，基本没法分发。

**解决办法**：换成 **essentials** 版（两个合计约 60MB）：

1. 下载 <https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip>（约 30MB zip）
2. 解压后把 `ffmpeg.exe` + `ffprobe.exe` 放进 `build/ffmpeg/`
3. 重跑 `python build/make_portable.py`

`find_ffmpeg()` 会**优先用 `build/ffmpeg/`**；没有才回退到 PATH 上的。
`make_portable.py` 还会检查体积，超阈值打印警告并提示换 essentials。

> 换 essentials 后便携包预计 **~210MB（zip ~90MB）**。
> 注意本机大文件下载容易被掐断（essentials 试了 10 分钟只拿到几百 KB），
> 断点续传也失败——建议手动下载后再打包。

---

## 五、环境（踩过的坑）

**必须用这个解释器**：

```bash
~/.workbuddy/binaries/python/envs/default/Scripts/python.exe
```

- ✅ 它有全部依赖：fastapi / uvicorn / numpy / scipy / sqlalchemy / alembic /
  cryptography / keyring / edge_tts / win32api / PIL / httpx / multipart
- ❌ 托管版 `binaries/python/versions/3.13.12/` **缺 fastapi、sqlalchemy 等**，
  拿它跑后端会静默失败（曾据此误判「沙箱拦截 import」——**真相是用错解释器**）
- ❌ 系统 Python 3.12 也缺 scipy / sqlalchemy / alembic / cryptography / keyring

**其他坑**：

- `pip install` 任何包都失败（索引不通）。用 `build/fetch_wheels.py`
- `curl` / `urllib` 访问 PyPI **必须用 pip 的完整 UA**，浏览器 UA 一律 403
- 构建时 PyInstaller 的 `--noconfirm` 删除旧目录会触发环境的
  `SAFE_DELETE_BULK_CONFIRM_REQUIRED`（>50 文件）→ 换一个 `--distpath` 即可
- 测试 exe 必须「启动→轮询 health→curl→kill」写在**同一条命令**里：
  本工具会回收长驻进程，`(cmd &)` 也会被杀

---

## 六、已验证清单（v2 产物实测）

- ✅ exe 启动 2 秒，`/api/health` 全绿
  （`code_current:true` / `stale_modules:[]` / `ambient_proxy:[]`）
- ✅ `_internal/data/app.db` 正确生成（SQLite 可写）
- ✅ `/api/providers` 读出 `_internal/providers.yaml` 完整配置
- ✅ `/` 返回 index.html；SPA 深链 `/projects/3` 正常回 index.html
- ✅ js 1.39MB / css 均 200；`/api/projects`、`/api/providers`、`/api/talents/options` 全 200
- ✅ **随包 ffmpeg 真的被调用**（上传非视频文件时后端确实执行了 ffmpeg 并报错）
- ✅ 中文日志正常（UTF-8 重配 + 同时落 `avs.log`）

**未验证**（换 essentials 后需重测）：真实视频的抽帧/装配/混音全链路。

---

## 七、待办

- [ ] 换 essentials 版 ffmpeg，把包压到 ~210MB
- [ ] exe 改名（当前 ASCII `avs.exe`，可改「AI视频工作室.exe」）
- [ ] 图标（PyInstaller `icon=`）
- [ ] 在**干净机器**上验证（本机有开发环境会掩盖漏包问题）
- [ ] 跑通一条真实 15s 片（抽帧→装配→混音）
