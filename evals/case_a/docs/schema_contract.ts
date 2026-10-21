/* Case A evaluation contract: Schema v0.3. Runtime source of truth: backend/app/schemas.py. */
export type EntitySubtype = 'person' | 'organization' | 'object' | 'location';
export type EventSubtype = 'reported' | 'documented' | 'verified';
export type InfoUnitType = 'entity' | 'event' | 'state' | 'rule' | 'position';
export type InfoUnitSubtype = EntitySubtype | EventSubtype;
export type SourceKind = 'document' | 'communication' | 'testimony' | 'record' | 'other';

export interface SourceRef {
  file_id: string;
  source_kind: SourceKind;
  quote: string;
  start: number;
  end: number;
}

interface InfoUnitCommon {
  id: string;
  title: string; // Trimmed Unicode length: 2-24.
  content: string; // Trimmed Unicode length: 4-220.
  source_refs: SourceRef[];
  side: string | null;
  actor: string | null; // Same-case entity InfoUnit.id.
  target: string | null; // Same-case entity InfoUnit.id.
  related_issue: string | null; // Exact issue_set member when non-null.
  created_at: string;
}

export type InfoUnit = InfoUnitCommon & (
  | {type: 'entity'; subtype: EntitySubtype; speaker: string | null}
  | {type: 'event'; subtype: EventSubtype | null; speaker: string | null}
  | {type: 'state'; subtype: null; speaker: string}
  | {type: 'rule'; subtype: null; speaker: string | null}
  | {type: 'position'; subtype: null; speaker: string}
);

export interface QuoteUnit {
  quote_id: string;
  file_id: string;
  start: number;
  end: number;
  text: string;
  role: 'text' | 'temporal_context' | 'message' | 'record' | 'group_boundary';
  group_id: string | null;
  message_id: string | null;
  context_quote_ids: string[]; // Earlier temporal QuoteUnits in the same group.
}
interface ModelInfoUnitCommon {
  local_id: string;
  title: string;
  content: string;
  evidence_ids: string[]; // Current-case QuoteUnit IDs; never persisted on InfoUnit.
  side: string | null;
  actor_local_id: string | null; // Must reference an entity local_id.
  target_local_id: string | null; // Must reference an entity local_id.
  related_issue: string | null;
}
export type ModelInfoUnit = ModelInfoUnitCommon & (
  | {type: 'entity'; subtype: EntitySubtype; speaker: string | null}
  | {type: 'event'; subtype: Exclude<EventSubtype, 'verified'> | null; speaker: string | null}
  | {type: 'state'; subtype: null; speaker: string}
  | {type: 'rule'; subtype: null; speaker: string | null}
  | {type: 'position'; subtype: null; speaker: string}
);
export interface ModelExtraction { core_issue: string | null; issue_set: string[]; info_units: ModelInfoUnit[]; }
export interface ExtractionOutput { core_issue: string | null; issue_set: string[]; info_units: InfoUnit[]; }

export type RelationType = 'about' | 'support' | 'contradict';
export interface Relation {
  id: string;
  source_id: string;
  target_id: string;
  relation_type: RelationType;
  evidence_unit_ids: string[];
  rationale: string;
  certainty: 'explicit' | 'inferred';
}

export interface ViewSelection { view_type: 'timeline' | 'conflict' | 'hypothesis'; reason: string; }
export interface LayoutItem { info_unit_id: string; role: 'claim' | 'evidence' | 'event' | 'context'; emphasis: boolean; default_expanded: boolean; }
export interface ConflictView { view_type: 'conflict'; title: string; sections: {id: string; title: string; placement:'left'|'right'|'center'; items: LayoutItem[]}[]; missing_information: string[]; }
export interface TimelineView { view_type: 'timeline'; title: string; events: {id:string;title:string;time_label:string;sort_date:string|null;time_precision:'day'|'month'|'year'|'unknown';summary:string;related_info_unit_ids:string[]}[]; }
export interface HypothesisView { view_type: 'hypothesis'; title: string; hypothesis:string; steps:{id:string;claim:string;supporting_info_unit_ids:string[];opposing_info_unit_ids:string[];depends_on_step_ids:string[]}[]; missing_information:string[]; }
export type ViewPayload = ConflictView | TimelineView | HypothesisView;
export interface LawEntry { id:string;title:string;article_no:string;text:string;source_url:string;effective_status:'effective'|'unknown';verified_at:string|null; }
export interface LawMatch { law_id:string;issue:string;related_info_unit_ids:string[];relevance_reason:string; }
export interface EvalCase { id:string;stage:string;input:Record<string,unknown>;expected:Record<string,unknown>;checks:string[]; }
