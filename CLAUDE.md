# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Memory Protocol (CRITICAL)

**Definition of Done** for ANY task:
1. **Code:** Changes implemented and verified?
2. **Docs:** Check if documentation needs updates:
   - **Logic/Commands/Architecture:** Update `CLAUDE.md`.
   - **Infrastructure/IPs/Servers/Network:** Update `knowledge_base.md`.
3. **Commit:** Push changes to git.

---

## Project Overview

Telegram-бот (aiogram 3.x) для согласования заявок GLPI руководителем.
**Единственный активный файл — `bot.py` (~1535 строк).** Всё остальное — мёртвый код из другого проекта (`director/`).

**Сервер:** SFA-MNG (192.168.0.35), путь: `/home/maimik/Projects/GLPI_Director-Bot`

### Dead-code файлы (НЕ используются ботом, не трогать)

| Файл | Откуда | Примечание |
|------|--------|------------|
| `app.py` | director/ | Flask-приложение, Flask не установлен в venv |
| `ai_orchestrator.py` | director/ | SSH/sgpt оркестратор |
| `ssh_manager.py` | director/ | Connection pool для SSH |
| `backup_manager.py` | director/ | Архивация бэкапов |
| `config.py` | ai-chat-server/ | Flask Config class для SocketIO |
| `requirements.txt` | director/ | Flask+SocketIO+psutil — **НЕ использовать** для бота |
| `modules/monitor.py` | director/ | SystemMonitor (psutil) |
| `static/`, `templates/` | director/ | Flask-шаблоны |

### Зависимости бота

```bash
cd ~/Projects/GLPI_Director-Bot && source venv/bin/activate
pip install -r requirements.txt
```
Фактически установлено: aiogram 3.25.0, aiohttp 3.13.3, bs4 4.14.3.

---

## Commands

### Service Management
```bash
service director-bot start|stop|restart|status
tail -f /home/maimik/Projects/GLPI_Director-Bot/logs/service.log
```

### Debug Run
```bash
cd ~/Projects/GLPI_Director-Bot
source venv/bin/activate
python bot.py
```

### Syntax Check
```bash
python3 -m py_compile /home/maimik/Projects/GLPI_Director-Bot/bot.py
```

---

## Architecture

Весь бот — один файл `bot.py` без модулей.

| Класс/Функция | Назначение |
|---------------|------------|
| `Config` | Env vars из `.env` |
| `GLPIClient` | Async GLPI REST API клиент с session management |
| `init_db()` | SQLite init (таблицы `processed_validations`, `tickets`) |
| `Form(StatesGroup)` | FSM состояния (4 state) |
| `get_main_menu_kb()` | InlineKeyboard главного меню (3 кнопки) |
| `check_validations(silent=True)` | Фоновая проверка: личные согласования директора |
| `check_tickets()` | Фоновая проверка: новые тикеты и смена статусов; возвращает `new_count` |
| `monitor_loop()` | Бесконечный цикл: `check_validations → check_tickets → sleep` |
| `get_status_name()` | Код статуса → строка (1-6) |
| `main()` | init_db → init_session → diagnose → create_task → polling |

### Конфигурация (`.env`)
```
TG_BOT_TOKEN, TG_ADMIN_ID
GLPI_URL, GLPI_APP_TOKEN, GLPI_USER_TOKEN
GLPI_MY_ID        # ID пользователя GLPI (default: 21)
GLPI_CHECK_INTERVAL  # Интервал проверки сек (default: 300)
```

### Database (`data/director.db`)
```sql
processed_validations (id, glpi_id UNIQUE)  -- анти-спам: уже отправленные согласования
tickets (id, glpi_id UNIQUE, status, title, last_update)  -- трекинг статусов тикетов
```

### Telegram-команды бота
| Команда | Хендлер | Назначение |
|---------|---------|------------|
| `/start` | `cmd_start` | Главное меню |
| `/approvals` | `cmd_approvals` | Список ВСЕХ ожидающих согласований |
| `/my_tickets` | `cmd_my_tickets` | Мои активные заявки |
| `/help` | `cmd_help` | Справка |

Все хендлеры проверяют `message.from_user.id != Config.ADMIN_ID` → early return (бот однопользовательский).

---

## FSM States

```python
class Form(StatesGroup):
    waiting_for_refusal      # Ввод причины отказа
    waiting_for_ticket_type  # Выбор типа тикета (Задача/Инцидент)
    waiting_for_ticket_title # Ввод темы
    waiting_for_ticket_desc  # Ввод описания
```

---

## Keyboard Helpers

**Единственный хелпер:**
```python
get_main_menu_kb()  # 3 кнопки: Проверить согласования / Мои заявки / Создать заявку
```

Кнопка "🏠 Меню" на финальных сообщениях — inline, строится локально:
```python
kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]
])
```

**`callback_main_menu` (F.data == "main_menu"):**
- Использует `call.message.answer()` (новое сообщение), **НЕ `edit_text()`**
- Намеренно: уведомления фонового монитора нельзя редактировать

---

## GLPI Integration (критичные детали)

### Инициализация сессии
`init_session()` → авторизация → `_enable_global_view()` (`changeActiveEntities(entities_id=0, is_recursive=True)`).
Глобальный вид нужен для видимости тикетов из всех филиалов.

### Получение валидаций (два режима)

| Метод | Для кого | Используется |
|-------|---------|-------------|
| `get_pending_validations()` | Только для `GLPI_MY_ID` (status==2 AND validator==my_id) | `check_validations()` (фон) |
| `get_all_pending_validations()` | Все ожидающие (status==2, любой validator) | callback `check_validations`, `/approvals` |

Оба метода: прямой `GET /TicketValidation` с фильтрацией в Python (Search API ненадёжен — поля маппинга нестабильны).

### Имя заявителя
Отдельный запрос: `GET /Ticket/{id}/Ticket_User` → ищем `type==1` (Requester) → `_get_user_name(user_id)`.

### Location + Priority + Extra fields
Search API Field 83 ненадёжен (возвращает None).
В `get_active_tickets()` для каждого тикета: прямой `GET /Ticket/{id}` → берём `locations_id`, `priority`, `date_creation`, `users_id_lastupdater`.

### Вспомогательные методы GLPIClient
| Метод | Endpoint | Возвращает |
|-------|---------|-----------|
| `_get_user_name(id)` | `GET /User/{id}` | `"Имя Фамилия"` или `"User #id"` |
| `_get_entity_name(id)` | `GET /Entity/{id}` | `name` |
| `_get_location_name(id)` | `GET /Location/{id}` | `completename` или `name` |
| `_get_ticket_solution(id)` | `GET /Ticket/{id}/ITILSolution?range=0-1&order=DESC` | `{user_name, content}` или None |
| `_get_ticket_followup(id)` | `GET /Ticket/{id}/ITILFollowup?range=0-1&order=DESC` | `{user_name, content}` или None |
| `_get_user_profile(id)` | `GET /User/{id}` | полный профиль (для `locations_id`) |
| `get_user_groups()` | `GET /User/{id}/Group_User` | список `groups_id` |
| `diagnose_search_options()` | `GET /listSearchOptions/TicketValidation` | логирует поля 3 (Status), 4 (Date), 7 (Validator) |

### Создание тикета — обязательные поля
```python
{
    "_users_id_requester": [Config.GLPI_MY_ID],  # МАССИВ с underscore!
    "locations_id": locations_id,  # из профиля пользователя
    "_groups_id_observer": [1],    # Administrators как наблюдатель
    "type": ticket_type,           # 1=Инцидент, 2=Задача
    "entities_id": 0,              # Root entity для глобальной видимости
}
```

---

## Create Ticket FSM Flow (3 шага)

1. `waiting_for_ticket_type` — кнопки "📋 Задача" / "🔥 Инцидент" (callback: `ticket_type_2` / `ticket_type_1`)
2. `waiting_for_ticket_title` — текстовый ввод → `edit_text()` сообщения с типом
3. `waiting_for_ticket_desc` — текстовый ввод → `create_ticket()` → ответ с "🏠 Меню"

---

## My Tickets — логика "3 запроса + merge"

1. Field 4 (Requester), Field 5 (Assignee), Field 66 (Observer) — по `GLPI_MY_ID`
2. `get_user_groups()` → для каждой группы Field 65 (Observer Group)
3. Merge по ID, фильтр `status != 6` (исключить Closed)
4. Для каждого тикета: прямой `GET /Ticket/{id}` для `locations_id`, `priority`, etc.

---

## Background Monitor (`monitor_loop`)

Запускается через `asyncio.create_task()` в `main()`.
Цикл: `check_validations()` → `check_tickets()` → адаптивный `sleep`.

### Адаптивный интервал
```python
await check_validations()              # silent=True, не влияет на интервал
new_tickets = await check_tickets()    # возвращает только НОВЫЕ заявки (int)
interval = 60 if new_tickets > 0 else Config.CHECK_INTERVAL  # 60 или 300 сек
await asyncio.sleep(interval)
```
`check_tickets()` возвращает `new_count` — только новые заявки (первое появление в БД, `row is None`).
Изменения статусов отслеживаются и уведомляются, но на интервал **не влияют**.

### Формат уведомления о согласовании (`check_validations`)
```
📑 ТРЕБУЕТСЯ СОГЛАСОВАНИЕ

🎫 Заявка #{ticket_id}
👤 Кто: {requester}
📝 Тема: {title}
📄 Описание:
{content[:300]}

🔗 Открыть в GLPI [hyperlink]
```
Кнопки: "✅ Согласовать" / "❌ Отказать" — **без "🏠 Меню"**.
Callback-формат: `approve_{val_id}_{ticket_id}`, `refuse_{val_id}_{ticket_id}`.
Защита от дублей: in-memory `glpi.notified_validations` set + таблица `processed_validations` в SQLite.

### Формат уведомления о новом тикете (`check_tickets`)
```
🆕 НОВАЯ ЗАЯВКА #{glpi_id}

📋 {title}

👤 От кого: {requester}
[👷 Кому: {technician}]   ← если назначен
[📝 Описание:
{content[:500]}]

📍 Местоположение: {location}

📅 Создано: {date_creation[:16]}
⚡ Приоритет: {priority_name}
📊 Статус: {status_name}
```
Кнопка "🔗 Открыть в GLPI".
Дедупликация: если тикет уже был в `glpi.notified_ticket_ids` (уведомлён через согласование) → в БД без уведомления.

### Формат уведомления об изменении статуса
```
{emoji} Статус заявки #{id} изменён   ← 🆕🔧📅⏸️✅🔒 по статусу 1-6

📋 {title}

Было: {old_status}
Стало: {new_status}
👤 Кто изменил: {users_id_lastupdater → имя}
[🔧 Назначена: {technician}]   ← только при статусе 2

[💡 Решение ({solver}):         ← статусы 5/6: ITILSolution, fallback ITILFollowup
{text[:500]}]
[💬 Комментарий ({author}):    ← прочие статусы: последний ITILFollowup
{text[:500]}]
```

---

## HTML Safety

Два метода — не путать:
- `clean_html_to_text(html)` — **синхронный**, использовать везде в хендлерах. Возвращает текст, экранированный для Telegram HTML.
- `_clean_html(html)` — async, использует BeautifulSoup (не используется в handlers напрямую).

**Правило:** `content` из GLPI всегда прогонять через `clean_html_to_text()` перед отправкой.

---

## GLPI Search API Field IDs
| Field | Описание |
|-------|----------|
| 1 | Title |
| 2 | Ticket ID |
| 4 | Requester (User) |
| 5 | Technician/Assignee |
| 12 | Status |
| 15 | Date |
| 21 | Content |
| 65 | Observer Group |
| 66 | Observer User |
| 83 | Location (ненадёжен — возвращает None) |

Search API возвращает ключи как **строки** (`'2'`, `'12'`), не int.

## Validation Status Codes
| Code | Значение |
|------|---------|
| 2 | Waiting (нужно согласование) |
| 3 | Approved |
| 4 | Refused |

## Ticket Status Codes
| Code | Значение |
|------|---------|
| 1 | New |
| 2 | Processing (назначена) |
| 3 | Planned |
| 4 | Pending |
| 5 | Solved |
| 6 | Closed (фильтруется из списков, не отслеживается монитором) |

---

## Troubleshooting

| Проблема | Причина | Решение |
|----------|---------|---------|
| Stale PID file | Жёсткая перезагрузка | `rm ~/Projects/GLPI_Director-Bot/data/director-bot.pid` |
| Telegram parsing error | Raw HTML из GLPI | `clean_html_to_text()` |
| Валидация не найдена | Search API ненадёжен | Используем прямой `/TicketValidation` с Python-фильтром |
| Location = None | Search API Field 83 | Прямой `GET /Ticket/{id}` + `_get_location_name()` |
| "None" в callback_data | val_id=None из API | Защита: `if "None" in call.data: return` |

## Service Robustness

Init-скрипт `/etc/init.d/director-bot`:
1. **Stale PID Cleanup** — `kill -0 $PID`, удаляет мёртвые PID-файлы
2. **Network Wait** — пингует 8.8.8.8 до 60 сек перед запуском
