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
  const dialog = node('dialog'); const form = node('form');
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
async function processSource(id, extract) {
  const job = await post('/api/sources/' + id + '/process', {idempotency_key: crypto.randomUUID(), extract});
  await trackJob(job.job_id);
}
async function viewSource(id) {
  const result = await api('/api/sources/' + id + '?limit=200');
  const d = node('dialog'); d.append(node('h2', result.source.title));
  d.append(node('p', 'Автор: ' + (result.source.author ?? 'не указан') + ' · Издание: ' + (result.source.edition ?? 'не указано')));
  d.append(node('a', 'Скачать оригинал', {href: '/api/sources/' + id + '/original'}));
  for (const part of result.fragments) {
    d.append(node('p', [part.chapter, part.file_page ? 'Страница файла ' + part.file_page : null, part.printed_label ? 'Печатная метка ' + part.printed_label : null, part.paragraph ? 'Абзац ' + part.paragraph : null, part.table_index ? 'Таблица ' + part.table_index + ', ячейка ' + part.cell_row + ':' + part.cell_column : null].filter(Boolean).join(' · ')), node('pre', part.text));
  }
  d.append(button('Закрыть', () => d.close())); d.addEventListener('close', () => d.remove());
  document.body.append(d); d.showModal();
}
async function openLibrary(lib) {
  selected = lib; const root = byId('library-detail'); root.replaceChildren(node('h2', lib.name));
  const grants = await api('/api/libraries/' + lib.id + '/grants');
  const mine = grants.find(g => g.principal_id === identity.principal_id);
  const curator = ['curator', 'manager'].includes(mine?.role);
  const contributor = curator || mine?.role === 'contributor';
  root.append(node('p', 'Тип: ' + lib.kind + ' · Доступ: ' + (mine?.role ?? 'через группу')));
  if (contributor) {
    const form = node('form'); form.append(node('h3', 'Загрузить файл')); field(form, 'Название материала', 'title');
    const input = field(form, 'PDF, EPUB, DOCX, HTML или текст', 'file', '', 'file'); input.required = true;
    field(form, 'Автор (если известен)', 'author'); field(form, 'Издание (если известно)', 'edition');
    const extract = node('input', null, {type: 'checkbox', id: 'extract'});
    form.append(extract, node('label', 'Извлечь знания и правила через мою подписку MiniMax (для владельца)', {for: 'extract'}), node('button', 'Загрузить и обработать', {type: 'submit'}));
    form.addEventListener('submit', safe(async () => {
      const data = new FormData(); data.set('file', input.files[0]); data.set('idempotency_key', crypto.randomUUID());
      const values = new FormData(form); if (values.get('title')) data.set('title', values.get('title'));
      message('Сохраняю оригинал…'); const saved = await api('/api/libraries/' + lib.id + '/sources/upload', {method: 'POST', body: data});
      await api('/api/sources/' + saved.source.id + '/metadata', {method: 'PATCH', body: JSON.stringify({author: values.get('author') || null, edition: values.get('edition') || null, publication_ref: null})});
      await processSource(saved.source.id, extract.checked);
    })); root.append(form);
    const urlForm = node('form'); urlForm.append(node('h3', 'Добавить статью по URL'));
    const urlInput = field(urlForm, 'URL статьи (http или https)', 'url', '', 'url');
    urlInput.required = true; urlInput.maxLength = 2048; urlInput.autocomplete = 'url';
    const titleInput = field(urlForm, 'Название материала (необязательно)', 'title'); titleInput.maxLength = 300;
    const extractUrl = node('input', null, {type: 'checkbox', id: 'extract-url'});
    urlForm.append(extractUrl, node('label', 'Извлечь знания и правила через мою подписку MiniMax (для владельца)', {for: 'extract-url'}));
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
  for (const source of sources) {
    const card = node('article', null, {class: 'card'});
    card.append(button(source.title, () => viewSource(source.id)), node('p', 'Статус: ' + source.processing));
    if (contributor) {
      card.append(button('Разобрать текст', () => processSource(source.id, false)));
      card.append(button('Извлечь / продолжить черновики', () => processSource(source.id, true)));
    } root.append(card);
  }
  root.append(node('h3', 'Знания'));
  for (const item of await api('/api/libraries/' + lib.id + '/knowledge')) {
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
  for (const rule of await api('/api/libraries/' + lib.id + '/rules')) {
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
      card.append(button('Я применил это правило', () => dialogForm('Зафиксировать применение', form => {
        field(form, 'Где применили', 'where_applied'); field(form, 'Какой результат ожидали', 'expected_result', '', 'textarea');
      }, async data => {
        const use = await post('/api/uses', {rule_id: rule.id, version_no: rule.version_no, where_applied: data.get('where_applied') || null, expected_result: data.get('expected_result') || null});
        await post('/api/uses/' + use.id + '/applied'); message('Применение записано. Результат добавьте в разделе «Опыт».');
      })));
      card.append(button('Экспорт SKILL.md', async () => {
        const result = await post('/api/libraries/' + lib.id + '/export', {rule_ids: [rule.id]});
        const url = URL.createObjectURL(new Blob([result.content], {type: 'text/markdown;charset=utf-8'}));
        const a = node('a', null, {href: url, download: 'SKILL.md'}); a.click(); setTimeout(() => URL.revokeObjectURL(url), 5000);
      }));
    } root.append(card);
  }
  if (lib.kind === 'project' && curator) {
    const form = node('form'); const select = node('select', null, {name: 'library_id', 'aria-label': 'Библиотека для проекта'});
    for (const other of libraries.filter(v => v.id !== lib.id)) select.append(node('option', other.name, {value: other.id}));
    form.append(select, node('button', 'Связать с проектом', {type: 'submit'}));
    form.addEventListener('submit', safe(async () => { await post('/api/projects/' + lib.id + '/libraries', {library_id: select.value, is_required: true}); message('Связь сохранена. Права на связанную библиотеку назначаются отдельно.'); })); root.append(form);
  }
}
function ruleEditor(lib, rule) {
  dialogForm(rule ? 'Новая версия правила' : 'Новое правило', form => {
    for (const [title, key, type] of [['Название', 'title', 'text'], ['Когда применять', 'when_to_apply', 'textarea'], ['Входные данные — по одному на строку', 'inputs', 'textarea'], ['Условия', 'preconditions', 'textarea'], ['Шаги — по одному на строку', 'actions', 'textarea'], ['Исключения', 'exceptions', 'textarea'], ['Ожидаемый эффект', 'expected_effect', 'textarea'], ['Как проверить', 'verification', 'textarea']]) field(form, title, key, Array.isArray(rule?.[key]) ? rule[key].join('\n') : rule?.[key], type);
  }, async data => {
    const body = Object.fromEntries(data); for (const key of ['inputs', 'preconditions', 'actions', 'exceptions']) body[key] = body[key].split('\n').filter(v => v.trim());
    for (const key of ['when_to_apply', 'expected_effect', 'verification']) body[key] ||= null;
    body.fragment_ids = (rule?.provenance ?? []).map(p => p.fragment_id).filter(Boolean);
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
    else card.append(button(use.outcomes.length ? 'Добавить исправление' : 'Записать результат', () => dialogForm('Результат применения', form => {
      field(form, 'Что получилось', 'observed_result', '', 'textarea'); const rating = field(form, 'Полезность от 1 до 5 (можно не указывать)', 'rating', '', 'number'); rating.min = 1; rating.max = 5;
      field(form, 'Почему такая оценка', 'rating_basis', '', 'textarea');
    }, async data => { await post('/api/uses/' + use.id + '/outcomes', {observed_result: data.get('observed_result') || null, rating: data.get('rating') ? Number(data.get('rating')) : null, rating_basis: data.get('rating_basis') || null, is_correction: use.outcomes.length > 0}); await loadUses(); })));
    list.append(card);
  }
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
  for (const hit of result.hits) list.append(node('p', hit.statement), node('pre', JSON.stringify(hit.provenance, null, 2)));
}));
byId('logout').addEventListener('click', safe(async () => { await post('/auth/logout'); location.reload(); }));
for (const tab of document.querySelectorAll('[data-tab]')) tab.addEventListener('click', safe(async () => {
  for (const name of ['libraries', 'uses']) byId(name + '-tab').hidden = name !== tab.dataset.tab;
  for (const item of document.querySelectorAll('[data-tab]')) item.setAttribute('aria-pressed', String(item === tab));
  if (tab.dataset.tab === 'uses') await loadUses();
}));
safe(async () => {
  identity = await api('/api/me'); document.querySelector('.login').hidden = true;
  const readiness = await api('/readyz');
  message(readiness.semantic_search_enabled ? 'Поиск по смыслу доступен для опубликованных знаний. Извлечение создаёт черновики для проверки.' : 'Поиск по словам · извлечение создаёт черновики для проверки.');
  await loadLibraries();
})();
