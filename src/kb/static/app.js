'use strict';
const byId = id => document.getElementById(id);
const node = (tag, text, attrs = {}) => {
  const n = document.createElement(tag);
  if (text !== undefined && text !== null) n.textContent = String(text);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) n.addEventListener(key.slice(2), value);
    else if (key === 'checked') n.checked = value;
    else n.setAttribute(key, value);
  }
  return n;
};
let identity, selected, libraries = [], activeJob;
const message = text => { byId('status').textContent = text; };
const safe = fn => async event => {
  event?.preventDefault();
  try { await fn(event); } catch (error) { message(error.message); }
};
async function api(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (options.method && options.method !== 'GET') {
    const cookie = document.cookie.split('; ').find(v => v.startsWith('kb_csrf='));
    if (cookie) headers.set('X-KB-CSRF', decodeURIComponent(cookie.slice(8)));
  }
  const response = await fetch(path, {...options, headers, credentials: 'same-origin'});
  if (!response.ok) {
    if ([401,403].includes(response.status)) {message(response.status===401?'Войдите в свой аккаунт.':'Доступ к материалу отсутствует или отозван.');window.WisdomResearch?.invalidate();window.WisdomMap?.invalidate();document.querySelectorAll("dialog[data-wisdom-private]").forEach(d=>d.close());}
    if (response.status === 401) {
      const login = document.querySelector('.login'); if (login) login.hidden = false;
      const logout = byId('logout'); if (logout) logout.hidden = true;
    }
    if (response.status === 401) throw Error('Войдите в свой аккаунт через кнопку «Войти».');
    if (response.status === 403) throw Error('Для этого действия недостаточно прав.');
    const data = await response.json().catch(() => ({}));
    throw Error(typeof data.detail === 'string' ? data.detail : 'Запрос не выполнен. Проверьте данные.');
  }
  return response.status === 204 ? null : response.json();
}
const post = (path, data = {}) => api(path, {method: 'POST', body: JSON.stringify(data)});
const button = (text, fn) => node('button', text, {type: 'button', onclick: safe(fn)});
function field(form, title, name, value = '', type = 'text') {
  const id = 'f-' + crypto.randomUUID();
  form.append(node('label', title, {for: id}));
  const input = node(type === 'textarea' ? 'textarea' : 'input', null, {id, name});
  if (type !== 'textarea') input.type = type;
  input.value = value ?? '';
  form.append(input);
  return input;
}
function dialogForm(title, render, submit) {
  const dialog = node('dialog',null,{'data-wisdom-private':'true'}); const form = node('form');
  dialog.append(node('h2', title), form);
  render(form);
  form.append(node('button', 'Сохранить', {type: 'submit'}), button('Отмена', () => dialog.close()));
  form.addEventListener('submit', safe(async () => { await submit(new FormData(form)); dialog.close(); }));
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog); dialog.showModal();
}
async function loadLibraries() {
  const page = await api('/api/libraries'); libraries = page.libraries;
  const list = byId('library-list'); list.replaceChildren(node('h2', 'Мои библиотеки'));
  if (!libraries.length) list.append(node('p', 'База чистая. Создайте библиотеку и загрузите материал.'));
  for (const lib of libraries) list.append(button(lib.name + ' · ' + lib.kind, () => openLibrary(lib)));
}
async function trackJob(id) {
  activeJob = id;
  const job = await api('/api/jobs/' + id);
  message('Обработка: ' + job.status + ' · частей: ' + job.chunks_done + '/' + (job.chunks_total ?? '?') + (job.error_code ? ' · ' + job.error_code : ''));
  if (['done', 'partial', 'failed', 'needs_ocr'].includes(job.status)) {
    activeJob = null;
    await openLibrary(selected);
    message(job.status === 'done' ? 'Материал обработан. Проверьте черновики перед публикацией.' : 'Обработка: ' + job.status + ' · ' + (job.error_code ?? '') + '. Можно продолжить; готовые части сохраняются.');
  } else setTimeout(() => { if (activeJob === id) safe(() => trackJob(id))(); }, 3000);
}
async function processSource(id, extract, idempotencyKey = crypto.randomUUID()) {
  const job = await post('/api/sources/' + id + '/process', {idempotency_key: idempotencyKey, extract});
  await trackJob(job.job_id);
}
async function viewSource(id) {
  const d = node('dialog',null,{'data-wisdom-private':'true'});
  const initial = await api('/api/sources/' + id + '?from_fragment=0&limit=200');
  d.append(node('h2', initial.source.title));
  d.append(node('p', 'Автор: ' + (initial.source.author ?? 'не указан') + ' · Издание: ' + (initial.source.edition ?? 'не указано')));
  d.append(node('a', 'Скачать оригинал', {href: '/api/sources/' + id + '/original'}));
  const content = node('div'), pager = node('p');
  let currentFrom = 0, currentLastOrdinal = initial.fragments.at(-1)?.ordinal, previousFrom = [], pending = false, hasNext = initial.fragments.length === 200;
  const previous = button('Предыдущая часть', async () => {
    if (pending || !previousFrom.length) return;
    await renderPage(previousFrom.at(-1));
  });
  const next = button('Следующая часть', async () => {
    if (pending) return;
    pending = true; next.disabled = true;
    try {
      const nextFrom = typeof currentLastOrdinal === 'number' ? currentLastOrdinal + 1 : currentFrom + 200;
      const page = await api('/api/sources/' + id + '?from_fragment=' + encodeURIComponent(nextFrom) + '&limit=200');
      if (page.fragments.length) {
        const lastOrdinal = page.fragments.at(-1)?.ordinal;
        previousFrom.push(currentFrom);
        currentFrom = nextFrom;
        currentLastOrdinal = typeof lastOrdinal === 'number' ? lastOrdinal : nextFrom + page.fragments.length - 1;
        renderFragments(page.fragments);
      } else {
        hasNext = false; pager.textContent = 'Дальше фрагментов нет.';
      }
    } catch (error) { pager.textContent = error.message; }
    finally { pending = false; next.disabled = !hasNext; previous.disabled = previousFrom.length === 0; }
  });
  async function renderPage(from) {
    pending = true; previous.disabled = true; next.disabled = true; pager.textContent = 'Загружаю фрагменты…';
    try {
      const page = await api('/api/sources/' + id + '?from_fragment=' + encodeURIComponent(from) + '&limit=200');
      if (!page.fragments.length) { pager.textContent = 'Предыдущая часть фрагментов не найдена.'; return; }
      previousFrom.pop(); currentFrom = from;
      const lastOrdinal = page.fragments.at(-1)?.ordinal;
      currentLastOrdinal = typeof lastOrdinal === 'number' ? lastOrdinal : from + page.fragments.length - 1;
      renderFragments(page.fragments);
    } catch (error) { pager.textContent = error.message; }
    finally { pending = false; previous.disabled = previousFrom.length === 0; next.disabled = !hasNext; }
  }
  function renderFragments(fragments) {
    content.replaceChildren();
    hasNext = fragments.length === 200;
    for (const part of fragments) {
      content.append(node('p', [part.chapter, part.file_page != null ? 'Страница файла ' + part.file_page : null, part.printed_label ? 'Печатная метка ' + part.printed_label : null, part.paragraph != null ? 'Абзац ' + part.paragraph : null, part.table_index ? 'Таблица ' + part.table_index + ', ячейка ' + part.cell_row + ':' + part.cell_column : null].filter(Boolean).join(' · ')), node('pre', part.text));
    }
    pager.textContent = fragments.length ? 'Показано фрагментов: ' + fragments.length + (hasNext ? ' · возможно, есть продолжение' : '') : 'Фрагментов в источнике пока нет.';
  }
  d.append(previous, next, pager, content, button('Закрыть', () => d.close())); d.addEventListener('close', () => d.remove());
  document.body.append(d); d.showModal();
  renderFragments(initial.fragments); currentLastOrdinal = initial.fragments.at(-1)?.ordinal; previous.disabled = true; next.disabled = !hasNext;
}
async function openLibrary(lib) {
  selected = lib; const root = byId('library-detail'); root.replaceChildren(node('h2', lib.name));
  const policy = await api('/api/libraries/' + lib.id + '/policy');
  if (selected?.id !== lib.id) return;
  const role = policy.effective_role;
  const curator = ['curator', 'manager'].includes(role);
  const contributor = curator || role === 'contributor';
  const extractionAllowed = Boolean(policy.generation_allowed && policy.external_processing_allowed);
  root.append(node('p', 'Тип: ' + lib.kind + ' · Доступ: ' + (role ?? 'не указан')));
  if (contributor) {
    const form = node('form'); form.append(node('h3', 'Загрузить файл')); const uploadTitle = field(form, 'Название материала', 'title');
    const input = field(form, 'PDF, EPUB, DOCX, HTML или текст', 'file', '', 'file'); input.required = true;
    const uploadAuthor = field(form, 'Автор (если известен)', 'author'); const uploadEdition = field(form, 'Издание (если известно)', 'edition');
    const extract = node('input', null, {type: 'checkbox', id: 'extract'});
    extract.disabled = !extractionAllowed;
    form.append(extract, node('label', 'Извлечь знания и правила через мою подписку MiniMax (для владельца)', {for: 'extract'}));
    if (!extractionAllowed) form.append(node('p', 'Извлечение отключено политикой библиотеки. Менеджер может включить внешнюю обработку и генерацию выше.', {class: 'muted'}));
    form.append(node('button', 'Загрузить и обработать', {type: 'submit'}));
    let pendingUpload = null;
    form.addEventListener('submit', safe(async () => {
      const file = input.files[0];
      if (!file) { message('Выберите файл для загрузки.'); input.focus(); return; }
      const values = {title: uploadTitle.value.trim(), author: uploadAuthor.value.trim(), edition: uploadEdition.value.trim(), extract: extract.checked};
      const same = pendingUpload && pendingUpload.file === file && JSON.stringify(pendingUpload.values) === JSON.stringify(values);
      if (!same) pendingUpload = {file, values, uploadKey: crypto.randomUUID(), processKey: crypto.randomUUID(), sourceId: null};
      const operation = pendingUpload;
      if (!operation.sourceId) {
        const data = new FormData(); data.set('file', file); data.set('idempotency_key', operation.uploadKey);
        if (values.title) data.set('title', values.title);
        message('Сохраняю оригинал…');
        const saved = await api('/api/libraries/' + lib.id + '/sources/upload', {method: 'POST', body: data});
        operation.sourceId = saved.source.id;
      }
      try {
        await api('/api/sources/' + operation.sourceId + '/metadata', {method: 'PATCH', body: JSON.stringify({author: values.author || null, edition: values.edition || null, publication_ref: null})});
      } catch (error) {
        message('Файл сохранён как источник ' + operation.sourceId + ', но метаданные не обновились: ' + error.message + '. Повторите с теми же полями.');
        return;
      }
      try {
        await processSource(operation.sourceId, values.extract, operation.processKey);
        pendingUpload = null;
      } catch (error) {
        message('Источник ' + operation.sourceId + ' сохранён, но обработку подтвердить не удалось: ' + error.message + '. Повторите отправку с теми же полями.');
      }
    })); root.append(form);
    const urlForm = node('form'); urlForm.append(node('h3', 'Добавить статью по URL'));
    const urlInput = field(urlForm, 'URL статьи (http или https)', 'url', '', 'url');
    urlInput.required = true; urlInput.maxLength = 2048; urlInput.autocomplete = 'url';
    const titleInput = field(urlForm, 'Название материала (необязательно)', 'title'); titleInput.maxLength = 300;
    const extractUrl = node('input', null, {type: 'checkbox', id: 'extract-url'});
    extractUrl.disabled = !extractionAllowed;
    urlForm.append(extractUrl, node('label', 'Извлечь знания и правила через мою подписку MiniMax (для владельца)', {for: 'extract-url'}));
    if (!extractionAllowed) urlForm.append(node('p', 'Извлечение отключено политикой библиотеки. Менеджер может включить внешнюю обработку и генерацию выше.', {class: 'muted'}));
    const urlSubmit = node('button', 'Добавить и обработать', {type: 'submit'});
    urlForm.append(urlSubmit);
    let pendingUrlOperation = null;
    const submitUrl = async () => {
      const url = urlInput.value.trim();
      const title = titleInput.value.trim();
      const extract = extractUrl.checked;
      const unchanged = pendingUrlOperation && pendingUrlOperation.url === url && pendingUrlOperation.title === title && pendingUrlOperation.extract === extract;
      if (!url || url.length > 2048) { pendingUrlOperation = null; message('Укажите URL статьи длиной до 2048 символов.'); urlInput.focus(); return; }
      if (title.length > 300) { pendingUrlOperation = null; message('Название должно содержать не более 300 символов.'); titleInput.focus(); return; }
      try {
        const parsed = new URL(url);
        if (!['http:', 'https:'].includes(parsed.protocol)) throw Error();
      } catch (_) { pendingUrlOperation = null; message('Введите полный URL статьи, начинающийся с http:// или https://.'); urlInput.focus(); return; }
      if (!unchanged) pendingUrlOperation = {url, title, key: crypto.randomUUID(), extract};
      urlSubmit.disabled = true;
      message('Сохраняю URL статьи…');
      try {
        const body = {url: pendingUrlOperation.url, idempotency_key: pendingUrlOperation.key};
        if (pendingUrlOperation.title) body.title = pendingUrlOperation.title;
        const saved = await post('/api/libraries/' + lib.id + '/sources/from-url', body);
        const operation = pendingUrlOperation;
        pendingUrlOperation = null;
        urlForm.reset();
        message('Статья сохранена как источник ' + saved.source.id + '. Запускаю обработку…');
        try {
          await processSource(saved.source.id, operation.extract);
        } catch (error) {
          message('Источник ' + saved.source.id + ' сохранён, но статус обработки получить не удалось: ' + error.message + '. Проверьте обработку в списке источников.');
        }
      } catch (error) {
        message(error.message + (pendingUrlOperation ? ' При неопределённом результате повторите отправку этой же статьи: ключ операции сохранён.' : ''));
      } finally { urlSubmit.disabled = false; }
    };
    urlForm.addEventListener('submit', safe(submitUrl)); root.append(urlForm);
    root.append(button('Создать правило вручную', () => ruleEditor(lib)));
  }
  root.append(node('h3', 'Источники и происхождение'));
  const sources = await api('/api/libraries/' + lib.id + '/sources');
  if (selected?.id !== lib.id) return;
  for (const source of sources) {
    const card = node('article', null, {class: 'card'});
    card.append(button(source.title, () => viewSource(source.id)), node('p', 'Статус: ' + source.processing));
    if (contributor) {
      card.append(button('Разобрать текст', () => processSource(source.id, false)));
      const extractAction = button('Извлечь / продолжить черновики', () => processSource(source.id, true));
      extractAction.disabled = !extractionAllowed;
      if (!extractionAllowed) { extractAction.title = 'Отключено политикой библиотеки'; card.append(extractAction, node('p', 'Извлечение отключено политикой библиотеки.', {class: 'muted'})); }
      else card.append(extractAction);
    } root.append(card);
  }
  root.append(node('h3', 'Знания'));
  const knowledge = await api('/api/libraries/' + lib.id + '/knowledge');
  if (selected?.id !== lib.id) return;
  for (const item of knowledge) {
    const card = node('article', null, {class: 'card'});
    card.append(node('p', item.kind + ' · ' + item.publication), node('p', item.statement));
    if (item.publication === 'withdrawn') card.append(node('p', 'Знание отозвано.'));
    if (curator && ['draft', 'needs_review'].includes(item.publication)) {
      const publish = button('Подтвердить знание', async () => { await post('/api/knowledge/' + item.id + '/publish'); await openLibrary(lib); });
      const reject = button('Отклонить черновик', async event => {
        const action = event.currentTarget; action.disabled = true;
        try { await post('/api/knowledge/' + item.id + '/reject'); await openLibrary(lib); }
        catch (error) { message(error.message); }
        finally { action.disabled = false; }
      });
      card.append(publish, reject);
    }
    root.append(card);
  }
  root.append(node('h3', 'Правила и навыки'));
  const rules = await api('/api/libraries/' + lib.id + '/rules');
  if (selected?.id !== lib.id) return;
  for (const rule of rules) {
    const card = node('article', null, {class: 'card'}); card.append(node('h4', rule.title + ' · v' + rule.version_no + ' · ' + rule.publication));
    for (const [title, value] of [['Когда', rule.when_to_apply], ['Входные данные', rule.inputs?.join('\n')], ['Шаги', rule.actions?.join('\n')], ['Исключения', rule.exceptions?.join('\n')], ['Ожидаемый эффект', rule.expected_effect]]) card.append(node('strong', title), node('p', value ?? 'не указано'));
    card.append(node('details', null)); const detail = card.lastChild; detail.append(node('summary', 'Происхождение'), node('pre', JSON.stringify(rule.provenance, null, 2)));
    if (rule.publication === 'withdrawn') card.append(node('p', 'Правило отозвано.'));
    if (curator && ['draft', 'needs_review'].includes(rule.publication)) {
      const publish = button('Опубликовать', async () => { await post('/api/rules/' + rule.id + '/publish'); await openLibrary(lib); });
      const reject = button('Отклонить черновик', async event => {
        const action = event.currentTarget; action.disabled = true;
        try { await post('/api/rules/' + rule.id + '/reject'); await openLibrary(lib); }
        catch (error) { message(error.message); }
        finally { action.disabled = false; }
      });
      card.append(publish, reject);
    }
    if (contributor) card.append(button('Новая версия', () => ruleEditor(lib, rule)));
    if (rule.publication === 'published') {
      card.append(button('Я применил это правило', () => {
        let pendingUse = null;
        return dialogForm('Зафиксировать применение', form => {
        field(form, 'Где применили', 'where_applied'); field(form, 'Какой результат ожидали', 'expected_result', '', 'textarea');
      }, async data => {
        const payload = {rule_id: rule.id, version_no: rule.version_no, where_applied: data.get('where_applied') || null, expected_result: data.get('expected_result') || null};
        if (!pendingUse || JSON.stringify(pendingUse.payload) !== JSON.stringify(payload)) pendingUse = {payload, key: crypto.randomUUID()};
        const use = await post('/api/uses', {...pendingUse.payload, idempotency_key: pendingUse.key});
        await post('/api/uses/' + use.id + '/applied'); message('Применение записано. Результат добавьте в разделе «Опыт».');
      });
      }));
      if (policy.export_allowed) card.append(button('Экспорт SKILL.md', async () => {
        const result = await post('/api/libraries/' + lib.id + '/export', {rule_ids: [rule.id]});
        const url = URL.createObjectURL(new Blob([result.content], {type: 'text/markdown;charset=utf-8'}));
        const a = node('a', null, {href: url, download: 'SKILL.md'}); a.click(); setTimeout(() => URL.revokeObjectURL(url), 5000);
      }));
      else card.append(node('p', 'Экспорт отключён политикой библиотеки.', {class: 'muted'}));
    } root.append(card);
  }
  if (lib.kind === 'project' && curator) {
    const form = node('form'); const select = node('select', null, {name: 'library_id', 'aria-label': 'Библиотека для проекта'});
    for (const other of libraries.filter(v => v.id !== lib.id)) select.append(node('option', other.name, {value: other.id}));
    form.append(select, node('button', 'Связать с проектом', {type: 'submit'}));
    form.addEventListener('submit', safe(async () => { await post('/api/projects/' + lib.id + '/libraries', {library_id: select.value, is_required: true}); message('Связь сохранена. Права на связанную библиотеку назначаются отдельно.'); })); root.append(form);
  }
  await window.WisdomCompletion.enhanceLibrary(root, lib, featureHost);
}
function ruleEditor(lib, rule) {
  let evidence;
  dialogForm(rule ? 'Новая версия правила' : 'Новое правило', form => {
    for (const [title, key, type] of [['Название', 'title', 'text'], ['Когда применять', 'when_to_apply', 'textarea'], ['Входные данные — по одному на строку', 'inputs', 'textarea'], ['Условия', 'preconditions', 'textarea'], ['Шаги — по одному на строку', 'actions', 'textarea'], ['Исключения', 'exceptions', 'textarea'], ['Ожидаемый эффект', 'expected_effect', 'textarea'], ['Как проверить', 'verification', 'textarea']]) field(form, title, key, Array.isArray(rule?.[key]) ? rule[key].join('\n') : rule?.[key], type);
    evidence = window.WisdomRuleEvidence.mount(form, {api, libraryId: lib.id, initialIds: (rule?.provenance ?? []).map(p => p.fragment_id).filter(Boolean)});
  }, async data => {
    const body = Object.fromEntries(data); for (const key of ['inputs', 'preconditions', 'actions', 'exceptions']) body[key] = body[key].split('\n').filter(v => v.trim());
    for (const key of ['when_to_apply', 'expected_effect', 'verification']) body[key] ||= null;
    body.fragment_ids = evidence.selectedIds();
    await post('/api/libraries/' + lib.id + '/rules' + (rule ? '?replaces=' + rule.id : ''), body); await openLibrary(lib);
  });
}
async function loadUses() {
  const list = byId('uses-list'); list.replaceChildren();
  const records = await api('/api/uses'); if (!records.length) list.append(node('p', 'Применения пока не записаны. Просмотр и поиск не создают опыт.'));
  for (const use of records) {
    const card = node('article', null, {class: 'card'}); card.append(node('h2', use.title + ' · v' + use.rule_version_no), node('p', 'Состояние: ' + use.state), node('p', 'Ожидали: ' + (use.expected_result ?? 'не указано')), node('p', 'Где: ' + (use.where_applied ?? 'не указано')));
    for (const result of use.outcomes) card.append(node('p', (result.is_correction ? 'Исправление: ' : 'Результат: ') + (result.observed_result ?? 'не указан') + ' · Оценка: ' + (result.rating ?? 'не задана') + ' · Основание: ' + (result.rating_basis ?? 'не указано')));
    if (use.state === 'retrieved') card.append(button('Подтвердить применение', async () => { await post('/api/uses/' + use.id + '/applied'); await loadUses(); }));
    else card.append(button(use.outcomes.length ? 'Добавить исправление' : 'Записать результат', () => {
      let pendingOutcome = null;
      return dialogForm('Результат применения', form => {
      field(form, 'Что получилось', 'observed_result', '', 'textarea'); const rating = field(form, 'Полезность от 1 до 5 (можно не указывать)', 'rating', '', 'number'); rating.min = 1; rating.max = 5;
      field(form, 'Почему такая оценка', 'rating_basis', '', 'textarea');
    }, async data => {
      const payload = {observed_result: data.get('observed_result') || null, rating: data.get('rating') ? Number(data.get('rating')) : null, rating_basis: data.get('rating_basis') || null, is_correction: use.outcomes.length > 0};
      if (!pendingOutcome || JSON.stringify(pendingOutcome.payload) !== JSON.stringify(payload)) pendingOutcome = {payload, key: crypto.randomUUID()};
      await post('/api/uses/' + use.id + '/outcomes', {...pendingOutcome.payload, idempotency_key: pendingOutcome.key}); await loadUses();
    });
    }));
    list.append(card);
  }
  await window.WisdomCompletion.enhanceUses(list, featureHost);
}
byId('create-library').addEventListener('submit', safe(async event => {
  const data = Object.fromEntries(new FormData(event.target)); data.organisation_id = identity.account_id;
  const project = data.kind === 'project'; if (project) delete data.kind;
  const result = await post(project ? '/api/projects' : '/api/libraries', data);
  event.target.reset(); await loadLibraries(); await openLibrary(project ? result.project : result);
}));
byId('search').addEventListener('submit', safe(async event => {
  const query = new FormData(event.target).get('q'); const result = await api('/api/search?q=' + encodeURIComponent(query));
  const list = byId('search-results'); list.replaceChildren(node('p', ({semantic:'Поиск по смыслу',hybrid:'Поиск по смыслу и словам',lexical:'Поиск по словам'}[result.mode] || result.mode) + ' · ' + result.hits.length + ' совпадений'));
  if (result.indexed_library_count != null || result.pending_library_count != null) {
    const counts = [];
    if (result.indexed_library_count != null) counts.push('индекс готов — ' + result.indexed_library_count);
    if (result.pending_library_count != null) counts.push('индекс не готов — ' + result.pending_library_count);
    list.append(node('p', 'Библиотеки: ' + counts.join(' · ') + '.'));
  }
  if (result.fallback_reason === 'index_pending_or_unavailable') {
    list.append(node('p', 'Пока ищем по словам: индекс библиотеки ещё не готов или временно недоступен.'));
  }
  for (const hit of result.hits) {
    list.append(node('p', hit.statement));
    const citations = Array.isArray(hit.provenance) ? hit.provenance : [];
    if (!citations.length) list.append(node('p', 'Источник и место в тексте не указаны.'));
    for (const provenance of citations) {
    const source = provenance.source && typeof provenance.source === 'object' ? provenance.source : provenance;
    const locator = provenance.locator && typeof provenance.locator === 'object' ? provenance.locator : {};
    const label = value => typeof value === 'string' || typeof value === 'number' ? String(value) : null;
    const citation = [
      label(source.title),
      label(source.author) ? 'Автор: ' + label(source.author) : null,
      label(source.publication_ref),
      label(locator.chapter),
      label(locator.file_page) !== null ? 'Страница файла ' + label(locator.file_page) : null,
      label(locator.printed_label) ? 'Печатная метка ' + label(locator.printed_label) : null,
      label(locator.paragraph) !== null ? 'Абзац ' + label(locator.paragraph) : null
    ].filter(Boolean);
    const details = node('p', citation.length ? citation.join(' · ') : 'Источник и место в тексте не указаны.');
    const sourceId = provenance.source_id;
    if (typeof sourceId === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(sourceId)) {
      details.append(document.createTextNode(' · '), button('Открыть источник', () => viewSource(sourceId)));
    }
    if (typeof source.url === 'string') {
      try {
        const url = new URL(source.url);
        if (['http:', 'https:'].includes(url.protocol)) details.append(document.createTextNode(' · '), node('a', 'Ссылка', {href: url.href}));
      } catch {}
    }
    list.append(details);
    }
  }
}));
byId('logout').addEventListener('click', safe(async () => { await post('/auth/logout'); location.reload(); }));
function downloadBlob(filename,blob) {
  const url=URL.createObjectURL(blob),a=node('a',null,{href:url,download:filename});
  document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
const featureHost={node,button,api,post,notify:message,libraries:()=>libraries,currentLibrary:()=>selected,identity:()=>identity,
  refreshLibrary:()=>selected?openLibrary(selected):Promise.resolve(),
  openSource:viewSource,download:(filename,text,type)=>downloadBlob(filename,new Blob([text],{type})),
  downloadBlob,dialog:dialogForm,navigateResearch:()=>switchTab('research'),
  openRuleVersion:async(id,version)=> {
    const rule=await api('/api/rules/'+id);const d=node('dialog',null,{'data-wisdom-private':'true'});
    d.append(node('h2',rule.title+' · Версия '+(version||rule.version_no)),node('p',rule.when_to_apply),node('p',rule.expected_effect));
    for(const a of rule.actions||[])d.append(node('p',a.body||a));
    d.append(button('Закрыть',()=>d.close()));d.addEventListener('close',()=>d.remove());document.body.append(d);d.showModal();
  }
};
window.WisdomResearch.configure(featureHost);
async function switchTab(name) {
  window.WisdomResearch.invalidate();window.WisdomMap.invalidate();
  for(const section of ['libraries','uses','research','maps','access','jobs'])byId(section+'-tab').hidden=section!==name;
  for(const item of document.querySelectorAll('[data-tab]'))item.setAttribute('aria-pressed',String(item.dataset.tab===name));
  if(name==='uses')await loadUses();
  if(name==='research')await window.WisdomResearch.mount(byId('research-workspace'),featureHost);
  if(name==='maps')await window.WisdomMap.mount(byId('maps-workspace'),featureHost);
  if(name==='access')await window.WisdomCompletion.mountAccess(byId('access-workspace'),featureHost);
  if(name==='jobs')await window.WisdomCompletion.mountJobs(byId('jobs-workspace'),featureHost);
}
for (const tab of document.querySelectorAll('[data-tab]')) tab.addEventListener('click',safe(()=>switchTab(tab.dataset.tab)));
safe(async () => {
  identity = await api('/api/me');
  const login = document.querySelector('.login'); if (login) login.hidden = true;
  const logout = byId('logout'); if (logout) logout.hidden = false;
  const readiness = await api('/readyz');
  message(readiness.semantic_search_enabled ? 'Поиск по смыслу доступен для опубликованных знаний. Извлечение создаёт черновики для проверки.' : 'Поиск по словам · извлечение создаёт черновики для проверки.');
  await loadLibraries();
})();
