# Personal Avatar RAG Strategy

最近更新：2026-05-21

## 目标

这个 RAG 不追求“把文档切碎后语义搜索”这么简单，而是服务一个明确场景：访客围绕李鑫的职业经历、项目案例、AI 产品能力、岗位匹配、面试追问进行问答，并且回答要能标注来源、区分事实和推断。

## 当前职业档案结构

职业生涯全景档案是半结构化 Markdown，主要层级是：

- 时间线。
- 按产品线组织的经历：AI 对话、智能门锁、IoT 设备平台、FSM、SaaS 化、MES/WMS/ERP、工单 AI、个人实践、商业化。
- 横向能力地图。
- 推荐职业叙事。
- 岗位调用索引。
- 稳妥表达库。
- 待核实素材池。

这类文档不能只按固定长度切分。更好的基本单元是“语义小节”，例如“8.4 关键产品判断”“11.1 AI 产品能力”“13.1 AI 应用产品经理方向”。

## Chunk 设计

当前采用 Markdown 标题结构切分，并做了两点修正：

- 跳过只有标题、没有正文的信息空 chunk。
- 给每个 chunk 加元数据：`parent_heading`、`section_title`、`section_kind`、`is_personal_practice`、`is_pending`。

推荐继续演进成三层索引：

- `atomic chunk`：三级小节，适合精确引用和来源悬浮。
- `route chunk`：每条产品线合成一张项目卡，包含定位、职责、判断、结果、风险口径，适合先召回项目。
- `thematic chunk`：能力地图、岗位索引、职业叙事，适合回答“优势/匹配/怎么介绍他”。

## 检索策略

当前链路：

1. Qwen 分类并改写问题。
2. 用硅基流动 `BAAI/bge-m3` 生成 query embedding。
3. 对职业档案 chunk embedding 做余弦相似度排序。
4. 叠加轻量关键词分、问题类型 boost、元数据 boost。
5. 做简单多样性控制，避免 Top 6 被同一产品线占满。
6. 取 Top 6 给 MiniMax 回答。

推荐下一步：

- 先召回 Top 20，再用 reranker 重排到 Top 6。
- 分类器输出 `retrieval_intent`，例如 `timeline`、`project_deep_dive`、`ai_pm_pitch`、`risk_check`。
- 对 `is_pending=true` 的 chunk 默认降权，只有用户问指标、风险、口径时升权。
- 对 `is_personal_practice=true` 的 chunk 默认降权，只有用户问 Agent/RAG/个人实践时升权。
- 对“适合什么岗位/核心优势”这类问题，优先召回能力地图、职业主线、工单 AI、真实 AI 对话产品，再补充个人实践。

## 多轮对话

多轮对话中不要直接用当前短问题检索。当前做法是：

- 保存 conversation 和 messages。
- 分类器读取最近若干轮对话。
- 分类器输出 `rewritten_question`。
- RAG 用 `rewritten_question` 检索。
- MiniMax 回答时同时看到最近对话上下文和检索片段。

## 评测数据

上线前 golden set 应至少覆盖：

- 职业主线介绍。
- 公司/时间线事实。
- AI 产品经验。
- 工单 AI 分析项目。
- 智能硬件 / IoT 项目。
- 岗位匹配。
- 公司项目 vs 个人实践边界。
- 待核实指标。
- 无关问题拒答。
- 隐私问题拒答。

上线后 bad case 需要记录：

- 原始问题。
- 多轮上下文。
- 分类结果和分类理由。
- 改写后的检索问题。
- 检索候选数量、Top chunk、分数、来源标题。
- 最终回答。
- 用户反馈。
- 修复状态。

这些字段已经进入 SQLite 的 `questions`、`messages` 和 `eval_cases` 表。
