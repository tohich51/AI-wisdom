'use strict';
/*
 * Постраничный список. Списки длинные (применения, задания, источники, знания,
 * правила, узлы карты), а сервер отдаёт их целиком и менять контракт нельзя,
 * поэтому страницы живут на клиенте: порядок, состав и обработчики каждого
 * элемента сохраняются, показывается по 25 записей.
 */
(() => {
  const DEFAULT_SIZE = 25;

  const create = (host, options = {}) => {
    const size = Math.max(1, Number(options.size) || DEFAULT_SIZE);
    const renderItem = options.renderItem;
    const list = document.createElement('div');
    list.className = 'pager-list';
    const navigation = document.createElement('nav');
    navigation.className = 'pager';
    navigation.setAttribute('aria-label', options.label || 'Страницы списка');
    const previous = document.createElement('button');
    previous.type = 'button';
    previous.textContent = 'Предыдущая страница';
    const next = document.createElement('button');
    next.type = 'button';
    next.textContent = 'Следующая страница';
    const status = document.createElement('p');
    status.className = 'pager-status muted';
    status.setAttribute('role', 'status');
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

  window.WisdomList = {create, DEFAULT_SIZE};
})();
