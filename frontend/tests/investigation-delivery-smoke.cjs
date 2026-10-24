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
    await page.goto('http://127.0.0.1:5178');
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
    for (const goal of ['首轮来源直用：什么时候搬进去的', '遗漏来源交付：什么时候搬进去的']) {
      const result = await ask(goal);
      assert.equal(result.tool_budget_used.read, 0);
      assert.equal(result.findings.length, 1);
      assert.deepEqual(result.evidence_refs, [{ info_unit_id: 'U026', source_ref_index: 0 }]);
      assert.match(await panel.locator(':scope > .investigation-answer').innerText(), /据张某陈述/);
      await panel.getByText('查看调查依据', { exact: true }).click();
      const source = panel.locator('.investigation-source');
      assert.match(await source.locator('blockquote').innerText(), /3 月 8 号就直接搬进去住了/);
      await source.getByRole('button', { name: '张某笔录.docx', exact: true }).click();
      assert.equal(await page.locator('.canvas-node.node-selected').getAttribute('data-node-id'), 'U026');
    }
    const recovered = await ask('恢复参数并提出假设');
    assert.equal(recovered.proposals.length, 1);
    assert.equal(recovered.tool_budget_used.read, 0);
    assert.equal(recovered.tool_budget_used.proposal, 2); // Rejected + corrected; one artifact only.
    await page.getByText('查看待确认建议（1）', { exact: true }).waitFor();

    const exploration = await ask('探索无直接证据的解释和缺口');
    assert.equal(exploration.findings.length, 0);
    assert.equal(exploration.evidence_refs.length, 0);
    assert.equal(exploration.proposals.length, 2);
    assert.equal(exploration.completion_status, 'evidence_insufficient');
    await page.getByText('查看待确认建议（2）', { exact: true }).click();
    const cards = page.locator('.reasoning-suggestion');
    assert.equal(await cards.getByText('尚无直接来源依据，待检验；采纳不代表已经证实。', { exact: true }).count(), 2);
    assert.ok(await cards.nth(1).getByRole('button', { name: '采纳建议', exact: true }).isDisabled());
    await page.screenshot({ path: 'tmp/investigation-delivery-exploratory.png' });
    await cards.nth(0).getByRole('button', { name: '采纳建议', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('.reasoning-suggestion').length === 1);
    assert.ok(await cards.nth(0).getByRole('button', { name: '采纳建议', exact: true }).isEnabled());
    let state = (await (await page.request.get(url)).json()).workspaces[issue].reasoning;
    assert.equal(state.hypotheses[0].judgment, 'unverified');
    assert.equal(state.gaps.length, 0);
    await cards.nth(0).getByRole('button', { name: '采纳建议', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('.reasoning-suggestion').length === 0);
    state = (await (await page.request.get(url)).json()).workspaces[issue].reasoning;
    assert.equal(state.gaps.length, 1);
    const after = (await (await page.request.get(`${api}/api/case`)).json()).case;
    for (const field of ['info_units', 'relations', 'workspace_state']) assert.deepEqual(after[field], before[field]);
    assert.deepEqual(errors, []);
    console.log('PASS: zero-read initial citations; missing Findings recovered with original source navigation; rejected/corrected Proposal; ungrounded warnings, explicit insufficiency and independent approval; shared case unchanged.');
  } catch (e) {
    if (page) await page.screenshot({ path: 'tmp/investigation-delivery-failure.png' }).catch(() => {});
    throw e;
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
