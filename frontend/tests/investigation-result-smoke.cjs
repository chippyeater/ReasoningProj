// Display-only response fixture derived from Case A. No Gold/cache files are changed.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({ headless: false });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.request.post('http://127.0.0.1:8018/api/cases/case-4325561f/select');
    const original = await (await page.request.get('http://127.0.0.1:8018/api/case')).json();
    const fixture = structuredClone(original);
    const unit = fixture.case.info_units.find(unit => unit.id === 'U026');
    const duplicate = fixture.case.info_units.find(unit => unit.id === 'U027');
    duplicate.title = '入住日期的笔录依据';
    duplicate.content = '张某称替租人于3月8日搬入，尚未独立核实。';
    duplicate.source_refs = structuredClone(unit.source_refs);
    const related = fixture.case.info_units.find(unit => unit.id === 'U022');
    const evidence = [unit, duplicate, related].map(unit => ({
      info_unit_id: unit.id, source_ref_index: 0, ...unit.source_refs[0],
      filename: fixture.case.files.find(file => file.file_id === unit.source_refs[0].file_id).filename,
    }));
    const answer = '据张某陈述，替租人于 2026 年 3 月 8 日搬入，尚未独立核实。';
    const result = {
      goal: '替租人什么时候搬进去的？',
      findings: [{ statement: answer, info_unit_ids: [unit.id, duplicate.id], relation_ids: [],
        evidence_refs: evidence.slice(0, 2).map(({ info_unit_id, source_ref_index }) => ({ info_unit_id, source_ref_index })) },
      { statement: '聊天记录中的搬离说法并非替租人入住日期的独立佐证。', info_unit_ids: [related.id], relation_ids: [],
        evidence_refs: [{ info_unit_id: related.id, source_ref_index: 0 }] }],
      relevant_info_unit_ids: [unit.id, duplicate.id, related.id], relevant_relation_ids: [],
      evidence_refs: evidence.map(({ info_unit_id, source_ref_index }) => ({ info_unit_id, source_ref_index })), evidence,
      open_questions: ['get_evidence 返回 truncated，该查询未获得完整有效结果。',
        'search_info_units 返回 truncated，该查询未获得完整有效结果。',
        '是否有独立的入住或交接记录？', '能否向新租客核对实际入住日期？', '是否有可核对的交接签收材料？'],
      completion_status: 'evidence_insufficient', run_id: 'investigation-private-test', stop_reason: 'model_finished', tool_call_count: 3,
    };
    await page.route('**/api/case', route => route.fulfill({ json: fixture }));
    let responseFixture = result;
    await page.route('**/agent/run', route => route.fulfill({ json: responseFixture }));
    await page.goto('http://127.0.0.1:5178');
    await page.waitForFunction(() => [...document.querySelectorAll('button')].some(b => b.textContent === 'Agent 调查' && !b.disabled));
    await page.getByRole('button', { name: 'Agent 调查', exact: true }).click();
    await page.getByPlaceholder('提出调查问题，或检查假设与证据缺口…').fill(result.goal);
    await page.getByRole('button', { name: '发送调查问题' }).click();
    const panel = page.getByRole('region', { name: 'Agent 调查结果' });
    await panel.getByText(answer, { exact: true }).waitFor();
    const initialText = await panel.innerText();
    assert.equal(await panel.locator('blockquote:visible').count(), 1);
    assert.equal(await panel.locator('blockquote').filter({ hasText: unit.source_refs[0].quote }).count(), 1);
    assert.match(initialText, /部分材料尚未核查/);
    assert.equal(initialText.split('部分材料尚未核查').length - 1, 1);
    assert.ok(!initialText.includes('能否向新租客核对实际入住日期？'));
    await page.screenshot({ path: 'tmp/investigation-result-default.png' });
    const source = panel.locator('.investigation-source').first();
    await source.getByRole('button', { name: '张某笔录.docx', exact: true }).click();
    assert.match(await page.locator('.canvas-node.node-selected').innerText(), /张某称新租客3月8日入住/);
    await source.getByText('关联信息（2）', { exact: true }).click();
    for (const item of [unit, duplicate]) {
      await source.getByRole('button', { name: item.title, exact: true }).click();
      assert.ok((await page.locator('.canvas-node.node-selected').innerText()).includes(item.title));
    }
    await panel.getByText('查看完整调查结果', { exact: true }).click();
    for (const details of await panel.locator('details').all()) await details.evaluate(element => { element.open = true; });
    const fullText = await panel.innerText();
    assert.match(fullText, /能否向新租客核对实际入住日期/);
    assert.match(fullText, /不构成多份独立佐证/);
    assert.equal(await panel.locator('blockquote').count(), 2);
    assert.ok(fullText.length > initialText.length * 1.4);
    assert.doesNotMatch(fullText, /info-|file-|investigation-private|U026|U027|U022|truncated|get_evidence|search_info_units/);
    await page.screenshot({ path: 'tmp/investigation-result-expanded.png' });
    const after = await (await page.request.get('http://127.0.0.1:8018/api/case')).json();
    assert.deepEqual(after, original);
    // Another finding's evidence must not appear as the main answer's evidence.
    responseFixture = structuredClone(result);
    responseFixture.findings[0].evidence_refs = [];
    await page.getByRole('button', { name: '发送调查问题' }).click();
    await panel.getByText('主要发现尚未取得对应的原文依据。', { exact: true }).waitFor();
    assert.equal(await panel.locator('blockquote:visible').count(), 0);
    await panel.getByText('查看完整调查结果', { exact: true }).click();
    assert.equal(await panel.locator('blockquote:visible').count(), 2);
    // Same filename/quote from another file is a separate source, never merged.
    responseFixture = structuredClone(result);
    responseFixture.evidence.push({ ...evidence[0], file_id: 'file-different-origin', info_unit_id: related.id });
    await page.getByRole('button', { name: '发送调查问题' }).click();
    await panel.getByText(answer, { exact: true }).waitFor();
    assert.equal(await panel.locator('blockquote').count(), 3);
    assert.equal(await panel.locator('blockquote').filter({ hasText: unit.source_refs[0].quote }).count(), 2);
    assert.deepEqual(errors, []);
    console.log(`PASS: core answer, exact source deduplication, both node links, hidden IDs, complete expansion, no mutation; default ${initialText.length} / expanded ${fullText.length} characters.`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
