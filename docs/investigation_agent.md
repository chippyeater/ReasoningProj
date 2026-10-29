# Investigation Agent：自主调查与本轮工作记忆

入口为 `run_investigation(case, goal, selected_info_unit_ids=None, work_context=None, issue_key=None)` 和 `POST /api/cases/{case_id}/agent/run`。运行绑定当前案件的深拷贝；正式对象仍由用户逐项采纳 Proposal 后写入。

## 执行和交付

只有一个工具调用循环。首轮即提供 `search_info_units`、`get_relations`、`get_evidence`、四个 `propose_*` 工具及 `finish_investigation`，原生 `tool_choice=auto`。没有规划阶段、Question/InvestigationStep 模型、更新工具或关联参数。读取范围、工具顺序、Proposal 数量和结束时机由 Goal 与读取结果决定。

通过 finish 交付 `answer`、`answer_basis`、可选内容的 `findings`、`open_questions` 和 `completion_status`。空 Findings/Proposal 合法。answer_basis 为 general（能力/通用回答）、case_metadata（可靠案件统计）或 case_material（案件判断）；它描述答复依据，不创建另一条执行流程。涉及案件依据的成果必须选择 case_material。目录不授予材料或引用权限；尚无材料/有效读取结果时，案件判断返回可恢复错误。实际 empty、读取 error、空案件或预算限制可以支持明确的证据不足说明，不能证明事实。后端不以关键词猜测 Goal，也不能独立证明自然语言任务已经充分覆盖。

Findings、Proposal 引用须属于当前案件并实际取得；初始选中/画布材料提供的来源片段继续由缓存 TXT 核验。证据不足须列明未解条件；completed 不可同时宣称 Goal 仍有未解条件。答复保留原文、Markdown，长度上限与工具 Schema 一致为1000字符；非法答复不截断、不发布，成功 Proposal 不因之后模型失败而丢失。

保留8次读取、6次 Proposal、3次 finish 尝试的独立预算，最多20次模型调用；重复无进展最多3次。预算与完整活动只由后端记录，耗尽的工具从下次 Schema 移除。连续非法调用/重复读取、模型失败和审计写入失败均有显式结束或异常，不自动重试付费请求。普通文本不能冒充已验证交付，下一轮提示通过 finish 提交。

## 每轮上下文

只发送 System Prompt 与一个动态 JSON User Message，不重复附带上一轮 Assistant/Tool 消息。动态字段：

- `goal`、`current_issue`、必要案件背景 `core_issue`。
- `case_info_directory`：总数、稳定 ID、标题、类型、截断及省略数量；按 ID 确定排序，最多80项、条目累计6000字符，标题最多120字符。
- `materials.info_units/relations/source_refs`：本轮实际取得的去重工具投影，各自带 `items/context_truncated/omitted_ids`。
- `reasoning`：当前 Issue 中相关的正式假设、缺口和证据关联，保留用户判断、弃用/解决状态，不发送修订号、时间戳和显示布局。
- `proposals`：本轮成功提交的完整提案，保留实际返回 ID 和依赖。
- `notices` 与 `recovery`：必要的空结果、截断范围、来源限制和当前错误。

KnowledgeState 按 InfoUnit ID、Relation ID、SourceRef 的节点/索引合并材料，完整状态不随上下文裁剪而删除。每轮从该状态重建视图；正文、关系和来源分别最多约6500/3500/7500字符，总上下文最多24000字符。优先保留本次明确读取的材料、Goal 文本相关材料和近期材料；超限明确列出省略 ID，模型可重新读取，缓存结果也重新进入视图。不发送完整工具历史、活动索引、计划状态、预算计数或运行审计。

## Prompt 和可观测性

当前唯一源文件是 `backend/prompts/investigation_common_prompt.md`，稳定注册名 `investigation_common`；使用所有 Agent 共用的 `load_prompt`。阶段 Prompt、组合注册、组合加载接口和对应测试已移除。`sync_versions.py --sync` 只递增变化文件版本，`--check` 校验实际内容 SHA；业务 Schema 与 Gold 版本不变。Git 及冻结历史运行保留原样。

输入审计保存实际 System Prompt 和工具 Schema；每次模型调用与 Langfuse Generation 使用相同名称、版本、SHA、可验证 Commit、Schema、模型和发布状态。工具参数/结果、预算、读取统计和最终 KnowledgeState 在本地审计与对应 Trace 中保留。用户数据不进入 System 指令。

## 离线验证

```powershell
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -q
backend/.venv/Scripts/python.exe -m unittest discover -s evals/case_a/tests -p 'test_*.py' -q
backend/.venv/Scripts/python.exe evals/case_a/tests/validate_eval.py
backend/.venv/Scripts/python.exe scripts/run_investigation_eval.py dry-run
backend/.venv/Scripts/python.exe scripts/sync_versions.py --sync
backend/.venv/Scripts/python.exe scripts/sync_versions.py --check
npm --prefix frontend run build
node --experimental-strip-types --test frontend/tests/investigation-presentation.test.mjs frontend/tests/investigation-summary.test.mjs
git diff --check
```

测试覆盖不建计划的事实检索、两个竞争性假设及共同缺口、跨轮引用、去重/省略/重新读取、非法来源、empty/truncated/error、预算、Proposal 部分成功和独立审批、Issue 隔离与 API。Case A 固定 Dataset/Gold/历史 Trace 不改写；Mock 不构成语义质量验收，真实模型行为需另行授权评测。

本地 UI smoke 可用 `backend/.venv/Scripts/python.exe backend/tests/serve_agent_smoke.py` 启动隔离后端，并在 frontend 运行 `npm run dev -- --host 127.0.0.1`。它只模拟模型边界，不访问外部模型；案件修改与审批只写临时目录。
