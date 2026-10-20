# LogicWeaver

当前后端采用根目录《LogicWeaver｜最小 MVP 数据 Schema v0.2.md》作为唯一业务数据契约。
旧格式案件、旧卡片接口及数据转换层已移除。React 前端尚未迁移，当前不能与新后端完整联调。

## 本轮可用能力

- multipart 上传与追加文件，直接持久化 v0.2 `CaseData`。
- DOCX、PDF、UTF-8 TXT / Markdown 解析；图片和其他格式明确为 `unsupported`，不执行 OCR。
- 原文件保存在 `backend/data/{case_id}/upload_files/`，完整正文保存在 `parsed/{file_id}.txt`。
- `original_path`、`parsed_text_path` 均为案件目录下的相对路径。`case.json` 不复制完整正文。
- 提取输入仅从缓存 TXT 构建；缓存缺失、空白或编码错误均返回错误，不重新解析原文件。
- Extraction 已接通：完整有效 TXT → 全案 LLM 初稿 → 严格校验 → 显式全案合并复核 → 后端来源定位、正式 ID / 时间与去重 → 校验 → 原子保存。
- `InfoUnit`、争点、来源切片、直接关系和视图引用校验；完整定义全部 v0.2 模型。
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

本轮只验证 v0.2，不运行历史模型基线：

```powershell
& .\backend\.venv\Scripts\python.exe -m unittest discover -s backend/tests -p 'test_v02_*.py'
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
| `POST /api/cases` | multipart：必填 `case_title`、一个或多个 `files`、`source_kinds`；201 `{case: CaseData}` |
| `POST /api/cases/{case_id}/files` | multipart：一个或多个 `files`、`source_kinds`；200 `{case: CaseData}` |
| `GET /api/cases` | `{cases: CaseData[], current_case_id}` |
| `GET /api/case` | `{case: CaseData \| null}` |
| `GET /api/cases/{case_id}` | `{case: CaseData}` |
| `POST /api/cases/{case_id}/select` | 设置当前案件 |
| `PATCH /api/cases/{case_id}` | `{case_title}` |
| `PATCH /api/cases/{case_id}/workspace` | 完整 v0.2 `WorkspaceState` |
| `DELETE /api/cases/{case_id}` | 删除指定案件及其文件 |
| `DELETE /api/case` | 删除当前案件；不自动切换其他案件 |
| `GET /api/cases/{case_id}/files/{file_id}/text` | `ParsedDocument`，读取已缓存正文 |
| `GET /api/cases/{case_id}/extraction-input` | `ExtractionInput`；可重复传 `?file_id=...` 选择文件 |
| `POST /api/extraction/run` | `{case_id}` → 完整 `ExtractionOutput`，同时保存到案件 |

未指定文件时构建全部文件的提取输入；存在失败或不支持文件时明确报错。
要继续处理可用文件，应显式选择已解析的文件 ID，不会静默略过失败项。
错误使用 HTTP 4xx / 5xx；业务错误体为 `{detail: {code, message}}`。
旧格式快照返回 409；文件格式不支持通过 `CaseFile.parse_status` 表达；不接受 JSON 上传或全量旧案件 upsert。
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
每次 Extraction 正常执行包含两个明确步骤：先抽取全案初稿并通过结构和精确来源校验，再将初稿与全部原文交给独立的合并复核 Prompt。
第二步针对跨文件重复命题、遗漏和语义粒度复核，逐项记录初稿的保留、修改、合并、拆分或删除决定及最终索引，明确列出遗漏补充。
审阅封装仅供运行日志，业务响应仍为完整 `ExtractionOutput`；不完整或索引不一致的审阅记录直接拒绝。
最终结果仍须通过同样的结构与来源校验；任一步失败均终止，不返回初稿冒充成功。
两步不是隐式重试或错误修复。调用配置与外部端点相同，数据传输包含原始文本及衍生初稿，应在相应授权范围内运行。
后端还会对内容、类型、发言人、角色、事件参与者和争点完全一致的重复节点进行确定性合并，保留来源并去掉重复引用。
后端不使用相似度阈值猜测不同表述是否同一事实；语义最小性仍须通过真实评测审查。

`validation.locate_source_ref` 从缓存 TXT 计算 Unicode 字符位置 `[start,end)`。
找不到引用或存在多个匹配（含重叠匹配）均报错；已有精确位置的引用必须逐字匹配切片。
`validate_extraction` 校验结果为 `ValidationResult[ExtractionOutput]`，不静默丢弃错误条目。
任何条目校验失败时返回全部已发现来源错误，`valid=false`、`data=null`。

模型边界只允许语义字段及 `file_id + quote`；ID、时间、来源类型和偏移不由模型生成。
来源类型直接取 `CaseFile.source_kind`；保存案件时再次检查引文切片及文件来源类型一致性。
模型输出 JSON Schema 通过四个联合分支直接约束一级类型与子类配对；这只是同一个 `InfoUnit` 的模型输出限制，不新增业务实体表。
输入 JSON 不修复、不剥离 Markdown；重复 JSON 键、非法常量、额外字段、错误类型、非法分类或无效争点都明确拒绝。
所有校验通过后才替换 `core_issue / issue_set / info_units`。失败只追加日志，不覆盖已有有效抽取。
模型运行期间案件若发生变化，返回 409，避免覆盖其他请求；已有关系、视图或画布引用会因替换失效时也拒绝提交，不擅自删除下游数据。

每次执行的 `StageRun` 记录模型、Prompt 内容哈希版本、状态和输入/输出快照路径。
`runs/{run_id}.input.json` 保存模型输入及两个提示；`.consolidation.input.json` 记录第二步的完整输入。
`.output.json` 保存两步原始响应、模型 JSON、逐项编辑发现、遗漏清单、物化结果、合并记录、调用次数及失败步骤。
`.stage.json` 独立保存运行索引；若缓存损坏导致 `case.json` 无法合法保存日志索引，接口明确返回审计持久化错误及原始错误代码，独立日志仍可定位。
授权头不写日志，已配置 API Key / Token 在日志中脱敏。

错误代码区分 `llm_call_failed / llm_timeout`、`llm_response_invalid / llm_incomplete / llm_refused`、
`extraction_json_invalid`、`extraction_schema_invalid`、`extraction_source_invalid`、存储错误及并发冲突。
JSON / Schema / 来源错误通常为 422，模型调用失败为 502，缺少配置为 503；错误响应提供 `run_id`。

配置读取进程环境（优先）与根目录 `.env`，按已配置的 Azure / OpenAI / GitHub provider 选择一个明确部署。
Azure 使用 `AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY / AZURE_OPENAI_MODEL`；没有默认模型，也不在失败后切换服务、修复 JSON 或降级结构化输出。
已配置的 Foundry project endpoint 按其资源域名转为 `/openai/v1` 模型推理路径；每个明确步骤调用一次 Chat Completions 并请求严格 JSON Schema，没有自动重试。
实现依据：[OpenAI 结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)、[Azure v1 端点与认证](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle?tabs=key)。

以下新接口已保留入口和错误行为，当前执行均返回 **501 `pipeline_not_implemented`**：

| 接口 | 下一阶段目标 |
| --- | --- |
| `POST /api/relation/run` | `{case_id}` → `RelationInput` / `RelationOutput`，不再生成重复连线 |
| `POST /api/reasoning/run` | `ReasoningInput` → 选择视图、生成 `ViewPayload`、保存 `ReasoningView` |
| `POST /api/law/retrieve` | `LawRetrieveInput` → `LawRetrieveOutput`，引用已核验 `LawEntry` |

没有 Mock、业务兜底、旧格式解析或 LLM 隐式回退。
`RunStatus.fallback` 仅因 v0.2 契约明确定义而保留枚举值；本轮没有使用它的执行分支。
仅 Extraction 初稿与合并复核两个 Prompt 已进入运行路径；关系及其他视图 Prompt 不加载，需下一阶段审核。
当前 Extraction 的技术流程已验证，真实语义质量尚未验收通过：案件 A 最新核心覆盖 10/16（62.5%），
存在必要信息遗漏、同义立场重复和发言人归属问题。
见 [实施文件、93 项测试及真实评测明细](evals/case_a/results/extraction_20261020_v6/review.md)。
本地 `backend/data/laws.json` 已直接整理为 `LawEntry[]`，保留条文、条号、标题、ID 与来源。
可从有效 Gold 确认的核验日期原样保留；其他条目的核验日期为 null，不虚构日期。
React 的类型、状态和三种视图渲染需下一阶段统一改用 `InfoUnit`、`Relation`、`ReasoningView`。
