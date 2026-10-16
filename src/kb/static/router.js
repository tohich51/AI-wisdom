'use strict';
/*
 * Маршруты страниц по адресам /app/*.
 *
 * Контракт: адреса экранов живут под /app/*, и SPA fallback обслуживает только
 * /app/*, включая Reload. Сам fallback — это маршрут сервера, его готовит F,
 * поэтому здесь только клиентская половина: разбор location.pathname, переходы
 * через history.pushState/replaceState и обработчик popstate. Пути вне /app/*
 * этот роутер не переписывает: чужой адрес остаётся чужим, а документ, который
 * сервер отдал не под /app/*, просто показывает раздел по умолчанию.
 *
 * Схема адресов:
 *   /app/libraries, /app/libraries/<id>,
 *   /app/libraries/<id>/{sources|knowledge|rules},
 *   /app/projects/<id>/context, /app/research, /app/research/<runId>,
 *   /app/maps?library=<id>&view=semantic, /app/experience,
 *   /app/settings/access, /app/jobs
 *
 * Старые ссылки #/<раздел> продолжают открывать тот же раздел: они один раз
 * приводятся к /app/... через replaceState, поэтому в адресной строке всегда
 * остаётся адрес контракта, а не хэш.
 */
(() => {
  const APP = '/app';
  const routes = new Map();
  const listeners = new Set();
  let fallback = 'libraries';
  let current = null;
  let generation = 0;
  let started = false;

  // Корень адреса контракта для каждого раздела. Всё, что раздел строит сам,
  // добавляется к этому корню.
  const ROOT = {
    libraries: APP + '/libraries',
    context: APP + '/projects',
    research: APP + '/research',
    maps: APP + '/maps',
    uses: APP + '/experience',
    access: APP + '/settings/access',
    jobs: APP + '/jobs'
  };

  // Порядок важен: /app/libraries должен провериться раньше /app/libraries/<id>.
  const PATTERNS = [
    {name: 'libraries', re: new RegExp('^' + APP + '/libraries$')},
    {name: 'libraries', re: new RegExp('^' + APP + '/libraries/([^/]+)$')},
    {name: 'libraries', re: new RegExp('^' + APP + '/libraries/([^/]+)/(sources|knowledge|rules)$')},
    {name: 'context', re: new RegExp('^' + APP + '/projects/([^/]+)/context$')},
    {name: 'research', re: new RegExp('^' + APP + '/research$')},
    {name: 'research', re: new RegExp('^' + APP + '/research/([^/]+)$')},
    {name: 'maps', re: new RegExp('^' + APP + '/maps$')},
    {name: 'uses', re: new RegExp('^' + APP + '/experience$')},
    {name: 'access', re: new RegExp('^' + APP + '/settings/access$')},
    {name: 'jobs', re: new RegExp('^' + APP + '/jobs$')}
  ];

  // Прежние имена разделов в адресах и в хэше. Раздел не переименовывается —
  // меняется только его адрес, поэтому старые ссылки продолжают работать.
  const ALIAS = {
    libraries: 'libraries', library: 'libraries', libraries_: 'libraries',
    uses: 'uses', experience: 'uses',
    research: 'research', runs: 'research',
    maps: 'maps', map: 'maps',
    access: 'access', settings: 'access',
    jobs: 'jobs', job: 'jobs',
    context: 'context', projects: 'context', project: 'context'
  };

  const splitQuery = value => {
    const text = String(value == null ? '' : value);
    const at = text.indexOf('?');
    return at < 0 ? [text, ''] : [text.slice(0, at), text.slice(at + 1)];
  };

  const build = (name, params = [], query = null) => {
    const parts = (Array.isArray(params) ? params : [params])
      .filter(part => part !== undefined && part !== null && part !== '');
    const root = ROOT[name] || (APP + '/' + name);
    let path = root;
    if (name === 'context') path = root + '/' + encodeURIComponent(String(parts[0] || '')) + '/context';
    else for (const part of parts) path += '/' + encodeURIComponent(String(part));
    const search = query instanceof URLSearchParams ? query : new URLSearchParams(query || {});
    const text = search.toString();
    return text ? path + '?' + text : path;
  };

  // Любой адрес, который зовёт navigate, приводится к адресу контракта:
  // /app/... остаётся как есть, #/uses и /uses становятся /app/experience,
  // голое имя раздела собирается через build.
  const toPath = value => {
    const raw = String(value == null ? '' : value).trim();
    if (!raw) return build(fallback);
    const [path, query] = splitQuery(raw.startsWith('#') ? raw.replace(/^#/, '') : raw);
    if (path === APP || path.startsWith(APP + '/')) return query ? path + '?' + query : path;
    const parts = path.split('/').filter(part => part.length);
    const head = ALIAS[parts[0]];
    if (!head) return build(fallback);
    return build(head, parts.slice(1), query ? new URLSearchParams(query) : null);
  };

  const match = (path, search) => {
    for (const pattern of PATTERNS) {
      const found = pattern.re.exec(path);
      if (!found) continue;
      return {
        name: pattern.name,
        params: found.slice(1).map(part => decodeURIComponent(part)),
        query: new URLSearchParams(search || '')
      };
    }
    return null;
  };

  // Старый хэш приводится к адресу контракта один раз, при загрузке.
  const fromHash = () => {
    const hash = String(location.hash || '');
    if (hash.length < 2) return null;
    const head = hash.replace(/^#\/?/, '').split('/')[0];
    return head && ALIAS[head] ? toPath(hash) : null;
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

  // Разбор текущего адреса. Возвращает раздел, параметры и запрос, а также
  // адрес, на который документ нужно привести, если он не под /app/*.
  const home = () => {
    const path = build(fallback);
    const [pathOnly, query] = splitQuery(path);
    const found = match(pathOnly, query);
    return found ? {...found, rewrite: path} : null;
  };

  const resolve = () => {
    const path = location.pathname || '/';
    const search = location.search || '';
    const direct = match(path, search);
    if (direct) return {...direct, rewrite: null};
    // Корневой документ («/») или неизвестный адрес внутри /app/* сводятся к
    // разделу по умолчанию. Адрес вне /app/* и не «/» не переписывается: это
    // может быть служебный путь сервера, и рутер не имеет права его подменять.
    if (path === '/' || path.startsWith(APP + '/')) {
      if (path === '/') {
        const legacy = fromHash();
        if (legacy) {
          const [legacyPath, legacyQuery] = splitQuery(legacy);
          const found = match(legacyPath, legacyQuery);
          if (found) return {...found, rewrite: legacy};
        }
      }
      return home();
    }
    const fallbackRoute = home();
    return fallbackRoute ? {...fallbackRoute, rewrite: null} : null;
  };

  async function render() {
    const resolved = resolve();
    if (!resolved) return;
    const route = routes.get(resolved.name);
    if (!route) return;
    if (resolved.rewrite) history.replaceState(history.state, '', resolved.rewrite);
    const previous = current;
    const token = ++generation;
    current = {name: route.name, params: resolved.params, query: resolved.query, token};
    setSectionVisibility(route.name);
    setNavigationState(route.name);
    if (route.title) document.title = route.title;
    if (previous && previous.name !== route.name && typeof routes.get(previous.name)?.leave === 'function') {
      try { routes.get(previous.name).leave(); } catch (_) { /* закрытый раздел не должен блокировать переход */ }
    }
    for (const listener of listeners) {
      try { listener(current, previous); } catch (_) { /* наблюдатель не блокирует маршрут */ }
    }
    if (typeof route.mount !== 'function') return;
    const container = document.getElementById(route.name + '-workspace') || document.getElementById(route.name + '-tab');
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

  const navigate = (target, {replace = false} = {}) => {
    const path = toPath(target);
    if ((location.pathname + location.search) === path) { render(); return path; }
    if (replace) history.replaceState(history.state, '', path);
    else history.pushState(history.state, '', path);
    render();
    return path;
  };

  // Ссылки внутри приложения меняют адрес через history и не перезагружают
  // документ: переход обрабатывает render(). Внешние ссылки и ссылки с
  // модификаторами не перехватываются.
  const onClick = event => {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target && typeof event.target.closest === 'function' ? event.target.closest('a[href]') : null;
    if (!link || link.target || link.hasAttribute('download')) return;
    const href = link.getAttribute('href') || '';
    if (!href.startsWith(APP + '/') && href !== APP) return;
    event.preventDefault();
    navigate(href);
  };

  const start = (options = {}) => {
    fallback = options.fallback || fallback;
    if (started) return;
    started = true;
    window.addEventListener('popstate', () => { render(); });
    document.addEventListener('click', onClick);
    render();
  };

  window.WisdomRouter = {
    register(name, route) { routes.set(name, {...route, name}); return window.WisdomRouter; },
    names: () => [...routes.keys()],
    path: build,
    toPath,
    navigate,
    start,
    refresh: render,
    current: () => current,
    onChange(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    focusHeading
  };
})();
