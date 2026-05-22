# AGENTS.md

本项目是个人资料问答 Agent 的第一版，当前实现重点是可信 RAG + 真实 LLM 回答，而不是完整自主 Agent。

## 工作规则

- 开始修改前先读 `CONTEXT.md`、`README.md` 和 `knowledge/sources/source-registry.json`。
- 不要直接读取或接入整个 `/Users/sanjin/无用` 目录；当前公开问答检索只允许使用职业生涯全景档案。
- 不要把 `AREA/数字化身/shadow.md`、日记、账号、密钥、联系方式等隐私资料加入公开 registry
- 回答逻辑必须保留“资料不足就说明无法确认”的边界。
- 任何会改变公开问答资料范围的改动，都要同步更新 source registry 和 `CONTEXT.md`。

## 项目结构

- `app/main.py`：标准库 HTTP 后端，包含分类、职业档案检索、LLM 流式回答、日志和后台接口。
- `app/static/`：Codex 风格聊天页和后台页。
- `knowledge/public/`：本项目自有公开资料和回答规则。
- `knowledge/sources/source-registry.json`：外部资料源登记表。
- `data/`：运行时 SQLite 数据，不应提交敏感内容。

## 验证

```bash
python3 app/main.py --port 8787
```

至少验证：

- `/` 能打开聊天页。
- `/admin` 能打开后台。
- `/api/chat/stream` 能流式返回进度、回答片段、来源、质量分和后续推荐问题。
- `/api/llm/status` 能看到当前 provider，不暴露 API key。
- `/api/admin/summary` 能看到统计。

## Context 维护

对本项目做实质修改后必须更新 `CONTEXT.md`。如果项目状态或目录总览改变，也要更新上级 `项目/CONTEXT.md`。
