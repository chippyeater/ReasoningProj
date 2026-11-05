# Issue 协作推理 MVP

实现依据为用户任务 `tasks/10231658.md`。可勾选验收清单只保留本地 `tasks/10231658-checklist.md`，不纳入 Git。

## 使用闭环

在 Issue 画布右键“新建假设”，保存内容、证据判断（未证实/已证实/已证伪）与工作状态（活跃/已弃用）。弃用只降低视觉优先级，可以恢复。节点“查看与编辑”或右键进入编辑器。

从假设编辑器或右键“关联已有证据”，选择全案 InfoUnit，填写支持/反对/相关语义与理由。支持为绿色实线，反对为红色虚线，相关为灰色虚线；点击连线可修改或解除。来源原文在关联编辑器按需展开，可定位原信息。默认 related（相关）表示材料与假设有关，但不明确支持或反对。人工/Agent 写入的来源独立于关联语义，用户修改保留来源并标明人工修正。

“新建证据缺口”或假设中的“添加证据缺口”支持关联多个假设、可选缺口形成依据和未解决/已解决状态。“用于回应缺口的信息”与形成依据分开管理：先保存缺口，再通过“添加回应材料”选择当前案件 InfoUnit；标题可定位材料，“移除关联”仅解除当前缺口的关联，不删除案件信息或来源。编辑器保存时发送新增与移除差集；未保存的回应修改会禁用“请 Agent 检查”。回应变化不自动改变缺口状态。缺口是问题，不是虚构的材料。正式节点复用当前画布选择、拖动、缩放、坐标与 Issue 恢复机制；新节点寻找空位，不重排已有节点。

保存用户修正后可“请 Agent 检查”，从现有灵动岛发送。提案即时校验后暂存，正常 Finish 后自动整体提交；界面展示实际变更和当时的依据，可查看、编辑、删除或整轮撤销/恢复，不再逐项采纳。变更记录随工作区持久化，关闭结果或刷新后仍可查看。

## 数据与接口

沿用 `issue_workspaces.json`，每个 Issue 增加默认空的 `reasoning`：

- `hypotheses`：稳定 ID、内容、判断、工作状态、修订号、最近修改时间/来源、可选采纳凭据。
- `gaps`：稳定 ID、问题、关联假设 IDs、`basis_info_unit_ids`（提出缺口的已有信息）、`addressing_info_unit_ids`（创建后累积的回应材料）、解决状态和修订元数据。两类引用独立，不自动复制；同一 InfoUnit 可经明确操作分别关联两种角色。
- `links`：稳定 ID、InfoUnit 与 Hypothesis IDs、语义、理由、人工/Agent 来源及最近修改者。
- `accepted_proposals`：服务端运行与提案到正式对象的幂等映射。
- `investigation_runs`：实际变更、原提案、来源快照及整轮撤销/恢复状态。

所属 Issue 由工作区父键确定。业务 InfoUnits、Relations、SourceRefs 不复制、不修改；`HypothesisView` 等既有生成式视图不承担这些可编辑工作对象。旧文件缺少 `reasoning` 时读作空集合，旧坐标与字段保留。引用了某个 InfoUnit 的正式推理关联/缺口会阻止删除该共享信息，避免断开引用。

`PATCH /api/cases/{case_id}/issue-reasoning` 每次接受一个人工操作：`hypothesis`、`gap`、`link` 或 `unlink_id`，绑定 `issue`，可附新节点 `position`。后端分配 ID、修订号和来源，校验所有引用、当前 Issue 归属及重复关联，并通过现有原子 JSON 写入统一保存。Gap 创建时回应列表为空，非空回应只能追加到已有 Gap；编辑时传入 `addressing_info_unit_ids` 与原值按首次出现顺序合并去重，空列表也不会清空旧关联。省略 `basis_info_unit_ids` 或 `status` 时保留原值；显式传入依据列表可替换形成依据，显式传入状态才改变解决状态。追加回应不会改变 Hypothesis 的 judgment。两类引用都校验当前 Case ID 范围，都阻止删除被引用的 InfoUnit，引用类型分别为 `evidence_gap_basis` / `evidence_gap_addressing`。

`POST /api/cases/{case_id}/investigation-changes/reverse` 接收 `issue/run_id/restore`，默认撤销，restore=true 恢复；重复执行幂等。`POST /api/cases/{case_id}/investigation-changes/retry` 可重试正常 Finish 后失败的整轮写入，不调用模型、不更新运行起点、不改写原审计；预算耗尽及异常中止的运行不可重试提交。旧逐项采纳接口已移除。变更涉及的对象须与运行前/后快照一致，且不能破坏后续依赖；否则整轮拒绝并返回409，保护用户后续编辑。撤销保留运行记录和对象映射，不会让重复执行重新创建已删除对象。

## Agent 边界

保留所有现有 Proposal 工具。每次校验参数、当前案件/议题对象、实际读取的来源与本轮依赖，并在独立副本复用正式编辑逻辑验证可应用性；失败返回具体字段和修复说明，成功只暂存。新 Hypothesis 的返回 proposal_id 可供本轮 Gap/Link 引用，正常 Finish 时统一分配正式 ID。状态提案不改 content，判断材料不自动创建 EvidenceLink。已有 Link 按 Hypothesis/InfoUnit 对定位，更新语义时保留 ID。

正常 Finish 后，对照运行开始时的完整服务端推理快照及材料哈希，重新验证来源的 Unicode TXT 切片，再在副本应用全部提案；最后一次原子替换保存正式对象、画布、映射和 `investigation_runs`。同轮编辑不再互相检验中间 revision。人工编辑、画布更新、案件快照写入及整轮提交/撤销共享工作区锁；并发推理或相关材料变化返回冲突，不自动覆盖。

`investigation_runs` 保存实际变化对象的 before/after、产生变化的提案键、原提案及当时引用的 SourceRef。它复用现有推理对象模型，不修改 CaseData/Gold 版本，也不复制案件材料为另一份业务数据。旧 `accepted_proposals` 字段保留稳定映射；操作来源沿用现有 agent_accepted 标记，界面显示“Agent 写入”。完整服务端快照与运行记录不进入模型上下文。

无实际变化的 Hypothesis/Gap/Link 操作成功返回 no_change，不追加提案、写工作区或递增 revision。Gap 更新只追加去重 `addressing_info_unit_ids`，保留原 ID、content、形成依据和完整 Hypothesis 关联；部分回应可保持 open。引用必须指向本次回应材料，错误应修正原更新，不另建同问题 Gap。Hypothesis 的证据判断独立于 Gap 状态。

异常、预算耗尽、并发冲突和提交失败整轮不写入；合法提案、工具错误、运行起点和提交状态保存在本地审计供检查。工作区原子运行记录是实际落盘的凭据，即使提交后的审计输出刷新失败也可查验。系统不自动重试付费调用，不提供历史 Proposal 的隐式采纳。

上下文仍为有界投影：按需发现被省略的 Gap，引用 ID 不授予材料读取权限。Agent 自动写入仅限当前 Investigation 的内部推理对象，原始文件、InfoUnits、Relations 和外部操作不在范围内。来源可定位不等于语义充分支持，更不代表单方陈述已被证实。

## 已保存 Gap 的一次性迁移

工作区没有独立 Schema 版本注册；本次不修改 Extraction/CaseData Schema 版本。生产模型只接受拆分后的字段。迁移脚本先验证整份新工作区及当前案件 InfoUnit 引用，保留其他字段、稳定 ID、状态、修订、画布和采纳记录原值；`--apply` 前保存逐字节原文件备份 `issue_workspaces.json.before-gap-reference-split.bak`，通过现有原子写入保存。重复执行不再改写，混合新旧字段或备份冲突明确失败。应在停止该工作区写入时执行，脚本不会迁移历史运行审计。

```powershell
backend/.venv/Scripts/python.exe scripts/migrate_gap_references.py backend/data/<case_id>/issue_workspaces.json
backend/.venv/Scripts/python.exe scripts/migrate_gap_references.py backend/data/<case_id>/issue_workspaces.json --apply
```

旧 Gap 的 `info_unit_ids` 原样转为 `basis_info_unit_ids`，`addressing_info_unit_ids` 初始化为空；无运行时别名或旧字段 fallback。

所有模型输入、工具返回及最终建议沿用现有审计与 Langfuse 机制。错误按 JSON 解析、结构、Proposal 语义及引用分类记录字段路径和错误码；即使追踪不采集内容，也保留不含案件文本的错误位置及类别。调查阶段只写独立运行审计；正式工作区只能由人工编辑或明确采纳接口写入。

## 离线验收

后端使用隔离案件与 Mock 模型传输，重点覆盖 `test_investigation_commit.py`、`test_gap_update_proposals.py`、`test_reasoning_proposals.py` 和现有上下文/循环/编辑回归。前端组件测试使用真实 TSX；可见浏览器使用隔离的 `serve_agent_smoke.py` 后端，验证首轮自动提交、统一上传补证、编辑和撤销/恢复。历史审计、固定评测及 Gold 不改写，不调用真实付费模型。
