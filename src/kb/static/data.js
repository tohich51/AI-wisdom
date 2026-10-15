'use strict';
/*
 * Один загрузчик данных для всех экранов.
 *
 * Задача: сохранить поведение app.js (CSRF, 401/403, тексты ошибок, 204) и
 * добавить к нему кэш чтений и сброс кэша, чтобы список не перезапрашивался
 * при каждом переходе между разделами и всегда показывал свежее после записи.
 *
 * Правила кэша:
 *   * кэшируются только GET из CACHEABLE — ответы, которые раздел перечитывает;
 *   * одинаковые параллельные GET объединяются в один сетевой запрос;
 *   * любая успешная запись (POST/PUT/PATCH/DELETE) сбрасывает весь кэш;
 *     точность важнее экономии, а разделы перечитывают данные после каждой записи;
 *   * отмена (AbortSignal) отменяет только ожидание вызывающего, общий запрос
 *     доезжает до конца и попадает в кэш — «поздний ответ» не может изменить
 *     уже закрытый экран, потому что вызывающий получил AbortError раньше.
 */
(() => {
  const TTL = 30000;
  const CACHEABLE = [
    /^\/api\/me$/,
    /^\/readyz$/,
    /^\/api\/libraries$/,
    /^\/api\/libraries\/[^/]+\/(policy|sources|knowledge|rules|grants|experience)$/,
    /^\/api\/projects\/[^/]+\/context$/,
    /^\/api\/uses$/,
    /^\/api\/jobs$/,
    /^\/api\/experience-summary$/,
    /^\/api\/retrievals$/
  ];
  const cache = new Map();
  const inflight = new Map();
  const sinks = new Set();
  const stats = {hits: 0, misses: 0, shares: 0, invalidations: 0, requests: 0};
  const defaultSink = text => {
    const status = document.getElementById('status');
    if (status) status.textContent = text;
  };
  const emit = text => {
    if (!sinks.size) { defaultSink(text); return; }
    for (const sink of sinks) { try { sink(text); } catch (_) { /* отдельный экран не должен ломать сообщение */ } }
  };
  const pathname = path => String(path || '').split('?')[0];
  const keyFor = path => {
    const value = pathname(path);
    return CACHEABLE.some(pattern => pattern.test(value)) ? String(path) : null;
  };
  const scopeOf = path => {
    const value = pathname(path);
    const library = value.match(/^\/api\/libraries\/([^/]+)/);
    if (library) return 'library:' + decodeURIComponent(library[1]);
    const project = value.match(/^\/api\/projects\/([^/]+)/);
    if (project) return 'project:' + decodeURIComponent(project[1]);
    return 'root';
  };
  const invalidationHooks = new Set();

  function invalidate(reason) {
    if (!cache.size && !inflight.size) { stats.invalidations++; return; }
    cache.clear();
    stats.invalidations++;
    for (const hook of invalidationHooks) { try { hook(reason); } catch (_) { /* сброс кэша не зависит от экрана */ } }
  }

  function deny(status) {
    emit(status === 401 ? 'Войдите в свой аккаунт.' : 'Доступ к материалу отсутствует или отозван.');
    for (const hook of invalidationHooks) { try { hook('access'); } catch (_) { /* экран мог быть уже закрыт */ } }
    if (typeof window.WisdomResearch?.invalidate === 'function') window.WisdomResearch.invalidate();
    if (typeof window.WisdomMap?.invalidate === 'function') window.WisdomMap.invalidate();
    document.querySelectorAll('dialog[data-wisdom-private]').forEach(dialog => dialog.close());
    if (status === 401) {
      const login = document.querySelector('.login'); if (login) login.hidden = false;
      const logout = document.getElementById('logout'); if (logout) logout.hidden = true;
    }
  }

  async function fetchJson(path, options = {}) {
    const headers = new Headers(options.headers);
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
    const method = (options.method || 'GET').toUpperCase();
    if (options.method && method !== 'GET') {
      const cookie = document.cookie.split('; ').find(value => value.startsWith('kb_csrf='));
      if (cookie) headers.set('X-KB-CSRF', decodeURIComponent(cookie.slice(8)));
    }
    stats.requests++;
    const response = await fetch(path, {...options, method, headers, credentials: 'same-origin'});
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) { invalidate('denied'); deny(response.status); }
      if (response.status === 401) throw Error('Войдите в свой аккаунт через кнопку «Войти».');
      if (response.status === 403) throw Error('Для этого действия недостаточно прав.');
      if (response.status === 429) throw Error('Очередь обработки заполнена. Повторите позже; готовые материалы остаются доступны.');
      const data = await response.json().catch(() => ({}));
      throw Error(typeof data.detail === 'string' ? data.detail : 'Запрос не выполнен. Проверьте данные.');
    }
    return response.status === 204 ? null : response.json();
  }

  const withSignal = (promise, signal) => {
    if (!signal) return promise;
    if (signal.aborted) return Promise.reject(new DOMException('Отменено', 'AbortError'));
    return Promise.race([promise, new Promise((_, reject) => {
      const onAbort = () => reject(new DOMException('Отменено', 'AbortError'));
      signal.addEventListener('abort', onAbort, {once: true});
      const forget = () => signal.removeEventListener('abort', onAbort);
      promise.then(forget, forget);
    })]);
  };

  async function api(path, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const key = method === 'GET' && !options.noCache ? keyFor(path) : null;
    if (key !== null) {
      const hit = cache.get(key);
      if (hit && Date.now() - hit.at < TTL) { stats.hits++; return withSignal(Promise.resolve(hit.value), options.signal); }
      let shared = inflight.get(key);
      if (shared) stats.shares++;
      else {
        stats.misses++;
        shared = fetchJson(path, {headers: options.headers})
          .then(value => { cache.set(key, {value, at: Date.now(), scope: scopeOf(path)}); return value; })
          .finally(() => { inflight.delete(key); });
        inflight.set(key, shared);
      }
      return withSignal(shared, options.signal);
    }
    const value = await fetchJson(path, options);
    if (method !== 'GET' && method !== 'HEAD') invalidate(method + ' ' + pathname(path));
    return value;
  }

  const post = (path, data = {}) => api(path, {method: 'POST', body: JSON.stringify(data)});

  window.WisdomData = {
    api,
    post,
    invalidate,
    onMessage(sink) { sinks.add(sink); return () => sinks.delete(sink); },
    onInvalidate(hook) { invalidationHooks.add(hook); return () => invalidationHooks.delete(hook); },
    scopeOf,
    cache: {
      size: () => cache.size,
      keys: () => [...cache.keys()],
      has: key => cache.has(key),
      clear: () => invalidate('manual')
    },
    stats: () => ({...stats, cached: cache.size, inflight: inflight.size})
  };
})();
