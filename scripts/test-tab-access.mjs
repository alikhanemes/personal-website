// Policy tests execute the shipped bootstrap, without claiming browser UI coverage.
// Run from PowerShell: node .\scripts\test-tab-access.mjs
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../static/tab-access.js', import.meta.url), 'utf8');
const priorId = 'a'.repeat(48);
const marker = (extra = {}) => 'shygarma-tab:' + JSON.stringify({ id: priorId, ...extra });
const settle = async () => { for (let turn = 0; turn < 4; turn++) await new Promise(setImmediate); };

async function page({ type = 'navigate', name = marker(), lock = 'available', gate = false,
                      server = {}, continuation } = {}) {
  const calls = [], listeners = {}, links = [], written = [], assigned = [];
  const status = { textContent: '' };
  let reloads = 0, randomSeed = 0;
  const navigation = { querySelector: () => links.find(link => link.className === 'write-link'),
    append: link => links.push(link) };
  const document = {
    querySelector: selector => ({ '#auth-config': { textContent: JSON.stringify({ csrf: 'csrf-test', gate }) },
      '#signin-status': status, '.primary-nav': navigation }[selector] || null),
    createElement: () => ({}), addEventListener: (type, callback) => { listeners[type] = callback; },
    open() {}, write: html => written.push(html), close() {},
  };
  const location = { href: 'http://127.0.0.1:5000/shygarma', origin: 'http://127.0.0.1:5000',
    pathname: '/shygarma', assign: value => assigned.push(value), reload: () => { reloads++; } };
  const window = { name, document, location,
    crypto: { getRandomValues: values => values.fill(++randomSeed) },
    performance: { getEntriesByType: () => type ? [{ type }] : [] },
    addEventListener: (type, callback) => { listeners[type] = callback; } };
  if (continuation) window.__shygarmaContinuation = continuation;
  const navigator = lock === 'missing' ? {} : { locks: {
    request: async (_name, _options, callback) => callback(lock === 'held' ? null : {}),
  } };
  const fetch = async (url, options) => {
    calls.push({ url, options });
    const binding = options.headers['X-Tab-Token'];
    let ok = false, payload = {};
    if (url === '/shygarma/status' || url === '/shygarma/workspace') ok = binding === priorId;
    if (url === '/shygarma/navigation') { ok = binding === priorId; payload.ticket = 'one-use-ticket'; }
    if (url === '/shygarma/resume') {
      const data = JSON.parse(options.body);
      ok = data.ticket === 'one-use-ticket' && data.tab_id === priorId && !server.consumed;
      if (ok) server.consumed = true;
    }
    return { ok, json: async () => payload, text: async () => '<main>Authorized workspace</main>' };
  };
  const context = { window, document, navigator, location, fetch, URL, Uint8Array,
    addEventListener: window.addEventListener };
  for (const name of ['localStorage', 'sessionStorage']) {
    Object.defineProperty(context, name, { get() { throw new Error('Web Storage must not hold tab access.'); } });
    Object.defineProperty(window, name, { get() { throw new Error('Web Storage must not hold tab access.'); } });
  }
  vm.runInNewContext(source, context, { filename: 'static/tab-access.js', timeout: 1000 });
  await settle();
  return { window, calls, links, written, assigned, listeners, status,
    binding: () => JSON.parse(window.name.slice('shygarma-tab:'.length)), reloads: () => reloads };
}

test('a fresh or duplicated navigate does not reuse an unlocked marker', async () => {
  const result = await page();
  assert.notEqual(result.binding().id, priorId);
  assert.equal(result.calls.length, 0);
  assert.equal(result.links.length, 0);
  assert.match(result.status.textContent, /password/);
});

test('ordinary reload validates the same binding before showing Write post', async () => {
  const result = await page({ type: 'reload' });
  assert.equal(result.binding().id, priorId);
  assert.equal(result.calls[0].url, '/shygarma/status');
  assert.equal(result.calls[0].options.headers['X-Tab-Token'], priorId);
  assert.equal(result.links[0].textContent, 'Write post');
});

test('the editor shell opens a workspace only after successful reload validation', async () => {
  const result = await page({ type: 'reload', gate: true });
  assert.deepEqual(result.calls.map(call => call.url), ['/shygarma/status', '/shygarma/workspace']);
  assert.equal(result.written.length, 1);
});

test('history restoration and unknown navigation fail closed', async () => {
  for (const type of ['back_forward', undefined]) {
    const result = await page({ type: type ?? '' });
    assert.notEqual(result.binding().id, priorId);
    assert.equal(result.calls.length, 0);
    assert.equal(result.links.length, 0);
  }
});

test('an owned or unsupported exclusive lock cannot reuse the prior binding', async () => {
  for (const lock of ['held', 'missing']) {
    const result = await page({ type: 'reload', lock });
    assert.notEqual(result.binding().id, priorId, lock);
    assert.equal(result.links.length, 0, lock);
    assert.equal(result.written.length, 0, lock);
    assert.ok(result.calls.every(call => call.options.headers['X-Tab-Token'] !== priorId), lock);
  }
});

test('same-tab navigation obtains a ticket and removes it before resuming', async () => {
  const current = await page({ type: 'reload' });
  const event = { target: { closest: () => ({ href: 'http://127.0.0.1:5000/', target: '', download: '' }) },
    button: 0, preventDefault() { this.prevented = true; } };
  await current.listeners.click(event);
  assert.equal(event.prevented, true);
  assert.equal(current.binding().ticket, 'one-use-ticket');
  assert.equal(current.assigned[0], 'http://127.0.0.1:5000/');
  const server = {};
  const destination = await page({ name: current.window.name, server });
  assert.equal(destination.calls[0].url, '/shygarma/resume');
  assert.equal(destination.binding().ticket, undefined);
  assert.equal(destination.links.length, 1);
  const replay = await page({ name: current.window.name, server, gate: true });
  assert.equal(replay.links.length, 0);
  assert.match(replay.status.textContent, /password/);
});

test('a bfcache-restored document clears its marker and reloads', async () => {
  const result = await page({ type: 'reload' });
  result.listeners.pageshow({ persisted: true });
  assert.equal(result.window.name, '');
  assert.equal(result.reloads(), 1);
});
