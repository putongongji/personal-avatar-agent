# Personal Avatar Agent

个人资料问答 Agent 的第一版实现。当前阶段以 RAG 问答为核心，目标是让用户能提问关于叁金的职业经历、项目案例、AI 产品能力、智能硬件经验和公开协作方式，并获得带来源的回答。

## 当前能力

- 聊天页：输入问题，或点击推荐问题。
- 问题分类：职业经历、项目案例、AI PM 匹配、IoT/硬件、个人风格、隐私边界等。
- 流式回答：只在真实步骤完成后显示进度，例如问题进入处理、方向判断完成、职业档案检索完成、回答上下文构造完成。
- 向量 RAG：当前只从职业生涯全景档案中分块，使用硅基流动 `BAAI/bge-m3` embedding 做向量召回，并保留关键词检索兜底。
- 真实 LLM 回答：只使用 MiniMax。
- 来源标注：回答正文内使用 `S1`、`S2`、`S3` 等标记，不显示来源卡片。
- 来源悬浮：回答中的 `S1`、`S2`、`S3` 引用标记支持悬浮查看原文中最相关的一句话，不再显示来源卡片。
- 推荐问题：初始随机出现 3 个，回答完成后根据回答内容生成 3 个后续问题。
- 提问记录：每次提问写入 SQLite。
- 后台入口：查看高频提问、分类统计、资料缺口和回答质量分。

## 运行

```bash
cd /Users/sanjin/无用/项目/personal-avatar-agent
python3 app/main.py --port 8787
```

如果要使用真实 LLM，配置环境变量或本项目 `.env`：

```bash
MINIMAX_API_KEY=...
MINIMAX_BASE_URL=https://api.minimax.io/v1
MINIMAX_MODEL=MiniMax-M2.7
SILICONFLOW_API_KEY=...
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=BAAI/bge-m3
CLASSIFICATION_MODEL=Qwen/Qwen3-8B
CLASSIFICATION_FALLBACK_MODELS=Qwen/Qwen2.5-7B-Instruct
ADMIN_EMAIL=...
ADMIN_PASSWORD=...
```

`Qwen/Qwen3.5-4B` 在当前 SiliconFlow chat completions 调用中会出现读超时，线上分类默认改用已验证可稳定返回 JSON 的 `Qwen/Qwen3-8B`。

`SILICONFLOW_API_KEY` 使用硅基流动 API key；如果认证失败，系统会保留关键词检索兜底，并在 `/api/llm/status` 暴露 embedding 错误状态。

访问：

- 聊天页：`http://127.0.0.1:8787/`
- 后台：`http://127.0.0.1:8787/admin`

访问聊天页前需要选择身份：访客可一键进入并自动生成访客账号；管理员需要用 `ADMIN_EMAIL` / `ADMIN_PASSWORD` 登录，只有管理员能访问后台。

## 部署

项目已适配 Vercel：

- `public/`：线上静态聊天页和后台页。
- `api/`：Vercel Python Functions 入口，复用 `app/main.py` 的 API 处理逻辑。
- `knowledge/public/career-panorama.md`：线上可检索的公开职业档案副本。

Vercel 环境变量需要在项目设置里配置，不要提交到仓库：

```bash
MINIMAX_API_KEY=...
MINIMAX_BASE_URL=https://api.minimax.io/v1
MINIMAX_MODEL=MiniMax-M2.7
SILICONFLOW_API_KEY=...
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=BAAI/bge-m3
CLASSIFICATION_MODEL=Qwen/Qwen3.5-4B
```

当前线上 SQLite 数据库运行在 Vercel 可写临时目录里，适合 MVP 访问验证；如果要长期保存提问记录、评测报告和后台统计，需要换成持久化数据库。

## 资料源

资料源登记在 `knowledge/sources/source-registry.json`。当前问答检索只启用职业生涯全景档案：

- `knowledge/public/career-panorama.md`

`shadow.md`、日记、隐私信息和密钥文件不进入公开问答。

## 后续方向

- 加入 reranker 和引用一致性检查。
- 优化 LLM prompt 和质量评估。
- 增加用户身份和资料权限。
- 增加资料缺口到知识库的确认式写回。
- 用 LangGraph 增加轻 Agent 调度：分类、检索、追问、质量评估、待确认事项生成。

## 评测

- 评估维度：`docs/evaluation-framework.md`
- Golden set：`eval/golden-set.json`
- 本地数据库：golden cases 已写入 SQLite 的 `eval_cases` 表，kind 为 `golden_single` / `golden_multi`。
