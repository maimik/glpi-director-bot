# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**GLPI Director Bot** — Telegram-бот на Python для руководителя, автоматизирующий работу с GLPI.

**Функции:**
- Согласование/отклонение заявок прямо из Telegram
- Создание заявок с выбором типа (Запрос/Инцидент) и указанием описания
- Мониторинг активных тикетов с rich-уведомлениями
- Режим супервизора — обзор ВСЕХ ожидающих согласований

**Стек:** Python 3, aiogram 3.x, aiohttp, SQLite
**Целевая платформа:** Linux (Debian-based, SysVinit)

## Commands

### Service Management (на сервере)
```bash
service director-bot start|stop|restart|status
tail -f $PROJECT_DIR/logs/service.log
```

### Установка сервиса
```bash
sudo $PROJECT_DIR/setup_sysvinit.sh
```

### Локальная разработка
```bash
source venv/bin/activate
python bot.py
```

### Deployment через SSH
```bash
# Копирование изменённого файла на сервер
scp bot.py your-server:$PROJECT_DIR/

# Перезапуск
ssh your-server "service director-bot restart"

# Просмотр логов
ssh your-server "tail -50 $PROJECT_DIR/logs/service.log"
```

## Architecture

### Ключевые модули
| Файл | Назначение |
|------|------------|
| `bot.py` | Telegram бот — FSM, GLPIClient, мониторинг |
| `modules/monitor.py` | Сбор метрик: CPU, RAM, Disk, Network |
| `setup_sysvinit.sh` | Автоматическая установка SysVinit-сервиса |

### Конфигурация
- `.env` — токены (TG_BOT_TOKEN, GLPI_APP_TOKEN, GLPI_USER_TOKEN, GLPI_MY_ID)
- `data/director.db` — SQLite (processed_validations, tickets)

## GLPI Integration (критичные детали)

- **Получение валидаций:** прямой `GET /TicketValidation` (НЕ Search API — ненадёжный маппинг Field ID)
- **Requester name:** отдельный запрос `GET /Ticket/{id}/Ticket_User` (type=1)
- **HTML safety:** GLPI возвращает raw HTML → `clean_html_to_text()` перед отправкой в Telegram
- **Button handling:** удалять keyboard, результат как НОВОЕ сообщение (избегать HTML parsing errors)
- **Observer:** при создании заявки автоматически назначается группа Administrators (id=1) как наблюдатель

## SSH & File Editing Rules

1. **NO Complex One-Liners:** Do not try to write complex Python logic using `python -c "..."`. It fails due to shell escaping issues.
2. **USE Quoted Heredocs:** When writing files via SSH, ALWAYS use `cat << 'EOF'` (with single quotes around EOF).
   - Correct: `cat > filename.py << 'EOF'` (Disables shell expansion, safe for f-strings/$ symbols).
   - Incorrect: `cat > filename.py << EOF` (Shell tries to expand variables, causing syntax errors).
3. **NO `sed` for Logic:** Do not use `sed` to patch Python code. It is fragile. Overwrite the whole file or the specific function using a temporary python script.
4. **Backslash in f-strings:** Use `chr(92)` to insert literal backslash when constructing strings dynamically (e.g., `f"text{chr(92)}nmore"` for `\n`).

## Feature Logic Reference

### Bot Menu Commands
- `/start` — Главное меню с inline-кнопками
- `/approvals` — Режим супервизора (все согласования)
- `/my_tickets` — Активные заявки пользователя
- `/help` — Справка по командам

### Ticket Creation Flow
1. Пользователь нажимает «➕ Создать заявку»
2. Выбор типа: 📋 Запрос (по умолчанию) или 🔥 Инцидент
3. Ввод заголовка (краткая суть)
4. Ввод описания (подробное содержание)
5. Создание тикета в GLPI с автоматическим назначением:
   - Тип заявки (Запрос/Инцидент)
   - Наблюдатель: группа Administrators
   - Локация из профиля пользователя

### Return to Menu
После каждой операции (создание заявки, согласование, отклонение, просмотр) отображается кнопка «🏠 Меню» для возврата в главное меню.

### Notification Deduplication
- `check_validations()` — приоритетные уведомления о согласованиях
- `check_ticket_updates()` — уведомления о новых/изменённых тикетах
- Если тикет уведомлён через валидацию, `check_ticket_updates` записывает его в БД **тихо** (без повторного уведомления)

### Supervisor Mode (Approvals)
Показывает ВСЕ ожидающие согласования в системе, не только свои.

**Логика:**
1. `get_all_pending_validations()` — получает все `TicketValidation` со `status=2` (Waiting)
2. Для каждой валидации получает Parent Ticket через `get_ticket_details()`
3. **Ghost Filtering** (критично!):
   - Пропускать если `ticket is None` (404)
   - Пропускать если `ticket.is_deleted == 1`
   - Пропускать если `ticket.status == 6` (Closed)
4. Резолвит имя валидатора: `users_id_validate` → `_get_user_name()`
5. Подсвечивает `🔴 ВАС!` если `validator_id == Config.GLPI_MY_ID`

### Smart Ticket Visibility ("My Tickets")
Показывает заявки где пользователь участник в любой роли.

**Стратегия "3 запроса + слияние":**
1. `_fetch_by_role(4, "Requester")` — Field 4 (заявитель)
2. `_fetch_by_role(5, "Assignee")` — Field 5 (исполнитель)
3. `_fetch_by_role(66, "Observer")` — Field 66 (наблюдатель-пользователь)
4. **Групповой поиск:**
   - `get_user_groups()` → `GET /User/{id}/Group_User` → список group_ids
   - Для каждой группы: `_fetch_by_role_group(65, group_id)` — Field 65 (наблюдатель-группа)
5. Слияние по ID (dict) → дедупликация
6. **Block List фильтр:** `status != 6` (показывать всё кроме Closed)
7. **ID Resolution:** для каждого тикета резолвить location, requester, technician

### Universal ID Resolution (UX критично!)
**Правило:** НИКОГДА не показывать сырые ID пользователю. Всегда резолвить в человекочитаемые имена.

**Хелперы:**
| Метод | API Endpoint | Возвращает |
|-------|--------------|------------|
| `_get_user_name(id)` | `GET /User/{id}` | `firstname + realname` или `name` |
| `_get_location_name(id)` | `GET /Location/{id}` | `completename` или `name` |

### GLPI Search API Field IDs
| Field | Описание |
|-------|----------|
| 1 | Title/Name |
| 2 | Ticket ID |
| 4 | Requester (User) |
| 5 | Technician/Assignee |
| 12 | Status |
| 15 | Date |
| 21 | Content |
| 65 | Observer Group |
| 66 | Observer User |
| 83 | Location |

**Важно:** Search API возвращает ключи как строки (`'2'`, `'12'`), не int!

## Database Schema

**File:** `data/director.db` (SQLite)

```sql
-- Anti-spam: не отправлять повторные уведомления
CREATE TABLE processed_validations (
    id INTEGER PRIMARY KEY,
    glpi_id INTEGER UNIQUE
);

-- Отслеживание статусов тикетов для уведомлений
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY,
    glpi_id INTEGER UNIQUE,
    status INTEGER,
    title TEXT,
    last_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
