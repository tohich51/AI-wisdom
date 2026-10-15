'use strict';
(() => {
  const el = (tag, text, attrs = {}) => {
    const out = document.createElement(tag);
    if (text != null) out.textContent = String(text);
    for (const [key, value] of Object.entries(attrs)) out.setAttribute(key, value);
    return out;
  };
  const card = (title, body) => {
    const box = el('article', null, {class: 'card completion-card'});
    box.append(el('h2', title)); if (body) box.append(body); return box;
  };
  const field = (label, name, type = 'text', required = false) => {
    const wrap = el('div', null, {class: 'completion-field'}), id = 'completion-' + name + '-' + crypto.randomUUID();
    wrap.append(el('label', label, {for: id}));
    const input = el(type === 'select' ? 'select' : 'input', null, {id, name});
    if (type !== 'select') input.type = type;
    if (required) input.required = true;
    wrap.append(input); return {wrap, input};
  };
  const action = (label, fn, kind = 'secondary') => {
    const button = el('button', label, {type: 'button', class: kind});
    button.addEventListener('click', async () => {
      button.disabled = true;
      try { await fn(); } catch (error) { window.WisdomCompletionHost?.notify(error.message); }
      finally { button.disabled = false; }
    }); return button;
  };
  const fail = (root, error) => {
    root.replaceChildren(el('p', error.message || 'Не удалось загрузить данные. Попробуйте обновить раздел.', {role: 'alert', class: 'completion-error'}));
  };
  const empty = (root, text) => root.append(el('p', text, {class: 'completion-empty'}));

  async function mountJobs(root, host) {
    root.replaceChildren(el('p', 'Загружаю задания…', {role: 'status'}));
    try {
      const jobs = await host.api('/api/jobs');
      const list = el('div', null, {class: 'completion-grid'});
      if (!jobs.length) empty(list, 'Заданий пока нет. Они появятся после обработки источника.');
      const jobCard = job => {
        const box = card(job.title || 'Материал без названия');
        box.append(el('p', 'Состояние: ' + job.status));
        box.append(el('p', 'Готовые части: ' + job.chunks_done + ' из ' + (job.chunks_total ?? 'не указано')));
        if (job.error_code) box.append(el('p', 'Код ошибки: ' + job.error_code, {class: 'completion-error'}));
        if (job.created_at) box.append(el('p', 'Создано: ' + new Date(job.created_at).toLocaleString('ru-RU'), {class: 'muted'}));
        if (job.completed_at) box.append(el('p', 'Завершено: ' + new Date(job.completed_at).toLocaleString('ru-RU'), {class: 'muted'}));
        return box;
      };
      const paged = window.WisdomList && jobs.length ? window.WisdomList.create(host, {label: 'Страницы списка заданий', renderItem: jobCard}) : null;
      if (paged) { paged.set(jobs); list.append(paged.element, paged.navigation, paged.status); }
      else for (const job of jobs) list.append(jobCard(job));
      root.replaceChildren(action('Обновить список', () => mountJobs(root, host)), list);
    } catch (error) { fail(root, error); }
  }

  async function mountAccess(root, host) {
    root.replaceChildren(el('p', 'Загружаю доступ…', {role: 'status'}));
    try {
      const accountId = host.identity()?.account_id;
      if (!accountId) throw Error('В текущем профиле не найден идентификатор организации.');
      const [members, groups, invitations] = await Promise.all([
        host.api('/api/orgs/' + encodeURIComponent(accountId) + '/members'),
        host.api('/api/orgs/' + encodeURIComponent(accountId) + '/groups'),
        host.api('/api/orgs/' + encodeURIComponent(accountId) + '/invitations')
      ]);
      const content = el('div', null, {class: 'completion-grid'});
      const libraries = host.libraries().filter(item => item?.id);
      const activeMembers = members.filter(member => member.status === 'active' && member.principal_id);
      const addOptions = (select, items, id, label) => {
        select.append(el('option', 'Выберите…', {value: ''}));
        for (const item of items) select.append(el('option', label(item), {value: item[id]}));
      };
      const roleField = (parent, name = 'role') => {
        const wrap = el('div', null, {class: 'completion-field'}), id = 'completion-' + name + '-' + crypto.randomUUID();
        const select = el('select', null, {id, name});
        for (const value of ['reader', 'contributor', 'curator', 'manager']) select.append(el('option', value, {value}));
        wrap.append(el('label', 'Роль', {for: id}), select); parent.append(wrap); return select;
      };
      const selectField = (parent, label, name, items, id, text) => {
        const wrapper = field(label, name, 'select');
        addOptions(wrapper.input, items, id, text); parent.append(wrapper.wrap); return wrapper.input;
      };
      const memberBox = card('Участники');
      if (!members.length) empty(memberBox, 'Участники не отображаются для текущей роли.');
      const memberItem = member => {
        const memberLine = el('div', null, {class: 'completion-list-item'});
        const label = [member.display_name, member.email, member.status].filter(Boolean).join(' · ') || member.principal_id;
        memberLine.append(el('p', label));
        if (member.status === 'active') {
          const deactivate = el('details'); deactivate.append(el('summary', 'Деактивировать участника'));
          const form = el('form', null, {class: 'completion-form'}), reason = field('Причина (необязательно)', 'reason');
          const confirmed = field('Подтверждаю блокировку участника и отзыв его активных сессий', 'confirmed', 'checkbox');
          const submit = el('button', 'Деактивировать', {type: 'submit'});
          form.append(reason.wrap, confirmed.wrap, submit);
          form.addEventListener('submit', async event => {
            event.preventDefault(); if (!confirmed.input.checked) { confirmed.input.setCustomValidity('Подтвердите блокировку участника.'); confirmed.input.reportValidity(); return; }
            submit.disabled = true;
            try {
              const result = await host.post('/api/orgs/' + encodeURIComponent(accountId) + '/members/' + encodeURIComponent(member.principal_id) + '/deactivate', {reason: reason.input.value.trim() || null});
              host.notify(result.revocation_outstanding ? 'Участник заблокирован; отзыв сессии ожидает оператора.' : 'Участник заблокирован, отзыв сессии подтверждён.');
              await mountAccess(root, host);
            } catch (error) { host.notify(error.message); } finally { submit.disabled = false; }
          });
          deactivate.append(form); memberLine.append(deactivate);
        }
        return memberLine;
      };
      const memberList = window.WisdomList && members.length ? window.WisdomList.create(host, {label: 'Страницы списка участников', renderItem: memberItem}) : null;
      if (memberList) { memberList.set(members); memberBox.append(memberList.element, memberList.navigation, memberList.status); }
      else for (const member of members) memberBox.append(memberItem(member));
      const inviteBox = card('Приглашения');
      if (!invitations.length) empty(inviteBox, 'Ожидающих приглашений нет.');
      for (const item of invitations) inviteBox.append(el('p', [item.email, item.subject, item.status].filter(Boolean).join(' · ') || 'Приглашение'));
      const inviteForm = el('form', null, {class: 'completion-form'});
      const email = field('Электронная почта (необязательно)', 'email', 'email');
      const subject = field('Идентификатор subject (необязательно)', 'subject');
      email.input.maxLength = 320; subject.input.maxLength = 255;
      const submit = el('button', 'Записать приглашение', {type: 'submit'});
      inviteForm.append(email.wrap, subject.wrap, el('p', 'Это только запись в очереди оператора; письмо не отправляется.', {class: 'muted'}), submit);
      inviteForm.addEventListener('submit', async event => {
        event.preventDefault();
        if (!email.input.value.trim() && !subject.input.value.trim()) { email.input.setCustomValidity('Укажите почту или subject.'); email.input.reportValidity(); return; }
        email.input.setCustomValidity(''); submit.disabled = true;
        try {
          await host.post('/api/orgs/' + encodeURIComponent(accountId) + '/invitations', {email: email.input.value.trim() || null, subject: subject.input.value.trim() || null});
          await mountAccess(root, host);
        } catch (error) { host.notify(error.message); } finally { submit.disabled = false; }
      });
      inviteBox.append(inviteForm);
      const groupBox = card('Группы');
      if (!groups.length) empty(groupBox, 'Групп пока нет.');
      const groupCards = [];
      for (const group of groups) {
        const groupCard = card(group.name);
        const membershipForm = el('form', null, {class: 'completion-form'});
        const memberSelect = selectField(membershipForm, 'Участник для добавления в группу', 'principal_id', activeMembers, 'principal_id', member => [member.display_name, member.email].filter(Boolean).join(' · ') || member.principal_id);
        const add = el('button', 'Добавить участника', {type: 'submit'}); membershipForm.append(add);
        membershipForm.addEventListener('submit', async event => {
          event.preventDefault(); if (!memberSelect.value) return;
          add.disabled = true;
          try { await host.api('/api/orgs/' + encodeURIComponent(accountId) + '/groups/' + encodeURIComponent(group.id) + '/members/' + encodeURIComponent(memberSelect.value), {method: 'PUT'}); host.notify('Участник добавлен в группу.'); await mountAccess(root, host); }
          catch (error) { host.notify(error.message); } finally { add.disabled = false; }
        });
        const removeForm = el('form', null, {class: 'completion-form'});
        const removeMember = selectField(removeForm, 'Участник для удаления из группы', 'principal_id', activeMembers, 'principal_id', member => [member.display_name, member.email].filter(Boolean).join(' · ') || member.principal_id);
        const explainLibrary = libraries.length ? selectField(removeForm, 'Проверить оставшийся доступ к библиотеке (необязательно)', 'library_id', libraries, 'id', item => item.name || item.id) : null;
        const remove = el('button', 'Убрать из группы', {type: 'submit'}); removeForm.append(remove);
        removeForm.append(el('p', 'Показываются активные участники организации. API не предоставляет отдельный список состава группы; отсутствие участника будет показано как отказ API.', {class: 'muted'}));
        removeForm.addEventListener('submit', async event => {
          event.preventDefault(); if (!removeMember.value) return;
          if (!window.confirm('Убрать выбранного участника из группы? Его прямые разрешения сохранятся.')) return;
          remove.disabled = true;
          try {
            await host.api('/api/orgs/' + encodeURIComponent(accountId) + '/groups/' + encodeURIComponent(group.id) + '/members/' + encodeURIComponent(removeMember.value), {method: 'DELETE'});
          } catch (error) { host.notify(error.message); remove.disabled = false; return; }
          let outcome = 'Участник удалён из группы.';
          if (explainLibrary?.value) {
            try {
              const detail = await host.api('/api/orgs/' + encodeURIComponent(accountId) + '/access/' + encodeURIComponent(removeMember.value) + '/libraries/' + encodeURIComponent(explainLibrary.value));
              const paths = detail.paths.map(path => path.kind === 'direct' ? 'прямое разрешение ' + path.role : 'группа ' + (path.group_name || path.group_id) + ' · ' + path.role);
              outcome += ' После изменения: ' + (detail.denied ? 'доступ отсутствует (' + detail.reason + ')' : 'эффективная роль ' + (detail.effective_role || 'не указана')) + (paths.length ? '; пути: ' + paths.join(', ') : '; активных путей нет') + ' (ревизия ' + detail.policy_revision + ').';
            } catch (error) { outcome += ' Проверка оставшегося доступа не удалась: ' + error.message; }
          }
          host.notify(outcome); await mountAccess(root, host);
        });
        groupCard.append(membershipForm, removeForm);

        const grantsPath = '/api/orgs/' + encodeURIComponent(accountId) + '/groups/' + encodeURIComponent(group.id) + '/grants';
        try {
          const grants = await host.api(grantsPath);
          if (!grants.length) empty(groupCard, 'Разрешений группы на библиотеки пока нет.');
          for (const grant of grants) {
            const library = libraries.find(item => item.id === grant.library_id);
            const row = el('p', (library?.name || grant.library_id) + ' · ' + grant.role);
            const revoke = action('Отозвать разрешение', async () => {
              if (!window.confirm('Отозвать разрешение группы «' + group.name + '» для этой библиотеки?')) return;
              await host.api(grantsPath + '/' + encodeURIComponent(grant.library_id), {method: 'DELETE'}); await mountAccess(root, host);
            }, 'secondary');
            row.append(document.createTextNode(' · '), revoke); groupCard.append(row);
          }
        } catch (error) { groupCard.append(el('p', error.message, {role: 'alert', class: 'completion-error'})); }
        if (libraries.length) {
          const grantForm = el('form', null, {class: 'completion-form'});
          const librarySelect = selectField(grantForm, 'Библиотека для группы', 'library_id', libraries, 'id', item => item.name || item.id);
          const role = roleField(grantForm); const save = el('button', 'Назначить роль группе', {type: 'submit'}); grantForm.append(save);
          grantForm.addEventListener('submit', async event => {
            event.preventDefault(); if (!librarySelect.value) return;
            save.disabled = true;
            try { await host.api(grantsPath + '/' + encodeURIComponent(librarySelect.value), {method: 'PUT', body: JSON.stringify({role: role.value})}); host.notify('Роль группы сохранена.'); await mountAccess(root, host); }
            catch (error) { host.notify(error.message); } finally { save.disabled = false; }
          });
          groupCard.append(grantForm);
        }
        groupCards.push(groupCard);
      }
      const groupList = window.WisdomList && groupCards.length ? window.WisdomList.create(host, {label: 'Страницы списка групп', renderItem: built => built}) : null;
      if (groupList) { groupList.set(groupCards); groupBox.append(groupList.element, groupList.navigation, groupList.status); }
      else for (const groupCard of groupCards) groupBox.append(groupCard);
      const groupForm = el('form', null, {class: 'completion-form'});
      const groupName = field('Название группы (латиница, цифры, _ или -)', 'name'); groupName.input.pattern = '[a-z][a-z0-9_-]{1,63}'; groupName.input.required = true;
      const create = el('button', 'Создать группу', {type: 'submit'}); groupForm.append(groupName.wrap, create);
      groupForm.addEventListener('submit', async event => {
        event.preventDefault(); create.disabled = true;
        try { await host.post('/api/orgs/' + encodeURIComponent(accountId) + '/groups', {name: groupName.input.value}); await mountAccess(root, host); }
        catch (error) { host.notify(error.message); } finally { create.disabled = false; }
      });
      groupBox.append(groupForm);
      const directBox = card('Прямые разрешения и объяснение доступа');
      for (const library of libraries) {
        const libraryCard = card(library.name || library.id);
        try {
          const [policy, grants] = await Promise.all([
            host.api('/api/libraries/' + encodeURIComponent(library.id) + '/policy'),
            host.api('/api/libraries/' + encodeURIComponent(library.id) + '/grants')
          ]);
          const manager = policy.effective_role === 'manager';
          if (!grants.length) empty(libraryCard, 'Видимых прямых разрешений нет.');
          for (const grant of grants) {
            const member = members.find(item => item.principal_id === grant.principal_id);
            const row = el('p', [member?.display_name || grant.principal_id, member?.email, grant.role].filter(Boolean).join(' · '));
            if (manager) row.append(document.createTextNode(' · '), action('Отозвать', async () => {
              if (!window.confirm('Отозвать прямое разрешение участника на «' + (library.name || library.id) + '»? Групповой доступ при этом может сохраниться.')) return;
              await host.api('/api/libraries/' + encodeURIComponent(library.id) + '/grants/' + encodeURIComponent(grant.principal_id), {method: 'DELETE'}); await mountAccess(root, host);
            }, 'secondary'));
            libraryCard.append(row);
          }
          libraryCard.append(el('p', 'Действующая роль с учётом прямых и групповых разрешений: ' + (policy.effective_role || 'нет доступа'), {class: 'muted'}));
          if (manager) {
            const form = el('form', null, {class: 'completion-form'});
            const memberSelect = selectField(form, 'Участник', 'principal_id', activeMembers, 'principal_id', member => [member.display_name, member.email].filter(Boolean).join(' · ') || member.principal_id);
            const role = roleField(form); const save = el('button', 'Сохранить прямую роль', {type: 'submit'}); form.append(save);
            form.addEventListener('submit', async event => {
              event.preventDefault(); if (!memberSelect.value) return;
              save.disabled = true;
              try { await host.api('/api/libraries/' + encodeURIComponent(library.id) + '/grants', {method: 'PUT', body: JSON.stringify({principal_id: memberSelect.value, role: role.value})}); host.notify('Прямое разрешение сохранено.'); await mountAccess(root, host); }
              catch (error) { host.notify(error.message); } finally { save.disabled = false; }
            });
            libraryCard.append(form);
          } else libraryCard.append(el('p', 'Менять прямые разрешения может только менеджер этой библиотеки.', {class: 'muted'}));
        } catch (error) { libraryCard.append(el('p', error.message, {role: 'alert', class: 'completion-error'})); }
        directBox.append(libraryCard);
      }
      if (libraries.length) {
        const explain = el('form', null, {class: 'completion-form'});
        const member = selectField(explain, 'Проверить участника', 'principal_id', members, 'principal_id', item => [item.display_name, item.email, item.status].filter(Boolean).join(' · ') || item.principal_id);
        const library = selectField(explain, 'Библиотека', 'library_id', libraries, 'id', item => item.name || item.id);
        const lookup = el('button', 'Показать пути доступа', {type: 'submit'}), result = el('div', null, {class: 'completion-access-result'});
        explain.append(lookup, result);
        explain.addEventListener('submit', async event => {
          event.preventDefault(); if (!member.value || !library.value) return;
          lookup.disabled = true; result.replaceChildren(el('p', 'Проверяю действующие пути…', {role: 'status'}));
          try {
            const detail = await host.api('/api/orgs/' + encodeURIComponent(accountId) + '/access/' + encodeURIComponent(member.value) + '/libraries/' + encodeURIComponent(library.value));
            result.replaceChildren(el('p', (detail.denied ? 'Доступ отсутствует: ' + detail.reason : 'Доступ разрешён') + ' · эффективная роль: ' + (detail.effective_role || 'нет доступа') + ' · ревизия политики: ' + detail.policy_revision));
            if (!detail.paths.length) result.append(el('p', 'Активных путей доступа нет.'));
            for (const path of detail.paths) result.append(el('p', path.kind === 'direct' ? 'Прямое разрешение · ' + path.role : 'Группа ' + (path.group_name || path.group_id) + ' · ' + path.role));
          } catch (error) { result.replaceChildren(el('p', error.message, {role: 'alert', class: 'completion-error'})); }
          finally { lookup.disabled = false; }
        });
        directBox.append(explain);
      }
      content.append(memberBox, inviteBox, groupBox, directBox);
      root.replaceChildren(action('Обновить доступ', () => mountAccess(root, host)), content);
    } catch (error) { fail(root, error); }
  }

  async function enhanceLibrary(root, library, host) {
    root.querySelector('.completion-library')?.remove();
    const area = el('section', null, {class: 'completion-library'});
    const policyCard = card('Обработка и экспорт');
    policyCard.append(el('p', 'Получаю действующие ограничения библиотеки…', {role: 'status'}));
    area.append(policyCard);
    if (!host.currentLibrary() || host.currentLibrary().id !== library.id) return;
    root.insertBefore(area, root.children[2] || null);
    try {
      const policy = await host.api('/api/libraries/' + encodeURIComponent(library.id) + '/policy');
      if (!host.currentLibrary() || host.currentLibrary().id !== library.id) return;
      policyCard.replaceChildren(el('h3', 'Обработка и экспорт'));
      const manager = policy.effective_role === 'manager';
      const canReview = manager || policy.effective_role === 'curator';
      const names = [
        ['external_processing_allowed', 'Внешняя обработка'], ['cloud_mcp_allowed', 'Облачные MCP-клиенты'],
        ['export_allowed', 'Экспорт'], ['generation_allowed', 'Генерация']
      ];
      policyCard.append(el('p', 'Роль: ' + (policy.effective_role || 'не указана')));
      const form = el('form', null, {class: 'completion-form'});
      for (const [key, label] of names) {
        const row = field(label, key, 'checkbox'); row.input.checked = Boolean(policy[key]); row.input.disabled = !manager;
        row.wrap.classList.add('completion-check'); form.append(row.wrap);
      }
      policyCard.append(el('p', 'Локальный поиск продолжит работать при выключенных облачных и внешних обработчиках. Генерация изначально выключена.', {class: 'muted'}));
      if (manager) {
        const save = el('button', 'Сохранить ограничения', {type: 'submit'}); form.append(save);
        form.addEventListener('submit', async event => {
          event.preventDefault(); save.disabled = true;
          try {
            const payload = Object.fromEntries(names.map(([key]) => [key, form.elements[key].checked]));
            const next = await host.api('/api/libraries/' + encodeURIComponent(library.id) + '/policy', {method: 'PUT', body: JSON.stringify(payload)});
            if (!host.currentLibrary() || host.currentLibrary().id !== library.id) return;
            host.notify('Ограничения сохранены.');
            if (host.refreshLibrary) await host.refreshLibrary(); else await enhanceLibrary(root, library, host);
          } catch (error) { fail(policyCard, error); } finally { save.disabled = false; }
        });
      } else policyCard.append(el('p', 'Изменять ограничения может только менеджер библиотеки.', {class: 'muted'}));
      policyCard.append(form);
      const [knowledge, rules] = await Promise.all([
        host.api('/api/libraries/' + encodeURIComponent(library.id) + '/knowledge'),
        host.api('/api/libraries/' + encodeURIComponent(library.id) + '/rules')
      ]);
      if (!host.currentLibrary() || host.currentLibrary().id !== library.id) return;
      const drafts = [...knowledge.filter(item => ['draft', 'needs_review'].includes(item.publication)).map(item => ({...item, item_kind: 'knowledge'})),
        ...rules.filter(item => ['draft', 'needs_review'].includes(item.publication)).map(item => ({...item, item_kind: 'rule'}))];
      const review = card('Проверка черновиков');
      if (!drafts.length) empty(review, 'Черновиков для проверки нет.');
      const checks = [];
      for (const item of drafts) {
        const label = el(canReview ? 'label' : 'p', null, {class: 'completion-review-item'});
        if (canReview) {
          const check = el('input', null, {type: 'checkbox', value: item.id});
          checks.push({check, item}); label.append(check);
        }
        label.append(el('span', (item.title || item.statement || 'Без названия') + ' · ' + item.item_kind + ' · ' + item.publication));
        review.append(label);
        if (item.item_kind === 'knowledge') {
          const canEdit = canReview || Boolean(item.created_by && item.created_by === host.identity()?.principal_id);
          const editor = el('details'); editor.append(el('summary', canEdit ? 'Редактировать текст и свидетельство' : 'Свидетельства'));
          const provenance = item.provenance || [];
          if (!provenance.length) editor.append(el('p', 'Связанные свидетельства не указаны.', {class: 'muted'}));
          for (const proof of provenance) {
            const line = el('p', [proof.quote, proof.locator?.chapter, proof.locator?.file_page ? 'Страница ' + proof.locator.file_page : null, proof.fragment_id].filter(Boolean).join(' · ') || 'Фрагмент источника');
            if (typeof proof.source_id === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(proof.source_id)) line.append(document.createTextNode(' · '), action('Открыть источник', () => host.openSource(proof.source_id)));
            editor.append(line);
          }
          if (canEdit) {
            const text = el('textarea'); text.value = item.statement || ''; text.setAttribute('aria-label', 'Текст черновика');
            const save = el('button', 'Сохранить текст', {type: 'button'});
            save.addEventListener('click', async () => {
              save.disabled = true;
              try {
                await host.api('/api/knowledge/' + encodeURIComponent(item.id), {method: 'PATCH', body: JSON.stringify({statement: text.value, expected_statement: item.statement, kind: item.kind})});
                if (!host.currentLibrary() || host.currentLibrary().id !== library.id) return;
                host.notify('Черновик обновлён.'); await enhanceLibrary(root, library, host);
              } catch (error) { fail(review, error); } finally { save.disabled = false; }
            }); editor.append(text, save);
          }
          review.append(editor);
        }
      }
      if (canReview && drafts.length) for (const [decision, title] of [['publish', 'Опубликовать выбранное'], ['reject', 'Отклонить выбранное']]) review.append(action(title, async () => {
        const selected = checks.filter(({check}) => check.checked);
        if (!selected.length) throw Error('Сначала отметьте черновики для этого действия.');
        if (selected.some(({item}) => item.item_kind !== selected[0].item.item_kind)) throw Error('Выберите знания или правила одного типа за раз.');
        review.querySelector('[data-batch-confirm]')?.remove();
        const confirmation = el('div', null, {class:'completion-form', 'data-batch-confirm':'true'});
        confirmation.append(el('p', (decision === 'publish' ? 'Опубликовать' : 'Отклонить') + ' выбранных элементов: ' + selected.length));
        confirmation.append(action('Подтвердить', async () => {
          const now = checks.filter(({check}) => check.checked).map(({item}) => item.id);
          if (JSON.stringify(now) !== JSON.stringify(selected.map(({item}) => item.id))) throw Error('Выбор изменился. Повторите действие.');
          await host.post('/api/libraries/' + encodeURIComponent(library.id) + '/review', {item_kind: selected[0].item.item_kind, item_ids: now, decision});
          if (!host.currentLibrary() || host.currentLibrary().id !== library.id) return;
          await enhanceLibrary(root, library, host);
        }), action('Отмена', () => confirmation.remove()));
        review.append(confirmation);
      }, decision === 'publish' ? 'primary' : 'secondary'));
      area.append(review);
    } catch (error) { fail(policyCard, error); }
  }

  async function enhanceUses(root, host) {
    root.querySelector('.completion-experience')?.remove();
    const area = el('section', null, {class: 'completion-experience'});
    area.append(el('p', 'Загружаю сводку…', {role: 'status'}));
    root.append(area);
    try {
      const result = await host.api('/api/experience-summary');
      const list = el('div', null, {class: 'completion-grid'});
      if (!result.by_rule_version.length) empty(list, 'Сводка появится после записей о собственном применении правил.');
      for (const row of result.by_rule_version) {
        const box = card(row.title + ' · версия ' + row.rule_version_no);
        box.append(el('p', 'Записей использования: ' + row.retrieved + ' · применено: ' + row.applied));
        box.append(el('p', 'Есть запись результата: ' + row.reported + ' · Результат пока не записан: ' + row.outcome_unknown));
        box.append(el('p', 'Средняя самооценка: ' + (row.average_rating ?? 'нет оценки') + ' · оценок: ' + row.rated)); list.append(box);
      }
      area.replaceChildren(el('p', 'Сводка учитывает только ваш опыт. Просмотр и поиск не считаются применением; отсутствие записи результата не означает неудачу.', {class: 'muted'}), el('p','Выдач материалов в доступной личной истории: ' + (result.visible_issuance_count ?? 'неизвестно') + '. Выдача не доказывает чтение.'), list);
      const journal = el('details'); journal.append(el('summary','Последние выдачи материалов'));
      const entries = await host.api('/api/retrievals');
      if (!entries.length) journal.append(el('p','История выдачи пока пуста.'));
      for (const entry of entries) journal.append(el('p',(entry.title ?? 'Материал') + ' · ' + entry.item_kind + (entry.version_no ? ' · v' + entry.version_no : '') + ' · ' + entry.delivered_at));
      area.append(journal);
    } catch (error) { fail(area, error); }
  }

  window.WisdomCompletion = {mountAccess, mountJobs, enhanceLibrary, enhanceUses};
})();
