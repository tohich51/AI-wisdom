# Проверки по изменению

Правила сокращают повторные прогоны; существующий набор не удаляется по числу тестов. Точные test names выбирает root через scoped `rg`/`pytest --collect-only`. `<...>` — параметры задания, не готовые shell-аргументы. Пустой `-k`, deselected и skipped не являются доказательством поведения.

## Датированный статус (2026-10-20)

Пути ниже — приватные операторские receipts на конкретном хосте `/home/kb-inspect`; они относятся к этому хосту и не являются переносимыми файлами в Git.

- Deployed production SHA `65a6eb9b`: gate 1554 tests, 139.54 s. Receipt: `/home/kb-inspect/ai-wisdom-handoff/architecture-20261015/results/citation-load-wave-20261020/FINAL-WAVE/release/gate-65a6eb9b-r2/receipt.json`.
- Candidate SHA `da8796` — потомок deployed `65a6eb9b`, не наоборот. Candidate не развёрнут: его evidence не выдаётся за production-tested.
- Acceptance matrix: 24 из 24 supported сценариев в пределах scope оригинальных A01–A20 и S01–S04, с сохранением каждого case-specific evidence limitation. Это не полная 42-card матрица и не native cold-latency acceptance. Файл: `/home/kb-inspect/ai-wisdom-handoff/architecture-20261015/results/citation-load-wave-20261020/acceptance-matrix-current.json`.
- C32 restore: один одноразовый exact manifest/schema/code-compatible restore выполнен и удалён; permanent teststand не создан. Файл: `/home/kb-inspect/ai-wisdom-handoff/architecture-20261015/results/citation-load-wave-20261020/C32/acceptance.json`.
- C07 candidate: реальный Authlib generic OAuth2Client переиспользует существующий HTTPx; пройдены 33 browser flow и 1 реальный существующий Keycloak flow, независимый Sol прошёл. Не развёрнут.
- Runtime: канонические 8 container units = 7 permanent + 1 provisioning oneshot, запускаемый timer'ом; timer не является container'ом. Проверено реальным Quadlet dryrun без изменения live services, Sol прошёл.
- Concept similarity overlay реализован и принят native 90/90; формулировка «ждёт собственный индекс» больше не является текущим статусом.
- C34: приняты 19 поисковых вызовов по трём фиксированным запросам в явно указанных фазах. Это не 19 различных эталонных вопросов. Более позднее предложение корпуса из 50 вопросов — отдельное расширение, не обязательный повтор C34.
- Открыто: Sol завершил аудит 42 карточек и 161 нумерованного пункта на da879608: 31 supported, 7 needs_work (включая два унаследованных URL-дефекта), 1 unverified, 3 awaiting_release. Это source/evidence audit; актуальные исправления ещё не приняты и не выпущены. Исходный отчёт: /home/kb-inspect/ai-wisdom-handoff/architecture-20261015/results/original-plan-reconciliation-20261020/CARDS-42/sol-audit/verdict.json. Native first MCP 1–2 s не закрыт: последнее измерение 4.537 s на границе tool против 0.992 s gateway, компонент вне gateway не атрибутирован.
- Исторические замеры (639 тестов за 47.77 s на `1fc0e5f`; неразвёрнутый cloud-dev `45c4af5`) — прошлое измерение одной среды, не baseline и не SLA.

## Во время реализации

Все команды — в назначенном checkout с подготовленной `.venv`. Актуальность `justfile`/pyproject и наличие нужного сервиса проверяются; окружение готовится один раз на версию lockfiles и toolchain. Зависимости не переустанавливаются в каждой задаче, локальный runner не подменяется.

| Изменение | Минимальная проверка | Когда расширить |
|---|---|---|
| Документация/правила | `git diff --check`, JSON parse, ссылки и фактические пути | Изменена команда/контракт: проверить entrypoint |
| Python функция | `PYTHONPATH=src .venv/bin/ruff check <changed.py>`, format check, focused regression | Общий интерфейс: зависимые тесты + mypy |
| Embedding/reader/writer | focused существующие тесты embedding/vector по `pytest -q <files>` | Provider/profile/SDK: один реальный canary владельцем интеграции |
| Семантическая проекция | `PYTHONPATH=src .venv/bin/pytest -q tests/integration/provisioning/test_semantic_projection.py` | Реальная OpenViking граница: canary, закрытый scope, revoke, restart |
| SQL/RLS/пользователи | Релевантные файлы из `tests/integration/rls/`, `members/`, `libraries/` | Всегда настоящая PostgreSQL и хотя бы один отказ доступа; общий DDL тянет зависимые модули |
| Загрузка/URL | `PYTHONPATH=src .venv/bin/pytest -q tests/integration/upload/` | Изменён parser/queue: релевантные `tests/runtime/test_runtime.py -k ...` |
| Очередь/версии/опыт | `PYTHONPATH=src .venv/bin/pytest -q tests/runtime/test_runtime.py -k '<selected_existing_tests>'` | Concurrency/запись: реальная PostgreSQL, retry/revoke/idempotency, поздний ответ |
| Identity/session | `PYTHONPATH=src .venv/bin/pytest -q tests/integration/identity/test_session_store.py tests/integration/identity/test_token_validation.py` | Изменён реальный OIDC/logout: владельческий live Keycloak gate |
| UI JS | `node --check src/kb/static/app.js` (и изменённых JS), один сценарий в браузере | Запись: успешная операция + отказ прав/ошибка; синтаксис не доказывает UX |
| CSS/HTML | Открыть затронутый экран, малый и широкий viewport, действия/ошибки | Изменена навигация/auth: соответствующий браузерный сценарий |

Новый модуль сначала должен реально появиться. Обычная UI форма не требует десятков unit tests реализации DOM. Новый тест нужен для новой регрессии, границы данных или непокрытого риска. Одну причину отказа объединять параметризацией; разные гарантии не объединять так, чтобы исчезло наблюдаемое доказательство. Удаление дубликата — только отдельным root diff-review с названием сохранившегося эквивалентного теста.

## Интеграция и выпуск

1. Root собирает совместимые accepted patches в один кандидат; на границе волны выполняется один набор зависимых интеграционных проверок.
2. Перед выпуском: чистый exact SHA, `just check`, требуемый PostgreSQL и live stack gate, архитектура `linux/amd64`, image/source hashes.
3. Доступные recipes проверяются по реальному `justfile` (`prepare`, `check`, `integration`, `acceptance`, `package`, `verify-self`, `env`). Другой justfile кандидата не считается принятым; локальный `just integration` не заменяет provider leg. Обязательный gate, который не может выполниться, обязан выйти nonzero — зелёный receipt непроведённой проверки хуже красного.
4. Acceptance: сопоставлять каждый сценарий с реальным evidence и писать только отсутствующие проверки; не дублировать уже доказанные свойства.
5. Разрешённый rollout: согласованный backup, один release owner, rollback, независимый readiness/MCP/UI readback. Успешный backup не заменяет restore.

Focused dev evidence переиспользуется только при неизменных tested files, зависимостях, DDL, contracts и среде. Release evidence привязано к полному SHA/build inputs. Повтор полного прогона нужен после новой правки, сбоя или неразрешённого риска, а не из-за подключения ещё одного агента.
