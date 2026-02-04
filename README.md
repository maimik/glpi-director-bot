# 🤖 GLPI Director Bot

**Mobile Admin Panel for IT Directors via Telegram**

---

## 📖 Description | Описание

### English
An async Telegram bot for IT Directors and managers to manage GLPI helpdesk tickets on-the-go. Approve/reject validation requests, monitor ticket statuses, and create new tickets — all from your phone.

Built with Python 3 + aiogram 3.x for high-performance async operations.

### Русский
Асинхронный Telegram-бот для IT-директоров и руководителей для управления заявками GLPI на ходу. Согласование/отклонение запросов на валидацию, мониторинг статусов заявок и создание новых заявок — всё с телефона.

Построен на Python 3 + aiogram 3.x для высокопроизводительных асинхронных операций.

---

## ✨ Key Features | Ключевые возможности

### 🔐 Supervisor Mode (Режим супервизора)
- **EN:** View ALL pending approvals across the entire GLPI system, not just your own
- **RU:** Просмотр ВСЕХ ожидающих согласований во всей системе GLPI, не только своих
- Highlights approvals assigned to YOU with 🔴 indicator
- Ghost filtering: automatically skips deleted/closed tickets

### 🎯 Smart ID Resolution (Умное разрешение ID)
- **EN:** Automatically converts raw IDs to human-readable names
- **RU:** Автоматически конвертирует сырые ID в читаемые имена
- `User ID 21` → `"Иванов Иван"`
- `Location ID 5` → `"Branch: Traian-11 (Magazin)"`

### 👁️ Smart Ticket Visibility (Умная видимость заявок)
- **EN:** Shows tickets where user is Requester, Assignee, or Observer
- **RU:** Показывает заявки, где пользователь — Заявитель, Исполнитель или Наблюдатель
- Includes group membership lookups (Observer Groups)
- Merges results from 3+ API queries with deduplication

### ➕ Ticket Creation (Создание заявок)
- **EN:** Create tickets directly from Telegram with proper Requester linking
- **RU:** Создание заявок напрямую из Telegram с корректной привязкой Заявителя
- Auto-fills Location from user profile
- Uses GLPI underscore syntax: `_users_id_requester`

### 🔄 Service Resilience (Отказоустойчивость)
- **EN:** Designed for 24/7 operation with crash recovery
- **RU:** Разработан для работы 24/7 с восстановлением после сбоев
- Stale PID cleanup after power outage
- Network wait loop (60s) before connecting to Telegram API
- SysVinit service with auto-start on boot

---

## 🏗️ Architecture | Архитектура

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Telegram Bot   │────▶│   GLPI REST     │────▶│   GLPI MySQL    │
│  (aiogram 3.x)  │◀────│      API        │◀────│    Database     │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │
        ▼
┌─────────────────┐
│  SQLite Cache   │  (processed_validations, tickets)
└─────────────────┘
```

### Core Components | Основные компоненты

| File | Purpose | Назначение |
|------|---------|------------|
| `bot.py` | Main bot application | Главное приложение бота |
| `setup_sysvinit.sh` | Service installer (SysVinit) | Установщик сервиса |
| `modules/monitor.py` | System metrics collector | Сборщик метрик (опционально) |

---

## 🚀 Installation | Установка

### Prerequisites | Требования
- Python 3.8+
- Linux server (Debian/Ubuntu/MX Linux)
- GLPI with REST API enabled
- Telegram Bot Token (from @BotFather)

### Steps | Шаги

```bash
# 1. Clone repository | Клонировать репозиторий
git clone https://github.com/maimik/glpi-director-bot.git
cd glpi-director-bot

# 2. Create virtual environment | Создать виртуальное окружение
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies | Установить зависимости
pip install -r requirements.txt

# 4. Configure environment | Настроить окружение
cp .env.example .env
nano .env  # Fill in your values | Заполнить значения

# 5. Test run | Тестовый запуск
python bot.py

# 6. Install as service | Установить как сервис
sudo ./setup_sysvinit.sh
```

---

## ⚙️ Configuration | Конфигурация

### Environment Variables | Переменные окружения

| Variable | Description | Описание |
|----------|-------------|----------|
| `TG_BOT_TOKEN` | Telegram Bot API token | Токен Telegram бота |
| `TG_ADMIN_ID` | Your Telegram user ID | Ваш Telegram user ID |
| `GLPI_URL` | GLPI base URL | Базовый URL GLPI |
| `GLPI_APP_TOKEN` | GLPI API Application token | Токен приложения GLPI API |
| `GLPI_USER_TOKEN` | GLPI User API token | Пользовательский токен GLPI |
| `GLPI_MY_ID` | Your GLPI User ID | Ваш ID пользователя в GLPI |
| `GLPI_CHECK_INTERVAL` | Polling interval (seconds) | Интервал проверки (секунды) |

### Getting GLPI Tokens | Получение токенов GLPI

1. **App Token:** GLPI → Setup → General → API → Add API client
2. **User Token:** GLPI → My Settings → Remote access keys → Regenerate

---

## 📱 Bot Commands | Команды бота

| Command | Description | Описание |
|---------|-------------|----------|
| `/start` | Main menu | Главное меню |
| `/approvals` | Supervisor mode (all approvals) | Режим супервизора |
| `/my_tickets` | Your active tickets | Ваши активные заявки |
| `/help` | Help information | Справка |

---

## 🔧 Service Management | Управление сервисом

```bash
# Start | Запустить
sudo service director-bot start

# Stop | Остановить
sudo service director-bot stop

# Restart | Перезапустить
sudo service director-bot restart

# Status | Статус
sudo service director-bot status

# View logs | Просмотр логов
tail -f logs/service.log
```

---

## 🧠 Business Logic for AI Agents | Бизнес-логика для AI-агентов

### GLPI API Specifics (Critical\!)

1. **Validation Fetching:**
   - Use direct `GET /TicketValidation` (NOT Search API)
   - Search API has unreliable Field ID mapping

2. **Requester Name Resolution:**
   - Standard `expand_dropdowns=true` does NOT return requester
   - Fetch via `GET /Ticket/{id}/Ticket_User` where `type=1`

3. **Ticket Creation:**
   - Use `_users_id_requester` (with underscore\!) as array: `[user_id]`
   - Fetch `locations_id` from user profile before creating

4. **ID Resolution Pattern:**
   - NEVER show raw IDs to users
   - Always resolve: `_get_user_name(id)`, `_get_location_name(id)`

5. **Search API Field IDs:**
   ```
   1=Title, 2=ID, 4=Requester, 5=Technician, 12=Status,
   15=Date, 21=Content, 65=ObserverGroup, 66=ObserverUser, 83=Location
   ```

---

## 📄 License | Лицензия

MIT License - Feel free to use and modify.

---

## 🤝 Contributing | Участие

Pull requests welcome\! Please read CLAUDE.md for code style guidelines.

---

**Made with ❤️ for IT Directors who need to approve tickets at 3 AM**
