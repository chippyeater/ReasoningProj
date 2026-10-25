# Case A 离线检索诊断

仅评估 `search_info_units` 的访问与排序，不运行 Agent/LLM、不连接 Langfuse、不改生产 Prompt、工具、Gold 或案件。复用 `investigation/data.py` 的固定快照及 `build_case()`，当前17个节点。不是独立运行时或新的评测平台。

`queries.json` 是新增查询相关性草标，**全部 requires_human_review**。Gold 只提供节点记载和来源；哪些节点能回答某个查询，是本次新增主观判断。每个 need 的 `basis` 说明事实出处，`relevant_ids` 是该明确问题的正例，`context_ids` 是有用背景、但不计入二值正例。不得把存在引用等同于案件事实已证实。

27条主查询按9类正向信息需求组织，包含v15宽泛原查询、聚焦词、自然中文整句、同义表达和类型过滤；另有7条不计主指标的重复词、全角数字、来源原话、负例、过滤及非法输入诊断。原始调用参数和实际返回ID来自v15审计，无案件原文副本。草标v2只修正了不计主指标的 `source_only` 信息需求：原文定位对应U019，不能当作U008合同规则查询；27条主查询标签未变。

## 复现

在仓库根目录使用现有venv，每次指定新的输出路径（既有JSON或同名Markdown会被拒绝覆盖）：

```powershell
backend/.venv/Scripts/python.exe scripts/run_retrieval_eval.py --output evals/case_a/results/retrieval-quality/run-1.json
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -p 'test_retrieval_eval.py' -q
```

如本地保留v15审计，可额外逐字段核实来源；没有该忽略文件仍能从记录的真实调用复现固定快照上的搜索结果，输出会明确 `local_audit_verified=false`：

```powershell
backend/.venv/Scripts/python.exe scripts/run_retrieval_eval.py --output evals/case_a/results/retrieval-quality/run-2.json --audit evals/case_a/results/investigation/a84e44deb88644f5bf094425fc127355/A1/runs/investigation-78cef306f2634e9ab15797b35206677b.output.json
```

输出为完整JSON及相邻Markdown：全命中排序和分数、原查询关键节点诊断、每Query/每策略指标和漏检、首个及逐节点排名、分页可达性与调用成本、分组宏平均。记录查询集/生产与实验源码/固定快照/CaseData哈希、Python版本及Git HEAD/dirty；输出位于已有忽略的results目录，不提交日志。

## 指标与实验边界

- `Recall@k = hits / 本需求正例数`；`Precision@k = hits / k`，返回不足k条仍固定分母，避免少返回制造高分。另报 `returned_precision_at_k`；空返回的该值为NA，平均分母另记，不能脱离漏检率比较。
- `query_coverage` 是全命中列表能覆盖的正例比例，不受首屏k影响，是不改变匹配策略时分页的召回上限。`relevant_ranks` 包含所有正例首次排名，None表示整个查询未匹配，不能与排名大于k混淆。
- 主表对每个need先平均，再对9个need等权平均，避免同义改写数量左右结果。也提供Query宏平均及按kind/need分组。7条诊断查询和无正例负例不参与主表；34条所有数据仍输出。没有总分通过线。
- 生产基线直接调用现有工具，并核对离线完整排序与实际@5/@8前缀及has_more一致。分数为命中不同词项数优先、标题3/正文1命中权重其次、ID打破同分；不引入语义真值评分。
- `manual_focus` 是人工设计的单Query替换，正例标签不变。它是输入敏感性对照，**不是已验证的Tool Description效果**，人工知道材料词汇，存在乐观偏差；单点查询也可能遗漏多目标任务。独立的6次人工A1查询计划仅报告访问覆盖和调用成本，不与单次R@k混算，更不冒充真实Agent路径。
- 分页仅在基线完整排名上模拟offset切片，保留排序和词项。首屏R@5/R@8完全不变；提供每个节点首次页数、扫完所需调用数和最终覆盖，未匹配节点永远不能靠翻页出现。生产工具仍无offset/cursor。
- `literal_and` 对原始字面词项要求全部出现；不自动猜测AND/OR，不改生产默认OR。含噪声或多个替代表达的查询会漏检。
- `bigram_coverage` 只将连续汉字拆成重叠二元字词，单字及拉丁/数字保留，沿用原coverage/title-weight排序；不使用案例词表、同义词或付费分词/向量服务。
- `bigram_bm25` 在同样二元字词候选上使用BM25（k1=1.2，b=.75，title+content，无字段加权），仅用于拆分“解析”与“排序”影响。未调参，无独立holdout；纯词汇差异的同义词仍不可恢复。粗切分扩大噪声、BM25可使部分已有效查询退化，不能只看宏平均。

本集只有一个小案件，查询和相关性均为人工草标；需要人工复核并在其他案件上验证后才能外推。离线召回或排序改善不代表 Agent 能正确理解、引用、继续调查或选择恰当结束状态。
