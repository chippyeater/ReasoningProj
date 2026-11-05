// Real Case A API/loop/cache verification; only model replies are mocked.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');
const assert = require('node:assert/strict');
const api = 'http://127.0.0.1:8018';

(async () => {
  const browser = await chromium.launch({ headless: false });
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.request.post(`${api}/api/cases/case-4325561f/select`);
    const before = (await (await page.request.get(`${api}/api/case`)).json()).case;
    const issue = before.issue_set[1];
    const url = `${api}/api/cases/${before.case_id}/issue-workspaces`;
    const work = (await (await page.request.get(url)).json()).workspaces[issue];
    await page.request.patch(url, { data: { issue, canvas: { ...work.canvas,
      visible_node_ids: ['U026', 'U029'], selected_node_ids: ['U026'], focused_node_id: 'U026' } } });
    await page.goto('http://127.0.0.1:5178', { waitUntil: 'domcontentloaded' });
    await page.getByRole('tab', { name: issue, exact: true }).waitFor();
    await page.locator('.canvas-node[data-node-id="U026"]').waitFor();
    await page.getByRole('button', { name: 'Agent 调查', exact: true }).click();
    const panel = page.getByRole('region', { name: 'Agent 调查结果' });
    const input = page.getByPlaceholder('提出调查问题，或检查假设与证据缺口…');
    async function ask(goal) {
      await input.fill(goal);
      const response = page.waitForResponse(r => r.url().endsWith('/agent/run') && r.request().method() === 'POST');
      await page.getByRole('button', { name: '发送调查问题' }).click();
      const result = await (await response).json();
      assert.equal(result.summary_status, 'completed', JSON.stringify(result));
      await panel.locator(':scope > .investigation-answer').waitFor();
      return result;
    }
    const result = await ask('检查同轮假设和缺口');
    assert.equal(result.workspace_commit.status, 'applied', JSON.stringify(result));
    assert.equal(result.proposals.length, 4);
    const history = page.getByRole('region', { name: '调查变更记录' });
    await history.getByRole('button', { name: '撤销本轮变更', exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: /采纳|忽略建议/ }).count(), 0);
    const state = (await (await page.request.get(url)).json()).workspaces[issue].reasoning;
    assert.equal(state.hypotheses.length, 2);
    assert.equal(state.gaps.length, 2);
    const ids = new Set(state.hypotheses.map(h => h.id));
    assert.ok(state.gaps.every(g => g.hypothesis_ids.every(id => ids.has(id))));
    const after = (await (await page.request.get(`${api}/api/case`)).json()).case;
    for (const field of ['info_units', 'relations', 'workspace_state']) assert.deepEqual(after[field], before[field]);
    assert.deepEqual(errors, []);
    console.log('PASS: autonomous proposals automatically commit related Hypotheses and Gaps; source scope and shared case preserved.');
  } catch (e) {
    if (page) await page.screenshot({ path: 'tmp/investigation-delivery-failure.png' }).catch(() => {});
    throw e;
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
