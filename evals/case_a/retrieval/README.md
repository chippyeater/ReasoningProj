# Case A 离线检索诊断

仅评估 `search_info_units` 的访问与排序，不运行 Agent/LLM、不连接 Langfuse、不改生产 Prompt、工具、Gold 或案件。复用 `investigation/data.py` 的固定快照及 `build_case()`，当前17个节点。不是独立运行时或新的评测平台。

`queries.json` 只保存检索质量评测所需的信息需求、查询与相关性草标，**全部 requires_human_review**。信息需求是“希望从案件中找到什么”，同一需求可用宽泛、聚焦、整句或同义Query表达。Gold提供节点记载和来源，Query相关性是另行作出的待复核判断，不代表案件事实已证实。

每个need用中文`description`说明目标，`basis`说明标签依据：`relevant_ids`直接回答该需求，计入Recall/Precision；`context_ids`提供背景但不计入二值正例。唯一的`deposit`有意同时包含U006（押金金额）与U008（返还及提前退租规则），背景为U010/U019，不再把规则单独计权。

当前质量集共26条Query，分两组：

- **A. 单项信息需求**：`loss`、`rent`、`departure`、`arrival`、`lease`、`vacancy`、`deposit`，共7个need、25条Query，是主要检索指标的统计范围。一个需求可以需要多个节点。
- **B. 综合调查目标**：`a1`及原始v15宽泛Query，单独报告Recall、相关节点排名与漏检。它同时要求租金、时间、空置与双方损失陈述，混入A组会把多个已经计入的主题再次赋权。

工具契约测试检查“工具是否按接口约定工作”，不作主观相关性评分。这8类边界不再留在质量JSON中，由已有`backend/tests/test_investigation_tools.py`覆盖：来源原话`source_only`（以U019为依据，title/content不含该措辞）、无匹配`negative`、类型排除`wrong_type_empty`、无效`punctuation/blank`、重复词`duplicate_terms`、Unicode数字归一化`nfkc_number`、规则过滤`rule_filter`。`test_retrieval_eval.py`保留评测计算、实验适配器、统计分组和隔离测试，避免再次复制生产契约用例。

当前评测版本为`case-a-retrieval-diagnostic-3`。旧版主平均包含9个need、27条Query，新版移除了重复押金规则权重并独立统计A1，**新旧总体宏平均不可直接比较**；相同Query的逐项结果仍可核查。旧定义通过Git Commit `f336e597e434d827784e3f2681a38dd123003147`恢复，不创建历史副本，也不覆盖已有输出。

## 复现

在仓库根目录使用现有venv，每次指定新的输出路径（既有JSON或同名Markdown会被拒绝覆盖）：

```powershell
backend/.venv/Scripts/python.exe scripts/run_retrieval_eval.py --output evals/case_a/results/retrieval-quality/run-1.json
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -p 'test_retrieval_eval.py' -q
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -p 'test_investigation_tools.py' -q
```

如本地保留v15审计，可额外逐字段核实来源；没有该忽略文件仍能从记录的真实调用复现固定快照上的搜索结果，输出会明确 `local_audit_verified=false`：

```powershell
backend/.venv/Scripts/python.exe scripts/run_retrieval_eval.py --output evals/case_a/results/retrieval-quality/run-2.json --audit evals/case_a/results/investigation/a84e44deb88644f5bf094425fc127355/A1/runs/investigation-78cef306f2634e9ab15797b35206677b.output.json
```

输出为完整JSON及相邻Markdown：A组主指标及逐Query结果、B组A1指标/排名/漏检、原始查询完整排序、人工多次查询覆盖。JSON中`summary[策略].primary_need_macro`为A组主指标，`summary[策略].a1`为B组；`manual_a1_plan`单列人工计划。输出还保留候选排名/分数、分页成本、查询集和源码/快照哈希、Python及Git HEAD/dirty，存放在既有忽略的results目录。

## 指标与实验边界

- `Recall@k = hits / 本需求正例数`；`Precision@k = hits / k`，返回不足k条仍固定分母，避免少返回制造高分。另报 `returned_precision_at_k`；空返回的该值为NA，平均分母另记，不能脱离漏检率比较。
- `query_coverage` 是全命中列表能覆盖的正例比例，不受首屏k影响，是不改变匹配策略时分页的召回上限。`relevant_ranks` 包含所有正例首次排名，None表示整个查询未匹配，不能与排名大于k混淆。
- 主表先对同一need的Query平均，再对A组7个need等权平均；deposit只计一份权重。也提供A组Query宏平均及按kind/need分组。A1不进入这些平均，契约测试不参与检索质量评分。没有总分通过线。
- 生产基线直接调用现有工具，并核对离线完整排序与实际@5/@8前缀及has_more一致。分数为命中不同词项数优先、标题3/正文1命中权重其次、ID打破同分；不引入语义真值评分。
- `manual_focus` 是人工设计的单Query替换，正例标签不变。它是输入敏感性对照，**不是已验证的Tool Description效果**，人工知道材料词汇，存在乐观偏差；单点查询也可能遗漏多目标任务。独立的6次人工A1查询计划仅报告访问覆盖和调用成本，不与单次R@k混算，更不冒充真实Agent路径。
- 分页仅在基线完整排名上模拟offset切片，保留排序和词项。首屏R@5/R@8完全不变；提供每个节点首次页数、扫完所需调用数和最终覆盖，未匹配节点永远不能靠翻页出现。生产工具仍无offset/cursor。
- `literal_and` 对原始字面词项要求全部出现；不自动猜测AND/OR，不改生产默认OR。含噪声或多个替代表达的查询会漏检。
- `bigram_coverage` 只将连续汉字拆成重叠二元字词，单字及拉丁/数字保留，沿用原coverage/title-weight排序；不使用案例词表、同义词或付费分词/向量服务。
- `bigram_bm25` 在同样二元字词候选上使用BM25（k1=1.2，b=.75，title+content，无字段加权），仅用于拆分“解析”与“排序”影响。未调参，无独立holdout；纯词汇差异的同义词仍不可恢复。粗切分扩大噪声、BM25可使部分已有效查询退化，不能只看宏平均。

本集只有一个小案件，查询和相关性均为人工草标；需要人工复核并在其他案件上验证后才能外推。离线召回或排序改善不代表 Agent 能正确理解、引用、继续调查或选择恰当结束状态。
