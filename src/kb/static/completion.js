'use strict';
(() => {
  let accessEpoch = 0;
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const el = (tag, text, attrs = {}) => {
    const out = document.createElement(tag);
    if (text != null) out.textContent = String(text);
    for (const [key, value] of Object.entries(attrs)) out.setAttribute(key, value);
    return out;
  };
  window.WisdomForms?.configure(el);
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
  const errorMessage = error => {
    if (typeof error?.message === 'string' && error.message.trim()) return error.message;
    if (typeof error === 'string' && error.trim()) return error;
    return 'Неизвестная ошибка.';
  };
  const action = (label, fn, kind = 'secondary') => {
    const button = el('button', label, {type: 'button', class: kind});
    let pending = false;
    button.addEventListener('click', async () => {
      if (pending) return;
      pending = true;
      const disabled = button.disabled, busy = button.getAttribute('aria-busy');
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      try { await fn(); } catch (error) {
        const text = errorMessage(error);
        if (window.WisdomCompletionHost?.notify) window.WisdomCompletionHost.notify(text);
        else button.insertAdjacentElement('afterend', el('span', text, {role:'alert', class:'completion-error'}));
      } finally {
        pending = false;
        button.disabled = disabled;
        if (busy === null) button.removeAttribute('aria-busy'); else button.setAttribute('aria-busy', busy);
      }
    }); return button;
  };
  const fail = (root, error) => {
    root.replaceChildren(el('p', errorMessage(error), {role: 'alert', class: 'completion-error'}));
  };
  const empty = (root, text) => root.append(el('p', text, {class: 'completion-empty'}));

  const localizedEnum = (dictionary, value, unknownPrefix) => {
    if (value == null || value === '') return '';
    const raw = String(value);
    return dictionary.has(raw) ? dictionary.get(raw) : unknownPrefix + ': ' + raw;
  };
  const ROLE_LABELS = new Map([
    ['reader', 'Читатель'],
    ['contributor', 'Автор'],
    ['curator', 'Куратор'],
    ['manager', 'Менеджер']
  ]);
  const ITEM_KIND_LABELS = new Map([
    ['knowledge', 'Знание'],
    ['rule', 'Правило'],
    ['source', 'Источник']
  ]);
  const PUBLICATION_LABELS = new Map([
    ['draft', 'Черновик'],
    ['needs_review', 'Требует проверки'],
    ['published', 'Опубликовано'],
    ['rejected', 'Отклонено']
  ]);
  const MEMBER_STATUS_LABELS = new Map([
    ['active', 'активен'],
    ['blocked', 'заблокирован'],
    ['deactivated', 'деактивирован'],
    ['disabled', 'отключён'],
    ['inactive', 'неактивен'],
    ['removed', 'удалён'],
    ['suspended', 'приостановлен']
  ]);
  const INVITATION_STATUS_LABELS = new Map([
    ['accepted', 'принято'],
    ['cancelled', 'отменено'],
    ['declined', 'отклонено'],
    ['expired', 'истекло'],
    ['failed', 'ошибка'],
    ['pending', 'ожидает обработки'],
    ['queued', 'в очереди'],
    ['rejected', 'отклонено'],
    ['revoked', 'отозвано'],
    ['sent', 'отправлено']
  ]);
  const roleLabel = value => localizedEnum(ROLE_LABELS, value, 'неизвестная роль');
  const itemKindLabel = value => localizedEnum(ITEM_KIND_LABELS, value, 'неизвестный тип элемента');
  const publicationLabel = value => localizedEnum(PUBLICATION_LABELS, value, 'неизвестный статус публикации');
  const memberStatusLabel = value => localizedEnum(MEMBER_STATUS_LABELS, value, 'неизвестный статус участника');
  const invitationStatusLabel = value => localizedEnum(INVITATION_STATUS_LABELS, value, 'неизвестный статус приглашения');

  async function mountJobs(root, host) {
    root.replaceChildren(el('p', 'Загружаю задания…', {role: 'status'}));
    try {
      const jobs = await host.api('/api/jobs');
      const list = el('div', null, {class: 'completion-grid'});
      if (!jobs.length) empty(list, 'Заданий пока нет. Они появятся после обработки источника.');
      const statusLabels=new Map([
        ['queued','В очереди'],
        ['running','Обрабатывается'],
        ['done','Готово'],
        ['partial','Готово частично'],
        ['failed','Ошибка обработки'],
        ['needs_ocr','Требуется распознавание текста'],
        ['cancelled','Отменено']
      ]);
      const jobCard = job => {
        const box = card(job.title || 'Материал без названия');
        const status=statusLabels.get(job.status) || ('неизвестный статус ('+String(job.status)+')');
        box.append(el('p', 'Состояние: ' + status));
        const chunks=[];
        if (job.chunks_done != null) chunks.push('Готовые части: '+job.chunks_done);
        if (job.chunks_total != null) chunks.push('Всего частей: '+job.chunks_total);
        if (chunks.length) box.append(el('p', chunks.join(' · ')));
        if (job.error_code) box.append(el('p', 'Код ошибки: ' + job.error_code, {class: 'completion-error'}));
        if (job.created_at) box.append(el('p', 'Создано: ' + new Date(job.created_at).toLocaleString('ru-RU'), {class: 'muted'}));
        if (job.completed_at) box.append(el('p', 'Завершено: ' + new Date(job.completed_at).toLocaleString('ru-RU'), {class: 'muted'}));
        return box;
      };
      const paged = window.WisdomList && jobs.length ? window.WisdomList.create(host, {label: 'Страницы списка заданий', library: null, renderItem: jobCard}) : null;
      if (paged) { paged.set(jobs); list.append(paged.element, paged.navigation, paged.status); }
      else for (const job of jobs) list.append(jobCard(job));
      root.replaceChildren(action('Обновить список', () => mountJobs(root, host)), list);
    } catch (error) { fail(root, error); }
  }

  async function mountAccess(root, host) {
    const mountEpoch=++accessEpoch, routeToken=host.routeToken();
    const current=()=>mountEpoch===accessEpoch&&host.routeIsCurrent(routeToken)&&root.isConnected;
    root.replaceChildren(el('p', 'Загружаю доступ…', {role: 'status'}));
    try {
      const accountId = host.identity()?.account_id;
      if (!accountId) throw Error('В текущем профиле не найден идентификатор организации.');
      const [members, groups, invitations] = await Promise.all([
        host.api('/api/orgs/' + encodeURIComponent(accountId) + '/members'),
        host.api('/api/orgs/' + encodeURIComponent(accountId) + '/groups'),
        host.api('/api/orgs/' + encodeURIComponent(accountId) + '/invitations')
      ]);
      if(!current())return;
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
        for (const value of ['reader', 'contributor', 'curator', 'manager']) select.append(el('option', roleLabel(value), {value}));
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
        const label = [member.display_name, member.email, memberStatusLabel(member.status)].filter(Boolean).join(' · ') || member.principal_id;
        memberLine.append(el('p', label));
        if (member.status === 'active') {
          const deactivate = el('details'); deactivate.append(el('summary', 'Деактивировать участника'));
          const form = el('form', null, {class: 'completion-form'}), reason = field('Причина (необязательно)', 'reason');
          const confirmed = field('Подтверждаю блокировку участника и отзыв его активных сессий', 'confirmed', 'checkbox');
          confirmed.input.required = true;
          const submit = el('button', 'Деактивировать', {type: 'submit'});
          form.append(reason.wrap, confirmed.wrap, submit);
          window.WisdomForms.submit(form, async event => {
            event.preventDefault();
            submit.disabled = true;
            try {
              const result = await host.post('/api/orgs/' + encodeURIComponent(accountId) + '/members/' + encodeURIComponent(member.principal_id) + '/deactivate', {reason: reason.input.value.trim() || null});
              host.notify(result.revocation_outstanding ? 'Участник заблокирован; отзыв сессии ожидает оператора.' : 'Участник заблокирован, отзыв сессии подтверждён.');
              await mountAccess(root, host);
            } catch (error) { window.WisdomForms.formError(form, errorMessage(error)); } finally { submit.disabled = false; }
          });
          deactivate.append(form); memberLine.append(deactivate);
        }
        return memberLine;
      };
      const memberList = window.WisdomList && members.length ? window.WisdomList.create(host, {label: 'Страницы списка участников', library: null, renderItem: memberItem}) : null;
      if (memberList) { memberList.set(members); memberBox.append(memberList.element, memberList.navigation, memberList.status); }
      else for (const member of members) memberBox.append(memberItem(member));
      const inviteBox = card('Приглашения');
      if (!invitations.length) empty(inviteBox, 'Ожидающих приглашений нет.');
      for (const item of invitations) inviteBox.append(el('p', [item.email, item.subject, invitationStatusLabel(item.status)].filter(Boolean).join(' · ') || 'Приглашение'));
      const inviteForm = el('form', null, {class: 'completion-form'});
      const email = field('Электронная почта (необязательно)', 'email', 'email');
      const subject = field('Идентификатор subject (необязательно)', 'subject');
      email.input.maxLength = 320; subject.input.maxLength = 255;
      const submit = el('button', 'Записать приглашение', {type: 'submit'});
      inviteForm.append(email.wrap, subject.wrap, el('p', 'Это только запись в очереди оператора; письмо не отправляется.', {class: 'muted'}), submit);
      const bothInvitationFieldsEmpty = () => !email.input.value.trim() && !subject.input.value.trim();
      const clearInvitationErrors = () => window.WisdomForms.clearErrors(inviteForm, [email.input, subject.input]);
      email.input.addEventListener('input', clearInvitationErrors);
      subject.input.addEventListener('input', clearInvitationErrors);
      window.WisdomForms.submit(inviteForm, async event => {
        event.preventDefault();
        if (bothInvitationFieldsEmpty()) {
          window.WisdomForms.showErrors(inviteForm, [
            {control: email.input, message: 'Укажите почту или subject.', active: bothInvitationFieldsEmpty},
            {control: subject.input, message: 'Укажите почту или subject.', active: bothInvitationFieldsEmpty}
          ]);
          email.input.focus();
          return;
        }
        clearInvitationErrors();
        submit.disabled = true;
        try {
          await host.post('/api/orgs/' + encodeURIComponent(accountId) + '/invitations', {email: email.input.value.trim() || null, subject: subject.input.value.trim() || null});
          await mountAccess(root, host);
        } catch (error) { window.WisdomForms.formError(inviteForm, errorMessage(error)); } finally { submit.disabled = false; }
      });
      inviteBox.append(inviteForm);
      const groupBox = card('Группы');
      if (!groups.length) empty(groupBox, 'Групп пока нет.');
      const groupCards = [];
      for (const group of groups) {
        const groupCard = card(group.name);
        const membershipForm = el('form', null, {class: 'completion-form'});
        const memberSelect = selectField(membershipForm, 'Участник для добавления в группу', 'principal_id', activeMembers, 'principal_id', member => [member.display_name, member.email].filter(Boolean).join(' · ') || member.principal_id);
        memberSelect.required = true;
        memberSelect.addEventListener('change', () => window.WisdomForms.clearErrors(membershipForm, [memberSelect]));
        const add = el('button', 'Добавить участника', {type: 'submit'}); membershipForm.append(add);
        window.WisdomForms.submit(membershipForm, async () => {
          if (!memberSelect.value) return;
          add.disabled = true;
          try { await host.api('/api/orgs/' + encodeURIComponent(accountId) + '/groups/' + encodeURIComponent(group.id) + '/members/' + encodeURIComponent(memberSelect.value), {method: 'PUT'}); host.notify('Участник добавлен в группу.'); await mountAccess(root, host); }
          catch (error) { window.WisdomForms.formError(membershipForm, errorMessage(error)); } finally { add.disabled = false; }
        });
        const removeForm = el('form', null, {class: 'completion-form'});
        const removeMember = selectField(removeForm, 'Участник для удаления из группы', 'principal_id', activeMembers, 'principal_id', member => [member.display_name, member.email].filter(Boolean).join(' · ') || member.principal_id);
        removeMember.required = true;
        const explainLibrary = libraries.length ? selectField(removeForm, 'Проверить оставшийся доступ к библиотеке (необязательно)', 'library_id', libraries, 'id', item => item.name || item.id) : null;
        const remove = el('button', 'Убрать из группы', {type: 'submit'}); removeForm.append(remove);
        removeForm.append(el('p', 'Показываются активные участники организации. API не предоставляет отдельный список состава группы; отсутствие участника будет показано как отказ API.', {class: 'muted'}));
        let removePending = false;
          window.WisdomForms.submit(removeForm, async event => {
            event.preventDefault();
            if (removePending || !removeMember.value) return;
            const memberId = removeMember.value, libraryId = explainLibrary?.value || '';
            const memberLabel = removeMember.selectedOptions[0]?.textContent || memberId;
            const groupLabel = group.name || group.id;
            const actionEpoch=accessEpoch, actionRouteToken=host.routeToken();
            const actionCurrent=()=>actionEpoch===accessEpoch&&host.routeIsCurrent(actionRouteToken)&&root.isConnected;
            const disabled = remove.disabled, busy = remove.getAttribute('aria-busy');
            removePending = true;
            remove.disabled = true;
            remove.setAttribute('aria-busy', 'true');
            try {
              const confirmed = await host.confirm({
                title: 'Убрать участника из группы',
                description: 'Убрать «' + memberLabel + '» из группы «' + groupLabel + '»? Доступ через эту группу перестанет предоставляться. Прямые разрешения участника и доступ через другие группы сохранятся.',
                confirmLabel: 'Убрать из группы'
              });
              if (!confirmed || !actionCurrent()) return;
              await host.api('/api/orgs/' + encodeURIComponent(accountId) + '/groups/' + encodeURIComponent(group.id) + '/members/' + encodeURIComponent(memberId), {method: 'DELETE'});
              if (!actionCurrent()) return;
              let outcome = 'Участник удалён из группы.';
              if (libraryId) {
                try {
                  const detail = await host.api('/api/orgs/' + encodeURIComponent(accountId) + '/access/' + encodeURIComponent(memberId) + '/libraries/' + encodeURIComponent(libraryId));
                  if(!actionCurrent())return;
                  const paths = detail.paths.map(path => path.kind === 'direct' ? 'прямое разрешение ' + roleLabel(path.role) : 'группа ' + (path.group_name || path.group_id) + ' · ' + roleLabel(path.role));
                  outcome += ' После изменения: ' + (detail.denied ? 'доступ отсутствует (' + detail.reason + ')' : 'эффективная роль ' + (roleLabel(detail.effective_role) || 'не указана')) + (paths.length ? '; пути: ' + paths.join(', ') : '; активных путей нет') + ' (ревизия ' + detail.policy_revision + ').';
                } catch (error) { if (actionCurrent()) outcome += ' Проверка оставшегося доступа не удалась: ' + errorMessage(error); }
              }
              if (!actionCurrent()) return;
              host.notify(outcome);
              await mountAccess(root, host);
            } catch (error) {
              if (actionCurrent()) window.WisdomForms.formError(removeForm, errorMessage(error));
            } finally {
              removePending = false;
              remove.disabled = disabled;
              if (busy === null) remove.removeAttribute('aria-busy'); else remove.setAttribute('aria-busy', busy);
            }
          });
        groupCard.append(membershipForm, removeForm);

        const grantsPath = '/api/orgs/' + encodeURIComponent(accountId) + '/groups/' + encodeURIComponent(group.id) + '/grants';
        try {
          const grants = await host.api(grantsPath);
          if(!current())return;
          if (!grants.length) empty(groupCard, 'Разрешений группы на библиотеки пока нет.');
          for (const grant of grants) {
            const library = libraries.find(item => item.id === grant.library_id);
            const row = el('p', (library?.name || grant.library_id) + ' · ' + roleLabel(grant.role));
            const revoke = action('Отозвать разрешение', async () => {
              const libraryLabel = library?.name || grant.library_id, groupLabel = group.name || group.id;
              const actionEpoch=accessEpoch, actionRouteToken=host.routeToken();
              const actionCurrent=()=>actionEpoch===accessEpoch&&host.routeIsCurrent(actionRouteToken)&&root.isConnected;
              try {
                const confirmed = await host.confirm({
                  title: 'Отозвать разрешение группы',
                  description: 'Отозвать роль «' + roleLabel(grant.role) + '» группы «' + groupLabel + '» для библиотеки «' + libraryLabel + '»? Доступ через эту группу для этой библиотеки перестанет предоставляться. Прямые разрешения участников и доступ через другие группы могут сохранить доступ.',
                  confirmLabel: 'Отозвать разрешение'
                });
                if (!confirmed || !actionCurrent()) return;
                await host.api(grantsPath + '/' + encodeURIComponent(grant.library_id), {method: 'DELETE'});
                if (!actionCurrent()) return;
                await mountAccess(root, host);
              } catch (error) { if (actionCurrent()) throw error; }
            }, 'secondary');
            row.append(document.createTextNode(' · '), revoke); groupCard.append(row);
          }
        } catch (error) { groupCard.append(el('p', error.message, {role: 'alert', class: 'completion-error'})); }
        if (libraries.length) {
          const grantForm = el('form', null, {class: 'completion-form'});
          const librarySelect = selectField(grantForm, 'Библиотека для группы', 'library_id', libraries, 'id', item => item.name || item.id);
          librarySelect.required = true;
          const role = roleField(grantForm); const save = el('button', 'Назначить роль группе', {type: 'submit'}); grantForm.append(save);
          window.WisdomForms.submit(grantForm, async event => {
            event.preventDefault(); if (!librarySelect.value) return;
            save.disabled = true;
            try { await host.api(grantsPath + '/' + encodeURIComponent(librarySelect.value), {method: 'PUT', body: JSON.stringify({role: role.value})}); host.notify('Роль группы сохранена.'); await mountAccess(root, host); }
            catch (error) { window.WisdomForms.formError(grantForm, errorMessage(error)); } finally { save.disabled = false; }
          });
          groupCard.append(grantForm);
        }
        groupCards.push(groupCard);
      }
      const groupList = window.WisdomList && groupCards.length ? window.WisdomList.create(host, {label: 'Страницы списка групп', library: null, renderItem: built => built}) : null;
      if (groupList) { groupList.set(groupCards); groupBox.append(groupList.element, groupList.navigation, groupList.status); }
      else for (const groupCard of groupCards) groupBox.append(groupCard);
      const groupForm = el('form', null, {class: 'completion-form'});
      const groupName = field('Название группы (латиница, цифры, _ или -)', 'name'); groupName.input.pattern = '[a-z][a-z0-9_-]{1,63}'; groupName.input.required = true;
      const create = el('button', 'Создать группу', {type: 'submit'}); groupForm.append(groupName.wrap, create);
      window.WisdomForms.submit(groupForm, async event => {
        event.preventDefault(); create.disabled = true;
        try { await host.post('/api/orgs/' + encodeURIComponent(accountId) + '/groups', {name: groupName.input.value}); await mountAccess(root, host); }
        catch (error) { window.WisdomForms.formError(groupForm, errorMessage(error)); } finally { create.disabled = false; }
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
          if(!current())return;
          const manager = policy.effective_role === 'manager';
          if (!grants.length) empty(libraryCard, 'Видимых прямых разрешений нет.');
          for (const grant of grants) {
            const member = members.find(item => item.principal_id === grant.principal_id);
            const row = el('p', [member?.display_name || grant.principal_id, member?.email, roleLabel(grant.role)].filter(Boolean).join(' · '));
            if (manager) row.append(document.createTextNode(' · '), action('Отозвать', async () => {
              const libraryLabel = library.name || library.id;
              const memberLabel = [member?.display_name, member?.email].filter(Boolean).join(' · ') || grant.principal_id;
              const actionEpoch=accessEpoch, actionRouteToken=host.routeToken();
              const actionCurrent=()=>actionEpoch===accessEpoch&&host.routeIsCurrent(actionRouteToken)&&root.isConnected;
              try {
                const confirmed = await host.confirm({
                  title: 'Отозвать прямое разрешение',
                  description: 'Отозвать роль «' + roleLabel(grant.role) + '» участника «' + memberLabel + '» на библиотеку «' + libraryLabel + '»? Прямой доступ перестанет предоставляться; доступ через группы может сохраниться.',
                  confirmLabel: 'Отозвать разрешение'
                });
                if (!confirmed || !actionCurrent()) return;
                await host.api('/api/libraries/' + encodeURIComponent(library.id) + '/grants/' + encodeURIComponent(grant.principal_id), {method: 'DELETE'});
                if (!actionCurrent()) return;
                await mountAccess(root, host);
              } catch (error) { if (actionCurrent()) throw error; }
            }, 'secondary'));
            libraryCard.append(row);
          }
          libraryCard.append(el('p', 'Действующая роль с учётом прямых и групповых разрешений: ' + (roleLabel(policy.effective_role) || 'нет доступа'), {class: 'muted'}));
          if (manager) {
            const form = el('form', null, {class: 'completion-form'});
            const memberSelect = selectField(form, 'Участник', 'principal_id', activeMembers, 'principal_id', member => [member.display_name, member.email].filter(Boolean).join(' · ') || member.principal_id);
            memberSelect.required = true;
            memberSelect.addEventListener('change', () => window.WisdomForms.clearErrors(form, [memberSelect]));
            const role = roleField(form); const save = el('button', 'Сохранить прямую роль', {type: 'submit'}); form.append(save);
            window.WisdomForms.submit(form, async () => {
              if (!memberSelect.value) return;
              save.disabled = true;
              try { await host.api('/api/libraries/' + encodeURIComponent(library.id) + '/grants', {method: 'PUT', body: JSON.stringify({principal_id: memberSelect.value, role: role.value})}); host.notify('Прямое разрешение сохранено.'); await mountAccess(root, host); }
              catch (error) { window.WisdomForms.formError(form, errorMessage(error)); } finally { save.disabled = false; }
            });
            libraryCard.append(form);
          } else libraryCard.append(el('p', 'Менять прямые разрешения может только менеджер этой библиотеки.', {class: 'muted'}));
        } catch (error) { libraryCard.append(el('p', error.message, {role: 'alert', class: 'completion-error'})); }
        directBox.append(libraryCard);
      }
      if (libraries.length) {
        const explain = el('form', null, {class: 'completion-form'});
        const member = selectField(explain, 'Проверить участника', 'principal_id', members, 'principal_id', item => [item.display_name, item.email, memberStatusLabel(item.status)].filter(Boolean).join(' · ') || item.principal_id);
        const library = selectField(explain, 'Библиотека', 'library_id', libraries, 'id', item => item.name || item.id);
        member.required = true; library.required = true;
        const lookup = el('button', 'Показать пути доступа', {type: 'submit'}), result = el('div', null, {class: 'completion-access-result'});
        explain.append(lookup, result);
        window.WisdomForms.submit(explain, async event => {
          event.preventDefault(); if (!member.value || !library.value) return;
          lookup.disabled = true; result.replaceChildren(el('p', 'Проверяю действующие пути…', {role: 'status'}));
          try {
            const detail = await host.api('/api/orgs/' + encodeURIComponent(accountId) + '/access/' + encodeURIComponent(member.value) + '/libraries/' + encodeURIComponent(library.value));
            result.replaceChildren(el('p', (detail.denied ? 'Доступ отсутствует: ' + detail.reason : 'Доступ разрешён') + ' · эффективная роль: ' + (roleLabel(detail.effective_role) || 'нет доступа') + ' · ревизия политики: ' + detail.policy_revision));
            if (!detail.paths.length) result.append(el('p', 'Активных путей доступа нет.'));
            for (const path of detail.paths) result.append(el('p', path.kind === 'direct' ? 'Прямое разрешение · ' + roleLabel(path.role) : 'Группа ' + (path.group_name || path.group_id) + ' · ' + roleLabel(path.role)));
          } catch (error) { window.WisdomForms.formError(explain, errorMessage(error)); result.replaceChildren(el('p', errorMessage(error), {role: 'alert', class: 'completion-error'})); }
          finally { lookup.disabled = false; }
        });
        directBox.append(explain);
      }
      if(!current())return;
      content.append(memberBox, inviteBox, groupBox, directBox);
      root.replaceChildren(action('Обновить доступ', () => mountAccess(root, host)), content);
    } catch (error) { if (current()) fail(root, error); }
  }

  async function enhanceLibrary(root, library, host) {
    const routeToken = host.routeToken();
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
      policyCard.append(el('p', 'Роль: ' + (roleLabel(policy.effective_role) || 'не указана')));
      const form = el('form', null, {class: 'completion-form'});
      for (const [key, label] of names) {
        const row = field(label, key, 'checkbox'); row.input.checked = Boolean(policy[key]); row.input.disabled = !manager;
        row.wrap.classList.add('completion-check'); form.append(row.wrap);
      }
      policyCard.append(el('p', 'Локальный поиск продолжит работать при выключенных облачных и внешних обработчиках. Генерация изначально выключена.', {class: 'muted'}));
      if (manager) {
        const save = el('button', 'Сохранить ограничения', {type: 'submit'}); form.append(save);
        window.WisdomForms.submit(form, async event => {
          event.preventDefault(); save.disabled = true;
          try {
            const payload = Object.fromEntries(names.map(([key]) => [key, form.elements[key].checked]));
            const next = await host.api('/api/libraries/' + encodeURIComponent(library.id) + '/policy', {method: 'PUT', body: JSON.stringify(payload)});
            if (!host.currentLibrary() || host.currentLibrary().id !== library.id) return;
            host.notify('Ограничения сохранены.');
            if (host.refreshLibrary) await host.refreshLibrary(); else await enhanceLibrary(root, library, host);
          } catch (error) { window.WisdomForms.formError(form, errorMessage(error)); } finally { save.disabled = false; }
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
        label.append(el('span', (item.title || item.statement || 'Без названия') + ' · ' + itemKindLabel(item.item_kind) + ' · ' + publicationLabel(item.publication)));
        review.append(label);
        if (item.item_kind === 'knowledge') {
          const canEdit = canReview || Boolean(item.created_by && item.created_by === host.identity()?.principal_id);
          const editor = el('details'); editor.append(el('summary', canEdit ? 'Редактировать текст и свидетельство' : 'Свидетельства'));
          const provenance = item.provenance || [];
          if (!provenance.length) editor.append(el('p', 'Связанные свидетельства не указаны.', {class: 'muted'}));
          for (const proof of provenance) {
            const line = el('p', [proof.quote, proof.locator?.chapter, proof.locator?.file_page ? 'Страница ' + proof.locator.file_page : null, proof.fragment_id].filter(Boolean).join(' · ') || 'Фрагмент источника');
            const locator = proof.locator && typeof proof.locator === 'object' ? proof.locator : {};
            const hasExactTarget = Boolean(
              (typeof proof.quote === 'string' && proof.quote.length > 0) ||
              (proof.fragment_id != null && String(proof.fragment_id).length > 0) ||
              ['chapter', 'file_page', 'printed_label', 'paragraph', 'table_index', 'cell_row', 'cell_column'].some(key => locator[key] != null)
            );
            const sourceOptions = {
              quote: proof.quote,
              locator: proof.locator,
              fragmentId: proof.fragment_id,
              isCurrent: () => host.routeIsCurrent(routeToken) && host.currentLibrary()?.id === library.id && area.isConnected
            };
            if (typeof proof.source_id === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(proof.source_id)) line.append(document.createTextNode(' · '), action(hasExactTarget ? 'Открыть точное место' : 'Открыть источник', () => host.openSource(proof.source_id, sourceOptions)));
            editor.append(line);
          }
          if (canEdit) {
            const form = el('form', null, {class: 'completion-form'});
            form.dataset.draftKey = uuid.test(String(item.id || '')) ? 'knowledge:' + library.id + ':' + item.id : '';
            const text = el('textarea', null, {name: 'statement', 'aria-label': 'Текст черновика'});
            text.value = item.statement || '';
            const actions = el('div');
            const save = el('button', 'Сохранить текст', {type: 'submit'});
            const cancel = el('button', 'Отмена', {type: 'button'});
            cancel.addEventListener('click', () => {
              window.WisdomForms.discardDraft(form);
              editor.remove();
            });
            actions.append(save, cancel);
            form.append(text, actions);
            window.WisdomForms.submit(form, async () => {
              editor.querySelector('[data-knowledge-save-error]')?.remove();
              try {
                await host.api('/api/knowledge/' + encodeURIComponent(item.id), {method: 'PATCH', body: JSON.stringify({statement: text.value, expected_statement: item.statement, kind: item.kind})});
              } catch (error) {
                const detail = error?.message ? ': ' + error.message : '.';
                editor.append(el('p', 'Черновик не сохранён' + detail, {role: 'alert', 'data-knowledge-save-error': 'true'}));
                return;
              }
              window.WisdomForms.discardDraft(form);
              if (!host.currentLibrary() || host.currentLibrary().id !== library.id) return;
              try {
                await enhanceLibrary(root, library, host);
              } catch (error) {
                if (!host.currentLibrary() || host.currentLibrary().id !== library.id) return;
                const detail = error?.message ? ': ' + error.message : '';
                host.notify('Черновик обновлён, но список не обновился' + detail + '. Обновите страницу.');
                return;
              }
              if (host.currentLibrary()?.id === library.id) host.notify('Черновик обновлён.');
            });
            editor.append(form);
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
          try {
            await enhanceLibrary(root, library, host);
          } catch (error) {
            if (!host.currentLibrary() || host.currentLibrary().id !== library.id) return;
            const detail = error?.message ? ': ' + error.message : '';
            host.notify('Решение сохранено, но список не обновился' + detail + '. Обновите страницу.');
            return;
          }
          if (host.currentLibrary()?.id === library.id) host.notify('Решение сохранено.');
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
      for (const entry of entries) journal.append(el('p',(entry.title ?? 'Материал') + ' · ' + itemKindLabel(entry.item_kind) + (entry.version_no ? ' · v' + entry.version_no : '') + ' · ' + entry.delivered_at));
      area.append(journal);
    } catch (error) { fail(area, error); }
  }

  window.WisdomCompletion = {mountAccess, mountJobs, enhanceLibrary, enhanceUses};
})();
