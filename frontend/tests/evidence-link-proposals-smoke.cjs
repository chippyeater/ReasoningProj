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
    await page.goto('http://127.0.0.1:5178');
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
    await page.waitForFunction(() => document.querySelectorAll('.pending-reasoning-edge').length === 3);
    assert.equal(await page.locator('.reasoning-candidate').count(), 5);
    assert.equal(await page.locator('[data-reasoning-link-id]').count(), 0);
    for (const meaning of ['反对', '支持', '待核实'])
      assert.match(await page.locator('.pending-reasoning-edge').allTextContents().then(t => t.join(' ')), new RegExp(meaning));
    const candidate = index => page.locator(`.reasoning-candidate[data-proposal-index="${index}"]`);
    const suggestion = index => page.locator(`.reasoning-suggestion[data-proposal-index="${index}"]`);
    assert.ok(await candidate(2).getByRole('button', { name: '采纳', exact: true }).isDisabled());
    await candidate(2).getByText('预览建议依据', { exact: true }).click();
    assert.match(await candidate(2).innerText(), /张某陈述无损失/);
    await page.locator('.canvas-wrapper').click({ position: { x: 1200, y: 1100 } });
    const labels = await page.locator('.pending-reasoning-edge .edge-label-box').evaluateAll(nodes => nodes.map(n => {
      const box = n.getBoundingClientRect(); return { x: box.x, y: box.y, right: box.right, bottom: box.bottom };
    }));
    for (let i = 0; i < labels.length; i++) for (let j = i + 1; j < labels.length; j++)
      assert.ok(labels[i].right <= labels[j].x || labels[j].right <= labels[i].x
        || labels[i].bottom <= labels[j].y || labels[j].bottom <= labels[i].y, 'Pending relation labels must remain readable');
    await page.screenshot({ path: 'tmp/evidence-link-proposals-pending.png' });
    await page.getByRole('button', { name: 'Agent 调查', exact: true }).click();
    await page.locator('.reasoning-suggestions > details > summary').click();
    assert.match(await suggestion(2).innerText(), /证据关联.*反对/s);
    assert.match(await suggestion(2).innerText(), /→/);
    assert.match(await suggestion(2).innerText(), /先采纳关联假设/);
    await suggestion(0).getByRole('button', { name: '采纳建议', exact: true }).click();
    await suggestion(0).waitFor({ state: 'hidden' });
    assert.equal(await page.locator('.pending-reasoning-edge').count(), 3); // Parent adoption does not adopt Links.
    assert.ok(await suggestion(2).getByRole('button', { name: '采纳建议', exact: true }).isEnabled());
    assert.ok(await suggestion(3).getByRole('button', { name: '采纳建议', exact: true }).isDisabled());
    await suggestion(2).getByRole('button', { name: '采纳建议', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('[data-reasoning-link-id]').length === 1);
    assert.equal(await page.locator('.pending-reasoning-edge').count(), 2);
    await suggestion(1).getByRole('button', { name: '采纳建议', exact: true }).click();
    await suggestion(1).waitFor({ state: 'hidden' });
    await suggestion(3).getByRole('button', { name: '采纳建议', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('[data-reasoning-link-id]').length === 2);
    await suggestion(4).getByRole('button', { name: '忽略建议', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('.pending-reasoning-edge').length === 0);
    assert.equal(await page.locator('.reasoning-candidate').count(), 0);
    assert.equal(await page.locator('.canvas-link.reasoning-pending').count(), 0);
    const work = (await (await page.request.get(url)).json()).workspaces;
    assert.equal(work[issue].reasoning.hypotheses.length, 2);
    assert.deepEqual(work[issue].reasoning.links.map(l => l.meaning).sort(), ['oppose', 'support']);
    assert.equal(work[before.core_issue].reasoning.links.length, 0);
    await page.locator('.canvas-wrapper').click({ position: { x: 1200, y: 1100 } });
    await page.screenshot({ path: 'tmp/evidence-link-proposals-adopted.png' });
    await page.getByRole('tab', { name: before.core_issue, exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('[data-reasoning-link-id]').length === 0);
    assert.equal(await page.locator('[data-reasoning-link-id]').count(), 0);
    await page.getByRole('tab', { name: issue, exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('[data-reasoning-link-id]').length === 2);
    await page.reload();
    await page.waitForFunction(() => document.querySelectorAll('[data-reasoning-link-id]').length === 2);
    assert.equal(await page.locator('.pending-reasoning-edge').count(), 0);
    const after = (await (await page.request.get(`${api}/api/case`)).json()).case;
    for (const field of ['info_units', 'relations', 'workspace_state']) assert.deepEqual(after[field], before[field]);
    assert.deepEqual(errors, []);
    console.log('PASS: live selected/focused request, 2 Hypotheses + 3 typed pending Links, evidence preview, independent parent/Link adoption and ignore, stable endpoints, Issue isolation/reload, no shared data mutation.');
  } catch (error) {
    if (page) await page.screenshot({ path: 'tmp/evidence-link-proposals-failure.png' }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
