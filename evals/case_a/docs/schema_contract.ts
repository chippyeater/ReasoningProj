/* LogicWeaver 目标 MVP Schema v0.2：source_kind 移至每条 SourceRef，非当前仓库已实现的 API。 */
export type InfoUnitType = 'entity' | 'event' | 'state' | 'statement';
/* subtype 仅用于结构分类；不表达真实性和法律效力。 */
export type InfoUnitSubtype = 'person' | 'organization' | 'object' | 'location' | 'action' | 'occurrence' | 'transfer' | 'communication' | 'temporal' | 'quantitative' | 'descriptive' | 'fact_assertion' | 'assessment' | 'provision';
export type SourceKind = 'document' | 'communication' | 'testimony' | 'record' | 'other';
export interface SourceRef { file_id: string; source_kind: SourceKind; quote: string; start: number; end: number; }
export interface InfoUnit {
  id: string;
  type: InfoUnitType;
  subtype: InfoUnitSubtype;
  title: string;
  summary: string;
  content: string;
  source_refs: SourceRef[];
  speaker: string | null;
  side: string | null; // 案件内角色，允许如 landlord / tenant 等具体案件标签
  actor: string | null;
  target: string | null;
  stance_target: string | null;
  created_at: string;
}
export interface ExtractionOutput { core_issue: string | null; issue_set: string[]; info_units: InfoUnit[]; }
export type RelationType = 'actor_of' | 'target_of' | 'about' | 'support' | 'contradict' | 'occur_at';
export interface Relation {
  id: string; source_id: string; target_id: string;
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
