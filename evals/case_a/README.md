# LogicWeaver｜案件 A 当前评测集

本评测集用于提前退租与押金返还纠纷，案例 ID 为 `case-4325561f`。其目标数据契约为 **Schema v0.2**，包含 17 条面向争点的核心 InfoUnit，并要求每条 `SourceRef` 自带 `source_kind`。评测集只保存当前有效标准答案，没有旧 Schema 转换规则。静态校验不代表模型或应用已经通过测试。

## 文件分工

- `fixtures/parsed/*.txt`：五份固定 UTF-8 案件输入；`fixtures/file_manifest.json` 记录文件 ID、路径、哈希、长度及来源类型。
- `fixtures/raw/*.docx`：原始 DOCX（需在本地提供），用于真实解析测试；`fixtures/variants/` 记录异常与补充文件输入。
- `gold/gold_issues.json`：争议问题；`gold_info_units.jsonl`：17 个必要信息单元；`gold_relations.jsonl` 与 `negative_relations.jsonl`：9 个应成立、10 个禁止误建关系。
- `gold/gold_parse_cases.jsonl`：7 个解析场景；`gold_view_selection.jsonl`：10 个视图选择任务；`gold_view_cases.jsonl`：6 个布局参考；`gold_end_to_end.jsonl`：7 个完整流程场景。
- `gold/verified_laws.json`：15 条法条 Gold；`gold_law_cases.jsonl`：13 个法律检索用例；`gold/evaluation_rubric.json`：评分指标。
- `docs/schema_contract.ts`：目标结构化数据契约；`docs/annotation_review.md`：必要节点、去重和争议边界；`docs/law_corpus_review.md`：法条收录依据和适用前提。
- `tests/validate_eval.py`：固定 TXT、来源偏移、关系和任务引用的静态完整性检查；`test_eval_data.py`：当前数据契约单元测试；`score_predictions.py`：基于人工语义对齐的抽取评分；`score_view_selection.py`、`score_law_retrieval.py`：视图选择和法规检索评分。
- `tests/alignment_template.jsonl`：17 个 Gold ID 与预测 ID 的人工映射模板；`tests/test_manifest.json`：当前评测范围和数量；`tests/check_docx_parsing.py`：实际上传五份 DOCX、核对固定 TXT 和缓存复用并检查异常输入。

## 运行

从项目根目录运行静态检查：

```bash
python evals/case_a/tests/validate_eval.py
python -m unittest discover -s evals/case_a/tests -p 'test_*.py'
```

`test_*.py` 覆盖静态数据、实际 Pydantic 契约及离线评分器回归；需使用后端虚拟环境，不启动服务、不调用模型。真实解析检查单独运行：

```bash
python evals/case_a/tests/check_docx_parsing.py
```

请将五份原始 DOCX 放在 `evals/case_a/fixtures/raw/`，不要放在 `docs/`。固定 TXT 的偏移使用 Unicode 字符索引，左闭右开 `[start,end)`；解析结果不一致时，需要审核、重新计算引用，禁止让验证器静默更改原文。

## 评分说明

U026 仅表示张某声称接租者入住，U027 独立表示张某声称另签租约；U015 只以附条件协商提议为必要内容。旧版16条Gold评分不可与新版17条直接比较，需重新进行语义对齐。

按独立推理用途审查拆并质量。U008 的互斥处理规则、U019 的整体拒退立场、U035 的多时点空置说法均为合法综合节点；不能只因跨句、跨时点或条件不同判定误合并。U026/U027、U029/U030 有独立核查或判断用途，仍分别计分。一条预测最多匹配一条 Gold，不可为了评分拆开或改写预测。可另外记录“关键内容已出现但与另一独立信息误合并”，该诊断不增加命中数。

抽取输出 ID 与 Gold ID 无需相同。将当前 `tests/alignment_template.jsonl` 复制到新的评测目录，逐条复核预测，填写 `predicted_id`、`match`（`yes/no`）和非空 `review_comment`。保留模板的 `gold_version`、`gold_basis_sha256`，不要批量给旧对齐贴上新版本。模板必须完整覆盖当前 Gold，每个 ID 恰好一次；`unreviewed` 不产生评分。再运行：

```bash
python evals/case_a/tests/score_predictions.py --prediction /path/to/predicted_extraction.json --alignment /path/to/reviewed_alignment.jsonl --output /path/to/extraction_score.json
```

如需评估额外节点，可选 `--extra-review`，标注 `valid_extra/duplicate/irrelevant/unsupported`。额外节点必须先人工判断，不应自动视为幻觉。视图选择支持 `constrained`（唯一目标）、`flexible`（多个合理候选）和 `open`（只评分实际选择视图的内容）三种模式。对应评分脚本通过 `--predictions` 输入模型输出。

`tests/gold_basis.py` 可打印当前版本、实际数量及 Gold/固定来源哈希。评分输出记录这些依据及预测、对齐文件哈希；已有输出文件会被拒绝覆盖。`tests/run_extraction_eval.py` 也会记录相同依据并拒绝复用非空输出目录；它运行完整 Extraction（含 Consolidation），不是仅第一轮工具。本轮未执行该脚本。

历史版本映射见 `docs/historical_evaluation_index.json`，包含 v6、v7、v7.1 等本地归档。历史报告中的拆分诊断保留原样，只反映当时口径，不是 v1.7 的评判依据。重评历史预测时使用原预测及现有文件 ID 映射，重新逐条审核 v1.7 模板，在新的目录保存结果；不得修改历史输出、拆分节点或复用旧命中判断。

法规检索使用 `backend/data/laws.json` 作为运行库，`gold/verified_laws.json` 作为评测参照。同一 ID 的法条原文、条号、标题与来源必须一致。可运行：

```bash
python evals/case_a/tests/score_law_retrieval.py --predictions /path/to/law_predictions.jsonl
```

条号和原文准确并不等于法律适用成立；违约金调整、押金性质及实际损失认定需要结合事实和法律条件进行专业复核。假设场景中的补充 TXT 是测试数据，不是案件证据。当前也未覆盖用户手动编辑节点的功能。

## 数据出处与人工复核

固定输入文本取自仓库中案件 A 的 extraction 请求记录：`backend/data/case-4325561f/llm_logs/20260331T141925321170Z_extraction_success_99f43f.json` 的 `request.files[].content`。该来源用于保留输入可重复性，不代表材料或事实经过法律核实。需要核对原始 DOCX 的完整文本和分段、多人对同一事件的陈述边界，以及候选法条与本案的实际关联。

## Gold v1.7 变更说明

- 根据核心争点下的推理用途组织卡片，而非逐句拆分；U008、U019、U035 保留综合表达。
- U015 不将3月1日“到时候看情况再说”列为必要节点内容，U016聚焦已找到接租者而非先前愿帮寻找的原话。
- U026（入住）与新增 U027（另签合同）分别核查，新合同签订日期不明。
- 旧评测中的 16 条 Gold 命中率不可直接作为新版17条Gold的基准，必须重新映射历史预测。
- 旧运行结果和 Gold 哈希不在本次补丁中修改；不要覆盖 `results/` 下既有运行记录。
