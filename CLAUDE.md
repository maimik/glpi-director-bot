# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Director** — распределённая система автоматизации IT-инфраструктуры на Python. Три компонента:
- **Telegram-бот** (aiogram 3.x) — согласование заявок GLPI для руководителя
- **Web-дашборд** (Flask + Socket.IO) — real-time мониторинг системных ресурсов
- **AI-оркестратор** — безопасное выполнение AI-генерируемых команд через SSH (sgpt + DeepSeek)

**Целевая платформа:** Linux (MX Linux, Debian-based, SysVinit)
**Сервер:** SFA-MNG (192.168.0.35), путь: `/home/maimik/Projects/director`

## Commands

### Service Management (на сервере SFA-MNG)
```bash
service director-bot start|stop|restart|status
tail -f /home/maimik/Projects/director/logs/service.log
```

### Установка сервиса
```bash
sudo /home/maimik/Projects/director/setup_sysvinit.sh
```

### Локальная разработка (на сервере)
```bash
# Telegram бот — отдельное venv
source venv_bot/bin/activate
python bot.py

# Web-дашборд — основное venv
source venv/bin/activate
python app.py
```

### Deployment через SSH (с Windows)
```bash
# Копирование изменённого файла на сервер
scp bot.py sfa-mng:/home/maimik/Projects/director/

# Выполнение команды на сервере
ssh sfa-mng "cd /home/maimik/Projects/director && service director-bot restart"

# Просмотр логов в реальном времени
ssh sfa-mng "tail -50 /home/maimik/Projects/director/logs/service.log"
```

### Отладка
```bash
# Запуск бота в foreground (без сервиса) для просмотра ошибок
ssh sfa-mng "cd /home/maimik/Projects/director && source venv_bot/bin/activate && python bot.py"

# Проверка синтаксиса Python файла
ssh sfa-mng "python3 -m py_compile /home/maimik/Projects/director/bot.py"
```

## Architecture

### Ключевые модули
| Файл | Назначение |
|------|------------|
| `bot.py` | Telegram бот — FSM для согласования, GLPIClient |
| `app.py` | Flask веб-сервер + Socket.IO для real-time |
| `ai_orchestrator.py` | AI выполнение: sgpt → проверка → SSH |
| `ssh_manager.py` | Пул SSH-соединений с переподключением |
| `modules/monitor.py` | Сбор метрик: CPU, RAM, Disk, Network |
| `backup_manager.py` | Архивирование с ротацией (5 бэкапов) |

### Виртуальные окружения
| Папка | Зависимости | Назначение |
|-------|-------------|------------|
| `venv_bot/` | `bot_requirements.txt` | Telegram бот (aiogram 3.x) |
| `venv/` | `requirements.txt` | Flask дашборд (Flask, Socket.IO) |

### Конфигурация
- `.env` — токены (TG_BOT_TOKEN, GLPI_APP_TOKEN, GLPI_USER_TOKEN, GLPI_MY_ID)
- `config/ai_nodes.yaml` — список управляемых серверов с SSH параметрами
- `data/director.db` — SQLite (processed_validations, tickets)

### Управляемые узлы
| Узел | IP | Назначение |
|------|----|----|
| sfa-mng | 192.168.0.35 | AI Gateway, Оркестратор |
| zbxglpi-pvl | 192.168.0.33 | GLPI + Zabbix |
| pvl-cloud | 192.168.0.25 | Nextcloud |
| nas | 192.168.10.10 | NAS (Траян) |
| fr-sw | 192.168.3.7 | Франко |

## GLPI Integration (критичные детали)

- **Получение валидаций:** прямой `GET /TicketValidation` (НЕ Search API — ненадёжный маппинг Field ID)
- **Requester name:** отдельный запрос `GET /Ticket/{id}/Ticket_User` (type=1)
- **HTML safety:** GLPI возвращает raw HTML → `clean_html_to_text()` перед отправкой в Telegram
- **Button handling:** удалять keyboard, результат как НОВОЕ сообщение (избегать HTML parsing errors)

## AI Orchestrator Safety

Чёрный список опасных команд (rm -rf /, mkfs, shutdown, etc.)
Перед редактированием файлов — автоматический бэкап
Использование /tmp вместо прямого удаления

## SSH Access (для Claude Code)

Разрешено в `.claude/settings.local.json`:
```bash
ssh sfa-mng "command"    # Выполнение на сервере
scp file sfa-mng:path    # Копирование файлов
```

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

**Формат вывода `/my_tickets`:**
```
📂 МОИ ЗАЯВКИ

🎫 #12345 — Заявка на ремонт принтера
   🏢 SFA (Софийская, 31)
   📅 2026-02-01 | 🟡 В работе
   📝 Описание заявки...

🎫 #12346 — Установка ПО
   🏢 MGM (Magazin, Traian-11)
   📅 2026-02-02 | 🟢 Новый
   📝 Нужно установить...
```

### Universal ID Resolution (UX критично!)
**Правило:** НИКОГДА не показывать сырые ID пользователю. Всегда резолвить в человекочитаемые имена.

**Хелперы:**
| Метод | API Endpoint | Возвращает |
|-------|--------------|------------|
| `_get_user_name(id)` | `GET /User/{id}` | `firstname + realname` или `name` |
| `_get_location_name(id)` | `GET /Location/{id}` | `completename` или `name` |

**Fallback логика (Search API ненадёжен):**
- Search API Field 83 (Location) часто возвращает `None`
- **Решение:** Для каждого тикета делать прямой запрос `GET /Ticket/{id}` и брать `locations_id`
- Затем резолвить через `_get_location_name(locations_id)`

**Где применяется:**
1. `get_active_tickets()` — резолвит location, requester, technician после merge
2. `check_tickets()` — уведомления о новых/изменённых тикетах
3. `/my_tickets` — список активных заявок
4. `/approvals` — список согласований

### Rich Notifications
Уведомления о новых тикетах и изменениях статуса.

**Формат уведомления "Новый тикет":**
```
🟢 🆕 Новый тикет #12345
🏢 Филиал: SFA (Софийская, 31)
👤 От кого: Иванова И.
👷 Кому: Петров П.
📝 Тема: Заявка на ремонт
📄 Описание заявки...
```

**ID Resolution (никогда не показывать числовые ID):**
- `locations_id` → `_get_location_name()` → `GET /Location/{id}`
- `users_id` (Field 4) → `_get_user_name()` → `GET /User/{id}`
- `users_id` (Field 5) → `_get_user_name()` → Technician/Assignee
- `_users_id_requester` → через `GET /Ticket/{id}/Ticket_User` (type=1)

**Content Preview:**
- Field 21 (Content) → `clean_html_to_text()` → truncate 100-150 chars

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
