# Быстрая разработка AI-wisdom

Правила применяются к новым незакреплённым пакетам на безопасной границе; текущие sealed/release contracts сохраняются. Подготовка правил не регистрирует Cloud-клиентов и не запускает разработку функций. Проверки local-first; сеть используется для согласованной подготовки, реальных интеграций и provider checks. Hosted CI не служит циклом каждой правки, но обязательный gate не отменяется. PAYG coding fallback запрещён; GitHub push/deploy выполняются только в соответствующем scope. Пользовательские изменения сохраняются, секреты и приватные книги не отправляются, токены не попадают в логи.

1. По умолчанию target root — три независимых пакета в отдельных workspaces/files, с disjoint resource locks и frozen interfaces. Каждый executor возвращает один ограниченный артефакт; для него проводится одна независимая Codex acceptance, а root фиксирует итог. Дополнительный reviewer допустим только для прямо названного unresolved risk или более строго требуемой policy. После fix Codex в рамках той же acceptance перепроверяет изменённое поведение и связанные риски; полный re-review и новое планирование не обязательны.

2. Для многозадачной волны перед dispatch root поддерживает три готовых task packet и два buffer packet; для меньшего остатка не создавать лишние задачи ради WIP. Пока один пакет находится в `running`, `waiting`, на `review`/`fix` или `blocked`, root продолжает направлять, готовить или принимать другие готовые независимые пакеты. Общий wait запрещён, пока в разрешённой области остаётся полезная независимая работа, и допустим только после её исчерпания.

3. Минимальный task packet содержит objective, exact SHA, allowed files, dependencies, frozen interfaces, checks и output. Root отдельно передаёт exact test matrix и wave plan; в тексте остаются только ссылки на [TEST-MATRIX.md](TEST-MATRIX.md) и [PARALLEL-WAVES.md](PARALLEL-WAVES.md), без угадывания команд. Root независимо применяет артефакт и выполняет acceptance checks; статус executor/model их не заменяет.

4. `MiniMax-M3.1-Flash-Preview` в thinking использует существующую OAuth-подписку. Фактические выбор model, subscription и capabilities проверяются, а не предполагаются. Text chat не имеет filesystem и tests и только готовит артефакт. Разрешённые безопасные шаги продолжаются без вопросов; отсутствующий secret или service capacity — точный blocker только своей lane, независимые lanes продолжаются.

5. Переиспользуются текущие модули, официальные библиотеки и OSS. Speculative React rewrite, замена платформы, новый orchestrator и изменение product/model concurrency запрещены. Сохраняются одна существующая product generation и один существующий OpenViking writer.

6. Capacity evidence относится только к проверенному resource/lease; лимиты provider действуют лишь в его собственном lease. При неизвестной capacity выполняется малый bounded probe с допуском до трёх независимых calls при подходящем бюджете ресурсов; WIP снижается только у provider, получившего `429` или resource failure. Неизвестная capacity не блокирует все prepared lanes. Network-only coding provider calls отделяются от local CPU-heavy jobs; проверенные bounded coding calls группируются под одну server allocation. На single shared xorek остаётся одна heavy allocation и не более одного тяжёлого integration/model/live-fixture/OCI-build/restore task. Conflicting writes, shared server heavy slots и release сериализуются. Production locks никогда не удаляются вслепую.

7. Luna получает bounded задачи по low-risk UI, forms, docs и synthetic fixtures в независимых worktrees. Для отдельной Cloud-сессии сначала фактически проверяются выбор Luna, subscription, файловые инструменты и окружение; если surface не позволяет выбрать модель, молчаливая подмена запрещена. Review-субагент не доказывает запуск отдельной Cloud coding task. Auth, ACL, SQL, contracts и deploy остаются у назначенного backend/root owner.

8. Используются короткая progressive-disclosure карта `AGENTS`, versioned feature board и progress. Поддерживается тёплая проверенная среда; feature-пакет даёт один наблюдаемый пользовательский результат. Документация и fixtures сдаются как конкретный артефакт без переписывания общего плана. Малый progress/receipt хранит source SHA, artifact, checks, unresolved и next. Merge и release выполняет только root.

9. Dev checks local-first и пропорциональны: docs — diff, links и files; Python — focused changed-domain regression; JS — Node syntax и реальный browser flow; persistence/security — реальный PostgreSQL и negatives; live — изменённая граница. Для неизменённого final exact SHA full required gate выполняется один раз перед release; если candidate, failure или risk инвалидирует evidence, gate повторяется. Сохраняются реальные ACL, idempotency, provenance, auth, restore matrix и обязательные release gates. Число 639 не становится целью. Duplicate test удаляется только после root-review с доказательством сохранившегося покрытия; тесты, зеркалящие реализацию, и повторные прогоны без причины не добавляются. Fake pass и skipped-accepted недопустимы.

10. Исторический baseline — 639 тестов за 47.77 с на server SHA `1fc0e5f`; cloud-dev SHA `45c4af5` с auth test/admin-helper, receipt и local/live justfile changes не развёрнут и не принят root. Он не является current baseline. Перед назначением проверяется fresh HEAD и используются fresh receipts с сохранением API/ACL rules. Устаревшие README/JVM/embedding blocked descriptions заменяются свежими native MCP, `readyz` и root receipts.

11. Dev receipt кэшируется только для тех же tested files, dependency closure, schema и env; любое изменение source SHA инвалидирует full release receipt. `stop_reason=max_tokens` и пустой JSON отклоняются. После failure идентичный retry запрещён: задача дробится, допустимы максимум две исправленные попытки. `UNKNOWN` внешних writes сверяется в той же попытке. Model/subscription success принимается только при фактическом результате, удовлетворяющем сохранённым границам модели и подписки.

12. Решение владельца от 15.10.2026 ставит efficiency-first и явно разрешает parallel independent execution без coordinator idle loop `executor → review → fix → re-review`. Завершение event-based: записываются фактические task, model, test, queue, acceptance и fix times; status-only model calls запрещены, а ускорение не заявляется без чисел. Этот batch — проба трёх малых параллельных subscription doc-запросов, а не измерение throughput для всех jobs.

## Research/Studio/maps runtime extension · 2026-10-14

The original ten MCP tools remain; fourteen additive tools use the same authenticated catalog.
Research/Studio use immutable selected-source snapshots, own-principal runs/notes, exact Unicode
citation offsets and hashes, and full dependency RLS. Generated output stays a draft. The runner
holds the existing single slot across generation and conservative support verification. Runtime
stream completion is accepted only with the configured model, thinking block, message_stop and
end_turn; truncated or malformed output is never a usable result. No paid API fallback.

Studio covers summary, FAQ, comparison, checklist and flashcards, plus a persisted topic hierarchy
and library-scoped semantic drafts. Sampling is bounded and explicitly disclosed; it is not a
claim of full-book coverage. Semantic relations are typed and directed, with evidence/hypothesis
labels; no embedding-similarity score is invented. The concept similarity overlay awaits its own
compatible index. Manual revisions of generated concepts retain all inherited source dependencies.

HTTP and MCP use identical functions. Only explicit curator review publishes semantic records;
rule handoff creates a draft and never creates usage/outcomes. Canonical graphs contain only
stored source/fragment/provenance/exact-version usage/outcome records. Audio/video/slides are not
implemented. New features do not close the entire original acceptance matrix by test count.
