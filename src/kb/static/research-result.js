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
    const n = (tag, value, attrs = {}) => actions.node(tag, str(value), attrs);
    const button = (label, fn) => actions.button(str(label), async event => {
      try { await fn(event); } catch (error) { actions.notify(str(error && error.message || 'Не удалось выполнить действие.')); }
    });
    const citationRecords = new Map();
    const ambiguousCitationIds = new Set();
    citations.forEach((meta, index) => {
      if (!meta || typeof meta.id !== 'string' || !meta.id.trim()) return;
      const id = meta.id;
      if (ambiguousCitationIds.has(id)) return;
      if (citationRecords.has(id)) {
        citationRecords.delete(id);
        ambiguousCitationIds.add(id);
      } else {
        citationRecords.set(id, { meta: meta, index: index + 1 });
      }
    });
    const citation = id => {
      if (typeof id !== 'string' || !id.trim() || ambiguousCitationIds.has(id)) return null;
      const record = citationRecords.get(id);
      return record ? record.meta : null;
    };
    const citationControl = id => {
      const meta = citation(id);
      if (!meta) return n('span', 'Цитата недоступна');
      const record = citationRecords.get(id);
      const title = typeof meta.title === 'string' && meta.title.trim()
        ? meta.title.trim() : 'Без названия';
      const identity = `Цитата ${record.index}: ${title}`;
      const control = button(identity, () => actions.citation(id));
      control.setAttribute('aria-label', identity);
      return control;
    };
    const cite = item => {
      const box = n('div', '');
      for (const id of Array.isArray(item.citation_ids) ? item.citation_ids : []) {
        box.append(citationControl(id));
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
      const scroll = n('div', '', {
        class: 'research-table-scroll', tabindex: '0', role: 'region',
        'aria-label': 'Сравнение источников и выводов'
      });
      scroll.style.overflowX = 'auto';
      scroll.style.maxWidth = '100%';
      scroll.style.overscrollBehaviorInline = 'contain';
      const table = n('table', ''), head = n('thead', ''), header = n('tr', '');
      const caption = n('caption', 'Сравнение источников и выводов. Если таблица не помещается, прокрутите эту область по горизонтали.');
      for (const label of ['Критерий', 'Источник', 'Вывод', 'Цитаты']) header.append(n('th', label, { scope: 'col' }));
      head.append(header); table.append(caption, head);
      const body = n('tbody', '');
      for (const item of items) {
        const sourceId = typeof item.source_id === 'string' && item.source_id.trim()
          ? item.source_id : '';
        const meta = sourceId
          ? citations.find(c => c && typeof c.source_id === 'string' && c.source_id === sourceId)
          : null;
        const row = n('tr', '');
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
        const row = n('article', '', { class: 'research-topic-row' }), heading = str(item.heading);
        row.style.setProperty('--topic-depth', String(Math.min(depth(i), 5)));
        const ids = [...new Set((Array.isArray(item.citation_ids) ? item.citation_ids : [])
          .map(citation).filter(Boolean).map(c => c.source_id)
          .filter(value => typeof value === 'string' && value.trim()))];
        row.append(button(heading || 'Понятие', () => actions.question('Что означает ' + heading + '?', ids)), n('p', item.text), cite(item)); rows.append(row);
      }
      root.append(rows);
    } else if (k === 'network' || k === 'semantic_network') {
      const concepts = n('div', '');
      for (const item of items) concepts.append(paragraph(item));
      root.append(concepts);
      const list = n('ul', '');
      for (const rel of Array.isArray(r.relations) ? r.relations : []) {
        if (!rel || !Number.isInteger(rel.from_item) || !Number.isInteger(rel.to_item) || rel.from_item < 0 || rel.from_item >= items.length || rel.to_item < 0 || rel.to_item >= items.length) continue;
        const from = str(items[rel.from_item].heading), to = str(items[rel.to_item].heading), line = `${from} → ${to} — ${({supports:"поддерживает",part_of:"часть",used_for:"используется для",contradicts:"противоречит"})[rel.predicate] || str(rel.predicate)}`;
        const evidenceIds = Array.isArray(rel.citation_ids) ? rel.citation_ids : [];
        const entry = n('li', '');
        entry.append(n('span', line), n('p', rel.hypothesis ? 'Гипотеза' : 'Направленная связь'));
        if (evidenceIds.length) entry.append(cite({ citation_ids: evidenceIds }));
        else entry.append(n('p', 'Доказательства не указаны: эта направленная связь не подтверждена.', { class: 'muted' }));
        list.append(entry);
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
  };
})();
