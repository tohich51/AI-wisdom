'use strict';
/*
 * Маршруты страниц. Хэш-адреса, потому что сервер отдаёт только «/» и /static,
 * а менять бэкенд в этой волне нельзя. Прямая ссылка, перезагрузка, «назад» и
 * «вперёд» работают штатно: состояние раздела живёт в адресе.
 *
 * Схема: #/<раздел>[/<параметр>][?запрос]
 *   #/libraries, #/libraries/<id>, #/uses, #/research?library=<id>,
 *   #/maps?library=<id>&mode=semantic, #/access, #/jobs
 */
(() => {
  const routes = new Map();
  const listeners = new Set();
  let fallback = 'libraries';
  let current = null;
  let generation = 0;
  let started = false;

  const parse = hash => {
    const value = String(hash || '').replace(/^#/, '');
    const [path, query = ''] = value.split('?');
    const parts = path.split('/').filter(part => part.length);
    return {name: parts[0] || fallback, params: parts.slice(1), query: new URLSearchParams(query)};
  };

  const build = (name, params = [], query = null) => {
    const path = ['#', name, ...params].join('/');
    if (!query) return path;
    const search = query instanceof URLSearchParams ? query : new URLSearchParams(query);
    const text = search.toString();
    return text ? path + '?' + text : path;
  };

  const setSectionVisibility = name => {
    for (const id of routes.keys()) {
      const section = document.getElementById(id + '-tab');
      if (section) section.hidden = id !== name;
    }
  };

  const setNavigationState = name => {
    for (const link of document.querySelectorAll('[data-route]')) {
      const active = link.dataset.route === name;
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
      link.classList.toggle('is-current', active);
    }
  };

  const focusHeading = () => {
    const section = routes.get(current?.name) ? document.getElementById(current.name + '-tab') : null;
    const heading = section?.querySelector('h1');
    if (!heading) return;
    if (!heading.hasAttribute('tabindex')) heading.setAttribute('tabindex', '-1');
    heading.focus({preventScroll: true});
  };

  async function render() {
    const parsed = parse(location.hash);
    const route = routes.get(parsed.name) || routes.get(fallback);
    if (!route) return;
    const previous = current;
    const token = ++generation;
    current = {name: route.name, params: parsed.params, query: parsed.query, token};
    setSectionVisibility(route.name);
    setNavigationState(route.name);
    if (route.title) document.title = route.title;
    if (previous && previous.name !== route.name && typeof routes.get(previous.name)?.leave === 'function') {
      try { routes.get(previous.name).leave(); } catch (_) { /* закрытый раздел не должен блокировать переход */ }
    }
    const container = document.getElementById(route.name + '-workspace') || document.getElementById(route.name + '-tab');
    for (const listener of listeners) {
      try { listener(current, previous); } catch (_) { /* наблюдатель не блокирует маршрут */ }
    }
    if (typeof route.mount !== 'function') return;
    try {
      await route.mount(container, {
        name: route.name,
        params: current.params,
        query: current.query,
        token,
        isCurrent: () => token === generation,
        path: build(route.name, current.params, current.query)
      });
    } catch (error) {
      if (token !== generation) return;
      for (const listener of listeners) {
        try { listener(current, previous, error); } catch (_) { /* ошибка маршрута уже видна в экране */ }
      }
    }
  }

  const navigate = (path, {replace = false} = {}) => {
    const value = String(path || '').trim();
    const [name, search = ''] = value.split('?');
    const target = value.startsWith('#') ? value : build(name.replace(/^\//, ''), search ? new URLSearchParams(search) : null);
    if (location.hash === target) { render(); return target; }
    if (replace) location.replace(target);
    else location.hash = target;
    return target;
  };

  const start = (options = {}) => {
    fallback = options.fallback || fallback;
    if (started) return;
    started = true;
    window.addEventListener('hashchange', () => { render(); });
    if (!location.hash || !routes.has(parse(location.hash).name)) {
      location.replace(build(fallback));
    } else {
      render();
    }
  };

  window.WisdomRouter = {
    register(name, route) { routes.set(name, {...route, name}); return window.WisdomRouter; },
    names: () => [...routes.keys()],
    path: build,
    navigate,
    start,
    refresh: render,
    current: () => current,
    onChange(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    focusHeading
  };
})();
