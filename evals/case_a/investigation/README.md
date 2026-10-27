# Case A Investigation × Langfuse v5

使用原生 Dataset、`dataset.run_experiment`、Experiment Trace 和 Scores。没有新调查运行时、评分前端或 LLM-as-a-Judge；运行直接使用当前 Production Prompt 与完成规则；Schema v0.3 和原 Extraction/Relation Gold 保持不变。

## 三项任务及参考标准

完整 Investigation Gold 位于 [dataset.json](dataset.json)：每项包含调查目标、必要证据类别、可选线索、可接受结论边界、关键错误、来源访问组和评分依据。不要求唯一措辞、固定工具顺序、Proposal 数量或总分阈值。

| 任务 | 调查重点 | 关键结论边界 |
| --- | --- | --- |
| A1 实际损失及量化 | 对立损失主张、搬离/入住来源、计租与收款条件、合同月租 | 合同已记载月租 3000 元；不能把日期相差三天或一条公式当作已确认实际损失。缺少新租约、计租/收款条件时保留未解决方面 |
| A2 提前退租与押金 | 合同条款、拒退理由、提前搬离、条件式协商及后续交涉 | 条款存在不等于已认定法律效力；可协商部分退还不等于无条件承诺或最终退款协议 |
| A3 过程还原 | 聊天、双方笔录、日期来源、替租入住/另签租约、押金交涉 | 聊天日期不能自动等同事件日期；新租约签署日和最终退款结果未提供，不能补写 |

`snapshot.json` 固定已有材料的 SHA256，不复制案件原文。数据来自当前 Case A 的 5 份 cached TXT、17 个 Gold InfoUnit、7 条 Gold Relation 与既有争点文件。每次执行先校验文件哈希，并构造时间戳固定、无 StageRun/人工推理/工作区历史的 `CaseData`。三个任务使用相同 core_issue、空选择、空推理初态，各自获得新的临时目录；只允许写入本次 Agent 审计。无原始上传文件解析、提取或 Relation 模型调用。

历史 A1 Trace 是此前另一份实际提取快照，InfoUnit ID 与固定 Gold 不同。它是失败案例来源，不是该固定快照上的新实验或可直接合并的访问覆盖基线。

当前执行使用独立的 `execution_context.json`（`case-a-task-issues-v2`）作为可追踪运行配置：A1 的 `current_issue/issue_key` 选择已有争点“出租人是否存在实际经济损失”；A2/A3 保持押金工作区。`goal` 保留原始任务，`core_issue` 保留案件整体背景；生产 API 的 current_issue 仍来自用户选中的合法工作区，不按 Goal 猜测替换。原 Dataset、初态、Gold、快照和历史 Trace 不改写。Experiment 元数据记录 execution_context_version/SHA-256；比较新旧运行时须同时比较该上下文配置，不能将其冒充完全相同输入的 Prompt 单变量实验。

## 已初始化的远端对象

- Dataset：`logicweaver/investigation/case-a-v1`
- ID：`cmvmadrn2015had0e63z1810r`
- [Dataset 页面](https://us.cloud.langfuse.com/project/cmvj1510102mcad0ee6m4fm63/datasets/cmvmadrn2015had0e63z1810r)
- A1 `source_trace_id`：`7a661b732c21ac28903cff5155755943`；`source_observation_id`：`a7938ff395f644e1`。使用 SDK 正式字段建立来源关系，不给历史 Trace 伪造 Experiment 属性。

人工 Score Config 均为 NUMERIC，范围 0–2：

| 名称 | Config ID | 0 / 1 / 2 |
| --- | --- | --- |
| task_coverage | 25924700-cfb2-456e-ac56-5e2b0271134e | 主要目标未覆盖 / 部分覆盖 / 关键目标合理处理 |
| evidence_support | f4799f7e-31a5-4af5-869d-12a37afafc1e | 重要结论无支持或误用 / 部分支持 / 结论与来源关系合理 |
| gap_handling | 54d069a7-212b-4253-8f50-4b10a6ee3c21 | 重要缺口遗漏或当作事实 / 已识别但处理不足 / 合理调查或记录 |
| completion_calibration | 63d36db7-0119-4873-8d45-d16ff100c270 | 明显提前/错误完成 / 基本合理但局部不一致 / 与实际完成程度一致 |

在 Trace/Observation 详情点击 **Annotate**，选择以上配置、给出整数 0/1/2，并在 Comment 写明 Finding、来源指针或模型/工具/Question 记录的依据。新实验执行后，也可从 Experiment compare view 打开对应任务和原生 Trace 评分。UI 的评论功能可用，但后端配置不会强制人工必须填写评论；这是标注规范。Runner 不自动预填人工分，也不设置未经验证的总分通过线。经明确授权的 Codex 审阅可使用原生 ANNOTATION 预标注，必须记录 `reviewer=Codex`、`human_verified=false`，供真人复核；不能冒充已完成真人标注。当前不额外建立 Annotation Queue。

## 命令

在仓库根目录，使用现有 venv：

```powershell
# 默认操作，无远端写入、无真实模型调用
backend/.venv/Scripts/python.exe scripts/run_investigation_eval.py
backend/.venv/Scripts/python.exe scripts/run_investigation_eval.py dry-run
backend/.venv/Scripts/python.exe scripts/run_investigation_eval.py dry-run --task A1

# 显式初始化 Dataset / 3 个 Item / 4 个 Score Config，不运行模型或 Experiment
backend/.venv/Scripts/python.exe scripts/run_investigation_eval.py init

# 为历史 A1 附加确定性执行指标，不运行模型、不创建新 Experiment
backend/.venv/Scripts/python.exe scripts/run_investigation_eval.py score-history

# 仅在明确接受付费执行后使用
backend/.venv/Scripts/python.exe scripts/run_investigation_eval.py live --allow-paid-models

# 仅执行 A1 一次，不运行 A2/A3
backend/.venv/Scripts/python.exe scripts/run_investigation_eval.py live --task A1 --allow-paid-models
```

`--task A1|A2|A3` 仅用于 dry-run/live。省略时仍运行三个任务；指定时先核对完整远端 Dataset，再仅在本地筛选原生 Item，不修改 Dataset。运行元数据记录选择范围及本次调用上界，单任务最多 28 次模型调用。Runner 不自动重跑任务；远端核验失败时，可从审计目录的 `execution.json` 找到本次 Experiment/Trace ID，继续只读核查，避免为同步问题重新调用模型。

`init` 使用确定性 Dataset Item ID；已存在且一致的对象不重建。定义、快照、配置或远端 Item 漂移时明确失败，不静默覆盖。远端 unavailable、权限不足、写入拒绝或读回核验失败均返回非零退出码，不冒充同步成功。`live` 缺少授权标志会在加载模型或执行调查前失败；还要求干净且已提交的工作区和开启 Langfuse 内容记录。

当前 v18 的离线协议脚本先提交 Question/Step 计划，再读取来源并完成交付（每项3次 Mock 模型调用）。运行元数据包含两阶段 Prompt manifest 与两组工具契约的整体哈希；不读取旧单文件 Prompt。它只验证阶段切换、来源访问、步骤依据和完成协议，不能代替三项任务的真实语义验收。

`dry-run` 使用内存 Span Exporter 和只接受 Mock API 的 httpx transport。它实际运行 SDK v5 Experiment Runner、三个隔离任务、完整 Agent 工具循环、原生 Evaluator 和 Trace 嵌套；模型回复为明确标注的协议样例。报告中的完成状态只说明协议通过，不是真实调查质量或真实 Experiment 上传成功。

首轮 `max_concurrency=1`。独立 CLI 进程临时切换 `case_store.DATA_DIR`，不能嵌入正在处理其他案件请求的服务器进程。每项实际调用完整 `run_investigation`，不把 Gold 传入模型、不改工具、预算或结束规则。原生 Experiment 根下保留 Agent chain、model/tool 调用（包括校验错误反馈）和 final working state。

每个新实验记录 Dataset 内容版本及服务器版本时间、任务 ID、CaseData/材料快照/初态哈希、配置模型、Prompt version/SHA/最后修改 Commit、**独立代码 HEAD Commit**、Schema、工具契约哈希和 SDK 版本。真实返回的模型快照、Token 明细和原始输入输出继续由既有 Agent 审计与 generation observation 保留。

live 的审计保存在被 Git 忽略的 `evals/case_a/results/investigation/<run>/<A1|A2|A3>/runs/`。Trace/Score 上传后，通过 v2 Observations、原生 Experiments 和 Scores 读取核对关联，失败时保留审计并报错；最多核验 5 次，间隔 1/2/4/8 秒，仅等待服务写入可见性，不重试模型。每次新实验使用独立运行名和自己的 Trace ID。

Experiments 查询使用服务器支持的 `limit=100` 与 cursor 分页，并请求 `core,dataset` 字段组以读取 Dataset ID；返回的 Item/Trace/Dataset 关联集合必须与本次选择完全一致，额外任务也会导致核验失败。Scores 读取显式请求 `details,subject`，供核对评论、配置、审阅来源及关联对象。核验故障可仅修复读取逻辑后使用本次 `execution.json` 中的 ID 继续查询，不重写原始运行记录或重新执行付费调查。

当前 Agent 每项最多 **28 次模型调用**，三项最多 **84 次**；每次最多 `LLM_MAX_COMPLETION_TOKENS`（当前默认 20,000），理论 completion 上界 1,680,000，含 reasoning tokens。模型客户端没有自动重试；提前结束通常显著少于此上界。输入量、Azure 实际单价及总费用不由此上界保证。

## 自动 Scores 的解释

自动报告：`runtime_finish_validated`、运行失败、工具错误数（含请求/完成校验错误）、工具尝试数、finish 尝试/失败、no_progress_limit、显式读取的 InfoUnit/SourceRef 数量、上下文提供的来源范围、模型调用数、耗时及 Token 使用量。缺失审计时标记 `audit_available=0`，不伪造未知的调用数或访问数；缺失 Token usage 时标记 `token_usage_complete=0` 并省略无法确认的总量。reasoning tokens 已包含在 completion tokens 中，不重复累计。

每个 `evidence_access_<group>` 只判断组内 InfoUnit 是否至少有一条来源被上下文提供或成功读取，按 Gold 的 all/any 组合。它不保证该节点所有来源均已提供、不保证关键措辞已读到，更不代表理解、引用支持或任务完成。实际 SourceRef 指针和只读摘要范围同时保存在任务 output，人工据此复核。必要/可选组只是参考标准；访问分没有语义通过阈值，所有 opportunities 不构成必读清单。

`score-history` 仅为原 Trace 添加上述执行/资源指标。由于历史快照不同，省略固定 Gold 访问组指标；不自动给四个人工语义维度打分。此次已写入并读回核验 19 个历史执行 Scores：3 次模型调用、1 次成功 finish、2 个显式读取 InfoUnit / 6 个 SourceRef、29,413 total tokens（其中 completion 2,751，reasoning 1,728）。`runtime_finish_validated=1` 仅说明当时 Runtime 接受结束，不能抵消 A1 的历史语义失败。

## 验证与官方接口依据

```powershell
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -p 'test_investigation_eval.py' -v
```

初始集成验证：集成测试 21 项、后端全量 308 项、Case A 原有回归 21 项均通过；独立 dry-run 完成三项协议检查（30 个内存观察节点、0 次真实模型调用）。Dataset 重复初始化返回一致对象；历史 19 个自动 Scores 已从远端读回。单任务参数及远端读取修复后，集成回归 25 项、Case A 原有回归 21 项通过，A1 dry-run 仅运行一个任务（10 个内存观察节点、0 次真实模型调用）。Prompt 版本同步检查通过。

- [SDK v5 原生 Experiment Runner](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk)
- [Dataset 与 source_trace_id](https://langfuse.com/docs/evaluation/experiments/data-model)
- [Score Config](https://langfuse.com/faq/all/manage-score-configs)
- [UI 人工评分与评论](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-ui)
- [v2 读取与原生 Experiment 查询](https://langfuse.com/docs/api-and-data-platform/features/public-api)

Mock 不作为质量验收结论。真实模型质量和远端 Experiment 摄取须通过明确授权的 live 运行逐次验证，具体结果保留在被忽略的运行审计与 Langfuse 中。
