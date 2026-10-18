'use strict';
/* Owner: общий клиентский слой форм; подключается платформой до функциональных скриптов. */
(() => {
  let nodeFactory = null;
  let serial = 0;
  const MAX_DRAFTS = 20;
  const MAX_DRAFT_TEXT = 128 * 1024;
  const MAX_FIELD_TEXT = 32 * 1024;
  const states = new WeakMap();
  const validatedSubmits = new WeakSet();
  const create = (tag, text, attrs = {}) => {
    if (nodeFactory) return nodeFactory(tag, text, attrs);
    const out = document.createElement(tag);
    if (text != null) out.textContent = String(text);
    for (const [key, value] of Object.entries(attrs)) {
      if (value != null) out.setAttribute(key, String(value));
    }
    return out;
  };
  const controlsFor = form => Array.from(form.querySelectorAll('input, select, textarea'))
    .filter(control => control.willValidate && !control.disabled);
  const labelFor = control => {
    const label = Array.from(control.labels || []).map(item => String(item.textContent || '').trim()).find(Boolean);
    return label || control.getAttribute('aria-label') || control.name || 'Поле';
  };
  const messageFor = control => {
    const validity = control.validity;
    if (validity.valid) return '';
    if (validity.valueMissing) {
      if (control.type === 'file') return 'Выберите файл.';
      if (control.type === 'checkbox' || control.type === 'radio') return 'Подтвердите выбор.';
      if (control instanceof HTMLSelectElement) return 'Выберите значение.';
      return 'Введите значение.';
    }
    if (validity.typeMismatch) {
      if (control.type === 'email') return 'Введите адрес электронной почты.';
      if (control.type === 'url') return 'Введите полный URL-адрес.';
      if (control.type === 'tel') return 'Введите телефон в допустимом формате.';
      if (control.type === 'number' || control.type === 'range') return 'Введите число.';
      return 'Введите значение в допустимом формате.';
    }
    if (validity.rangeUnderflow) return 'Минимальное допустимое значение: ' + (control.min || 'не указано') + '.';
    if (validity.rangeOverflow) return 'Максимальное допустимое значение: ' + (control.max || 'не указано') + '.';
    if (validity.stepMismatch) return 'Укажите значение с шагом ' + (control.step || '1') + '.';
    if (validity.tooShort) return 'Введите не менее ' + control.minLength + ' символов.';
    if (validity.tooLong) return 'Введите не более ' + control.maxLength + ' символов.';
    if (validity.patternMismatch) return 'Значение не соответствует требуемому формату.';
    if (validity.badInput) return 'Введите корректное значение.';
    if (validity.customError) return 'Значение не прошло проверку поля.';
    return 'Проверьте значение поля.';
  };
  const clearFieldError = (state, control) => {
    const entry = state.fieldErrors.get(control);
    if (!entry) return;
    entry.node.remove();
    if (entry.originalDescribed === null) control.removeAttribute('aria-describedby');
    else control.setAttribute('aria-describedby', entry.originalDescribed);
    if (entry.originalInvalid === null) control.removeAttribute('aria-invalid');
    else control.setAttribute('aria-invalid', entry.originalInvalid);
    state.fieldErrors.delete(control);
  };
  const collectErrors = state => {
    const errors = [];
    const radioNames = new Set();
    for (const control of controlsFor(state.form)) {
      if (control.type === 'radio') {
        if (radioNames.has(control.name)) continue;
        radioNames.add(control.name);
      }
      const forced = state.forced.get(control);
      if (forced?.active) {
        let active = true;
        try { active = forced.active() !== false; } catch (_) { active = true; }
        if (!active) {
          state.forced.delete(control);
          continue;
        }
      }
      if (forced) errors.push({control, message: forced.message, label: labelFor(control)});
      else if (!control.checkValidity()) errors.push({control, message: messageFor(control), label: labelFor(control)});
    }
    return errors;
  };
  const render = (state, errors, focusFirst = false) => {
    for (const control of [...state.fieldErrors.keys()]) clearFieldError(state, control);
    if (!errors.length) {
      state.summary.replaceChildren();
      state.summary.hidden = true;
      state.summaryVisible = false;
      return errors;
    }
    const heading = create('h3', 'Проверьте форму');
    const intro = create('p', 'Исправьте ошибки в следующих полях:');
    const list = create('ul');
    for (const error of errors) {
      const control = error.control;
      const id = control.id || (control.id = 'wisdom-field-' + (++serial));
      const originalDescribed = control.getAttribute('aria-describedby');
      const originalInvalid = control.getAttribute('aria-invalid');
      const node = create('p', error.message, {id: id + '-error', class: 'form-field-error'});
      (control.closest('label') || control).insertAdjacentElement('afterend', node);
      const described = new Set(String(originalDescribed || '').split(/\s+/).filter(Boolean));
      described.add(id + '-error');
      control.setAttribute('aria-describedby', [...described].join(' '));
      control.setAttribute('aria-invalid', 'true');
      state.fieldErrors.set(control, {node, originalDescribed, originalInvalid});
      const link = create('a', error.label + ': ' + error.message, {href: '#' + id});
      link.addEventListener('click', event => {
        event.preventDefault();
        control.focus();
      });
      const item = create('li');
      item.append(link);
      list.append(item);
    }
    state.summary.replaceChildren(heading, intro, list);
    state.summary.hidden = false;
    state.summaryVisible = true;
    if (focusFirst && typeof errors[0].control.focus === 'function') errors[0].control.focus();
    return errors;
  };
  const enhance = form => {
    const existing = states.get(form);
    if (existing) return existing;
    const state = {
      form,
      summary: create('div', null, {class: 'form-error-summary', role: 'alert', tabindex: '-1'}),
      serverError: create('div', null, {class: 'form-server-error', role: 'alert', 'aria-atomic': 'true'}),
      fieldErrors: new Map(),
      forced: new Map(),
      draftEdits: new Map(),
      draftOversized: new Set(),
      lastDraftKey: null,
      recovery: null,
      installed: false,
      pending: false,
      summaryVisible: false
    };
    state.summary.hidden = true;
    state.serverError.hidden = true;
    const submit = Array.from(form.children).find(child =>
      child instanceof HTMLElement &&
      child.matches('button[type="submit"], input[type="submit"], button:not([type])')
    );
    form.insertBefore(state.serverError, submit || null);
    form.insertBefore(state.summary, submit || null);
    form.noValidate = true;
    states.set(form, state);
    const corrected = () => {
      const visible = state.summaryVisible;
      for (const [control, forced] of [...state.forced]) {
        let active = true;
        try { active = typeof forced.active !== 'function' || forced.active() !== false; } catch (_) { active = true; }
        if (!active) {
          state.forced.delete(control);
          clearFieldError(state, control);
        }
      }
      for (const control of [...state.fieldErrors.keys()])
        if (!state.forced.has(control) && control.validity.valid) clearFieldError(state, control);
      if (visible) render(state, collectErrors(state), false);
    };
    form.addEventListener('input', event => {
      captureDraftEdit(state, event);
      corrected();
    });
    form.addEventListener('change', event => {
      captureDraftEdit(state, event);
      corrected();
    });
    form.addEventListener('reset', () => {
      queueMicrotask(() => discardDraft(form));
    });
    return state;
  };
  const enhanceTree = node => {
    if (!(node instanceof Element)) return;
    const forms = node instanceof HTMLFormElement ? [node] : [];
    forms.push(...node.querySelectorAll('form'));
    for (const form of forms) {
      if (!form.closest('.login')) enhance(form);
    }
  };
  const formObserver = new MutationObserver(records => {
    for (const record of records) {
      for (const node of record.addedNodes) enhanceTree(node);
    }
  });
  const observerRoot = document.body || document.documentElement;
  if (observerRoot) {
    enhanceTree(observerRoot);
    formObserver.observe(observerRoot, {childList: true, subtree: true});
  }
  const validate = form => {
    const state = enhance(form);
    const errors = collectErrors(state);
    return render(state, errors, errors.length > 0);
  };
  const install = (form, handler, options = {}) => {
    const state = enhance(form);
    if (state.installed) return;
    state.installed = true;
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const validationWasHandled = validatedSubmits.has(event);
      validatedSubmits.delete(event);
      if (state.pending) {
        event.stopImmediatePropagation();
        return;
      }
      state.serverError.replaceChildren();
      state.serverError.hidden = true;
      if (!validationWasHandled && validate(form).length) {
        event.stopImmediatePropagation();
        return;
      }
      const submitter = event.submitter instanceof HTMLElement
        ? event.submitter
        : form.querySelector('button[type="submit"], input[type="submit"], button:not([type])');
      const previousDisabled = submitter && 'disabled' in submitter ? Boolean(submitter.disabled) : null;
      const previousBusy = submitter?.getAttribute('aria-busy');
      snapshotState(state);
      const submittedDraftKey = currentDraftKey(form);
      state.pending = true;
      if (submitter) {
        submitter.setAttribute('aria-busy', 'true');
        if (previousDisabled !== null) submitter.disabled = true;
      }
      try {
        await handler.call(form, event);
        if (submittedDraftKey && !form.isConnected && !draftsSuspended) discardDraft(form, submittedDraftKey);
      } catch (error) {
        if (error?.name === 'AbortError') return;
        try {
          if (typeof options.onError === 'function') await options.onError.call(form, error, event);
          else {
            const text = typeof error?.message === 'string' && error.message.trim()
              ? error.message
              : typeof error === 'string' && error.trim()
                ? error
                : 'Действие не подтверждено. Проверьте состояние объекта и повторите попытку.';
            formError(form, text);
          }
        } catch (secondary) {
          if (secondary?.name !== 'AbortError') formError(form, secondary?.message || 'Действие не подтверждено. Проверьте состояние объекта.');
        }
      } finally {
        state.pending = false;
        if (submitter) {
          if (previousDisabled !== null) submitter.disabled = previousDisabled;
          if (previousBusy === null) submitter.removeAttribute('aria-busy');
          else submitter.setAttribute('aria-busy', previousBusy);
        }
      }
    }, true);
  };
  const showErrors = (form, entries) => {
    const state = enhance(form);
    for (const entry of entries || []) {
      if (!entry?.control) continue;
      state.forced.set(entry.control, {message: entry.message || 'Проверьте значение поля.', active: entry.active});
    }
    render(state, collectErrors(state), true);
  };
  const clearErrors = (form, controls = null) => {
    const state = enhance(form);
    const requested = controls ? new Set(controls) : new Set([...state.forced.keys(), ...state.fieldErrors.keys()]);
    for (const control of requested) {
      state.forced.delete(control);
      clearFieldError(state, control);
    }
    const remaining = controls
      ? collectErrors(state).filter(error => !requested.has(error.control) || !error.control.validity.valid)
      : [];
    render(state, remaining, false);
  };
  function formError(form, text) {
    const state = enhance(form);
    state.serverError.textContent = typeof text === 'string' && text.trim() ? text : 'Действие не подтверждено. Проверьте состояние объекта и повторите попытку.';
    state.serverError.hidden = false;
  }
  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.closest('.login')) return;
    const state = enhance(form);
    if (state.pending) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    if (validate(form).length) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    if (state.installed) validatedSubmits.add(event);
  }, true);
  const draftInputTypes = new Set(['text', 'search', 'email', 'url', 'tel', 'number']);
  const sensitiveField = /pass(word)?|secret|token|csrf|cookie|session|credential|authorization|api[-_]?key|private[-_]?key|otp|парол|секрет|токен/i;
  const allFormControls = form => Array.from(form.querySelectorAll('input, select, textarea'));
  const controlKind = control => control instanceof HTMLSelectElement
    ? (control.multiple ? 'select-multiple' : 'select-one')
    : control instanceof HTMLTextAreaElement ? 'textarea' : control.type;
  const draftShape = control => {
    let eligible = control instanceof HTMLTextAreaElement ||
      (control instanceof HTMLInputElement && draftInputTypes.has(control.type)) ||
      (control instanceof HTMLSelectElement && !control.multiple);
    if (!eligible) return false;
    const marker = [
      control.getAttribute('name'),
      control.id,
      control.getAttribute('autocomplete'),
      control.getAttribute('aria-label')
    ].join(' ');
    return !sensitiveField.test(marker);
  };
  const draftControl = control => draftShape(control) && !control.disabled && !control.readOnly;
  const draftControlsFor = form => allFormControls(form).filter(draftShape);
  const fieldName = control => String(control.getAttribute('name') || labelFor(control)).trim().slice(0, 160);
  const fieldKey = (form, control) => {
    const same = draftControlsFor(form).filter(item =>
      fieldName(item) === fieldName(control) && controlKind(item) === controlKind(control)
    );
    return JSON.stringify([fieldName(control), controlKind(control), same.indexOf(control)]);
  };
  const normalizedText = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 240);
  const titleFor = form => {
    const scope = form.closest('dialog, [role="dialog"]') || form.parentElement || form;
    const ownLabel = form.getAttribute('aria-label');
    if (ownLabel) return normalizedText(ownLabel);
    const labelledBy = form.getAttribute('aria-labelledby');
    if (labelledBy) {
      const text = labelledBy.split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ');
      if (normalizedText(text)) return normalizedText(text);
    }
    const heading = scope.querySelector('legend, h1, h2, h3, h4, [role="heading"]');
    return normalizedText(heading?.textContent || '');
  };
  const formSignature = form => {
    const draftKey = String(form.dataset.draftKey || '').trim().slice(0, 512);
    if (!draftKey) return '';
    return JSON.stringify([
      titleFor(form), draftKey,
      draftControlsFor(form).map(control => [fieldName(control), controlKind(control)])
    ]).slice(0, 2048);
  };

  const drafts = new Map();
  const draftWeights = new Map();
  let totalDraftText = 0;
  let contextProvider = null;
  let draftsSuspended = false;
  const requiredIdentityPart = value => value !== null && value !== undefined && String(value).length > 0;
  const contextNow = () => {
    if (draftsSuspended || typeof contextProvider !== 'function') return null;
    let value;
    try { value = contextProvider(); } catch (_) { return null; }
    if (!value || !requiredIdentityPart(value.accountId) || !requiredIdentityPart(value.principalId)
        || !requiredIdentityPart(value.routeName) || !Array.isArray(value.params)) return null;
    return {
      accountId: String(value.accountId),
      principalId: String(value.principalId),
      routeName: String(value.routeName).slice(0, 64),
      params: value.params.map(part => String(part)).slice(0, 32),
      query: String(value.query || '').slice(0, 2048),
      route: String(value.route || '').split(/[?#]/)[0].slice(0, 1024),
      libraryId: value.libraryId === null || value.libraryId === undefined ? '' : String(value.libraryId).slice(0, 256),
      projectId: value.projectId === null || value.projectId === undefined ? '' : String(value.projectId).slice(0, 256),
      authorized: value.authorized === true
    };
  };
  const draftRecordKey = (context, signature) => JSON.stringify([
    context.accountId,
    context.principalId,
    context.routeName,
    context.params,
    context.query,
    context.route,
    context.libraryId,
    context.projectId,
    signature
  ]);
  const currentDraftKey = form => {
    const context = contextNow();
    const signature = formSignature(form);
    return context && signature ? draftRecordKey(context, signature) : null;
  };
  const removeDraft = key => {
    if (!drafts.has(key)) return;
    drafts.delete(key);
    totalDraftText -= draftWeights.get(key) || 0;
    draftWeights.delete(key);
    if (totalDraftText < 0) totalDraftText = 0;
  };
  const connectedStates = () => Array.from(document.querySelectorAll('form')).map(form => states.get(form)).filter(Boolean);
  const matchingDraft = (context, signature) => signature ? drafts.get(draftRecordKey(context, signature)) : null;
  const hideRecovery = state => { if (state.recovery) state.recovery.root.hidden = true; };
  const syncRecovery = state => {
    const context = contextNow();
    const record = context?.authorized ? matchingDraft(context, formSignature(state.form)) : null;
    if (!record || state.draftEdits.size) { hideRecovery(state); return; }
    if (!state.recovery) {
      const root = create('div', null, {class: 'form-draft-recovery'});
      const text = create('p');
      const action = create('button', 'Восстановить введённый текст', {
        type: 'button',
        'data-wisdom-restore-draft': 'true'
      });
      root.append(text, action);
      const submit = Array.from(state.form.children).find(child =>
        child instanceof HTMLElement && child.matches('button[type="submit"], input[type="submit"], button:not([type])')
      );
      state.form.insertBefore(root, submit || null);
      state.recovery = {root, text, action};
    }
    state.recovery.text.textContent = 'Восстановление заменит значения полей. Проверьте их перед сохранением. Текст хранится только в памяти этой вкладки; он не переживает закрытие, перезагрузку или аварию браузера.';
    state.recovery.root.hidden = false;
  };
  const syncRecoveryAll = () => { for (const state of connectedStates()) syncRecovery(state); };
  const snapshotState = state => {
    const context = contextNow();
    const signature = formSignature(state.form);
    if (!context || !signature) return;
    const key = draftRecordKey(context, signature);
    if (!state.draftEdits.size || state.draftOversized.size) {
      removeDraft(key);
      state.lastDraftKey = null;
      syncRecoveryAll();
      return;
    }
    const values = Array.from(state.draftEdits, ([field, value]) => ({field, value}));
    const weight = (signature.length + JSON.stringify(values).length) * 2;
    if (weight > MAX_DRAFT_TEXT) {
      removeDraft(key);
      state.lastDraftKey = null;
      syncRecoveryAll();
      return;
    }
    removeDraft(key);
    const record = {
      accountId: context.accountId,
      principalId: context.principalId,
      routeName: context.routeName,
      params: context.params,
      query: context.query,
      route: context.route,
      libraryId: context.libraryId,
      projectId: context.projectId,
      signature,
      values
    };
    drafts.set(key, record);
    draftWeights.set(key, weight);
    state.lastDraftKey = key;
    totalDraftText += weight;
    while (drafts.size > MAX_DRAFTS || totalDraftText > MAX_DRAFT_TEXT) {
      const oldest = drafts.keys().next().value;
      if (oldest === undefined) break;
      removeDraft(oldest);
    }
    syncRecoveryAll();
  };
  const captureDraftEdit = (state, event) => {
    const control = event.target;
    if (!draftControl(control) || !state.form.contains(control)) return;
    const key = fieldKey(state.form, control);
    const value = String(control.value || '');
    if (value.length > MAX_FIELD_TEXT) {
      state.draftEdits.delete(key);
      state.draftOversized.add(key);
    } else {
      state.draftEdits.set(key, value);
      state.draftOversized.delete(key);
    }
    snapshotState(state);
  };
  const clearEditedValues = state => {
    const controls = draftControlsFor(state.form);
    for (const key of state.draftEdits.keys()) {
      const control = controls.find(item => fieldKey(state.form, item) === key);
      if (!control) continue;
      control.value = '';
      if (control instanceof HTMLSelectElement && !control.value) control.selectedIndex = -1;
    }
    state.draftEdits.clear();
    state.draftOversized.clear();
    state.serverError.replaceChildren();
    state.serverError.hidden = true;
    for (const [control] of [...state.fieldErrors]) clearFieldError(state, control);
    state.summary.replaceChildren();
    state.summary.hidden = true;
    state.summaryVisible = false;
  };
  const discardDraft = (form, forcedKey = null) => {
    const state = states.get(form);
    if (!state) return;
    if (draftsSuspended && !forcedKey) {
      clearEditedValues(state);
      hideRecovery(state);
      return;
    }
    const keys = new Set([currentDraftKey(form), state.lastDraftKey, forcedKey].filter(Boolean));
    for (const key of keys) removeDraft(key);
    state.draftEdits.clear();
    state.draftOversized.clear();
    state.lastDraftKey = null;
    hideRecovery(state);
  };
  const restoreDraft = form => {
    const context = contextNow();
    const signature = formSignature(form);
    if (!context?.authorized || !signature) return;
    const key = draftRecordKey(context, signature);
    const record = drafts.get(key);
    if (!record) { syncRecovery(stateOf(form)); return; }
    const controls = draftControlsFor(form).filter(control => !control.disabled && !control.readOnly);
    let complete = true;
    const assignments = [];
    for (const saved of record.values) {
      const control = controls.find(item => fieldKey(form, item) === saved.field);
      if (!control || (control instanceof HTMLSelectElement && !Array.from(control.options).some(option => option.value === saved.value))) {
        complete = false;
        continue;
      }
      assignments.push([control, saved.value]);
    }
    if (!complete) {
      formError(form, 'Набор полей изменился. Восстановление отменено; текст остаётся в памяти вкладки.');
      return;
    }
    for (const [control, value] of assignments) {
      control.value = value;
      control.dispatchEvent(new Event('input', {bubbles: true}));
      control.dispatchEvent(new Event('change', {bubbles: true}));
    }
    removeDraft(key);
    const state = stateOf(form);
    state.lastDraftKey = null;
    hideRecovery(state);
  };
  const stateOf = form => enhance(form);
  const snapshotAll = () => { for (const state of connectedStates()) snapshotState(state); };
  const suspend = () => {
    draftsSuspended = true;
    for (const state of connectedStates()) {
      clearEditedValues(state);
      hideRecovery(state);
    }
  };
  const resume = () => {
    if (typeof contextProvider !== 'function') return;
    draftsSuspended = false;
    syncRecoveryAll();
  };
  const clearDrafts = () => {
    drafts.clear();
    draftWeights.clear();
    totalDraftText = 0;
    suspend();
    for (const state of connectedStates()) state.lastDraftKey = null;
  };
  document.addEventListener('click', event => {
    const target = event.target instanceof Element ? event.target : null;
    const action = target?.closest('button[data-wisdom-restore-draft]');
    const form = action?.closest('form');
    if (form instanceof HTMLFormElement && document.contains(form)) restoreDraft(form);
  });

  window.WisdomForms = {
    configure(factory) { if (typeof factory === 'function') nodeFactory = factory; },
    configureContext(provider) {
      contextProvider = typeof provider === 'function' ? provider : null;
      draftsSuspended = !contextProvider;
      syncRecoveryAll();
    },
    snapshotAll,
    suspend,
    resume,
    clearDrafts,
    refreshRecovery: syncRecoveryAll,
    discardDraft,
    enhance,
    validate,
    submit: install,
    showErrors,
    clearErrors,
    formError
  };
})();
