import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';

// Optional browser regression, following the existing PLAYWRIGHT_MODULE setup.
// All API routes are local fixtures: no backend, case files or model calls.
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const copy = value => structuredClone(value);
const canvas = () => ({ visible_node_ids: [], selected_node_ids: [], focused_node_id: null,
  current_view_id: null, node_positions: {}, display_levels: {}, viewport: { zoom: 1, offset_x: 0, offset_y: 0 } });
const reasoning = () => ({ hypotheses: [], gaps: [], links: [], accepted_proposals: {}, investigation_runs: {} });
const workspace = () => ({ status: 'pending', canvas: canvas(), reasoning: reasoning() });
const caseData = id => ({ case_id: id, case_title: id, created_at: '2026-11-08', updated_at: '2026-11-08',
  files: [], core_issue: 'Issue A', issue_set: ['Issue A', 'Issue B'], info_units: [], relations: [],
  reasoning_views: [], law_matches: [], stage_runs: [], workspace_state: canvas() });

test('Agent Runs keep their Workspace owner across Issue and case switches', { timeout: 60000 }, async () => {
  const server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)),
    server: { host: '127.0.0.1', port: 0 } });
  await server.listen();
  let browser;
  try {
    browser = await chromium.launch({ headless: false, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(8000);
    const errors = [], unexpected = [], pending = [];
    page.on('pageerror', error => errors.push(error.message));
    let selectedCase = 'case-one', failRefresh = false, refreshCount = 0, holdRestoration = false;
    const delayedRestorations = [], savedResults = {};
    savedResults[JSON.stringify(['case-one', 'Issue A'])] = {
      goal: '修复前的调查', answer: '这是已经保存但未显示的答复。', answer_basis: 'general',
      run_id: 'investigation-' + 'a'.repeat(32), run_status: 'incomplete', finish_status: 'validated',
      summary_status: 'completed', completion_status: 'evidence_insufficient',
      stop_reason: 'finish_investigation', tool_call_count: 1, relevant_info_unit_ids: [], relevant_relation_ids: [],
      evidence: [], evidence_refs: [], open_questions: [], proposals: [], workspace_commit: { status: 'no_change', changes: [] } };
    const cases = Object.fromEntries(['case-one', 'case-two'].map(id => [id, caseData(id)]));
    const states = Object.fromEntries(Object.keys(cases).map(id => [id, { active_issue: 'Issue A',
      workspaces: { 'Issue A': workspace(), 'Issue B': workspace() } }]));
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url()), method = route.request().method();
      const path = decodeURIComponent(url.pathname);
      const reply = json => route.fulfill({ json: copy(json) });
      if (method === 'OPTIONS') return route.fulfill({ status: 204 });
      if (path === '/api/case') return reply({ case: cases[selectedCase] });
      if (path === '/api/cases') return reply({ cases: Object.keys(cases).map(id => ({ ...cases[id], title: id,
        is_current: id === selectedCase })), current_case_id: selectedCase });
      const match = path.match(/^\/api\/cases\/(case-one|case-two)(.*)$/);
      if (match) {
        const [, caseId, action] = match;
        if (action === '/select') { selectedCase = caseId; return reply({ case: cases[caseId] }); }
        if (!action) return reply({ case: cases[caseId] });
        if (action === '/agent/latest') {
          const result = copy(savedResults[JSON.stringify([caseId, url.searchParams.get('issue') ?? ''])] ?? null);
          if (holdRestoration) return new Promise(resolve => delayedRestorations.push(async () => {
            await reply({ result }); resolve();
          }));
          return reply({ result });
        }
        if (action === '/issue-workspaces') {
          if (method === 'PATCH') {
            const { issue, ...patch } = route.request().postDataJSON();
            Object.assign(states[caseId].workspaces[issue], patch);
            states[caseId].active_issue = issue;
          } else {
            refreshCount++;
            if (failRefresh) { failRefresh = false; return route.fulfill({ status: 500, json: { detail: 'fixture refresh failure' } }); }
          }
          return reply(states[caseId]);
        }
        if (action === '/agent/run') {
          const input = route.request().postDataJSON();
          assert.match(input.run_id, /^investigation-[0-9a-f]{32}$/);
          // Hold the HTTP response until the test explicitly releases it.
          return new Promise(resolve => pending.push({ caseId, input, async finish(status = 'no_change') {
            if (status === 'error') await route.fulfill({ status: 502, json: { detail: 'fixture model failure' } });
            else {
              if (status === 'applied') {
                const target = states[caseId].workspaces[input.current_issue];
                const hypothesisId = `hyp-${input.run_id}`;
                target.reasoning.hypotheses.push({ id: hypothesisId, content: '仅属于 A 的新假设',
                  judgment: 'unverified', work_status: 'active', revision: 1, updated_at: '2026-11-08',
                  last_edited_by: 'agent_accepted', proposal_key: `${input.run_id}:p1` });
                target.canvas.visible_node_ids.push(hypothesisId);
                target.canvas.node_positions[hypothesisId] = { x: 30, y: 100 };
              }
              const result = { goal: input.goal, answer: `答复：${input.goal}`, answer_basis: 'general',
                run_id: input.run_id, run_status: 'completed', finish_status: 'validated', summary_status: 'completed',
                completion_status: 'completed', stop_reason: 'finish_investigation', tool_call_count: 1,
                relevant_info_unit_ids: [], relevant_relation_ids: [], evidence: [], evidence_refs: [],
                open_questions: [], proposals: [], workspace_commit: { status, changes: [] } };
              savedResults[JSON.stringify([caseId, input.current_issue ?? ''])] = copy(result);
              await reply(result);
            }
            resolve();
          } }));
        }
      }
      unexpected.push(`${method} ${path}`);
      return route.fulfill({ status: 500, json: { detail: 'Unexpected fixture request' } });
    });
    await page.goto(server.resolvedUrls.local[0]);
    const tab = issue => page.getByRole('tab', { name: issue, exact: true });
    const send = page.getByRole('button', { name: '发送调查问题' });
    const panel = page.getByRole('region', { name: 'Agent 调查结果' });
    const input = page.getByPlaceholder('提出调查问题，或检查假设与证据缺口…');
    const saved = () => page.waitForFunction(() => !document.querySelector('.workspace-save-state')?.textContent);
    async function switchIssue(issue) {
      await tab(issue).click();
      await page.waitForFunction(issue => [...document.querySelectorAll('[role=tab]')]
        .some(tab => tab.title === issue && tab.getAttribute('aria-selected') === 'true'), issue);
      await saved();
    }
    async function start(goal) {
      if (!await input.isVisible()) await page.getByRole('button', { name: 'Agent 调查', exact: true }).click();
      await input.fill(goal);
      const count = pending.length;
      await send.click();
      await assertEventually(() => pending.length === count + 1);
      return pending.at(-1);
    }
    async function assertEventually(predicate) {
      const deadline = Date.now() + 8000;
      while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20));
      assert.ok(predicate());
    }
    await tab('Issue A').waitFor();
    await saved();
    await panel.getByText('这是已经保存但未显示的答复。', { exact: true }).waitFor();
    assert.equal(await send.isEnabled(), true);
    const a = await start('调查 A');
    assert.equal(a.input.current_issue, 'Issue A');
    await switchIssue('Issue B');
    assert.equal(await panel.count(), 0);
    const b = await start('调查 B');
    assert.equal(b.input.current_issue, 'Issue B');
    await switchIssue('Issue A');
    assert.equal(await send.isDisabled(), true); // A is still running; it cannot be submitted twice.
    await switchIssue('Issue B');
    const beforeB = copy(states['case-one'].workspaces['Issue B']);
    await a.finish('applied');
    await assertEventually(() => refreshCount >= 2);
    assert.equal(await panel.count(), 0); // A must never appear as B's result.
    assert.equal(await send.isDisabled(), true); // A's completion must not clear B's busy state.
    await b.finish();
    await panel.getByText('答复：调查 B', { exact: true }).waitFor();
    assert.deepEqual(states['case-one'].workspaces['Issue B'], beforeB);
    await switchIssue('Issue A');
    await panel.getByText('答复：调查 A', { exact: true }).waitFor();
    await page.locator(`[data-node-id="hyp-${a.input.run_id}"]`).waitFor();
    assert.equal(await send.isEnabled(), true);

    const failed = await start('A 失败');
    await switchIssue('Issue B');
    await failed.finish('error');
    await switchIssue('Issue A');
    await page.getByText('fixture model failure', { exact: true }).waitFor();
    assert.equal(await send.isEnabled(), true);
    const noChange = await start('A 无变化');
    const reads = refreshCount;
    await noChange.finish();
    await panel.getByText('答复：A 无变化', { exact: true }).waitFor();
    assert.equal(refreshCount, reads); // No state write requires no reasoning refresh.

    const refreshFailure = await start('A 刷新失败');
    failRefresh = true;
    await refreshFailure.finish('applied');
    await panel.getByText('答复：A 刷新失败', { exact: true }).waitFor();
    await page.getByText(/调查结果已保存，但工作区刷新失败/).waitFor();
    assert.equal(await send.isEnabled(), true);

    const offCase = await start('原案件的调查');
    async function switchCase(caseId) {
      await page.locator('.case-selector-trigger').click();
      await page.locator('.case-selector-item').filter({ hasText: caseId }).click();
      await tab('Issue A').waitFor();
      await saved();
    }
    await switchCase('case-two');
    const otherCase = await start('其他案件调查');
    await offCase.finish('applied');
    assert.equal(await panel.count(), 0);
    assert.equal(await send.isDisabled(), true);
    await otherCase.finish();
    await panel.getByText('答复：其他案件调查', { exact: true }).waitFor();
    await switchCase('case-one');
    await panel.getByText('答复：原案件的调查', { exact: true }).waitFor();
    await page.locator(`[data-node-id="hyp-${offCase.input.run_id}"]`).waitFor();
    const requestsBeforeReload = pending.length;
    await page.reload();
    await panel.getByText('答复：原案件的调查', { exact: true }).waitFor();
    assert.equal(pending.length, requestsBeforeReload); // Restore is a read, never an Agent re-run.

    holdRestoration = true;
    await page.reload();
    await tab('Issue A').waitFor();
    await assertEventually(() => delayedRestorations.length === 1);
    const newerRun = await start('新调查优先于迟到的历史结果');
    await delayedRestorations[0]();
    assert.equal(await panel.count(), 0);
    assert.equal(await send.isDisabled(), true);
    await newerRun.finish();
    await panel.getByText('答复：新调查优先于迟到的历史结果', { exact: true }).waitFor();
    assert.equal(new Set(pending.map(run => run.input.run_id)).size, pending.length);
    assert.deepEqual(unexpected, []);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
