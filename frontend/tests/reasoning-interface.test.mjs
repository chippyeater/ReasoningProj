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
  findings: [], limitations: [], open_questions: [], relevant_info_unit_ids: [], relevant_relation_ids: [],
  summary_status: 'completed', completion_status: 'evidence_insufficient', answer_basis: 'case_material',
  goal: '核对收款', answer: '实际收款尚待核实。' };
const proposal = { proposal_id: 'proposal-test', kind: 'hypothesis', content: '房东少收租金',
  rationale: '需要核对计租与收款。', evidence_refs: [], info_unit_ids: ['info-rent'], hypothesis_ids: [] };
const props = { state, caseData, dismissed: [], busy: false, error: '',
  onAccept() {}, onIgnore() {}, onLocate() {} };

test('proposal list explains approval once and marks source scope in the evidence control', () => {
  const html = render(ReasoningSuggestions, { ...props, result: { ...result, proposals: [proposal,
    { ...proposal, proposal_id: 'proposal-gap', kind: 'evidence_gap', content: '是否足额收款？',
      basis_info_unit_ids: ['info-rent'], hypothesis_ids: ['hyp-existing'] }] } });
  assert.equal(html.split('采纳不代表命题已被证实').length - 1, 1);
  assert.equal(html.split('未附原文引用').length - 1, 2);
  assert.equal(html.split('租金已足额收取').length - 1, 1);
  assert.match(html, /采纳建议/);
});

test('Link target is shown once while a pending parent still blocks approval', () => {
  const parent = { ...proposal, proposal_id: 'proposal-parent', content: '待确认命题' };
  const link = { ...proposal, proposal_id: 'proposal-link', kind: 'evidence_link', content: null,
    meaning: 'support', hypothesis_proposal_ids: [parent.proposal_id] };
  const html = render(ReasoningSuggestions, { ...props, result: { ...result, proposals: [parent, link] }, dismissed: [0] });
  assert.equal(html.split('待确认命题').length - 1, 1);
  assert.match(html, /请先采纳关联假设/);
  assert.match(html, /disabled=""/);
});

test('result has one missing-source warning and metadata answers do not imply missing evidence', () => {
  const html = render(InvestigationResultPanel, { result, infoUnits: [], relations: [], onLocate() {}, onClose() {} });
  assert.equal(html.split('本轮尚未提交可定位的原文依据').length - 1, 1);
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
