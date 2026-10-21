# LogicWeaver｜最小 MVP 数据 Schema v0.3（修订版）

**状态：Schema 设计稿 v0.3；尚未修改业务代码、Prompt 或 Gold；原 v0.2 与 Gold v1.7 均保持不变**\
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
8. **内容类型与可信程度区分**：一级类型回答信息表达什么；`event.subtype` 仅记录事件依据/核实阶段，不等于法律事实认定。
9. **引用统一使用 InfoUnit ID**：`actor`、`target` 和关系两端均引用已有节点的 `InfoUnit.id`；提交前检查引用完整性。

约定：下面使用 TypeScript 表达类型，方便阅读。真正修改后端时再映射为 Pydantic 模型。`?` 表示可选字段，`null` 表示允许空值。

---

## 阶段 1：上传文件 → 解析与存储

### 1.1 输入 `UploadRequest`

通过 `multipart/form-data` 传输，不是直接提交 JSON。

```ts
type UploadRequest = {
  case_title: string;
  files: File[];
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
  file_size: number;

  original_path: string;
  parsed_text_path: string | null;

  parse_status: ParseStatus;
  error_message: string | null;

  uploaded_at: string;
};
```

**新增核心字段：** `parsed_text_path`。

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

### 2.2 模型候选与正式输出

模型先返回候选结构 `ModelExtraction`；后端校验来源、分配正式 ID、将候选节点之间的局部引用映射为正式 `InfoUnit.id`，才生成 `ExtractionOutput`。这避免要求模型预先猜测后端持久化 ID。

```ts
type ModelSourceQuote = {
  file_id: string;
  quote: string; // 一段连续原文；仍按现有定位流程处理
};

type ModelInfoUnit = Omit<
  InfoUnit,
  "id" | "created_at" | "source_refs" | "actor" | "target" | "subtype"
> & {
  subtype: Exclude<InfoUnitSubtype, "verified"> | null; // 初始模型不得输出 verified
  local_id: string;                  // 同次模型输出内部唯一
  actor_local_id: string | null;     // 指向本次输出的 entity.local_id
  target_local_id: string | null;    // 指向本次输出的 entity.local_id
  source_quotes: ModelSourceQuote[];
};

type ModelExtraction = {
  core_issue: string | null;
  issue_set: string[];
  info_units: ModelInfoUnit[];
};

type ExtractionOutput = {
  core_issue: string | null;
  issue_set: string[];
  info_units: InfoUnit[]; // 已完成校验与引用映射的正式结构
};
```

- `core_issue` 为 `null` 或逐字存在于 `issue_set` 中；`issue_set` 不应存在语义重复争点。
- `related_issue` 为 `null` 或逐字存在于 `issue_set` 中，替代旧字段 `stance_target`。
- `local_id` 只在模型候选批次内有意义。后端生成持久化 ID，并原子性地映射所有 `actor_local_id/target_local_id`。跨文件增量处理时，可以通过后端提供的既有正式 ID 建立引用，不直接使用其它批次的 `local_id`。
- `ModelSourceQuote` 保留 v0.2 已实现方向（`file_id + quote`）；未在本次分类修订中强行引入尚未完成的段落/证据 ID 预处理。未来改为 `evidence_ids` 时，只替换候选来源接口，不改变正式 `SourceRef`。
- 提取默认输出对案件争点必要、可以独立核查或参与后续推理的最小有用节点集合，不按原文句数设定目标节点数。

### 2.3 基础信息 `InfoUnit`（5 类语义对象）

取消 `statement` 一级分类，改为五种互不替代的语义用途：`entity`、`event`、`state`、`rule`、`position`。对于“A 说自己转账了”，节点仍是 `event`；是否经过核实由 `event.subtype` 表达，原文中的归属和不确定性由 `content`、`speaker`、`source_refs` 一并保留。

| `type` | `subtype` 允许值 | 含义与示例 |
| --- | --- | --- |
| `entity` | `person` / `organization` / `object` / `location` | 人、机构、物件、地点；保留原四种实体分类 |
| `event` | `reported` / `documented` / `verified` / `null` | 搬离、转账、签约、沟通等事件；子类仅反映当前依据/核实阶段，不表示事件行为类别 |
| `state` | `null` | 金额、期间、数量、占用、空置、损坏等状态或属性 |
| `rule` | `null` | 合同、裁判文本记载的条件、权利义务和处理规则；不自动推定法律效力 |
| `position` | `null` | 主体对争议事项的判断、评价、请求、处理立场或协商方案 |

**事件 subtype 的严格含义：**

- `reported`：仅有一方、证人或其他叙述性材料陈述该事件；没有完成独立核实。即使多次重复出现同一说法，也不能自动升级。
- `documented`：原始材料中存在直接记录该行为的文书/交易记录等，例如案件中提供的转账文字记录；仅表示“材料有直接记录”，**不等于记录真实或已验真**。转述一份记录的单方说法仍是 `reported`。
- `verified`：经过独立的外部证据核验或有权用户明确确认的事件；**Extraction 模型不得自行输出 `verified`**。未来通过人工核验工作流和审计记录升级。当前 MVP 没有核验工作流，因此首轮模型输出中该值应始终禁用。
- `null`：暂不适用或无法确定当前阶段。计划、条件性未来行为必须在 `content` 中明确表达计划性/条件性，不能把它写成已发生事件。事件子类不承担“已发生/计划中”的完整时态建模。

此 `subtype` 实际上是压缩保存的事件证据阶段标签，不是对法律事实的最终认定；同一条事实受到对方反驳，并不会单凭反驳而从 `documented` 自动退级。需要分别比较矛盾证据时，由 Relation/视图表达争议。

**节点粒度原则：** 以是否具有独立核查、对比或推理价值决定拆合；同一规则的互补条件可以综合；不同事件、已约定条件与实际履行、事实判断与处理诉求应在需要分别核查时拆分。日期、数值和人员通常是节点属性，除非确有独立用途。身份卡只包含身份和案件角色。

**主体归属原则：** `speaker` 保留原有字符串字段，表示该节点语义内容归属于谁的说法或哪份直接记录。`state`、`position` **强制为非空字符串**；`entity`、`event`、`rule` 不强制，可以为 `null`。当 `state` 直接来自合同/记录而没有自然人发言时，`speaker` 填真实的文件或记载主体（如“房屋租赁合同”“转账记录”），不得虚构发言人。若状态来自张某自述，则填“张某”。多个相互独立的主体说法不得靠填一个含糊 speaker 合并成已确认事实。

**节点交叉引用原则：** `actor`、`target` 在正式数据中均为 `InfoUnit.id | null`，非名称字符串；非空时必须指向同一案件中存在的 `entity` 节点。`actor` 指执行行为的实体，`target` 指主要作用对象/受影响实体。一个事件有多个对象时，MVP 在 `content` 保留其余对象，不在单值字段中拼接多个 ID；未来确有需求再扩展参与者结构。`position/rule/state` 若确有明确主体/对象也可填写，否则 `null`。

```ts
type InfoUnitType =
  | "entity"
  | "event"
  | "state"
  | "rule"
  | "position";

type InfoUnitSubtype =
  | "person"
  | "organization"
  | "object"
  | "location"
  | "reported"
  | "documented"
  | "verified";

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
  start: number; // 在解析文本中的 Unicode 字符左边界
  end: number;   // 右边界（不含）
};

type InfoUnit = {
  id: string; // 后端分配
  type: InfoUnitType;
  subtype: InfoUnitSubtype | null; // 无子类时显式为 null

  title: string;   // 2—24 个 Unicode 字符
  content: string; // 4—220 个 Unicode 字符，保留必要条件及来源限定

  source_refs: SourceRef[];
  speaker: string | null; // state / position 必填非空
  side: string | null; // 案件内部角色，如承租人、出租人

  actor: string | null; // 指向 entity InfoUnit.id
  target: string | null; // 指向 entity InfoUnit.id

  related_issue: string | null; // 替代 stance_target
  created_at: string; // 后端生成
};
```

**长度要求：** `title` 2–24 字符，`content` 4–220 字符；后端使用 Unicode 码点长度和去除首尾空白后的文本做校验。数字、英文、标点均参与计数。控制的是单节点文本，不是要求把多条独立信息压到一个节点，也不允许为满足上限而静默截断重要条件。超出上限应返回候选校验错误，由模型重写或合理拆分。

**Subtype 校验：** `entity` 必须从四种实体子类中选择；`event` 只允许 `reported/documented/null` 由初始提取产生，`verified` 只能由独立核验工作流写入；`state/rule/position` 的 `subtype` 必须为 `null`。

**重要边界：** 单方陈述一个搬离行为仍然是 `event/reported`，不能因为文字描述了发生动作就当作已证明。`position` 中表示“应当返还押金”等判断和诉求，而不是任何人说过的话。合同中的具体押金条件是 `rule`；“合同记载月租 3000 元”可以是 `state`。规则、状态和立场若同时具有独立核查价值，不应因来自同一合同或同一段笔录强行合并。

这套类型是面向 MVP 的工程归纳，不是 ACE、PROV 或 LegalDocML 规定的统一分类表。参考链接仍见原 v0.2 文档：ACE、W3C PROV、OASIS LegalDocML 等。

### 2.4 字段含义与生成责任

| 字段 | 定义 | 谁生成 / 校验 |
| --- | --- | --- |
| `id` | InfoUnit 的稳定 ID | 后端生成；所有关联字段引用它 |
| `type` | 五类信息语义功能 | 模型选择，后端枚举校验 |
| `subtype` | entity 的对象分类；event 的依据/核实阶段；其余 `null` | 模型只可输出候选允许值，核验阶段才可写 `verified` |
| `title` | 2–24 字符的独立卡片标题 | 模型生成，后端长度校验 |
| `content` | 4–220 字符的完整必要语义，保留归属、不确定性及关键条件 | 模型生成；禁止静默截断 |
| `source_refs` | 至少一条精确来源 | 后端基于 `file_id + quote` 补齐 `source_kind/start/end` |
| `speaker` | 说法的主体，或状态信息的直接记载主体 | `state/position` 强制非空；其它可为 `null` |
| `side` | 案件内角色或立场，不设跨案件固定枚举 | 模型生成或 `null` |
| `actor` | 主要行动主体对应的 `entity` 节点 ID | 模型先引用 local_id，后端映射成正式 ID |
| `target` | 主要受影响/作用对象对应的 `entity` 节点 ID | 模型先引用 local_id，后端映射成正式 ID |
| `related_issue` | 关联争议问题的完整字符串 | `null` 或精确匹配 `issue_set` |
| `created_at` | 创建时间 | 后端生成 |

`summary` 已删除；折叠态摘要在界面层按需生成或截取，不再持久化第二份语义正文。`speaker` 没有改成 ID（本轮明确改为 ID 的只有 `actor/target`）；后续若要链接到人物卡，可在独立的实体消歧环节处理。

不引入模型自评 `confidence`、`extraction_reason` 或覆盖 `speaker` 含义的额外字段。

### 2.5 原文定位约定

`SourceRef.start` 和 `end` 是规范化 `.txt` 文件的 Unicode 字符位置，采用左闭右开区间 `[start, end)`。

必须满足：

- `file_id` 存在于当前案件；
- 对应文本可以从 `parsed_text_path` 读取；
- `[start, end)` 内文本与 `quote` 一致；
- 原文引用找不到或存在无法确定的重复匹配时，标记该条抽取为校验失败，不擅自选取来源。

**建议让模型返回 `file_id + quote`，由后端程序计算 `start/end`，并从文件元数据核验对应 `SourceRef.source_kind`。** 不让模型直接预测字符位置，因为精确位置计算应当由程序完成。

### 2.6 案件 A 示例：ID 链接与事件 subtype

以下为**正式持久化结构的示意**。示例中的原文引用以程序从真实文本定位后的结果为准，不代表本次已经重新计算偏移；例子刻意省略具体偏移数值，以免误将演示数字当作校验通过的来源。

```ts
const entities: Pick<InfoUnit, "id" | "type" | "subtype" | "title">[] = [
  { id: "info_person_zhang", type: "entity", subtype: "person", title: "张某" },
  { id: "info_person_li", type: "entity", subtype: "person", title: "李某" },
];

const event: Partial<InfoUnit> = {
  id: "info_deposit_payment",
  type: "event",
  subtype: "documented", // 仅记录中直接记载；不是独立验真
  title: "押金转账记录",
  content: "转账文字记录记载张某向李某支付2000元房屋押金；原始银行流水尚未独立核实。",
  speaker: null,
  actor: "info_person_zhang",
  target: "info_person_li",
  related_issue: "押金是否应退还",
};
```

如果只出现“张某说自己已经支付押金”，而没有对应直接记录，该事件应标为 `event/reported`，并在 `content` 保留“张某称”。`speaker` 可填“张某”，但 `event` 类型不强制填写。

### 2.7 跨文件 ID 映射和增量提取预留

- 一轮首次提取中，模型输出 `local_id`；后端先完成节点去重和正式 ID 分配，再把 `actor_local_id/target_local_id` 映射为正式 ID。找不到目标实体 ID 时拒绝该候选或先创建对应 entity，禁止回退为字符串姓名。
- 多轮逐文件提取需要向模型提供既有 `InfoUnit.id` 和节点摘要。若新候选引用现有主体，应由输入中预先列出的正式 ID 识别；此项属于未来多轮候选协议扩展，v0.3 不在第一轮同时实现。
- `actor/target` 与 `Relation` 中的 `actor_of/target_of` 若表示同一事实，不建立两个互不受控的权威副本；基础连线可由节点字段确定性派生。

## 阶段 3：结构化数据校验与保存

这一阶段不调用 LLM。输入为原始候选 `ModelExtraction`、已解析文件、当前已有实体节点（若有）；输出为通过验证且引用已映射的 `ExtractionOutput`。

```ts
type ValidationInput = {
  extraction: ModelExtraction;
  parsed_files: CaseFile[];
  existing_info_units?: InfoUnit[];
};

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

`T = ExtractionOutput`；最低校验条件：

1. `type/subtype` 合法配对：`entity` 需实体子类，`event` 允许 `reported/documented/null`（首轮不得由模型提交 `verified`），其它类型的 `subtype` 只能为 `null`。
2. `title` 为去除首尾空白后 2–24 个字符；`content` 为 4–220 个字符；不能依靠截断过检。
3. `state` 与 `position` 的 `speaker` 是非空且不是空白的字符串；其余类型可为 `null`。缺乏真实可归属的主体时应返回校验错误，不能编造说话人。
4. 各节点至少有一条来源；`file_id` 属于本案件，`quote` 是对应缓存全文中的真实连续引用，完整 `SourceRef` 的 `source_kind/start/end` 由后端物化且严格验证。
5. `core_issue`、`related_issue` 只能为 `null` 或指向 `issue_set` 中的原始字符串。
6. `local_id` 在本轮唯一，正式 `id` 在案件中唯一；所有非空 `actor/target` 通过 ID 映射后均指向确实存在的 `entity` InfoUnit；不允许姓名字符串漏入正式结构。
7. `reported/documented/verified` 的层级不得被模型凭推测升级。尤其 `verified` 必须有独立人工或程序核验审计依据，不能只因出现交易记录便标记为 `verified`。
8. 对同一事件的不同主体、日期、条件等不能因合并而丢失来源限定；引用实际支持该节点文本，必要时执行单独的语义复核。

当前第一轮评测采用**整批正式契约校验**；若未来要允许部分成功，应在评测协议中单列被拒节点、引用依赖和部分成功性质，不能悄然改变历史通过率口径。任何校验失败均不得对现有 CaseData 做部分写入。

## 阶段 4：基础信息 → 直接关系构建

对应当前 `run_relation_agent()`。输入仍为 `ExtractionOutput.info_units` 和争点列表，但 `InfoUnit.actor/target` 已是正式 InfoUnit ID；不再要求关系模型把姓名解析一遍。

### 4.1 模型输入 `RelationInput`

```ts
type RelationInput = {
  core_issue: string | null;
  issue_set: string[];
  info_units: InfoUnit[];
};
```

### 4.2 模型输出 `RelationOutput`

```ts
type RelationType =
  | "about"
  | "support"
  | "contradict";

type Relation = {
  id: string;
  source_id: string; // InfoUnit.id
  target_id: string; // InfoUnit.id
  relation_type: RelationType;
  evidence_unit_ids: string[]; // 全部引用 InfoUnit.id
  rationale: string;
  certainty: "explicit" | "inferred";
};

type RelationOutput = { relations: Relation[] };
```

`actor_of` 与 `target_of` 不再由 LLM 作为独立关系保存：它们可根据 `InfoUnit.actor/target` 确定性生成画布连线；存储两处会发生更新不同步。旧 `occur_at` 依赖已取消的 `state/temporal`，不再作为必需的基础关系。时间信息仍在 `content` 中，时间线按现有 `TimelineView` 生成事件日期；未来若需时间标准化再单独设计时间字段。

### 4.3 关系约束

| 类型 | 最小条件 |
| --- | --- |
| `about` | `source_id` 和 `target_id` 指向两个有效 InfoUnit，表示明确讨论某个主体、事件或信息，而不只是共享关键词 |
| `support` | 依据节点 `source_id` 支持 `target_id` 所表达的、可判断真伪或可争议的事件、状态、规则或立场；`evidence_unit_ids` 必须为有效 ID |
| `contradict` | 两个节点对**同一可比较的事实/条件/立场**表达不能同时成立的内容；不因双方立场不同就自动成立 |

所有非空关联 ID 必须存在，不能自环；不应自动把“涉及押金”识别为支持，也不能把“找到新租客”自动视为证明“无经济损失”。对 `actor/target` 生成的关系线在展示层标记为派生关系，不能无审计地作为 `RelationOutput` 的独立 LLM 推断。

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

一个案件可以存储多个推理视图。重新打开案件后，可以重新渲染此前生成的结果。若某 InfoUnit 被更新/归档，依赖其 ID 的既有视图必须重新验证或标为过期，不可静默保留失效引用。

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

**元信息卡片原则上从 `InfoUnit` 生成；语义关系连线从 `Relation` 生成，行为主体/对象连线从 `InfoUnit.actor/target` 派生。** 不再要求模型同时生成 `InfoUnit` 和一份重复的 `MetaCard`。

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

评测时既要检查模型内容，也要检查 Schema 合法性与引用真实性、`actor/target` 引用完整性以及 `state/position` 的归属约束。按新分类生成新的 Gold 标签映射，保留 Gold v1.7 原始字节及旧评分；改分类后的分数不得直接当作 v7.2 同口径结果。`fallback` 不能计为对应 LLM 模块的正常成功结果。

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
- `info_units`：基础信息的唯一权威集合，`actor/target` 使用集合内实体 ID。
- `relations`：非派生的语义关系集合；行为主体/对象关联以 `info_units` 字段为唯一权威来源。
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

## 五、v0.3 已确认的 Schema 决定

**决定 A：InfoUnit 为五种一级类型。** `entity/event/state/rule/position`；`statement` 删除。`entity.subtype` 继续使用 `person/organization/object/location`；`event.subtype` 表示 `reported/documented/verified` 的依据阶段；其它类型 `subtype=null`。首轮模型不得产生 `verified`。

**决定 B：删除 `summary`，限制文本长度。** `title` 2–24 字符、`content` 4–220 字符。重要事实、条件、限定语不得因长度限制而静默删除。

**决定 C：`speaker` 的校验按类型区分。** `state/position` 必填真实归属主体；`entity/event/rule` 可为 `null`。不因此推定事件发生或证据真实性。

**决定 D：统一 ID 引用。** `InfoUnit.actor/target` 在正式存储中引用 `entity InfoUnit.id`，不使用姓名字符串；模型使用本轮 local_id，后端分配并映射稳定 ID。`stance_target` 改名为 `related_issue`。

**决定 E：下游关系最小化。** `Relation` 保存 `about/support/contradict`；行为主体/对象连接由 `actor/target` 派生；`occur_at` 旧约束不再保留。既有三种视图的主要 Payload 结构不变。

**决定 F：来源格式本轮不扩张。** 仍保留 `SourceRef(file_id, source_kind, quote, start, end)`；模型候选只给文件 ID 与连续引文，后端完成位置计算与完整校验。证据段 ID 定位另列后续改造，不与本次类型调整混算。

## 六、来源合并与最小节点补充约定（保留）

1. `InfoUnit` 不再具有 `source_kind` 字段；`SourceRef` 必须携带自己的 `source_kind`。
2. 一个节点可以引用不同文件以及不同 `source_kind` 的原文；合并不表示材料真实性提高或独立证据数量增加。
3. 每条引用仍必须通过 `(file_id, start, end, quote)` 校验，`source_kind` 必须和该文件实际来源类型匹配。
4. 目标 Gold 以关键案情语义保留和节点去重为首要标准；对于同一方重复陈述，不为达到抽取数量要求强制建卡。
5. 新版评测另行审查未对齐的预测信息属于必要补充、重复、无关还是无依据内容；不能直接按预测节点数评价质量。

---

## 七、下游实施与迁移清单（按依赖顺序）

| 顺序 | 模块 / 位置 | 必须修改的内容 | 验收标准 |
| --- | --- | --- | --- |
| 1 | 后端 `schemas.py` / Pydantic 模型 | 更新 Type/Subtype 枚举与配对约束；移除 `summary`；设 `title/content` 长度；`speaker` 条件非空；`stance_target→related_issue`；`actor/target` 为 ID | 五种类型及 `null` 组合通过，旧子类及非法说话人/长度被拒绝 |
| 2 | Extraction Prompt + 严格 JSON Schema | 将输出切换到 `ModelExtraction` 候选；用 `local_id` 与 `actor_local_id/target_local_id`；不允许模型输出 `verified`；保留来源限定 | 模型输出可严格解析，单方自述为 `event/reported`，金额状态有真实 speaker |
| 3 | Extraction 归一化与保存 | 后端生成正式 ID；映射 actor/target；引用匹配并生成 SourceRef；更新 `related_issue` 校验；整批原子保存 | 所有非空关联指向当前案件 entity；错误引用拒绝写库，不修改既有 CaseData |
| 4 | Consolidation / 去重 | 用信息用途与独立核查性决定合并；合并后重映射 actor/target 及外部引用，禁止悬挂 ID | 合并重复实体后依赖节点仍正确引用；不同核查对象不强行合并 |
| 5 | Relation Prompt、Schema 与关系构建 | `about/support/contradict` 三类；删除对 `statement`、`state/temporal` 的依赖；actor_of/target_of 改为派生边 | 所有关系端点/evidence ID 有效；不会重复维护 actor/target 连线 |
| 6 | 前端 TypeScript 类型、卡片及连线渲染 | 显示 `title/content`，移除 `summary` 字段读取；更新五类型颜色/标签；以 entity ID 解析 actor/target 并绘制派生边 | 卡片在无 summary 情况下正常折叠；actor/target 无需字符串匹配即可定位 |
| 7 | 推理 Selector / Composers | 更新类别相关 Prompt；`ConflictView.role=claim` 是布局角色，不是旧 `statement` Type；Timeline 使用 event 的日期内容，不能再依赖 state/temporal；Hypothesis 仍用 InfoUnit ID | 三种视图均可引用新节点并保留来源；不会因为分类变更丢失时间事件 |
| 8 | `CaseData` / 旧案件读取层 | 为旧记录增加显式版本迁移或只读兼容；将旧 `actor/target` 名称解析成实体 ID；旧 `statement` 按语义映射，不机械转类 | 历史数据可安全读取；无法消歧时给出迁移错误，不静默乱连 |
| 9 | Gold / Evaluator / 评测脚本 | 在独立副本上迁移标签及字段；保留 Gold v1.7、历史 Prediction 和 Prompt 指纹；新增 `speaker`、subtype、ID、长度与完整契约断言 | 旧评测可复现；新口径报告明确分开，不能冒充 v7.2 同口径基线 |
| 10 | 未来 InfoUnit Tools（本轮不必实现） | create/update/archive 共用同一服务校验；核验时才能把 event 升级为 verified；删除前检查关系与视图引用 | 已发布 InfoUnit 的 ID 稳定；核验升级可审计，删除不造成悬挂关系 |

### 迁移规则（重要）

- `statement/fact_assertion`：按其**所指内容**分别迁至 `event` 或 `state`，来源中的“张某称”仍写入 `content`，不得一律判为 `verified`。
- `statement/assessment`：真正的评价、处理主张和诉求归 `position`；对可核查经济损失“存在/多少”的说法可归 `state`，要保留归属主体。
- `statement/provision`：具有规则或条件约束功能的条款归 `rule`；纯粹的合同金额属性可以归 `state`，不强行重复提取。
- `event/action/occurrence/transfer/communication`：均变为 `event`，新的 subtype 由来源性质决定（reported/documented），不从旧枚举直接自动推导真实程度。
- `state/temporal/quantitative/descriptive`：统一为 `state/subtype=null`，必要时继续在内容中准确保存时间/金额/数量。
- `actor/target` 旧名称不能直接赋予新的 ID 字段；应根据案件已有实体做消歧映射。重名或缺实体需显式报告。
- `speaker` 强制规则可能让历史状态节点不合法：从真实来源材料中补充归属，无法确定时先标记为待迁移而非编造。
- `summary` 删除后，旧 UI 如依赖摘要，使用 `content` 的折叠展示或展示层派生文本，不允许修改基础数据以凑成摘要。

### 本轮边界

已修改的是**文档中的目标数据契约与迁移说明**，并未修改代码仓库、实际 Pydantic Schema、Prompt、Gold 或运行评测。多轮 Agent Tools、全文片段证据 ID、状态核验工作流都可以共享此结构，但属于后续独立实现和评测。
