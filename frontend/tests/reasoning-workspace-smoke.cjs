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
    assert.equal(await page.locator('.reasoning-suggestion:visible').count(), 0);
    assert.equal(await page.getByRole('region', { name: 'Agent 调查结果' }).locator('blockquote:visible').count(), 0);
    await page.locator('.reasoning-suggestions > details > summary').click();
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
    const acceptedId = work.reasoning.accepted_proposals[`${result.run_id}:${result.proposals[1].proposal_id}`];
    assert.ok(acceptedId); assert.equal(await page.locator(`[data-node-id="${acceptedId}"]`).count(), 1);
    // Replaying adoption is idempotent, and the older remaining status is stale.
    const duplicate = await page.request.post(`${api}/api/cases/${before.case_id}/reasoning-proposals/accept`, {
      data: { issue, run_id: result.run_id, proposal_id: result.proposals[1].proposal_id } });
    assert.equal((await duplicate.json()).already_accepted, true);
    await page.locator('.reasoning-suggestion').getByRole('button', { name: '采纳建议' }).click();
    await page.locator('.reasoning-suggestions').getByText('推理或材料已被修改，请重新调查后再采纳。').waitFor();
    assert.equal((await current()).reasoning.hypotheses.find(h => h.id === h1).judgment, 'unverified');
    await input.fill('核对损失说法');
    await page.getByRole('button', { name: '发送调查问题' }).click();
    await page.getByRole('region', { name: 'Agent 调查结果' }).waitFor();
    assert.equal(await page.locator('.reasoning-suggestion').count(), 0);
    assert.equal(await page.getByLabel('候选推理建议', { exact: true }).count(), 0);
    await page.getByRole('region', { name: 'Agent 调查结果' }).getByText('查看调查依据', { exact: true }).click();
    await page.getByRole('region', { name: 'Agent 调查结果' }).getByRole('button', { name: '张某笔录.docx', exact: true }).first().click();
    await page.locator('[data-node-id="U029"].node-selected').waitFor();
    await page.reload(); await page.waitForFunction(() => document.querySelector('[role=tab][aria-selected=true]')); await saved();
    assert.equal((await current()).reasoning.gaps.length, preInvestigation.gaps.length + 1);
    assert.equal(await page.locator('.reasoning-candidate').count(), 0);
    await command('适应全部节点'); await saved();
    // Gap -> same-run Hypothesis: original indices survive ignore/filter/adopt.
    await page.getByRole('button', { name: 'Agent 调查', exact: true }).click();
    await input.fill('检查同轮新假设与证据缺口');
    const dependencyResponse = page.waitForResponse(response => response.url().endsWith('/agent/run'));
    await page.getByRole('button', { name: '发送调查问题' }).click();
    const dependencyResult = await (await dependencyResponse).json();
    await page.waitForFunction(() => document.querySelectorAll('.reasoning-suggestion').length === 4);
    await page.locator('.reasoning-suggestions > details > summary').click();
    const firstGap = page.locator('.reasoning-suggestion').filter({ has: page.getByText('核对同轮假设的租金流水。', { exact: true }) });
    const secondGap = page.locator('.reasoning-suggestion').filter({ has: page.getByText('核对同轮假设的交房记录。', { exact: true }) });
    const canvasGap = page.locator('.reasoning-candidate').filter({ has: page.getByText('核对同轮假设的租金流水。', { exact: true }) });
    assert.ok(await firstGap.getByRole('button', { name: '采纳建议', exact: true }).isDisabled());
    assert.ok(await canvasGap.getByRole('button', { name: '采纳', exact: true }).isDisabled());
    await firstGap.getByText('关联假设', { exact: true }).click();
    await firstGap.getByText('待采纳 · 同轮新假设：入住虽有间隔，但计租连续，租金可能未减少。', { exact: true }).waitFor();
    await page.locator('.reasoning-suggestion').first().getByRole('button', { name: '忽略建议' }).click();
    assert.equal(await page.locator('.reasoning-suggestion').count(), 3);
    assert.ok(await firstGap.getByRole('button', { name: '采纳建议', exact: true }).isDisabled());
    const dependencyBefore = (await current()).reasoning;
    const blocked = await page.request.post(`${api}/api/cases/${before.case_id}/reasoning-proposals/accept`, {
      data: { issue, run_id: dependencyResult.run_id, proposal_id: dependencyResult.proposals[2].proposal_id } });
    assert.equal(blocked.status(), 409);
    assert.deepEqual((await current()).reasoning, dependencyBefore);
    await page.screenshot({ path: 'tmp/gap-dependency-pending.png' });
    const target = page.locator('.reasoning-suggestion').filter({ has: page.getByText('同轮新假设：入住虽有间隔，但计租连续，租金可能未减少。', { exact: true }) });
    await target.getByRole('button', { name: '采纳建议', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('.reasoning-suggestion').length === 2);
    assert.ok(await firstGap.getByRole('button', { name: '采纳建议', exact: true }).isEnabled());
    assert.ok(await canvasGap.getByRole('button', { name: '采纳', exact: true }).isEnabled());
    const parentId = (await current()).reasoning.accepted_proposals[`${dependencyResult.run_id}:${dependencyResult.proposals[1].proposal_id}`];
    assert.ok(parentId);
    await firstGap.getByRole('button', { name: '采纳建议', exact: true }).click();
    await firstGap.waitFor({ state: 'hidden' });
    await secondGap.getByRole('button', { name: '采纳建议', exact: true }).click();
    await secondGap.waitFor({ state: 'hidden' });
    work = await current();
    for (const index of [2, 3]) {
      const id = work.reasoning.accepted_proposals[`${dependencyResult.run_id}:${dependencyResult.proposals[index].proposal_id}`];
      assert.deepEqual(work.reasoning.gaps.find(gap => gap.id === id).hypothesis_ids, [parentId]);
    }
    assert.equal(work.reasoning.hypotheses.length, dependencyBefore.hypotheses.length + 1);
    await page.reload(); await page.waitForFunction(() => document.querySelector('[role=tab][aria-selected=true]')); await saved();
    assert.deepEqual((await current()).reasoning, work.reasoning);
    assert.equal(await page.locator('.reasoning-candidate').count(), 0);
    await page.screenshot({ path: 'tmp/reasoning-workspace.png' });
    // Checked artifacts remain pending even if the final prose generation fails.
    const beforePartial = (await current()).reasoning;
    await page.getByRole('button', { name: 'Agent 调查', exact: true }).click();
    await input.fill('检查同轮新假设与缺口，总结失败');
    await page.getByRole('button', { name: '发送调查问题' }).click();
    await page.getByRole('region', { name: 'Agent 调查结果' }).getByText('本轮总结未完成', { exact: true }).waitFor();
    assert.match(await page.getByRole('region', { name: 'Agent 调查结果' }).innerText(), /建议尚未采纳/);
    await page.locator('.reasoning-suggestions').getByText('查看待确认建议（4）', { exact: true }).waitFor();
    assert.deepEqual((await current()).reasoning, beforePartial);
    await page.screenshot({ path: 'tmp/investigation-summary-failed.png' });
    const after = (await (await page.request.get(`${api}/api/case`)).json()).case;
    for (const field of ['files', 'info_units', 'relations', 'workspace_state']) assert.deepEqual(after[field], before[field]);
    assert.deepEqual(errors, []);
    console.log('PASS: UI Gap addressing add/remove/locate, delta payloads, dirty/undo/revisions/reload and preserved basis/status/Hypotheses; manual reasoning and Proposal approvals; business data unchanged.');
  } catch (error) {
    if (page) await page.screenshot({ path: 'tmp/reasoning-failure.png' }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
