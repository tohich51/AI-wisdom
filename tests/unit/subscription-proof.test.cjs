'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { mkdtempSync, mkdirSync, writeFileSync, rmSync } = require('node:fs');
const { join, resolve } = require('node:path');
const { tmpdir } = require('node:os');
const { spawnSync } = require('node:child_process');
const proof = resolve(__dirname, '../../src/kb/subscription-proof.cjs');

function fixture(t) {
  const dir = mkdtempSync(join(tmpdir(), 'proof-synthetic-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const cli = join(dir, 'cli');
  mkdirSync(join(cli, 'dist'), { recursive: true });
  mkdirSync(join(cli, 'node_modules/undici'), { recursive: true });
  const runtime = join(cli, 'dist/mmx.mjs');
  writeFileSync(runtime, '// synthetic runtime; never executed\n');
  writeFileSync(join(cli, 'package.json'), JSON.stringify({ name: 'mmx-cli', version: '1.0.25' }));
  writeFileSync(join(cli, 'node_modules/undici/package.json'), JSON.stringify({ name: 'undici', main: 'index.cjs' }));
  writeFileSync(join(cli, 'node_modules/undici/index.cjs'), `
    class Agent {
      constructor(options) { this.options = options; }
      dispatch(options, handler) { globalThis.dispatched = { options, handler }; return 17; }
    }
    module.exports = { Agent, setGlobalDispatcher(agent) { globalThis.agent = agent; } };
  `);
  const env = { ...process.env, MMX_RUNTIME: runtime };
  delete env.NODE_OPTIONS;
  delete env.NODE_PATH;
  return { dir, cli, runtime, env };
}
function run(env, code, args = []) {
  return spawnSync(process.execPath, [...args, '-e', code], {
    env, encoding: 'utf8', timeout: 10000,
  });
}

test('missing and relative runtime fail closed during preload', () => {
  for (const runtime of [undefined, 'relative/mmx.mjs']) {
    const env = { ...process.env, NODE_OPTIONS: `--require=${JSON.stringify(proof)}` };
    delete env.MMX_RUNTIME;
    if (runtime) env.MMX_RUNTIME = runtime;
    const result = run(env, 'process.stdout.write("unexpected")');
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /MMX_RUNTIME_MISSING/);
    assert.equal(result.stdout, '');
  }
});

test('invalid runtime and missing sibling dependency fail closed', t => {
  const f = fixture(t);
  let result = run({ ...f.env, MMX_RUNTIME: join(f.dir, 'absent.mjs') }, `require(${JSON.stringify(proof)})`);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /MMX_RUNTIME_INVALID/);
  rmSync(join(f.cli, 'node_modules'), { recursive: true });
  result = run(f.env, `require(${JSON.stringify(proof)})`);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /MMX_RUNTIME_UNDICI_UNAVAILABLE/);
});

test('portable dependency, timeout adapter, SSE shape and body preservation', t => {
  const f = fixture(t);
  const result = run(f.env, `
    const assert = require('node:assert/strict');
    const data = [
      {type:'message_start', message:{model:'synthetic-model'}},
      {type:'content_block_start', content_block:{type:'thinking',thinking:'SYNTHETIC_PRIVATE'}},
      {type:'content_block_delta', delta:{text:'SYNTHETIC_PRIVATE'}},
      {type:'message_delta', delta:{stop_reason:'end_turn'}},
      {type:'message_stop'}
    ].map(v => 'data: ' + JSON.stringify(v) + '\\r\\n\\r\\n').join('');
    globalThis.fetch = async () => new Response(data);
    require(${JSON.stringify(proof)});
    assert.deepEqual(agent.options, {connect:{timeout:10000}});
    const signal = new AbortController().signal;
    const handler = {};
    assert.equal(agent.dispatch({headersTimeout:1, bodyTimeout:2, signal}, handler), 17);
    assert.equal(dispatched.options.headersTimeout, 920000);
    assert.equal(dispatched.options.bodyTimeout, 920000);
    assert.equal(dispatched.options.signal, signal);
    assert.equal(dispatched.handler, handler);
    (async () => {
      const response = await fetch('https://synthetic.invalid/anthropic/v1/messages', {body:'{"stream":true}'});
      assert.equal(await response.text(), data);
    })().catch(() => { process.exitCode = 1; });
  `);
  assert.equal(result.status, 0);
  assert.equal(result.stdout, '');
  const lines = result.stderr.trim().split('\n');
  assert.equal(lines.length, 1);
  assert.ok(lines[0].startsWith('MMX_STREAM_PROOF='));
  assert.deepEqual(JSON.parse(lines[0].slice('MMX_STREAM_PROOF='.length)), {
    model: 'synthetic-model', stop_reason: 'end_turn', message_stop: true,
    thinking_seen: true, error: false,
  });
  assert.ok(!result.stderr.includes('SYNTHETIC_PRIVATE'));
});

test('malformed and error events preserve error proof without body logging', t => {
  const f = fixture(t);
  const result = run(f.env, `
    globalThis.fetch = async () => new Response('data: malformed\\n\\ndata: {"type":"error"}\\n\\n');
    require(${JSON.stringify(proof)});
    fetch('https://synthetic.invalid/anthropic/v1/messages', {body:'{"stream":true}'});
  `);
  assert.equal(result.status, 0);
  assert.deepEqual(JSON.parse(result.stderr.trim().slice('MMX_STREAM_PROOF='.length)), {
    model:null, stop_reason:null, message_stop:false, thinking_seen:false, error:true,
  });
});

test('nonstream and other endpoints emit no proof', t => {
  const f = fixture(t);
  const result = run(f.env, `
    globalThis.fetch = async () => new Response('synthetic');
    require(${JSON.stringify(proof)});
    Promise.all([
      fetch('https://synthetic.invalid/anthropic/v1/messages', {body:'{}'}),
      fetch('https://synthetic.invalid/other', {body:'{"stream":true}'})
    ]);
  `);
  assert.equal(result.status, 0);
  assert.equal(result.stderr, '');
});

test('NODE_OPTIONS preload also works in inherited wrapper/auth subprocess', t => {
  const f = fixture(t);
  const result = run({ ...f.env, NODE_OPTIONS: `--require=${JSON.stringify(proof)}` }, `
    const assert = require('node:assert/strict');
    const { spawnSync } = require('node:child_process');
    assert.ok(globalThis.agent);
    const child = spawnSync(process.execPath, ['-e', 'if (!globalThis.agent) process.exit(9)'], {
      env: process.env, encoding:'utf8', timeout:5000
    });
    assert.equal(child.status, 0);
    assert.equal(child.stdout, '');
    assert.equal(child.stderr, '');
  `);
  assert.equal(result.status, 0);
  assert.equal(result.stderr, '');
});
