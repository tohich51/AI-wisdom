# Параллельная работа и волны

Правило распределения, не сообщение о запуске feature/cloud задач. Актуальные deployed/candidate HEAD и статус устанавливаются по receipts — сводка с датами в [TEST-MATRIX.md](TEST-MATRIX.md); историческому README не доверять.

## Очередь и приёмка

- До трёх независимых направлений: backend, UI существующих API, качество/документация. Для большой волны заранее держать три готовых пакета и два buffer; для малого остатка работу ради числа исполнителей не создавать.
- Готовый пакет: цель, exact base SHA, workspace, allowed files/owner, принятые зависимости и frozen interfaces, точные checks, ожидаемый артефакт, leases.
- Пока один пакет работает, исправляется или ждёт приёмки/ресурса, root запускает, готовит или принимает другой готовый. Блокер касается только своей задачи. Не перечитывать весь проект и не перепланировать принятую работу.
- В этой волне до root integration обязательны внутренний MiniMax review и независимый Sol 6.1 medium на xorek. Дополнительный reviewer только для указанного нерешённого риска или обязательного контракта. После фикса перепроверяется изменённый сценарий и связанные риски.
- Focused checks при правке; полный обязательный gate — на конечном exact SHA перед release. Новый кандидат, ошибка или неизвестный риск инвалидируют относящееся evidence.
- Ожидание события — только после исчерпания полезной независимой работы в текущем scope. Status-only model calls запрещены. Метрики очереди, выполнения, ресурса, приёмки и фиксов разделять; неизвестные значения — null.
- Новая сессия начинается с карточки/receipt/source. Лестница исправлений: две self-fix попытки, затем максимум две дополнительные с реально полученным primary internet-контекстом и расширенными skills, затем сохранение результата или точного blocker (правила — в [FAST-DEVELOPMENT.md](FAST-DEVELOPMENT.md), п. 11–12).

## Ресурсы и владельцы

- Capacity относится к конкретному provider/account/host/workload; неизвестный лимит проверяется bounded probe, а не блокирует pipeline. При `429`/перегрузке снижается только затронутый ресурс.
- Bounded documentation probe описан в [parallel-capacity-2026-10-15.json](parallel-capacity-2026-10-15.json); его результат не является production load benchmark или бессрочной квотой.
- Network-only coding requests и CPU-heavy jobs — разные ресурсы. Проверенные малые coding calls группируются в одну серверную allocation. На общем xorek одна heavy allocation, одна product generation, один OpenViking writer. Production locks не удаляются. Merge и release — root.
- Изолировать worktrees, файлы, браузер, БД, test users, build directories. Один owner у migrations/contracts/locks/auth, общего `static/app.js` и `tests/runtime/test_runtime.py`. Общие интерфейсы согласует root; serial merge сохраняется, остальные независимые пакеты продолжают execution.
- MiniMax остаётся на существующей OAuth-подписке. Для Luna/Cloud сначала проверить конкретные модель, подписку, инструменты и workspace. Не переносить owner secrets, не подменять модель, не выдавать review-agent за Cloud executor.
- Ресурсное ожидание отмечать `resource_wait` с точной причиной и следующим шагом; root берёт следующую или buffer карточку. Отдельный ограниченный retry исправления не запускает новое исследование или общий reviewer chain.

## Исторические примеры волн · 15.10.2026

Ниже — примеры разбиения работ, а не актуальная очередь. Принятые функции не запускаются повторно. Текущий остаток назначается по свежей feature-board и exact-SHA receipts.

### Волна 0 — снять повторную подготовку

Root сверяет candidate `da8796` против receipts (сводка — в [TEST-MATRIX.md](TEST-MATRIX.md)), фиксирует стабильные API для следующей волны и обновляет карточку состояния. Используются существующие venv/locks, Procrastinate/Keycloak/OpenViking SDK; новый оркестратор и переписывание интерфейса только ради harness запрещены. Root разрешает конкретную задачу, перечисляет её resource leases и checks.

### Волна 1 — три независимые задачи

| Направление | Исполнитель | Допустимые файлы | Результат и приёмка |
|---|---|---|---|
| A: workflow review/знания/опыт | MiniMax, один backend owner | Назначенная часть `catalog/writes.py`, `http/content.py`, новый изолированный regression file; общие schemas только root | Один завершённый сценарий (например, отклонение черновика); real PG positive/negative, стабильный API |
| B: формы существующих API | Luna с проверенными инструментами | `static/app.js` единственный owner; нужные `index.html`/`style.css` только при резервировании | Одна форма URL либо membership/grants; node syntax + browser positive/error/denied; новая API-зависимость блокируется до контракта root |
| C: качество/документация | Luna или второй MiniMax с resource-scoped capacity evidence | Новый `evals/retrieval/*.json`, собственный `docs/workflow/*` документ, отдельные synthetic fixtures | Предложение корпуса из 50 answerable запросов (отдельно от принятых 19 поисковых вызовов C34) с relevance/citation expectations; schema/provenance review, без provider calls и shared runtime test edits |

Пути `evals/` и новые тесты — предложенные новые файлы, не существующая инфраструктура. B может работать с уже существующими API одновременно с A; UI для нового A endpoint начинает интеграцию после root contract freeze. `app.js` нельзя отдать сразу нескольким Luna; при необходимости root один раз создаёт небольшие согласованные модули и обновляет leases. Расширение до 50 вопросов не является обязательным повтором принятого C34.

### Волна 2 — собрать продуктовые сценарии

- MiniMax: требования опыта (критерий успеха, evidence/assessor, разрешённые агрегаты) и брендовый context. Автоматический результат не выдумывать: агент явно вызывает start_use/record_outcome.
- Luna: UI для уже принятого API, доступов, project pins и review.
- Root: реальное прохождение книга→draft→publication→search→exact rule→outcome с изоляцией двух identities. Один интеграционный прогон, не по одному на автора.

### Волна 3 — эксплуатационная приёмка

План полного restore (одноразовый проверенный C32 не создаёт permanent teststand), missing-case mapping A01–A20 / S01–S04 и устранение выявленных source/evidence пробелов 42-card по независимому Sol audit; native latency измерять с атрибуцией компонента, а не сближением чисел gateway и tool. По очереди выполнять restore, real service tests и benchmark. Проверить gateway и worker до увеличения concurrency. Root завершает exact-SHA bundle, разрешённый release и независимый MCP readback; GitHub publication — отдельный scope.

Состояния: `planned → ready → running → review → accepted`. `blocked` требует точной недостающей зависимости; другие lanes продолжаются. Каждый executor сдаёт checks, unresolved и следующий шаг; root меняет общую доску. Принятый patch не равен развёрнутой функции.
