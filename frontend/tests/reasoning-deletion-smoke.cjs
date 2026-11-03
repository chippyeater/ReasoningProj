// Headed UI against serve_agent_smoke.py's isolated, offline case.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');
const assert = require('node:assert/strict');
const api = 'http://127.0.0.1:8018';

(async () => {
  const browser = await chromium.launch({ headless: false, executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(12000);
  try {
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.request.post(`${api}/api/cases/case-4325561f/select`);
    const before = (await (await page.request.get(`${api}/api/case`)).json()).case;
    const issue = before.issue_set[1];
    const stateUrl = `${api}/api/cases/${before.case_id}/issue-workspaces`;
    const current = async () => (await (await page.request.get(stateUrl)).json()).workspaces[issue];
    const saved = () => page.waitForFunction(() => !document.querySelector('.workspace-save-state')?.textContent);
    await page.goto('http://127.0.0.1:5178', { waitUntil: 'domcontentloaded' });
    await page.getByRole('tab', { name: issue, exact: true }).click();
    await page.locator('.canvas-node').first().waitFor(); await saved();
    const canvas = page.locator('.canvas-wrapper');
    const editor = page.locator('.reasoning-editor');
    async function command(name) {
      await canvas.click({ button: 'right', position: { x: 25, y: 25 } });
      await page.getByRole('menuitem', { name, exact: true }).click();
    }
    async function save() {
      await editor.getByRole('button', { name: '保存推理', exact: true }).click();
      await editor.waitFor({ state: 'hidden' }); await saved();
    }
    async function open(id) {
      await command('适应全部节点'); await saved();
      await page.locator(`.canvas-node[data-node-id="${id}"]`).getByRole('button', { name: '查看与编辑' }).click();
    }
    async function hypothesis(content) {
      await command('新建假设');
      await editor.getByLabel('假设内容', { exact: true }).fill(content); await save();
      return (await current()).reasoning.hypotheses.find(h => h.content === content).id;
    }
    const aText = '存在空置损失，待核实。', bText = '租金连续，不存在空置损失，待核实。';
    const a = await hypothesis(aText), b = await hypothesis(bText);
    async function gap(content, shared) {
      await open(a);
      await editor.getByRole('button', { name: '添加证据缺口', exact: true }).click();
      await editor.getByLabel('缺口问题', { exact: true }).fill(content);
      if (shared) await editor.getByLabel(bText, { exact: true }).check();
      await editor.getByText('缺口形成依据（可选）', { exact: true }).click();
      await editor.getByLabel(before.info_units.find(u => u.id === 'U026').title, { exact: true }).check();
      await save();
      return (await current()).reasoning.gaps.find(g => g.content === content).id;
    }
    const sharedId = await gap('核实计租日和收款记录。', true);
    const exclusiveId = await gap('核实仅与第一项解释有关的事实。', false);
    await open(a);
    await editor.getByRole('button', { name: '关联已有证据', exact: true }).click(); await save();
    await open(sharedId);
    await editor.getByRole('button', { name: '添加回应材料', exact: true }).click();
    const addressing = editor.getByRole('group', { name: '选择回应材料', exact: true });
    await addressing.getByLabel(before.info_units.find(u => u.id === 'U029').title, { exact: true }).check();
    await save();
    const baseline = await current();
    // Reproduce the reported viewport and force long content through real UI.
    await open(sharedId);
    await page.setViewportSize({ width: 718, height: 722 });
    await editor.getByText('缺口形成依据（可选）', { exact: true }).click();
    await editor.getByRole('button', { name: '添加回应材料', exact: true }).click();
    async function layout() {
      return editor.evaluate(el => {
        const body = el.querySelector('.reasoning-editor-body');
        const rect = el.querySelector('footer').getBoundingClientRect();
        const save = [...el.querySelectorAll('button')].find(b => b.textContent === '保存推理').getBoundingClientRect();
        return { overflow: getComputedStyle(body).overflowY, height: body.clientHeight, scrollHeight: body.scrollHeight,
          top: rect.top, bottom: rect.bottom, saveTop: save.top, saveBottom: save.bottom, viewport: innerHeight };
      });
    }
    for (const size of [{ width: 718, height: 722 }, { width: 390, height: 520 }]) {
      await page.setViewportSize(size);
      const state = await layout();
      assert.equal(state.overflow, 'scroll'); assert.ok(state.scrollHeight > state.height);
      assert.ok(state.top >= 0 && state.bottom <= state.viewport);
      assert.ok(state.saveTop >= 0 && state.saveBottom <= state.viewport);
    }
    await page.setViewportSize({ width: 718, height: 722 });
    const scrollBody = editor.locator('.reasoning-editor-body');
    const box = await scrollBody.boundingBox();
    const initial = await scrollBody.evaluate(el => el.scrollTop);
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2); await page.mouse.wheel(0, 650);
    await page.waitForFunction(initial => document.querySelector('.reasoning-editor-body').scrollTop > initial, initial);
    assert.ok((await layout()).bottom <= 722);
    await page.screenshot({ path: 'tmp/reasoning-editor-scroll.png' });
    await save();
    assert.deepEqual((await current()).reasoning, baseline.reasoning);
    // Cancellation and server failure both retain the object and the editor.
    await page.setViewportSize({ width: 1440, height: 1000 });
    await open(sharedId);
    page.once('dialog', dialog => dialog.dismiss());
    await editor.getByRole('button', { name: '删除证据缺口', exact: true }).click();
    assert.deepEqual((await current()).reasoning, baseline.reasoning);
    await page.route('**/issue-reasoning', route => route.fulfill({ status: 500, json: { detail: 'offline failure' } }));
    page.once('dialog', dialog => dialog.accept());
    await editor.getByRole('button', { name: '删除证据缺口', exact: true }).click();
    await editor.getByRole('alert').waitFor();
    assert.deepEqual((await current()).reasoning, baseline.reasoning);
    await page.unroute('**/issue-reasoning');
    await editor.getByRole('button', { name: '关闭推理编辑器', exact: true }).click();
    // Hypothesis deletion explicitly confirms dependent cleanup; shared Gap survives.
    await open(a);
    let confirmation;
    page.once('dialog', async dialog => { confirmation = dialog.message(); await dialog.accept(); });
    await editor.getByRole('button', { name: '删除假设', exact: true }).click();
    await editor.waitFor({ state: 'hidden' }); await saved();
    assert.match(confirmation, /1 条证据关联和 1 个仅关联此假设的证据缺口/);
    assert.match(confirmation, /1 个共享缺口会保留/);
    let work = await current();
    assert.deepEqual(work.reasoning.hypotheses.map(h => h.id), [b]);
    assert.equal(work.reasoning.links.length, 0);
    const shared = work.reasoning.gaps.find(g => g.id === sharedId);
    const old = baseline.reasoning.gaps.find(g => g.id === sharedId);
    assert.deepEqual(shared.hypothesis_ids, [b]); assert.equal(shared.revision, old.revision + 1);
    for (const field of ['content', 'basis_info_unit_ids', 'addressing_info_unit_ids', 'status']) assert.deepEqual(shared[field], old[field]);
    for (const id of [a, exclusiveId]) {
      assert.equal(await page.locator(`.canvas-node[data-node-id="${id}"]`).count(), 0);
      assert.ok(!work.canvas.visible_node_ids.includes(id)); assert.ok(!work.canvas.selected_node_ids.includes(id));
      assert.ok(!(id in work.canvas.node_positions)); assert.notEqual(work.canvas.focused_node_id, id);
    }
    await page.reload(); await page.locator(`.canvas-node[data-node-id="${sharedId}"]`).waitFor(); await saved();
    assert.deepEqual((await current()).reasoning, work.reasoning);
    await open(sharedId);
    page.once('dialog', dialog => dialog.accept());
    await editor.getByRole('button', { name: '删除证据缺口', exact: true }).click();
    await editor.waitFor({ state: 'hidden' }); await saved();
    work = await current(); assert.equal(work.reasoning.gaps.length, 0); assert.equal(work.reasoning.hypotheses[0].id, b);
    assert.equal(await page.locator(`.canvas-node[data-node-id="${sharedId}"]`).count(), 0);
    await open(b);
    page.once('dialog', dialog => dialog.accept());
    await editor.getByRole('button', { name: '删除假设', exact: true }).click();
    await editor.waitFor({ state: 'hidden' }); await saved();
    await page.reload(); await page.locator('.canvas-node').first().waitFor(); await saved();
    assert.equal((await current()).reasoning.hypotheses.length, 0);
    const after = (await (await page.request.get(`${api}/api/case`)).json()).case;
    for (const field of ['files', 'info_units', 'relations', 'workspace_state']) assert.deepEqual(after[field], before[field]);
    assert.deepEqual(errors, []);
    console.log('PASS: manual Hypothesis/Gap deletion, confirmation/cancel/failure/reload, shared Gap and sources preserved; scroll and visible footer at 718x722 and 390x520.');
  } catch (error) {
    await page.screenshot({ path: 'tmp/reasoning-deletion-failure.png' }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
