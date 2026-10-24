'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawnSync, spawn } = require('node:child_process');
const wrapper = path.resolve(__dirname, '../../scripts/mmx-subscription.cjs');
const fixture = `import fs from 'node:fs';
const a=process.argv.slice(2), e=process.env;
fs.appendFileSync(e.FAKE_LOG, JSON.stringify({a,key:e.MINIMAX_API_KEY,cn:e.MINIMAX_CN_API_KEY,url:e.MINIMAX_BASE_URL,verbose:e.MINIMAX_VERBOSE})+'\\n');
if(a.includes('status')) {
 if(e.FAKE_MODE==='hang') {setInterval(()=>{},1000);await new Promise(()=>{});}
 if(e.FAKE_MODE==='overflow') {console.log('x'.repeat(100000));process.exit(0);}
 if(e.FAKE_MODE==='status-fail') {console.error('PRIVATE_SENTINEL');process.exit(7);}
 if(e.FAKE_MODE==='invalid') {console.log('PRIVATE_SENTINEL');process.exit(0);}
 const refreshed=fs.existsSync(e.FAKE_LOG+'.refreshed');
 console.log(JSON.stringify({method:e.FAKE_MODE==='apikey'?'api_key':'oauth',
 token_expires:e.FAKE_MODE==='no-expiry'?null:new Date(Date.now()+((e.FAKE_MODE==='expired'&&!refreshed)||e.FAKE_MODE==='still-expired'?-1000:3600000)).toISOString(),secret:'PRIVATE_SENTINEL'}));
} else if(a.includes('refresh')) {
 console.log('PRIVATE_SENTINEL');
 if(e.FAKE_MODE==='refresh-fail')process.exit(8);
 fs.writeFileSync(e.FAKE_LOG+'.refreshed','yes');
} else if(e.FAKE_MODE==='wait') {
 console.log('READY');setInterval(()=>{},1000);
} else {console.log(JSON.stringify(a));process.exit(Number(e.FAKE_EXIT||0));}
`;
function setup(t, mode='valid') {
 const dir=mkdtempSync(path.join(os.tmpdir(),'mmx-offline-'));
 t.after(()=>rmSync(dir,{recursive:true,force:true}));
 mkdirSync(path.join(dir,'dist'));
 writeFileSync(path.join(dir,'package.json'),JSON.stringify({name:'mmx-cli',version:'1.0.25'}));
 const runtime=path.join(dir,'dist/mmx.mjs');writeFileSync(runtime,fixture);
 const log=path.join(dir,'calls');
 const env={...process.env,MMX_RUNTIME:runtime,FAKE_LOG:log,FAKE_MODE:mode,MINIMAX_API_KEY:'private',MINIMAX_CN_API_KEY:'private',MINIMAX_BASE_URL:'private',MINIMAX_VERBOSE:'true'};
 delete env.NODE_OPTIONS;
 return {env, runtime, log, run:(args)=>spawnSync(process.execPath,[wrapper,...args],{env,encoding:'utf8',timeout:6000}),calls:()=>existsSync(log)?readFileSync(log,'utf8').trim().split('\n').map(JSON.parse):[]};
}
for(const args of [['--api-key','private'],['text','chat','--api-key=private'],['--base-url=private'],['--verbose'],['config','set','x'],['--region=global','update'],['auth','logout']]) {
 test('refuses '+args.join(' '),t=>{const f=setup(t);const r=f.run(args);assert.equal(r.status,3);assert.equal(f.calls().length,0);assert.ok(!r.stderr.includes('private'));});
}
test('pin and missing runtime fail closed',t=>{const f=setup(t);delete f.env.MMX_RUNTIME;assert.equal(f.run(['text','chat']).status,3);f.env.MMX_RUNTIME=f.runtime;writeFileSync(path.resolve(f.runtime,'../../package.json'),JSON.stringify({name:'mmx-cli',version:'2'}));assert.equal(f.run(['text','chat']).status,3);assert.equal(f.calls().length,0);});
test('preflight scrub default model and exit',t=>{const f=setup(t);f.env.FAKE_EXIT='17';const r=f.run(['text','chat','--prompt','synthetic']);assert.equal(r.status,17);const calls=f.calls();assert.equal(calls.length,2);for(const c of calls){assert.equal(c.key,undefined);assert.equal(c.cn,undefined);assert.equal(c.url,undefined);assert.equal(c.verbose,undefined);}assert.ok(calls[1].a.includes('MiniMax-M3.1-Flash-Preview'));assert.ok(!(r.stdout+r.stderr).includes('PRIVATE_SENTINEL'));});
test('preflight preserves region and forces JSON',t=>{const f=setup(t);assert.equal(f.run(['--region','cn','text','chat']).status,0);const a=f.calls()[0].a;assert.equal(a[a.indexOf('--region')+1],'cn');assert.equal(a[a.indexOf('--output')+1],'json');});
test('help-looking prompt cannot bypass OAuth',t=>{const f=setup(t,'apikey');assert.equal(f.run(['text','chat','--prompt','--help']).status,3);assert.equal(f.calls().length,1);});
test('oversized auth output fails closed',t=>{const f=setup(t,'overflow');assert.equal(f.run(['text','chat']).status,3);assert.equal(f.calls().length,1);});
test('auth status timeout is bounded',t=>{const f=setup(t,'hang');const start=Date.now();const r=spawnSync(process.execPath,[wrapper,'text','chat'],{env:f.env,encoding:'utf8',timeout:19000});assert.equal(r.status,3);assert.ok(Date.now()-start<18500);assert.equal(f.calls().length,1);});
test('explicit model preserved',t=>{const f=setup(t);assert.equal(f.run(['text','chat','--model','explicit']).status,0);const a=f.calls()[1].a;assert.equal(a.filter(x=>x==='--model').length,1);assert.ok(a.includes('explicit'));});
for(const mode of ['status-fail','invalid','apikey','no-expiry'])test('refuses '+mode,t=>{const f=setup(t,mode);const r=f.run(['text','chat']);assert.equal(r.status,3);assert.equal(f.calls().length,1);assert.ok(!(r.stdout+r.stderr).includes('PRIVATE_SENTINEL'));});
test('bounded refresh verifies then executes',t=>{const f=setup(t,'expired');const r=f.run(['text','chat']);assert.equal(r.status,0);assert.deepEqual(f.calls().map(x=>x.a.includes('status')?'status':x.a.includes('refresh')?'refresh':'chat'),['status','refresh','status','chat']);assert.ok(!(r.stdout+r.stderr).includes('PRIVATE_SENTINEL'));});
test('refresh cannot loop or run after still-expired verification',t=>{const f=setup(t,'still-expired');assert.equal(f.run(['text','chat']).status,3);assert.equal(f.calls().length,3);});
test('explicit refresh failure does not disclose diagnostics',t=>{const f=setup(t,'refresh-fail');const r=f.run(['auth','refresh']);assert.equal(r.status,3);assert.ok(!(r.stdout+r.stderr).includes('PRIVATE_SENTINEL'));assert.equal(f.calls().length,1);});
test('status has no credential or status output',t=>{const f=setup(t);const r=f.run(['auth','status']);assert.equal(r.status,0);assert.equal(r.stdout,'');assert.equal(r.stderr,'');});
test('help and login do not run generation preflight',t=>{const f=setup(t);assert.equal(f.run(['--help']).status,0);assert.equal(f.run(['auth','login']).status,0);assert.equal(f.calls().length,2);});
for(const [signal,code] of [['SIGINT',130],['SIGTERM',143]])test('forwards '+signal,async t=>{
 const f=setup(t,'wait');const child=spawn(process.execPath,[wrapper,'text','chat'],{env:f.env,stdio:['ignore','pipe','pipe']});
 t.after(()=>{if(child.exitCode===null)child.kill('SIGKILL');});
 const exit=new Promise(resolve=>child.once('exit',(c,s)=>resolve([c,s])));
 await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('child not ready')),4000);child.stdout.on('data',b=>{if(b.toString().includes('READY')){clearTimeout(timer);resolve();}});});
 child.kill(signal);assert.deepEqual(await exit,[code,null]);
});
