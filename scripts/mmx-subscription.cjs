#!/usr/bin/env node
'use strict';
// Subscription-only adapter. Runtime and OAuth state are operator-owned, not bundled.
const { spawnSync, spawn } = require('node:child_process');
const { readFileSync, realpathSync } = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const fail = (message, code = 3) => { process.stderr.write(message + '\n'); process.exit(code); };
const args = process.argv.slice(2);
const runtime = process.env.MMX_RUNTIME;
if (!runtime || !path.isAbsolute(runtime)) fail('MMX_RUNTIME_MISSING');
try {
  const resolved = realpathSync(runtime);
  const pkg = JSON.parse(readFileSync(path.resolve(path.dirname(resolved), '../package.json'), 'utf8'));
  if (path.basename(resolved) !== 'mmx.mjs' || pkg.name !== 'mmx-cli' || pkg.version !== '1.0.25')
    fail('MMX_RUNTIME_PIN_REQUIRED');
} catch { fail('MMX_RUNTIME_INVALID'); }
if (args.some(arg => /^(--api-key|--base-url)(=|$)/.test(arg))) fail('MMX_SUBSCRIPTION_ROUTE_ONLY');
if (args.some(arg => /^--verbose(=|$)/.test(arg))) fail('MMX_UNSAFE_CONFIGURATION_OPERATION');
// Parse only the pinned runtime's global options. Unknown prefixes fail closed.
const valueOptions = new Set(['--region', '--output', '--timeout']);
const switches = new Set(['--no-color', '--non-interactive', '--help', '--version']);
const region = [];
for (let j = 0; j < args.length; j++) {
  if (args[j] === '--region') {
    if (!args[j + 1] || args[j + 1].startsWith('-')) fail('MMX_ARGUMENT_INVALID');
    region.push('--region', args[++j]);
  } else if (args[j].startsWith('--region=')) region.push(args[j]);
}
let i = 0;
for (; i < args.length && args[i].startsWith('-'); i++) {
  const key = args[i].split('=')[0];
  if (valueOptions.has(key)) {
    if (!args[i].includes('=')) {
      if (!args[i + 1] || args[i + 1].startsWith('-')) fail('MMX_ARGUMENT_INVALID');
      i++;
    }
  } else if (!switches.has(args[i])) fail('MMX_ARGUMENT_INVALID');
}
const resource = args[i];
const command = args[i + 1];
if (['config', 'update'].includes(resource)) fail('MMX_UNSAFE_CONFIGURATION_OPERATION');
const env = { ...process.env };
delete env.MINIMAX_API_KEY;
delete env.MINIMAX_CN_API_KEY;
delete env.MINIMAX_BASE_URL;
delete env.MINIMAX_VERBOSE;
const tail = args.slice(i);
const help = !args.length || (args.length === 1 && ['--help', '--version'].includes(args[0])) ||
  ((tail.length === 2 || tail.length === 3) && tail.at(-1) === '--help' &&
    tail.slice(0, -1).every(arg => !arg.startsWith('-')));
const capture = (command, timeout) => spawnSync(process.execPath,
  [runtime, ...region, '--no-color', '--non-interactive', '--output', 'json', 'auth', command],
  { env, encoding: 'utf8', timeout, maxBuffer: 65536 });
function status() {
  const result = capture('status', 15000);
  if (result.status !== 0) fail('MMX_SUBSCRIPTION_AUTH_UNAVAILABLE');
  let value;
  try { value = JSON.parse(result.stdout); } catch { fail('MMX_AUTH_STATUS_INVALID'); }
  if (!value || value.method !== 'oauth') fail('MMX_OAUTH_REQUIRED');
  const expiry = Date.parse(value.token_expires);
  if (!Number.isFinite(expiry)) fail('MMX_AUTH_STATUS_INVALID');
  return expiry;
}
function refresh() {
  if (capture('refresh', 20000).status !== 0) fail('MMX_OAUTH_REFRESH_REQUIRES_OWNER');
  if (status() <= Date.now()) fail('MMX_OAUTH_REFRESH_REQUIRES_OWNER');
}
if (!help && resource === 'auth') {
  if (command === 'status') { status(); process.exit(0); }
  if (command === 'refresh') { refresh(); process.exit(0); }
  if (command !== 'login') fail('MMX_UNSAFE_CONFIGURATION_OPERATION');
  // Interactive login alone may display its authorization instructions.
} else if (!help) {
  if (status() <= Date.now() + 5 * 60 * 1000) refresh();
  if (resource === 'text' && command === 'chat' && !args.some(arg => /^--model(=|$)/.test(arg)))
    args.push('--model', 'MiniMax-M3.1-Flash-Preview');
}
const child = spawn(process.execPath, [runtime, ...args], { env, stdio: 'inherit' });
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
child.on('error', () => fail('MMX_EXEC_FAILED', 1));
child.on('exit', (code, signal) => process.exit(code === null ? 128 + (os.constants.signals[signal] || 15) : code));
