'use strict';
/*
 * Постраничные списки, по 50 записей на страницу.
 *
 * Два режима с одной оболочкой (список, кнопки, строка состояния):
 *
 *   createServer — список, который просит у сервера страницы limit/offset и
 *     дописывает их целиком. Короткая страница (короче limit) закрывает список.
 *     Точные totals не показываются: контракт оставляет ответы массивами и не
 *     добавляет поле с общим числом записей, поэтому UI не знает и не
 *     показывает, сколько записей есть всего. Выборки на клиенте нет — сервер
 *     отдаёт страницу, UI её рисует.
 *
 *   create — список, который уже пришёл целиком (задания, участники, группы,
 *     узлы карты, опубликованный опыт): те же 50 записей на страницу, но
 *     страницы нарезаются из уже полученного массива, потому что эти эндпоинты
 *     по контракту limit/offset не принимают.
 */
(() => {
  const DEFAULT_SIZE = 50;

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
    const size = Math.max(1, Number(options.size) || DEFAULT_SIZE);
    const renderItem = options.renderItem;
    const {list, navigation, status} = shell(options);
    const previous = document.createElement('button');
    previous.type = 'button';
    previous.textContent = 'Предыдущая страница';
    const next = document.createElement('button');
    next.type = 'button';
    next.textContent = 'Следующая страница';
    let items = [];
    let page = 1;

    const pages = () => Math.max(1, Math.ceil(items.length / size));
    const clamp = () => { page = Math.min(Math.max(1, page), pages()); };

    function paint() {
      clamp();
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
        ? 'Показано ' + (items.length ? from + 1 : 0) + '–' + Math.min(from + size, items.length) + ' из ' + items.length
        : '';
      status.hidden = !items.length;
      previous.disabled = page <= 1;
      next.disabled = page >= total;
    }

    previous.addEventListener('click', () => { page--; paint(); list.scrollIntoView({block: 'nearest'}); });
    next.addEventListener('click', () => { page++; paint(); list.scrollIntoView({block: 'nearest'}); });

    navigation.append(previous, next);
    return {
      element: list,
      navigation,
      status,
      set(nextItems) { items = Array.isArray(nextItems) ? nextItems : []; page = 1; paint(); return this; },
      reset() { page = 1; paint(); return this; },
      count: () => items.length
    };
  };

  const createServer = (host, options = {}) => {
    const size = Math.max(1, Number(options.size) || DEFAULT_SIZE);
    const fetchPage = options.fetchPage;
    const renderItem = options.renderItem;
    if (typeof fetchPage !== 'function') {
      throw new Error('Для серверного списка нужен fetchPage({limit, offset, signal}).');
    }
    const {list, navigation, status} = shell(options);
    const more = document.createElement('button');
    more.type = 'button';
    more.textContent = 'Следующая страница';
    navigation.append(more);
    let offset = 0;
    let shown = 0;
    let done = false;
    let busy = false;
    let epoch = 0;
    let controller = null;

    // Поздняя страница не должна перерисовать уже закрытый экран: пока элемент
    // списка в документе, ответ принадлежит текущему экрану; после detach —
    // нет. epoch гасит ответ, пришедший после reset.
    const alive = token => token === epoch && list.isConnected;

    const paintStatus = (text, {pending = false} = {}) => {
      status.textContent = text;
      status.hidden = !text;
      if (pending) status.setAttribute('data-state', 'loading');
      else status.removeAttribute('data-state');
      more.disabled = busy || done || !list.isConnected;
      navigation.hidden = done && !shown;
    };

    async function load() {
      if (busy || done) return;
      const token = epoch;
      busy = true;
      controller?.abort();
      controller = new AbortController();
      paintStatus(shown ? 'Загружаю следующую страницу…' : 'Загружаю страницу…', {pending: true});
      let page;
      try {
        page = await fetchPage({limit: size, offset, signal: controller.signal});
      } catch (error) {
        if (token !== epoch) return;
        busy = false;
        if (error && error.name === 'AbortError') return;
        paintStatus(error.message || 'Страница не загрузилась.');
        return;
      }
      if (!alive(token)) return;
      const items = Array.isArray(page) ? page : [];
      const emptyNode = list.querySelector('.pager-empty');
      if (!items.length && !shown) {
        list.replaceChildren();
        const empty = document.createElement('p');
        empty.className = 'pager-empty completion-empty';
        empty.textContent = options.empty || 'Записей пока нет.';
        list.append(empty);
      } else {
        if (emptyNode) emptyNode.remove();
        const fragment = document.createDocumentFragment();
        for (const item of items) {
          const built = renderItem ? renderItem(item, shown) : null;
          if (!built) continue;
          fragment.append(built);
          shown++;
        }
        list.append(fragment);
      }
      offset += items.length;
      // Короткая страница закрывает список: сервер не обещает за ней ещё
      // записи, а точного totals в ответе нет.
      if (items.length < size) done = true;
      busy = false;
      paintStatus(shown
        ? 'Показано записей: ' + shown + (done ? ' · список загружен целиком' : ' · есть следующая страница')
        : '');
    }

    more.addEventListener('click', () => { load(); });

    return {
      element: list,
      navigation,
      status,
      load,
      reset() {
        epoch++;
        controller?.abort();
        controller = null;
        offset = 0; shown = 0; done = false; busy = false;
        list.replaceChildren();
        return load();
      },
      count: () => shown,
      hasMore: () => !done
    };
  };

  window.WisdomList = {create, createServer, DEFAULT_SIZE};
})();
