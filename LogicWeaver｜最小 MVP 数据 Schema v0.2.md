# LogicWeaver｜最小 MVP 数据 Schema v0.2

**状态：目标 Schema v0.2；分类与来源合并规则已同步案件 A Gold，应用代码尚未修改**\
**依据：** ReasoningProj 当前 `main` 分支；ACE 信息抽取、W3C PROV 与 OASIS LegalDocML 的通用建模原则\
**目标：** 支持案件 A 从文件上传、信息抽取、关系构建、生成式推理、法条检索到画布展示和评测的完整流程。

## 一、总体设计原则

1. **原始数据与界面数据分离**：文件、信息单元、关系和推理结果是业务数据；卡片位置、展开状态是展示数据。
2. **所有基础信息必须可溯源**：可定位到文件中的具体原文，而不仅仅是文件名。
3. **来源记录不等于事实真实**：模型只能确认材料如何记载，不能把单方陈述自动视为已证实事实。
4. **基础关系与推论分离**：关系提取只建立直接关系，复杂解释留给推理阶段。
5. **LLM 输出不直接入库**：先校验数据类型、引用 ID、原文位置，再保存。
6. **避免为同一信息建立多个权威副本**：卡片及连线尽量由结构化业务数据生成。
7. **明确保留原型边界**：暂不实现用户编辑、自动法律裁判、完整法规搜索和复杂权限系统。

约定：下面使用 TypeScript 表达类型，方便阅读。真正修改后端时再映射为 Pydantic 模型。`?` 表示可选字段，`null` 表示允许空值。

---

## 阶段 1：上传文件 → 解析与存储

### 1.1 输入 `UploadRequest`

通过 `multipart/form-data` 传输，不是直接提交 JSON。

```ts
type UploadRequest = {
  case_title: string;
  files: File[];
  // multipart 中传 JSON 字符串，按本批原始文件名精确绑定；同批文件名须唯一。
  source_kinds: Record<string, SourceKind>;
};
```

### 1.2 持久化输出 `CaseFile`

```ts
type FileType =
  | "pdf"
  | "docx"
  | "txt"
  | "markdown"
  | "image"
  | "other";

type ParseStatus =
  | "parsed"
  | "failed"
  | "unsupported";

type CaseFile = {
  file_id: string;
  filename: string;
  file_type: FileType;
  source_kind: SourceKind; // 上传方明确提供，持久化后填充并校验 SourceRef
  file_size: number;

  original_path: string;
  parsed_text_path: string | null;

  parse_status: ParseStatus;
  error_message: string | null;

  uploaded_at: string;
};
```

**新增核心字段：** `parsed_text_path`。Extraction 阶段经确认补充必填 `source_kind`，不由模型或扩展名推断。

创建案件和追加文件上传均要求 multipart `source_kinds` 参数（JSON 对象），按原始文件名精确绑定，不按顺序绑定。
缺失、非法枚举、额外键、重复键、同批重复文件名或无法一一对应时拒绝上传，不使用默认类型。
同名文件可分批上传，保存后按各自唯一 `file_id` 关联来源；不新增来源编辑接口或独立元数据文件。

推荐目录：

```text
backend/data/{case_id}/
  case.json
  upload_files/
    {file_id}.docx
  parsed/
    {file_id}.txt
```

`original_path` 和 `parsed_text_path` 均保存相对于案件目录的路径。

解析文本采用 UTF-8 纯文本，保留原始段落顺序和换行。PDF 可以在各页前加入 `[Page 1]` 等页码标记；DOCX 保留原有段落分隔。

理由：

- 避免每次 LLM 调用都重新读取和解析 DOCX/PDF；
- 解析结果可以独立检查、复用和评测；
- 后续可以利用固定文本偏移定位证据；
- 不引入额外数据库或文档服务。

本 MVP 对图片暂不进行文字识别。不能解析的文件明确返回 `unsupported` 或 `failed`，不允许将占位文本作为案件事实。

### 1.3 运行时输出 `ParsedDocument`

```ts
type ParsedDocument = {
  file_id: string;
  text: string;
};
```

此对象供后续阶段读取，无需作为完整内容重复保存在 `case.json` 中。

---

## 阶段 2：解析文本 → 核心争议与基础信息

对应当前 `run_extraction_agent()`。

### 2.1 模型输入 `ExtractionInput`

```ts
type ExtractionInput = {
  case_title: string;

  files: {
    file_id: string;
    filename: string;
    text: string;
  }[];
};
```

模型输入使用第一阶段缓存的完整解析文本，而非文件预览。

当前接口中的 `raw_text`、`context` 可以暂时保留兼容，但首轮评测仅测试文件输入。

### 2.2 模型输出 `ExtractionOutput`

```ts
type ExtractionOutput = {
  core_issue: string | null;
  issue_set: string[];
  info_units: InfoUnit[];
};
```

继续沿用当前的争议问题表示方式。首轮不另外引入 `Issue` 数据表或争议 ID。

约束：

- `core_issue` 为 `null`，或者必须存在于 `issue_set` 中。
- `issue_set` 不应包含重复或仅措辞不同的问题。
- 信息单元通过 `stance_target` 关联某个争议问题。提取默认输出案件当前争点所需的最小关键节点集合，不以材料句数或文件篇数为目标。

### 2.3 基础信息 `InfoUnit`（跨案件最小分类）

采用四个一级类型：`entity`（对象）、`event`（发生的事情）、`state`（属性或状态）、`statement`（被表达的命题）。分类依据是**信息本身表达什么**，而不是该信息来自合同、聊天还是笔录，也不是它是否已被证实。

| `type` | `subtype` | 含义与跨案件示例 |
| --- | --- | --- |
| `entity` | `person` | 个人：租客、员工、伤者、证人 |
| `entity` | `organization` | 组织：企业、学校、行政机关 |
| `entity` | `object` | 有独立身份的有形或数字对象：车辆、房屋、商品、电子设备 |
| `entity` | `location` | 地点：事故现场、工地、交付地点 |
| `event` | `action` | 人或组织实施的行为：签署、搬离、施工、解雇 |
| `event` | `occurrence` | 不以明确行为主体为前提的发生事件：火灾、事故、受伤 |
| `event` | `transfer` | 金钱、货物、占有等转移：付款、发货、返还财物 |
| `event` | `communication` | 通知、沟通、协商、发送消息等表达行为 |
| `state` | `temporal` | 时间点、期间、期限：事故时间、合同期限 |
| `state` | `quantitative` | 金额、数量、面积、比例等数值属性 |
| `state` | `descriptive` | 非数值状态：损坏、占用中、停工、身体状况 |
| `statement` | `fact_assertion` | 对事实的表述或断言：证人称某人当晚在场 |
| `statement` | `assessment` | 判断、评价、法律解释或立场：认为对方存在过错 |
| `statement` | `provision` | 文字中规定的权利、义务、条件或处理规则：合同解除条款、裁判主文 |

**关键节点提取与合并规则：** 先识别与争议、时间链、双方立场、缺失证据有直接关系的核心语义，再判断是否需要画布节点。相同人对同一事件的重复叙述优先汇总为一个 InfoUnit，同时在 `source_refs` 保存各处完整引用；合同的期限/租金可在一条必要节点中表达，而非为每个数值强制建卡。只要事实命题不同或存在真正的立场冲突（如“合同约定押金数额”和“押金实际支付记录”），就保持独立。原文无论是否成为节点均可在解析文本中查阅。

**节点删除测试：** 如果删去某张节点后，不影响核心争点、关键时间线、证据支持/反对和信息缺口，且该节点没有必要的独立引用价值，则不要求提取。不同来源的合并不得被计算为独立证明；保留原发言人、日期和不确定性。不得将两次表述自动改写为新的确证事实。

`statement/provision` 只表示**文件写了什么**，不自动认定合同条款有效或裁判已生效；外部法规全文仍由独立的 `LawEntry` 管理。`event/action` 只说明材料涉及某个行为，不等于已经法律认定为合法、违法或违约。原有的 `violation` 不再作为基础分类，以免把法律判断提前写入抽取结果。

```ts
type InfoUnitType =
  | "entity"
  | "event"
  | "state"
  | "statement";

type InfoUnitSubtype =
  // entity
  | "person"
  | "organization"
  | "object"
  | "location"

  // event
  | "action"
  | "occurrence"
  | "transfer"
  | "communication"

  // state
  | "temporal"
  | "quantitative"
  | "descriptive"

  // statement
  | "fact_assertion"
  | "assessment"
  | "provision";

// 反映文件表达形式，不承担真假判断。
type SourceKind =
  | "document"
  | "communication"
  | "testimony"
  | "record"
  | "other";

type SourceRef = {
  file_id: string;
  source_kind: SourceKind;
  quote: string;
  start: number;
  end: number;
};

type InfoUnit = {
  id: string;

  type: InfoUnitType;
  subtype: InfoUnitSubtype;

  title: string;
  summary: string;
  content: string;

  source_refs: SourceRef[];

  speaker: string | null;
  side: string | null; // 案件内立场或角色，如原告、被告、雇主、承租人；不使用全局角色枚举

  actor: string | null;
  target: string | null;

  stance_target: string | null;

  created_at: string;
};
```

**最小判别顺序**：①独立可识别的人、物、组织、地点 → `entity`；②发生或实施的行为 → `event`；③时间、数量或静态属性 → `state`；④某方的说法、判断、约定或规则文本 → `statement`。不要求从同一句话机械地产生四种卡片。

对于“张某称自己 3 月 5 日搬离”，若抽取目标是**张某作出的说法**，用 `statement/fact_assertion`；若需要记录其**所描述的搬离事件**，用 `event/action`，在 `content` 中注明“据张某陈述”，并保留同一原文引用和 `speaker`。两种表示不能因类型不同就被当作两份相互独立的证据；同一段文本也不必默认重复建卡。对于“张某发送了一条搬离通知”，沟通行为本身才用 `event/communication`。

这套类型是面向 MVP 的**工程归纳**，不是 ACE、PROV 或 LegalDocML 现成的统一分类表。主要参考：[ACE 信息抽取任务（实体、数值、时间、关系、事件）](https://catalog.ldc.upenn.edu/LDC2006T06)、[W3C PROV 的 Entity / Activity / Agent 与来源责任模型](https://www.w3.org/TR/prov-primer/)、[OASIS Akoma Ntoso 对法律文档内容、结构与语义的分离](https://docs.oasis-open.org/legaldocml/akn-core/v1.0/akn-core-v1.0-part1-vocabulary.html)、[就业法庭裁判文本的信息抽取研究（事实、主张、法律依据等）](https://arxiv.org/abs/2403.12936)。

### 2.4 字段含义

| 字段              | 说明                              |
| --------------- | ------------------------------- |
| `id`            | 信息单元唯一标识                        |
| `type`          | 四类通用语义对象：entity / event / state / statement                   |
| `subtype`       | 14 个明确的跨案件子类，按一级类型限制取值                       |
| `title`         | 画布节点标题                          |
| `summary`       | 卡片简短摘要                          |
| `content`       | 信息单元完整语义内容                      |
| `source_refs[].source_kind` | 每条引文的材料形式（文档、沟通记录、笔录、交易转写等），不表示真实性 |
| `source_refs`   | 对应原文引用及精确位置，至少一项                |
| `speaker`       | 该说法的发言者或事件记载的陈述来源；不限于 `statement` |
| `side`          | 案件内所属角色或立场，使用字符串；不写死承租人/出租人等类型               |
| `actor`         | 事件中的行为发起者；自然发生的事件可为 null                      |
| `target`        | 事件的对象或受影响方，可以为 null                       |
| `stance_target` | 关联的争议问题；无对应争点时为 null                         |
| `created_at`    | 后端生成的创建时间                       |

**与旧 Schema 的差别：**

- 用 `content` 合并原有 `detail` 的语义职责。
- 将 `source_refs: string[]` 改为 `SourceRef[]`，支持精确溯源。
- 新增 `source_refs[].source_kind`，明确来源形式；以跨案件通用的 document / communication / testimony / record / other 表示。
- `speaker` 不再被限制为仅 `statement` 使用；角色 `side` 使用案件内字符串而非特定纠纷角色枚举。
- 暂时不保留 `extraction_reason`，该解释放入运行日志。
- 暂时不保留 `confidence` 作为核心事实字段。模型对提取结果的自评置信度不能代表陈述真实性。
- 一级分类统一为 entity / event / state / statement，取消预设合法性判断的 violation，以及具体案件才适用的 landlord / tenant 等固定枚举。

### 2.5 原文定位约定

`SourceRef.start` 和 `end` 是规范化 `.txt` 文件的 Unicode 字符位置，采用左闭右开区间 `[start, end)`。

必须满足：

- `file_id` 存在于当前案件；
- 对应文本可以从 `parsed_text_path` 读取；
- `[start, end)` 内文本与 `quote` 一致；
- 原文引用找不到或存在无法确定的重复匹配时，标记该条抽取为校验失败，不擅自选取来源。

**建议让模型返回 `file_id + quote`，由后端程序计算 `start/end`，并从文件元数据核验对应 `SourceRef.source_kind`。** 不让模型直接预测字符位置，因为精确位置计算应当由程序完成。

### 2.6 案件 A 中的示例

```json
{
  "id": "info_09",
  "type": "event",
  "subtype": "transfer",
  "title": "押金支付记录",
  "summary": "转账文字记录载明张某向李某支付2000元押金",
  "content": "材料中的转账文字记录载明，2026年1月5日张某向李某转账2000元，备注为房屋押金。",
  "source_refs": [
    {
      "source_kind": "record",
      "file_id": "file-31ebd1366e",
      "quote": "金额：2000 元",
      "start": 45,
      "end": 54
    }
  ],
  "speaker": null,
  "side": null,
  "actor": "张某",
  "target": "李某",
  "stance_target": "押金是否应退还",
  "created_at": "2026-03-31T14:19:25Z"
}
```

示例中的 `start/end=45/54` 对应本次案件 A 固定解析文本里的“金额：2000 元”。正式实现时必须由程序重新定位并验证，不能把这两个位置硬编码到通用流程。

---

## 阶段 3：结构化数据校验与保存

这一阶段不调用 LLM。

### 输入

```ts
type ValidationInput = {
  extraction: ExtractionOutput;
  parsed_files: CaseFile[];
};
```

### 输出

```ts
type ValidationResult<T> = {
  valid: boolean;
  data: T | null;
  errors: {
    item_id: string | null;
    field: string;
    message: string;
  }[];
};
```

对于信息提取，`T = ExtractionOutput`。

最低校验条件：

1. `type/subtype` 合法匹配。
2. 每条信息必须有来源。
3. 来源文件及引文真实存在。
4. `stance_target` 为 `null` 或属于 `issue_set`。
5. ID 不重复。
6. 必填内容非空。
7. 不将模型的单方陈述自动转写成已经证实的事实；`statement/provision` 不等于规则有效。

可以允许部分有效的信息单元通过校验，但应明确记录被拒绝的条目；不能让错误条目静默消失。

---

## 阶段 4：基础信息 → 直接关系构建

对应 `run_relation_agent()`。

### 4.1 模型输入 `RelationInput`

```ts
type RelationInput = {
  core_issue: string | null;
  issue_set: string[];

  info_units: InfoUnit[];
};
```

这里必须把 `speaker`、`side`、`stance_target`、`actor`、`target` 一并发送给模型。

### 4.2 模型输出 `RelationOutput`

```ts
type RelationType =
  | "actor_of"
  | "target_of"
  | "about"
  | "support"
  | "contradict"
  | "occur_at";

type Relation = {
  id: string;

  source_id: string;
  target_id: string;

  relation_type: RelationType;

  evidence_unit_ids: string[];
  rationale: string;

  certainty: "explicit" | "inferred";
};

type RelationOutput = {
  relations: Relation[];
};
```

每条关系必须引用已存在的 `InfoUnit.id`。

`evidence_unit_ids` 记录关系判断所依据的信息单元，不重复复制原文；原文由对应信息单元的 `source_refs` 提供。

关系类型继续沿用现有 Prompt 中的六类，但后端和前端必须统一枚举。

### 4.3 关系的最小合法性约束

| 关系           | 最小约束        |
| ------------ | ----------- |
| `actor_of`   | 事件 → `entity`（行为主体）     |
| `target_of`  | 事件 → `entity`（对象或受影响方） |
| `about`      | `statement` → 相关信息   |
| `support`    | 信息 → 被支持的 `statement` |
| `contradict` | 相互冲突的 `statement`     |
| `occur_at`   | 事件 → `state/temporal`   |

不构成上述关系的内容不应被强行生成 `related_to`。

**边界：** “两段信息都涉及押金”不足以自动构成支持关系。“找到新租客”也不能直接证明“房东没有实际损失”。

---

## 阶段 5：用户问题 → 推理视图选择

对应 `run_reasoning_agent()` 内部的 Selector。

### 5.1 输入 `ReasoningInput`

```ts
type ReasoningInput = {
  case_id: string;
  question: string;

  selected_info_unit_ids: string[];
  selected_relation_ids: string[];
};
```

后端读取 `CaseData`，组成实际模型上下文：

```ts
type ReasoningContext = {
  question: string;

  core_issue: string | null;
  issue_set: string[];

  info_units: InfoUnit[];
  relations: Relation[];

  selected_info_unit_ids: string[];
  selected_relation_ids: string[];
};
```

选中范围为空时，可以使用当前案件中与问题相关的全部有效信息。

### 5.2 视图选择输出

```ts
type ViewType =
  | "timeline"
  | "conflict"
  | "hypothesis";

type ViewSelection = {
  view_type: ViewType;
  reason: string;
};
```

这里只决定使用什么推理视图，不生成法律结论或界面组件代码。

---

## 阶段 6：视图选择 → 生成式布局规划

对应 `timeline_composer`、`conflict_layer_composer`、`hypothesis_composer`。

### 6.1 输入

```ts
type ComposerInput = ReasoningContext & {
  view_type: ViewType;
};
```

### 6.2 共同输出

所有视图必须包含：

- `view_type`：决定使用哪一个前端渲染器；
- `title`：当前视图标题；
- 引用现有信息单元的 ID；
- 可被对应 Pydantic Schema 校验的结构化内容。

### 6.3 冲突视图

```ts
type LayoutItem = {
  info_unit_id: string;

  role:
    | "claim"
    | "evidence"
    | "event"
    | "context";

  emphasis: boolean;
  default_expanded: boolean;
};

type ConflictView = {
  view_type: "conflict";
  title: string;

  sections: {
    id: string;
    title: string;

    placement:
      | "left"
      | "right"
      | "center";

    items: LayoutItem[];
  }[];

  missing_information: string[];
};
```

最小能力：围绕争点组织双方观点、关联证据、共有事实及待补信息。

布局分组由 LLM 决定，视觉样式由前端实现。

### 6.4 时间—事件链

```ts
type TimelineView = {
  view_type: "timeline";
  title: string;

  events: {
    id: string;
    title: string;

    time_label: string;
    sort_date: string | null;

    time_precision:
      | "day"
      | "month"
      | "year"
      | "unknown";

    summary: string;

    related_info_unit_ids: string[];
  }[];
};
```

`time_label` 用于界面展示，`sort_date` 用于排序。

`sort_date` 为 `null` 时表示暂时无法确定时间，不能擅自编造日期。

时间线中的事件必须能回溯到已有信息单元。

### 6.5 假设—证据视图

```ts
type HypothesisView = {
  view_type: "hypothesis";
  title: string;

  hypothesis: string;

  steps: {
    id: string;
    claim: string;

    supporting_info_unit_ids: string[];
    opposing_info_unit_ids: string[];

    depends_on_step_ids: string[];
  }[];

  missing_information: string[];
};
```

`steps` 用于表达多阶段推理过程；依赖关系使前端能够渲染推理路径，而不仅是证据列表。

最小约束：

- 每个推理步骤可以引用已有信息。
- 步骤之间的依赖 ID 必须有效。
- 不允许循环依赖。
- 支持、反对与信息不足必须能够区分。
- 不能把生成的假设当作已经成立的事实。

### 6.6 三种视图的联合类型

```ts
type ViewPayload =
  | ConflictView
  | TimelineView
  | HypothesisView;
```

前端根据 `view_type` 选择对应组件，按结构化字段渲染。

---

## 阶段 7：推理结果 → 持久化与画布展示

### 7.1 需要保存的推理结果

```ts
type ReasoningView = {
  id: string;
  question: string;

  view_type: ViewType;
  selection_reason: string;

  payload: ViewPayload;

  created_at: string;
};
```

本 MVP 暂不把它命名为已经确认的法律结论。

一个案件可以存储多个推理视图。重新打开案件后，可以重新渲染此前生成的结果。

### 7.2 画布状态

```ts
type Point = {
  x: number;
  y: number;
};

type WorkspaceState = {
  selected_node_ids: string[];
  focused_node_id: string | null;
  current_view_id: string | null;

  node_positions: Record<string, Point>;

  display_levels: Record<
    string,
    1 | 2 | 3
  >;

  viewport: {
    zoom: number;
    offset_x: number;
    offset_y: number;
  };
};
```

这些字段用于保存用户的画布操作状态。

**元信息卡片原则上从 `InfoUnit` 生成，基础关系连线从 `Relation` 生成。** 不再要求模型同时生成 `InfoUnit` 和一份重复的 `MetaCard`。

当前代码中已有 `meta_cards`、`inference_cards`、`edges` 等结构，改动时可以保留兼容层逐步迁移，不要求第一轮全部删除。

---

## 阶段 8：争议问题 → 法律依据检索

这一步不是第四种推理视图，而是给推理过程提供候选法律依据。

### 8.1 本地法规库 `LawEntry`

```ts
type LawEntry = {
  id: string;

  title: string;
  article_no: string;
  text: string;

  source_url: string;

  effective_status:
    | "effective"
    | "unknown";

  verified_at: string | null;
};
```

最小法规库继续使用 `backend/data/laws.json`，只保存经过核对的条文。`verified_at` 记录最后一次人工核验日期。

### 8.2 检索输入

```ts
type LawRetrieveInput = {
  case_id: string;

  issue: string;
  selected_info_unit_ids: string[];

  max_results: number;
};
```

不再以“冲突卡片必须被接受”为前置条件。用户在分析争议时即可请求候选法律依据。

### 8.3 检索输出

```ts
type LawMatch = {
  law_id: string;

  issue: string;
  related_info_unit_ids: string[];

  relevance_reason: string;
};

type LawRetrieveOutput = {
  matches: LawMatch[];
};
```

`law_id` 必须引用真实存在且已核验的 `LawEntry`。模型只负责选择候选条文、解释相关性，不负责生成条文原文。

`LawMatch` 只是候选法律依据的关联记录，不意味着该条文必然适用，也不是最终法律判断。

---

## 阶段 9：各阶段运行日志与评测

为能够复现模型结果，统一保存最小运行记录。

```ts
type PipelineStage =
  | "parse"
  | "extraction"
  | "relation"
  | "view_selection"
  | "view_composition"
  | "law_retrieval";

type RunStatus =
  | "success"
  | "fallback"
  | "failed";

type StageRun = {
  id: string;
  stage: PipelineStage;
  status: RunStatus;

  model: string | null;
  prompt_version: string | null;

  input_snapshot_path: string | null;
  output_snapshot_path: string | null;

  error_message: string | null;
  created_at: string;
};
```

大型输入、原始模型回复和调试信息可以单独保存为 JSON 日志文件，`StageRun` 只记录索引和状态。

首轮评测集无需重新定义整个后端接口，采用以下最小评测记录格式即可：

```ts
type EvalCase = {
  id: string;

  stage:
    | PipelineStage
    | "end_to_end";

  input: Record<string, unknown>;
  expected: Record<string, unknown>;

  checks: string[];
};
```

评测时既要检查模型内容，也要检查 Schema 合法性与引用真实性。`fallback` 不能计为对应 LLM 模块的正常成功结果。

---

## 阶段 10：案件统一存储 `CaseData`

所有业务数据最终组织为：

```ts
type CaseData = {
  case_id: string;
  case_title: string;

  files: CaseFile[];

  core_issue: string | null;
  issue_set: string[];

  info_units: InfoUnit[];
  relations: Relation[];

  reasoning_views: ReasoningView[];
  law_matches: LawMatch[];

  workspace_state: WorkspaceState;

  stage_runs: StageRun[];

  created_at: string;
  updated_at: string;
};
```

其中：

- `files`：原始文件信息和完整解析文本路径。
- `info_units`：基础信息的唯一权威集合。
- `relations`：基础关系的唯一权威集合。
- `reasoning_views`：已经生成的三类推理视图。
- `law_matches`：案件争点与已核验法条的关联。
- `workspace_state`：前端画布状态。
- `stage_runs`：处理及评测追踪信息。

`LawEntry` 保存在独立法规库中，不随案件重复复制。

---

## 四、按照实际执行顺序的输入输出总表

| 阶段       | 输入                                            | 输出                      | 是否调用 LLM        |
| -------- | --------------------------------------------- | ----------------------- | --------------- |
| 1. 上传解析  | `UploadRequest`                               | `CaseFile[]` + 文本文件     | 否               |
| 2. 信息抽取  | `ExtractionInput`                             | `ExtractionOutput`      | 是               |
| 3. 校验    | `ExtractionOutput`                            | `ValidationResult`      | 否               |
| 4. 关系提取  | `RelationInput`                               | `RelationOutput`        | 是               |
| 5. 视图选择  | `ReasoningContext`                            | `ViewSelection`         | 是               |
| 6. 视图规划  | `ComposerInput`                               | `ViewPayload`           | 是               |
| 7. 画布渲染  | `InfoUnit[]` + `Relation[]` + `ReasoningView` | 前端组件与状态                 | 否               |
| 8. 法条检索  | `LawRetrieveInput` + `LawEntry[]`             | `LawRetrieveOutput`     | 检索可不使用，相关性解释可使用 |
| 9. 日志与评测 | 各阶段输入和输出                                      | `StageRun` + `EvalCase` | 按评测方法决定         |
| 10. 持久化  | 前述业务数据                                        | `CaseData`              | 否               |

说明：这张表是数据流顺序，并不要求第 8 阶段只能在第 7 阶段之后触发。法条检索可以在分析争点时按需调用。

## 五、此次需要确认的关键设计决定

这版草案已按讨论修订，不需要补充信息才能继续，但以下四点值得在建立标准答案之前仔细确认。

**决定 A：采用 `entity/event/state/statement` 四类通用信息。**

用 14 个最小子类覆盖人物、机构、物件、地点、行为、事故、转移、沟通、时间、数值、状态、事实陈述、判断和规则性文本。合同条款用 `statement/provision`，金额独立成节点时用 `state/quantitative`，不将“违约”预先认定为已证实事件；材料形式由 `source_refs[].source_kind` 区分。

**决定 B：用 `SourceRef` 精确绑定原文。**

模型只提供文件 ID 和原文引用；后端校验文本并计算位置。不把“引用了哪个文件”当作已经完成溯源。

**决定 C：暂时保留现有六类基础关系。**

只对关系字段和校验规则进行统一，不在第一版引入复杂推理关系。若评测发现某类必要关系无法表达，再根据失败案例扩充。

**决定 D：简化三类推理视图的布局协议。**

保留动态内容选择、分组、强调、事件排序、假设推理链等核心能力，暂不保留现有 Schema 中大量只服务于特定视觉效果的字段。展开和缩放等状态交给前端管理。

**后续同步事项：** 本轮仅修订文档，尚未修改 GitHub 代码或案件 A 的评测文件。实施时需同步更新 `InfoUnitType`/`InfoUnitSubtype`、抽取与关系 Prompt、归一化校验、前端类型及 Gold JSONL 中的旧标签；旧评测集不能直接用新枚举评分。


## 六、v0.2 来源合并与最小节点补充约定

1. `InfoUnit` 不再具有 `source_kind` 字段；`SourceRef` 必须携带自己的 `source_kind`。
2. 一个节点可以引用不同文件以及不同 `source_kind` 的原文；合并不表示材料真实性提高或独立证据数量增加。
3. 每条引用仍必须通过 `(file_id, start, end, quote)` 校验，`source_kind` 必须和该文件实际来源类型匹配。
4. 目标 Gold 以关键案情语义的保留和**节点去重**为首要标准；对同一方重复陈述，不为达到抽取数量要求强制建卡。
5. 新版评测另行审查未对齐的预测信息属于必要补充、重复、无关还是无依据内容；不能直接按预测节点数量评价好坏。
