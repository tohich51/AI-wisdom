# Быстрая разработка AI-wisdom

Правила применяются к новым незакреплённым пакетам на безопасной границе; текущие sealed/release contracts сохраняются. Подготовка правил не регистрирует Cloud-клиентов и не запускает разработку функций. Проверки local-first; сеть используется для согласованной подготовки, реальных интеграций и provider checks. Hosted CI не служит циклом каждой правки, но обязательный gate не отменяется. PAYG coding fallback запрещён; GitHub push/deploy выполняются только в соответствующем scope. Пользовательские изменения сохраняются, секреты не отправляются, токены не попадают в логи.

1. Один пакет — один ограниченный результат, один исполнитель и независимое принятие Codex. Дополнительный reviewer допустим только для прямо названного unresolved risk. Worker доходит до `review`, root фиксирует `accepted`; планирование и repository scans не повторяются.

2. Минимальный контекст: objective, exact SHA, allowed files, dependencies, checks и output. Root отдельно передаёт точные test matrix и wave plan; в тексте есть только ссылки на [TEST-MATRIX.md](TEST-MATRIX.md) и [PARALLEL-WAVES.md](PARALLEL-WAVES.md), без угадывания команд.

3. `MiniMax-M3.1-Flash-Preview` в thinking использует существующую OAuth-подписку. Text chat не имеет filesystem и tests и только готовит артефакт. Разрешённые безопасные шаги продолжаются без вопросов; отсутствующий secret или service capacity — точный blocker, независимые lanes продолжаются.

4. Переиспользовать текущие модули, официальные библиотеки и OSS. Не предпринимать speculative React rewrite, замену платформы или создание оркестратора. Один OpenViking writer сохраняется без изменений.

5. Luna получает bounded задачи по low-risk UI, forms, docs и synthetic fixtures в независимых worktrees. Для отдельной Cloud-сессии сначала проверить реальный выбор Luna, subscription, файловые инструменты и окружение; если surface не позволяет выбрать модель, не подменять её молча. Auth, ACL, SQL, contracts и deploy остаются у назначенного backend/root owner. Review-субагент не доказывает запуск отдельной Cloud coding task.

6. До подтверждения capacity запускать одну coding task; целевой WIP — до трёх независимых задач после проверки фактических лимитов. На общем xorek одновременно максимум один тяжёлый integration/model/live-fixture/OCI-build/restore task. Merge и release выполняет только root.

7. Параллельные пакеты требуют одновременно disjoint files, resource locks и frozen APIs. У `static/app.js` один владелец, пока root не выберет component split.

8. Использовать короткую карту `AGENTS` с progressive disclosure, versioned feature board и progress. Поддерживать тёплую проверенную среду; feature-пакет даёт один наблюдаемый пользовательский результат. Документация и fixtures сдаются как конкретный артефакт. Не переписывать общий план и не исследовать заново уже подтверждённый стык.

9. Проверки local-first и пропорциональны: docs — diff, links и files; Python — focused changed-domain regression; JS — Node syntax и реальный browser flow; persistence/security — реальный PostgreSQL и negatives; live — изменённая граница. Перед release — full required suite на exact source SHA. Сохранять существующие гарантии ACL, idempotency, provenance, restore matrix; число 639 не становится целью. Дубликат теста удаляется только после root-review с доказательством сохранившегося покрытия. Не добавлять тесты, зеркалящие реализацию, и не повторять прогоны без причины. Fake pass и skipped-accepted недопустимы.

10. Baseline приёмки Codex: 639 тестов за 47.77 с, server SHA `1fc0e5f`; cloud-dev SHA `45c4af5` содержит auth test/admin-helper, receipt и local/live justfile изменения, не развёрнут и не принят root. Проверить свежий HEAD перед назначением. Исторические README/JVM/embedding blocked descriptions устарели: использовать fresh native MCP, `readyz` и root receipts.

11. Dev receipt кэшируется только для тех же tested files, dependency closure, schema и env; для release любое изменение source SHA инвалидирует full receipt. `stop_reason=max_tokens` и пустой JSON отклоняются. После failure идентичный retry запрещён: дробить задачу, допустимы максимум две исправленные попытки; `UNKNOWN` внешних writes сверять в той же попытке.

12. Оставлять малый progress/receipt: source SHA, artifact, checks, unresolved, next. Worker достигает review, root принимает. Не выражать status процентами от test count. Измерять elapsed task, model wait, test wait, integration и fail attempts; не заявлять достигнутый speedup без данных.
