'use strict';
/*
 * Постраничные списки, по 50 записей на страницу.
 *
 * Два режима с одной оболочкой (список, кнопки, строка состояния):
 *
 *   createServer — одна фиксированная серверная страница limit/offset.
 *     Навигация Previous/Next не переигрывает и не кеширует промежуточные
 *     страницы. Точный total неизвестен и не показывается.
 *
 *   create — список, который уже пришёл целиком (задания, участники, группы,
 *     узлы карты, опубликованный опыт): страницы нарезаются из массива.
 *
 * Между экземплярами сохраняется только числовой индекс страницы. Map
 * ограничен 100 ключами и не содержит записей, содержимого, access-токенов или
 * метаданных ACL. Без однозначных identity/library/list кеширование отключено.
 */
(() => {
  const DEFAULT_SIZE = 50;
  const MAX_SAVED_POSITIONS = 100;
  const savedPositions = new Map();
  const activeControllers = new Set();
  let invalidationBound = false;
  let invalidationGeneration = 0;

  // Only the identity shape and list options used by this product are supported.
  const positionKey = (host, options, mode) => {
    try {
      const identity = typeof options.identity === 'function' ? options.identity() : options.identity || host.identity?.();
      const list = options.stateKey ?? options.label;
      if (typeof identity?.principal_id !== 'string' || typeof identity?.account_id !== 'string' ||
          typeof list !== 'string' || !list.trim() || list.length > 512 || /[?#]/.test(list)) return '';
      const library = Object.hasOwn(options, 'library')
        ? (typeof options.library === 'function' ? options.library() : options.library)
        : host.currentLibrary?.();
      const libraryId = library == null ? '@global' : typeof library === 'string' ? library : library.id;
      if (typeof libraryId !== 'string' || !libraryId) return '';
      return JSON.stringify([mode, identity.principal_id, identity.account_id, libraryId, list]);
    } catch (_) { return ''; }
  };

  const readPosition = key => {
    if (!key || !savedPositions.has(key)) return null;
    const position = savedPositions.get(key);
    savedPositions.delete(key);
    savedPositions.set(key, position);
    return position;
  };

  const writePosition = (key, position) => {
    if (!key || !Number.isSafeInteger(position) || position < 0) return;
    savedPositions.delete(key);
    savedPositions.set(key, position);
    while (savedPositions.size > MAX_SAVED_POSITIONS) {
      savedPositions.delete(savedPositions.keys().next().value);
    }
  };

  const invalidatePositions = () => {
    savedPositions.clear();
    invalidationGeneration++;
    for (const pending of Array.from(activeControllers)) {
      activeControllers.delete(pending);
      try { pending.abort(); } catch (error) { /* AbortController уже освобождён. */ }
    }
  };

  const bindInvalidation = () => {
    if (invalidationBound) return;
    const source = window.WisdomData;
    if (!source || typeof source.onInvalidate !== 'function') return;
    try {
      source.onInvalidate(invalidatePositions);
      invalidationBound = true;
    } catch (error) {
      invalidationBound = false;
    }
  };

  const isAccessError = error => {
    if (!error) return false;
    const status = Number(error.status ?? error.statusCode);
    if (status === 401 || status === 403) return true;
    const code = String(error.code || error.reason || '').toLowerCase();
    return code === 'unauthorized' || code === 'forbidden'
      || code === 'access_denied' || code === 'accessdenied';
  };

  const shell = options => {
    const list = document.createElement('div');
    list.className = 'pager-list';
    const navigation = document.createElement('nav');
    navigation.className = 'pager';
    navigation.setAttribute('aria-label', options.label || 'Страницы списка');
    const status = document.createElement('p');
    status.className = 'pager-status muted';
    status.setAttribute('role', 'status');
    return {list, navigation, status};
  };

  const create = (host, options = {}) => {
    bindInvalidation();
    const size = Math.max(1, Number(options.size) || DEFAULT_SIZE);
    const renderItem = options.renderItem;
    const {list, navigation, status} = shell(options);
    const previous = document.createElement('button');
    previous.type = 'button';
    previous.textContent = 'Предыдущая страница';
    const next = document.createElement('button');
    next.type = 'button';
    next.textContent = 'Следующая страница';

    const hasExplicitStateKey = typeof options.stateKey === 'string' && Boolean(options.stateKey.trim());
    let cacheKey = positionKey(host, options, 'client');
    const restored = readPosition(cacheKey);
    let items = [];
    let page = Number.isSafeInteger(restored) && restored >= 1 ? restored : 1;
    let hydrated = false;
    let observedInvalidation = invalidationGeneration;

    const pages = () => Math.max(1, Math.ceil(items.length / size));
    const clamp = () => { page = Math.min(Math.max(1, page), pages()); };

    const syncScope = () => {
      const nextKey = positionKey(host, options, 'client');
      const invalidated = observedInvalidation !== invalidationGeneration;
      observedInvalidation = invalidationGeneration;
      if (nextKey === cacheKey && !invalidated) return false;
      cacheKey = nextKey;
      const saved = readPosition(cacheKey);
      page = Number.isSafeInteger(saved) && saved >= 1 ? saved : 1;
      items = [];
      hydrated = false;
      return true;
    };

    function paint() {
      clamp();
      writePosition(cacheKey, page);
      list.replaceChildren();
      const from = (page - 1) * size;
      const slice = items.slice(from, from + size);
      if (!slice.length) {
        const empty = document.createElement('p');
        empty.className = 'pager-empty completion-empty';
        empty.textContent = options.empty || 'Записей пока нет.';
        list.append(empty);
      } else {
        const fragment = document.createDocumentFragment();
        for (const [index, item] of slice.entries()) {
          const built = renderItem(item, from + index);
          if (!built) continue;
          fragment.append(built);
        }
        list.append(fragment);
      }
      const total = pages();
      navigation.hidden = total < 2;
      status.textContent = items.length
        ? 'Показано ' + (from + 1) + '–' + Math.min(from + size, items.length) + ' из ' + items.length
        : '';
      status.hidden = !items.length;
      previous.disabled = page <= 1;
      next.disabled = page >= total;
    }

    previous.addEventListener('click', () => {
      if (syncScope()) paint();
      if (!items.length || page <= 1) return;
      page--;
      paint();
      list.scrollIntoView({block: 'nearest'});
    });

    next.addEventListener('click', () => {
      if (syncScope()) paint();
      if (!items.length || page >= pages()) return;
      page++;
      paint();
      list.scrollIntoView({block: 'nearest'});
    });

    navigation.append(previous, next);
    return {
      element: list,
      navigation,
      status,
      set(nextItems) {
        syncScope();
        items = Array.isArray(nextItems) ? nextItems : [];
        if (hydrated && !hasExplicitStateKey) page = 1;
        hydrated = true;
        paint();
        return this;
      },
      reset() {
        syncScope();
        page = 1;
        hydrated = true;
        paint();
        return this;
      },
      count: () => items.length
    };
  };

  const createServer = (host, options = {}) => {
    bindInvalidation();
    const size = Math.max(1, Number(options.size) || DEFAULT_SIZE);
    const fetchPage = options.fetchPage;
    const renderItem = options.renderItem;
    if (typeof fetchPage !== 'function') {
      throw new Error('Для серверного списка нужен fetchPage({limit, offset, signal}).');
    }

    const {list, navigation, status} = shell(options);
    const previous = document.createElement('button');
    previous.type = 'button';
    previous.textContent = 'Предыдущая страница';
    const next = document.createElement('button');
    next.type = 'button';
    next.textContent = 'Следующая страница';
    navigation.append(previous, next);

    const normalizePage = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
    const offsetFor = value => {
      const page = normalizePage(value);
      if (page === null) return null;
      const offset = page * size;
      return Number.isSafeInteger(offset) && offset >= 0 ? offset : null;
    };

    let cacheKey = positionKey(host, options, 'server');
    const initialOffset = offsetFor(readPosition(cacheKey));
    let offset = initialOffset === null ? 0 : initialOffset;
    let shown = 0;
    let done = false;
    let busy = false;
    let epoch = 0;
    let controller = null;
    let observedInvalidation = invalidationGeneration;

    const paintStatus = (text, {pending = false} = {}) => {
      status.textContent = text;
      status.hidden = !text;
      if (pending) status.setAttribute('data-state', 'loading');
      else status.removeAttribute('data-state');
      previous.disabled = busy || offset === 0 || !list.isConnected;
      next.disabled = busy || done || !list.isConnected;
      navigation.hidden = !list.isConnected || (done && !shown && !busy);
    };

    const clearView = () => {
      observedInvalidation = invalidationGeneration;
      offset = 0;
      shown = 0;
      done = false;
      list.replaceChildren();
      paintStatus('');
    };

    const syncScope = () => {
      const nextKey = positionKey(host, options, 'server');
      const invalidated = observedInvalidation !== invalidationGeneration;
      if (nextKey === cacheKey && !invalidated) return false;
      cacheKey = nextKey;
      observedInvalidation = invalidationGeneration;
      const restoredOffset = offsetFor(readPosition(cacheKey));
      clearView();
      offset = restoredOffset === null ? 0 : restoredOffset;
      return true;
    };

    async function loadPage(targetPage, scrollAfter) {
      if (busy || !list.isConnected) return;
      let pageIndex = normalizePage(targetPage);
      let requestOffset = offsetFor(pageIndex);
      if (requestOffset === null) {
        pageIndex = 0;
        requestOffset = 0;
      }

      const token = epoch;
      const requestKey = cacheKey;
      const requestGeneration = invalidationGeneration;
      const requestController = new AbortController();
      controller = requestController;
      activeControllers.add(requestController);
      busy = true;

      let fallbackAvailable = true;
      let committed = false;
      let discard = false;
      let failureText = '';

      try {
        while (true) {
          paintStatus(shown
            ? 'Загружаю следующую страницу…'
            : 'Загружаю страницу…', {pending: true});

          const response = await fetchPage({
            limit: size,
            offset: requestOffset,
            signal: requestController.signal
          });

          const stillCurrent = token === epoch
            && requestGeneration === invalidationGeneration
            && positionKey(host, options, 'server') === requestKey;
          if (!stillCurrent) {
            discard = token === epoch;
            break;
          }
          if (!list.isConnected) break;

          const rows = Array.isArray(response) ? response : [];
          if (!rows.length && requestOffset !== 0 && fallbackAvailable) {
            fallbackAvailable = false;
            pageIndex = 0;
            requestOffset = 0;
            continue;
          }

          const fragment = document.createDocumentFragment();
          if (!rows.length) {
            const empty = document.createElement('p');
            empty.className = 'pager-empty completion-empty';
            empty.textContent = options.empty || 'Записей пока нет.';
            fragment.append(empty);
          } else {
            for (const [index, row] of rows.entries()) {
              const built = renderItem ? renderItem(row, requestOffset + index) : null;
              if (built) fragment.append(built);
            }
          }

          list.replaceChildren(fragment);
          offset = requestOffset;
          shown = rows.length;
          done = rows.length < size;
          writePosition(cacheKey, pageIndex);
          committed = true;
          break;
        }
      } catch (error) {
        if (token === epoch) {
          if (isAccessError(error)) invalidatePositions();
          const keyChanged = positionKey(host, options, 'server') !== requestKey;
          if (requestGeneration !== invalidationGeneration || keyChanged) {
            discard = true;
          } else if (!error || error.name !== 'AbortError') {
            failureText = error.message || 'Страница не загрузилась.';
          }
        }
      } finally {
        activeControllers.delete(requestController);
        if (controller === requestController) controller = null;
        if (token === epoch) busy = false;
      }

      if (token !== epoch) return;
      if (discard) {
        clearView();
        return;
      }
      if (failureText) {
        paintStatus(failureText);
        return;
      }
      if (!committed) return;

      paintStatus(shown
        ? 'Показано ' + (offset + 1) + '–' + (offset + shown)
          + (done
            ? ' · страница короче размера'
            : ' · следующая страница ещё не проверена')
        : '');
      if (scrollAfter) list.scrollIntoView({block: 'nearest'});
    }

    async function load() {
      if (busy || done) return;
      syncScope();
      const currentPage = offset / size;
      const targetPage = shown ? currentPage + 1 : currentPage;
      return loadPage(targetPage, false);
    }

    previous.addEventListener('click', () => {
      if (busy) return;
      syncScope();
      if (!shown || offset === 0) return;
      void loadPage(offset / size - 1, true);
    });

    next.addEventListener('click', () => {
      if (busy) return;
      syncScope();
      if (!shown || done) return;
      void loadPage(offset / size + 1, true);
    });

    return {
      element: list,
      navigation,
      status,
      load,
      reset() {
        syncScope();
        epoch++;
        const oldController = controller;
        controller = null;
        if (oldController) {
          activeControllers.delete(oldController);
          oldController.abort();
        }
        busy = false;
        clearView();
        writePosition(cacheKey, 0);
        return loadPage(0, false);
      },
      count: () => shown,
      hasMore: () => !done
    };
  };

  window.WisdomList = {create, createServer, DEFAULT_SIZE};
  bindInvalidation();
})();
