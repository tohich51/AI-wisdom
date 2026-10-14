(() => {
  'use strict';
  const W = window.WisdomResult || (window.WisdomResult = {});
  const str = value => value == null ? '' : String(value);
  W.render = function (container, result, actions) {
    const r = result || {};
    const k = str(r.kind).toLowerCase();
    const items = Array.isArray(r.items) ? r.items : [];
    const citations = Array.isArray(r.citations) ? r.citations : [];
    const root = actions.node('section', '');
    container.replaceChildren(root);
    const n = (tag, value) => actions.node(tag, str(value));
    const button = (label, fn) => actions.button(str(label), async event => {
      try { await fn(event); } catch (error) { actions.notify(str(error && error.message || 'Не удалось выполнить действие.')); }
    });
    const citation = id => citations.find(c => str(c.id) === str(id));
    const cite = item => {
      const box = n('div', '');
      for (const id of Array.isArray(item.citation_ids) ? item.citation_ids : []) {
        const meta = citation(id);
        box.append(button((meta && meta.title) || 'Цитата', () => actions.citation(id)));
      }
      return box;
    };
    const paragraph = item => {
      const block = n('article', '');
      if (str(item.heading)) block.append(n('h3', item.heading));
      if (str(item.text)) block.append(n('p', item.text));
      block.append(cite(item)); return block;
    };
    root.append(n('h2', r.title || 'Результат исследования'));
    root.append(n('p', 'Черновик: проверьте выводы по первоисточникам.'));
    if (r.context_limited) root.append(n('p', 'Контекст ограничен: выводы могут не охватывать все доступные сведения.'));
    if (r.no_evidence || k === 'no_evidence' || k === 'no-evidence' || k === 'noevidence') {
      root.append(n('p', 'В выбранных фрагментах недостаточно оснований для ответа. Уточните вопрос или выберите другие источники.')); return;
    }
    if (!items.length) { root.append(n('p', 'Нет данных для отображения.')); return; }
    if (k === 'comparison') {
      const scroll = n('div', ''); scroll.style.overflowX = 'auto';
      const table = n('table', ''), head = n('thead', ''), header = n('tr', '');
      for (const label of ['Критерий', 'Источник', 'Вывод', 'Цитаты']) header.append(n('th', label));
      head.append(header); table.append(head);
      const body = n('tbody', '');
      for (const item of items) {
        const row = n('tr', ''), meta = citations.find(c => str(c.source_id) === str(item.source_id));
        const refs=n('td','');refs.append(cite(item));
        row.append(n('td', item.heading), n('td', (meta && meta.title) || "Источник"), n('td', item.text), refs); body.append(row);
      }
      table.append(body); scroll.append(table); root.append(scroll);
    } else if (k === 'flashcards' || k === 'flashcard') {
      let at = 0, flipped = false;
      const face = n('article', ''), activeCitations = n('div', ''), controls = n('div', '');
      const previous = button('Назад', async () => { if (at) { at--; flipped = false; paint(); } });
      const next = button('Далее', async () => { if (at + 1 < items.length) { at++; flipped = false; paint(); } });
      const flip = button('Показать ответ', async () => { flipped = !flipped; paint(); });
      const position = n('p', '');
      function paint() {
        const item = items[at];
        face.replaceChildren(flipped ? n('p', str(item.text) || 'Ответ отсутствует') : n('h3', str(item.heading) || 'Вопрос отсутствует'));
        activeCitations.replaceChildren(cite(item)); position.textContent = `Карточка ${at + 1} из ${items.length}`;
        previous.disabled = at === 0; next.disabled = at === items.length - 1; flip.textContent = flipped ? 'Показать вопрос' : 'Показать ответ';
      }
      controls.append(previous, flip, next); root.append(position, face, activeCitations, controls); paint();
    } else if (k === 'topic' || k === 'topics' || k === 'topic_hierarchy') {
      const depth = i => {
        const seen = new Set([i]); let p = items[i].parent_index, d = 0;
        while (Number.isInteger(p) && p >= 0 && p < items.length) {
          if (seen.has(p)) return 0; seen.add(p); d++; p = items[p].parent_index;
        }
        return d;
      };
      const rows = n('div', '');
      for (const [i, item] of items.entries()) {
        const row = n('article', ''), heading = str(item.heading);
        row.style.marginLeft = `${Math.min(depth(i), 5) * 0.75}rem`; row.style.borderLeft = '2px solid #888'; row.style.paddingLeft = '.5rem';
        const ids = [...new Set((Array.isArray(item.citation_ids) ? item.citation_ids : []).map(citation).filter(Boolean).map(c => c.source_id).filter(v => v != null))];
        row.append(button(heading || 'Понятие', () => actions.question('Что означает ' + heading + '?', ids)), n('p', item.text), cite(item)); rows.append(row);
      }
      root.append(rows);
    } else if (k === 'network' || k === 'semantic_network') {
      const concepts = n('div', '');
      for (const item of items) concepts.append(paragraph(item));
      root.append(concepts);
      const list = n('ul', '');
      for (const rel of Array.isArray(r.relations) ? r.relations : []) {
        if (!Number.isInteger(rel.from_item) || !Number.isInteger(rel.to_item) || rel.from_item < 0 || rel.from_item >= items.length || rel.to_item < 0 || rel.to_item >= items.length) continue;
        const from = str(items[rel.from_item].heading), to = str(items[rel.to_item].heading), line = `${from} → ${to} — ${({supports:"поддерживает",part_of:"часть",used_for:"используется для",contradicts:"противоречит"})[rel.predicate] || str(rel.predicate)}`;
        list.append(n('li', rel.hypothesis ? 'Гипотеза: ' + line : line));
      }
      root.append(n('h3', 'Связи'), list);
    } else if (k === 'faq' || k === 'faqs') {
      const list = n('div', ''); for (const item of items) list.append(paragraph(item)); root.append(list);
    } else if (k === 'checklist' || k === 'check_list') {
      const list = n('ul', '');
      for (const item of items) { const li = n('li', ''), body = n('div', ''); body.append(n('span', '• '), n('strong', item.heading)); if (str(item.text)) body.append(n('br'), n('span', item.text)); li.append(body, cite(item)); list.append(li); }
      root.append(list);
    } else {
      const list = n('div', ''); for (const item of items) list.append(paragraph(item)); root.append(list);
    }
  };})();
