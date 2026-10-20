# LogicWeaver / ReasoningProj — 案件 A 评测集 v1.4（法规库与法条检索扩充）

## 评测范围与状态

案件：`case-4325561f`，租房提前退租及押金争议。依据 **GitHub 2026-03-31 的 extraction 日志中实际传入模型的五份文件全文** 制作，并非仅根据截断预览，也非直接从模型原有 `info_units` 复制标准答案。

- 文件文本：**已从历史日志恢复并以 UTF-8 固定**；对应原始 DOCX 二进制尚未逐字核对。
- 信息/关系/视图 Gold：**初步人工标注草案**，已通过格式、引用、内部一致性检查；仍需第二名评审核对语义粒度与法律解释。
- 法条原文与条号：参考最高人民法院公开的《中华人民共和国民法典》全文整理并核对，网页 https://www.court.gov.cn/zixun/xiangqing/233181.html ；**法条适用性没有被认定为案件正确法律结论**。
- 不包含自动运行完毕的 LLM 基线评分，也没有对系统进行代码修改。当前仓库还在旧版 schema，需先完成目标 Schema 迁移才适合跑所有阶段的正式评测。

## 内容索引

| 位置 | 用途 | 数量 |
| --- | --- | ---: |
| `fixtures/parsed/*.txt` | 五份固定的模型实际输入文本，含换行 | 5 |
| `fixtures/file_manifest.json` | 文件 ID、哈希、出处和目标路径映射 | 5 |
| `gold/gold_issues.json` | 核心争点及规范化争点集合 | 3 个争点 |
| `gold/gold_info_units.jsonl` | 按照通用 `entity/event/state/statement`（14 子类型）标注的信息单元，含**真实字符偏移** | 16 |
| `gold/gold_relations.jsonl` | 建议存在的直接关系 | 9 |
| `gold/negative_relations.jsonl` | 禁止的直接关系，专测过度关联 | 10 |
| `gold/gold_parse_cases.jsonl` | 文件缓存与解析异常 | 7 |
| `gold/gold_view_selection.jsonl` | Timeline / Conflict / Hypothesis 选择 | 10 |
| `gold/gold_view_cases.jsonl` | 两种 Conflict、两种 Timeline、两种 Hypothesis 结构参考 | 6 |
| `gold/verified_laws.json` | 核对名称/条号/正文的候选法条库 | 15 |
| `gold/gold_law_cases.jsonl` | 法条查准、适用条件、不相关法条陷阱 | 13 |
| `gold/gold_end_to_end.jsonl` | 完整/缺失文件/新增证据/坏引用/持久化 | 7 |
| `gold/evaluation_rubric.json` | 评分规则与验收条件 | 1 |
| `tests/validate_eval.py` | Gold 内部检查（无需第三方库） | — |
| `tests/score_predictions.py` | 经人工语义对齐后的抽取覆盖与引用有效性报告 | — |
| `tests/alignment_template.jsonl` | 金标准与模型输出 ID 的人工对照模板 | 16 |
| `docs/annotation_review.md` | 关键节点去重、来源合并与人工复核规则 | — |
| `docs/legacy_id_migration.json` | 37 条旧 ID 到 16 条 Gold 的处理表 | 37 |
| `docs/merged_source_audit.json` | 多来源合并的原文审计清单 | — |
| `docs/baseline_audit.md` | 现有仓库输出可观察的问题，非新 Schema 评分 | — |

**v1.2 信息压缩与来源合并：** 37 条旧 Gold 经人工语义重审压缩为 16 条关键节点；保留需独立推理的证据命题，合并同一方重复陈述和同一节点内的相关参数。旧 ID 处理规则详见 `docs/legacy_id_migration.json`。

**Schema 变更：** `InfoUnit.source_kind` 移除；每条 `SourceRef` 新增 `source_kind`。同一节点可来自聊天与笔录，且不因此被当成多个独立证据。`type/subtype` 仍是四类、14 子类。应用仓库的后端/Prompt/前端尚未实现本目标 Schema；数据校验不代表实际模型通过测试。

## 运行与整合

```bash
# 解压后将 evals/case_a/ 放在仓库根目录
python evals/case_a/tests/validate_eval.py
python -m unittest discover -s evals/case_a/tests -p 'test_*.py'
```

用法：先按 `gold_info_units.jsonl` 建立独立标注结果；正式测试信息提取时从 `fixtures/parsed` 读取五个全文 TXT；测试关系时输入**金标准信息单元**排除上游错误，端到端测试时改成实际抽取结果。视图输出按联合类型 Schema 解析，再检查 `must_include_info_unit_ids`、有效引用和禁止断言。法条直接从 `verified_laws.json` 查询，模型不得改写条文原文。

`gold_info_units` 中 `SourceRef.start/end` 是日志 `request.files[].content` 字符串的 Python/Unicode 字符偏移（左闭右开），不是原始 DOCX 文件的段落/页码；如果正式解析文件文本不同，**需重新计算哈希、偏移并审核标准答案**，不能拿错误偏移强行通过。

模型输出 ID 可能不等于 U001 等金标准 ID：先通过人工语义核对 `alignment_template.jsonl` 的 `gold_id -> predicted_id`，再执行：

```bash
python evals/case_a/tests/score_predictions.py \
  --prediction /path/to/predicted_extraction.json \
  --alignment /path/to/reviewed_alignment.jsonl \
  --output /path/to/extraction_score.json
```

`match` 必须由评审者改为 `yes`，未匹配的预测单元**不能直接当作幻觉**；可能是 Gold 漏标的有效信息。对抽取精确率还需另行逐一复核这些额外单元。支持 `--extra-review` 输入额外预测节点的人工标签 `valid_extra/duplicate/irrelevant/unsupported`，输出重复与无关节点的分布，不能直接将未匹配节点称为幻觉。关系评测则以语义对齐后的实体 ID 比较正例/负例。

## 重要约束

1. **标准答案不是原有代码输出**：原代码只生成 5 个信息单元和 4 条关系，它们不能充当 Gold。
2. **“材料载明”与“事实为真”不同**：合同、聊天、笔录、转账文字转写分别标注其来源表达形式。
3. **法条条号/文字准确不意味着适用性确定**：尤其不要把“押金”自动等同《民法典》中的“定金”，也不能直接判“提前退租押金一律不退”有效或无效。
4. `view_cases` 是**可接受参考输出**，不能要求模型字面或节点顺序全部相同；应检查必要内容、结构合法、来源和禁止断言。
5. `source_refs` 内每条必须携带 `source_kind`，并真实定位；无定位不通过，不能回退绑定所有文件。
6. “补充证据”的场景使用明确标识为合成的 TXT，**不是案件真实证据**。
7. 用户修改功能尚未实现，不在 v1.2 成功标准中。
8. 如修改字段名、拆分/合并信息单元，须同时更新所有引用它的关系、视图、法条用例和评测脚本；先跑完整校验再提交。

## 版本与出处

- Schema：对话中目标《LogicWeaver｜最小 MVP 数据 Schema v0.2》。
- 案件输入：GitHub `chippyeater/ReasoningProj`，`backend/data/case-4325561f/llm_logs/20260331T141925321170Z_extraction_success_99f43f.json` 的 `request.files[].content`。
- 人工评审状态：**待复核**；这版是工程可检查的第一版，不代表法律事实或司法结论已获专家认证。

## v1.3 补充：视图评测、法律候选与 DOCX 解析

- `gold_view_selection.jsonl` 增加 `selection_mode`：`constrained`（目标视图明确，必须命中）、`flexible`（时间/冲突等多种组织方式合理，只验证允许集合）、`open`（“展示整体内容”一类开放请求，不评价视图选择是否匹配预设标签；只审核实际所选视图的内容完整性、Schema、相关性及来源）。新增 S009/S010。运行模型后可用 `tests/score_view_selection.py --predictions <file>` 按三类任务分别统计；**它不会替代视图内容的人工/程序评分**。
- `gold_law_cases.jsonl` 对确切条号查询坚持 ID 和原文精确匹配；开放式候选查询支持 `required_any_of_law_ids`（至少命中一个备选法条，仍须解释关联）。`E2E001` 不再强制只能选冲突视图或一定命中第584条。法律条文必须属于有限语料库，无法从库外检索法规。
- 取消负例 N007：房屋空置可以与**潜在的**租金收益损失有解释性联系，但不能直接证明损失金额或全额扣押金。继续禁止虚假等同。
- `gold_parse_cases.jsonl` 五个正常解析用例增加 `input.raw_file_path=fixtures/raw/{filename}` 的建议路径。请把五份原 DOCX 从 `docs/` 移至 `fixtures/raw/`；若仍在 `docs/` 下，新脚本也兼容。**以前的 `validate_eval.py` 只检查已经存在的 TXT 和 Gold，不运行 DOCX 解析器。** 新增显式测试 `tests/check_docx_parsing.py`，需要在原应用仓库内（含后端依赖、原 DOCX）手动运行。当前后端已观察到 DOCX 错误占位文本被报告为 `parsed` 的风险；该脚本包含失败状态检查，修复前可能有预期的测试失败。
- 解析器单元测试不同于完整链路集成测试：后者还必须核实上传、落盘 `parsed_text_path`、再次读取直接命中缓存、解析失败不传给抽取模型。这些功能依赖目标 Schema 的后端代码迁移，目前尚未实现，也不能声称已通过。

## v1.4 补充：运行库与法律 Gold 一致化

- 新增正式司法解释第 **63、64、65、67** 条到 `gold/verified_laws.json`；**第61条不作为案件 A 核心法条**：它有特定的不支付租金等金钱债务、解除合同等前提，材料不足以确认。
- `backend/data/laws.json` 是**运行时法条检索库的替换文件**（19条）；与 Gold 共享 15 个相同的法条 ID、标题、条号与原文；另外保留并修正旧库的合同约束力、合同解释、诚实履行、电子数据真实性四条。原运行库第707、第714条等存在条号与正文不符，不能继续作为事实来源。详情见 `docs/law_corpus_review.md`。
- `gold/gold_law_cases.jsonl` 从 8 例扩至 13 例。第64、65条的匹配只表示有条件相关，不能预先认定“押金不退”等于违约金；第65条的30%规则也不是自动退款公式；第67条测试押金不当然适用定金罚则。
- v1.4 额外修正 `fixtures/file_manifest.json` 里的 `record`、`communication` 来源分类，与 Schema v0.2 的 `SourceRef.source_kind` 对齐，不改输入文字或原始 TXT 哈希。
- **运行测试：** 从仓库根目录执行 `python evals/case_a/tests/validate_eval.py`、`python -m unittest discover -s evals/case_a/tests -p 'test_*.py'`。前者如果检测到 `backend/data/laws.json`，会校验 Gold 与运行库的每一条共有法规均严格相同。
- **真实模型输出评分：** `python evals/case_a/tests/score_law_retrieval.py --predictions /path/to/law_predictions.jsonl`。每行传入 `{"id":"L001","selected_laws":[{"law_id":"LAW-0585","text":"...","source_url":"...","relevance_reason":"..."}]}`。脚本只验证 ID/原文/来源及应命中/禁止命中；适用前提与模型的解释仍需人工评价。
- **仍未修改后端检索函数。** 当前 GitHub 主分支的法律检索需要“已接受的冲突卡片”，且运行旧版 `LawRetrieveRequest`；必须完成目标 Schema 迁移才能执行新 Gold 所定义的直接法律检索请求。文件测试通过不代表真实端到端检索已经通过。
