# Основания правил · 12 октября 2026

Правила адаптированы к AI-wisdom; рекомендации статей не означают измеренный
прирост скорости или право отменить текущие gates.

- [OpenAI: Harness engineering](https://openai.com/index/harness-engineering/):
  короткий AGENTS как карта, знания и планы внутри repo, progressive disclosure,
  среда, в которой агент видит UI/ошибки и может проверять свой результат.
- [OpenAI: Codex best practices](https://learn.chatgpt.com/guides/best-practices):
  понятные команды, одна сессия на связный результат, отдельные worktrees для
  параллельных изменений и компактные устойчивые инструкции.
- [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents):
  список функций, небольшие приращения и progress artifacts между сессиями;
  проверка пользовательского сценария до объявления готовности.
- Опыт CMS Гадальни: принятие компонента не закрывает пользовательский сценарий;
  один исполнитель + Codex acceptance; лишнее reviewer/planning и повторный
  запуск stale/duplicate очереди убираются. Исторический лимит CMS не переносится.
- AI-wisdom acceptance `1fc0e5f`: 639 passed / 47.77 s; реальный MCP semantic,
  provenance/revoke/restart и off-host backup. Полный restore/матрица не приняты.

Текущие правила не запускают automations/cloud jobs и не меняют product code,
CLI provider, subscription capacity, тестовые recipes или required gates.
