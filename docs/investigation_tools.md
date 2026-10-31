# Investigation Agent 只读工具（阶段 1）

入口：`backend/app/investigation_tools.py`。普通同步 Python 函数，由调用方传入当前案件的有效 `CaseData`，后续工具绑定时固定该参数，模型只提供查询参数。工具不自动读取“当前案件”、切换案件、保存数据、构建引用索引或调用模型。没有新增 HTTP 接口。

```python
from app.investigation_tools import search_info_units, get_relations, get_evidence

hits = search_info_units(case, query="实际 损失", types=["position", "state"])
edges = get_relations(case, info_unit_ids=["U029"], relation_types=["contradict"])
sources = get_evidence(case, info_unit_ids=["U029", "U036"])
```

## 统一响应

所有函数返回可 JSON 序列化的字典：

```json
{"status": "empty", "items": [], "has_more": false, "error": null}
```

无结果时 `status="empty"`；有结果且完整时为 `ok`；有未返回的结果时为 `truncated` 且 `has_more=true`。`invalid_request` 表示参数无效，`error` 表示证据读取、来源完整性错误或单条完整引用超出预算；这两种状态均返回空 `items`、`has_more=false`，以及 `{code, message}` 错误对象，不混入部分成功数据。

`has_more` 只描述结果条目是否被省略。摘要的局部截断使用 `content_truncated`、`rationale_truncated`、`evidence_ids_truncated` 单独标记。它不代表还有下一跳，也不证明存在尚未收集的证据。

## 检索与边界

| 工具 | 输入 | `items` 内容与限制 |
| --- | --- | --- |
| `search_info_units` | `query`，可选 `types`、`limit` | `id/type/subtype/title/content/content_truncated/speaker`；默认 5 条，上限 8 条，内容前 120 个 Unicode 字符 |
| `get_relations` | `info_unit_ids`，可选 `relation_types`、`limit` | `{relation, adjacent_info_units}`；默认 5 条，上限 8 条，只查请求端点的持久化入边和出边 |
| `get_evidence` | `info_unit_ids` | `{info_unit_id, source_ref_index, source_ref}`；最多 8 个完整引用，原文总计不超过 4000 个 Unicode 字符 |

- ID 列表必须有 1–5 个唯一、非空 ID，全部属于传入案件。混入任何非法 ID 时拒绝整个请求，不静默忽略。稳定 ID 在不同案件中若重名，仅指传入案件中的节点；案件选择由调用方负责。
- `limit` 必须是 1–8 的整数（布尔值也拒绝），超限报错，不静默截小。类型筛选必须为非空、无重复的合法枚举列表；不传代表全部合法类型。
- 查询限 1–200 字符。检索只检查标题与内容，不检查原文、争点集合或全案元数据。先 NFKC 归一化并忽略大小写，再以标点/空白切分字词；连续中文作为字面子串匹配，无中文分词、语义扩展或通配符。多个关键词按 OR 匹配，重复词不加权。
- Agent 调用搜索时，后端从本次模型请求的 `materials.info_units.items` 提取 ID，经内部关键字参数 `exclude_info_unit_ids` 排除这些节点，过滤先于评分、排序和 limit 截取；模型工具 Schema 不包含该参数。独立 Python 调用默认不排除节点。排除集不使用全部历史 seen ID，因上下文截断而不可见的旧节点仍可取得。缓存键也包含当前排除集，不能用不同可见范围下的旧搜索结果直接响应。
- 搜索的 `has_more` 只表示过滤后匹配节点数量超过本次 limit；返回 empty 可能只是匹配节点已可见。它不是穷尽全案的要求。每次围绕一个具体信息需求查询，只有仍缺影响当前判断的信息时才继续有针对性地检索。
- 排序先按命中的不同关键词数量降序，再按标题命中权重 3、内容命中权重 1 降序，最后按原始 ID 升序。独立的 `_keyword_score` 函数可替换并有离线测试。排序不表示事实可信度。
- 关系按原始关系 ID 升序返回，每条只出现一次，保留原始 ID、source/target 方向、类型和 certainty。rationale 限 120 字符，evidence_unit_ids 限 8 个并单独标记截断；相邻节点仅包含已返回边的外部端点摘要。actor/target 字段不派生为关系，支持/矛盾关系也不自动反向或递归展开。
- 证据按请求节点顺序及各节点原有 SourceRef 顺序返回，保留 `file_id/source_kind/quote/start/end`。偏移是缓存 TXT 中 Unicode 字符的左闭右开区间 `[start,end)`，不使用字节或 UTF-16 偏移。
- 仅使用已有 `read_parsed_document` 读取实际返回引用所在的缓存 TXT，每个相关文件每次最多读取一次，逐条验证精确切片；不读取原始文件，不重解析，不读取无关缓存，不把整文件放入返回结果。即使 quote 已存在于 SourceRef，缓存缺失或切片不符仍明确报错。
- 引用条数或总长度预算耗尽时，仅返回能完整容纳的前缀，标记 `truncated`。单条引用超过 4000 字符时报 `error/evidence_budget_exceeded`，不会伪造短引文或偏移。
- 已取得的 InfoUnit 内容可用于探索性 Hypothesis 和 Evidence Gap，来源定位不等于事实证实。仅在需要核对精确措辞、时间、金额、陈述主体或上下文时读取原始 SourceRef；不要求每个节点都读原文。Evidence Link 和 Hypothesis Status 仍须有合法且已取得的来源引用，初始已核验的 SourceRef 继续有效。

## 离线验收与阶段局限

```powershell
& backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -p 'test_investigation_tools.py' -v
```

测试使用案件 A 当前 17 个 Gold 节点、7 条 Gold 关系和五份固定 TXT，在临时目录组装并验证 `CaseData`；不依赖或修改运行中的案件快照。补充构造边界场景只存在于测试内存/临时目录中，不改 Gold 或历史输出。

用“损失”检索，继续查 U029 的一跳矛盾关系，再追溯 U029/U036 原文，验证 Agent 能找到调查下一步，而每次仅取得局部摘要、关系或引用。来源可定位、关系已持久化均不等于内容已经证实；`speaker/subtype/certainty` 保留这一判断所需的上下文。

阶段 1 无分页游标。截断后应收窄关键词、类型或节点集合；单节点引用超过预算时无法用此接口遍历剩余引用，超大单条引用也需后续单独设计受限的证据读取接口。工具并不保证所有查询都有更多可检索结果，也不评价推理或法律适用是否充分。
