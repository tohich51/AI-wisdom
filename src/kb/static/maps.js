'use strict';
window.WisdomMap = (() => {
  let epoch=0, controller, canvas, root, host;
  const invalidate=()=> { epoch++; controller?.abort(); canvas?.destroy(); canvas=null; root?.replaceChildren(); };
  async function mount(container, helpers, chosenId) {
    invalidate(); root=container; host=helpers; const generation=epoch, n=host.node;
    controller=new AbortController(); const valid=()=>generation===epoch;
    const libs=host.libraries(); if (!libs.length) { root.append(n('p','Создайте библиотеку и добавьте материалы.')); return; }
    const library=n('select',null,{id:'map-library'});
    for(const lib of libs) library.append(n('option',lib.name,{value:lib.id}));
    library.value=chosenId || host.currentLibrary()?.id || libs[0].id;
    const mode=n('select',null,{id:'map-mode'});
    mode.append(n('option','Происхождение и опыт',{value:'provenance'}),n('option','Семантическая сеть',{value:'semantic'}));
    const controls=n('div',null,{class:'map-controls'}), content=n('div',null,{class:'card'}), list=n('div',null,{class:'map-list'});
    const details=n('aside',null,{class:'card map-details','aria-live':'polite'});
    controls.append(n('label','Библиотека',{for:'map-library'}),library,n('label','Вид карты',{for:'map-mode'}),mode);
    const surface=n('div',null,{class:'map-surface',tabindex:0,'aria-label':'Граф знаний. Все узлы доступны в списке ниже.'});
    root.append(controls,content,list,details); content.append(surface);
    let data, readSerial=0;
    const endpoint=()=>'/api/graphs/'+mode.value+'?library_id='+library.value+'&limit=200';
    const act=fn=>async e=> { e?.preventDefault(); try { await fn(); } catch(error) {
      if(valid() && error.name!=='AbortError') { canvas?.destroy();canvas=null;surface.replaceChildren();list.replaceChildren();details.replaceChildren();host.notify(error.message); }
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
    }
    async function review(entityId,entityKind,decision) {
      await host.post('/api/graphs/review',{entity_id:entityId,entity_kind:entityKind,decision}); await load();
    }
    async function load() {
      const serial=++readSerial; details.replaceChildren();
      const fresh=await host.api(endpoint(),{signal:controller.signal}); if(!valid() || serial!==readSerial) return; data=fresh;
      manual.hidden=mode.value!=='semantic' || !data.capabilities?.can_create; relationButton.hidden=mode.value!=='semantic' || !data.capabilities?.can_create;
      canvas?.destroy();canvas=null; surface.replaceChildren();list.replaceChildren(n('h2','Узлы и связи'));
      const status=n('p',(mode.value==='semantic'?'Понятия и направленные смысловые связи':'Источник → фрагмент → знание или правило → опыт → результат')+
        ' · '+data.nodes.length+' узлов · '+data.edges.length+' связей'+(data.truncated?' · Показана часть карты.':''));
      content.querySelector('.map-summary')?.remove();status.className='map-summary'; content.prepend(status);
      if(mode.value==='semantic' && !data.similarity?.available) list.append(n('p','Связи построены по цитатам или добавлены вручную. Слой близости понятий по эмбеддингам пока недоступен.'));
      if(!data.nodes.length) {
        surface.append(n('p',mode.value==='semantic'?'Пока нет понятий и связей. Создайте сеть в Studio или добавьте понятие вручную.':'После загрузки источников здесь появится их происхождение.'));
        if(mode.value==='semantic') surface.append(b('Открыть Studio',()=>window.WisdomResearch.question('',[],library.value)));
      } else {
        canvas=window.WisdomGraphCanvas.render(surface,data,mode.value,node=>act(()=>renderDetail(node))());
      }
      for(const node of data.nodes) list.append(b((types[node.kind]||node.kind)+' · '+node.label,()=>renderDetail(node)));
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
    library.addEventListener('change',act(()=>mount(root,host,library.value)));
    mode.addEventListener('change',act(()=> {document.querySelectorAll('dialog[data-wisdom-private]').forEach(d=>d.close());return load();}));
    controls.append(b('Обновить',load),b('Уместить граф',()=>canvas?.fit(30)),b('Приблизить',()=>canvas?.zoom(canvas.zoom()*1.2)),b('Отдалить',()=>canvas?.zoom(canvas.zoom()/1.2)),
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
