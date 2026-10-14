'use strict';
window.WisdomResearch = (() => {
  let epoch = 0, controller, timer, root, host, pending;
  const invalidate = () => {
    epoch++; controller?.abort(); clearTimeout(timer);
    root?.replaceChildren();
    document.querySelectorAll('dialog[data-wisdom-private]').forEach(d => d.close());
  };
  function question(query, sourceIds, libraryId) {
    pending = {query, sourceIds, libraryId}; host?.navigateResearch();
  }
  async function mount(container, helpers, libraryId) {
    invalidate(); root = container; host = helpers;
    const generation = epoch; controller = new AbortController();
    const n = host.node; let selected = new Set(), currentRun, runSerial=0, operation, listSerial=0;
    const valid = () => generation === epoch;
    const request = async (path, options = {}) => {
      const data = await host.api(path, {...options, signal: controller.signal});
      if (!valid()) throw new DOMException('Отменено', 'AbortError');
      return data;
    };
    const action = fn => async e => {
      e?.preventDefault();
      try { await fn(); } catch (error) {
        if (valid() && error.name !== 'AbortError') { result.replaceChildren(); host.notify(error.message); }
      }
    };
    const b = (text, fn) => n('button', text, {type:'button', onclick:action(fn)});
    const fetchPost = (path, data, method = 'POST') => request(path, {method, body:JSON.stringify(data)});
    const libs = host.libraries();
    if (!libs.length) { root.append(n('p','Создайте библиотеку и загрузите источники.')); return; }
    const chosen = pending?.libraryId || libraryId || host.currentLibrary()?.id || libs[0].id;
    const selector = n('select',null,{id:'research-library'});
    for (const lib of libs) selector.append(n('option',lib.name,{value:lib.id}));
    selector.value = libs.some(l => l.id === chosen) ? chosen : libs[0].id;
    const libId = selector.value;
    selector.addEventListener('change', () => { pending = null; mount(root,host,selector.value).catch(e=>host.notify(e.message)); });
    root.append(n('label','Библиотека',{for:'research-library'}),selector);
    const layout=n('div',null,{class:'research-layout'}), sources=n('aside',null,{class:'card'});
    const work=n('div',null,{class:'research-work'}), studio=n('aside',null,{class:'card'});
    const result=n('div',null,{class:'research-result','aria-live':'polite'});
    const history=n('div',null,{class:'card'}), notes=n('div',null,{class:'card'});
    layout.append(sources,work,studio); root.append(layout); work.append(result,history,notes);
    const freshRun = id => request('/api/research/'+id);
    const isActive = status => status === 'queued' || status === 'running';
    const runStatus = new Map();
    const rememberRun = (id,status) => {
      runStatus.delete(id); runStatus.set(id,status);
      if (runStatus.size>100) runStatus.delete(runStatus.keys().next().value);
    };
    const locate = cite => [cite.locator?.chapter, cite.locator?.file_page != null ? 'Страница файла '+cite.locator.file_page : null,
      cite.locator?.printed_label, cite.locator?.paragraph != null ? 'Абзац '+cite.locator.paragraph : null].filter(Boolean).join(' · ');
    async function citation(id) {
      const runId=currentRun, selection=runSerial;
      const fresh=await freshRun(runId); if(runId!==currentRun || selection!==runSerial) return; const cite=fresh.result?.citations.find(c=>c.id===id);
      if (!cite) throw Error('Цитата больше не доступна.');
      const d=n('dialog',null,{'data-wisdom-private':'true'});
      d.append(n('h2',cite.title),n('p',[cite.author,cite.edition].filter(Boolean).join(' · ')),n('p',locate(cite)),n('blockquote',cite.quote));
      d.append(b('Открыть источник',()=>host.openSource(cite.source_id)),b('Закрыть',()=>d.close()));
      d.addEventListener('close',()=>d.remove()); document.body.append(d); d.showModal();
    }
    async function refreshLists(owner) {
      const requestSerial=++listSerial;
      const [runs,saved]=await Promise.all([request('/api/research?library_id='+libId),request('/api/notes?library_id='+libId)]);
      if (!valid() || requestSerial!==listSerial || (owner && (owner.id!==currentRun || owner.serial!==runSerial))) return false;
      history.replaceChildren(n('h3','История исследований'));
      if (!runs.runs.length) history.append(n('p','Пока нет исследований.'));
      for (const run of runs.runs) history.append(b((run.query || labels[run.kind])+' · '+statusLabel(run.status),()=>showRun(run.id)));
      notes.replaceChildren(n('h3','Мои заметки'));
      if (!saved.notes.length) notes.append(n('p','Сохраните полезный результат как заметку. Она будет доступна на других устройствах.'));
      for (const note of saved.notes) notes.append(b(note.title,async()=> {
        const selection=++runSerial;
        const fresh=await request('/api/notes/'+note.id); if(selection!==runSerial) return;
        const shown=await showRun(fresh.note.research_run_id);
        if (!valid() || !shown || currentRun!==fresh.note.research_run_id) return;
        result.append(n('h3','Личный комментарий'),n('p',fresh.note.comment),b('Изменить заметку',()=>editNote(fresh.note)));
      }));
      if (runs.truncated || saved.truncated) history.append(n('p','Показаны последние записи.'));
      return true;
    }
    function formFields(form, fields) {
      for (const [key,label,value,type='textarea'] of fields) {
        const id='research-'+crypto.randomUUID(), input=n(type,null,{id,name:key});
        input.value=value ?? ''; input.required=key!=='comment';
        input.maxLength=key==='comment'?8000:key==='title'?500:2000;
        form.append(n('label',label,{for:id}),input);
      }
    }
    function editNote(note) {
      host.dialog('Изменить заметку',form=>formFields(form,[['title','Название',note.title,'input'],['comment','Комментарий',note.comment]]),async data=> {
        await fetchPost('/api/notes/'+note.id,{title:data.get('title'),comment:data.get('comment'),revision:note.revision},'PATCH');
        await refreshLists(); host.notify('Заметка сохранена.');
      });
    }
    async function showRun(id) {
      clearTimeout(timer); currentRun=id; const serial=++runSerial;
      const run=await freshRun(id); if(serial!==runSerial || currentRun!==id) return false;
      const wasActive=isActive(runStatus.get(id)), terminal=run.status==='done' || run.status==='failed';
      rememberRun(id,run.status);
      const finishTerminal=async()=> {
        if (terminal && wasActive) {
          try { await refreshLists({id,serial}); }
          catch (error) {
            if (!valid() || serial!==runSerial || currentRun!==id) return false;
            if (error.name==='AbortError') throw error;
            host.notify(error.message);
          }
        }
        return valid() && serial===runSerial && currentRun===id;
      };
      result.replaceChildren(n('h2',run.query || labels[run.kind]));
      if (isActive(run.status)) {
        result.append(n('p',statusLabel(run.status)+'. Можно открыть другой раздел: результат сохранится в базе.'));
        timer=setTimeout(()=>action(()=>showRun(id))(),5000); return true;
      }
      if (run.status==='failed') {
        const message=run.error_code==='access_revoked'
          ? 'Не удалось создать результат: нет доступа к одному или нескольким источникам. Восстановите доступ и попробуйте ещё раз.'
          : run.error_code==='generation_failed'
          ? 'Не удалось выполнить генерацию. Попробуйте ещё раз.'
          : 'Не удалось создать результат. Попробуйте ещё раз.';
        result.append(n('p',message)); return finishTerminal();
      }
      window.WisdomResult.render(result,run.result,{node:n,button:b,citation,question:(q,ids)=>question(q,ids,libId),notify:host.notify});
      if (run.result.no_evidence) return finishTerminal();
      const actions=n('div',null,{class:'actions'}); result.append(actions);
      actions.append(b('Сохранить заметку',()=>host.dialog('Новая заметка',form=>formFields(form,[['title','Название',run.result.title,'input'],['comment','Личный комментарий','']]),async data=> {
        await fetchPost('/api/notes',{run_id:id,title:data.get('title'),comment:data.get('comment')}); await refreshLists(); host.notify('Заметка сохранена в базе.');
      })),b('Скачать Markdown',async()=> {
        const doc=await request('/api/research/'+id+'/markdown'); host.download(doc.filename,doc.markdown,'text/markdown');
      }),b('Создать черновик правила',()=>host.dialog('Черновик правила',form=>formFields(form,[['title','Название',run.result.title,'input'],['when_to_apply','Когда применять',''],['expected_effect','Ожидаемый результат','']]),async data=> {
        await fetchPost('/api/research/'+id+'/draft-rule',{run_id:id,title:data.get('title'),when_to_apply:data.get('when_to_apply'),expected_effect:data.get('expected_effect')});
        host.notify('Черновик создан. Проверьте и опубликуйте его в библиотеке.');
      })));
      return finishTerminal();
    }
    async function start(kind,query) {
      if (!selected.size || selected.size>10) throw Error('Выберите от 1 до 10 готовых источников.');
      if (kind==='comparison' && selected.size<2) throw Error('Для сравнения нужны минимум два источника.');
      if (kind==='research' && !query.trim()) throw Error('Напишите вопрос.');
      submit.disabled=generate.disabled=true;
      try {
        const fingerprint=JSON.stringify({library_id:libId,source_ids:[...selected].sort(),kind,query});
        if(!operation || operation.fingerprint!==fingerprint) operation={fingerprint,key:crypto.randomUUID()};
        const job=await fetchPost('/api/research',{library_id:libId,source_ids:[...selected],kind,query,idempotency_key:operation.key});
        operation=null;
        await showRun(job.run_id); await refreshLists();
      } finally { if (valid()) submit.disabled=generate.disabled=!canGenerate; }
    }
    let canGenerate=false;
    const ask=n('form',null,{class:'card'}), query=n('textarea',null,{id:'research-query',maxlength:2000,rows:4});
    query.value=pending?.query || ''; ask.append(n('h2','Исследование'),n('label','Вопрос по выбранным источникам',{for:'research-query'}),query);
    const submit=n('button','Исследовать',{type:'submit',disabled:true}); ask.append(submit); work.prepend(ask);
    ask.addEventListener('submit',action(()=>start('research',query.value)));
    studio.append(n('h2','Studio'),n('p','Создайте полезный формат из тех же источников.'));
    const format=n('select',null,{id:'studio-format'});
    for (const [kind,label] of Object.entries(labels).filter(([kind])=>kind!=='research')) format.append(n('option',label,{value:kind}));
    const generate=b('Сгенерировать',()=>start(format.value,query.value)); generate.disabled=true;
    studio.append(n('label','Формат',{for:'studio-format'}),format,generate,n('p','Аудио, видео и слайды пока не подключены.'));
    try {
      const data=await request('/api/research/sources?library_id='+libId); canGenerate=data.can_generate;
      sources.append(n('h2','Источники'),n('p','Выберите материалы. Оригиналы остаются в библиотеке.'));
      for (const source of data.sources) {
        const id='source-'+source.id, check=n('input',null,{id,type:'checkbox',value:source.id});
        check.disabled=!source.text_available;
        if (pending?.sourceIds?.includes(source.id) && !check.disabled) { check.checked=true; selected.add(source.id); }
        check.addEventListener('change',()=>check.checked?selected.add(source.id):selected.delete(source.id));
        const row=n('div',null,{class:'source-option'}); row.append(check,n('label',source.title+(source.partial?' · частично обработан':''),{for:id})); sources.append(row);
      }
      pending=null; submit.disabled=generate.disabled=!canGenerate;
      if (!canGenerate) studio.append(n('p','Генерация доступна владельцу с правами добавления материалов.'));
      if (data.truncated) sources.append(n('p','Показаны 100 последних источников.'));
      if (!data.sources.length) sources.append(n('p','Загрузите и обработайте материал в разделе «Библиотеки».'));
      await refreshLists();
    } catch (error) { if (valid() && error.name!=='AbortError') { result.replaceChildren(); host.notify(error.message); } }
  }
  const labels={research:'Исследование',summary:'Краткое содержание',faq:'Вопросы и ответы',comparison:'Сравнение',checklist:'Чек-лист',flashcards:'Карточки',topic:'Карта тем',network:'Семантическая сеть'};
  const statusLabel=value=>({queued:'В очереди',running:'Генерация',done:'Готово',failed:'Ошибка'}[value]||value);
  return {mount,invalidate,question,configure:helpers=>{host=helpers;}};
})();
