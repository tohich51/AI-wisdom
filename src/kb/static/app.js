'use strict';
/*
 * Ядро интерфейса. Все экраны сохранены: библиотеки, опыт, исследование и
 * Studio, карта знаний, доступ, задания. Переход между ними стал маршрутом
 * (window.WisdomRouter), чтение данных идёт через один загрузчик
 * (window.WisdomData), длинные списки — постранично (window.WisdomList).
 */
const {api, post} = window.WisdomData;
const router = window.WisdomRouter;
const byId = id => document.getElementById(id);
const uuid = () => globalThis.crypto?.randomUUID?.() || Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
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
let identity, selected, libraries = [], activeJob, libraryView = 0, sourceViewSerial = 0, sourceViewController, searchSerial = 0, searchController, latestClickTrigger = null;
let privateDialogInvalidators = new Set();
const captureRouteToken = () => router.current()?.token;
const isRouteCurrent = token => router.current()?.token === token;
const registerPrivateDialogInvalidator = (dialog, invalidate) => {
  const entry = {dialog, invalidate};
  privateDialogInvalidators.add(entry);
  dialog.addEventListener('close', () => privateDialogInvalidators.delete(entry), {once: true});
};
const invalidatePrivateDialogs = () => {
  for (const entry of [...privateDialogInvalidators]) {
    privateDialogInvalidators.delete(entry);
    if (!entry.dialog.isConnected) continue;
    try { entry.invalidate(); } catch (_) { /* закрытие диалога не должно блокировать переход */ }
  }
  document.querySelectorAll('dialog[data-wisdom-private]').forEach(dialog => {
    if (dialog.open) dialog.close();
    else dialog.remove();
  });
};
const SOURCE_PAGE_SIZE = 200;
const SOURCE_AUTO_SEARCH_PAGES = 3;
const SOURCE_REQUEST_TIMEOUT_MS = 12000;
const message = text => { byId('status').textContent = text; };
window.WisdomData.onMessage(message);
window.WisdomData.onInvalidate(() => {
  cancelSourceView();
  searchSerial++;
  searchController?.abort();
  searchController = null;
  const form = byId('search');
  if (form instanceof HTMLFormElement) form.reset();
  const clear = form?.querySelector('[data-wisdom-clear-search]');
  if (clear) clear.hidden = true;
  const results = byId('search-results');
  if (results) results.replaceChildren(node('p', 'Введите запрос для поиска.'));
});
function cancelSourceView() {
  sourceViewSerial++;
  const active = sourceViewController;
  active?.abort();
  document.querySelectorAll('dialog[data-wisdom-source-view]').forEach(dialog => dialog.close());
  if (sourceViewController === active) sourceViewController = null;
}
document.addEventListener('click', event => {
  const trigger = event.target instanceof Element ? event.target.closest('button, a[href]') : null;
  latestClickTrigger = trigger;
  queueMicrotask(() => { if (latestClickTrigger === trigger) latestClickTrigger = null; });
}, true);
const readableError = error => {
  if (typeof error?.message === 'string' && error.message.trim()) return error.message;
  if (typeof error === 'string' && error.trim()) return error;
  return 'Неизвестная ошибка.';
};
const currentReturnFocus = () => document.activeElement instanceof HTMLElement && document.activeElement !== document.body
  ? document.activeElement
  : latestClickTrigger instanceof HTMLElement && latestClickTrigger.isConnected ? latestClickTrigger : null;
const restoreFocus = target => queueMicrotask(() => {
  if (target instanceof HTMLElement && target.isConnected && typeof target.focus === 'function' && !target.matches(':disabled')) {
    target.focus({preventScroll: true});
  }
});
const safe = (fn, onError) => {
  let pending = false;
  return async event => {
    event?.preventDefault();
    if (pending) return;
    pending = true;
    const current = event?.currentTarget instanceof HTMLElement ? event.currentTarget : null;
    const submitter = event?.type === 'submit'
      ? (event.submitter instanceof HTMLElement ? event.submitter : current?.querySelector('button[type=submit]'))
      : null;
    const target = submitter || current;
    const disabled = target && 'disabled' in target ? Boolean(target.disabled) : null;
    const busy = target?.getAttribute('aria-busy');
    if (target) target.setAttribute('aria-busy', 'true');
    try {
      const operation = fn(event);
      if (target && disabled !== null) target.disabled = true;
      await operation;
    } catch (error) {
      if (onError) onError(error, event); else message(readableError(error));
    } finally {
      pending = false;
      if (target) {
        if (disabled !== null) target.disabled = disabled;
        if (busy === null) target.removeAttribute('aria-busy'); else target.setAttribute('aria-busy', busy);
      }
    }
  };
};
const button = (text, fn) => node('button', text, {type: 'button', onclick: safe(fn)});
const pagerButton = (text, fn) => {
  const control = node('button', text, {type: 'button'});
  control.addEventListener('click', event => { event.preventDefault(); fn(event); });
  return control;
};
function field(form, title, name, value = '', type = 'text') {
  const id = 'f-' + uuid();
  form.append(node('label', title, {for: id}));
  const input = node(type === 'textarea' ? 'textarea' : 'input', null, {id, name});
  if (type !== 'textarea') input.type = type;
  input.value = value ?? '';
  form.append(input);
  return input;
}
function dialogForm(title, render, submit) {
  const returnFocus = currentReturnFocus();
  const routeToken = captureRouteToken();
  const controller = new AbortController();
  const headingId = 'dialog-title-' + uuid(), errorId = 'dialog-error-' + uuid();
  const dialog = node('dialog', null, {'data-wisdom-private':'true','aria-labelledby':headingId});
  const isCurrent = () => isRouteCurrent(routeToken) && !controller.signal.aborted && dialog.isConnected;
  const form = node('form');
  const errorSummary = node('div', null, {id:errorId, role:'alert', tabindex:'-1', class:'dialog-form-error'});
  errorSummary.hidden = true;
  form.setAttribute('aria-describedby', errorId);
  dialog.append(node('h2', title, {id:headingId}), form);
  render(form);
  const submitButton = node('button', 'Сохранить', {type: 'submit'});
  submitButton.setAttribute('aria-describedby', errorId);
  form.append(errorSummary, submitButton, button('Отмена', () => dialog.close()));
  form.addEventListener('submit', safe(async () => {
    if (!isCurrent()) { if (dialog.open) dialog.close(); return; }
    errorSummary.textContent = '';
    errorSummary.hidden = true;
    const data = new FormData(form);
    await submit(data, {isCurrent, signal: controller.signal});
    if (isCurrent()) dialog.close();
    else if (dialog.open) dialog.close();
  }, error => {
    if (!isCurrent()) { if (dialog.open) dialog.close(); return; }
    errorSummary.textContent = 'Не удалось выполнить действие. Проверьте значения формы и повторите попытку. Причина: ' + readableError(error);
    errorSummary.hidden = false;
    errorSummary.focus();
  }));
  registerPrivateDialogInvalidator(dialog, () => controller.abort());
  dialog.addEventListener('close', () => {
    controller.abort();
    dialog.remove();
    if (isRouteCurrent(routeToken)) restoreFocus(returnFocus);
  });
  if (!isRouteCurrent(routeToken)) return;
  document.body.append(dialog);
  dialog.showModal();
}
function confirmDialog({title = 'Подтверждение действия', description, confirmLabel = 'Подтвердить', cancelLabel = 'Отмена'} = {}) {
  const returnFocus = currentReturnFocus();
  const routeToken = captureRouteToken();
  return new Promise(resolve => {
    const titleId = 'confirm-title-' + uuid(), descriptionId = 'confirm-description-' + uuid();
    const dialog = node('dialog', null, {'data-wisdom-private':'true','aria-labelledby':titleId,'aria-describedby':descriptionId});
    let settled = false;
    const finish = value => {
      if (settled) return;
      settled = true;
      resolve(Boolean(value) && isRouteCurrent(routeToken));
      if (dialog.open) dialog.close();
    };
    dialog.append(
      node('h2', title, {id:titleId}),
      node('p', description || 'Подтвердите выполнение действия.', {id:descriptionId})
    );
    const cancelButton = node('button', cancelLabel, {type:'button', onclick:() => finish(false)});
    const confirmButton = node('button', confirmLabel, {type:'button', onclick:() => finish(true)});
    dialog.append(cancelButton, confirmButton);
    dialog.addEventListener('cancel', event => { event.preventDefault(); finish(false); });
    registerPrivateDialogInvalidator(dialog, () => finish(false));
    dialog.addEventListener('close', () => {
      if (!settled) { settled = true; resolve(false); }
      dialog.remove();
      if (isRouteCurrent(routeToken)) restoreFocus(returnFocus);
    });
    if (!isRouteCurrent(routeToken)) { finish(false); return; }
    document.body.append(dialog);
    dialog.showModal();
    cancelButton.focus({preventScroll:true});
  });
}
const pager = options => window.WisdomList.create(featureHost, options);
const titledList = (root, title, items, options) => {
  root.append(node('h3', title));
  const list = pager(options);
  list.set(items);
  root.append(list.element, list.navigation, list.status);
  return list;
};
function downloadBlob(filename, blob) {
  const url = URL.createObjectURL(blob), a = node('a', null, {href: url, download: filename});
  document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
// Список, который читается с сервера страницами limit/offset по 50 записей.
// Элементы попадают в документ до первой загрузки, чтобы ответ страницы знал,
// какому экрану принадлежит, и поздний ответ не перерисовал закрытый список.
const pagedList = (root, title, path, key, options) => {
  root.append(node('h3', title));
  const group = node('div', null, {class: 'paged-list', 'data-paged': key});
  const list = window.WisdomList.createServer(featureHost, {
    ...options,
    label: 'Страницы списка: ' + title,
    fetchPage: ({limit, offset, signal}) => api(path + '?limit=' + limit + '&offset=' + offset, {signal})
  });
  group.append(list.element, list.navigation, list.status);
  root.append(group);
  list.load();
  return list;
};
// Адрес библиотеки не должен терять подраздел: /app/libraries/<id>/sources
// остаётся собой и после перезагрузки, и после перерисовки того же экрана.
const libraryRoute = (id, {replace = false} = {}) => {
  const here = String(location.pathname).match(/^\/app\/libraries\/([^/]+)\/(sources|knowledge|rules)$/);
  const section = here && here[1] === id ? here[2] : null;
  const target = router.path('libraries', section ? [id, section] : [id]);
  if (location.pathname + location.search === target) return;
  router.navigate(target, {replace});
};
async function loadLibraries() {
  const page = await api('/api/libraries'); libraries = page.libraries;
  const list = byId('library-list'); list.replaceChildren(node('h2', 'Мои библиотеки'));
  if (!libraries.length) list.append(node('p', 'База чистая. Создайте библиотеку и загрузите материал.'));
  const items = pager({
    renderItem: lib => button(lib.name + ' · ' + lib.kind, () => openLibrary(lib)),
    empty: 'База чистая. Создайте библиотеку и загрузите материал.'
  });
  if (!libraries.length) return;
  items.set(libraries);
  list.append(items.element, items.navigation, items.status);
}
// Каждый вход в раздел перечитывает список, как и раньше: сетевой запрос
// делает только слой данных (кэш и объединение одинаковых чтений).
const ensureLibraries = () => loadLibraries();
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
async function viewSource(id, options = {}) {
  sourceViewController?.abort();
  const controller = new AbortController();
  const selection = ++sourceViewSerial;
  const callerIsCurrent = typeof options.isCurrent === 'function' ? options.isCurrent : () => true;
  sourceViewController = controller;
  const isCurrent = () => callerIsCurrent() && selection === sourceViewSerial && !controller.signal.aborted;
  const explicitReturnFocus = options.returnFocus instanceof Element ? options.returnFocus : null;
  const clickedReturnFocus = latestClickTrigger instanceof Element && latestClickTrigger.isConnected ? latestClickTrigger : null;
  const activeReturnFocus = document.activeElement instanceof Element ? document.activeElement : null;
  const returnFocus = explicitReturnFocus || clickedReturnFocus || activeReturnFocus;
  const requestSource = async path => {
    if (!isCurrent()) throw new DOMException('Просмотр источника отменён.', 'AbortError');
    const requestController = new AbortController();
    const forwardAbort = () => requestController.abort();
    if (controller.signal.aborted) requestController.abort();
    else controller.signal.addEventListener('abort', forwardAbort, {once: true});
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      requestController.abort();
    }, SOURCE_REQUEST_TIMEOUT_MS);
    try {
      const response = await api(path, {signal: requestController.signal});
      if (!isCurrent()) throw new DOMException('Просмотр источника отменён.', 'AbortError');
      return response;
    } catch (error) {
      if (timedOut && !controller.signal.aborted) throw new Error('Время ожидания ответа источника истекло. Повторите открытие.');
      throw error;
    } finally {
      clearTimeout(timer);
      controller.signal.removeEventListener('abort', forwardAbort);
    }
  };
  const targetQuote = typeof options.quote === 'string' && options.quote.length ? options.quote : '';
  const targetFragmentId = options.fragmentId != null && String(options.fragmentId) ? String(options.fragmentId) : '';
  const targetLocator = options.locator && typeof options.locator === 'object' ? options.locator : {};
  const locatorPairs = [
    ['chapter', 'chapter'], ['file_page', 'file_page'], ['printed_label', 'printed_label'],
    ['paragraph', 'paragraph'], ['table_index', 'table_index'], ['cell_row', 'cell_row'], ['cell_column', 'cell_column']
  ].filter(([key]) => targetLocator[key] != null);
  const hasTarget = Boolean(targetQuote || targetFragmentId || locatorPairs.length);
  const matchesTarget = part => {
    if (targetFragmentId && String(part.id ?? part.fragment_id ?? '') !== targetFragmentId) return false;
    if (targetQuote && (typeof part.text !== 'string' || !part.text.includes(targetQuote))) return false;
    return locatorPairs.every(([locatorKey, fragmentKey]) => String(part[fragmentKey] ?? '') === String(targetLocator[locatorKey]));
  };
  let initial, displayPage, displayFrom = 0, previousStarts = [], targetPart = null;
  let pagesScanned = 0, fragmentsScanned = 0, searchStop = 'none';
  try {
    initial = await requestSource('/api/sources/' + encodeURIComponent(id) + '?from_fragment=0&limit=' + SOURCE_PAGE_SIZE);
    if (!isCurrent()) return;
    pagesScanned = 1;
    fragmentsScanned = initial.fragments.length;
    if (hasTarget) {
      let page = initial, pageFrom = 0, priorStarts = [], previousOrdinal = null;
      for (let pageNumber = 1; pageNumber <= SOURCE_AUTO_SEARCH_PAGES && isCurrent(); pageNumber++) {
        const match = page.fragments.find(matchesTarget) || null;
        if (match) {
          targetPart = match;
          displayPage = page;
          displayFrom = pageFrom;
          previousStarts = priorStarts.slice();
          searchStop = 'found';
          break;
        }
        const last = page.fragments.at(-1);
        if (page.fragments.length < SOURCE_PAGE_SIZE) { searchStop = 'end'; break; }
        if (!Number.isInteger(last?.ordinal) || last.ordinal < 0 ||
            (previousOrdinal !== null && last.ordinal <= previousOrdinal)) {
          searchStop = 'ordinal';
          break;
        }
        if (pageNumber === SOURCE_AUTO_SEARCH_PAGES) { searchStop = 'limit'; break; }
        previousOrdinal = last.ordinal;
        const nextFrom = last.ordinal + 1;
        priorStarts.push(pageFrom);
        pageFrom = nextFrom;
        page = await requestSource('/api/sources/' + encodeURIComponent(id) + '?from_fragment=' + encodeURIComponent(nextFrom) + '&limit=' + SOURCE_PAGE_SIZE);
        if (!isCurrent()) return;
        pagesScanned = pageNumber + 1;
        fragmentsScanned += page.fragments.length;
      }
    }
    if (!displayPage) { displayPage = initial; displayFrom = 0; previousStarts = []; }
  } catch (error) {
    if (!isCurrent()) return;
    throw error;
  }
  const headingId = 'source-view-title-' + uuid();
  const d = node('dialog',null,{'data-wisdom-private':'true','data-wisdom-source-view':'true','aria-labelledby':headingId});
  d.append(node('h2', initial.source.title, {id:headingId}));
  d.append(node('p', 'Автор: ' + (initial.source.author ?? 'не указан') + ' · Издание: ' + (initial.source.edition ?? 'не указано')));
  d.append(node('a', 'Скачать оригинал', {href: '/api/sources/' + id + '/original', download: initial.source.title}));
  const content = node('div'), pagerLine = node('p'), targetLine = node('p');
  const locationOf = part => [
    part.chapter != null && part.chapter !== '' ? 'Глава: ' + part.chapter : null,
    part.file_page != null ? 'Страница файла ' + part.file_page : null,
    part.printed_label ? 'Печатная метка: ' + part.printed_label : null,
    part.paragraph != null ? 'Абзац: ' + part.paragraph : null,
    part.table_index != null ? 'Таблица ' + part.table_index + (part.cell_row != null || part.cell_column != null ? ', ячейка ' + String(part.cell_row ?? '?') + ':' + String(part.cell_column ?? '?') : '') : null,
    Number.isInteger(part.ordinal) ? 'Фрагмент ' + part.ordinal : null
  ].filter(Boolean).join(' · ');
  const samePart = part => Boolean(targetPart && (part === targetPart || (Number.isInteger(part?.ordinal) && part.ordinal === targetPart.ordinal)));
  if (hasTarget) {
    targetLine.setAttribute('role', 'status');
    const targetDescription = [
      targetFragmentId ? 'фрагмента' : null,
      targetQuote ? 'цитаты' : null,
      locatorPairs.length ? 'локатора' : null
    ].filter(Boolean).join(' и ');
    if (targetPart) {
      const foundLabel = targetQuote ? 'Цитата найдена' : targetFragmentId ? 'Фрагмент найден' : 'Локатор найден';
      targetLine.textContent = foundLabel + ' в источнике' + (locationOf(targetPart) ? ' · ' + locationOf(targetPart) : '') + '.';
    } else if (searchStop === 'limit') {
      targetLine.textContent = 'Точное совпадение ' + targetDescription + ' не найдено в первых ' + SOURCE_AUTO_SEARCH_PAGES + ' страницах (' + fragmentsScanned + ' фрагментов). Источник может продолжаться; автоматический поиск ограничен тремя страницами.';
    } else if (searchStop === 'ordinal') {
      targetLine.textContent = 'Точное совпадение ' + targetDescription + ' не найдено среди ' + fragmentsScanned + ' фрагментов. Автоматическое продолжение остановлено: сервер не вернул корректный следующий порядковый номер.';
    } else {
      targetLine.textContent = 'Точное совпадение ' + targetDescription + ' не найдено среди ' + fragmentsScanned + ' фрагментов доступной выдачи источника. Проверьте данные или вернитесь к результату.';
    }
  } else targetLine.hidden = true;
  let currentFrom = displayFrom, currentLastOrdinal = displayPage.fragments.at(-1)?.ordinal;
  let previousFrom = previousStarts.slice(), pending = false, hasNext = displayPage.fragments.length === SOURCE_PAGE_SIZE;
  const previous = pagerButton('Предыдущая часть', async () => {
    if (!previousFrom.length) return;
    await renderPage(previousFrom.at(-1));
  });
  const next = pagerButton('Следующая часть', async () => {
    if (pending) return;
    pending = true; next.disabled = true;
    next.setAttribute('aria-busy', 'true');
    try {
      if (!Number.isInteger(currentLastOrdinal)) {
        hasNext = false;
        pagerLine.textContent = 'Сервер не указал порядковый номер последнего фрагмента, поэтому продолжение вычислять нельзя.';
      } else {
        const nextFrom = currentLastOrdinal + 1;
        const page = await requestSource('/api/sources/' + encodeURIComponent(id) + '?from_fragment=' + encodeURIComponent(nextFrom) + '&limit=' + SOURCE_PAGE_SIZE);
        if (!isCurrent() || !d.isConnected) return;
        if (page.fragments.length) {
          const lastOrdinal = page.fragments.at(-1)?.ordinal;
          previousFrom.push(currentFrom);
          currentFrom = nextFrom;
          currentLastOrdinal = Number.isInteger(lastOrdinal) ? lastOrdinal : null;
          hasNext = page.fragments.length === SOURCE_PAGE_SIZE;
          renderFragments(page.fragments);
        } else {
          hasNext = false; pagerLine.textContent = 'Дальше фрагментов нет.';
        }
      }
    } catch (error) {
      if (isCurrent() && d.isConnected) pagerLine.textContent = 'Не удалось загрузить следующую часть: ' + error.message;
    } finally {
      if (isCurrent() && d.isConnected) {
        pending = false;
        next.removeAttribute('aria-busy');
        next.disabled = !hasNext;
        previous.disabled = previousFrom.length === 0;
      }
    }
  });
  async function renderPage(from) {
    if (pending) return;
    pending = true;
    previous.disabled = true; next.disabled = true;
    previous.setAttribute('aria-busy', 'true');
    pagerLine.textContent = 'Загружаю фрагменты…';
    try {
      const page = await requestSource('/api/sources/' + encodeURIComponent(id) + '?from_fragment=' + encodeURIComponent(from) + '&limit=' + SOURCE_PAGE_SIZE);
      if (!isCurrent() || !d.isConnected) return;
      if (!page.fragments.length) { pagerLine.textContent = 'Предыдущая часть фрагментов не найдена.'; return; }
      previousFrom.pop(); currentFrom = from;
      const lastOrdinal = page.fragments.at(-1)?.ordinal;
      currentLastOrdinal = Number.isInteger(lastOrdinal) ? lastOrdinal : null;
      hasNext = page.fragments.length === SOURCE_PAGE_SIZE;
      renderFragments(page.fragments);
    } catch (error) {
      if (isCurrent() && d.isConnected) pagerLine.textContent = 'Не удалось загрузить предыдущую часть: ' + error.message;
    } finally {
      if (isCurrent() && d.isConnected) {
        pending = false;
        previous.removeAttribute('aria-busy');
        previous.disabled = previousFrom.length === 0;
        next.disabled = !hasNext;
      }
    }
  }
  let targetFocus = null;
  function renderFragments(fragments) {
    if (!isCurrent() || !d.isConnected) return;
    content.replaceChildren();
    targetFocus = null;
    hasNext = fragments.length === SOURCE_PAGE_SIZE;
    for (const part of fragments) {
      const fragment = node('article', null, {class:'source-fragment'});
      const exactTarget = samePart(part) && Boolean(targetQuote || targetFragmentId || locatorPairs.length > 0);
      if (exactTarget) {
        const exactLocation = locationOf(part);
        fragment.id = 'source-fragment-' + uuid();
        fragment.tabIndex = -1;
        fragment.dataset.sourceFragmentTarget = 'true';
        fragment.setAttribute('aria-label', 'Точное совпадение' + (exactLocation ? ' · ' + exactLocation : ''));
        fragment.append(node('p', 'Точное совпадение' + (exactLocation ? ' · ' + exactLocation : ''), {class:'muted source-fragment-target-label'}));
        targetFocus = fragment;
      }
      const pre = node('pre');
      const text = typeof part.text === 'string' ? part.text : '';
      if (exactTarget && targetQuote) {
        const at = text.indexOf(targetQuote);
        if (at >= 0) pre.append(document.createTextNode(text.slice(0, at)), node('mark', text.slice(at, at + targetQuote.length)), document.createTextNode(text.slice(at + targetQuote.length)));
        else pre.append(document.createTextNode(text));
      } else pre.append(document.createTextNode(text));
      fragment.append(node('p', locationOf(part) || 'Место в тексте не указано.'), pre);
      content.append(fragment);
    }
    pagerLine.textContent = fragments.length ? 'Показано фрагментов: ' + fragments.length + (hasNext ? ' · возможно, есть продолжение' : '') : 'Фрагментов в источнике пока нет.';
  }
  d.append(previous, next, pagerLine, targetLine, content);
  if (typeof options.onReturn === 'function') {
    const returnLabel = typeof options.returnLabel === 'string' && options.returnLabel.trim() ? options.returnLabel : 'Вернуться';
    d.append(button(returnLabel, async () => {
      const closed = new Promise(resolve => d.addEventListener('close', resolve, {once:true}));
      d.close();
      await closed;
      if (!callerIsCurrent()) return;
      return options.onReturn();
    }));
  }
  d.append(button('Закрыть', () => d.close()));
  d.addEventListener('close', () => {
    const ownsView = sourceViewController === controller;
    if (ownsView) sourceViewController = null;
    controller.abort();
    d.remove();
    if (ownsView && returnFocus?.isConnected && typeof returnFocus.focus === 'function') returnFocus.focus({preventScroll: true});
  });
  if (!isCurrent()) {
    if (sourceViewController === controller) sourceViewController = null;
    return;
  }
  document.body.append(d);
  if (!isCurrent()) {
    if (sourceViewController === controller) sourceViewController = null;
    d.remove();
    return;
  }
  d.showModal();
  if (!isCurrent()) { d.close(); return; }
  renderFragments(displayPage.fragments);
  if (targetFocus) {
    targetFocus.scrollIntoView({block:'center'});
    targetFocus.focus({preventScroll:true});
  }
  previous.disabled = previousFrom.length === 0;
  next.disabled = !hasNext;
}
async function openLibrary(lib, {guard = null} = {}) {
  if (!lib) return;
  libraryView++;
  const view = libraryView;
  const current = () => view === libraryView && selected?.id === lib.id && (!guard || guard());
  libraryRoute(lib.id, {replace: selected?.id === lib.id});
  selected = lib; const root = byId('library-detail'); root.replaceChildren(node('h2', lib.name));
  const policy = await api('/api/libraries/' + lib.id + '/policy');
  if (!current()) return;
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
  pagedList(root, 'Источники и происхождение', '/api/libraries/' + lib.id + '/sources', 'sources', {
    empty: 'Источников пока нет. Загрузите материал выше.',
    renderItem: source => {
      const card = node('article', null, {class: 'card'});
      card.append(button(source.title, () => viewSource(source.id, {isCurrent:current})), node('p', 'Статус: ' + source.processing),
        button('Показать связанные понятия', () => featureHost.navigateMapSource(lib.id, source.id)));
      if (contributor) {
        card.append(button('Разобрать текст', () => processSource(source.id, false)));
        const extractAction = button('Извлечь / продолжить черновики', () => processSource(source.id, true));
        extractAction.disabled = !extractionAllowed;
        if (!extractionAllowed) { extractAction.title = 'Отключено политикой библиотеки'; card.append(extractAction, node('p', 'Извлечение отключено политикой библиотеки.', {class: 'muted'})); }
        else card.append(extractAction);
      }
      return card;
    }
  });
  pagedList(root, 'Знания', '/api/libraries/' + lib.id + '/knowledge', 'knowledge', {
    empty: 'Знаний пока нет.',
    renderItem: item => {
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
      return card;
    }
  });
  pagedList(root, 'Правила и навыки', '/api/libraries/' + lib.id + '/rules', 'rules', {
    empty: 'Правил пока нет.',
    renderItem: rule => {
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
        }, async (data, {isCurrent}) => {
          if (!isCurrent()) return;
          const payload = {rule_id: rule.id, version_no: rule.version_no, where_applied: data.get('where_applied') || null, expected_result: data.get('expected_result') || null};
          if (!pendingUse || JSON.stringify(pendingUse.payload) !== JSON.stringify(payload)) pendingUse = {payload, key: crypto.randomUUID()};
          if (!isCurrent()) return;
          const use = await post('/api/uses', {...pendingUse.payload, idempotency_key: pendingUse.key});
          if (!isCurrent()) return;
          await post('/api/uses/' + use.id + '/applied');
          if (!isCurrent()) return;
          message('Применение записано. Результат добавьте в разделе «Опыт».');
        });
        }));
        if (policy.export_allowed) card.append(button('Экспорт SKILL.md', async () => {
          const result = await post('/api/libraries/' + lib.id + '/export', {rule_ids: [rule.id]});
          const url = URL.createObjectURL(new Blob([result.content], {type: 'text/markdown;charset=utf-8'}));
          const a = node('a', null, {href: url, download: 'SKILL.md'}); a.click(); setTimeout(() => URL.revokeObjectURL(url), 5000);
        }));
        else card.append(node('p', 'Экспорт отключён политикой библиотеки.', {class: 'muted'}));
      }
      return card;
    }
  });
  if (lib.kind === 'project') await window.WisdomProjectContext.mount(root, lib, featureHost, {canCurate: curator});
  await window.WisdomCompletion.enhanceLibrary(root, lib, featureHost);
}
function ruleEditor(lib, rule) {
  let evidence;
  dialogForm(rule ? 'Новая версия правила' : 'Новое правило', form => {
    for (const [title, key, type] of [['Название', 'title', 'text'], ['Когда применять', 'when_to_apply', 'textarea'], ['Входные данные — по одному на строку', 'inputs', 'textarea'], ['Условия', 'preconditions', 'textarea'], ['Шаги — по одному на строку', 'actions', 'textarea'], ['Исключения', 'exceptions', 'textarea'], ['Ожидаемый эффект', 'expected_effect', 'textarea'], ['Как проверить', 'verification', 'textarea']]) field(form, title, key, Array.isArray(rule?.[key]) ? rule[key].join('\n') : rule?.[key], type);
    evidence = window.WisdomRuleEvidence.mount(form, {api, libraryId: lib.id, initialIds: (rule?.provenance ?? []).map(p => p.fragment_id).filter(Boolean)});
  }, async (data, {isCurrent}) => {
    if (!isCurrent()) return;
    const body = Object.fromEntries(data); for (const key of ['inputs', 'preconditions', 'actions', 'exceptions']) body[key] = body[key].split('\n').filter(v => v.trim());
    for (const key of ['when_to_apply', 'expected_effect', 'verification']) body[key] ||= null;
    body.fragment_ids = evidence.selectedIds();
    if (!isCurrent()) return;
    await post('/api/libraries/' + lib.id + '/rules' + (rule ? '?replaces=' + rule.id : ''), body);
    if (!isCurrent()) return;
    await openLibrary(lib);
  });
}
async function loadUses() {
  const list = byId('uses-list'); list.replaceChildren();
  const records = await api('/api/uses');
  const uses = pager({
    empty: 'Применения пока не записаны. Просмотр и поиск не создают опыт.',
    renderItem: use => {
      const card = node('article', null, {class: 'card'}); card.append(node('h2', use.title + ' · v' + use.rule_version_no), node('p', 'Состояние: ' + use.state), node('p', 'Ожидали: ' + (use.expected_result ?? 'не указано')), node('p', 'Где: ' + (use.where_applied ?? 'не указан')));
      const achievementLabels = {met:'достигнута', partly:'достигнута частично', not_met:'не достигнута', unknown:'пока неизвестно'};
      const assessorLabels = {human:'человек', agent:'агент', measurement:'измерение', unknown:'не указан'};
      for (const result of use.outcomes) card.append(node('p', (result.is_correction ? 'Исправление: ' : 'Результат: ') + (result.observed_result ?? 'не указан') + ' · Полезность: ' + (result.rating ?? 'не оценена') + ' · Цель: ' + (achievementLabels[result.achievement] ?? 'пока неизвестно') + ' · Автор оценки: ' + (assessorLabels[result.assessor_kind] ?? 'не указан') + ' · Основание полезности: ' + (result.rating_basis ?? 'не указано') + ' · Основание результата: ' + (result.assessment_evidence ?? 'не указано')));
      if (use.state === 'retrieved') card.append(button('Подтвердить применение', async () => { await post('/api/uses/' + use.id + '/applied'); await loadUses(); }));
      else card.append(button(use.outcomes.length ? 'Добавить исправление' : 'Записать результат', () => {
        let pendingOutcome = null;
        return dialogForm('Результат применения', form => {
        field(form, 'Что получилось', 'observed_result', '', 'textarea'); const rating = field(form, 'Полезность от 1 до 5 (можно не указывать)', 'rating', '', 'number'); rating.min = 1; rating.max = 5;
        field(form, 'Почему такая оценка', 'rating_basis', '', 'textarea');
        const outcomeId = 'achievement-' + crypto.randomUUID();
        const outcomeSelect = node('select', null, {id: outcomeId, name:'achievement'});
        for (const [value, label] of [['unknown','Пока неизвестно'],['met','Цель достигнута'],['partly','Достигнута частично'],['not_met','Цель не достигнута']]) outcomeSelect.append(node('option', label, {value}));
        form.append(node('label', 'Достижение ожидаемого результата', {for:outcomeId}), outcomeSelect);
        field(form, 'Наблюдение или ссылка, подтверждающие результат', 'assessment_evidence', '', 'textarea');
      }, async (data, {isCurrent}) => {
        if (!isCurrent()) return;
        const payload = {observed_result: data.get('observed_result') || null, rating: data.get('rating') ? Number(data.get('rating')) : null, rating_basis: data.get('rating_basis') || null, achievement:data.get('achievement'), assessor_kind:'human', assessment_evidence:data.get('assessment_evidence') || null, is_correction: use.outcomes.length > 0};
        if (!pendingOutcome || JSON.stringify(pendingOutcome.payload) !== JSON.stringify(payload)) pendingOutcome = {payload, key: crypto.randomUUID()};
        if (!isCurrent()) return;
        await post('/api/uses/' + use.id + '/outcomes', {...pendingOutcome.payload, idempotency_key: pendingOutcome.key});
        if (!isCurrent()) return;
        await loadUses();
      });
      }));
      return card;
    }
  });
  uses.set(records);
  list.append(uses.element, uses.navigation, uses.status);
  await window.WisdomCompletion.enhanceUses(list, featureHost);
  await window.WisdomSharedExperience.mount(list, featureHost, records);
}
byId('create-library').addEventListener('submit', safe(async event => {
  const form = event.target;
  const nameInput = form.elements.namedItem('name');
  const nameError = byId('library-name-error');
  const setNameError = text => {
    nameError.textContent = text;
    nameError.hidden = !text;
    nameInput.setAttribute('aria-invalid', text ? 'true' : 'false');
    if (text) nameInput.focus();
  };
  setNameError('');
  const data = Object.fromEntries(new FormData(form));
  if (!String(data.name ?? '').trim()) {
    setNameError('Введите название библиотеки.');
    return;
  }
  data.organisation_id = identity.account_id;
  const project = data.kind === 'project'; if (project) delete data.kind;
  let result;
  try {
    result = await post(project ? '/api/projects' : '/api/libraries', data);
  } catch (error) {
    setNameError('Не удалось создать библиотеку. Проверьте значения формы и повторите попытку. Причина: ' + readableError(error));
    throw error;
  }
  form.reset(); await ensureLibraries(); await openLibrary(project ? result.project : result);
}));
const searchForm = byId('search');
const searchInput = searchForm.querySelector('input[name="q"]');
const existingClear = searchForm.querySelector('[data-wisdom-clear-search]') || Array.from(searchForm.querySelectorAll('button')).find(candidate => /^(Очистить поиск|Clear search)$/i.test((candidate.textContent || '').trim()));
const searchClear = existingClear || node('button', 'Очистить поиск', {type: 'button'});
searchClear.type = 'button';
searchClear.setAttribute('data-wisdom-clear-search', 'true');
if (!existingClear) searchForm.append(searchClear);
searchClear.hidden = !String(searchInput?.value || '').trim();
searchClear.addEventListener('click', () => {
  searchController?.abort(); searchController = null; searchSerial++;
  searchForm.reset(); searchClear.hidden = true; searchInput?.focus();
  byId('search-results').replaceChildren(node('p', 'Введите запрос для поиска.'));
});
searchInput?.addEventListener('input', () => { searchClear.hidden = !String(searchInput.value || '').trim(); });
searchForm.addEventListener('submit', async event => {
  event.preventDefault();
  const query = String(new FormData(event.target).get('q') || '').trim();
  const list = byId('search-results');
  if (!query) {
    searchController?.abort(); searchController = null; searchSerial++;
    searchClear.hidden = true;
    list.replaceChildren(node('p', 'Введите запрос для поиска.'));
    message('Введите запрос для поиска.');
    return;
  }
  searchController?.abort();
  const controller = new AbortController(), serial = ++searchSerial;
  searchController = controller; searchClear.hidden = false;
  list.replaceChildren(node('p', 'Ищу совпадения…', {role: 'status'}));
  try {
    const result = await api('/api/search?q=' + encodeURIComponent(query), {signal: controller.signal});
    if (serial !== searchSerial || controller.signal.aborted) return;
    list.replaceChildren(node('p', ({semantic:'Поиск по смыслу',hybrid:'Поиск по смыслу и словам',lexical:'Поиск по словам'}[result.mode] || result.mode) + ' · ' + result.hits.length + ' совпадений'));
    if (result.indexed_library_count != null || result.pending_library_count != null) {
      const counts = [];
      if (result.indexed_library_count != null) counts.push('индекс готов — ' + result.indexed_library_count);
      if (result.pending_library_count != null) counts.push('индекс не готов — ' + result.pending_library_count);
      list.append(node('p', 'Библиотеки: ' + counts.join(' · ') + '.'));
    }
    if (result.fallback_reason === 'index_pending_or_unavailable') list.append(node('p', 'Пока ищем по словам: индекс библиотеки ещё не готов или временно недоступен.'));
    const hits = pager({
      empty: 'Совпадений не найдено.',
      size: 10,
      renderItem: hit => {
        const block = node('article', null, {class: 'search-hit'});
        block.append(node('p', hit.statement));
        const citations = Array.isArray(hit.provenance) ? hit.provenance : [];
        if (!citations.length) block.append(node('p', 'Источник и место в тексте не указаны.'));
        for (const provenance of citations) {
          const source = provenance.source && typeof provenance.source === 'object' ? provenance.source : provenance;
          const locator = provenance.locator && typeof provenance.locator === 'object' ? provenance.locator : {};
          const label = value => typeof value === 'string' || typeof value === 'number' ? String(value) : null;
          const citation = [
            label(source.title),
            label(source.author) ? 'Автор: ' + label(source.author) : null,
            label(source.publication_ref),
            label(locator.chapter) != null ? 'Глава: ' + label(locator.chapter) : null,
            label(locator.file_page) !== null ? 'Страница файла ' + label(locator.file_page) : null,
            label(locator.printed_label) ? 'Печатная метка ' + label(locator.printed_label) : null,
            label(locator.paragraph) !== null ? 'Абзац ' + label(locator.paragraph) : null,
            label(locator.table_index) !== null ? 'Таблица ' + label(locator.table_index) + (locator.cell_row != null || locator.cell_column != null ? ', ячейка ' + String(locator.cell_row ?? '?') + ':' + String(locator.cell_column ?? '?') : '') : null
          ].filter(Boolean);
          const details = node('p', citation.length ? citation.join(' · ') : 'Источник и место в тексте не указаны.');
          const sourceId = provenance.source_id, quote = typeof provenance.quote === 'string' ? provenance.quote : '';
          const hasLocator = ['chapter','file_page','printed_label','paragraph','table_index','cell_row','cell_column'].some(key => locator[key] != null);
          if (typeof sourceId === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(sourceId)) {
            const sourceOptions = {quote, locator, isCurrent:() => serial === searchSerial && !controller.signal.aborted};
            if (provenance.fragment_id != null) sourceOptions.fragmentId = provenance.fragment_id;
            details.append(document.createTextNode(' · '), button(quote || provenance.fragment_id != null || hasLocator ? 'Открыть точное место' : 'Открыть источник', () => viewSource(sourceId, sourceOptions)));
          }
          if (typeof source.url === 'string') {
            try {
              const url = new URL(source.url);
              if (['http:', 'https:'].includes(url.protocol)) details.append(document.createTextNode(' · '), node('a', 'Ссылка', {href: url.href}));
            } catch {}
          }
          block.append(details);
        }
        return block;
      }
    });
    hits.set(result.hits);
    list.append(hits.element, hits.navigation, hits.status);
  } catch (error) {
    if (serial !== searchSerial || error.name === 'AbortError') return;
    const text = 'Поиск не завершён. ' + (error.message ? error.message + ' ' : 'Сервер не указал причину. ') + 'Проверьте запрос и повторите поиск.';
    list.replaceChildren(node('p', text, {role: 'alert'}));
    message(text);
  } finally {
    if (serial === searchSerial) searchController = null;
  }
});
byId('logout').addEventListener('click', safe(async () => { await post('/auth/logout'); location.reload(); }));
const featureHost={node,button,api,post,notify:message,confirm:confirmDialog,libraries:()=>libraries,currentLibrary:()=>selected,identity:()=>identity,
  routeToken:captureRouteToken,routeIsCurrent:isRouteCurrent,
  refreshLibrary:()=>selected?openLibrary(selected):Promise.resolve(),
  openSource:viewSource,
  librarySourcesPath:libraryId=>router.path('libraries',[libraryId,'sources']),
  navigateMapSource:(libraryId,sourceId,view='semantic')=>{
    const query = new URLSearchParams({library:libraryId,view});
    if (sourceId) query.set('source',sourceId);
    router.navigate(router.path('maps') + '?' + query.toString());
  },
  download:(filename,text,type)=>downloadBlob(filename,new Blob([text],{type})),
  downloadBlob,dialog:dialogForm,navigateResearch:()=>router.navigate('research'),
  openRuleVersion:async(id,version)=>{
    const routeToken=captureRouteToken();
    const rule=await api('/api/rules/'+id);
    if(!isRouteCurrent(routeToken))return;
    const controller=new AbortController();
    const headingId='rule-version-title-'+uuid();
    const d=node('dialog',null,{'data-wisdom-private':'true','aria-labelledby':headingId});
    const isCurrent=()=>isRouteCurrent(routeToken)&&!controller.signal.aborted;
    d.append(node('h2',rule.title+' · Версия '+(version||rule.version_no),{id:headingId}),node('p',rule.when_to_apply),node('p',rule.expected_effect));
    for(const a of rule.actions||[])d.append(node('p',a.body||a));
    d.append(button('Закрыть',()=>d.close()));
    registerPrivateDialogInvalidator(d,()=>controller.abort());
    d.addEventListener('close',()=>{controller.abort();d.remove();});
    if(!isCurrent())return;
    document.body.append(d);
    if(!isCurrent()){d.remove();return;}
    d.showModal();
  }
};
window.WisdomResearch.configure(featureHost);

let identityReady = null;

const initializeIdentity = () => {
  if (identityReady !== null) return identityReady;

  identityReady = (async () => {
    identity = await api('/api/me');
    const login = document.querySelector('.login'); if (login) login.hidden = true;
    const logout = byId('logout'); if (logout) logout.hidden = false;
    const readiness = await api('/readyz');
    message(readiness.semantic_search_enabled ? 'Поиск по смыслу доступен для опубликованных знаний. Извлечение создаёт черновики для проверки.' : 'Поиск по словам · извлечение создаёт черновики для проверки.');
    return identity;
  })().catch(error => {
    identityReady = null;
    message(error.message);
    throw error;
  });

  return identityReady;
};

const ready = () => initializeIdentity();

ready().catch(() => {});

router.register('libraries', {
  title: 'AI-wisdom — Библиотеки знаний',
  leave: cancelSourceView,
  mount: async (container, context) => {
    await ready(); if (!context.isCurrent()) return;
    await ensureLibraries(); if (!context.isCurrent()) return;
    const id = context.params[0];
    if (id) {
      const lib = libraries.find(item => item.id === id);
      if (lib) { await openLibrary(lib, {guard: context.isCurrent}); return; }
      message('Библиотека не найдена или доступ к ней закрыт.');
      byId('library-detail').replaceChildren(node('h2', 'Библиотека не найдена'), node('p', 'Вернитесь к списку и выберите доступную библиотеку.'));
    }
  }
});
// Отдельный адрес контекста проекта: /app/projects/:id/context. Внутри библиотеки
// контекст по-прежнему монтируется на её экране — этот адрес его не заменяет.
router.register('context', {
  title: 'AI-wisdom — Контекст проекта',
  leave: cancelSourceView,
  mount: async (container, context) => {
    await ready(); if (!context.isCurrent()) return;
    await ensureLibraries(); if (!context.isCurrent()) return;
    const workspace = byId('context-workspace'); workspace.replaceChildren();
    const id = context.params[0];
    const project = libraries.find(item => item.id === id);
    if (!project) {
      message('Проект не найден или доступ к нему закрыт.');
      workspace.append(node('h2', 'Проект не найден'), node('p', 'Вернитесь к списку и выберите доступный проект.'));
      return;
    }
    // Контекст сам проверяет, что открыт именно он, а не другая библиотека,
    // поэтому на этом экране выбранной библиотекой становится проект.
    selected = project;
    const policy = await api('/api/libraries/' + project.id + '/policy');
    if (!context.isCurrent()) return;
    workspace.append(node('h2', project.name), node('p', 'Тип: ' + project.kind + ' · Доступ: ' + (policy.effective_role ?? 'не указан')));
    await window.WisdomProjectContext.mount(workspace, project, featureHost, {canCurate: ['curator', 'manager'].includes(policy.effective_role)});
  }
});
router.register('uses', {
  title: 'AI-wisdom — Опыт применения',
  leave: cancelSourceView,
  mount: async (container, context) => {
    await ready(); if (!context.isCurrent()) return;
    await loadUses();
  }
});
router.register('research', {
  title: 'AI-wisdom — Исследование и Studio',
  leave: () => { cancelSourceView(); window.WisdomResearch.invalidate(); },
  mount: async (container, context) => {
    await ready(); if (!context.isCurrent()) return;
    await ensureLibraries(); if (!context.isCurrent()) return;
    await window.WisdomResearch.mount(byId('research-workspace'), featureHost, context.query.get('library') || undefined, context.params[0] || undefined);
  }
});
router.register('maps', {
  title: 'AI-wisdom — Карта знаний',
  leave: () => { cancelSourceView(); window.WisdomMap.invalidate(); },
  mount: async (container, context) => {
    window.WisdomMap.invalidate();
    await ready(); if (!context.isCurrent()) return;
    await ensureLibraries(); if (!context.isCurrent()) return;
    const chosen = context.query.get('library') || undefined;
    const chosenMode = context.query.get('view') || context.query.get('mode') || undefined;
    const chosenSource = context.query.get('source') || undefined;
    await window.WisdomMap.mount(byId('maps-workspace'), featureHost, chosen, chosenMode, chosenSource);
    if (!context.isCurrent()) return;
    window.WisdomMap.restoreFocus(context.token);
  }
});
router.register('access', {
  title: 'AI-wisdom — Доступ',
  leave: cancelSourceView,
  mount: async (container, context) => {
    await ready(); if (!context.isCurrent()) return;
    await ensureLibraries(); if (!context.isCurrent()) return;
    await window.WisdomCompletion.mountAccess(byId('access-workspace'), featureHost);
  }
});
router.register('jobs', {
  title: 'AI-wisdom — Задания',
  leave: cancelSourceView,
  mount: async (container, context) => {
    await ready(); if (!context.isCurrent()) return;
    await window.WisdomCompletion.mountJobs(byId('jobs-workspace'), featureHost);
  }
});
// Ошибка загрузки раздела показывается на самом разделе и предлагает повтор.
// Раньше router отдавал её только наблюдателям, а единственный наблюдатель
// аргумент игнорировал: после сбоя оставался пустой или наполовину собранный
// экран без единого сообщения.
//
// Блок добавляется В НАЧАЛО контейнера, а не заменяет его содержимое: монтирование
// обращается к постоянным элементам экрана (library-list, library-detail,
// create-library, search для библиотек; uses-list для опыта), и если сбой удалит их,
// повтор уже ничего не сможет перерисовать. Так повтор пересобирает экран поверх
// сохранённого каркаса.
function routeErrorHost(target) {
  return byId(target.name + '-workspace') || byId(target.name + '-tab');
}
function routeFailure(target, error) {
  const host = routeErrorHost(target);
  if (!host) return;
  clearRouteFailure(target);
  const box = node('div', null, {class: 'route-error', role: 'alert'});
  box.append(node('h3', 'Не удалось загрузить раздел'));
  box.append(node('p', error && error.message ? error.message : 'Неизвестная ошибка загрузки.'));
  box.append(button('Повторить', () => { clearRouteFailure(target); router.refresh(); }));
  host.prepend(box);
}
// Успешный переход тоже чистит блок: он живёт в постоянном каркасе раздела, и без
// этого он остался бы висеть на следующем успешном посещении того же адреса.
function clearRouteFailure(target) {
  const host = routeErrorHost(target);
  if (host) host.querySelectorAll('.route-error').forEach(node => node.remove());
}
router.onChange((current, previous, error) => {
  const navigated = Boolean(previous && current && previous.token !== current.token);
  if (navigated && !error) {
    invalidatePrivateDialogs();
    if (previous.name === current.name) {
      cancelSourceView();
      if (current.name === 'maps') {
        window.WisdomMap.captureFocusIntent(current.token);
        window.WisdomMap.invalidate();
      }
    }
  }
  if (previous && current && previous.name !== current.name) router.focusHeading();
  if (!current) return;
  if (error) routeFailure(current, error);
  else clearRouteFailure(current);
});
router.start();
