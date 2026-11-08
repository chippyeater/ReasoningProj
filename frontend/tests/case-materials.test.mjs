import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const compiled = await build({
  stdin: { contents: `export { getCaseMaterialsZip } from './src/api';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)) },
  bundle: true, write: false, platform: 'node', format: 'cjs', define: { 'import.meta.env': '{}' },
});
const mod = { exports: {} };
new Function('module', 'exports', compiled.outputFiles[0].text)(mod, mod.exports);
const { getCaseMaterialsZip } = mod.exports;

test('ZIP client encodes the Case ID, preserves response bytes and carries cancellation', async t => {
  const controller = new AbortController();
  const raw = new Uint8Array([80, 75, 3, 4, 0, 255]);
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, 'http://localhost:8000/api/cases/case%2F%3F%23/materials.zip');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.signal, controller.signal);
    return new Response(raw, { headers: { 'content-type': 'application/zip' } });
  });
  const blob = await getCaseMaterialsZip('case/?#', controller.signal);
  assert.deepEqual(new Uint8Array(await blob.arrayBuffer()), raw);
});

test('ZIP client exposes missing-original errors and rejects HTML masquerading as an archive', async t => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({
    detail: { code: 'original_file_missing', message: '原始文件缺失：file-one' },
  }), { status: 409, headers: { 'content-type': 'application/json' } }));
  await assert.rejects(getCaseMaterialsZip('case-one'), /original_file_missing.*file-one/);
  fetch.mock.mockImplementation(async () => new Response('<html>gateway error</html>', { headers: { 'content-type': 'text/html' } }));
  await assert.rejects(getCaseMaterialsZip('case-one'), /未返回 ZIP/);
});
