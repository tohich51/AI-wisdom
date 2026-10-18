'use strict';
(() => {
  const mountIds = new WeakMap();
  const operationIds = new Map();
  const layers = ['brand', 'project', 'general'];
  const layerLabels = {brand: 'Бренд', project: 'Проект', general: 'Общий слой'};
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const hash = /^[0-9a-f]{64}$/i;

  const text = value => typeof value === 'string' && value.trim() ? value : null;
  const safeError = error => text(error?.message) || 'Запрос не выполнен. Проверьте подключение и права.';

  function element(host, tag, label, attrs = {}) {
    return host.node(tag, label, attrs);
  }

  function makeAction(host, label, callback) {
    return host.button(label, async event => {
      const control = event?.currentTarget;
      if (control) control.disabled = true;
      try { await callback(); }
      catch (error) { host.notify(safeError(error)); }
      finally { if (control) control.disabled = false; }
    });
  }

  function addField(host, form, label, name, type = 'text') {
    const id = 'project-context-' + name + '-' + crypto.randomUUID();
    form.append(element(host, 'label', label, {for: id}));
    const input = element(host, type === 'select' ? 'select' : 'input', null, {id, name});
    if (type !== 'select') input.type = type;
    form.append(input);
    return input;
  }

  function option(host, select, label, value = '') {
    select.append(element(host, 'option', label, {value}));
  }

  function setFormMessage(host, form, message) {
    let output = form.querySelector('[data-project-form-message]');
    if (!output) {
      output = element(host, 'p', null, {role: 'alert', class: 'completion-error', 'data-project-form-message': ''});
      form.prepend(output);
    }
    output.textContent = message;
  }

  function clearFormMessage(form) {
    form.querySelector('[data-project-form-message]')?.remove();
  }

  function setFormsEnabled(forms, enabled) {
    for (const form of forms) {
      for (const control of form.querySelectorAll('input, select, button')) {
        const fixedDisabled = control.dataset.projectDisabled === 'true';
        const needsRule = control.dataset.projectRuleControl === 'true'
          && control.dataset.projectRuleAvailable !== 'true';
        control.disabled = !enabled || fixedDisabled || needsRule;
      }
    }
  }

  function requiredLabel(item) {
    if (item?.is_required === true) return 'Обязательно';
    if (item?.is_required === false) return 'Необязательно';
    return 'Обязательность не указана';
  }

  function visiblePins(pins) {
    return pins.map((pin, index) => ({pin, index})).sort((a, b) => {
      const aLayer = layers.indexOf(a.pin.layer);
      const bLayer = layers.indexOf(b.pin.layer);
      const aRank = aLayer < 0 ? layers.length : aLayer;
      const bRank = bLayer < 0 ? layers.length : bLayer;
      if (aRank !== bRank) return aRank - bRank;
      if (aRank < layers.length && a.pin.priority !== b.pin.priority) return (b.pin.priority ?? 0) - (a.pin.priority ?? 0);
      return a.index - b.index;
    }).map(item => item.pin);
  }

  async function mount(root, library, host, {canCurate = false} = {}) {
    const mountId = (mountIds.get(root) || 0) + 1;
    mountIds.set(root, mountId);
    const isCurrent = () => {
      const current = host.currentLibrary?.();
      return mountIds.get(root) === mountId && current?.id === library?.id;
    };
    const area = element(host, 'section', null, {class: 'completion-project-context'});
    const contextArea = element(host, 'div', null, {class: 'completion-grid', 'aria-live': 'polite'});
    const forms = [];
    let contextReady = false;

    root.querySelector('.completion-project-context')?.remove();
    root.append(area);
    if (!library || library.kind !== 'project' || !library.id) {
      contextArea.append(element(host, 'p', 'Раздел контекста доступен только для проекта.', {class: 'completion-empty'}));
      area.append(contextArea);
      return;
    }

    const heading = element(host, 'h2', 'Контекст проекта');
    area.append(heading, contextArea);
    const operationId = operation => {
      const key = library.id + ':' + operation;
      if (!operationIds.has(key)) operationIds.set(key, crypto.randomUUID());
      return operationIds.get(key);
    };

    if (canCurate) {
      const availableLibraries = (host.libraries?.() || []).filter(item => item?.id);
      const linkLibraries = availableLibraries.filter(item =>
        item.id !== library.id && item.kind !== 'project'
      );
      const pinLibraries = availableLibraries;
      const linkCard = element(host, 'article', null, {class: 'card completion-card'});
      linkCard.append(element(host, 'h3', 'Добавить библиотеку в проект'));
      const linkForm = element(host, 'form', null, {class: 'completion-form'});
      linkForm.dataset.draftKey = 'project:' + library.id + ':link:' + operationId('link');
      const linkLibrary = addField(host, linkForm, 'Библиотека', 'library_id', 'select');
      option(host, linkLibrary, 'Выберите библиотеку');
      for (const item of linkLibraries) {
        option(host, linkLibrary, text(item.name) || 'Библиотека без названия', item.id);
      }
      linkLibrary.required = true;
      const linkRequired = addField(host, linkForm, 'Сделать обязательной', 'is_required', 'checkbox');
      linkRequired.checked = true;
      const linkSubmit = element(host, 'button', 'Связать с проектом', {type: 'submit'});
      linkForm.append(linkSubmit);
      if (!linkLibraries.length) {
        linkCard.append(element(host, 'p', 'Нет доступных библиотек для связи.', {class: 'completion-empty'}));
        linkLibrary.dataset.projectDisabled = 'true';
        linkRequired.dataset.projectDisabled = 'true';
        linkSubmit.dataset.projectDisabled = 'true';
      }
      window.WisdomForms.submit(linkForm, async () => {
        if (!isCurrent()) return;
        linkSubmit.disabled = true; clearFormMessage(linkForm);
        try {
          await host.post('/api/projects/' + encodeURIComponent(library.id) + '/libraries', {
            library_id: linkLibrary.value, is_required: linkRequired.checked
          });
          if (!isCurrent()) return;
          linkForm.reset(); linkRequired.checked = true;
          await refreshContext();
          if (isCurrent()) host.notify('Связь с библиотекой сохранена.');
        } catch (error) {
          if (!isCurrent()) return;
          setFormMessage(host, linkForm, safeError(error));
          host.notify(safeError(error));
        } finally { if (isCurrent()) linkSubmit.disabled = false; }
      });
      linkCard.append(linkForm);
      area.append(linkCard);
      forms.push(linkForm);

      const pinCard = element(host, 'article', null, {class: 'card completion-card'});
      pinCard.append(element(host, 'h3', 'Закрепить версию правила'));
      const pinForm = element(host, 'form', null, {class: 'completion-form'});
      pinForm.dataset.draftKey = 'project:' + library.id + ':rule-pin:' + operationId('rule-pin');
      const sourceLibrary = addField(host, pinForm, 'Библиотека с правилом', 'source_library_id', 'select');
      option(host, sourceLibrary, 'Выберите библиотеку');
      for (const item of pinLibraries) option(host, sourceLibrary, text(item.name) || 'Библиотека без названия', item.id);
      sourceLibrary.required = true;
      const ruleSelect = addField(host, pinForm, 'Опубликованная версия', 'rule_version', 'select');
      option(host, ruleSelect, 'Сначала выберите библиотеку');
      ruleSelect.required = true; ruleSelect.dataset.projectRuleControl = 'true';
      const priority = addField(host, pinForm, 'Приоритет', 'priority', 'number');
      priority.step = '1'; priority.min = '-2147483648'; priority.max = '2147483647';
      priority.required = true; priority.value = '0';
      const pinRequired = addField(host, pinForm, 'Сделать обязательной', 'is_required', 'checkbox');
      pinRequired.checked = true;
      const pinSubmit = element(host, 'button', 'Закрепить точную версию', {type: 'submit'});
      pinSubmit.dataset.projectRuleControl = 'true';
      pinForm.append(pinSubmit);
      if (!pinLibraries.length) {
        pinCard.append(element(host, 'p', 'Нет доступных библиотек для выбора правила.', {class: 'completion-empty'}));
        sourceLibrary.dataset.projectDisabled = 'true';
        pinSubmit.dataset.projectDisabled = 'true';
      }
      const ruleOptions = new Map();
      let ruleLoad = 0;
      sourceLibrary.addEventListener('change', async () => {
        const selectedLibrary = sourceLibrary.value;
        const requestId = ++ruleLoad;
        ruleOptions.clear();
        ruleSelect.replaceChildren();
        option(host, ruleSelect, selectedLibrary ? 'Загружаю опубликованные версии…' : 'Сначала выберите библиотеку');
        ruleSelect.dataset.projectRuleAvailable = 'false';
        pinSubmit.dataset.projectRuleAvailable = 'false';
        setFormsEnabled(forms, contextReady); clearFormMessage(pinForm);
        if (!selectedLibrary || !isCurrent()) return;
        try {
          const rules = await host.api('/api/libraries/' + encodeURIComponent(selectedLibrary) + '/rules');
          if (!isCurrent() || requestId !== ruleLoad || sourceLibrary.value !== selectedLibrary) return;
          ruleSelect.replaceChildren(); option(host, ruleSelect, 'Выберите точную версию');
          if (!Array.isArray(rules)) throw Error('Список правил вернулся в неизвестном формате.');
          for (const rule of rules) {
            if (rule?.publication !== 'published' || !uuid.test(rule.id || '')
                || !Number.isInteger(rule.version_no) || rule.version_no < 1) continue;
            const key = rule.id + ':' + rule.version_no;
            ruleOptions.set(key, rule);
            option(host, ruleSelect, (text(rule.title) || 'Правило без названия') + ' · v' + rule.version_no, key);
          }
          if (!ruleOptions.size) option(host, ruleSelect, 'Опубликованных версий нет');
          ruleSelect.dataset.projectRuleAvailable = String(ruleOptions.size > 0);
          pinSubmit.dataset.projectRuleAvailable = String(ruleOptions.size > 0);
          setFormsEnabled(forms, contextReady);
        } catch (error) {
          if (!isCurrent() || requestId !== ruleLoad) return;
          option(host, ruleSelect, 'Не удалось загрузить версии');
          ruleSelect.dataset.projectRuleAvailable = 'false';
          pinSubmit.dataset.projectRuleAvailable = 'false';
          setFormsEnabled(forms, contextReady);
          setFormMessage(host, pinForm, safeError(error));
          host.notify(safeError(error));
        }
      });
      window.WisdomForms.submit(pinForm, async () => {
        if (!isCurrent()) return;
        const rule = ruleOptions.get(ruleSelect.value);
        if (!rule) { setFormMessage(host, pinForm, 'Выберите опубликованную версию правила.'); return; }
        const priorityValue = Number(priority.value);
        if (!Number.isInteger(priorityValue) || priorityValue < -2147483648 || priorityValue > 2147483647) {
          setFormMessage(host, pinForm, 'Укажите целый приоритет от −2147483648 до 2147483647.'); return;
        }
        pinSubmit.disabled = true; clearFormMessage(pinForm);
        try {
          await host.post('/api/projects/' + encodeURIComponent(library.id) + '/rules', {
            rule_id: rule.id, version_no: rule.version_no,
            is_required: pinRequired.checked, priority: priorityValue
          });
          if (!isCurrent()) return;
          pinForm.reset(); pinRequired.checked = true; priority.value = '0';
          sourceLibrary.value = ''; sourceLibrary.dispatchEvent(new Event('change'));
          await refreshContext();
          if (isCurrent()) host.notify('Точная версия правила закреплена.');
        } catch (error) {
          if (!isCurrent()) return;
          setFormMessage(host, pinForm, safeError(error));
          host.notify(safeError(error));
        } finally { if (isCurrent()) setFormsEnabled(forms, contextReady); }
      });
      pinCard.append(pinForm);
      area.append(pinCard);
      forms.push(pinForm);
    }

    setFormsEnabled(forms, false);

    function confirmRemoval(container, label, remove) {
      container.replaceChildren(element(host, 'p', label));
      const confirm = makeAction(host, 'Подтвердить удаление', async () => {
        if (!isCurrent()) return;
        await remove();
      });
      const cancel = makeAction(host, 'Отмена', async () => { container.replaceChildren(); });
      container.append(confirm, cancel);
    }

    function renderContext(context) {
      if (!isCurrent()) return;
      contextArea.replaceChildren();
      const conflicts = Array.isArray(context.conflicts) ? context.conflicts : [];
      const state = conflicts.length ? 'conflicted' : context.context_state;
      const stateLabels = {
        complete: 'Контекст доступен',
        blocked: 'Контекст заблокирован',
        conflicted: 'Есть конфликт закреплённых версий'
      };
      contextArea.append(element(host, 'p', stateLabels[state] || 'Статус контекста не распознан', {
        role: state === 'complete' ? 'status' : 'alert',
        class: state === 'complete' ? 'muted' : 'completion-error'
      }));
      if (text(context.description)) contextArea.append(element(host, 'p', context.description, {class: 'muted'}));
      if (Number.isInteger(context.blocked_link_count) || Number.isInteger(context.blocked_pin_count)) {
        contextArea.append(element(host, 'p', 'Недоступные связи: ' + (Number.isInteger(context.blocked_link_count) ? context.blocked_link_count : 'не указано')
          + ' · закрепления: ' + (Number.isInteger(context.blocked_pin_count) ? context.blocked_pin_count : 'не указано'), {class: 'muted'}));
      }
      for (const conflict of conflicts) {
        const message = conflict?.code === 'multiple_versions_pinned'
          ? 'Для одной линии закреплено несколько версий. Проверьте закрепления проекта.'
          : 'Контекст содержит предупреждение о конфликте версии (' + (text(conflict?.code) || 'код не указан') + ').';
        contextArea.append(element(host, 'p', message, {role: 'alert', class: 'completion-error'}));
      }

      const linksCard = element(host, 'article', null, {class: 'card completion-card'});
      linksCard.append(element(host, 'h3', 'Библиотеки проекта'));
      const links = Array.isArray(context.links) ? context.links : [];
      if (!links.length) linksCard.append(element(host, 'p', 'Связанных библиотек пока нет.', {class: 'completion-empty'}));
      for (const link of links) {
        const row = element(host, 'div', null, {class: 'completion-project-row'});
        if (link?.status === 'available' && link.library && link.library_id) {
          row.append(element(host, 'strong', text(link.library.name) || 'Библиотека без названия'));
          row.append(element(host, 'p', requiredLabel(link) + ' · ' + (text(link.library.kind) || 'Тип не указан'), {class: 'muted'}));
          if (canCurate && uuid.test(link.library_id)) {
            const confirmArea = element(host, 'div', null, {class: 'completion-form'});
            row.append(makeAction(host, 'Убрать связь', () => confirmRemoval(
              confirmArea, 'Связь будет удалена. Подтвердите действие.', async () => {
                await host.api('/api/projects/' + encodeURIComponent(library.id) + '/libraries/' + encodeURIComponent(link.library_id), {method: 'DELETE'});
                if (isCurrent()) await refreshContext();
              }
            )));
            row.append(confirmArea);
          }
        } else {
          row.append(element(host, 'strong', 'Библиотека недоступна'));
          row.append(element(host, 'p', requiredLabel(link) + ' · статус: ' + (link?.status === 'blocked' ? 'заблокирована' : 'неизвестен'), {class: 'muted'}));
        }
        linksCard.append(row);
      }
      contextArea.append(linksCard);

      const pinsCard = element(host, 'article', null, {class: 'card completion-card'});
      pinsCard.append(element(host, 'h3', 'Закреплённые версии правил'));
      const pins = visiblePins(Array.isArray(context.pins) ? context.pins : []);
      if (!pins.length) pinsCard.append(element(host, 'p', 'Закреплённых правил пока нет.', {class: 'completion-empty'}));
      for (const pin of pins) {
        const row = element(host, 'div', null, {class: 'completion-project-row'});
        const isAvailable = pin?.status === 'available' && pin.rule && pin.rule_id;
        if (isAvailable) {
          row.append(element(host, 'strong', text(pin.rule.title) || 'Правило без названия'));
        } else {
          row.append(element(host, 'strong', 'Правило недоступно'));
        }
        const versionLabel = Number.isInteger(pin?.version_no) ? 'v' + pin.version_no : 'версия не указана';
        row.append(element(host, 'p', versionLabel + ' · ' + requiredLabel(pin)
          + ' · приоритет ' + (Number.isInteger(pin?.priority) ? pin.priority : 'не указан'), {class: 'muted'}));
        if (isAvailable) {
          if (layerLabels[pin.layer]) row.append(element(host, 'p', 'Слой: ' + layerLabels[pin.layer], {class: 'muted'}));
          const rule = pin.rule;
          const publication = {published: 'Опубликовано', draft: 'Черновик', needs_review: 'На проверке', withdrawn: 'Отозвано'};
          row.append(element(host, 'p', 'Статус: ' + (publication[rule.publication] || 'Не указан'), {class: 'muted'}));
          for (const [label, value] of [
            ['Когда применять', rule.when_to_apply], ['Входные данные', rule.inputs],
            ['Шаги', rule.actions], ['Исключения', rule.exceptions],
            ['Ожидаемый эффект', rule.expected_effect], ['Проверка', rule.verification]
          ]) {
            const printable = Array.isArray(value) && value.every(item => typeof item === 'string')
              ? value.join('\n') : text(value);
            if (printable) row.append(element(host, 'p', label + ': ' + printable));
          }
          const contentHash = pin.content_hash ?? rule.content_hash;
          if (Array.isArray(rule.provenance) && rule.provenance.length) {
            const refs = element(host, 'details');
            refs.append(element(host, 'summary', 'Происхождение правила'));
            for (const ref of rule.provenance) {
              const loc = ref.locator || {};
              const parts = [ref.author, ref.publication, loc.chapter,
                loc.file_page ? 'Страница файла ' + loc.file_page : null,
                loc.printed_label ? 'Печатная страница ' + loc.printed_label : null,
                loc.table ? 'Таблица ' + loc.table : null,
                Array.isArray(loc.cell) ? 'Ячейка ' + loc.cell.join(':') : null].filter(Boolean);
              refs.append(element(host, 'p', parts.join(' · ') || 'Источник ' + ref.source_id));
              if (hash.test(ref.content_hash || '')) refs.append(element(host, 'p', 'Хэш оригинала: ' + ref.content_hash, {class: 'muted'}));
            }
            row.append(refs);
          }
          if (typeof contentHash === 'string' && hash.test(contentHash)) row.append(element(host, 'p', 'Хэш версии: ' + contentHash, {class: 'muted'}));
          if (text(pin.bound_at)) row.append(element(host, 'p', 'Закреплено: ' + pin.bound_at, {class: 'muted'}));
          if (canCurate && uuid.test(pin.rule_id)) {
            const confirmArea = element(host, 'div', null, {class: 'completion-form'});
            row.append(makeAction(host, 'Снять закрепление', () => confirmRemoval(
              confirmArea, 'Точная версия правила будет исключена из контекста. Подтвердите действие.', async () => {
                await host.api('/api/projects/' + encodeURIComponent(library.id) + '/rules/' + encodeURIComponent(pin.rule_id), {method: 'DELETE'});
                if (isCurrent()) await refreshContext();
              }
            )));
            row.append(confirmArea);
          }
        } else {
          row.append(element(host, 'p', 'Недоступно для текущего доступа; сведения о правиле скрыты.', {class: 'muted'}));
        }
        pinsCard.append(row);
      }
      contextArea.append(pinsCard);
      contextReady = true;
      setFormsEnabled(forms, true);
    }

    async function refreshContext() {
      if (!isCurrent()) return;
      contextReady = false;
      setFormsEnabled(forms, false);
      contextArea.replaceChildren(element(host, 'p', 'Обновляю контекст проекта…', {role: 'status'}));
      try {
        const context = await host.api('/api/projects/' + encodeURIComponent(library.id) + '/context');
        if (!isCurrent()) return;
        renderContext(context);
      } catch (error) {
        if (!isCurrent()) return;
        contextArea.replaceChildren(element(host, 'p', safeError(error), {role: 'alert', class: 'completion-error'}));
        host.notify(safeError(error));
      }
    }

    await refreshContext();
  }

  window.WisdomProjectContext = {mount};
})();
