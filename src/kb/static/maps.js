'use strict';
window.WisdomMap = (() => {
  let epoch=0, controller, canvas, root, host, similarityTimer;
  const clearSimilarityTimer=()=> { const pending=similarityTimer; similarityTimer=null; if(pending) { clearTimeout(pending.id); pending.cancel(); } };
  const invalidate=()=> { epoch++; clearSimilarityTimer(); controller?.abort(); canvas?.destroy(); canvas=null; root?.replaceChildren(); };
  const waitForSimilarity=(ms,signal)=>new Promise((resolve,reject)=>{
    let id;
    const cancel=()=> {
      if(similarityTimer?.id===id) similarityTimer=null;
      clearTimeout(id); signal.removeEventListener('abort',cancel);
      const error=new Error('Ожидание обновления отменено'); error.name='AbortError'; reject(error);
    };
    id=setTimeout(()=> {
      if(similarityTimer?.id===id) similarityTimer=null;
      signal.removeEventListener('abort',cancel); resolve();
    },ms);
    similarityTimer={id,cancel};
    if(signal.aborted)cancel();else signal.addEventListener('abort',cancel,{once:true});
  });
  async function mount(container, helpers, chosenId, chosenMode) {
    invalidate(); root=container; host=helpers; const generation=epoch, n=host.node;
    controller=new AbortController(); const valid=()=>generation===epoch;
    const libs=host.libraries(); if (!libs.length) { root.append(n('p','Создайте библиотеку и добавьте материалы.')); return; }
    const library=n('select',null,{id:'map-library'});
    for(const lib of libs) library.append(n('option',lib.name,{value:lib.id}));
    library.value=chosenId || host.currentLibrary()?.id || libs[0].id;
    const mode=n('select',null,{id:'map-mode'});
    mode.append(n('option','Происхождение и опыт',{value:'provenance'}),n('option','Семантическая сеть',{value:'semantic'}));
    mode.value=chosenMode === 'semantic' ? 'semantic' : 'provenance';
    const controls=n('div',null,{class:'map-controls'}), content=n('div',null,{class:'card'}), list=n('div',null,{class:'map-list'});
    const details=n('aside',null,{class:'card map-details','aria-live':'polite'});
    const similarityControl=n('span',null,{class:'map-similarity-control'});
    const showSimilarity=n('input',null,{id:'map-similarity',type:'checkbox',checked:true,'aria-describedby':'map-similarity-status'});
    const similarityStatus=n('span',null,{id:'map-similarity-status',class:'map-similarity-status','aria-live':'polite'});
    similarityControl.hidden=mode.value!=='semantic';
    similarityControl.append(n('label','Показывать близость понятий',{for:'map-similarity'}),showSimilarity,similarityStatus);
    controls.append(n('label','Библиотека',{for:'map-library'}),library,n('label','Вид карты',{for:'map-mode'}),mode,similarityControl);
    const surface=n('div',null,{class:'map-surface',tabindex:0,'aria-label':'Граф знаний. Все узлы доступны в списке ниже.'});
    root.append(controls,content,details,list); content.append(surface);
    let data, readSerial=0, similarityPoll=0, similarityQueued=false, similarityNotice='', similarityRefreshButton;
    const noConceptsNotice='В библиотеке нет доступных понятий.';
    const cancelSimilarityPoll=()=> { similarityPoll++; clearSimilarityTimer(); similarityQueued=false; similarityNotice=''; };
    const endpoint=()=>'/api/graphs/'+mode.value+'?library_id='+library.value+'&limit=200';
    const renderSimilarityState=()=> {
      const semantic=mode.value==='semantic', similarity=data?.similarity;
      similarityControl.hidden=!semantic;
      if(!semantic) similarityStatus.replaceChildren();
      else {
        const ready=Number.isInteger(similarity?.ready)?similarity.ready:0;
        const total=Number.isInteger(similarity?.total)?similarity.total:0;
        let text='Близость понятий: ';
        if(similarityQueued) text+='обновление в очереди; готово '+ready+'/'+total+'.';
        else if(!similarity) text+='недоступна; готово 0/0 (состояние слоя не получено).';
        else if(!similarity.available) text+='недоступна; готово '+ready+'/'+total+'. Причина: '+(similarity.reason||'не указана сервером')+'.';
        else {
          text+='готово '+ready+'/'+total+'.';
          if(!Array.isArray(similarity.edges)||!similarity.edges.length) text+=' Пар выше порога нет.';
          if(similarity.truncated) text+=' Показана часть слоя.';
        }
        if(similarityNotice) text+=' '+similarityNotice;
        similarityStatus.replaceChildren(n('span',text+' Оценка — косинус сходства эмбеддингов, а не вероятность или доказательство.'));
      }
      if(!similarityRefreshButton) return;
      similarityRefreshButton.hidden=!semantic;
      const total=Number.isInteger(similarity?.total)?similarity.total:(data?.nodes||[]).filter(node=>node.kind==='concept'&&node.publication!=='withdrawn').length;
      const noEligible=similarityNotice===noConceptsNotice||total<1;
      similarityRefreshButton.disabled=!semantic||similarityQueued||!data?.capabilities?.can_create||noEligible;
    };
    const act=fn=>async e=> { e?.preventDefault(); try { await fn(); } catch(error) {
      if(valid() && error.name!=='AbortError') {
        content.querySelector('.map-error')?.remove();
        const failure=n('p','Не удалось обновить карту: '+error.message); failure.className='map-error map-summary'; content.prepend(failure);
        if(!canvas && !surface.hasChildNodes()) surface.append(n('p','Карта сейчас недоступна. Попробуйте обновить её позже.'));
        host.notify(error.message);
      }
    }};
    const b=(text,fn)=>n('button',text,{type:'button',onclick:act(fn)});
    async function renderDetail(node) {
      const selectedMode=mode.value,selectedLibrary=library.value,selection=++readSerial;
      const fresh=await host.api(endpoint(),{signal:controller.signal});
      if(!valid() || selection!==readSerial || selectedMode!==mode.value || selectedLibrary!==library.value) return;
      node=fresh.nodes.find(value=>value.id===node.id);
      if(!node) throw Error('Узел больше не доступен. Обновите карту.');
      details.replaceChildren(n('h2',node.label),n('p',[types[node.kind]||node.kind,publication[node.publication]||node.publication,
        node.version_no?'Версия '+node.version_no:null].filter(Boolean).join(' · ')));
      if(node.body) details.append(n('p',node.body));
      if(node.basis==='manual_without_evidence') details.append(n('p','Ручное понятие без подтверждающего источника.'));
      if(node.basis==='manual_revision_with_inherited_sources') details.append(n('p','Ручная правка. Источники унаследованы от предыдущей версии; проверьте новую формулировку.'));
      if(node.kind==='source') details.append(b('Открыть источник',()=>host.openSource(node.entity_id)));
      if(node.kind==='rule') details.append(b('Открыть правило',()=>host.openRuleVersion(node.entity_id,node.version_no)));
      for(const cite of node.evidence||[]) details.append(n('blockquote',cite.quote),n('p',cite.title),b('Открыть источник',()=>host.openSource(cite.source_id)));
      if(node.kind==='concept') {
        const ids=[...new Set((node.evidence||[]).map(c=>c.source_id))];
        details.append(b('Исследовать понятие',()=>window.WisdomResearch.question('Что означает '+node.label+'?',ids,library.value)));
        if(data.capabilities?.can_review && node.publication==='draft') details.append(b('Подтвердить понятие',()=>review(node.entity_id,'concept','publish')));
        if(data.capabilities?.can_review && node.publication!=='withdrawn') details.append(b('Снять понятие с публикации',()=>review(node.entity_id,'concept','withdraw')));
      }
      details.scrollIntoView({block:'nearest'});
    }
    async function review(entityId,entityKind,decision) {
      await host.post('/api/graphs/review',{entity_id:entityId,entity_kind:entityKind,decision}); await load();
    }
    async function load(options={}) {
      const preserveSimilarityPoll=options.preserveSimilarityPoll===true;
      if(!preserveSimilarityPoll) cancelSimilarityPoll();
      const serial=++readSerial; details.replaceChildren();
      const fresh=await host.api(endpoint(),{signal:controller.signal}); if(!valid() || serial!==readSerial) return; data=fresh;
      content.querySelector('.map-error')?.remove();
      const semantic=mode.value==='semantic';
      manual.hidden=!semantic || !data.capabilities?.can_create;
      relationButton.hidden=!semantic || !data.capabilities?.can_create;
      renderSimilarityState();
      canvas?.destroy();canvas=null; surface.replaceChildren();list.replaceChildren(n('h2','Узлы и связи'));
      const status=n('p',(mode.value==='semantic'?'Понятия и направленные смысловые связи':'Источник → фрагмент → знание или правило → опыт → результат')+
        ' · '+data.nodes.length+' узлов · '+data.edges.length+' связей'+(data.truncated?' · Показана часть карты.':''));
      content.querySelector('.map-summary')?.remove();status.className='map-summary'; content.prepend(status);
      if(data.nodes.length>40) status.append(n('span',' Нажмите на узел или выберите его в списке, чтобы увидеть подробности.'));
      if(mode.value==='semantic' && !data.similarity?.available)
        list.append(n('p','Слой близости недоступен. '+(data.similarity?.reason?'Причина: '+data.similarity.reason+'.':'Причина не указана.')+' На карте остаются только типизированные связи; сходство не является доказательством.'));
      if(!data.nodes.length) {
        surface.append(n('p',mode.value==='semantic'?'Пока нет понятий и связей. Создайте сеть в Studio или добавьте понятие вручную.':'После загрузки источников здесь появится их происхождение.'));
        if(mode.value==='semantic') surface.append(b('Открыть Studio',()=>window.WisdomResearch.question('',[],library.value)));
      } else {
        // Граф грузится лениво: библиотека нужна только здесь и только когда есть узлы.
        if(typeof window.WisdomGraphCanvas.ensureLibrary==='function') await window.WisdomGraphCanvas.ensureLibrary();
        if(!valid() || serial!==readSerial) return;
        canvas=window.WisdomGraphCanvas.render(surface,data,mode.value,node=>act(()=>renderDetail(node))(),{showSimilarity:showSimilarity.checked});
      }
      const nodeList=window.WisdomList?window.WisdomList.create(host,{empty:'Узлов пока нет.',label:'Страницы списка узлов',
        renderItem:node=>b((types[node.kind]||node.kind)+' · '+node.label,()=>renderDetail(node))}):null;
      if(nodeList) { nodeList.set(data.nodes); list.append(nodeList.element,nodeList.navigation,nodeList.status); }
      else for(const node of data.nodes) list.append(b((types[node.kind]||node.kind)+' · '+node.label,()=>renderDetail(node)));
      const labels=new Map(data.nodes.map(v=>[v.id,v.label]));
      for(const edge of data.edges) {
        const row=n('article',null,{class:'graph-relation'});
        row.append(n('p',labels.get(edge.source)+' → '+(predicates[edge.predicate]||edge.predicate)+' → '+labels.get(edge.target)),
          n('p',(edge.hypothesis?'Гипотеза · ':'')+(publication[edge.publication]||edge.basis)));
        if(data.capabilities?.can_review && mode.value==='semantic' && edge.publication==='draft') row.append(b('Подтвердить связь',()=>review(edge.entity_id,'relation','publish')));
        for(const cite of Array.isArray(edge.evidence)?edge.evidence:[]) row.append(n('blockquote',cite.quote),n('p',cite.title));
        list.append(row);
      }
    }
    library.addEventListener('change',act(()=>mount(root,host,library.value,mode.value)));
    mode.addEventListener('change',act(()=> {
      document.querySelectorAll('dialog[data-wisdom-private]').forEach(d=>d.close());
      const semantic=mode.value==='semantic';
      similarityControl.hidden=!semantic;
      if(similarityRefreshButton) { similarityRefreshButton.hidden=!semantic; similarityRefreshButton.disabled=true; }
      if(!semantic) similarityStatus.replaceChildren();
      return load();
    }));
    showSimilarity.addEventListener('change',act(load));
    async function refreshSimilarity() {
      if(mode.value!=='semantic' || !data?.capabilities?.can_create) return;
      const total=Number.isInteger(data.similarity?.total)?data.similarity.total:
        (data.nodes||[]).filter(node=>node.kind==='concept'&&node.publication!=='withdrawn').length;
      if(total<1) return;
      const selectedMode=mode.value, selectedLibrary=library.value, signal=controller.signal;
      clearSimilarityTimer();
      const operation=++similarityPoll;
      const active=()=>valid()&&!signal.aborted&&operation===similarityPoll&&selectedMode===mode.value&&selectedLibrary===library.value;
      similarityQueued=true; similarityNotice=''; renderSimilarityState();
      try {
        const result=await host.post('/api/graphs/similarity',{library_id:selectedLibrary});
        if(!active()) return;
        if(result?.status==='no_concepts') {
          similarityNotice=noConceptsNotice;
          await load({preserveSimilarityPoll:true});
          return;
        }
        for(let attempt=0;attempt<20&&active();attempt+=1) {
          await waitForSimilarity(3000,signal);
          if(!active()) return;
          await load({preserveSimilarityPoll:true});
          const current=data?.similarity;
          if(current?.available&&current.ready>=current.total) return;
          if(current?.total===0) return;
        }
        if(active()) similarityNotice='После 20 проверок результат не готов; обновление можно повторить позже.';
      } finally {
        if(active()) { similarityQueued=false; renderSimilarityState(); }
      }
    }
    similarityRefreshButton=b('Обновить близость',refreshSimilarity);
    similarityRefreshButton.disabled=true;
    controls.append(b('Обновить',load),similarityRefreshButton,b('Уместить граф',()=>canvas?.fit(30)),b('Приблизить',()=>canvas?.zoom(canvas.zoom()*1.2)),b('Отдалить',()=>canvas?.zoom(canvas.zoom()/1.2)),
      b('Скачать JSON',async()=> { const fresh=await host.api(endpoint(),{signal:controller.signal});if(valid())host.download('knowledge-graph.json',JSON.stringify(fresh,null,2),'application/json'); }),
      b('Скачать PNG',async()=> { await load(); if(!canvas)throw Error('В карте пока нет узлов.');const blob=await canvas.png({output:'blob-promise',bg:'#ffffff',full:true,scale:2});if(valid())host.downloadBlob('knowledge-graph.png',blob); }));
    const manual=b('Добавить понятие',()=>host.dialog('Понятие без источника',form=> {
      for(const [name,label] of [['label','Название понятия'],['body','Описание']]) {
        const id='concept-'+name,input=n(name==='body'?'textarea':'input',null,{id,name,required:true,maxlength:name==='body'?2000:500});form.append(n('label',label,{for:id}),input);
      }
    },async fields=> { await host.post('/api/graphs/concepts',{library_id:library.value,label:fields.get('label'),body:fields.get('body')});if(valid())await load(); }));
    controls.append(manual);
    const relationButton=b('Добавить связь',()=> {
      if(mode.value!=='semantic' || data.nodes.length<2) throw Error('Откройте семантическую сеть с двумя понятиями.');
      host.dialog('Ручная связь · без доказательства',form=> {
        for(const [name,label] of [['from_concept','От понятия'],['to_concept','К понятию']]) {
          const select=n('select',null,{id:name,name}); for(const node of data.nodes)select.append(n('option',node.label,{value:node.entity_id})); form.append(n('label',label,{for:name}),select);
        }
        const select=n('select',null,{id:'predicate',name:'predicate'});
        for(const p of ['supports','part_of','used_for','contradicts'])select.append(n('option',predicates[p],{value:p})); form.append(n('label','Тип связи',{for:'predicate'}),select);
      },async fields=> {await host.post('/api/graphs/relations',{library_id:library.value,from_concept:fields.get('from_concept'),to_concept:fields.get('to_concept'),predicate:fields.get('predicate'),hypothesis:true});if(valid())await load();});
    });
    controls.append(relationButton);
    await act(load)();
  }
  const types={source:'Источник',fragment:'Фрагмент',knowledge:'Знание',rule:'Правило',use:'Использование',outcome:'Результат',concept:'Понятие'};
  const publication={draft:'Черновик',published:'Подтверждено',private:'Личное',original:'Оригинал',withdrawn:'Снято с публикации'};
  const predicates={contains:'содержит',grounds:'подтверждает',retrieved:'получено',used:'применено',reports:'зафиксирован результат',supports:'поддерживает',part_of:'часть',used_for:'используется для',contradicts:'противоречит'};
  return {mount,invalidate};
})();
