// Visible browser, existing isolated smoke backend. Model calls stay mocked.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({ headless: false });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.request.post('http://127.0.0.1:8018/api/cases/case-4325561f/select');
    const seed = (await (await page.request.get('http://127.0.0.1:8018/api/case')).json()).case;
    await page.request.patch(`http://127.0.0.1:8018/api/cases/${seed.case_id}/workspace`, { data: {
      ...seed.workspace_state, selected_node_ids: [], focused_node_id: null,
      viewport: { zoom: 1, offset_x: 0, offset_y: 0 },
    } });
    await page.goto('http://127.0.0.1:5178');
    await page.waitForFunction(() => !document.querySelector('.canvas-agent-shortcut')?.disabled);
    const before = await (await page.request.get('http://127.0.0.1:8018/api/case')).json();
    const issues = [...new Set([before.case.core_issue, ...before.case.issue_set].filter(Boolean))];
    assert.equal(await page.locator('.canvas-edit-toolbar').count(), 0);
    const issueTrigger = page.getByLabel('切换当前 Issue');
    assert.equal(await issueTrigger.innerText(), issues[0]);
    let caseReloads = 0;
    page.on('request', request => { if (request.url().endsWith('/api/case')) caseReloads++; });
    await issueTrigger.click();
    const list = page.getByRole('group', { name: 'Issue 列表' });
    assert.equal(await list.getByRole('button').count(), issues.length);
    await list.getByRole('button', { name: issues[1], exact: true }).click();
    assert.equal(await issueTrigger.innerText(), issues[1]);
    assert.equal(caseReloads, 0);
    const afterIssue = await (await page.request.get('http://127.0.0.1:8018/api/case')).json();
    assert.deepEqual(afterIssue, before);
    const canvas = page.locator('.canvas-wrapper');
    const blank = { x: 30, y: 870 };
    await canvas.click({ button: 'right', position: blank });
    const menu = page.getByRole('menu', { name: '画布操作' });
    assert.equal(await menu.count(), 1);
    assert.equal(await menu.getByRole('menuitem').first().innerText(), '向 Agent 提问');
    assert.equal(await page.locator('.dynamic-island').count(), 0);
    assert.equal(await menu.getByRole('menuitem', { name: '编辑选中节点' }).count(), 0);
    await page.screenshot({ path: 'tmp/canvas-issue-menu.png' });
    await menu.getByRole('menuitem', { name: '向 Agent 提问' }).click();
    assert.equal(await menu.count(), 0);
    assert.equal(await page.locator('.dynamic-island').count(), 1);
    assert.match(await page.locator('.island-context').innerText(), new RegExp(issues[1]));
    const input = page.getByPlaceholder('提出调查问题（当前仅支持只读调查）…');
    await input.fill('核对损失说法');
    const request = page.waitForRequest(request => request.url().endsWith('/agent/run'));
    await page.getByRole('button', { name: '发送调查问题' }).click();
    const sent = (await request).postDataJSON();
    assert.match(sent.goal, new RegExp(issues[1]));
    assert.deepEqual(sent.selected_info_unit_ids, []);
    await page.getByRole('region', { name: 'Agent 调查结果' }).waitFor();
    // Right-click an unselected node; it becomes the context, without opening another island.
    await canvas.click({ position: blank });
    const node = page.locator('[data-node-id="U001"]');
    await node.click({ button: 'right' });
    assert.equal(await page.locator('.canvas-node.node-selected').getAttribute('data-node-id'), 'U001');
    await menu.getByRole('menuitem', { name: '向 Agent 提问' }).click();
    await input.fill('核对损失说法');
    const selectedRequest = page.waitForRequest(request => request.url().endsWith('/agent/run'));
    await page.getByRole('button', { name: '发送调查问题' }).click();
    assert.deepEqual((await selectedRequest).postDataJSON().selected_info_unit_ids, ['U001']);
    await page.getByRole('region', { name: 'Agent 调查结果' }).waitFor();
    await canvas.click({ position: blank });
    await node.click();
    await page.locator('[data-node-id="U002"]').click({ modifiers: ['Control'] });
    await node.click({ button: 'right' });
    assert.equal(await page.locator('.canvas-node.node-selected').count(), 2);
    await page.keyboard.press('Escape');
    assert.equal(await menu.count(), 0);
    // Contextual manual editing and relation operations are still reachable.
    await node.click({ button: 'right' });
    await menu.getByRole('menuitem', { name: '编辑选中节点' }).click();
    await page.getByRole('dialog', { name: '编辑信息节点' }).waitFor();
    await page.getByRole('button', { name: '关闭编辑器', exact: true }).click();
    await node.click({ button: 'right' });
    await menu.getByRole('menuitem', { name: '新建关系', exact: true }).click();
    await page.getByRole('dialog', { name: '新建关系' }).waitFor();
    await page.getByRole('button', { name: '关闭关系编辑器' }).click();
    await node.click({ button: 'right' });
    await menu.getByRole('menuitem', { name: '新建 InfoUnit', exact: true }).click();
    await page.getByRole('dialog', { name: '新建信息节点' }).waitFor();
    await page.getByRole('button', { name: '关闭编辑器', exact: true }).click();
    // Existing drag and zoom handlers keep operating.
    const box = await node.boundingBox();
    const position = await node.getAttribute('style');
    await page.mouse.move(box.x + 30, box.y + 30);
    await page.mouse.down(); await page.mouse.move(box.x + 65, box.y + 50); await page.mouse.up();
    assert.notEqual(await node.getAttribute('style'), position);
    const content = page.locator('.canvas-content');
    const oldTransform = await content.getAttribute('style');
    const bounds = await canvas.boundingBox();
    await page.mouse.move(bounds.x + 30, bounds.y + 870);
    await page.keyboard.down('Control'); await page.mouse.wheel(0, -100); await page.keyboard.up('Control');
    await page.waitForTimeout(250);
    assert.notEqual(await content.getAttribute('style'), oldTransform);
    await canvas.click({ button: 'right', position: blank });
    await menu.getByRole('menuitem', { name: '适应全部节点' }).click();
    await page.locator('.relation-edge-hit').first().dispatchEvent('contextmenu', {
      bubbles: true, cancelable: true, clientX: bounds.x + 80, clientY: bounds.y + 80,
    });
    await menu.getByRole('menuitem', { name: '编辑关系', exact: true }).click();
    await page.getByRole('dialog', { name: '关系详情与编辑' }).waitFor();
    await page.getByRole('button', { name: '关闭关系编辑器' }).click();
    // Verify extraction still reaches the existing extraction→relation handlers,
    // while intercepting both generation endpoints so no paid call is possible.
    const generationCalls = [];
    await page.route('**/api/extraction/run', route => {
      generationCalls.push('extraction');
      return route.fulfill({ json: { core_issue: before.case.core_issue, issue_set: before.case.issue_set, info_units: before.case.info_units } });
    });
    await page.route('**/api/relation/run', route => {
      generationCalls.push('relation');
      return route.fulfill({ json: { status: 'empty', added_relations: [], discarded_count: 0 } });
    });
    await canvas.click({ button: 'right', position: blank });
    await menu.getByRole('menuitem', { name: '开始提取' }).click();
    await page.waitForFunction(() => !document.querySelector('.canvas-context-menu'));
    await page.waitForTimeout(350);
    assert.deepEqual(generationCalls, ['extraction', 'relation']);
    // Empty case has a visible first-extraction call to action, even without materials.
    await page.locator('.case-selector > .case-selector-trigger').click();
    await page.getByRole('button', { name: '空白测试案件', exact: true }).click();
    await page.locator('.canvas-initial-action').waitFor();
    assert.equal(await page.locator('.canvas-initial-action').getByRole('button', { name: '开始提取' }).isVisible(), true);
    assert.match(await page.locator('.canvas-initial-action').innerText(), /先上传/);
    assert.equal(await issueTrigger.innerText(), '暂无 Issue');
    assert.deepEqual(errors, []);
    console.log('PASS: Issue navigation without reload/write, one context menu, island invocation, selected-node payload, multi-selection, editors, drag/zoom/fit and empty-case extraction entry.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
