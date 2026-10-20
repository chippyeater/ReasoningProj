# v1.4 仅法规库与法律 Gold 变更

请将本增量包从仓库根目录解压覆盖。`backend/data/laws.json` 为经核对的完整替换文件（19条），不要与旧错误条目混合合并。`evals/case_a/gold/verified_laws.json` 为15条案件 A 评测候选法条；扩充了第63、64、65、67条，未纳入第61条。法律测试由8例变为13例；16条InfoUnit、9正例关系、10负例关系、10视图选择、6视图规划、7端到端均维持不变。

校验：`python evals/case_a/tests/validate_eval.py`；`python -m unittest discover -s evals/case_a/tests -p 'test_*.py'`。其中真实DOCX解析测试依然需要原始DOCX和迁移后后端环境。

`backend/data/laws.json` 的旧ID `law_1..law_8` 经过统一替换；不要假设存量案件中原法条引用可自动迁移。新增条文的正文按最高人民法院正式文本核实；条件化法律适用仍需人工审阅。详见 `evals/case_a/docs/law_corpus_review.md`。
