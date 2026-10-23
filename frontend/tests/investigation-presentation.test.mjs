import { test } from 'node:test';
import assert from 'node:assert/strict';
import { groupSources, displayText, presentQuestion, hypothesisProposalTargets } from '../src/components/investigationPresentation.ts';

const ref = Object.freeze({ info_unit_id: 'info-one', source_ref_index: 0,
  file_id: 'file-one', filename: '张某笔录.docx', quote: '他 3 月 8 号就直接搬进去住了',
  start: 78, end: 95, source_kind: 'testimony' });

test('Gap targets use original indices and exact run adoption rather than filtered rows', () => {
  const result = Object.freeze({ run_id: 'run-a', proposals: Object.freeze([
    { kind: 'hypothesis', content: '无关建议' },
    { kind: 'evidence_gap', hypothesis_proposal_indices: [2] },
    { kind: 'hypothesis', content: '与缺口关联的假设' },
  ]) });
  const state = { hypotheses: [{ id: 'h', proposal_key: 'run-b:2' }], accepted_proposals: { 'run-b:2': 'h' } };
  assert.deepEqual(hypothesisProposalTargets(result, state, 1), [{ title: '与缺口关联的假设', accepted: false }]);
  state.accepted_proposals['run-a:2'] = 'h';
  assert.equal(hypothesisProposalTargets(result, state, 1)[0].accepted, false);
  state.hypotheses[0].proposal_key = 'run-a:2';
  assert.equal(hypothesisProposalTargets(result, state, 1)[0].accepted, true);
  assert.deepEqual(hypothesisProposalTargets(result, state, 0), []); // Older outputs omit the new field.
  assert.equal(result.proposals[2].content, '与缺口关联的假设');
});

test('wrong proposal type, missing accepted object and invalid index never enable adoption', () => {
  const state = { hypotheses: [], accepted_proposals: { 'run-a:0': 'deleted' } };
  const result = { run_id: 'run-a', proposals: [{ kind: 'evidence_gap', hypothesis_proposal_indices: [0, 5] }] };
  assert.ok(hypothesisProposalTargets(result, state, 0).every(target => !target.accepted));
});

test('same exact source groups all pointers without changing originals', () => {
  const second = Object.freeze({ ...ref, info_unit_id: 'info-two', source_ref_index: 3 });
  const original = Object.freeze([ref, second]);
  const groups = groupSources(original);
  assert.equal(groups.length, 1);
  assert.deepEqual(groups[0].refs, [ref, second]);
  assert.equal(groups[0].refs[1].source_ref_index, 3);
});
test('different files with the same filename and quote remain distinct', () => {
  assert.equal(groupSources([ref, { ...ref, file_id: 'file-two' }]).length, 2);
});
test('different positions and nonidentical text must not be merged', () => {
  assert.equal(groupSources([ref, { ...ref, start: 100, end: 117 }, { ...ref, quote: '不同原文' }]).length, 3);
});
test('technical identifiers turn into labels with token boundaries', () => {
  assert.equal(displayText('info-one 与 U026 引用 file-one；info-missing；U0260',
    [{ id: 'info-one', title: '入住说法' }, { id: 'U026', title: '入住日期' }], [ref], []),
    '入住说法 与 入住日期 引用 张某笔录.docx；相关材料；相关材料');
});
test('runtime messages share plain language instead of exposing tool names', () => {
  assert.deepEqual(presentQuestion('get_evidence 返回 truncated，未取得完整结果。'),
    presentQuestion('search_info_units 返回 truncated，未取得完整结果。'));
  assert.match(presentQuestion('get_evidence 返回 error').text, /暂时无法读取/);
});
test('distinct substantive questions about budgets or incomplete records stay intact', () => {
  for (const question of ['房东的维修预算依据是什么？', '被截断的聊天内容是什么？', '是否有入住记录？'])
    assert.deepEqual(presentQuestion(question), { limit: false, text: question });
});
