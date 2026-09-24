import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

test('job advances only after evidence, survives restart, and resets QA when artwork changes', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'hive-mint-test-'));
  const port = 37410 + Math.floor(Math.random() * 2000);
  let child;
  const base = `http://127.0.0.1:${port}`;
  async function launch() {
    child = spawn(process.execPath, ['server.mjs'], { cwd: path.resolve(import.meta.dirname, '..'), env: { ...process.env, HIVE_MINT_DATA_DIR: dir, PORT: String(port) }, stdio: 'pipe' });
    for (let i = 0; i < 80; i++) {
      if (child.exitCode !== null) throw new Error(`Server exited: ${child.exitCode}`);
      try { const response = await fetch(`${base}/api/state`); if (response.ok) return; }
      catch { /* Wait for bind. */ }
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw new Error('Server did not start');
  }
  async function stop() { child.kill(); await new Promise(resolve => child.once('exit', resolve)); }
  async function call(url, method = 'GET', data) {
    const response = await fetch(base + url, { method, headers: { origin: base, ...(data ? { 'content-type': 'application/json' } : {}) }, body: data ? JSON.stringify(data) : undefined });
    return { status: response.status, json: await response.json() };
  }
  try {
    await launch();
    const created = await call('/api/jobs', 'POST', { title: 'Imaginary synth emblem', audience: 'Independent synth fans', product: 'Poster' });
    assert.equal(created.status, 201);
    const id = created.json.id, target = `/api/jobs/${id}`;
    assert.equal((await call(`${target}/advance`, 'POST')).json.stage, 'research');
    assert.equal((await call(`${target}/advance`, 'POST')).status, 400);
    await call(target, 'PATCH', { research: 'Hypothesis; verify current listings manually.' });
    assert.equal((await call(`${target}/advance`, 'POST')).json.stage, 'design');
    await call(target, 'PATCH', { designBrief: 'Original geometric analog oscillator mark' });
    assert.equal((await call(`${target}/advance`, 'POST')).status, 400);
    const pixel = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL5WQAAAABJRU5ErkJggg==';
    assert.equal((await call(`${target}/artwork`, 'POST', { name: 'sample.png', data: pixel })).status, 200);
    assert.equal((await call(`${target}/advance`, 'POST')).json.stage, 'qa');
    await call(target, 'PATCH', { qaPassed: true, qaNotes: 'Checked text and print size' });
    assert.equal((await call(`${target}/advance`, 'POST')).json.stage, 'cost');
    await call(target, 'PATCH', { costs: { price: 30, production: 16, fulfillmentShipping: 5 } });
    assert.equal((await call(`${target}/advance`, 'POST')).json.stage, 'approval');
    assert.equal((await call(`${target}/approve`, 'POST')).json.stage, 'listing_draft');
    await stop(); await launch();
    assert.equal((await call('/api/state')).json.jobs[0].stage, 'listing_draft');
    assert.equal((await call(`${target}/artwork`, 'POST', { name: 'revised.png', data: pixel })).json.stage, 'design');
    assert.equal((await call('/api/state')).json.jobs[0].qaPassed, false);
    const hostile = await fetch(base + target, { method: 'PATCH', headers: { origin: 'https://example.org', 'content-type': 'application/json' }, body: '{}' });
    assert.equal(hostile.status, 403);
  } finally {
    if (child && child.exitCode === null) await stop();
    await fs.rm(dir, { recursive: true, force: true });
  }
});
