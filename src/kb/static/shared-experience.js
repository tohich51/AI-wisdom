'use strict';
(() => {
  const mounts = new WeakMap();
  const ownClass = 'shared-experience-panel';
  const text = value => value === null || value === undefined || value === '' ? 'не указано' : String(value);
  const safeError = error => error?.message || 'Запрос не выполнен. Проверьте подключение и права.';
  const achievementLabels = {met: 'Цель достигнута', partly: 'Достигнута частично', not_met: 'Цель не достигнута', unknown: 'Пока неизвестно'};
  const assessorLabels = {human: 'Человек', agent: 'Агент', measurement: 'Измерение', unknown: 'Не указан'};
  const achievementLabel = value => achievementLabels[value] || (value == null || value === '' ? 'Пока неизвестно' : 'Не указано');
  const assessorLabel = value => assessorLabels[value] || (value == null || value === '' ? 'Не указан' : 'Не указан');
  const correctionLabel = value => value === true ? 'Да' : value === false ? 'Нет' : 'Неизвестно';
  function renderCounts(counts, labels, missing) {
    if (counts == null || typeof counts !== 'object') return missing;
    const entries = Object.entries(counts);
    if (!entries.length) return missing;
    return entries.map(([key, value]) => (labels[key] || 'Не указано') + ': ' + text(value)).join(' · ');
  }

  function mount(root, host, records) {
    const mountId = (mounts.get(root) || 0) + 1;
    mounts.set(root, mountId);
    root.querySelector('.' + ownClass)?.remove();
    const area = host.node('section', null, {class: 'card ' + ownClass, 'aria-labelledby': 'shared-experience-title-' + mountId});
    const current = () => mounts.get(root) === mountId && area.isConnected && root.contains(area);
    const heading = host.node('h2', 'Опыт проекта', {id: 'shared-experience-title-' + mountId});
    const status = host.node('p', 'Выберите проектную библиотеку для просмотра общего опыта.', {class: 'muted', role: 'status', 'aria-atomic': 'true'});
    const controls = host.node('div', null, {class: 'completion-grid'});
    const feed = host.node('div', null, {class: 'completion-grid'});
    area.append(heading, status, controls, feed);
    root.append(area);

    const eligibleLibraries = (host.libraries?.() || []).filter(item => item && ['project', 'experience'].includes(item.kind) && item.id);
    const ownUses = (Array.isArray(records) ? records : []).filter(use => ['applied', 'outcomed', 'corrected'].includes(use?.state) && Array.isArray(use.outcomes) && use.outcomes.length && !(host.identity?.()?.principal_id && use.principal_id && use.principal_id !== host.identity().principal_id));
    const librarySelect = host.node('select', null, {id: 'shared-experience-library-' + mountId, 'aria-label': 'Проектная библиотека'});
    librarySelect.append(host.node('option', 'Выберите библиотеку', {value: ''}));
    for (const library of eligibleLibraries) librarySelect.append(host.node('option', text(library.name || library.title || library.id), {value: library.id}));
    const libraryField = host.node('div', null, {class: 'completion-field'});
    libraryField.append(host.node('label', 'Общая библиотека проекта', {for: librarySelect.id}), librarySelect);
    controls.append(libraryField);
    host.attachLibraryChooser(librarySelect, {accept:lib => ['project','experience'].includes(lib.kind), pool:eligibleLibraries});

    const useSelect = host.node('select', null, {id: 'shared-experience-use-' + mountId, 'aria-label': 'Собственное применение'});
    const outcomeSelect = host.node('select', null, {id: 'shared-experience-outcome-' + mountId, 'aria-label': 'Точный результат применения'});
    const useField = host.node('div', null, {class: 'completion-field'});
    const outcomeField = host.node('div', null, {class: 'completion-field'});
    useField.append(host.node('label', 'Моё применение', {for: useSelect.id}), useSelect);
    outcomeField.append(host.node('label', 'Какой именно результат', {for: outcomeSelect.id}), outcomeSelect);
    controls.append(useField, outcomeField);

    const review = host.node('div', null, {class: 'completion-form'});
    const reviewButton = host.button('Проверить выбранную копию', () => showReview());
    const publishButton = host.button('Опубликовать копию в проекте', () => publish());
    publishButton.disabled = true;
    review.append(reviewButton, publishButton);
    controls.append(review);

    const feedTitle = host.node('h3', 'Опубликованный опыт');
    feed.append(feedTitle);
    let policyRequest = 0, feedRequest = 0, reviewState = null, posting = false;

    function refreshUses() {
      useSelect.replaceChildren(host.node('option', ownUses.length ? 'Выберите применение' : 'Нет доступных применений', {value: ''}));
      for (const use of ownUses) useSelect.append(host.node('option', text(use.title || use.id) + ' · правило ' + text(use.rule_version_no), {value: use.id}));
      useSelect.value = '';
      refreshOutcomes();
    }
    function refreshOutcomes() {
      const use = ownUses.find(item => item.id === useSelect.value);
      outcomeSelect.replaceChildren(host.node('option', use ? 'Выберите точный результат' : 'Сначала выберите применение', {value: ''}));
      for (const result of use?.outcomes || []) {
        const parsedDate = result.reported_at ? new Date(result.reported_at) : null;
        const dateLabel = parsedDate && Number.isFinite(parsedDate.getTime()) ? new Intl.DateTimeFormat('ru-RU', {dateStyle: 'short', timeStyle: 'short'}).format(parsedDate) : 'Дата не указана';
        const observed = typeof result.observed_result === 'string' ? result.observed_result.trim() : '';
        const resultLabel = observed ? (observed.length > 80 ? observed.slice(0, 80).trimEnd() + '…' : observed) : 'результат пока не указан';
        outcomeSelect.append(host.node('option', (result.is_correction ? 'Исправление' : 'Результат') + ' · ' + dateLabel + ' · ' + resultLabel, {value: result.id}));
      }
      outcomeSelect.value = '';
      invalidateReview();
    }
    function invalidateReview() {
      reviewState = null;
      publishButton.disabled = true;
      reviewButton.disabled = posting;
      review.querySelectorAll('[data-shared-review]').forEach(node => node.remove());
    }
    function selected() {
      const use = ownUses.find(item => item.id === useSelect.value);
      const outcome = use?.outcomes?.find(item => item.id === outcomeSelect.value);
      const library = eligibleLibraries.find(item => item.id === librarySelect.value);
      return use && outcome && library ? {use, outcome, library} : null;
    }
    function appendFieldCard(parent, label, value) {
      const card = host.node('article', null, {class: 'card'});
      card.append(host.node('h4', label), host.node('p', text(value)));
      parent.append(card);
    }
    async function showReview() {
      invalidateReview();
      const choice = selected();
      if (!choice) {
        review.append(host.node('p', 'Выберите библиотеку, своё применение и точный результат.', {class: 'completion-error', role: 'alert', 'data-shared-review': ''}));
        return;
      }
      const token = ++policyRequest;
      const targetId = choice.library.id;
      reviewButton.disabled = true;
      const message = host.node('p', 'Проверяю права на публикацию…', {class: 'muted', role: 'status', 'data-shared-review': ''});
      review.append(message);
      let policy;
      try { policy = await host.api('/api/libraries/' + encodeURIComponent(targetId) + '/policy'); }
      catch (error) {
        if (current() && token === policyRequest && librarySelect.value === targetId) message.replaceWith(host.node('p', safeError(error), {class: 'completion-error', role: 'alert', 'data-shared-review': ''}));
        if (current() && token === policyRequest) reviewButton.disabled = false;
        return;
      }
      if (!current() || token !== policyRequest || librarySelect.value !== targetId) return;
      const latest = selected();
      const canPublish = ['curator', 'manager'].includes(policy?.effective_role);
      if (!latest || latest.use.id !== choice.use.id || latest.outcome.id !== choice.outcome.id) { reviewButton.disabled = false; return; }
      if (!canPublish) {
        message.replaceWith(host.node('p', 'Публикация доступна только куратору или менеджеру этой библиотеки.', {class: 'completion-error', role: 'alert', 'data-shared-review': ''}));
        reviewButton.disabled = false;
        return;
      }
      message.textContent = 'Проверка завершена. Выбранная копия готова к публикации.';
      const details = host.node('div', null, {class: 'completion-grid', 'data-shared-review': ''});
      details.append(host.node('p', 'Проверьте точную копию перед публикацией. Будущие исправления вашей личной записи останутся приватными, пока вы не опубликуете их отдельно.', {class: 'muted'}));
      appendFieldCard(details, 'Библиотека', latest.library.name || latest.library.title || latest.library.id);
      appendFieldCard(details, 'Правило · точная версия', text(latest.use.title) + ' · v' + text(latest.use.rule_version_no));
      appendFieldCard(details, 'Ожидаемый результат', latest.use.expected_result);
      appendFieldCard(details, 'Где применено', latest.use.where_applied);
      appendFieldCard(details, latest.outcome.is_correction ? 'Наблюдаемый результат · исправление' : 'Наблюдаемый результат', latest.outcome.observed_result);
      appendFieldCard(details, 'Полезность · основание', text(latest.outcome.rating) + ' · ' + text(latest.outcome.rating_basis));
      appendFieldCard(details, 'Достижение', achievementLabel(latest.outcome.achievement));
      appendFieldCard(details, 'Автор оценки · свидетельство', assessorLabel(latest.outcome.assessor_kind) + ' · ' + text(latest.outcome.assessment_evidence));
      review.append(details);
      reviewState = {useId: latest.use.id, outcomeId: latest.outcome.id, libraryId: latest.library.id};
      publishButton.disabled = false;
      reviewButton.disabled = false;
    }
    async function loadFeed() {
      const libraryId = librarySelect.value;
      const token = ++feedRequest;
      feed.replaceChildren(feedTitle);
      if (!libraryId) { status.textContent = 'Выберите проектную библиотеку для просмотра общего опыта.'; return; }
      status.textContent = 'Загружаю опубликованные записи…';
      feed.append(host.node('p', 'Загрузка…', {class: 'muted'}));
      try {
        const data = await host.api('/api/libraries/' + encodeURIComponent(libraryId) + '/experience');
        if (!current() || token !== feedRequest || librarySelect.value !== libraryId) return;
        renderFeed(data);
        status.textContent = Array.isArray(data?.items) && data.items.length ? 'Готово: опубликованные записи загружены.' : 'Готово: опубликованных записей пока нет.';
      } catch (error) {
        if (!current() || token !== feedRequest || librarySelect.value !== libraryId) return;
        status.textContent = 'Ошибка: не удалось загрузить общий опыт.';
        feed.replaceChildren(feedTitle, host.node('p', safeError(error), {class: 'completion-error'}));
      }
    }
    function renderFeed(data) {
      feed.replaceChildren(feedTitle);
      feed.append(host.node('p', 'Общие записи являются явными самоотчётами участников, а не доказательством бизнес-результата.', {class: 'muted'}));
      const items = Array.isArray(data?.items) ? data.items : [];
      const versions = Array.isArray(data?.by_rule_version) ? data.by_rule_version : [];
      const aggregateGrid = host.node('div', null, {class: 'completion-grid'});
      const titleByRuleVersion = new Map();
      for (const item of items) {
        const snapshot = item?.snapshot || {};
        const title = typeof snapshot.rule_title === 'string' ? snapshot.rule_title.trim() : '';
        if (title) titleByRuleVersion.set(JSON.stringify([
          item?.rule_id || snapshot.rule_id,
          item?.rule_version_no ?? snapshot.rule_version_no
        ]), title);
      }
      for (const row of versions) {
        const card = host.node('article', null, {class: 'card'});
        const matchingTitle = titleByRuleVersion.get(JSON.stringify([row.rule_id, row.rule_version_no]));
        card.append(host.node('h4', text(matchingTitle || 'Сводка по правилу') + ' · v' + text(row.rule_version_no)));
        card.append(host.node('p', 'Опубликованных записей: ' + text(row.total) + ' · оценок полезности: ' + text(row.rated)));
        card.append(host.node('p', 'Средняя полезность по опубликованным оценкам: ' + text(row.average_rating)));
        card.append(host.node('p', 'Достижение: ' + renderCounts(row.achievement_counts, achievementLabels, 'Нет данных') + ' · автор оценки: ' + renderCounts(row.assessor_kind_counts, assessorLabels, 'Нет данных')));
        aggregateGrid.append(card);
      }
      if (versions.length) feed.append(aggregateGrid);
      const publishedCards = [];
      for (const item of items) {
        const snapshot = item?.snapshot || {};
        const card = host.node('article', null, {class: 'card'});
        const ruleTitle = typeof snapshot.rule_title === 'string' ? snapshot.rule_title.trim() : '';
        card.append(host.node('h4', text(ruleTitle || 'Правило') + ' · v' + text(item.rule_version_no ?? snapshot.rule_version_no)));
        const fields = [
          ['Ожидаемый результат', snapshot.expected_result], ['Где применено', snapshot.where_applied],
          [snapshot.is_correction ? 'Наблюдаемый результат · исправление' : 'Наблюдаемый результат', snapshot.observed_result],
          ['Полезность', snapshot.rating], ['Основание оценки полезности', snapshot.rating_basis],
          ['Достижение', achievementLabel(snapshot.achievement)], ['Автор оценки', assessorLabel(snapshot.assessor_kind)],
          ['Свидетельство результата', snapshot.assessment_evidence], ['Время записи', snapshot.reported_at],
          ['Опубликовано', item.published_at], ['Отметка об исправлении', correctionLabel(snapshot.is_correction)]
        ];
        for (const [label, value] of fields) card.append(host.node('p', label + ': ' + text(value)));
        card.append(host.node('p', 'Самоотчёт участника; не измеренное доказательство бизнес-результата.', {class: 'muted'}));
        publishedCards.push(card);
      }
      if (window.WisdomList && publishedCards.length) {
        const published = window.WisdomList.create(host, {label: 'Страницы списка опубликованных записей', library:()=>librarySelect.value, renderItem: built => built});
        published.set(publishedCards);
        feed.append(published.element, published.navigation, published.status);
      } else for (const card of publishedCards) feed.append(card);
      if (data?.basis && data.basis !== 'explicit_shared_self_report_not_business_proof') feed.append(host.node('p', 'Основание данных: ' + text(data.basis), {class: 'muted'}));
    }
    async function publish() {
      const choice = selected();
      if (!current() || posting || !choice || !reviewState || choice.use.id !== reviewState.useId || choice.outcome.id !== reviewState.outcomeId || choice.library.id !== reviewState.libraryId) {
        invalidateReview();
        return;
      }
      const ids = {...reviewState};
      posting = true;
      const buttons = [...area.querySelectorAll('button, select')];
      buttons.forEach(control => { control.disabled = true; });
      const message = host.node('p', 'Публикую выбранную копию…', {class: 'muted', role: 'status'});
      review.append(message);
      try {
        await host.post('/api/libraries/' + encodeURIComponent(ids.libraryId) + '/experience', {use_id: ids.useId, outcome_id: ids.outcomeId});
        if (!current()) return;
        message.replaceWith(host.node('p', 'Выбранная копия опубликована. Личная запись и её будущие исправления не изменены.', {class: 'muted', role: 'status'}));
        invalidateReview();
        await loadFeed();
      } catch (error) {
        if (current()) message.replaceWith(host.node('p', safeError(error), {class: 'completion-error', role: 'alert'}));
      } finally {
        posting = false;
        if (current()) buttons.forEach(control => { control.disabled = false; });
        publishButton.disabled = true;
      }
    }
    for (const [select, handler] of [[librarySelect, () => { ++policyRequest; invalidateReview(); void loadFeed(); }], [useSelect, () => { ++policyRequest; refreshOutcomes(); }], [outcomeSelect, () => { ++policyRequest; invalidateReview(); }]]) select.addEventListener('change', handler);
    refreshUses();
    if (!eligibleLibraries.length) {
      status.textContent = 'На загруженной странице нет проектных библиотек. Используйте выбор из всех библиотек.';
      feed.append(host.node('p', 'Для общего опыта выберите библиотеку типа «Проект» или «Опыт».', {class: 'muted'}));
    }
    return area;
  }

  window.WisdomSharedExperience = {mount};
})();
