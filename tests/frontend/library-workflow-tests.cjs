/* Synthetic examples only. VM/DOM fixtures, no browser, network, books or credentials.
 * Uses the project's existing node:vm fixture approach (extension-checks.cjs).
 * Executes actual data/forms/pager and the relevant app function blocks. */
'use strict';
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const path = require('node:path');
const source = name => fs.readFileSync(path.join(__dirname, '../../src/kb/static', name), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
async function settle() { for (let n = 0; n < 12; n++) await tick(); }
function fixture() {
  let document;
  class Element {
    constructor(tag='div') { this.tagName=tag.toUpperCase(); this.children=[]; this.attrs={}; this.dataset={}; this.listeners={}; this.value=''; this.disabled=false; this.hidden=false; this._text=''; this.className=''; this.type=tag==='input'?'text':''; }
    get parentNode(){return this.parentElement;}
    get isConnected(){return this===document.body || Boolean(this.parentElement?.isConnected);}
    get textContent(){return this._text+this.children.map(x=>x.textContent).join('');}
    set textContent(v){this.replaceChildren(); this._text=String(v);}
    get options(){return this.children.filter(x=>x.tagName==='OPTION');}
    get elements(){const list=this.querySelectorAll('input, select, textarea, button');list.namedItem=name=>list.find(x=>x.name===name);return list;}
    get labels(){return document.querySelectorAll('label').filter(x=>x.getAttribute('for')===this.id);}
    get willValidate(){return ['INPUT','TEXTAREA','SELECT'].includes(this.tagName);}
    get validity(){const valueMissing=Boolean(this.required&&!this.value), patternMismatch=Boolean(this.pattern&&this.value&&!new RegExp('^(?:'+this.pattern+')$').test(this.value));return {valueMissing,patternMismatch,valid:!valueMissing&&!patternMismatch};}
    checkValidity(){return this.validity.valid;}
    append(...nodes){for(let n of nodes){if(typeof n==='string')n=document.createTextNode(n);if(n.tagName==='#FRAGMENT'){this.append(...n.children.slice());continue;}n.remove();n.parentElement=this;this.children.push(n);}}
    prepend(n){this.insertBefore(n,this.children[0]||null);}
    replaceChildren(...nodes){for(const c of this.children)c.parentElement=null;this.children=[];this._text='';this.append(...nodes);}
    remove(){if(this.parentElement){this.parentElement.children=this.parentElement.children.filter(x=>x!==this);this.parentElement=null;}}
    insertBefore(n,before){if(!before)return this.append(n);n.remove();n.parentElement=this;this.children.splice(this.children.indexOf(before),0,n);}
    insertAdjacentElement(where,n){if(where==='afterend')this.parentElement.insertBefore(n,this.parentElement.children[this.parentElement.children.indexOf(this)+1]||null);else throw Error(where);}
    setAttribute(k,v){this.attrs[k]=String(v);if(['id','name','class','type','value'].includes(k))this[k==='class'?'className':k]=String(v);if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,a)=>a.toUpperCase())]=String(v);}
    getAttribute(k){return this.attrs[k]??null;}
    removeAttribute(k){delete this.attrs[k];}
    matches(selector){return selector.split(',').some(raw=>{let s=raw.trim();if(s.includes(' '))s=s.split(' ').at(-1);if(s===':disabled')return this.disabled;let not=s.match(/:not\(([^)]+)\)/);if(not){s=s.replace(not[0],'');if(this.matches(not[1]))return false;}const tag=s.match(/^[\w-]+/);if(tag&&this.tagName!==tag[0].toUpperCase())return false;const id=s.match(/#([\w-]+)/);if(id&&this.id!==id[1])return false;const cls=s.match(/\.([\w-]+)/);if(cls&&!this.className.split(' ').includes(cls[1]))return false;for(const m of s.matchAll(/\[([^=\]]+)(?:=["']?([^\]"']+)["']?)?\]/g)){const val=this.getAttribute(m[1]);if(val===null)return false;if(m[2]!==undefined&&val!==m[2])return false;}return true;});}
    querySelectorAll(s){return this.children.flatMap(c=>[...(c.matches(s)?[c]:[]),...c.querySelectorAll(s)]);}
    querySelector(s){return this.querySelectorAll(s)[0]||null;}
    closest(s){return this.matches(s)?this:this.parentElement?.closest(s)||null;}
    contains(e){return e===this||this.children.some(c=>c.contains(e));}
    addEventListener(type,fn,opts){(this.listeners[type]||=[]).push({fn,once:opts?.once});}
    dispatchEvent(event){event.target ||= this;event.currentTarget=this;for(const entry of [...(this.listeners[event.type]||[])]){entry.fn(event);if(entry.once)this.listeners[event.type]=this.listeners[event.type].filter(x=>x!==entry);}return true;}
    click(){if(!this.disabled)this.dispatchEvent(new Event('click'));}
    focus(options){this.focusOptions=options;document.activeElement=this;}
    showModal(){this.open=true;this.focus();}
    close(){this.open=false;this.dispatchEvent(new Event('close'));}
    reset(){for(const e of this.elements)e.value='';}
  }
  class Form extends Element {constructor(){super('form');}}
  class Select extends Element {constructor(){super('select');}}
  class Input extends Element {constructor(){super('input');}}
  class Textarea extends Element {constructor(){super('textarea');}}
  class Event {constructor(type,options={}){this.type=type;Object.assign(this,options);}preventDefault(){}stopImmediatePropagation(){}}
  document={cookie:'',createElement(tag){return tag==='form'?new Form():tag==='select'?new Select():tag==='input'?new Input():tag==='textarea'?new Textarea():new Element(tag);},createTextNode(t){const e=new Element('#text');e.textContent=t;return e;},createDocumentFragment(){return new Element('#fragment');},addEventListener(){},querySelectorAll(s){return this.body.querySelectorAll(s);},querySelector(s){return this.body.querySelector(s);},getElementById(id){return this.querySelector('#'+id);}};
  document.body=new Element('body');document.documentElement=document.body;document.activeElement=document.body;
  const add=(tag,id,parent=document.body)=>{const n=document.createElement(tag);n.setAttribute('id',id);parent.append(n);return n;};
  add('div','status');add('div','library-list');add('button','create-library-type');add('form','search');add('div','search-results');add('div','library-detail');
  const createForm=add('form','create-library'),kind=add('select','library-kind',createForm);kind.name='kind';for(const key of ['reference','project']){const o=document.createElement('option');o.value=key;o.textContent=key;kind.append(o);}kind.value='reference';
  let handler=()=>{throw Error('Unexpected synthetic request');};const requests=[];
  const route={token:1,name:'libraries',params:[],query:new URLSearchParams()};
  const context={console,document,Element,HTMLElement:Element,HTMLFormElement:Form,HTMLSelectElement:Select,HTMLInputElement:Input,HTMLTextAreaElement:Textarea,Event,MutationObserver:class{observe(){}},AbortController,DOMException,Headers,URLSearchParams,URL,Blob,crypto:require('node:crypto').webcrypto,setTimeout,clearTimeout,queueMicrotask,location:{pathname:'/app/libraries',search:''},FormData:class{constructor(form){this.entries=form.elements.filter(x=>x.name&&!x.disabled).map(x=>[x.name,x.value]);}get(key){return this.entries.find(x=>x[0]===key)?.[1]??null;}[Symbol.iterator](){return this.entries[Symbol.iterator]();}},fetch:async(url,opts)=>{requests.push({url,opts});const r=await handler(url,opts);return {ok:r.status>=200&&r.status<300,status:r.status,json:async()=>r.body};}};
  context.window=context;context.globalThis=context;
  context.WisdomRouter={current:()=>route,path:(name,params=[])=>'/app/'+name+(params.length?'/'+params.join('/'):''),focusHeading(){},navigate(){}};
  context.WisdomResearch={invalidate(){}};context.WisdomMap={invalidate(){}};
  vm.createContext(context);const run=code=>vm.runInContext(code,context);
  run(source('data.js'));run(source('forms.js'));run(source('pager.js'));
  const app=source('app.js');
  run(app.slice(0,app.indexOf('const sourceProcessRequests'))+'\nconst sourceProcessRequests = new Map();\nconst featureHost={node,button,identity:()=>identity,currentLibrary:()=>selected};');
  run(app.slice(app.indexOf('function editSourceMetadata'),app.indexOf('async function openLibrary')));
  run("identity={account_id:'synthetic-account',principal_id:'synthetic-person'};");
  return {context,run,document,requests,route,add,setHandler:fn=>handler=fn,button:(root,text)=>root.querySelectorAll('button').find(x=>x.textContent===text),dialog:()=>document.querySelector('dialog'),submit:async form=>{form.dispatchEvent(new Event('submit'));await settle();}};
}
const response=body=>({status:200,body});
const lib=i=>({id:'library-'+i,name:'Пример · библиотека '+i,kind:'reference'});
const page=offset=>({libraries:Array.from({length:Math.min(50,73-offset)},(_,i)=>lib(offset+i)),total:73,limit:50,offset});
const sourceRecord={id:'source-example',title:'Синтетический источник · Пример',author:'Автор · Пример',edition:'Издание · Пример',publication_ref:'Публикация · Пример'};
const fragmentId='00000000-0000-4000-8000-000000000650';
const fragment=(ordinal,id='example-'+ordinal)=>({id,ordinal,text:'Синтетическая точная цитата · Пример',paragraph:8});
let passed=0;
async function test(name,fn){await fn();passed++;console.log('PASS '+name);}
(async()=>{
 await test('LibraryPage: 73 items, page 2, total, selected library retained',async()=>{
  const f=fixture();f.run('selected='+JSON.stringify(lib(70)));f.setHandler(url=>response(page(Number(new URL(url,'https://fixture.invalid').searchParams.get('offset')))));
  await f.run('loadLibraries()');assert.match(f.document.getElementById('library-list').textContent,/1–50 из 73/);
  f.button(f.document.body,'Следующая страница').click();await settle();
  assert.deepEqual(f.requests.map(r=>r.url),['/api/libraries?limit=50&offset=0','/api/libraries?limit=50&offset=50']);
  assert.match(f.document.getElementById('library-list').textContent,/51–73 из 73/);assert.equal(f.run('selected.id'),'library-70');assert(f.button(f.document.body,'Следующая страница').disabled);
 });
 await test('Mounted pager accepts a fresh page after cache invalidation',async()=>{
  const f=fixture();f.setHandler(url=>response(page(Number(new URL(url,'https://fixture.invalid').searchParams.get('offset')))));
  await f.run('loadLibraries()');f.context.WisdomData.invalidate('saved metadata');
  f.button(f.document.body,'Следующая страница').click();await settle();
  assert.match(f.document.getElementById('library-list').textContent,/1–50 из 73/);
  f.button(f.document.body,'Следующая страница').click();await settle();
  assert.match(f.document.getElementById('library-list').textContent,/51–73 из 73/);
 });
 await test('Library route loads custom titles before first page, retired type keeps base title',async()=>{
  const f=fixture(),custom={...lib(0),type_key:'custom_notes'};
  f.setHandler(url=>response(url==='/api/library-types'?[{key:'custom_notes',title:'Особые заметки',base_kind:'reference'}]:{libraries:[custom],total:1,limit:50,offset:0}));
  f.run("const ready=async()=>{};function cancelSourceView(){};router.register=(name,handlers)=>{window.libraryHandlers=handlers;};");
  const app=source('app.js');f.run(app.slice(app.indexOf("router.register('libraries'"),app.indexOf("router.register('context'")));
  await f.context.libraryHandlers.mount(null,{isCurrent:()=>true,params:[]});
  assert.deepEqual(f.requests.map(r=>r.url),['/api/library-types','/api/libraries?limit=50&offset=0']);
  assert.match(f.document.getElementById('library-list').textContent,/Особые заметки/);
  assert.equal(f.run("libraryKindLabel({kind:'reference',type_key:'retired_custom'})"),f.run("CORE_LIBRARY_KIND_LABELS.get('reference')"));
 });
 await test('Existing array-based server lists retain offset paging',async()=>{
  const f=fixture();f.context.offsets=[];
  f.run("window.arrayPager=WisdomList.createServer(featureHost,{library:null,fetchPage:async ({offset})=>{offsets.push(offset);return Array.from({length:offset===0?50:3},(_,i)=>({title:'Пример '+(offset+i)}));},renderItem:item=>node('p',item.title)});document.body.append(arrayPager.element,arrayPager.navigation,arrayPager.status);");
  await f.context.arrayPager.load();f.button(f.document.body,'Следующая страница').click();await settle();
  assert.deepEqual(Array.from(f.context.offsets),[0,50]);assert.equal(f.context.arrayPager.count(),3);assert.equal(f.context.arrayPager.hasMore(),false);
 });
 await test('Late identity response cannot populate libraries or pager',async()=>{
  const f=fixture();let resolve;f.setHandler(()=>new Promise(r=>resolve=r));const pending=f.run('loadLibraries()').catch(e=>e);
  f.run("identity={account_id:'other',principal_id:'other'};freshIdentitySerial++;");f.context.WisdomData.invalidate('identity check');resolve(response(page(0)));
  assert.equal((await pending).name,'AbortError');assert.equal(f.run('libraries.length'),0);assert(!f.document.getElementById('library-list').textContent.includes('библиотека 0'));
 });
 await test('Direct route outside page 1 uses supported GET by id',async()=>{
  const f=fixture();f.route.params=['library-70'];f.setHandler(url=>response(url.includes('?')?page(0):lib(70)));await f.run('loadLibraries()');
  assert.equal(f.requests[1].url,'/api/libraries/library-70');assert.equal(f.run('libraries.at(-1).id'),'library-70');
 });
 await test('Paged library chooser reaches later option without changing current library',async()=>{
  const f=fixture(),select=f.add('select','fixture-selector');f.run('selected='+JSON.stringify(lib(2)));f.setHandler(url=>response(page(Number(new URL(url,'https://fixture.invalid').searchParams.get('offset')))));
  f.context.fixtureSelect=select;f.run('attachLibraryChooser(fixtureSelect)');f.button(f.document.body,'Выбрать из всех библиотек').click();await settle();
  f.button(f.dialog(),'Следующая страница').click();await settle();f.button(f.dialog(),'Пример · библиотека 70 · Справочник').click();await settle();
  assert.equal(select.value,'library-70');assert.equal(f.run('selected.id'),'library-2');assert.equal(f.dialog(),null);
 });
 await test('Metadata prefill and cancel send no write',async()=>{
  const f=fixture();f.run('editSourceMetadata('+JSON.stringify(sourceRecord)+')');const form=f.dialog().querySelector('form');
  for(const key of ['author','edition','publication_ref'])assert.equal(form.elements.namedItem(key).value,sourceRecord[key]);
  f.button(f.dialog(),'Отмена').click();assert.equal(f.requests.length,0);assert.equal(f.dialog(),null);
 });
 for(const status of [403,422,500])await test('Metadata '+status+' keeps unsaved values and visible error',async()=>{
  const f=fixture();f.setHandler(()=>({status,body:{detail:'Синтетический отказ'}}));f.run('editSourceMetadata('+JSON.stringify(sourceRecord)+')');const d=f.dialog(),form=d.querySelector('form');form.elements.namedItem('author').value='Несохранённый автор · Пример';
  await f.submit(form);assert(d.isConnected&&d.open);assert.equal(form.elements.namedItem('author').value,'Несохранённый автор · Пример');assert(!form.querySelector('.dialog-form-error').hidden);assert.equal(f.run('identity.principal_id'),'synthetic-person');assert.equal(f.requests.length,1);
 });
 await test('Metadata PATCH only fields/null; success invalidates and rereads source',async()=>{
  const f=fixture();f.setHandler((url,opts)=>response(opts.method==='PATCH'?sourceRecord:{source:{...sourceRecord,author:null},fragments:[]}));f.run('editSourceMetadata('+JSON.stringify(sourceRecord)+')');const form=f.dialog().querySelector('form');form.elements.namedItem('author').value='';
  await f.submit(form);assert.deepEqual(JSON.parse(f.requests[0].opts.body),{author:null,edition:sourceRecord.edition,publication_ref:sourceRecord.publication_ref});assert.equal(f.requests[0].url,'/api/sources/source-example/metadata');assert.equal(f.context.WisdomData.generation(),1);assert.match(f.requests[1].url,/sources\/source-example\?from_fragment=0/);assert.match(f.dialog().textContent,/Автор: не указан/);
 });
 await test('Type conflict retains inputs; 201 rereads registry and selects real key safely',async()=>{
  const f=fixture();let conflict=true;f.setHandler((url,opts)=>opts.method==='POST'?(conflict?{status:409,body:{detail:'type key already registered'}}:{status:201,body:{key:'new_type'}}):response([{key:'reference',title:'Справочник'},{key:'new_type',title:'<img src=x onerror=alert(1)>',base_kind:'reference'}]));
  f.run('createLibraryType()');const form=f.dialog().querySelector('form');form.elements.namedItem('key').value='new_type';form.elements.namedItem('title').value='<img src=x onerror=alert(1)>';
  await f.submit(form);assert.match(form.textContent,/ключ уже занят/);assert.equal(form.elements.namedItem('title').value,'<img src=x onerror=alert(1)>');
  conflict=false;await f.submit(form);assert.equal(f.document.getElementById('library-kind').value,'new_type');assert.equal(f.requests.at(-1).url,'/api/library-types');assert.equal(f.document.querySelector('img'),null);assert(f.document.getElementById('library-kind').options.some(o=>o.value==='project'));
  assert.deepEqual(JSON.parse(f.requests[1].opts.body),{key:'new_type',title:'<img src=x onerror=alert(1)>'});
 });
 await test('Type invalid key is rejected without requests',async()=>{
  const f=fixture();f.run('createLibraryType()');const form=f.dialog().querySelector('form');form.elements.namedItem('key').value='BAD KEY';form.elements.namedItem('title').value='Пример';await f.submit(form);assert.equal(f.requests.length,0);assert(!form.querySelector('.form-error-summary').hidden);
 });
 await test('New library sends core kind with registered custom type key',async()=>{
  const f=fixture(),form=f.document.getElementById('create-library');
  const name=f.add('input','fixture-name',form);name.name='name';name.value='Пример новой библиотеки';
  const custom=f.add('option','fixture-type',f.document.getElementById('library-kind'));custom.value='new_type';custom.dataset.baseKind='reference';
  f.document.getElementById('library-kind').value='new_type';
  f.setHandler((url,opts)=>response(opts.method==='POST'?{...lib(74),kind:'reference',type_key:'new_type'}:page(0)));
  f.run('async function openLibrary(lib) { window.openedLibrary=lib; }');
  const app=source('app.js');f.run(app.slice(app.indexOf("byId('create-library').dataset"),app.indexOf('const searchForm =')));
  await f.submit(form);
  const write=f.requests.find(r=>r.opts.method==='POST');assert.equal(write.url,'/api/libraries');assert.equal(JSON.parse(write.opts.body).kind,'reference');assert.equal(JSON.parse(write.opts.body).type_key,'new_type');assert.equal(f.context.openedLibrary.type_key,'new_type');
 });
 await test('401 and read-403 still use access-loss path',async()=>{
  for(const status of [401,403]){
   const f=fixture();f.run('editSourceMetadata('+JSON.stringify(sourceRecord)+')');let notified=null;f.context.WisdomData.onBeforeInvalidate(code=>notified=code);f.setHandler(()=>({status,body:{}}));
   await assert.rejects(f.context.WisdomData.api('/api/sources/source-example'));
   assert.equal(notified,status);assert.equal(f.dialog(),null);
  }
 });
 await test('Unknown or denied exact fragment never opens evidence or scans',async()=>{
  for(const status of [403,404]){
   const f=fixture();f.setHandler(()=>({status,body:{detail:'Недоступно'}}));
   await f.run('viewSource("source-example",{fragmentId:'+JSON.stringify(fragmentId)+'})').catch(()=>{});
   assert.equal(f.requests.length,1);assert.equal(f.dialog(),null);
  }
 });
 await test('Cancelling metadata pending write prevents stale reopen',async()=>{
  const f=fixture();let resolve;f.setHandler(()=>new Promise(r=>resolve=r));f.run('editSourceMetadata('+JSON.stringify(sourceRecord)+')');
  const form=f.dialog().querySelector('form');const pending=f.submit(form);await tick();f.button(f.dialog(),'Отмена').click();resolve(response(sourceRecord));await pending;
  assert.equal(f.requests.length,1);assert.equal(f.dialog(),null);
 });
 await test('Exact citation starts at ordinal 650; next=850; explicit source start',async()=>{
  const f=fixture();f.setHandler(url=>response({source:sourceRecord,fragments:url.includes('fragment_id')?Array.from({length:200},(_,i)=>fragment(650+i,i===0?fragmentId:undefined)):[fragment(Number(new URL(url,'https://fixture.invalid').searchParams.get('from_fragment')))]}));
  await f.run('viewSource("source-example",'+JSON.stringify({fragmentId,quote:'точная цитата',locator:{paragraph:8}})+')');
  assert.deepEqual(f.requests.map(r=>r.url),['/api/sources/source-example?fragment_id='+fragmentId+'&limit=200']);assert.match(f.dialog().textContent,/Цитата найдена/);assert.match(f.dialog().textContent,/Фрагменты 650–849/);assert.notEqual(f.document.activeElement.focusOptions?.preventScroll,true);
  f.button(f.dialog(),'Следующая часть').click();await settle();assert.match(f.requests[1].url,/from_fragment=850&limit=200/);
  f.button(f.dialog(),'К началу источника').click();await settle();assert.match(f.requests[2].url,/from_fragment=0&limit=200/);
 });
 await test('HTTP 200 with wrong quote or locator never marks exact success',async()=>{
  for(const options of [{fragmentId,quote:'Нет такой цитаты'},{fragmentId,locator:{paragraph:99}}]){
   const f=fixture();f.setHandler(()=>response({source:sourceRecord,fragments:[fragment(650,fragmentId)]}));await f.run('viewSource("source-example",'+JSON.stringify(options)+')');
   assert.equal(f.requests.length,1);assert.match(f.dialog().textContent,/не найдено/);assert.equal(f.dialog().querySelector('[data-source-fragment-target]'),null);assert.equal(f.dialog().querySelector('mark'),null);
  }
 });
 await test('Quote-only search remains bounded to three pages and reports limit',async()=>{
  const f=fixture();f.setHandler(url=>{const start=Number(new URL(url,'https://fixture.invalid').searchParams.get('from_fragment'));return response({source:sourceRecord,fragments:Array.from({length:200},(_,i)=>fragment(start+i))});});
  await f.run('viewSource("source-example",{quote:"Отсутствующая цитата"})');assert.equal(f.requests.length,3);assert.match(f.dialog().textContent,/ограничен тремя страницами/);
 });
 await test('Cancelled source read cannot open a dialog',async()=>{
  const f=fixture();let resolve;f.setHandler(()=>new Promise(r=>resolve=r));const pending=f.run('viewSource("source-example",{fragmentId:'+JSON.stringify(fragmentId)+'})');f.run('cancelSourceView()');resolve(response({source:sourceRecord,fragments:[fragment(650,fragmentId)]}));await pending;assert.equal(f.dialog(),null);
 });

 await test('Search scope preserves global default and uses selected UUID only on request',async()=>{
  const f=fixture(),form=f.document.getElementById('search');
  const input=f.add('input','search-query',form);input.name='q';input.value='synthetic heading';
  const scope=f.add('select','search-scope',form);scope.name='scope';scope.value='all';
  for(const value of ['all','selected']){const o=f.add('option','scope-'+value,scope);o.setAttribute('value',value);}
  const id='11111111-1111-4111-8111-111111111111';
  f.run('libraries=[{id:'+JSON.stringify(id)+',name:"Пример"}];selected=libraries[0];');
  const app=source('app.js');f.run(app.slice(app.indexOf('const searchForm ='),app.indexOf("byId('logout')")));
  f.setHandler(()=>response({mode:'semantic',indexed_library_count:1,pending_library_count:1,fallback_reason:'index_pending_or_unavailable',hits:[{statement:'Synthetic shared heading',provenance:[]}]}));
  await f.submit(form);
  let params=new URL(f.requests.at(-1).url,'https://fixture.invalid').searchParams;
  assert.equal(params.get('q'),'synthetic heading');assert.equal(params.has('library_id'),false);
  assert.match(f.document.getElementById('search-results').textContent,/Поиск по смыслу/);
  assert.match(f.document.getElementById('search-results').textContent,/В библиотеках с неготовым индексом/);
  assert.doesNotMatch(f.document.getElementById('search-results').textContent,/Пока доступен поиск по словам/);
  scope.value='selected';scope.dispatchEvent(new f.context.Event('change'));
  assert.doesNotMatch(f.document.getElementById('search-results').textContent,/Synthetic shared heading/);
  await f.submit(form);
  params=new URL(f.requests.at(-1).url,'https://fixture.invalid').searchParams;
  assert.equal(params.get('library_id'),id);
  const count=f.requests.length;
  f.run('selected=null;');scope.value='selected';
  await f.submit(form);
  assert.equal(f.requests.length,count);assert.equal(scope.value,'all');
  assert.match(f.document.getElementById('search-results').textContent,/больше недоступна/);
 });
 await test('Changing scope cancels pending results and stale citation actions',async()=>{
  const f=fixture(),form=f.document.getElementById('search');
  const input=f.add('input','search-query',form);input.name='q';input.value='synthetic needle';
  const scope=f.add('select','search-scope',form);scope.name='scope';scope.value='all';
  for(const value of ['all','selected']){const o=f.add('option','pending-'+value,scope);o.setAttribute('value',value);}
  f.run('selected={id:"11111111-1111-4111-8111-111111111111",name:"Пример"};');
  const app=source('app.js');f.run(app.slice(app.indexOf('const searchForm ='),app.indexOf("byId('logout')")));
  let resolve;f.setHandler(()=>new Promise(r=>resolve=r));
  await f.submit(form);const request=f.requests.at(-1);assert(resolve);
  scope.value='selected';scope.dispatchEvent(new f.context.Event('change'));
  assert.equal(request.opts.signal.aborted,true);
  resolve(response({mode:'semantic',hits:[{statement:'Forbidden stale result',provenance:[{source_id:'22222222-2222-4222-8222-222222222222',fragment_id:'33333333-3333-4333-8333-333333333333'}]}]}));
  await settle();
  const results=f.document.getElementById('search-results');
  assert.doesNotMatch(results.textContent,/Forbidden stale result/);
  assert.equal(f.button(results,'Открыть точное место'),undefined);
 });
 await test('Core seed localization preserves renamed core and custom type titles',async()=>{
  const f=fixture();
  f.setHandler(()=>response([
   {key:'reference',title:'Reference',base_kind:'reference',is_core:true},
   {key:'brand',title:'Новый брендовый раздел',base_kind:'brand',is_core:true},
   {key:'manual',title:'Reference',base_kind:'reference',is_core:false},
   {key:'project',title:'Project',base_kind:'project',is_core:true}
  ]));
  await f.run('loadLibraryTypes()');
  const options=f.document.getElementById('library-kind').options;
  assert.equal(options.find(o=>o.value==='reference').textContent,'Справочник');
  assert.equal(options.find(o=>o.value==='project').textContent,'Проект');
  assert.equal(options.find(o=>o.value==='brand').textContent,'Новый брендовый раздел');
  assert.equal(options.find(o=>o.value==='manual').textContent,'Reference');
  assert.equal(options.find(o=>o.value==='manual').dataset.baseKind,'reference');
 });

 await test('Route change clears stale detail without re-keying its saved draft',async()=>{
  const f=fixture(),id='11111111-1111-4111-8111-111111111111';
  const scope=f.add('select','search-scope');scope.value='selected';
  const chosen=f.add('option','chosen-library-option',scope);chosen.setAttribute('value','selected');
  f.route.params=[id];
  f.run('selected={id:'+JSON.stringify(id)+',name:"Пример"};');
  f.context.WisdomForms.configureContext(()=>({
   accountId:'fixture-account',principalId:'fixture-reader',routeName:f.route.name,
   params:f.route.params,query:'',route:'/app/'+f.route.name+'/'+f.route.params.join('/'),
   libraryId:f.route.params[0]||'',projectId:'',authorized:true
  }));
  const makeForm=(parent)=>{
   const form=f.add('form','route-draft',parent),input=f.add('input','route-draft-title',form);
   input.name='title';form.dataset.draftKey='route-rule-title';form.setAttribute('aria-label','Правило');f.context.WisdomForms.enhance(form);return {form,input};
  };
  const old=makeForm(f.document.getElementById('library-detail'));
  old.input.value='Несохранённый личный черновик';old.form.dispatchEvent(new f.context.Event('input',{target:old.input}));
  f.context.WisdomRouter.onChange=fn=>{f.context.onRouteChanged=fn;};f.context.WisdomRouter.start=()=>{};
  for(const name of ['cancelJobPoll','invalidatePrivateDialogs','clearRouteFailure']){
   if(f.run('typeof '+name)==='undefined')f.run('var '+name+'=()=>{};');
  }
  const app=source('app.js');f.run(app.slice(app.indexOf('router.onChange(')));
  const previous={...f.route,params:[...f.route.params]};
  Object.assign(f.route,{token:2,name:'uses',params:[]});
  f.context.onRouteChanged(f.route,previous);
  assert.equal(f.document.getElementById('library-detail').children.length,0);
  assert.equal(old.form.isConnected,false);assert.equal(f.run('selected'),null);assert.equal(scope.value,'all');assert.equal(chosen.disabled,true);
  const unrelated=makeForm(f.document.body);f.context.WisdomForms.refreshRecovery();
  assert.equal(f.button(unrelated.form,'Восстановить введённый текст'),undefined,'old private text must not be re-keyed to Experience');
  unrelated.form.remove();
  Object.assign(f.route,{token:3,name:'libraries',params:[id]});
  const reopened=makeForm(f.document.getElementById('library-detail'));f.context.WisdomForms.refreshRecovery();
  const restore=f.button(reopened.form,'Восстановить введённый текст');
  assert(restore,'the old library draft must remain available on reopening');
  assert.equal(restore.parentElement.hidden,false);
 });

 console.log(JSON.stringify({passed,failed:0,synthetic:true,browser:false,network:false}));
})().catch(error=>{console.error(error);process.exitCode=1;});
