# Проверки по изменению

Сокращаем повторные прогоны и тесты без отдельного проверяемого требования.
Существующий набор не удаляется по числу тестов. Последний принятый серверный
прогон: 639 passed, 47.77 s, SHA `1fc0e5f6f38915165187849dd59a055ba2223680`.
Это историческое измерение одной среды, а не SLA или gate будущего коммита.

## Во время реализации

Все команды выполняются в назначенном checkout с подготовленной `.venv`.
Убедиться в актуальном `justfile`/pyproject и наличии нужного сервиса; подготовку
окружения делать один раз на версию lockfiles и toolchain. Не устанавливать
зависимости заново в каждой задаче и не подменять локальный runner другим проектом.

| Изменение | Минимальная проверка | Когда расширить |
|---|---|---|
| Документация/правила | `git diff --check`, JSON parse, ссылки и фактические пути | Изменена команда/контракт: проверить соответствующий entrypoint |
| Python функция | `PYTHONPATH=src .venv/bin/ruff check <changed.py>`, format check, существующий focused regression | Общий интерфейс: зависимые тесты + mypy |
| Embedding/reader/writer | `PYTHONPATH=src .venv/bin/pytest -q tests/unit/test_embedding_profile.py tests/unit/test_vector_reader.py tests/unit/test_vector_writer.py` — выбрать затронутые | Изменён provider/profile/SDK: один реальный canary владельцем интеграции |
| Семантическая проекция | `PYTHONPATH=src .venv/bin/pytest -q tests/integration/provisioning/test_semantic_projection.py` | Реальная OpenViking граница: canary, закрытый scope, revoke, restart |
| SQL/RLS/пользователи | Выбрать релевантные файлы из `tests/integration/rls/`, `members/`, `libraries/` | Всегда настоящая PostgreSQL и хотя бы отказ доступа; общий DDL затрагивает зависимые модули |
| Загрузка/URL | `PYTHONPATH=src .venv/bin/pytest -q tests/integration/upload/` или конкретный файл | Изменён parser/queue: релевантные `tests/runtime/test_runtime.py -k ...` |
| Очередь/версии/опыт | `PYTHONPATH=src .venv/bin/pytest -q tests/runtime/test_runtime.py -k '<selected_existing_tests>'` | Concurrency/запись: реальная PostgreSQL, retry/revoke/idempotency и поздний ответ |
| Identity/session | `PYTHONPATH=src .venv/bin/pytest -q tests/integration/identity/test_session_store.py tests/integration/identity/test_token_validation.py` | Изменён реальный OIDC/logout: владельческий live Keycloak gate |
| UI JS | `node --check src/kb/static/app.js` (и изменённых JS), один сценарий в браузере | Запись: успешная операция + отказ прав/ошибка; синтаксис не доказывает UX |
| CSS/HTML | Открыть затронутый экран, проверить небольшой и широкий viewport, действия/ошибки | Изменена навигация/auth: соответствующий браузерный сценарий |

`<...>` — параметры задания, не готовые shell аргументы. Root выбирает
существующие test names через scoped `rg`/`pytest --collect-only`; не запускать
пустой `-k` и не считать deselected/skipped доказательством нужного поведения.
Новые модули, обозначенные в плане, сначала должны реально появиться.

Обычная UI форма не требует десятков новых unit tests DOM implementation.
Новый тест нужен для новой регрессии, границы данных или риска, которого ещё нет
в покрытии. Комбинации с одной причиной отказа объединять параметризацией;
не объединять разные гарантии так, чтобы исчезло наблюдаемое доказательство.
Удаление дубликата допускается лишь отдельным root diff-review с указанием
сохранившегося эквивалентного теста; текущая поставка правил ничего не удаляет.

## Интеграция и выпуск

1. Root собирает совместимые accepted patches в один кандидат. На границе
   волны выполняет один набор зависимых интеграционных проверок.
2. Перед выпуском: чистый exact-SHA, `just check`, полный требуемый PostgreSQL
   и live stack gate, архитектура `linux/amd64`, image/source hashes.
3. У коммита `1fc0e5f` live Keycloak входит в общий pytest набор. Cloud candidate
   `45c4af5` предлагает `just live`/`integration-all`: это другой justfile,
   root ещё должен принять split. Проверить реально доступные recipes; не
   считать локальный `just integration` заменой provider leg.
4. `just acceptance` пока имеет ноль acceptance-marked cases и завершается
   nonzero. A01–A20/S01–S04 остаются обязательной отдельной работой. Чтобы
   закрыть матрицу, сопоставить каждый сценарий с реальным evidence и написать
   только отсутствующие проверки; не дублировать уже доказанные свойства.
5. Для разрешённого rollout: согласованный backup, один release owner,
   rollback, независимый readiness/MCP/UI readback. Текущий full restore из
   последнего snapshot не принят; успешный backup не заменяет его.

Focused dev evidence можно повторно использовать только при неизменных
проверенных файлах, зависимостях, DDL, contracts и среде. Release evidence
привязано к полному SHA/build inputs и при их изменении недействительно.
Повтор полного прогона нужен после новой правки/сбоя/неразрешённого риска,
а не потому что подключился ещё один агент.
