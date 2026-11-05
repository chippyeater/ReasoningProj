// Headed UI + real upload/Extraction/Agent/commit APIs; only model replies are mocked.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');
const assert = require('node:assert/strict');
const api = 'http://127.0.0.1:8018';

(async () => {
  const browser = await chromium.launch({ headless: false });
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } });
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const created = await page.request.post(`${api}/api/cases`, { multipart: {
      case_title: 'Gap 增量回应烟测', source_kinds: JSON.stringify({ 'basis.txt': 'testimony' }),
      files: { name: 'basis.txt', mimeType: 'text/plain', buffer: Buffer.from('当事人陈述租赁履行存在争议。') } } });
    assert.equal(created.status(), 201);
    const cid = (await created.json()).case.case_id;
    const extracted = await page.request.post(`${api}/api/extraction/run`, { data: { case_id: cid } });
    assert.equal(extracted.status(), 200, await extracted.text());
    const basis = (await extracted.json()).info_units[0].id;
    const issue = '租金实际损失';
    const edit = async values => {
      const response = await page.request.patch(`${api}/api/cases/${cid}/issue-reasoning`, { data: { issue, ...values } });
      assert.equal(response.status(), 200, await response.text());
      return (await response.json()).object_id;
    };
    const hids = [await edit({ hypothesis: { content: '新租约实际履行，无空置损失' } }),
      await edit({ hypothesis: { content: '新租约未实际履行，仍有空置损失' } })];
    const gid = await edit({ gap: { content: '新租约的起租约定是否实际履行？', hypothesis_ids: hids, basis_info_unit_ids: [basis] } });
    const workspace = async () => (await (await page.request.get(`${api}/api/cases/${cid}/issue-workspaces`)).json()).workspaces[issue];
    const before = await workspace();
    const upload = await page.request.post(`${api}/api/cases/${cid}/files`, { multipart: {
      source_kinds: JSON.stringify({ 'new-contract.txt': 'document' }), files: { name: 'new-contract.txt',
        mimeType: 'text/plain', buffer: Buffer.from('新合同约定起租，但未载明实际收款。') } } });
    assert.equal(upload.status(), 200);
    const incremental = await page.request.post(`${api}/api/extraction/run`, { data: { case_id: cid } });
    assert.equal(incremental.status(), 200, await incremental.text());
    assert.deepEqual((await workspace()).canvas, before.canvas);
    await page.goto('http://127.0.0.1:5178', { waitUntil: 'domcontentloaded' });
    await page.getByRole('tab', { name: issue, exact: true }).waitFor();
    await page.locator(`.canvas-node[data-node-id="${gid}"]`).waitFor();
    await page.getByRole('button', { name: 'Agent 调查', exact: true }).click();
    await page.getByPlaceholder('提出调查问题，或检查假设与证据缺口…').fill('检查补充合同对已有缺口的回应');
    const completed = page.waitForResponse(r => r.url().endsWith('/agent/run') && r.request().method() === 'POST');
    await page.getByRole('button', { name: '发送调查问题' }).click();
    const response = await completed;
    assert.equal(response.status(), 200, await response.text());
    const result = await response.json();
    assert.equal(result.proposals.length, 1);
    assert.equal(result.proposals[0].kind, 'evidence_gap_update');
    assert.equal(result.proposals[0].gap_id, gid);
    assert.equal(result.workspace_commit.status, 'applied', JSON.stringify(result));
    const changes = page.getByRole('region', { name: '调查变更记录' });
    await changes.getByText('查看变更依据', { exact: true }).click();
    assert.match(await changes.innerText(), /部分回应|实际履行/);
    assert.equal(await page.getByRole('button', { name: /采纳/ }).count(), 0);
    assert.equal(await page.locator('.reasoning-candidate').count(), 0);
    const after = await workspace();
    assert.equal(after.reasoning.gaps.length, 1);
    const gap = after.reasoning.gaps[0];
    assert.equal(gap.id, gid);
    assert.equal(gap.content, before.reasoning.gaps[0].content);
    assert.deepEqual(gap.hypothesis_ids, hids);
    assert.deepEqual(gap.basis_info_unit_ids, [basis]);
    assert.deepEqual(gap.addressing_info_unit_ids, result.proposals[0].addressing_info_unit_ids);
    assert.equal(gap.status, 'open');
    assert.deepEqual(after.reasoning.hypotheses.map(h => h.judgment), ['unverified', 'unverified']);
    await changes.getByRole('button', { name: '撤销本轮变更', exact: true }).click();
    await changes.getByRole('button', { name: '恢复本轮变更', exact: true }).waitFor();
    assert.deepEqual((await workspace()).reasoning.gaps, before.reasoning.gaps);
    await changes.getByRole('button', { name: '恢复本轮变更', exact: true }).click();
    await changes.getByRole('button', { name: '撤销本轮变更', exact: true }).waitFor();
    assert.deepEqual((await workspace()).reasoning.gaps[0], gap);
    assert.deepEqual(errors, []);
    console.log('PASS: upload + incremental Extraction -> existing shared Gap update automatic update -> undo/restore; open, stable ID/associations and preserved judgments.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
