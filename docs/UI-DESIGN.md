# Карта знаний: интерфейс чтения

## Фактический маршрут READ

1. Точка входа — уже существующий `material/source`; в карте это узел `source`.
2. «Показать связанные понятия» открывает семантическую сеть с выбранным `source_id`.
3. `filterGraphToSource` оставляет только существующие `concept`, у которых серверный
   `evidence` содержит этот `source_id`. Обычные связи и similarity edges фильтруются
   по тому же набору concept nodes.
4. Выбор concept загружает свежие данные через
   `/api/graphs/concepts/{entity_id}?library_id={library_id}`.
5. `renderDetail` показывает body и evidence. `renderCitation` выводит серверные
   `quote` и `locator`; «Открыть точное место» передаёт в `openSource` `source_id`,
   `fragment_id` (если он есть), точную цитату и locator.
6. «Вернуться к понятию» снова открывает details и возвращает фокус заголовку,
   если library, mode и selection generation всё ещё совпадают. При source filter
   доступна ссылка «Вернуться к материалу» на путь источников библиотеки.
7. Возврат из citation relation-row восстанавливает фокус строки связи.

Маршрут: `material/source → existing derived concepts → exact quote/location → return`.
Здесь нет создания concept из source, fragment или pasted quote. «Добавить понятие»
в текущем коде доступно только для семантической сети всей библиотеки, создаёт
concept без evidence и скрыто при source filter; это не часть READ.

## Точное место в источнике

- `locator` отображает `chapter`, `file_page`, `printed_label`, `paragraph`,
  `table_index`, `cell_row` и `cell_column` и передаётся без клиентского преобразования.
- Автолокация цитаты в источнике ограничена тремя выборками по 200 записей (3 × 200).
- Если точное место не найдено в этом пределе, используется manual page fallback.
- Если нет `quote`, `fragment_id` и locator, при наличии `source_id` открывается источник.
- Пустая цитата показывается как «Текст цитаты не указан»; клиент её не дополняет.

## Серверные данные и доступ

- Граф запрашивается как `/api/graphs/{mode}?library_id={library_id}&limit=200`;
  `data.truncated` показывается как неполная выдача.
- ACL library/entity проверяется на server endpoints; клиентский фильтр работает только
  после получения уже разрешённого ответа и не заменяет серверную проверку.
- `can_create` и `can_review` — возвращённые сервером capabilities для элементов UI.
- Citation evidence, `quote`, locator и `fragment_id` берутся из ответа concept detail
  без клиентского переписывания; UI не генерирует доказательство или понятие.
- Поиск ограничен уже загруженными узлами и не отправляет новый запрос.

## Клавиатура и браузер

- Surface графа имеет `tabindex=0`; все узлы продублированы доступными кнопками списка.
- `WisdomList` предоставляет постраничную навигацию списка и стандартную активацию кнопок.
- Выбор узла фокусирует заголовок details; возврат из цитаты фокусирует relation-row.
- Enter в локальном поиске открывает первый совпавший узел.
- Browser acceptance: клавиатурный список, возврат фокуса, ширины 320 и 390 px и
  desktop viewport. Статус: pending.

## Палитра и контракт графа

- Application theme владеет `--ink`, `--accent`, `--muted`, `--line-strong`, `--focus`;
  canvas только читает их и не изменяет.
- Для палитры один `getComputedStyle` используется на render; начальный размер переиспользует
  его, а пустые значения получают фиксированные безопасные fallback.
- `accent` заполняет source, `muted` — fragment, `line-strong` — обычные связи,
  `focus` — выбранный узел, `ink` — текст графа. Все категории используют те же контрастные роли; тип также подписан в списке.
- Длинные node labels имеют непрозрачную белую подложку; обычные edges — bezier,
  node и edge labels также имеют непрозрачную белую подложку.
- Нет regex/RGB-парсера, расчёта luminance или runtime contrast enforcement.
- API, lazy-load, resize lifecycle и per-node callbacks сохранены.
- Contrast acceptance рассчитывается отдельно по фактическим фиксированным значениям
  resolved tokens и category colors. Browser acceptance результата пока pending.
