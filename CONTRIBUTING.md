# 贡献指南

感谢关注本项目！欢迎通过 Issue 反馈问题、提交 PR。

## 提交前检查

1. **密钥安全（最高优先级）**
   - 任何 `api_key` / `token` 不得硬编码进代码、配置或测试
   - `backend/providers.yaml` 含个人通道信息，已被 `.gitignore` 排除，请勿 `git add -f`
   - 新配置项一律走 `*_env` 环境变量名或凭据库（`app/security/secret_store.py`）

2. **代码规范**
   - 后端：Python ≥ 3.12，类型标注尽量完整；SQLAlchemy 迁移用 Alembic（`get_table_names()` 返回字符串列表，注意返回类型）
   - 前端：`npm run typecheck`（tsc --noEmit）通过后再提交；构建用 `npm run build`

3. **供应商适配**
   - 新增供应商优先走配置（`providers.yaml`）而非写死适配器：OpenAI 兼容的 LLM / 文生图 / 图生视频路径与字段已全部可配
   - 视频领域没有 OpenAI 官方标准，`openai_video` 适配器的 submit/poll/extract/status 四处均可配置覆盖

4. **计费相关改动**
   - 所有"提交即计费"的入口必须经过 `app/services/budget.py::check()` 花费闸门
   - 先落台账（`render_task`）再发请求；连接层异常必须按「结果未知」处理（裸 `ConnectionResetError` 不是 `URLError` 子类），避免钱花了没人对账

## 本地运行

见 [README.md](README.md) 快速开始。后端不带 `--reload`，改完代码记得重启。

## 提交信息

- 使用简洁的祈使句，如 `fix: 修复 GBK 脚本导入崩溃`
- 一次 PR 聚焦一件事；行为变更请在 Issue 中先讨论
