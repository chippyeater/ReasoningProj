# LogicWeaver｜第三轮：用户驱动的 InfoUnit Agent（MVP）

> 状态：待开发。本文档放入仓库 `docs/agent_info_unit_mvp.md` 后，由 Codex 按项实施和勾选。前一轮画布缩放/闪烁修复属于独立任务，勿借本任务顺手重构画布。

## 目标与完成定义

用户能够输入自然语言，要求 Agent **从案件材料定向新增一条 InfoUnit**，或**修改选中的一条 InfoUnit**；Agent 基于当前案件的 InfoUnits 与 Quote Units 返回结构化候选，用户预览证据及差异后明确确认，才调用现有 CRUD 写入。取消、生成失败、校验失败均不改动正式 CaseData。

不是通用 Agent、全案重新 Extraction，也不是自动整理/修改全部节点。

## 工具边界与最小契约

| 能力 | Agent 是否可用 | 实施方式 |
| --- | --- | --- |
| `get_info_units` | 可读 | 复用 CaseData 的现有 InfoUnit 读取；可按 `info_unit_id` 取单个节点。 |
| `search_quote_units` | 可读 | 面向当前案件的 Quote Unit 检索/筛选；MVP 小规模数据可先直接提供相关或全部 Quote Units，不增加向量库或 RAG。 |
| `get_quote_context` | 按需可读 | 仅在需要还原日期、相邻消息时使用现有 `context_quote_ids` / 分组；不要新建第二套上下文存储。 |
| `create_info_unit` | **不得由模型直接执行** | 用户确认后，由应用层调用已有后端 CRUD。 |
| `update_info_unit` | **不得由模型直接执行** | 用户确认后，由应用层调用已有后端 CRUD；保留原 ID。 |
| `delete_info_unit` | 不开放 | 本轮不支持 Agent 删除。 |

**实现原则：**“工具”是已有服务能力的薄封装，不等于必须新建 HTTP 路由、MCP Server 或 Agent 框架。若现有 LLM 客户端只支持 Structured Output，可以先在服务端准备所需只读上下文并调用一次结构化生成；不要仅为名称叫 Tool 而引入复杂工具调度。所有正式写入仍走同一套后端 CRUD 与来源校验。

模型输出必须是**提案，不是执行结果**。仅支持 `action=create|update`，包含 `target_info_unit_id`（仅 update）、当前业务字段中必要的变更及所选 `evidence_ids`。严格使用现行 InfoUnit/Quote Unit 业务约束，禁止生成 `SourceRef`、offset、正式 InfoUnit ID 或新的 Schema 版本。更新时明确哪些字段改变、哪些保持原值；未触及节点不得更改。

## 开发待办

### A. 后端提案生成（先离线）

- [x] 阅读本地最新代码与根目录 `AGENTS.md`，识别现有 CRUD、Quote Unit、LLM 客户端和当前状态；仅列出真正需要修改的文件。
- [x] 实现最小只读上下文获取（现有 InfoUnit、Quote Unit 和必要的消息日期上下文）；不存在的工具能力先复用已有函数，不额外新建数据层。
- [x] 定义严格的 create/update 提案输入输出契约；模型只能提出一条节点的局部操作，不得直接写入 CaseData。
- [x] 生成候选后校验操作类型、目标节点、字段类型、`evidence_ids` 及案件归属；错误返回明确原因，不能将错误提案当正式数据。
- [x] 用户确认时调用现有 CRUD；拒绝因预览后案件已发生变化而覆盖较新状态（采用现有版本/快照检查可行的最小方案）。

### B. 前端接入（A 的离线测试通过后）

- [x] 复用现有灵动岛/输入入口，支持“定向新增”和“修改已选中 InfoUnit”；没有选中目标却要求修改时给出明确提示。
- [x] 显示**预览**：动作、目标节点、拟改字段、原值/新值、证据片段；提供「确认」「取消」。预览阶段不保存。
- [x] 确认后调用现有 CRUD 并刷新 CaseData；取消/失败不改变正式节点或其他画布内容。
- [x] 仅修改本轮必要的 UI/API；不重构画布、卡片、状态管理。

### C. 验收（逐项检查）

- [x] 用 **mock 模型响应**：指令新增遗漏事实 → 生成候选 → 预览 → 确认 → 新节点及真实来源被保存，刷新后仍存在。
- [x] 用 mock：选中现有节点 → 定向修改 → 预览差异 → 确认；该节点 ID 保持不变，未修改字段及其他节点不变。
- [x] 取消、模型输出非法 ID、无效 Quote ID、错误类型/字段、来源不属于当前案件时，正式 CaseData 保持不变。
- [x] 预览期间 CaseData 被其他操作改变时，确认操作被明确拒绝或要求重新生成，不静默覆盖。
- [ ] 浏览器实际测试新增和修改两个交互，检查 Console/Network，并验证证据可见；不能跑浏览器时明确写“未验证”，不以 build 替代。
- [x] 前端构建、相关后端测试及 `git diff --check` 通过；现有手动 CRUD 不回退。
- [ ] **真实 LLM 评测：未授权前保持未勾选**；不得用 mock 结果宣称真实模型效果通过。

## 不做的事项

不做多步 Agent 规划、MCP Server、向量数据库、长期记忆、多节点批量增删改、自动拆分/合并、Agent 自动删除、全案重新 Extraction、自动 Consolidation 优化、新 Gold/Schema 版本、无关重构。**禁止未经单独授权调用付费 LLM。** 不修改 Gold、固定案件材料及历史评测结果。

## 执行与报告规则

1. 按 A → B → C 执行。只在**实际完成且有验证证据**时把 `[ ]` 改为 `[x]`；“代码已写但未测试”不能勾选验收项。
2. 每轮在本文档末尾补一条简短记录：已勾选项目、实际测试命令/结果、未通过项和原因、对应 commit。不要写逐文件流水账或重复设计说明。
3. 优先最小可用实现，复用已有接口；遇到非本轮阻塞问题仅记录，不扩大范围。不能完成的步骤说明障碍，保留未勾选状态。
4. 完成开发后创建一个独立 commit（包含本文件的勾选更新），**不 Push**；无需因本任务修改仓库根目录 `AGENTS.md`。

### 执行记录

- 2026-10-21：完成 A、B，以及 C 中 mock 新增/修改、失败不落盘、过期提案保护和离线回归；真实写入始终由确认接口委托既有 CRUD，提案只携带 `evidence_ids`，SourceRef 继续由后端生成。
  - 必要修改：`backend/app/{api_models,case_service,info_unit_agent,main}.py`、`backend/prompts/info_unit_agent_prompt_v0_3.md`、`backend/tests/test_v03_info_unit_agent.py`、`frontend/src/{api,App}.tsx`、`frontend/src/styles.css` 与本任务文档。
  - 验证：后端全量 112 项通过；Agent/手动 CRUD 定向 10 项通过；案件 A 静态校验通过（17 InfoUnits、7 Relations），评测测试 21 项通过；前端 `npm run build` 通过；`git diff --check` 通过。
  - 未通过/未执行：浏览器控制运行时未提供已安装 Browser 技能要求的专用接口，未进行实际页面、Console/Network 验收，因此对应项保持未勾选；真实 LLM 未获授权且未调用，保持未勾选。
  - 对应 commit：本提交（SHA 见交付回复）。
