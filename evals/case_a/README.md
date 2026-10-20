# LogicWeaver｜案件 A 当前评测集

本评测集用于提前退租与押金返还纠纷，案例 ID 为 `case-4325561f`。其目标数据契约为 **Schema v0.2**，包含 16 条核心 InfoUnit，并要求每条 `SourceRef` 自带 `source_kind`。评测集只保存当前有效标准答案，没有旧 Schema 转换规则。静态校验不代表模型或应用已经通过测试。

## 文件分工

- `fixtures/parsed/*.txt`：五份固定 UTF-8 案件输入；`fixtures/file_manifest.json` 记录文件 ID、路径、哈希、长度及来源类型。
- `fixtures/raw/*.docx`：原始 DOCX（需在本地提供），用于真实解析测试；`fixtures/variants/` 记录异常与补充文件输入。
- `gold/gold_issues.json`：争议问题；`gold_info_units.jsonl`：16 个必要信息单元；`gold_relations.jsonl` 与 `negative_relations.jsonl`：9 个应成立、10 个禁止误建关系。
- `gold/gold_parse_cases.jsonl`：7 个解析场景；`gold_view_selection.jsonl`：10 个视图选择任务；`gold_view_cases.jsonl`：6 个布局参考；`gold_end_to_end.jsonl`：7 个完整流程场景。
- `gold/verified_laws.json`：15 条法条 Gold；`gold_law_cases.jsonl`：13 个法律检索用例；`gold/evaluation_rubric.json`：评分指标。
- `docs/schema_contract.ts`：目标结构化数据契约；`docs/annotation_review.md`：必要节点、去重和争议边界；`docs/law_corpus_review.md`：法条收录依据和适用前提。
- `tests/validate_eval.py`：固定 TXT、来源偏移、关系和任务引用的静态完整性检查；`test_eval_data.py`：当前数据契约单元测试；`score_predictions.py`：基于人工语义对齐的抽取评分；`score_view_selection.py`、`score_law_retrieval.py`：视图选择和法规检索评分。
- `tests/alignment_template.jsonl`：16 个 Gold ID 与预测 ID 的人工映射模板；`tests/test_manifest.json`：当前评测范围和数量；`tests/check_docx_parsing.py`：通过 v0.2 上传接口核对五份真实 DOCX 的解析哈希、缓存复用，以及空 TXT / 图片的失败契约。

## 运行

从项目根目录运行静态检查：

```bash
python evals/case_a/tests/validate_eval.py
python -m unittest discover -s evals/case_a/tests -p 'test_*.py'
```

`test_*.py` 覆盖当前评测数据契约及评分输入契约，不运行历史模型基线。真实上传及解析检查单独运行，使用后端虚拟环境：

```bash
backend/.venv/Scripts/python.exe evals/case_a/tests/check_docx_parsing.py
```

请将五份原始 DOCX 放在 `evals/case_a/fixtures/raw/`，不要放在 `docs/`。固定 TXT 的偏移使用 Unicode 字符索引，左闭右开 `[start,end)`；解析结果不一致时，需要审核、重新计算引用，禁止让验证器静默更改原文。

## 评分说明

抽取输出 ID 与 Gold ID 无需相同。人工填写 `tests/alignment_template.jsonl` 中的 `gold_id`、`predicted_id` 和 `match`，再运行：

```bash
python evals/case_a/tests/score_predictions.py --prediction /path/to/predicted_extraction.json --alignment /path/to/reviewed_alignment.jsonl --output /path/to/extraction_score.json
```

如需评估额外节点，可选 `--extra-review`，标注 `valid_extra/duplicate/irrelevant/unsupported`。额外节点必须先人工判断，不应自动视为幻觉。视图选择支持 `constrained`（唯一目标）、`flexible`（多个合理候选）和 `open`（只评分实际选择视图的内容）三种模式。对应评分脚本通过 `--predictions` 输入模型输出。

法规检索使用 `backend/data/laws.json` 作为运行库，`gold/verified_laws.json` 作为评测参照。同一 ID 的法条原文、条号、标题与来源必须一致。可运行：

```bash
python evals/case_a/tests/score_law_retrieval.py --predictions /path/to/law_predictions.jsonl
```

法规预测记录必须为 `{id, matches: LawMatch[]}`，每个 match 仅含 `law_id`、`issue`、`related_info_unit_ids`、`relevance_reason`。
条文与来源从被引用的运行库条目核对，不要求模型复制原文。运行库已使用 v0.2 `LawEntry` 字段；Gold 自身的标注元数据保持不变。
抽取、关系、推理和法条执行接口目前明确返回 501。这里的静态契约测试及真实上传测试通过不代表模型能力通过评测。

条号和原文准确并不等于法律适用成立；违约金调整、押金性质及实际损失认定需要结合事实和法律条件进行专业复核。假设场景中的补充 TXT 是测试数据，不是案件证据。当前也未覆盖用户手动编辑节点的功能。

## 数据出处与人工复核

固定输入文本取自仓库中案件 A 的 extraction 请求记录：`backend/data/case-4325561f/llm_logs/20260331T141925321170Z_extraction_success_99f43f.json` 的 `request.files[].content`。该来源用于保留输入可重复性，不代表材料或事实经过法律核实。需要核对原始 DOCX 的完整文本和分段、多人对同一事件的陈述边界，以及候选法条与本案的实际关联。
