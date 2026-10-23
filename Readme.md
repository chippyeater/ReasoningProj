# LogicWeaver

当前后端采用 `docs/LogicWeaver_schema_v0.3.md` 及 `backend/app/schemas.py` 作为业务数据契约。
旧卡片接口已移除；旧案件仅通过显式、非持久化迁移读取，语义或引用无法消歧时返回 409。前端通过展示投影消费 v0.3 业务数据。

## 本轮可用能力

- multipart 上传与追加文件，直接持久化 v0.3 `CaseData`。
- DOCX、PDF、UTF-8 TXT / Markdown 解析；图片和其他格式明确为 `unsupported`，不执行 OCR。
- 原文件保存在 `backend/data/{case_id}/upload_files/`，完整正文保存在 `parsed/{file_id}.txt`。
- `original_path`、`parsed_text_path` 均为案件目录下的相对路径。`case.json` 不复制完整正文。
- 提取输入仅从缓存 TXT 构建；缓存缺失、空白或编码错误均返回错误，不重新解析原文件。
- Extraction 已接通：完整有效 TXT → 全案 LLM 初稿 → 逐项校验 → 必要时单次定向修复 → 后端来源定位、正式 ID / 时间与去重 → 完整复验 → 原子保存。
- `InfoUnit`、争点、来源切片、直接关系和视图引用校验；完整定义全部 v0.3 模型。
- 解析日志使用 `StageRun`，输入输出快照保存在案件目录的 `runs/` 下。

DOCX 规范化按文档顺序读取非空段落和表格，去除段落边缘空白，以 LF 分隔段落，表格单元格以 Tab 分隔。
这与案件 A 固定 TXT 的规范化一致。TXT / Markdown 仅移除 UTF-8 BOM 并规范化换行；不截断正文。
PDF 对有可提取文本的页面加入 `[Page N]` 标记。空文件、空白 PDF、损坏文档和非 UTF-8 文本记录为 `failed`。
解析失败的原文件仍会保存，`parsed_text_path=null`，并有明确错误；上传成功不代表每份文件解析成功。
文件读取或磁盘写入失败则中止请求，回滚本次新建文件，保留原案件快照。

## 启动与验证

PowerShell，从仓库根目录执行：

```powershell
& .\backend\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
Set-Location backend
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

验证当前 v0.3 代码和唯一的案件 A Gold v1.8；这些命令不调用真实模型：

```powershell
& .\backend\.venv\Scripts\python.exe -m unittest discover -s backend/tests -p 'test_*.py'
& .\backend\.venv\Scripts\python.exe scripts/sync_versions.py --check
& .\backend\.venv\Scripts\python.exe evals/case_a/tests/check_docx_parsing.py
& .\backend\.venv\Scripts\python.exe evals/case_a/tests/validate_eval.py
& .\backend\.venv\Scripts\python.exe -m unittest discover -s evals/case_a/tests -p 'test_*.py'
```

## HTTP 接口

服务入口：`backend/app/main.py`，默认地址 `http://localhost:8000`，文档 `/docs`。
业务模型在 `schemas.py`，HTTP 响应封装在 `api_models.py`。

| 接口 | 输入 / 输出 |
| --- | --- |
| `GET /health` | 服务状态及 Schema 版本 |
| `POST /api/cases` | JSON `{case_title}` 创建空案件，或 multipart 提交 `case_title`、一个或多个 `files`、`source_kinds`；201 `{case: CaseData}` |
| `POST /api/cases/{case_id}/files` | multipart：一个或多个 `files`、`source_kinds`；200 `{case: CaseData}` |
| `GET /api/cases` | `{cases: CaseData[], current_case_id}` |
| `GET /api/case` | `{case: CaseData \| null}` |
| `GET /api/cases/{case_id}` | `{case: CaseData}` |
| `POST /api/cases/{case_id}/select` | 设置当前案件 |
| `PATCH /api/cases/{case_id}` | `{case_title}` |
| `PATCH /api/cases/{case_id}/workspace` | 完整 v0.3 `WorkspaceState`（旧全案画布） |
| `GET/PATCH /api/cases/{case_id}/issue-workspaces` | 独立 Issue 画布和人工状态，不修改 CaseData |
| `DELETE /api/cases/{case_id}` | 删除指定案件及其文件 |
| `DELETE /api/case` | 删除当前案件；不自动切换其他案件 |
| `GET /api/cases/{case_id}/files/{file_id}/text` | `ParsedDocument`，读取已缓存正文 |
| `GET /api/cases/{case_id}/extraction-input` | `ExtractionInput`；可重复传 `?file_id=...` 选择文件 |
| `POST /api/extraction/run` | `{case_id}` → 完整 `ExtractionOutput`，同时保存到案件 |

未指定文件时构建全部文件的提取输入；存在失败或不支持文件时明确报错。
要继续处理可用文件，应显式选择已解析的文件 ID，不会静默略过失败项。
错误使用 HTTP 4xx / 5xx；业务错误体为 `{detail: {code, message}}`。
旧格式快照仅在确定性迁移安全时以内存投影读取，歧义数据返回 409；文件格式不支持通过 `CaseFile.parse_status` 表达；不接受 JSON 文件上传或全量旧案件 upsert。
旧卡片编辑、`/api/inference/generate`、router、QA 和 interaction 接口已移除，返回 404。

## 上传来源类型与 Extraction

创建案件和追加文件时，multipart 的 `source_kinds` 字段是 JSON 字符串，形如：

```json
{"合同.docx":"document","沟通摘录.docx":"communication","笔录.docx":"testimony"}
```

键按本批原始文件名精确匹配，不依赖表单文件顺序。必须与本批文件一一对应，不能遗漏、额外提供或重复。
同批重复文件名拒绝上传；同名材料可以分批上传，保存后使用各自唯一 `file_id`。
每个 `CaseFile` 必须保存上传方明确提供的 `source_kind`，允许 document / communication / testimony / record / other。
没有默认来源类型，不根据扩展名或文件内容推断，不存在独立登记接口或 sidecar。

`POST /api/extraction/run` 不接受子集参数：一次发送案件全部 `parsed` 文件的完整 TXT。
`failed` / `unsupported` 文件不会进入模型，排除项和原因逐项记录在运行日志；没有有效文件时明确报错。
初稿全部合法时 Extraction 只调用一次模型。存在可定位的候选错误时，仅把失败对象、结构化错误、必要实体依赖和有界 Quote Unit 证据候选发送给 `extraction_validation_repair`，最多增加一次修复调用；旧 Evidence Repair 不再形成第二条运行分支。
修复可以替换失败候选的完整模型字段，但必须保留 `local_id`，且仍须重新通过分类、引用、来源物化和案件完整性校验。首次或修复后合法的节点可以部分接受；未修复节点及依赖失效节点只写入审计，不进入案件。
最终接受集合一次性原子保存；没有安全候选、不可定位的批次错误、模型/网络/存储错误均整阶段失败。已有 InfoUnit 时在建立安全刷新策略前返回 409，不覆盖或删除现有数据。
后端还会对内容、类型、发言人、角色、事件参与者和争点完全一致的重复节点进行确定性合并，保留来源并去掉重复引用。
后端不使用相似度阈值猜测不同表述是否同一事实；语义最小性仍须通过真实评测审查。

`validation.locate_source_ref` 从缓存 TXT 计算 Unicode 字符位置 `[start,end)`。
找不到引用或存在多个匹配（含重叠匹配）均报错；已有精确位置的引用必须逐字匹配切片。
`validate_extraction` 继续作为物化后的完整来源门禁；候选逐项校验会收集 `item_id / field_path / error_code / message`，并将舍弃项连同原因保留在运行审计中。

模型边界只允许批内 `local_id`、语义字段及 `file_id + quote`；正式 ID、时间、来源类型和偏移不由模型生成。
来源类型直接取 `CaseFile.source_kind`；保存案件时再次检查引文切片及文件来源类型一致性。
模型输出 JSON Schema 通过五个联合分支直接约束一级类型与子类配对；初始 event 不允许 `verified`。这只是同一个 `InfoUnit` 的模型输出限制，不新增业务实体表。
输入 JSON 不修复、不剥离 Markdown；重复 JSON 键、非法常量、额外字段、错误类型、非法分类或无效争点都明确拒绝。
所有最终接受节点通过完整复验后，才一次性写入 `core_issue / issue_set / info_units`。失败只追加日志，不覆盖已有有效抽取。
模型运行期间案件若发生变化，返回 409，避免覆盖其他请求；已有关系、视图或画布引用会因替换失效时也拒绝提交，不擅自删除下游数据。

每次执行的 `StageRun` 记录模型、Prompt 数字版本、状态和输入/输出快照路径。
`runs/{run_id}.input.json` 保存模型输入、Extraction Prompt 及 Validation Repair Prompt；发生修复时，最小修复输入另存为 `.validation-repair.input.json`。
`.output.json` 保存初始输出、逐项错误、修复输出与字段差异、复验结果、接受/舍弃数量、物化结果、调用次数及失败步骤。
`.stage.json` 独立保存运行索引；若缓存损坏导致 `case.json` 无法合法保存日志索引，接口明确返回审计持久化错误及原始错误代码，独立日志仍可定位。
授权头不写日志，已配置 API Key / Token 在日志中脱敏。

根目录 `versions.json` 登记稳定文件名 Prompt 的逻辑名称、路径、递增数字版本和内容 SHA-256，
并独立登记当前 Schema `0.3` 与案件 A Gold `1.8`。修改 Prompt 后运行：

```powershell
& .\backend\.venv\Scripts\python.exe scripts/sync_versions.py --check
& .\backend\.venv\Scripts\python.exe scripts/sync_versions.py --sync
```

`--check` 只读检查，`--sync` 仅对内容变化的 Prompt 将数字版本增加 1，并在完整校验后原子更新注册表；重复执行不会再次递增。
新增 Prompt 必须先登记，Schema 与 Gold 版本不会由脚本自动更新。

运行时 `prompt_version` 使用注册表中的可读版本（如 `v1`），`prompt_sha256` 始终按实际加载文本计算。
`prompt_git_commit` 仅在 Prompt 可由 Git 验证时记录其最近 Commit；Prompt 或注册表尚未提交时，
`prompt_release_status=uncommitted`。这些字段与 `prompt_name / schema_version / model` 按每次调用分别写入本地审计和 Langfuse Generation。
StageRun 保持原结构；旧日志中 SHA-256 形式的 `prompt_version` 无需迁移并仍可读取。

## LLM Observability（Langfuse）

后端使用 `langfuse==5.0.4` 的 OpenTelemetry API，为 Extraction、Extraction Validation Repair、Relation、Relation Validation Repair 和
InfoUnit Agent 建立命名 Trace，并将模型调用、解析与校验记录为独立 Observation。默认关闭外部追踪；即使开启，
默认也只发送字段名、长度和 SHA-256 等摘要，不发送 Prompt、案件正文或模型原始输出。现有 `runs/*.json` 审计文件与路径保持不变。
默认隐私模式下，云端 Trace 名称中的案件简称也会替换为案件 ID 的短哈希；本地 Run 索引仍显示真实案件简称。

在根目录 `.env` 或进程环境中配置：

```dotenv
LOGICWEAVER_OBSERVABILITY_ENABLED=true
LOGICWEAVER_OBSERVABILITY_CAPTURE_CONTENT=false
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_TRACING_ENVIRONMENT=development
```

只有在案件数据已获外发授权时，才将 `LOGICWEAVER_OBSERVABILITY_CAPTURE_CONTENT` 改为 `true`；密钥仍会脱敏。
追踪初始化、写入或刷新失败只记录后端警告，不中断业务与本地 JSON 审计。「开始提取」为 Extraction 与 Relation
传递同一个 `workflow_id`，Langfuse 中表现为相同 Session；每个阶段保留独立 Trace 与可读名称。

启动服务并完成一次 Mock 或获授权的调用后，登录 `LANGFUSE_BASE_URL`，进入对应项目的 Tracing/Traces，按
Session（`workflow_id`）、Trace 名称或 `logicweaver` 标签筛选即可。SDK 与接口依据：
[Langfuse Python instrumentation](https://langfuse.com/docs/observability/sdk/instrumentation)、
[Python v4 → v5 migration](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v4-to-v5)。

本地运行索引不访问 Langfuse，可按案件、阶段、状态和 ISO 时间筛选并输出对应 JSON 绝对路径：

```powershell
Set-Location backend
& .\.venv\Scripts\python.exe -m app.run_index --case 租赁 --stage extraction --status success --since 2026-10-22
```

错误代码区分 `llm_call_failed / llm_timeout`、`llm_response_invalid / llm_incomplete / llm_refused`、
`extraction_json_invalid`、`extraction_schema_invalid`、`extraction_source_invalid`、存储错误及并发冲突。
JSON / Schema / 来源错误通常为 422，模型调用失败为 502，缺少配置为 503；错误响应提供 `run_id`。

配置读取进程环境（优先）与根目录 `.env`，按已配置的 Azure / OpenAI / GitHub provider 选择一个明确部署。
Azure 使用 `AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY / AZURE_OPENAI_MODEL`；没有默认模型，也不在失败后切换服务、修复 JSON 或降级结构化输出。
已配置的 Foundry project endpoint 按其资源域名转为 `/openai/v1` 模型推理路径；每个明确步骤调用一次 Chat Completions 并请求严格 JSON Schema，没有自动重试。
实现依据：[OpenAI 结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)、[Azure v1 端点与认证](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle?tabs=key)。

Relation 已接通为增量生成：`POST /api/relation/run` 接收 `{case_id}` 进行全案检查，或额外传入至少两个
`selected_info_unit_ids` 仅检查选中节点；响应返回 `status`（`empty / success / partial_success`）、本次 `added_relations` 和 `discarded_count`。关系逐条校验，仅把失败关系及必要作用域摘要发送给单次 Relation Validation Repair；合法关系经完整复验后统一分配正式 ID 并原子追加，等价关系不会重复追加。

自然语言入口统一为画布左下角的“Agent 调查”快捷入口，以及右键菜单首项“向 Agent 提问”唤起的同一灵动岛，调用 `POST /api/cases/{case_id}/agent/run`。
顶部 Issue 标签导航独立画布，状态由用户修改并持久化；切换不重载案件。各画布初次按 `related_issue` 推荐节点，可手动加入全案其他信息，位置、节点集合、视口与选择保存到案件目录的 `issue_workspaces.json`；业务数据与旧 `workspace_state` 不复制、不改写。原工具栏操作保留在右键菜单中。
当前仅支持只读调查，结果与案件数据分离；发现、依据原文、未解决问题及任务状态可在输入框下查看，节点按钮可定位画布。
旧 InfoUnit Agent 提案/确认后端能力保留供后续复用，前端不再以其作为自然语言入口；手动编辑与画布功能保留。
运行边界和离线端到端验收见 [Investigation Agent 文档](docs/investigation_agent.md)。
`POST /api/extraction/run` 与 `POST /api/relation/run` 均可接收可选 `workflow_id`，用于关联一次用户工作流，不进入模型输入。
单条手动关系通过 `/api/cases/{case_id}/relations` 的 POST，以及
`/api/cases/{case_id}/relations/{relation_id}` 的 PATCH / DELETE 维护；读取继续复用案件接口。

以下执行入口仍返回 **501 `pipeline_not_implemented`**：

| 接口 | 下一阶段目标 |
| --- | --- |
| `POST /api/reasoning/run` | `ReasoningInput` → 选择视图、生成 `ViewPayload`、保存 `ReasoningView` |
| `POST /api/law/retrieve` | `LawRetrieveInput` → `LawRetrieveOutput`，引用已核验 `LawEntry` |

没有 Mock、业务兜底或 LLM 隐式回退。
`RunStatus.fallback` 仅因 v0.3 契约明确定义而保留枚举值；当前没有使用它的执行分支。
Extraction、两阶段 Validation Repair、Relation 与 InfoUnit Agent Prompt 已进入各自运行路径；4 份 Reasoning 视图 Prompt 已纳入版本注册表但尚未接通。
当前 Extraction 的技术流程已通过离线契约测试；尚未针对 Gold v1.8 运行新的真实模型评测。
历史 v7.2 结果仍按 Gold v1.7 记录为整批拒绝，11/17 只属于诊断对齐，不是成功率。
本地 `backend/data/laws.json` 已直接整理为 `LawEntry[]`，保留条文、条号、标题、ID 与来源。
可从有效 Gold 确认的核验日期原样保留；其他条目的核验日期为 null，不虚构日期。
React 类型、兼容展示投影和三种视图渲染已统一读取 `InfoUnit`、`Relation`、`ReasoningView`；业务权威数据仍由后端 v0.3 Schema 定义。
