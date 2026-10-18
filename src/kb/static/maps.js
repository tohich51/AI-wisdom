'use strict';
window.WisdomMap = (() => {
  let epoch=0, controller, canvas, root, host, similarityTimer, pendingFocusIntent=null;
  const draftOperations=new Map();
  const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const clearSimilarityTimer=()=> { const pending=similarityTimer; similarityTimer=null; if(pending) { clearTimeout(pending.id); pending.cancel(); } };
  const invalidate=()=> { epoch++; clearSimilarityTimer(); controller?.abort(); canvas?.destroy(); canvas=null; root?.replaceChildren(); };
  const captureFocusIntent=token=> {
    const active=document.activeElement;
    pendingFocusIntent={token,intent:active?.id==='map-library'?'library':active?.id==='map-mode'?'mode':'heading'};
  };
  const restoreFocus=token=> {
    if(!pendingFocusIntent || pendingFocusIntent.token!==token || !host?.routeIsCurrent(token) || !root?.isConnected)return;
    const intent=pendingFocusIntent.intent;
    pendingFocusIntent=null;
    const target=intent==='library'?root.querySelector('#map-library'):intent==='mode'?root.querySelector('#map-mode'):null;
    if(target instanceof HTMLElement && !target.disabled)target.focus({preventScroll:true});
    else window.WisdomRouter?.focusHeading();
  };
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
  async function mount(container, helpers, chosenId, chosenMode, chosenSourceId) {
    invalidate(); root=container; host=helpers; const generation=epoch, routeToken=host.routeToken(), n=host.node;
    const graphId=window.WisdomGraphCanvas.canonicalId;
    const uniqueRowsById=(rows,keepMissing=false)=>window.WisdomGraphCanvas.uniqueRowsById(rows,keepMissing);
    const visibleGraphRows=graph=>{
      const nodes=uniqueRowsById(Array.isArray(graph?.nodes)?graph.nodes:[]);
      const nodeIds=new Set(nodes.map(node=>graphId(node.id)));
      const candidates=uniqueRowsById(Array.isArray(graph?.edges)?graph.edges:[],true);
      const edges=candidates.filter(edge=>{
        const source=graphId(edge?.source),target=graphId(edge?.target);
        return source!==target&&nodeIds.has(source)&&nodeIds.has(target);
      });
      return {nodes,edges};
    };
    const entityIdentity=value=>{
      if(typeof value?.kind!=='string'||!value.kind.trim()||typeof value.entity_id!=='string'||!uuid.test(value.entity_id))return null;
      return JSON.stringify([value.kind,value.entity_id]);
    };
    controller=new AbortController(); const valid=()=>host.routeIsCurrent(routeToken)&&generation===epoch&&!controller.signal.aborted;
    let sourceFilter=typeof chosenSourceId==='string'&&chosenSourceId?chosenSourceId:null;
    const libs=host.libraries(); if (!libs.length) { root.append(n('p','Создайте библиотеку и добавьте материалы.')); return; }
    const libraryId=chosenId ?? host.currentLibrary()?.id ?? libs[0].id;
    const mapMode=chosenMode ?? 'provenance';
    if (!libs.some(lib=>lib.id===libraryId) || !['provenance','semantic'].includes(mapMode)) {
      root.append(
        n('p','Не удалось открыть карту: выбранная библиотека или режим недоступен.'),
        n('button','Открыть первую доступную библиотеку в режиме «Происхождение и опыт»',{onclick:()=>host.navigateMapSource(libs[0].id,null,'provenance')})
      );
      return;
    }
    const operationId=operation=>{
      const key=libraryId+':'+operation;
      if(!draftOperations.has(key))draftOperations.set(key,crypto.randomUUID());
      return draftOperations.get(key);
    };
    const library=n('select',null,{id:'map-library'});
    for(const lib of libs) library.append(n('option',lib.name,{value:lib.id}));
    library.value=libraryId;
    const mode=n('select',null,{id:'map-mode'});
    mode.append(n('option','Происхождение и опыт',{value:'provenance'}),n('option','Семантическая сеть',{value:'semantic'}));
    mode.value=(sourceFilter || mapMode === 'semantic') ? 'semantic' : 'provenance';
    const controls=n('div',null,{class:'map-controls'}), content=n('div',null,{class:'card'}), list=n('div',null,{class:'map-list'});
    const details=n('aside',null,{class:'card map-details','aria-live':'polite'});
    const similarityControl=n('span',null,{class:'map-similarity-control'});
    const showSimilarity=n('input',null,{id:'map-similarity',type:'checkbox',checked:true,'aria-describedby':'map-similarity-status'});
    const similarityStatus=n('span',null,{id:'map-similarity-status',class:'map-similarity-status','aria-live':'polite'});
    similarityControl.hidden=mode.value!=='semantic';
    similarityControl.append(n('label','Показывать близость понятий',{for:'map-similarity'}),showSimilarity,similarityStatus);
    controls.append(n('label','Библиотека',{for:'map-library'}),library,n('label','Вид карты',{for:'map-mode'}),mode,similarityControl);
    const surface=n('div',null,{class:'map-surface',tabindex:0,'aria-label':'Граф знаний. Все узлы и связи доступны в списке ниже.'});
    const sourceNotice=n('div',null,{'aria-live':'polite'});
    const searchPanel=n('div',null,{class:'map-search card'}), searchId='map-search-'+crypto.randomUUID();
    const searchInput=n('input',null,{id:searchId,type:'search',autocomplete:'off',enterkeyhint:'search',spellcheck:'false',disabled:true,placeholder:'Название или описание','aria-describedby':searchId+'-help '+searchId+'-status'});
    const clearSearch=n('button','Очистить поиск',{type:'button'});
    const searchHelp=n('p','Ищет только по названию и описанию уже загруженных доступных узлов; запросы не отправляются. Граф, фильтр источника и скачиваемый JSON не меняются. Enter открывает первый результат.',{class:'muted',id:searchId+'-help'});
    const searchStatus=n('p','Поиск станет доступен после загрузки карты.',{class:'map-search-status',id:searchId+'-status',role:'status','aria-live':'polite','aria-atomic':'true'});
    let searchQuery='',searchHighlightId=null,searchComposing=false;
    searchPanel.append(n('label','Поиск по загруженной карте',{for:searchId}),searchInput,clearSearch,searchHelp,searchStatus);
    root.append(controls,searchPanel,sourceNotice,content,details,list); content.append(surface);
    let data, loading=true, readSerial=0, graphSerial=0, similarityPoll=0, similarityQueued=false, similarityNotice='', similarityRefreshButton=null;
    const noConceptsNotice='В библиотеке нет доступных понятий.';
    const filterGraphToSource=(graph,graphMode=mode.value,activeSource=sourceFilter)=>{
      if(graphMode!=='semantic'||!activeSource)return graph;
      const graphRows=visibleGraphRows(graph);
      const nodes=graphRows.nodes.filter(value=>value&&value.kind==='concept'&&Array.isArray(value.evidence)&&value.evidence.some(cite=>cite&&typeof cite.source_id==='string'&&cite.source_id===activeSource));
      const nodeIds=new Set(nodes.map(value=>graphId(value.id)));
      const keepPair=edge=>{
        if(!edge)return false;
        const source=graphId(edge.source),target=graphId(edge.target);
        return source!==target&&nodeIds.has(source)&&nodeIds.has(target);
      };
      const similarity=graph.similarity&&typeof graph.similarity==='object'?{
        ...graph.similarity,
        edges:Array.isArray(graph.similarity.edges)?graph.similarity.edges.filter(keepPair):[]
      }:graph.similarity;
      return {...graph,nodes,edges:graphRows.edges.filter(keepPair),similarity};
    };
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
      const total=Number.isInteger(similarity?.total)?similarity.total:visibleGraphRows(data).nodes.filter(node=>node.kind==='concept'&&node.publication!=='withdrawn').length;
      const noEligible=similarityNotice===noConceptsNotice||total<1;
      similarityRefreshButton.disabled=!semantic||similarityQueued||!data?.capabilities?.can_create||noEligible;
    };
    const act=fn=>async e=> { e?.preventDefault(); try { await fn(); } catch(error) {
        if(valid() && error.name!=='AbortError') {
          content.querySelector('.map-error')?.remove();
          const failure=n('p','Не удалось обновить карту: '+error.message); failure.className='map-error map-summary'; content.prepend(failure);
          if(!canvas) surface.replaceChildren(n('p','Карта сейчас недоступна. Нажмите «Обновить», чтобы повторить загрузку.'));
          host.notify(error.message);
        }
    }};
    const b=(text,fn)=>n('button',text,{type:'button',onclick:act(fn)});
    const renderCitation=(cite,returnLabel,onReturn,isCallerCurrent)=>{
      const block=n('div',null,{class:'graph-citation'});
      const quote=typeof cite.quote==='string'?cite.quote:'';
      const locator=cite.locator&&typeof cite.locator==='object'?cite.locator:{};
      const place=[
        locator.chapter!=null&&locator.chapter!==''?'Глава: '+locator.chapter:null,
        locator.file_page!=null?'Страница файла '+locator.file_page:null,
        locator.printed_label?'Печатная метка: '+locator.printed_label:null,
        locator.paragraph!=null?'Абзац: '+locator.paragraph:null,
        locator.table_index!=null?'Таблица '+locator.table_index+(locator.cell_row!=null||locator.cell_column!=null?', ячейка '+String(locator.cell_row??'?')+':'+String(locator.cell_column??'?'):''):null
      ].filter(Boolean).join(' · ');
      block.append(n('blockquote',quote||'Текст цитаты не указан.'),n('p',[cite.title,place].filter(value=>value!=null&&value!=='').join(' · ')||'Источник цитаты'));
      const hasLocator=['chapter','file_page','printed_label','paragraph','table_index','cell_row','cell_column'].some(key=>locator[key]!=null);
      if(typeof cite.source_id==='string'&&cite.source_id.trim()) {
        const sourceOptions={quote,locator,isCurrent:isCallerCurrent,returnLabel,onReturn:onReturn?()=>{if(isCallerCurrent())return onReturn();}:undefined};
        if(cite.fragment_id!=null)sourceOptions.fragmentId=cite.fragment_id;
        block.append(b(quote||cite.fragment_id!=null||hasLocator?'Открыть точное место':'Открыть источник',()=>host.openSource(cite.source_id,sourceOptions)));
      }
      return block;
    };
    const renderSourceNotice=()=>{
      sourceNotice.replaceChildren();
      if(!sourceFilter)return;
      const count=Array.isArray(data?.nodes)?visibleGraphRows(data).nodes.length:null;
      const base='Выбранный источник';
      const scope=loading?'Загружаю связанные понятия…':count===null?'Связанные понятия не загружены; предыдущий граф очищен.':count?'Показано связанных понятий: '+count+'.':'В доступной выдаче связанных понятий пока нет.';
      sourceNotice.append(n('p',base+' · '+scope),b('Показать все понятия',()=>host.navigateMapSource(library.value,null,mode.value)));
      sourceNotice.append(n('p','Ручное создание понятия скрыто: понятие без доказательства не сохранилось бы в этом режиме источника. Сначала откройте сеть всей библиотеки.',{class:'muted'}));
    };
    async function renderDetail(node) {
      const selectedIdentity=entityIdentity(node),selectedMode=mode.value,selectedLibrary=library.value,selection=++readSerial;
      const signal=controller.signal;
      const selectedCurrent=()=>valid()&&selection===readSerial&&selectedMode===mode.value&&selectedLibrary===library.value;
      const showUnavailable=()=> {
        if(!selectedCurrent()||signal.aborted)return;
        details.replaceChildren(n('p','Подробности узла недоступны: идентичность записи не подтверждена однозначно. Обновите карту.'));
      };
      if(!selectedIdentity){showUnavailable();return;}
      const currentNodes=Array.isArray(data?.nodes)?data.nodes:[];
      const currentIdentityMatches=currentNodes.filter(value=>entityIdentity(value)===selectedIdentity);
      const visibleCurrentNodes=visibleGraphRows(data).nodes;
      if(currentIdentityMatches.length!==1||!visibleCurrentNodes.includes(currentIdentityMatches[0])){showUnavailable();return;}
      details.replaceChildren(n('p','Загрузка подробностей…'));
      let freshNode;
      if(selectedMode==='semantic' && node.kind==='concept') {
        freshNode=await host.api('/api/graphs/concepts/'+encodeURIComponent(node.entity_id)+'?library_id='+encodeURIComponent(selectedLibrary),{signal});
      } else {
        const fresh=await host.api(endpoint(),{signal});
        const freshNodes=Array.isArray(fresh?.nodes)?fresh.nodes:[];
        const identityMatches=freshNodes.filter(value=>entityIdentity(value)===selectedIdentity);
        const visibleFreshNodes=visibleGraphRows(fresh).nodes;
        if(identityMatches.length!==1||!identityMatches[0]||!visibleFreshNodes.includes(identityMatches[0])){showUnavailable();return;}
        freshNode=identityMatches[0];
      }
      if(!selectedCurrent() || signal.aborted) return;
      if(!freshNode||entityIdentity(freshNode)!==selectedIdentity){showUnavailable();return;}
      node=freshNode;
      const detailHeading=n('h2',node.label,{id:'map-detail-heading-'+crypto.randomUUID(),tabindex:'-1'});
      details.replaceChildren(detailHeading,n('p',[types[node.kind]||node.kind,publication[node.publication]||node.publication,
        node.version_no?'Версия '+node.version_no:null].filter(Boolean).join(' · ')));
      if(node.body) details.append(n('p',node.body));
      if(node.basis==='manual_without_evidence') details.append(n('p','Ручное понятие без подтверждающего источника.'));
      if(node.basis==='manual_revision_with_inherited_sources') details.append(n('p','Ручная правка. Источники унаследованы от предыдущей версии; проверьте новую формулировку.'));
      if(node.kind==='source') {
        details.append(b('Открыть источник',()=>host.openSource(node.entity_id,{isCurrent:selectedCurrent})));
        const sourceId=typeof node.entity_id==='string'?node.entity_id:'';
        if(sourceId) details.append(b('Показать связанные понятия',()=>host.navigateMapSource(selectedLibrary,sourceId,'semantic')));
      }
      if(node.kind==='rule') details.append(b('Открыть правило',()=>host.openRuleVersion(node.entity_id,node.version_no)));
      const evidence=Array.isArray(node.evidence)?node.evidence:[];
      for(const cite of evidence) details.append(renderCitation(cite,'Вернуться к понятию',()=>{if(selectedCurrent())return renderDetail(node);},selectedCurrent));
      if(node.kind==='concept') {
        const ids=[...new Set(evidence.map(cite=>cite?.source_id).filter(id=>typeof id==='string'&&id.trim()))];
        if(sourceFilter) details.append(n('a','Вернуться к материалу',{href:host.librarySourcesPath(selectedLibrary)}));
        if(ids.length) details.append(b('Исследовать понятие',()=>window.WisdomResearch.question('Что означает '+node.label+'?',ids,selectedLibrary)));
        else details.append(n('p','Доступных источников для исследования этого понятия нет.'));
        if(data.capabilities?.can_review && node.publication==='draft') details.append(b('Подтвердить понятие',()=>review(node.entity_id,'concept','publish')));
        if(data.capabilities?.can_review && node.publication!=='withdrawn') details.append(b('Снять понятие с публикации',()=>review(node.entity_id,'concept','withdraw')));
      }
      if(selectedCurrent()) {
        detailHeading.focus({preventScroll:true});
        details.scrollIntoView({block:'nearest'});
      }
    }
    function renderDetailEdge(edge) {
      if(!valid())return;
      const selectedMode=mode.value,selectedLibrary=library.value,selectedSource=sourceFilter,selection=++readSerial;
      const selectedCurrent=()=>valid()&&selection===readSerial&&selectedMode===mode.value&&selectedLibrary===library.value&&selectedSource===sourceFilter;
      const graphRows=visibleGraphRows(data);
      if(!graphRows.edges.includes(edge))return;
      const nodesById=new Map(graphRows.nodes.map(node=>[graphId(node.id),node]));
      const source=nodesById.get(graphId(edge.source));
      const target=nodesById.get(graphId(edge.target));
      if(!source||!target)return;
      const sourceText=String(source.label||types[source.kind]||'Узел');
      const targetText=String(target.label||types[target.kind]||'Узел');
      const predicateText=typeof edge.predicate==='string'&&edge.predicate?(predicates[edge.predicate]||edge.predicate):'тип связи не указан';
      const headingText=[sourceText,predicateText,targetText].join(' → ');
      const heading=n('h2',headingText,{id:'map-edge-heading-'+crypto.randomUUID(),tabindex:'-1'});
      const statusText=[publication[edge.publication],typeof edge.basis==='string'?edge.basis:'',edge.hypothesis?'Гипотеза':''].filter(Boolean).join(' · ')||'Типизированная связь';
      details.replaceChildren(heading,n('p',statusText));
      const evidence=Array.isArray(edge.evidence)?edge.evidence:[];
      if (!evidence.length) details.append(n('p','Доказательства не указаны: эта связь не подтверждена.'));
      for(const cite of evidence) details.append(renderCitation(cite,'Вернуться к связи',()=>{
        if(!selectedCurrent())return;
        heading.focus({preventScroll:true});details.scrollIntoView({block:'nearest'});
      },selectedCurrent));
      if(selectedMode==='semantic'&&data.capabilities?.can_review&&edge.publication==='draft')
        details.append(b('Подтвердить связь',()=>review(edge.entity_id,'relation','publish')));
      if(selectedCurrent()) {
        heading.focus({preventScroll:true});
        details.scrollIntoView({block:'nearest'});
      }
    }
    async function review(entityId,entityKind,decision) {
      if(!valid())return;
      await host.post('/api/graphs/review',{entity_id:entityId,entity_kind:entityKind,decision});
      if(valid())await load();
    }
    const foldSearch=value=>String(value??'').toLocaleLowerCase().replace(/\s+/g,' ').trim();
    const matchingNodes=()=> {
      const nodes=visibleGraphRows(data).nodes;
      return searchQuery?nodes.filter(node=>foldSearch(node?.label).includes(searchQuery)||foldSearch(node?.body).includes(searchQuery)):nodes.slice();
    };
    const renderLoadedList=()=>{
      const updateSearchMarker=id=> {
        searchHighlightId=graphId(id);
        const selected=searchHighlightId;
        for(const button of list.querySelectorAll('button[data-map-search-result]')) {
          const marker=button.querySelector('[data-map-search-marker]');
          const current=graphId(button.dataset.mapSearchResult)===selected;
          button.classList.toggle('map-search-current',current);
          if(current) {
            button.setAttribute('aria-current','true');
            if(!marker)button.prepend(n('strong','Выделенный результат: ',{'data-map-search-marker':'true'}));
          } else {
            button.removeAttribute('aria-current');
            marker?.remove();
          }
        }
      };
      if(searchQuery&&searchHighlightId!==null&&list.querySelector('button[data-map-search-result]')) {
        updateSearchMarker(searchHighlightId);
        return;
      }
      const graphRows=visibleGraphRows(data),allNodes=graphRows.nodes,nodes=matchingNodes();
      const nodeIds=new Set(nodes.map(node=>graphId(node.id)));
      const allEdges=graphRows.edges;
      const edges=allEdges.filter(edge=>{
        const source=graphId(edge?.source),target=graphId(edge?.target);
        return source!==target&&nodeIds.has(source)&&nodeIds.has(target);
      });
      const serial=graphSerial,selectedMode=mode.value,selectedLibrary=library.value,selectedSource=sourceFilter;
      const graphCurrent=()=>valid()&&serial===graphSerial&&selectedMode===mode.value&&selectedLibrary===library.value&&selectedSource===sourceFilter;
      if(searchQuery&&searchHighlightId===null)searchHighlightId=nodes[0]?graphId(nodes[0].id):null;
      list.replaceChildren(n('h2',searchQuery?'Результаты поиска':'Узлы и связи'));
      if(selectedMode==='semantic'&&!data?.similarity?.available)
        list.append(n('p','Слой близости недоступен. '+(data.similarity?.reason?'Причина: '+data.similarity.reason+'.':'Причина не указана.')+' На карте остаются только типизированные связи; сходство не является доказательством.'));
      if(!nodes.length) list.append(n('p',searchQuery?'Совпадений не найдено. Очистите поиск, чтобы вернуть полный список.':'Узлов пока нет.'));
      else {
        const nodeList=window.WisdomList?window.WisdomList.create(host,{empty:'Узлов пока нет.',label:'Страницы списка узлов',library:()=>library.value,stateKey:searchQuery?'':'map-nodes:'+mode.value+':'+(sourceFilter||'all'),renderItem:node=>{
          const button=b((types[node.kind]||node.kind)+' · '+node.label,()=> {
            if(searchQuery) updateSearchMarker(node.id);
            return renderDetail(node);
          });
          if(searchQuery) {
            button.classList.add('map-search-match');
            button.dataset.mapSearchResult=graphId(node.id);
            if(graphId(node.id)===graphId(searchHighlightId)) {
              button.classList.add('map-search-current');
              button.setAttribute('aria-current','true');
              button.prepend(n('strong','Выделенный результат: ',{'data-map-search-marker':'true'}));
            }
          }
          return button;
        }}):null;
        if(nodeList) { nodeList.set(nodes); list.append(nodeList.element,nodeList.navigation,nodeList.status); }
        else for(const node of nodes) list.append(b((types[node.kind]||node.kind)+' · '+node.label,()=>renderDetail(node)));
      }
      const labels=new Map(nodes.map(value=>[graphId(value.id),value.label]));
      for(const edge of edges) {
        const row=n('article',null,{class:'graph-relation'});
        const relationCurrent=()=>graphCurrent()&&row.isConnected;
        const relationText=String(labels.get(graphId(edge.source))||'Узел')+' → '+(predicates[edge.predicate]||edge.predicate||'тип связи не указан')+' → '+String(labels.get(graphId(edge.target))||'Узел');
        const relationButton=b(relationText,()=>renderDetailEdge(edge));
        relationButton.setAttribute('aria-label','Открыть связь: '+relationText);
        const relationHeading=n('h3','');
        relationHeading.append(relationButton);
        const status=(edge.hypothesis?'Гипотеза · ':'')+(publication[edge.publication]||edge.basis||'Типизированная связь');
        row.append(relationHeading,n('p',status));
        const evidence=Array.isArray(edge.evidence)?edge.evidence:[];
        if (!evidence.length) row.append(n('p','Доказательства не указаны: эта связь не подтверждена.',{class:'muted'}));
        if(data.capabilities?.can_review && selectedMode==='semantic' && edge.publication==='draft') row.append(b('Подтвердить связь',()=>review(edge.entity_id,'relation','publish')));
        for(const cite of evidence) row.append(renderCitation(cite,'Вернуться к связи',()=>{
          if(!relationCurrent())return;
          relationButton.focus({preventScroll:true});row.scrollIntoView({block:'nearest'});
        },relationCurrent));
        list.append(row);
      }
      searchInput.disabled=loading||!data;
      clearSearch.disabled=loading||!data||!searchInput.value;
      let text;
      if(loading) text='Загружаю карту для локального поиска…';
      else if(!data) text='Нет загруженной карты для поиска.';
      else if(searchQuery) text=(nodes.length?'Найдено узлов: '+nodes.length+' из '+allNodes.length+'.':'Совпадений не найдено: 0 из '+allNodes.length+' узлов.')+' Показано связанных строк: '+edges.length+' из '+allEdges.length+'.';
      else text='Показаны все узлы: '+allNodes.length+'; типизированных связей: '+edges.length+'.';
      if(data?.truncated)text+=' Поиск ограничен загруженной частью карты.';
      searchStatus.textContent=text;
    };
    const applyLocalSearch=value=> {
      if(loading||!data)return;
      const nextQuery=foldSearch(value);
      if(nextQuery!==searchQuery) { ++readSerial; details.replaceChildren(); }
      searchQuery=nextQuery;
      searchHighlightId=null;
      renderLoadedList();
    };
    const openFirstSearchMatch=()=> {
      if(!searchQuery||loading||!data)return;
      const node=matchingNodes()[0];
      if(!node)return;
      searchHighlightId=graphId(node.id);
      renderLoadedList();
      return renderDetail(node);
    };
    searchInput.addEventListener('compositionstart',()=>{searchComposing=true;});
    searchInput.addEventListener('compositionend',()=>{searchComposing=false;applyLocalSearch(searchInput.value);});
    searchInput.addEventListener('input',event=>{if(!searchComposing&&!event.isComposing)applyLocalSearch(event.currentTarget.value);});
    searchInput.addEventListener('keydown',event=>{
      if(event.key!=='Enter'||searchComposing||event.isComposing||event.keyCode===229)return;
      act(openFirstSearchMatch)(event);
    });
    clearSearch.addEventListener('click',()=> {
      searchComposing=false;searchInput.value='';searchQuery='';searchHighlightId=null;
      ++readSerial;details.replaceChildren();
      if(data&&!loading)renderLoadedList();
      searchInput.focus();
    });
    async function load(options={}) {
      const preserveSimilarityPoll=options.preserveSimilarityPoll===true;
      if(!preserveSimilarityPoll) cancelSimilarityPoll();
      const serial=++graphSerial, selectedMode=mode.value, selectedLibrary=library.value, selectedSource=sourceFilter;
      ++readSerial;
      loading=true; data=null; details.replaceChildren();
      canvas?.destroy();canvas=null;
      surface.replaceChildren(n('p','Загружаю карту…',{class:'map-loading',role:'status'}));
      list.replaceChildren(n('h2','Узлы и связи'),n('p','Узлы и связи загружаются…',{class:'map-loading',role:'status'}));
      searchInput.disabled=true;clearSearch.disabled=true;searchStatus.textContent='Загружаю карту для локального поиска…';
      content.querySelector('.map-error')?.remove();content.querySelector('.map-summary')?.remove();
      manual.hidden=true;relationButton.hidden=true;renderSourceNotice();
      try {
        const fresh=await host.api(endpoint(),{signal:controller.signal});
        if(!valid() || controller.signal.aborted || serial!==graphSerial || selectedMode!==mode.value || selectedLibrary!==library.value || selectedSource!==sourceFilter) return;
        data=filterGraphToSource(fresh,selectedMode,selectedSource);loading=false;
        renderSourceNotice(); content.querySelector('.map-error')?.remove();
        manual.hidden=selectedMode!=='semantic' || !data.capabilities?.can_create || Boolean(selectedSource);
        relationButton.hidden=selectedMode!=='semantic' || !data.capabilities?.can_create;
        renderSimilarityState();
        canvas?.destroy();canvas=null; surface.replaceChildren();list.replaceChildren(n('h2','Узлы и связи'));
        const visible=visibleGraphRows(data);
        const status=n('p',(mode.value==='semantic'?'Понятия и направленные смысловые связи':'Источник → фрагмент → знание или правило → опыт → результат')+
          ' · '+visible.nodes.length+' узлов · '+visible.edges.length+' связей'+(data.truncated?' · Показана часть карты.':''));
      content.querySelector('.map-summary')?.remove();status.className='map-summary'; content.prepend(status);
      if(visible.nodes.length>40) status.append(n('span',' Нажмите на узел или связь либо выберите их в списке, чтобы увидеть подробности.'));
      renderLoadedList();
      if(!visible.nodes.length) {
        const emptyMessage=mode.value==='semantic'?(sourceFilter?'В режиме выбранного источника нет связанных понятий.':'Пока нет понятий и связей. Создайте сеть в Studio или добавьте понятие вручную.'):'После загрузки источников здесь появится их происхождение.';
        surface.append(n('p',emptyMessage));
        if(mode.value==='semantic') surface.append(b('Открыть Studio',()=>window.WisdomResearch.question('',[],library.value)));
      } else {
        // Граф грузится лениво: библиотека нужна только здесь и только когда есть узлы.
        if(typeof window.WisdomGraphCanvas.ensureLibrary==='function') await window.WisdomGraphCanvas.ensureLibrary();
        if(!valid() || serial!==graphSerial) return;
        canvas=window.WisdomGraphCanvas.render(
          surface,{...data,nodes:visible.nodes,edges:visible.edges},mode.value,
          node=>act(()=>renderDetail(node))(),
          {showSimilarity:showSimilarity.checked,onSelectEdge:edge=>act(()=>renderDetailEdge(edge))()}
        );
      }
      } catch(error) {
        if(valid()&&serial===graphSerial) {
          loading=false;data=null;renderSourceNotice();renderSimilarityState();
          searchInput.disabled=true;clearSearch.disabled=true;
          searchStatus.textContent='Локальный поиск недоступен: карта не загружена. Нажмите «Обновить», чтобы повторить запрос.';
          surface.replaceChildren(n('p','Карта не загружена. Нажмите «Обновить», чтобы повторить запрос.'));
          list.replaceChildren(n('h2','Узлы и связи'),n('p','Список карты не загружен. Нажмите «Обновить», чтобы повторить запрос.',{role:'alert'}));
        }
        throw error;
      }
    }
    library.addEventListener('change',act(()=>host.navigateMapSource(library.value,null,mode.value)));
    mode.addEventListener('change',act(()=> {
      const semantic=mode.value==='semantic';
      return host.navigateMapSource(library.value,semantic?sourceFilter:null,mode.value);
    }));
    showSimilarity.addEventListener('change',act(load));
    async function refreshSimilarity() {
      if(mode.value!=='semantic' || !data?.capabilities?.can_create) return;
      const total=Number.isInteger(data.similarity?.total)?data.similarity.total:
        visibleGraphRows(data).nodes.filter(node=>node.kind==='concept'&&node.publication!=='withdrawn').length;
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
      b('Скачать JSON',async()=> { const selectedMode=mode.value,selectedLibrary=library.value,selectedSource=sourceFilter;const fresh=await host.api('/api/graphs/'+selectedMode+'?library_id='+encodeURIComponent(selectedLibrary)+'&limit=200',{signal:controller.signal});if(valid()&&!controller.signal.aborted&&selectedMode===mode.value&&selectedLibrary===library.value&&selectedSource===sourceFilter)host.download('knowledge-graph.json',JSON.stringify(filterGraphToSource(fresh,selectedMode,selectedSource),null,2),'application/json'); }),
      b('Скачать PNG',async()=> { await load(); if(!canvas)throw Error('В карте пока нет узлов.');const blob=await canvas.png({output:'blob-promise',bg:getComputedStyle(surface).backgroundColor,full:true,scale:2});if(valid())host.downloadBlob('knowledge-graph.png',blob); }));
    const manual=b('Добавить понятие',()=> {
      if(sourceFilter)throw Error('Сначала откройте сеть всей библиотеки: ручное понятие без доказательства не входит в фильтр источника.');
      const operation=operationId('concept');
      return host.dialog('Понятие без источника',form=> {
      form.dataset.draftKey=uuid.test(String(libraryId||''))?'concept:'+libraryId+':'+operation:'';
      for(const [name,label] of [['label','Название понятия'],['body','Описание']]) {
        const id='concept-'+name,input=n(name==='body'?'textarea':'input',null,{id,name,required:true,maxlength:name==='body'?2000:500});form.append(n('label',label,{for:id}),input);
      }
      },async (fields,{isCurrent})=> {
        const selectedLibrary=library.value,selectedMode=mode.value;
        if(!isCurrent()||!valid()||selectedLibrary!==library.value||selectedMode!==mode.value)return;
        await host.post('/api/graphs/concepts',{library_id:selectedLibrary,label:fields.get('label'),body:fields.get('body')});
        if(!isCurrent()||!valid()||selectedLibrary!==library.value||selectedMode!==mode.value)return;
        try {
          await load();
        } catch(error) {
          if(!isCurrent()||!valid()||selectedLibrary!==library.value||selectedMode!==mode.value)return;
          const detail=error?.message?': '+error.message:'';
          host.notify('Понятие сохранено, но карта не обновилась'+detail+'. Обновите карту.');
          return;
        }
      });
    });
    controls.append(manual);
    const relationButton=b('Добавить связь',()=> {
      if(mode.value!=='semantic' || visibleGraphRows(data).nodes.length<2) throw Error('Откройте семантическую сеть с двумя понятиями.');
      const operation=operationId('relation');
      host.dialog('Ручная связь · без доказательства',form=> {
        form.dataset.draftKey=uuid.test(String(libraryId||''))?'relation:'+libraryId+':'+operation:'';
        for(const [name,label] of [['from_concept','От понятия'],['to_concept','К понятию']]) {
          const select=n('select',null,{id:name,name}); for(const node of visibleGraphRows(data).nodes)select.append(n('option',node.label,{value:node.entity_id})); form.append(n('label',label,{for:name}),select);
        }
        const select=n('select',null,{id:'predicate',name:'predicate'});
        for(const p of ['supports','part_of','used_for','contradicts'])select.append(n('option',predicates[p],{value:p})); form.append(n('label','Тип связи',{for:'predicate'}),select);
      },async (fields,{isCurrent})=>{
        const selectedLibrary=library.value,selectedMode=mode.value;
        if(!isCurrent()||!valid()||selectedMode!=='semantic'||selectedLibrary!==library.value)return;
        await host.post('/api/graphs/relations',{library_id:selectedLibrary,from_concept:fields.get('from_concept'),to_concept:fields.get('to_concept'),predicate:fields.get('predicate'),hypothesis:true});
        if(!isCurrent()||!valid()||selectedLibrary!==library.value||selectedMode!==mode.value)return;
        try {
          await load();
        } catch(error) {
          if(!isCurrent()||!valid()||selectedLibrary!==library.value||selectedMode!==mode.value)return;
          const detail=error?.message?': '+error.message:'';
          host.notify('Связь сохранена, но карта не обновилась'+detail+'. Обновите карту.');
          return;
        }
      });
    });
    controls.append(relationButton);
    await act(load)();
  }
  const types={source:'Источник',fragment:'Фрагмент',knowledge:'Знание',rule:'Правило',use:'Использование',outcome:'Результат',concept:'Понятие'};
  const publication={draft:'Черновик',published:'Подтверждено',private:'Личное',original:'Оригинал',withdrawn:'Снято с публикации'};
  const predicates={contains:'содержит',grounds:'подтверждает',retrieved:'получено',used:'применено',reports:'зафиксирован результат',supports:'поддерживает',part_of:'часть',used_for:'используется для',contradicts:'противоречит'};
  return {mount,invalidate,captureFocusIntent,restoreFocus};
})();
