// Headed UI -> real API/loop with Case A and mocked model responses only.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');
const assert = require('node:assert/strict');
const api = 'http://127.0.0.1:8018';

(async () => {
  const browser = await chromium.launch({ headless: false });
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.request.post(`${api}/api/cases/case-4325561f/select`);
    const before = (await (await page.request.get(`${api}/api/case`)).json()).case;
    const issue = before.issue_set[1];
    const url = `${api}/api/cases/${before.case_id}/issue-workspaces`;
    const workspace = (await (await page.request.get(url)).json()).workspaces[issue];
    await page.request.patch(url, { data: { issue, canvas: { ...workspace.canvas,
      visible_node_ids: ['U029', 'U036'], selected_node_ids: ['U029'], focused_node_id: 'U029',
      node_positions: { U029: { x: 120, y: 120 }, U036: { x: 560, y: 120 } },
      viewport: { zoom: 1, offset_x: 0, offset_y: 0 } } } });
    await page.goto('http://127.0.0.1:5178', { waitUntil: 'domcontentloaded' });
    await page.getByRole('tab', { name: issue, exact: true }).waitFor();
    await page.locator('.canvas-node[data-node-id="U029"]').waitFor();
    await page.getByRole('button', { name: 'Agent 调查', exact: true }).click();
    await page.getByPlaceholder('提出调查问题，或检查假设与证据缺口…').fill('检查同轮证据关联');
    const request = page.waitForRequest(r => r.url().endsWith('/agent/run'));
    const response = page.waitForResponse(r => r.url().endsWith('/agent/run') && r.request().method() === 'POST');
    await page.getByRole('button', { name: '发送调查问题' }).click();
    const payload = (await request).postDataJSON();
    assert.deepEqual(payload.selected_info_unit_ids, ['U029']);
    assert.equal(payload.focused_object_id, 'U029');
    const result = await (await response).json();
    assert.equal(result.proposals.length, 5);
    assert.equal(result.workspace_commit.status, 'applied', JSON.stringify(result));
    const history = page.getByRole('region', { name: '调查变更记录' });
    await history.getByRole('button', { name: '撤销本轮变更', exact: true }).waitFor();
    const current = async () => (await (await page.request.get(url)).json()).workspaces[issue].reasoning;
    const state = await current();
    assert.equal(state.hypotheses.length, 2);
    assert.equal(state.links.length, 3);
    assert.equal(await page.locator('[data-reasoning-link-id]').count(), 3);
    assert.equal(await page.locator('.reasoning-candidate').count(), 0);
    assert.equal(await page.getByRole('button', { name: /采纳|忽略建议/ }).count(), 0);
    await history.getByRole('button', { name: '撤销本轮变更', exact: true }).click();
    await history.getByRole('button', { name: '恢复本轮变更', exact: true }).waitFor();
    assert.equal((await current()).hypotheses.length, 0);
    assert.equal((await current()).links.length, 0);
    await history.getByRole('button', { name: '恢复本轮变更', exact: true }).click();
    await history.getByRole('button', { name: '撤销本轮变更', exact: true }).waitFor();
    assert.deepEqual((await current()).links, state.links);
    await page.reload();
    await page.getByRole('button', { name: 'Agent 调查', exact: true }).click();
    await history.getByRole('button', { name: '撤销本轮变更', exact: true }).waitFor();
    await history.getByRole('button', { name: '查看与编辑', exact: true }).first().click();
    const editor = page.locator('.reasoning-editor');
    await editor.getByLabel('假设内容', { exact: true }).fill('用户修正：仍需独立材料核查。');
    await editor.getByRole('button', { name: '保存推理', exact: true }).click();
    await editor.waitFor({ state: 'hidden' });
    await history.getByRole('button', { name: '撤销本轮变更', exact: true }).click();
    await history.getByRole('alert').getByText(/已有后续编辑或依赖/).waitFor();
    assert.ok((await current()).hypotheses.some(h => h.content === '用户修正：仍需独立材料核查。'));
    const after = (await (await page.request.get(`${api}/api/case`)).json()).case;
    for (const field of ['info_units', 'relations', 'workspace_state']) assert.deepEqual(after[field], before[field]);
    assert.deepEqual(errors, []);
    console.log('PASS: 2 Hypotheses + 3 typed Links auto-commit; undo/restore/reload; user edits block stale undo; shared case unchanged.');
  } catch (error) {
    if (page) await page.screenshot({ path: 'tmp/evidence-link-proposals-failure.png' }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
