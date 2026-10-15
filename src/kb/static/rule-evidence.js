'use strict';
window.WisdomRuleEvidence = {
  mount(form, {api, libraryId, initialIds = []}) {
    const selected = new Set(initialIds), shown = new Set();
    const el = (tag, text) => { const n = document.createElement(tag); if (text != null) n.textContent = text; return n; };
    const box = el('fieldset'), legend = el('legend', 'Подтверждение источниками');
    const source = el('select'), label = el('label', 'Источник для правила');
    source.id = 'rule-source-' + crypto.randomUUID(); label.htmlFor = source.id;
    source.append(new Option('Выберите источник…', ''));
    const hint = el('p'), status = el('p'), fragments = el('div');
    status.setAttribute('role', 'status');
    const more = el('button', 'Показать ещё фрагменты'); more.type = 'button'; more.disabled = true;
    box.append(legend, el('p', 'Если правило основано на материале, отметьте подтверждающие фрагменты.'), label, source, hint, status, fragments, more);
    form.append(box);
    let generation = 0, cursor = 0, busy = false, hasMore = false;
    const showCount = () => { hint.textContent = 'Выбрано фрагментов: ' + selected.size + '. Выбор сохраняется при смене источника.'; };
    const updateButton = () => { more.disabled = busy || !source.value || !hasMore; };
    async function load(reset = false) {
      if (!source.value || (busy && !reset)) return;
      const token = reset ? ++generation : generation, sourceId = source.value;
      if (reset) { cursor = 0; fragments.replaceChildren(); shown.clear(); hasMore = true; }
      busy = true; updateButton(); status.textContent = 'Загружаю фрагменты…';
      try {
        const page = await api('/api/sources/' + encodeURIComponent(sourceId) + '?from_fragment=' + cursor + '&limit=20');
        if (token !== generation || !form.isConnected) return;
        const rows = page.fragments;
        for (const fragment of rows) {
          if (shown.has(fragment.id)) continue;
          const row = el('label'), check = el('input'); check.type = 'checkbox'; check.value = fragment.id;
          check.checked = selected.has(fragment.id);
          check.addEventListener('change', () => { if (check.checked) selected.add(fragment.id); else selected.delete(fragment.id); showCount(); });
          const location = [fragment.file_page != null ? 'Страница ' + fragment.file_page : null, fragment.printed_label ? 'Метка ' + fragment.printed_label : null, fragment.chapter].filter(Boolean).join(' · ');
          row.append(check, el('span', (location ? location + ': ' : '') + String(fragment.text || '').slice(0, 400)));
          row.className = 'completion-review-item'; fragments.append(row); shown.add(fragment.id);
        }
        if (rows.length) cursor = rows.at(-1).ordinal + 1;
        hasMore = rows.length === 20;
        status.textContent = rows.length ? 'Отметьте фрагменты, подтверждающие правило.' : 'Новых фрагментов нет. Если материал ещё не разобран, сначала запустите обработку.';
        more.textContent = 'Показать ещё фрагменты';
      } catch (error) {
        if (token !== generation || !form.isConnected) return;
        status.textContent = error.message; hasMore = true; more.textContent = 'Повторить загрузку';
      } finally {
        if (token === generation) { busy = false; updateButton(); }
      }
    }
    source.addEventListener('change', () => {
      if (source.value) { void load(true); }
      else { generation++; busy = false; hasMore = false; fragments.replaceChildren(); status.textContent = ''; updateButton(); }
    });
    more.addEventListener('click', () => { void load(); });
    showCount(); source.disabled = true;
    async function loadSources() {
      try {
        const sources = await api('/api/libraries/' + encodeURIComponent(libraryId) + '/sources');
        if (!form.isConnected) return;
        for (const item of sources) source.append(new Option(item.title, item.id));
        source.disabled = false;
        status.textContent = sources.length ? '' : 'В библиотеке пока нет источников.';
      } catch (error) {
        if (!form.isConnected) return;
        status.textContent = error.message;
        const retry = el('button', 'Повторить список источников'); retry.type = 'button';
        retry.addEventListener('click', () => { retry.remove(); void loadSources(); }); box.append(retry);
      }
    }
    void loadSources();
    return {selectedIds: () => [...selected]};
  }
};
