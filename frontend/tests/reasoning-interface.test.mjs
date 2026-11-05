import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { build } from 'esbuild';

// Compile the actual TSX components with the existing frontend toolchain.
// Keep React external so render and component hooks use the same instance.
const compiled = await build({
  stdin: { contents: `export { ReasoningSuggestions } from './src/components/ReasoningSuggestions';
    export { ReasoningEditor } from './src/components/ReasoningEditor';
    export { InvestigationResultPanel } from './src/components/InvestigationResultPanel';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)) },
  bundle: true, write: false, platform: 'node', format: 'cjs', jsx: 'automatic',
  external: ['react', 'react/*'],
});
const mod = { exports: {} };
new Function('require', 'module', 'exports', compiled.outputFiles[0].text)(
  createRequire(new URL('../package.json', import.meta.url)), mod, mod.exports);
const { ReasoningSuggestions, ReasoningEditor, InvestigationResultPanel } = mod.exports;
const render = (component, props) => renderToStaticMarkup(createElement(component, props));
const state = { hypotheses: [{ id: 'hyp-existing', content: '租金已足额收取', judgment: 'unverified', work_status: 'active' }],
  gaps: [], links: [], accepted_proposals: {} };
const caseData = { info_units: [{ id: 'info-rent', title: '收款记录', source_refs: [] }], files: [], relations: [] };
const result = { run_id: 'investigation-test', proposals: [], evidence: [], evidence_refs: [],
  limitations: [], open_questions: [], relevant_info_unit_ids: [], relevant_relation_ids: [],
  summary_status: 'completed', completion_status: 'evidence_insufficient', answer_basis: 'case_material',
  goal: '核对收款', answer: '实际收款尚待核实。' };
const proposal = { proposal_id: 'proposal-test', kind: 'hypothesis', content: '房东少收租金',
  rationale: '需要核对计租与收款。', evidence_refs: [], info_unit_ids: ['info-rent'], hypothesis_ids: [] };
const props = { state, caseData, busy: false, error: '', onReverse() {}, onRetry() {}, onEdit() {}, onLocate() {} };
const change = { collection: 'hypotheses', object_id: 'hyp-agent', before: null,
  after: { id: 'hyp-agent', content: '房东少收租金', judgment: 'unverified', work_status: 'active' },
  proposal_keys: ['investigation-test:proposal-test'] };
const run = { status: 'applied', changes: [change], proposals: [proposal], evidence: [] };

test('committed changes show editing and undo without approval controls', () => {
  const html = render(ReasoningSuggestions, { ...props, result, state: { ...state,
    hypotheses: [...state.hypotheses, change.after], investigation_runs: { [result.run_id]: run } } });
  assert.match(html, /已统一写入内部推理对象/);
  assert.match(html, /查看与编辑/);
  assert.match(html, /撤销本轮变更/);
  assert.match(html, /未附原文引用/);
  assert.doesNotMatch(html, /采纳建议|忽略建议|请先采纳/);
});

test('failed and budget exhausted runs expose retained proposals without write controls', () => {
  for (const status of ['failed', 'conflict', 'budget_exhausted', 'pending']) {
    const html = render(ReasoningSuggestions, { ...props, result: { ...result,
      proposals: [proposal], workspace_commit: { status, changes: [] } } });
    assert.match(html, /未写入任何推理变更/);
    assert.match(html, /房东少收租金/);
    assert.doesNotMatch(html, /采纳|撤销本轮变更|恢复本轮变更/);
  }
});

test('history survives closing the result and offers restore after undo', () => {
  const html = render(ReasoningSuggestions, { ...props, result: null, state: { ...state,
    investigation_runs: { [result.run_id]: { ...run, status: 'undone' } } } });
  assert.match(html, /已撤销/);
  assert.match(html, /恢复本轮变更/);
  assert.doesNotMatch(html, /采纳|待确认/);
});

test('historical basis uses the committed source snapshot after case material changes', () => {
  const cited = { ...proposal, evidence_refs: [{ info_unit_id: 'info-rent', source_ref_index: 0 }] };
  const html = render(ReasoningSuggestions, { ...props, result: null,
    caseData: { ...caseData, info_units: [{ ...caseData.info_units[0], source_refs: [{ quote: '用户后来改过的来源' }] }] },
    state: { ...state, investigation_runs: { [result.run_id]: { ...run, proposals: [cited], evidence: [
      { info_unit_id: 'info-rent', source_ref_index: 0, source_ref: { quote: '运行时实际引用的原文' } }] } } } });
  assert.match(html, /运行时实际引用的原文/);
  assert.doesNotMatch(html, /用户后来改过的来源/);
});

test('result has one missing-source warning and metadata answers do not imply missing evidence', () => {
  const html = render(InvestigationResultPanel, { result, infoUnits: [], relations: [], onLocate() {}, onClose() {} });
  assert.equal(html.split('本轮尚未取得可定位的原文依据').length - 1, 1);
  assert.doesNotMatch(html, /主要发现尚未取得/);
  const general = render(InvestigationResultPanel, { result: { ...result, answer_basis: 'general' },
    infoUnits: [], relations: [], onLocate() {}, onClose() {} });
  assert.doesNotMatch(general, /原文依据|当事人陈述/);
});

test('Hypothesis and Gap editors distinguish a proposition from a factual question', () => {
  const common = { state, caseData, issue: '租金损失', busy: false, error: '', onSave() {}, onClose() {},
    onEdit() {}, onLocate() {}, onInvestigate() {} };
  const hypothesis = render(ReasoningEditor, { ...common, target: { kind: 'hypothesis' } });
  assert.match(hypothesis, /待验证命题/);
  assert.match(hypothesis, /无须配对/);
  const gap = render(ReasoningEditor, { ...common, target: { kind: 'gap', hypothesisId: 'hyp-existing' } });
  assert.match(gap, /待核实的事实问题/);
  assert.match(gap, /value="hyp-existing"|租金已足额收取/);
});

test('only a normal Finish with failed persistence offers a write retry', () => {
  for (const status of ['failed', 'conflict', 'pending', 'budget_exhausted']) {
    const html = render(ReasoningSuggestions, { ...props, result: { ...result, proposals: [proposal],
      workspace_commit: { status, changes: [] } } });
    assert.equal(html.includes('重试写入本轮变更'), status === 'failed');
  }
});

test('new and historical results show summary and sources while ignoring old Findings', () => {
  const evidence = { info_unit_id: 'info-rent', source_ref_index: 0, file_id: 'file-rent',
    filename: '收款凭证.txt', quote: '已收到租金。', start: 0, end: 6, source_kind: 'record' };
  for (const legacy of [{}, { findings: [{ statement: '历史 Finding 不再展示', info_unit_ids: ['info-rent'],
    relation_ids: [], evidence_refs: [{ info_unit_id: 'info-rent', source_ref_index: 0 }] }] }]) {
    const html = render(InvestigationResultPanel, { result: { ...result, ...legacy,
      evidence: [evidence], evidence_refs: [{ info_unit_id: 'info-rent', source_ref_index: 0 }],
      relevant_info_unit_ids: ['info-rent'], open_questions: ['核对其他月份收款。'] },
      infoUnits: caseData.info_units, relations: [], onLocate() {}, onClose() {} });
    assert.match(html, /实际收款尚待核实/);
    assert.match(html, /收款凭证.txt/);
    assert.match(html, /已收到租金/);
    assert.match(html, /核对其他月份收款/);
    assert.doesNotMatch(html, /历史 Finding|已核查的发现|investigation-finding/);
  }
});

test('unfinished result preserves failure and material display without Findings', () => {
  const html = render(InvestigationResultPanel, { result: { ...result, summary_status: 'failed',
    limitations: ['来源文件暂时无法读取。'], proposals: [proposal] },
    infoUnits: caseData.info_units, relations: [], onLocate() {}, onClose() {} });
  assert.match(html, /role="alert"/);
  assert.match(html, /已取得的材料与暂存提案仍可查看/);
  assert.match(html, /本轮未写入推理工作区/);
  assert.match(html, /部分材料暂时无法读取/);
  assert.doesNotMatch(html, /已核查的发现/);
});
