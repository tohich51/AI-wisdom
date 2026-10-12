# Параллельная работа и ближайшие волны

Это исполнимый план распределения, не сообщение о запуске cloud задач.
Не менять работающий runtime ради внедрения правил. В каждой сессии сверить
cloud и deployed HEAD: на 12 октября deployed `1fc0e5f`, cloud `45c4af5`.
Новые cloud auth tests не приняты автоматически и не должны потеряться.

## Capacity и владение

- Целевой WIP — до трёх независимых coding задач после проверки реальных
  лимитов MiniMax/Codex и RAM/CPU. До подтверждения capacity запускать одну;
  исторические пять CMS slots не являются лимитом AI-wisdom.
- Допустимы отдельные лёгкие локальные проверки. На общем xorek одновременно
  не больше одного тяжёлого PG/full-suite/live-fixture/build/restore задания.
- Coding parallelism не повышает product model-runner concurrency: остаётся
  одна генерация и один OpenViking writer. Cloud Luna не получает root/секреты.
- Root фиксирует leases для файлов и ресурсов в task packets. Изолированный
  worktree не отменяет коллизии общей БД, браузера, test users и provider quota.
- `migrations/`, contracts, `pyproject.toml`, locks, auth, gateway/MCP, общий
  `static/app.js` и `tests/runtime/test_runtime.py` — по одному владельцу.
  Новые интерфейсы/DDL интегрирует root; не редактировать их из нескольких задач.
- У задания точный base SHA. Root проверяет base→artifact diff и применяет
  patch/cherry-pick по одному. Не делать force/reset/rebase активного агента.

## Волна 0 — снять повторную подготовку

Root сверяет новые C07 изменения, реальные команды и незакрытые критерии,
фиксирует стабильные API для следующей волны и обновляет карточку состояния.
Использовать существующие venv/locks, Procrastinate/Keycloak/OpenViking SDK;
не строить новый оркестратор и не переписывать интерфейс только ради harness.
Root разрешает конкретную задачу, перечисляет её resource leases и checks.

## Волна 1 — три независимые задачи

| Направление | Исполнитель | Допустимые файлы | Результат и приёмка |
|---|---|---|---|
| A: workflow review/знания/опыт | MiniMax, один backend owner | Назначенная часть `catalog/writes.py`, `http/content.py`, новый изолированный regression file; общие schemas только root | Один завершённый сценарий, например отклонение черновика; real PG positive/negative и стабильный API |
| B: формы существующих API | Luna Codex с проверенными инструментами | `static/app.js` единственный owner; нужные `index.html`/`style.css` только если зарезервированы ему | Одна форма URL либо membership/grants; node syntax + browser positive/error/denied; новая API зависимость блокируется до контракта root |
| C: качество/документация | Luna или второй MiniMax после capacity check | Новый `evals/retrieval/*.json`, собственный `docs/workflow/*` документ, отдельные synthetic fixtures | Набор 50 answerable запросов и relevance/citation expectations; schema/provenance review, без provider calls и shared runtime test edits |

Пути `evals/`/новые тесты — предложенные новые файлы, а не существующая
инфраструктура. B может работать с уже существующими API одновременно с A;
UI для нового A endpoint начинает интеграцию после root contract freeze.
`app.js` нельзя раздать сразу нескольким Luna. Если появится необходимость,
root один раз создаёт небольшие согласованные модули и обновляет leases.

## Волна 2 — собрать продуктовые сценарии

- MiniMax: оставшиеся требования опыта (критерий успеха, evidence/assessor,
  разрешённые агрегаты) и брендового context. Автоматический результат не
  выдумывать: агент явно вызывает start_use/record_outcome.
- Luna: UI для уже принятого API, доступов, project pins и review.
- Root: реальное прохождение книга→draft→publication→search→exact rule→outcome,
  изоляция двух identities. Один интеграционный прогон, не по одному на автора.

## Волна 3 — эксплуатационная приёмка

Параллельно подготовить план полного restore и missing-case mapping A01–A20 /
S01–S04. По очереди выполнять restore, real service tests и benchmark. Проверить
два gateway/worker до увеличения concurrency. Root завершает exact-SHA bundle,
разрешённый release и независимый MCP readback; GitHub publication — отдельный scope.

Состояния: `planned → running → review → accepted`. `blocked` требует точной
недостающей зависимости; другие lanes продолжаются. Каждый executor сдаёт
checks, unresolved и следующий шаг; root меняет общую доску. Принятый patch
не равен развёрнутой функции. Новую сессию начинать с карточки/receipt, а не
с повторного исследования всего проекта.
