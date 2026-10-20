# 案件 A Gold v1.7 依赖检查

本轮接受开始时的正式 17 条 Gold，不重新设计标准答案。所有 11 个 `gold/` 文件均与本轮开始时逐字节一致；引用原文、偏移、固定 TXT 和原始 DOCX 未改动。本轮只修补评测依赖，没有修改业务代码、Prompt、预测或历史记录，没有调用 LLM、重跑历史语义评测或提交 Git。

任务导向原则的实质变化是：拆分依据是核心争点下的独立核查或推理价值。U008 的两种互斥退款规则、U019 的整体拒退立场、U035 的多时点空置说法均可作为一个有用节点。U015 不再要求模糊后续回复，U016 聚焦已找到接租者。U026 仅表示入住，新增 U027 独立表示后来另签租约且日期未知；U029 与 U030 分别保留损失判断和押金处理诉求。上述内容已在用户提供的正式数据中生效，本轮没有再次改写它们。

检查了开始时 `evals/case_a/` 下全部 345 个文件：37 个当前数据、源码和说明文件，301 个历史结果文件，7 个已有字节码缓存。历史结构化文件均可解析；字节码只做文件哈希及非运行依赖确认。完整逐文件状态、检查方式及原因见 [file_inventory.json](file_inventory.json)，开始时的哈希见 [before_hashes.json](before_hashes.json)。

发现并处理的遗漏如下。

| 依赖问题 | 处理 |
| --- | --- |
| 评分器原本动态读取 Gold，但允许缺少部分 Gold 的旧对齐，也没有 Gold 版本证据 | 每个 Gold 必须恰好一行，要求当前版本和依据哈希；只接受完成复核的 yes/no 与非空说明，旧 16 条对齐不能直接运行 |
| 对齐中同一预测可多次填写，需要保留独立信息的计分边界 | 继续严格执行一对一；修改原错误提示中“人工拆分后可重复匹配”的措辞，禁止为评分拆分或改写预测 |
| 评分、真实评测脚本未统一记录当前 Gold 版本 | 新增 Gold 依据指纹，包含正式 Gold、固定 TXT 和来源 manifest 的字节哈希；新评分及未来运行报告记录依据和输入文件哈希 |
| 缺少历史评分的明确版本索引 | 新增独立的历史索引，原输出、审计、评分和报告保持原样 |
| 静态校验对任务嵌套输入及对齐模板的覆盖不完整 | 递归检查当前任务 InfoUnit、Relation 和来源文件引用；检查模板完整性、版本及哈希、说明文件数量和版本 |
| 预测来源偏移检查允许 Python 负索引和 bool 作为 int 的边界漏洞 | 明确要求非 bool 整数且 0 <= start < end <= 原文长度；不改变原文和引用 |
| 现有命令可能覆盖已有评分或复用运行目录 | 评分输出必须是新文件，真实运行输出目录必须为空 |

没有发现当前关系、视图、端到端任务仍使用 U026 的旧“入住加签约”语义或漏掉 U027 的必要更新；U027 已在完整时间线中以未知日期独立引用，并进入主要端到端任务。现有 9 条关系是选择性参考，不要求因增加一个节点而凭空新增关系。法律检索任务没有依赖 U026 的旧签约语义，不需要修改法律 Gold。现有 `test_eval_data.py` 的 17 条断言是本案件参考数据完整性检查，不是模型输出数量约束，予以保留。

历史目录中的旧 16 条统计、旧 U015/U026 语义和偏重语义原子拆分的报告保留为历史事实；不据此评价 v1.7。索引为 [historical_evaluation_index.json](../docs/historical_evaluation_index.json)。v6、v7、v7.1 的 Gold InfoUnit 哈希均为 `3735b786c0f9a1932a55051b049058cefa052a6694166164fcf64e74f9449a24`，rubric 哈希为 `34b6a72c59e49e77e79c62eb6e7dc91102085d38e0b8c2b65034bddc23e3a804`。与 Git HEAD 存储内容核对，后者版本字段为 `1.5`。InfoUnit JSONL 未单独存版本号，因此索引明确标记为 `historical-16 / rubric-1.5`，以内容哈希确定历史版本，不凭运行标签推造版本号。10 个本地历史目录及其中 10 份评分文件已登记，不能将其分数直接与 v1.7 比较。

本轮修改的已有文件共 7 个：

| 文件 | 原因 |
| --- | --- |
| `README.md` | 明确任务导向拆并规则、新对齐流程、历史隔离、测试范围和完整运行脚本的阶段边界 |
| `docs/annotation_review.md` | 说明合法综合节点、独立信息误合并诊断和一对一覆盖的区别 |
| `tests/test_manifest.json` | 增加显式 Gold 版本，澄清只能比较同一依据的评分 |
| `tests/alignment_template.jsonl` | 为现有 17 行加入当前版本和依据指纹；不填写预测或命中 |
| `tests/score_predictions.py` | 校验当前完整对齐，保留一对一，强化来源边界，记录依据和文件哈希，防止覆盖 |
| `tests/validate_eval.py` | 补全嵌套引用、模板、文档和基本字段校验 |
| `tests/run_extraction_eval.py` | 未来真实评测记录依据并保护已有目录；本轮未执行模型链路 |

新增文件共 10 个：`tests/gold_basis.py`、`tests/test_schema_contract.py`、`tests/test_scoring.py`、`docs/historical_evaluation_index.json`，以及本目录的 `report.md`、`before_hashes.json`、`historical_scan.json`、`file_inventory.json`、`test_results.json`、`integrity_verification.json`。其中测试数据只用于评分器与契约回归，不是新增模型预测或实际模型得分。

已检查且未改动的当前文件如下；完整历史文件名另外逐项列在清单中。

| 文件或逐项列出的文件组 | 保留原因 |
| --- | --- |
| `gold/gold_info_units.jsonl`、`gold/evaluation_rubric.json` | 用户指定的正式 v1.7 内容及评分规则，17 条、27 个来源引用均有效 |
| `gold/gold_issues.json` | 争点与全部节点归属一致 |
| `gold/gold_relations.jsonl`、`gold/negative_relations.jsonl` | 端点、证据 ID、类型及禁止关系均有效，没有旧签约语义依赖 |
| `gold/gold_view_cases.jsonl`、`gold/gold_view_selection.jsonl` | 已纳入 U027，未知签约日期没有被补成具体日期，选择规则无需改变 |
| `gold/gold_end_to_end.jsonl` | 当前必要/禁止 ID 及来源范围有效，主要任务已纳入 U027 |
| `gold/gold_parse_cases.jsonl` | 固定解析基准与本轮信息粒度无关 |
| `gold/gold_law_cases.jsonl`、`gold/verified_laws.json` | 引用有效，法条内容和适用前提不受本次粒度修订影响 |
| `docs/schema_contract.ts`、`docs/law_corpus_review.md` | v0.2 字段、枚举、视图结构与法律说明仍适用 |
| `tests/test_eval_data.py` | 用户已更新为 17 条及任务导向边界，现有 7 项测试通过 |
| `tests/score_view_selection.py`、`tests/score_law_retrieval.py` | 动态加载当前任务和有效 InfoUnit，新增离线测试验证 U027 可供法规结果引用 |
| `tests/check_docx_parsing.py` | 实际解析和缓存测试不依赖节点数量，通过且无需改写 |
| `fixtures/file_manifest.json` | 来源 ID、类型、长度和哈希正确 |
| `fixtures/parsed/file-06b00bee7b.txt`、`file-16d4ace3c7.txt`、`file-e256bb1ee8.txt`、`file-31ebd1366e.txt`、`file-3a4acd1eac.txt` | 固定来源不变，27 个 Gold 引文切片均一致 |
| `fixtures/raw/李某笔录.docx`、`聊天记录（节选）.docx`、`张某笔录.docx`、`转账记录.docx`、`租房合同.docx` | 5 份实际解析结果与固定 TXT 完全匹配 |
| `fixtures/variants/empty.txt`、`fixtures/variants/supplement_2026-03-09.txt` | 异常及合成补充场景不受影响 |
| `results/` 全部 301 文件，包含 v6、v7、v7.1 | 保留原始输出、日志、评分与运行记录；递归扫描、哈希核对通过 |
| `tests/__pycache__/` 原有 7 文件 | 只检查哈希，不修改、不作为源码依据；本轮 Python 使用 -B |

最后验收采用指定虚拟环境；完整 stdout、stderr 和退出码见 [test_results.json](test_results.json)。

```powershell
& .\backend\.venv\Scripts\python.exe -X utf8 -B evals/case_a/tests/validate_eval.py
& .\backend\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s evals/case_a/tests -p 'test_*.py' -v
& .\backend\.venv\Scripts\python.exe -X utf8 -B evals/case_a/tests/check_docx_parsing.py
git diff --check
```

静态校验通过：17 条 InfoUnit、27 个来源引用、9 条正关系、10 条禁止关系、10 个选择任务、6 个参考视图、15 条法条、13 个法律检索任务、7 个解析场景及 7 个端到端场景。实际 Pydantic 模型对 Gold Extraction、Relation 和全部参考视图验证通过。21 项离线单元测试与 2 项实际解析测试全部通过，0 失败、0 跳过；`git diff --check` 通过。初次新增测试曾出现 Windows 子进程 stdout 解码错误，已在测试调用中显式启用 UTF-8，最终复跑没有该错误。非法引用、旧对齐和一预测多 Gold 的拒绝是负例测试的预期结果，不是降低校验后通过。

历史预测读取测试只验证已有 v7.1 文件可被当前 17 条评分器读取、引用有效且原文件不变；没有对真实输出作语义匹配，不产生新的覆盖率。实际模型质量、历史输出按 v1.7 的命中和法律适用均未在本轮重新验证。

最终哈希核对见 [integrity_verification.json](integrity_verification.json)：本轮开始时 347 个受核对文件（含评测目录外的 Prompt 与运行法条）中，340 个不变，仅上述 7 个依赖文件改变，无删除或意外变更。全部 Gold、固定 TXT、DOCX、来源 manifest、历史记录、运行法条和用户的 Prompt 改动原样保留。开始时 Git 已有用户对 Gold、Prompt 和部分评测文件的修改；最终 `git diff` 中的这些预存改动不能归为本轮新增修改。

下一次真实 Extraction 应使用当前固定材料和来源类型，保存新的原始输出、Prompt 哈希、模型配置与 Gold 依据，保留后端确定性来源校验结果。复核时复制当前 17 行模板，按必要信息完整性及独立推理用途逐项评审；U008/U019/U035 不机械拆分，U026/U027 与 U029/U030 分别核查，关键内容出现但被错误合并可另列诊断但不增加覆盖。一、二轮若需比较，均使用同一 v1.7 依据和一对一规则，另存评分；不要拿历史 16 条分数直接计算提升。现有 `run_extraction_eval.py` 会运行完整两阶段流程，若只评第一轮，应使用已审查的第一轮调用/保存路径，不能把该完整脚本当成第一轮工具。
