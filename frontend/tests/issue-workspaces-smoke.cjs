// Headed browser + isolated real API. Only the model boundary is mocked.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');
const assert = require('node:assert/strict');
const api = 'http://127.0.0.1:8018';

(async () => {
  const browser = await chromium.launch({ headless: false });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(12000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.request.post(`${api}/api/cases/case-4325561f/select`);
    const seed = (await (await page.request.get(`${api}/api/case`)).json()).case;
    const url = `${api}/api/cases/${seed.case_id}/issue-workspaces`;
    const [a, b] = seed.issue_set;
    const aIds = seed.info_units.filter(unit => unit.related_issue === a).map(unit => unit.id);
    const bIds = seed.info_units.filter(unit => unit.related_issue === b).map(unit => unit.id);
    // Seed a former manually positioned node, preserving the old canvas exactly.
    const legacy = { ...seed.workspace_state, node_positions: { ...seed.workspace_state.node_positions,
      U029: { x: 620, y: 400 } } };
    await page.request.patch(`${api}/api/cases/${seed.case_id}/workspace`, { data: legacy });
    const state = await (await page.request.get(url)).json();
    // Deterministic re-runs use freshly recommended visible sets, not Gold edits.
    for (const issue of [a, b]) {
      const canvas = state.workspaces[issue].canvas;
      canvas.visible_node_ids = issue === a ? aIds : bIds;
      canvas.selected_node_ids = []; canvas.focused_node_id = null;
      canvas.viewport = { zoom: 1, offset_x: 0, offset_y: 0 };
      canvas.node_positions = issue === a ? legacy.node_positions : {};
      await page.request.patch(url, { data: { issue, canvas, status: 'pending' } });
    }
    await page.request.patch(url, { data: { issue: a } });
    await page.goto('http://127.0.0.1:5178');
    const tab = name => page.getByRole('tab', { name, exact: true });
    await page.waitForFunction(() => document.querySelector('[role=tab][aria-selected=true]'));
    assert.equal(await page.getByRole('tab').count(), new Set(seed.issue_set).size);
    assert.equal(await tab(a).getAttribute('title'), a);
    assert.match(await tab(a).innerText(), /核心/);
    assert.equal(await tab(a).getAttribute('aria-selected'), 'true');
    const visibleIds = async () => (await page.locator('.canvas-node').evaluateAll(nodes => nodes.map(node => node.dataset.nodeId))).sort();
    assert.deepEqual(await visibleIds(), [...aIds].sort());
    assert.ok(aIds.length < seed.info_units.length);
    const status = page.getByLabel('当前 Issue 状态');
    assert.equal(await status.inputValue(), 'pending');
    await status.selectOption('investigating');
    async function saved() { await page.waitForFunction(() => !document.querySelector('.workspace-save-state')?.textContent); }
    await saved();
    const canvas = page.locator('.canvas-wrapper');
    const blank = { x: 30, y: 860 };
    const menu = page.getByRole('menu', { name: '画布操作' });
    async function command(name) {
      await canvas.click({ button: 'right', position: blank });
      await menu.getByRole('menuitem', { name, exact: true }).click();
    }
    // Add a shared InfoUnit belonging to B through the user menu.
    const sharedId = bIds.find(id => !aIds.includes(id));
    assert.ok(sharedId);
    const shared = seed.info_units.find(unit => unit.id === sharedId);
    await command('添加已有节点');
    const picker = page.getByRole('dialog', { name: '添加已有节点', exact: true });
    await picker.getByLabel('搜索案件节点').fill(shared.title);
    await picker.getByRole('button').filter({ hasText: shared.title }).click();
    await picker.getByRole('button', { name: '关闭', exact: true }).click();
    const node = page.locator(`[data-node-id="${sharedId}"]`);
    await node.waitFor();
    const box = await node.boundingBox();
    await page.mouse.move(box.x + 35, box.y + 30);
    await page.mouse.down(); await page.mouse.move(box.x + 105, box.y + 60, { steps: 5 }); await page.mouse.up();
    const bounds = await canvas.boundingBox();
    await page.mouse.move(bounds.x + 60, bounds.y + 850);
    await page.keyboard.down('Control'); await page.mouse.wheel(0, -100); await page.keyboard.up('Control');
    await saved();
    const beforeSwitch = (await (await page.request.get(url)).json()).workspaces[a].canvas;
    const styleA = await node.getAttribute('style');
    const viewportA = await page.locator('.canvas-content').getAttribute('style');
    assert.notEqual(beforeSwitch.viewport.zoom, 1);
    let caseReloads = 0;
    page.on('request', request => { if (request.url().endsWith('/api/case')) caseReloads++; });
    await tab(b).click();
    await saved();
    assert.deepEqual(await visibleIds(), [...bIds].sort());
    assert.notEqual(await node.getAttribute('style'), styleA);
    const stateB = (await (await page.request.get(url)).json()).workspaces[b].canvas;
    assert.equal(stateB.viewport.zoom, 1);
    await tab(a).click(); await saved();
    assert.equal(await node.getAttribute('style'), styleA);
    assert.equal(await page.locator('.canvas-content').getAttribute('style'), viewportA);
    assert.equal(caseReloads, 0);
    // Refresh restores the active Issue, status, membership, exact position/viewport.
    await page.reload();
    await page.waitForFunction(() => document.querySelector('[role=tab][aria-selected=true]'));
    await saved();
    assert.equal(await tab(a).getAttribute('aria-selected'), 'true');
    assert.equal(await status.inputValue(), 'investigating');
    assert.equal(await node.getAttribute('style'), styleA);
    assert.equal(await page.locator('.canvas-content').getAttribute('style'), viewportA);
    assert.deepEqual((await visibleIds()), [...aIds, sharedId].sort());
    // Bring the zoomed canvas into view before editing, using the retained menu.
    await command('适应全部节点'); await saved();
    // Edit the shared InfoUnit via the existing editor and check B's independent canvas.
    await node.click({ button: 'right' });
    assert.equal(await menu.count(), 1);
    await menu.getByRole('menuitem', { name: '编辑选中节点' }).click();
    const editor = page.getByRole('dialog', { name: '编辑信息节点' });
    await editor.getByLabel('Title', { exact: true }).fill('共享编辑验证');
    await editor.getByRole('button', { name: '保存', exact: true }).click();
    await editor.waitFor({ state: 'hidden' }); await saved();
    await tab(b).click(); await saved();
    assert.match(await node.innerText(), /共享编辑验证/);
    assert.deepEqual((await (await page.request.get(url)).json()).workspaces[b].canvas.node_positions[sharedId], { x: 120, y: 120 });
    await node.click({ button: 'right' });
    await menu.getByRole('menuitem', { name: '编辑选中节点' }).click();
    await editor.getByLabel('Title', { exact: true }).fill(shared.title);
    await editor.getByRole('button', { name: '保存', exact: true }).click();
    await editor.waitFor({ state: 'hidden' }); await saved();
    await tab(a).click(); await saved();
    // Hiding only removes membership; adding again restores its saved position.
    const beforeHide = await node.getAttribute('style');
    await node.click({ button: 'right' });
    await menu.getByRole('menuitem', { name: '从当前画布移除' }).click(); await saved();
    assert.equal(await node.count(), 0);
    await command('添加已有节点');
    await picker.getByLabel('搜索案件节点').fill(shared.title);
    await picker.getByRole('button').filter({ hasText: shared.title }).click();
    await picker.getByRole('button', { name: '关闭', exact: true }).click(); await saved();
    assert.equal(await node.getAttribute('style'), beforeHide);
    // Agent receives Issue context with selected IDs, retaining full-case retrieval.
    await node.click({ button: 'right' });
    await menu.getByRole('menuitem', { name: '向 Agent 提问' }).click();
    const input = page.getByPlaceholder('提出调查问题（当前仅支持只读调查）…');
    await input.fill('核对损失说法');
    const request = page.waitForRequest(request => request.url().endsWith('/agent/run'));
    await page.getByRole('button', { name: '发送调查问题' }).click();
    const sent = (await request).postDataJSON();
    assert.equal(sent.current_issue, a);
    assert.deepEqual(sent.selected_info_unit_ids, [sharedId]);
    const result = page.getByRole('region', { name: 'Agent 调查结果' });
    await result.waitFor();
    await result.getByRole('button', { name: '张某笔录.docx', exact: true }).first().click();
    await page.locator('[data-node-id="U029"].node-selected').waitFor();
    await saved();
    assert.equal(await status.inputValue(), 'investigating');
    // A late result must not appear under a different Issue of the same case.
    await page.route('**/agent/run', async route => {
      const response = await route.fetch();
      await new Promise(resolve => setTimeout(resolve, 600));
      await route.fulfill({ response });
    });
    const late = page.waitForResponse(response => response.url().endsWith('/agent/run'));
    await input.fill('核对损失说法');
    await page.getByRole('button', { name: '发送调查问题' }).click();
    await page.getByText('调查中…正在按需检索和核对依据。', { exact: true }).waitFor();
    await tab(b).click();
    await late; await page.waitForTimeout(100);
    assert.equal(await result.count(), 0);
    await page.unroute('**/agent/run');
    await tab(a).click(); await saved();
    // Explicit failure and retry, without losing the local canvas or state.
    await page.route('**/issue-workspaces', route => route.request().method() === 'PATCH'
      ? route.fulfill({ status: 500, json: { detail: '离线模拟保存失败' } }) : route.continue());
    await status.selectOption('closed');
    await page.getByRole('button', { name: '重试保存' }).waitFor();
    await tab(b).click();
    await page.getByRole('button', { name: '重试保存' }).waitFor();
    assert.equal(await tab(a).getAttribute('aria-selected'), 'true');
    await page.unroute('**/issue-workspaces');
    await page.getByRole('button', { name: '重试保存' }).click(); await saved();
    await status.selectOption('closed'); await saved();
    await page.reload(); await page.waitForFunction(() => document.querySelector('[role=tab][aria-selected=true]'));
    assert.equal(await status.inputValue(), 'closed');
    await status.selectOption('pending'); await saved();
    // Relationship rendering only connects visible endpoints.
    const visible = new Set(await visibleIds());
    for (const relationId of await page.locator('[data-relation-id]').evaluateAll(nodes => nodes.map(node => node.dataset.relationId))) {
      const relation = seed.relations.find(item => item.id === relationId);
      assert.ok(visible.has(relation.source_id) && visible.has(relation.target_id));
    }
    await command('恢复旧画布节点'); await saved();
    assert.equal(await page.locator('.canvas-node').count(), seed.info_units.length);
    const after = (await (await page.request.get(`${api}/api/case`)).json()).case;
    assert.deepEqual(after.workspace_state, legacy);
    assert.deepEqual(after.info_units, seed.info_units);
    assert.deepEqual(after.relations, seed.relations);
    await tab(b).click(); await saved();
    await command('适应全部节点'); await saved();
    await page.screenshot({ path: 'tmp/issue-workspaces-tabs.png' });
    await page.setViewportSize({ width: 620, height: 1000 });
    const overflow = await page.locator('.issue-tabs').evaluate(element => element.scrollWidth > element.clientWidth);
    assert.equal(overflow, true);
    assert.equal(await status.isVisible(), true);
    await tab(a).focus(); await page.keyboard.press('ArrowRight'); await saved();
    assert.equal(await tab(b).getAttribute('aria-selected'), 'true');
    assert.deepEqual(errors, []);
    console.log('PASS: recommended membership, add/drag/zoom, independent A/B layouts, refresh/status/reopen, shared edit, Agent context and evidence navigation, legacy preservation, filtered relations.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
