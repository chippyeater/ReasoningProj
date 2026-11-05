// Task 10231658: headed UI → real API/loop/audits with only model responses mocked.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');
const assert = require('node:assert/strict');
const api = 'http://127.0.0.1:8018';

(async () => {
  const browser = await chromium.launch({ headless: false });
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(12000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.request.post(`${api}/api/cases/case-4325561f/select`);
    const before = (await (await page.request.get(`${api}/api/case`)).json()).case;
    const issue = before.issue_set[1];
    const stateUrl = `${api}/api/cases/${before.case_id}/issue-workspaces`;
    await page.goto('http://127.0.0.1:5178', { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => document.querySelector('[role=tab][aria-selected=true]'));
    const tab = name => page.getByRole('tab', { name, exact: true });
    async function saved() { await page.waitForFunction(() => !document.querySelector('.workspace-save-state')?.textContent); }
    await tab(issue).click(); await saved();
    const canvas = page.locator('.canvas-wrapper');
    const menu = page.getByRole('menu', { name: '画布操作' });
    async function command(name) {
      await canvas.click({ button: 'right', position: { x: 30, y: 830 } });
      await menu.getByRole('menuitem', { name, exact: true }).click();
    }
    const current = async () => (await (await page.request.get(stateUrl)).json()).workspaces[issue];
    const editor = page.locator('.reasoning-editor');
    async function save() {
      await editor.getByRole('button', { name: '保存推理', exact: true }).click();
      await editor.waitFor({ state: 'hidden' }); await saved();
    }
    async function addHypothesis(text) {
      await command('新建假设');
      await editor.getByLabel('假设内容', { exact: true }).fill(text);
      await save();
      return (await current()).reasoning.hypotheses.find(obj => obj.content === text).id;
    }
    const h1 = await addHypothesis('房东存在实际空置损失，仍需独立证据核实。');
    const h2 = await addHypothesis('房东可能没有实际空置损失，仍需核对。');
    async function editHypothesis(id) {
      await page.locator(`[data-node-id="${id}"]`).getByRole('button', { name: '查看与编辑' }).click();
    }
    async function addLink(hid, uid, meaning) {
      await editHypothesis(hid);
      await editor.getByRole('button', { name: '关联已有证据', exact: true }).click();
      assert.equal(await editor.getByLabel('关联语义', { exact: true }).inputValue(), 'related');
      assert.equal(await editor.getByText('相关：材料与假设有关，但不明确支持或反对。', { exact: true }).isVisible(), true);
      await editor.getByLabel('关联案件信息', { exact: true }).selectOption(uid);
      await editor.getByLabel('关联语义', { exact: true }).selectOption(meaning);
      await save();
    }
    // A single source remains case-owned while supporting different reasoning.
    await addLink(h1, 'U026', 'support');
    await addLink(h2, 'U026', 'oppose');
    await addLink(h1, 'U029', 'related');
    let work = await current();
    assert.equal(work.reasoning.links.length, 3);
    assert.equal(await page.locator('.reasoning-support').count(), 1);
    assert.equal(await page.locator('.reasoning-oppose').count(), 1);
    assert.equal(await page.locator('.reasoning-related').count(), 1);
    // Existing line hit targets open semantic editing with exact source text.
    const firstLink = work.reasoning.links[0];
    await page.locator(`[data-reasoning-link-id="${firstLink.id}"] .relation-edge-hit`).dispatchEvent('click', { bubbles: true });
    await editor.getByLabel('关联语义', { exact: true }).selectOption('related');
    await editor.getByText('核对来源原文', { exact: true }).click();
    assert.match(await editor.innerText(), /张某笔录.docx/);
    assert.match(await editor.innerText(), /3 月 8 号/);
    await save();
    await page.locator(`[data-reasoning-link-id="${firstLink.id}"] .relation-edge-hit`).dispatchEvent('click', { bubbles: true });
    await editor.getByRole('button', { name: '解除关联', exact: true }).click();
    await editor.waitFor({ state: 'hidden' }); await saved();
    assert.equal((await current()).reasoning.links.length, 2);
    await addLink(h1, 'U026', 'related');
    await editHypothesis(h1);
    await editor.getByRole('button', { name: '添加证据缺口', exact: true }).click();
    await editor.getByLabel('缺口问题', { exact: true }).fill('是否存在替租人的实际入住记录和新租约？');
    assert.equal(await editor.getByRole('button', { name: '添加回应材料', exact: true }).count(), 0);
    await editor.getByLabel('房东可能没有实际空置损失，仍需核对。', { exact: true }).check();
    await editor.getByText('缺口形成依据（可选）', { exact: true }).click();
    const basisCheckbox = editor.getByLabel(before.info_units.find(u => u.id === 'U026').title, { exact: true });
    await basisCheckbox.check();
    assert.equal(await basisCheckbox.isChecked(), true);
    assert.equal(await editor.locator('label button').count(), 0);
    await basisCheckbox.uncheck();
    assert.equal(await basisCheckbox.isChecked(), false);
    assert.equal(await editor.getByRole('button', { name: '定位信息', exact: true }).count(), 0);
    await basisCheckbox.check();
    await save();
    work = await current();
    const gap = work.reasoning.gaps.at(-1);
    assert.deepEqual([...gap.hypothesis_ids].sort(), [h1, h2].sort());
    assert.deepEqual(gap.basis_info_unit_ids, ['U026']);
    assert.deepEqual(gap.addressing_info_unit_ids, []);
    await page.locator(`[data-node-id="${gap.id}"]`).getByRole('button', { name: '查看与编辑' }).click();
    await editor.getByText('缺口形成依据（可选）', { exact: true }).click();
    assert.equal(await basisCheckbox.isChecked(), true);
    assert.equal(await editor.locator('label button').count(), 0);
    await editor.getByRole('button', { name: '定位信息', exact: true }).click();
    await editor.waitFor({ state: 'hidden' }); await saved();
    await page.locator('[data-node-id="U026"].node-selected').waitFor();
    // Manage addressing material through the editor. Requests carry deltas;
    // basis, status, shared Hypotheses and case-owned sources remain independent.
    const beforeAddressing = (await current()).reasoning;
    const editGap = () => page.locator(`[data-node-id="${gap.id}"]`).getByRole('button', { name: '查看与编辑' }).click();
    await editGap();
    const addressing = editor.getByRole('region', { name: '用于回应缺口的信息' });
    const picker = addressing.getByRole('group', { name: '选择回应材料', exact: true });
    const title = id => before.info_units.find(u => u.id === id).title;
    const agentCheck = editor.getByRole('button', { name: '请 Agent 检查', exact: true });
    async function saveGapDelta(added, removed) {
      const request = page.waitForRequest(r => r.url().endsWith('/issue-reasoning') && r.method() === 'PATCH');
      await save();
      const payload = (await request).postDataJSON().gap;
      assert.deepEqual(payload.addressing_info_unit_ids, added);
      assert.deepEqual(payload.remove_addressing_info_unit_ids, removed);
    }
    await addressing.getByRole('button', { name: '添加回应材料', exact: true }).click();
    await picker.getByLabel(title('U027'), { exact: true }).check();
    await picker.getByLabel(title('U004'), { exact: true }).check();
    assert.equal(await agentCheck.isDisabled(), true);
    assert.deepEqual((await current()).reasoning.gaps.find(g => g.id === gap.id).addressing_info_unit_ids, []);
    await saveGapDelta(['U027', 'U004'], []);
    work = await current();
    const addressed = work.reasoning.gaps.find(g => g.id === gap.id);
    assert.deepEqual(addressed.basis_info_unit_ids, ['U026']);
    assert.deepEqual(addressed.addressing_info_unit_ids, ['U027', 'U004']);
    assert.equal(addressed.status, 'open');
    assert.equal(work.reasoning.hypotheses.find(h => h.id === h1).judgment, 'unverified');
    assert.equal(addressed.revision, gap.revision + 1);
    await editGap();
    assert.equal(await agentCheck.isEnabled(), true);
    await addressing.getByRole('button', { name: '添加回应材料', exact: true }).click();
    for (const uid of ['U027', 'U004']) {
      assert.equal(await picker.getByLabel(title(uid), { exact: true }).isChecked(), true);
      assert.equal(await picker.getByLabel(title(uid), { exact: true }).isDisabled(), true);
    }
    await addressing.getByRole('button', { name: '添加回应材料', exact: true }).click();
    await addressing.getByRole('button', { name: `移除关联：${title('U027')}`, exact: true }).click();
    assert.equal(await agentCheck.isDisabled(), true);
    // Undoing a removal restores a clean set even when its local order changes.
    await addressing.getByRole('button', { name: '添加回应材料', exact: true }).click();
    await picker.getByLabel(title('U027'), { exact: true }).check();
    assert.equal(await agentCheck.isEnabled(), true);
    await saveGapDelta([], []);
    assert.deepEqual((await current()).reasoning.gaps.find(g => g.id === gap.id), addressed);
    await editGap();
    await addressing.getByRole('button', { name: `移除关联：${title('U027')}`, exact: true }).click();
    assert.equal(await agentCheck.isDisabled(), true);
    await saveGapDelta([], ['U027']);
    await page.reload();
    await page.locator(`[data-node-id="${gap.id}"]`).waitFor(); await saved();
    work = await current();
    const remaining = work.reasoning.gaps.find(g => g.id === gap.id);
    assert.deepEqual(remaining.addressing_info_unit_ids, ['U004']);
    for (const field of ['id', 'content', 'basis_info_unit_ids', 'hypothesis_ids', 'status'])
      assert.deepEqual(remaining[field], gap[field]);
    assert.equal(remaining.revision, gap.revision + 2);
    for (const uid of [h1, h2])
      assert.equal(work.reasoning.hypotheses.find(h => h.id === uid).revision,
        beforeAddressing.hypotheses.find(h => h.id === uid).revision + 2);
    assert.equal(await page.locator('[data-node-id="U027"]').count(), 1);
    await editGap();
    assert.equal(await agentCheck.isEnabled(), true);
    assert.equal(await addressing.getByRole('button', { name: title('U027'), exact: true }).count(), 0);
    await page.screenshot({ path: 'tmp/gap-addressing-editor.png' });
    await addressing.getByRole('button', { name: title('U004'), exact: true }).click();
    await editor.waitFor({ state: 'hidden' }); await saved();
    await page.locator('[data-node-id="U004"].node-selected').waitFor();
    await editGap();
    await editor.getByLabel('缺口状态', { exact: true }).selectOption('resolved'); await save();
    // Move the second Hypothesis, and restore exact layout after Issue switches/reload.
    const node = page.locator(`[data-node-id="${h2}"]`);
    let box = await node.boundingBox();
    await page.mouse.move(box.x + 30, box.y + 20); await page.mouse.down();
    await page.mouse.move(box.x - 100, box.y + 400, { steps: 4 }); await page.mouse.up(); await saved();
    const position = await node.getAttribute('style');
    await tab(before.core_issue).click(); await saved();
    assert.equal(await page.locator(`[data-node-id="${h2}"]`).count(), 0);
    await tab(issue).click(); await saved();
    assert.equal(await node.getAttribute('style'), position);
    await page.reload(); await page.waitForFunction(() => document.querySelector('[role=tab][aria-selected=true]')); await saved();
    assert.equal(await node.getAttribute('style'), position);
    assert.equal((await current()).reasoning.gaps.find(g => g.id === gap.id).status, 'resolved');
    // User judgment/work status are separate; unsaved edits cannot reach Agent.
    await editHypothesis(h1);
    await editor.getByLabel('假设内容', { exact: true }).fill('用户修正：不能将张某陈述等同于独立核实。');
    await editor.getByLabel('假设工作状态', { exact: true }).selectOption('deprecated');
    assert.equal(await editor.getByRole('button', { name: '请 Agent 检查' }).isDisabled(), true);
    await save();
    assert.ok((await page.locator(`[data-node-id="${h1}"]`).getAttribute('class')).includes('reasoning-deprecated'));
    await editHypothesis(h1);
    await editor.getByLabel('假设工作状态', { exact: true }).selectOption('active'); await save();
    const after = (await (await page.request.get(`${api}/api/case`)).json()).case;
    for (const field of ['files', 'info_units', 'relations', 'workspace_state']) assert.deepEqual(after[field], before[field]);
    assert.deepEqual(errors, []);
    console.log('PASS: manual reasoning, Gap addressing deltas and preserved business data. Agent commits are covered by evidence-link and gap-update smokes.');
  } catch (error) {
    if (page) await page.screenshot({ path: 'tmp/reasoning-failure.png' }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
