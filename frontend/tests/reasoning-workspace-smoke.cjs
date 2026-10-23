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
    await page.goto('http://127.0.0.1:5178');
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
      await editor.getByLabel('关联案件信息', { exact: true }).selectOption(uid);
      await editor.getByLabel('关联语义', { exact: true }).selectOption(meaning);
      await save();
    }
    // A single source remains case-owned while supporting different reasoning.
    await addLink(h1, 'U026', 'support');
    await addLink(h2, 'U026', 'oppose');
    await addLink(h1, 'U029', 'verify');
    let work = await current();
    assert.equal(work.reasoning.links.length, 3);
    assert.equal(await page.locator('.reasoning-support').count(), 1);
    assert.equal(await page.locator('.reasoning-oppose').count(), 1);
    assert.equal(await page.locator('.reasoning-verify').count(), 1);
    // Existing line hit targets open semantic editing with exact source text.
    const firstLink = work.reasoning.links[0];
    await page.locator(`[data-reasoning-link-id="${firstLink.id}"] .relation-edge-hit`).dispatchEvent('click', { bubbles: true });
    await editor.getByLabel('关联语义', { exact: true }).selectOption('verify');
    await editor.getByText('核对来源原文', { exact: true }).click();
    assert.match(await editor.innerText(), /张某笔录.docx/);
    assert.match(await editor.innerText(), /3 月 8 号/);
    await save();
    await page.locator(`[data-reasoning-link-id="${firstLink.id}"] .relation-edge-hit`).dispatchEvent('click', { bubbles: true });
    await editor.getByRole('button', { name: '解除关联', exact: true }).click();
    await editor.waitFor({ state: 'hidden' }); await saved();
    assert.equal((await current()).reasoning.links.length, 2);
    await addLink(h1, 'U026', 'verify');
    await editHypothesis(h1);
    await editor.getByRole('button', { name: '添加证据缺口', exact: true }).click();
    await editor.getByLabel('缺口问题', { exact: true }).fill('是否存在替租人的实际入住记录和新租约？');
    await editor.getByLabel('房东可能没有实际空置损失，仍需核对。', { exact: true }).check();
    await editor.getByText('关联已有信息（可选）', { exact: true }).click();
    await editor.getByLabel(before.info_units.find(u => u.id === 'U026').title, { exact: true }).check();
    await save();
    work = await current();
    const gap = work.reasoning.gaps.at(-1);
    assert.deepEqual([...gap.hypothesis_ids].sort(), [h1, h2].sort());
    await page.locator(`[data-node-id="${gap.id}"]`).getByRole('button', { name: '查看与编辑' }).click();
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
    await editHypothesis(h1);
    await editor.getByRole('button', { name: '请 Agent 检查' }).click();
    const input = page.getByPlaceholder('提出调查问题，或检查假设与证据缺口…');
    const request = page.waitForRequest(request => request.url().endsWith('/agent/run'));
    const response = page.waitForResponse(response => response.url().endsWith('/agent/run'));
    const preInvestigation = (await current()).reasoning;
    await page.getByRole('button', { name: '发送调查问题' }).click();
    const sent = (await request).postDataJSON();
    assert.deepEqual(sent.selected_reasoning_ids, [h1]); assert.deepEqual(sent.selected_info_unit_ids, []);
    const result = await (await response).json();
    await page.locator('.reasoning-suggestions').waitFor();
    assert.ok((await page.getByRole('region', { name: 'Agent 调查结果' }).boundingBox()).height > 100);
    assert.deepEqual((await current()).reasoning, preInvestigation);
    assert.equal(await page.getByLabel('候选推理建议', { exact: true }).count(), 3);
    await page.screenshot({ path: 'tmp/reasoning-candidates.png' });
    assert.doesNotMatch(await page.locator('.reasoning-suggestions').innerText(), /U029|hyp-|gap-|elink-|file-/);
    // Ignore one; adopt another only after explicit confirmation, with save failure/retry.
    await page.locator('.reasoning-suggestion').first().getByRole('button', { name: '忽略建议' }).click();
    assert.equal(await page.getByLabel('候选推理建议', { exact: true }).count(), 2);
    assert.deepEqual((await current()).reasoning, preInvestigation);
    await page.route('**/reasoning-proposals/accept', route => route.fulfill({ status: 500, json: { detail: 'offline failure' } }));
    await page.locator('.reasoning-suggestion').first().getByRole('button', { name: '采纳建议' }).click();
    await page.locator('.reasoning-suggestions').getByRole('alert').waitFor();
    assert.deepEqual((await current()).reasoning, preInvestigation);
    await page.unroute('**/reasoning-proposals/accept');
    await page.locator('.reasoning-suggestion').first().getByRole('button', { name: '采纳建议' }).click();
    await page.waitForFunction(() => document.querySelectorAll('.reasoning-suggestion').length === 1);
    work = await current();
    assert.equal(work.reasoning.gaps.length, preInvestigation.gaps.length + 1);
    const acceptedId = work.reasoning.accepted_proposals[`${result.run_id}:1`];
    assert.ok(acceptedId); assert.equal(await page.locator(`[data-node-id="${acceptedId}"]`).count(), 1);
    // Replaying adoption is idempotent, and the older remaining status is stale.
    const duplicate = await page.request.post(`${api}/api/cases/${before.case_id}/reasoning-proposals/accept`, {
      data: { issue, run_id: result.run_id, proposal_index: 1 } });
    assert.equal((await duplicate.json()).already_accepted, true);
    await page.locator('.reasoning-suggestion').getByRole('button', { name: '采纳建议' }).click();
    await page.locator('.reasoning-suggestions').getByText('推理或材料已被修改，请重新调查后再采纳。').waitFor();
    assert.equal((await current()).reasoning.hypotheses.find(h => h.id === h1).judgment, 'unverified');
    await input.fill('核对损失说法');
    await page.getByRole('button', { name: '发送调查问题' }).click();
    await page.getByRole('region', { name: 'Agent 调查结果' }).waitFor();
    assert.equal(await page.locator('.reasoning-suggestion').count(), 0);
    assert.equal(await page.getByLabel('候选推理建议', { exact: true }).count(), 0);
    await page.getByRole('region', { name: 'Agent 调查结果' }).getByRole('button', { name: '张某笔录.docx', exact: true }).first().click();
    await page.locator('[data-node-id="U029"].node-selected').waitFor();
    await page.reload(); await page.waitForFunction(() => document.querySelector('[role=tab][aria-selected=true]')); await saved();
    assert.equal((await current()).reasoning.gaps.length, preInvestigation.gaps.length + 1);
    assert.equal(await page.locator('.reasoning-candidate').count(), 0);
    await command('适应全部节点'); await saved();
    await page.screenshot({ path: 'tmp/reasoning-workspace.png' });
    const after = (await (await page.request.get(`${api}/api/case`)).json()).case;
    for (const field of ['info_units', 'relations', 'workspace_state']) assert.deepEqual(after[field], before[field]);
    assert.deepEqual(errors, []);
    console.log('PASS: task 10231658 manual hypotheses/links/gaps, source verification, shared evidence, drag/restore, user corrections, real dynamic Agent, transient preview, ignore/adopt/retry/idempotency/stale protection, plain answer and old business data unchanged.');
  } catch (error) {
    if (page) await page.screenshot({ path: 'tmp/reasoning-failure.png' }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
