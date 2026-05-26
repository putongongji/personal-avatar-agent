# personal-avatar-agent Context

最近更新时间：2026-05-25

## 这个项目是什么

一个个人资料问答 Agent 的 MVP。当前阶段先做可信 RAG 问答：用户可以提问关于叁金的职业经历、项目案例、AI 产品能力、智能硬件经验和公开协作方式，系统基于登记资料源检索并回答，同时标注来源。

## 当前目标

快速验证“提问 -> 分类 -> 检索 -> 回答 -> 来源 -> 日志 -> 后台反馈”主链路，为后续轻 Agent 化预留模块边界。

## 当前状态

已搭建标准库 HTTP 单体应用，包含 Codex 风格聊天页、简洁执行进度、可停止的逐字打字机回答、Markdown 回答渲染、动态推荐问题、S1/S2 引用悬浮原句、登录入口、后台入口、职业档案检索、MiniMax 回答、硅基流动 `BAAI/bge-m3` embedding 接口、Qwen 分类、多轮会话、SQLite 提问记录、资料缺口统计和回答质量分。登录状态使用签名 cookie，项目已增加 Vercel 部署适配，线上以 `public/` 静态页 + `api/` Python Functions 运行。

## 最近进展

- 新增 `app/main.py`，实现标准库 HTTP 服务、问题分类、检索、回答、来源组装、日志和后台 API。
- 接入 MiniMax answerer；本地 `.env` 当前配置为 MiniMax；没有可用模型时回退到本地模板摘要。
- 新增 `/api/chat/stream`，前端可显示理解问题、检索职业档案、生成回答、整理来源等进度，并逐段渲染回答。
- 调整流式体验：进度只在真实步骤完成后显示，回答内容在前端逐字输出，取消来源卡片，改为回答里的 S1/S2/S3 引用标记悬浮显示最相关原句。
- 重写 MiniMax prompt：限定只回答李鑫职业生涯相关问题，要求区分公司项目、个人实践、待核实和推断；无关问题直接拒答，不再检索或返回来源。
- 改进本地检索排序：按问题类型扩展检索词，叠加标题命中、事实确认、类型匹配、覆盖度和长度归一化；来源悬浮句优先展示完整原文句子。
- 将职业问答系统约束移入 MiniMax system message，user message 只放当前问题和检索片段。
- 将向量召回切换为硅基流动，使用 `https://api.siliconflow.cn/v1/embeddings` 和 `BAAI/bge-m3`；embedding 成功时优先向量检索，失败时保留关键词检索兜底。
- 修正检索进度文案：不再暴露模型名、分类、候选数量或来源标题，只显示意图识别、检索资料、组织回答、整理来源等用户可理解动作。
- 接入硅基流动 Qwen 做问题分类和检索问题改写；`Qwen/Qwen3.5-4B` 实测会读超时，线上默认改用 `Qwen/Qwen3-8B`，并保留 `Qwen/Qwen2.5-7B-Instruct` 与规则分类 fallback。
- 新增 `conversations`、`messages`、`eval_cases` 表，支持多轮对话、聊天状态恢复、golden set / bad case 数据沉淀。
- 改进职业档案 chunk：跳过只有标题的空 chunk，增加父级产品线、小节类型、个人实践、待核实等元数据，并在检索排序中使用这些元数据。
- 新增 `docs/rag-strategy.md`，记录面向职业问答的 chunk、检索、召回、多轮和评测策略。
- 新增 `docs/evaluation-framework.md` 和 `eval/golden-set.json`，定义 8 个评估维度与 24 条 golden cases；数据库初始化会自动种入 `eval_cases` 表。
- 后台新增评测面板，可运行前 5 条或全部 golden cases，并把分类、检索、回答、断言检查结果写回 `eval_cases`；后台高频提问、分类、缺口和最近提问统计会排除评测流量。
- 重做聊天页交互：新增对话列表与新话题入口，流式输出默认跟随最新内容但尊重用户手动滚动；用户滚轮向上、触摸、拖动滚动区或按 PageUp / 方向键时会立即暂停自动跟随，输入框回车发送、Shift+Enter 换行且不出现内部滚动条，进度改为运行中动效与完成后默认折叠的执行过程，回答中可点击停止。
- 改进后台评测报告：`eval_runner` 不再只显示失败原因，会按分类、检索、答案覆盖、边界和引用拆分维度分，并标明缺失事实是未召回还是已召回但未写入回答。
- 后台评测报告改为弹窗呈现，并新增按需生成的 MiniMax AI 分析，基于每条 golden case 的维度分、实际回答、实际来源和召回结果分析原因与改进方案，结果缓存到 `details_json.ai_analysis`。
- 新增 Vercel 部署结构：`public/` 存放线上静态页面，`api/` 暴露 Python Function 入口，`knowledge/public/career-panorama.md` 存放公开职业档案副本。
- 新增轻量登录：管理员用环境变量配置账号密码登录并访问后台；访客一键进入，系统自动生成访客账号；对话、消息和提问记录都会绑定用户身份，后台可查看用户维度数据。
- 修复 Vercel 临时 SQLite 导致后台刷新后 session 丢失的问题：session 改为签名 cookie；后台刷新失败时只显示状态，不再自动跳登录；评测面板会显示 golden cases 和 bad cases。
- 修复前端引用渲染二次替换导致的 `S1">S1` 问题。
- 改造 `app/static/`，聊天页取消侧栏和说明文案，以聊天框为中心，推荐问题放进输入区。
- 更新 `knowledge/sources/source-registry.json`，当前仅启用职业生涯全景档案作为检索来源。
- 新增 `knowledge/public/answer-policy.md`，定义公开回答边界。

## 当前卡点

- embedding 代码已按硅基流动接口接入，本地单条调用已验证可返回 1024 维向量。
- Qwen 分类可用；线上使用 `Qwen/Qwen3-8B`，当前保留备用模型和规则 fallback。
- MiniMax 回答已接入，prompt 已按职业档案问答重写，但仍需要用真实访客问题继续打磨检索权重和回答口径。
- 当前公开问答只支持职业生涯档案，项目 context 和数字化身摘要暂不参与回答。
- Vercel 上的 SQLite 数据库写在临时目录，适合 MVP 链路验证；要长期保存提问记录、评测报告和后台统计，需要接入持久化数据库。
- 当前登录 session 不再依赖 SQLite；但用户提问、评测运行结果和后台统计仍写在 Vercel 临时 SQLite，正式使用需要持久化数据库。

## 下一步

1. 本地跑通并用 20 个标准问题测试回答质量。
2. 根据真实问题补充公开 FAQ 和资料缺口。
3. 基于硅基流动向量召回的真实问题结果，继续优化检索权重和来源一致性检查。
4. 增加轻 Agent 调度：模糊问题追问、资料缺口生成、待确认事项。

## 重要文件

- `app/main.py`
- `api/_handler.py`
- `vercel.json`
- `public/index.html`
- `app/static/index.html`
- `app/static/login.html`
- `app/static/admin.html`
- `knowledge/sources/source-registry.json`
- `knowledge/public/answer-policy.md`
- `docs/rag-strategy.md`
- `docs/evaluation-framework.md`
- `eval/golden-set.json`
- `README.md`
- `AGENTS.md`

## 给 AI 的注意事项

- 不要把本地所有个人资料直接接入公开问答；当前检索范围只限职业生涯全景档案。
- 职业事实优先以 `AREA/职业生涯/职业生涯全景档案-按公司产品线.md` 为准。
- 数字化身资料只用于公开协作方式和结构说明；`shadow.md` 和日记不进入公开问答。
- AI / Agent / RAG 经验必须区分公司项目、个人实践和推断，不要包装过度。
