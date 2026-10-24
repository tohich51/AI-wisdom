// Observe protocol completion metadata; never log text, thinking or credentials.
// Coding-process-only timeout adapter. Keep CLI OAuth, TLS and AbortSignal unchanged.
const { createRequire } = require('node:module');
const { isAbsolute } = require('node:path');
const { realpathSync, statSync } = require('node:fs');
const runtime = process.env.MMX_RUNTIME;
if (!runtime || !isAbsolute(runtime)) throw new Error('MMX_RUNTIME_MISSING');
let runtimeRequire;
try {
  const resolved = realpathSync(runtime);
  if (!statSync(resolved).isFile()) throw new Error();
  runtimeRequire = createRequire(resolved);
} catch { throw new Error('MMX_RUNTIME_INVALID'); }
// Operator-trusted pinned CLI installation; never consult an owner fallback.
// This preload runs in the wrapper and its auth subprocesses as well as the CLI.
let Agent, setGlobalDispatcher;
try { ({ Agent, setGlobalDispatcher } = runtimeRequire('undici')); }
catch { throw new Error('MMX_RUNTIME_UNDICI_UNAVAILABLE'); }
class BoundedCodingAgent extends Agent {
  dispatch(options, handler) {
    return super.dispatch({ ...options, headersTimeout: 920000, bodyTimeout: 920000 }, handler);
  }
}
setGlobalDispatcher(new BoundedCodingAgent({ connect: { timeout: 10000 } }));

const realFetch = globalThis.fetch;
globalThis.fetch = async function(input, options) {
  const response = await realFetch(input, options);
  let streamed = false;
  try { streamed = JSON.parse(options?.body || '{}').stream === true; } catch {}
  if (!streamed || !String(input).includes('/anthropic/v1/messages')) return response;
  const proof = { model: null, stop_reason: null, message_stop: false, thinking_seen: false, error: false };
  const observer = response.clone();
  (async () => {
    try {
      const reader = observer.body.getReader(), decoder = new TextDecoder();
      let buffer = '';
      function event(part) {
        const data = part.split('\n').filter(v => v.startsWith('data:')).map(v => v.slice(5).trimStart()).join('\n');
        if (!data || data === '[DONE]') return;
        let value; try { value = JSON.parse(data); } catch { proof.error = true; return; }
        if (value.type === 'message_start') proof.model = value.message?.model || null;
        if (value.type === 'message_delta' && value.delta?.stop_reason) proof.stop_reason = value.delta.stop_reason;
        if (value.type === 'message_stop') proof.message_stop = true;
        if (value.type === 'content_block_start' && value.content_block?.type === 'thinking') proof.thinking_seen = true;
        if (value.type === 'error' || value.base_resp?.status_code) proof.error = true;
      }
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        buffer = buffer.replace(/\r\n/g, '\n');
        let split;
        while ((split = buffer.indexOf('\n\n')) >= 0) { event(buffer.slice(0, split)); buffer = buffer.slice(split + 2); }
      }
      buffer += decoder.decode();
      if (buffer.trim()) event(buffer);
    } catch { proof.error = true; }
    process.stderr.write('\nMMX_STREAM_PROOF=' + JSON.stringify(proof) + '\n');
  })();
  return response;
};
