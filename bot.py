#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🕴️ DIRECTOR ASSISTANT BOT
Специализированный бот для согласования заявок GLPI
Путь: /home/maimik/Projects/director/bot.py
"""

import os
import asyncio
import logging
import sqlite3
import html
import re
from datetime import datetime, timedelta
from pathlib import Path
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, 
    InlineKeyboardMarkup, BotCommand, ReplyKeyboardRemove
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiohttp
from dotenv import load_dotenv

# Загрузка конфига
load_dotenv()
PROJECT_ROOT = Path(__file__).parent
DATABASE_PATH = PROJECT_ROOT / "data" / "director.db"
LOG_FILE = PROJECT_ROOT / "logs" / "bot.log"

# === КОНФИГУРАЦИЯ ===
class Config:
    BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
    ADMIN_ID = int(os.getenv("TG_ADMIN_ID", "0"))

    GLPI_URL = os.getenv("GLPI_URL", "").rstrip('/')
    GLPI_APP_TOKEN = os.getenv("GLPI_APP_TOKEN")
    GLPI_USER_TOKEN = os.getenv("GLPI_USER_TOKEN")
    GLPI_MY_ID = int(os.getenv("GLPI_MY_ID", "21"))
    CHECK_INTERVAL = int(os.getenv("GLPI_CHECK_INTERVAL", "300"))

# === ЛОГИРОВАНИЕ ===
if not os.path.exists(LOG_FILE.parent):
    os.makedirs(LOG_FILE.parent)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# === GLPI API CLIENT ===
class GLPIClient:
    def __init__(self):
        self.session_token = None
        self.headers = {
            "Content-Type": "application/json",
            "App-Token": Config.GLPI_APP_TOKEN
        }
        # Память для отправленных уведомлений о валидациях
        self.notified_validations = set()
        # Ticket IDs, уведомлённые через согласования (для дедупликации)
        self.notified_ticket_ids = set()

    async def init_session(self):
        """Авторизация и переключение в режим Global View"""
        try:
            url = f"{Config.GLPI_URL}/apirest.php/initSession"
            headers = self.headers.copy()
            headers["Authorization"] = f"user_token {Config.GLPI_USER_TOKEN}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.session_token = data.get("session_token")
                        
                        # Логируем текущую сущность
                        current_entity = data.get("session", {}).get("glpiactive_entity", "Unknown")
                        logger.info(f"✅ GLPI Session initialized. Current Entity ID: {current_entity}")
                        
                        # Переключаемся в режим Global View (Root entity с рекурсией)
                        if await self._enable_global_view():
                            logger.info("✅ Global View enabled: entities_id=0, recursive=true")
                        else:
                            logger.warning("⚠️ Failed to enable Global View, using current entity")
                        
                        return True
                    logger.error(f"GLPI Auth failed: {resp.status}")
                    return False
        except Exception as e:
            logger.error(f"Connection error: {e}")
            return False
    
    async def _enable_global_view(self):
        """Переключение в режим просмотра всех сущностей (Root + recursive)"""
        try:
            url = f"{Config.GLPI_URL}/apirest.php/changeActiveEntities"
            payload = {
                "entities_id": 0,  # Root entity
                "is_recursive": True  # Включить рекурсивный просмотр
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=self.get_headers(), json=payload) as resp:
                    if resp.status in [200, 201]:
                        logger.info("🔍 Recursive search enabled for all entities")
                        return True
                    logger.warning(f"changeActiveEntities returned: {resp.status}")
                    return False
        except Exception as e:
            logger.error(f"Error enabling global view: {e}")
            return False

    def get_headers(self):
        if not self.session_token:
            return None
        h = self.headers.copy()
        h["Session-Token"] = self.session_token
        return h

    async def clean_text(self, text):
        """Очистка HTML от GLPI"""
        if not text: return ""
        text = html.unescape(str(text))
        text = re.sub(r'<[^>]+>', '\n', text)
        return "\n".join([line.strip() for line in text.splitlines() if line.strip()])

    async def get_pending_validations(self):
        """Поиск заявок на согласование для директора (DIRECT OBJECT RETRIEVAL)"""
        if not self.session_token:
            await self.init_session()
        
        # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ v2.3:
        # Отказываемся от ненадежного Search API (проблемы с Field ID mapping)
        # Используем прямое получение объектов /TicketValidation
        # Это дает нам чистый JSON с именованными ключами: id, tickets_id, users_id_validate, status
        
        validations = []
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/TicketValidation"
                params = {
                    "range": "0-100",      # Лимит на 100 записей
                    "order": "DESC",       # Сортировка по убыванию
                    "sort": "id",          # Сортировать по ID
                    "is_deleted": 0        # Только активные (не в корзине)
                }
                
                logger.info(f"🚀 Fetching validations from: {url}")
                
                async with session.get(url, headers=self.get_headers(), params=params) as resp:
                    if resp.status != 200:
                        logger.error(f"❌ API Error: HTTP {resp.status}")
                        return []
                    
                    raw_data = await resp.json()
                    
                    # Логируем первый элемент для диагностики структуры
                    if raw_data and isinstance(raw_data, list) and len(raw_data) > 0:
                        logger.info(f"📦 First item sample: {raw_data[0]}")
                    
                    my_id = Config.GLPI_MY_ID
                    
                    # Фильтруем в Python (надежнее, чем полагаться на GLPI Search API)
                    for item in raw_data:
                        try:
                            # Извлекаем ключевые поля
                            status = int(item.get('status', 0))
                            validator_id = int(item.get('users_id_validate', 0))
                            
                            # Фильтр: Status = 2 (Waiting) И Validator = Я
                            if status == 2 and validator_id == my_id:
                                validations.append({
                                    'id': item['id'],
                                    'ticket_id': item['tickets_id'],
                                    'comment_submission': item.get('comment_submission', '')
                                })
                                logger.info(f"  ✅ Validation ID: {item['id']}, Ticket ID: {item['tickets_id']}, Validator: {validator_id}")
                        except (KeyError, ValueError, TypeError) as e:
                            logger.warning(f"  ⚠️ Skipping malformed item: {e}")
                            continue
                    
                    logger.info(f"✅ Found {len(validations)} pending validations for User {my_id}")
                    return validations
                    
        except Exception as e:
            logger.error(f"❌ Validation fetch error: {e}")
            return []
    
    async def get_all_pending_validations(self):
        """Получить ВСЕ ожидающие согласования (режим супервизора)"""
        if not self.session_token:
            await self.init_session()
        
        validations = []
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/TicketValidation"
                params = {
                    "range": "0-50",
                    "order": "DESC",
                    "sort": "id",
                    "is_deleted": 0
                }
                
                async with session.get(url, headers=self.get_headers(), params=params) as resp:
                    if resp.status != 200:
                        return []
                    
                    raw_data = await resp.json()
                    
                    for item in raw_data:
                        try:
                            status = int(item.get("status", 0))
                            if status == 2:  # Waiting
                                validator_id = int(item.get("users_id_validate", 0))
                                validator_name = await self._get_user_name(validator_id)
                                
                                validations.append({
                                    "id": item["id"],
                                    "ticket_id": item["tickets_id"],
                                    "validator_id": validator_id,
                                    "validator_name": validator_name,
                                    "is_mine": validator_id == Config.GLPI_MY_ID
                                })
                        except (KeyError, ValueError, TypeError):
                            continue
                    
                    return validations
                    
        except Exception as e:
            logger.error(f"Error fetching all validations: {e}")
            return []

    async def get_ticket_details(self, ticket_id):
        """Получить детали тикета по ID с информацией о заявителе"""
        try:
            async with aiohttp.ClientSession() as session:
                # 1. Получаем основные данные тикета
                url = f"{Config.GLPI_URL}/apirest.php/Ticket/{ticket_id}"
                params = {"expand_dropdowns": "true"}
                async with session.get(url, headers=self.get_headers(), params=params) as resp:
                    if resp.status != 200:
                        return None
                    ticket = await resp.json()
                
                # 2. Получаем связанных пользователей через Ticket_User
                users_url = f"{Config.GLPI_URL}/apirest.php/Ticket/{ticket_id}/Ticket_User"
                async with session.get(users_url, headers=self.get_headers()) as resp:
                    if resp.status == 200:
                        ticket_users = await resp.json()
                        # Ищем заявителя (type=1)
                        for tu in ticket_users:
                            if tu.get('type') == 1:  # Requester
                                user_id = tu.get('users_id')
                                if user_id:
                                    # Получаем имя пользователя
                                    user_name = await self._get_user_name(user_id)
                                    ticket['_users_id_requester'] = user_name
                                break
                
                return ticket
        except Exception as e:
            logger.error(f"Error in get_ticket_details: {e}")
            return None

    async def _clean_html(self, html_content):
        """Очистка HTML с использованием BeautifulSoup или regex"""
        if not html_content:
            return ""
        try:
            # Пробуем использовать BeautifulSoup, если установлен
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, "html.parser")
            text = soup.get_text(separator="\n")
            # Убираем лишние пробелы и пустые строки
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            return "\n".join(lines)
        except ImportError:
            # Fallback на regex
            import re
            text = re.sub(r'<[^>]+>', '\n', str(html_content))
            text = html.unescape(text)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            return "\n".join(lines)
    
    def clean_html_to_text(self, html_content):
        """
        Очистка HTML для безопасного отображения в Telegram
        1. Unescape HTML entities
        2. Заменяем <br>, <p>, </div> на переводы строк
        3. Удаляем все остальные теги
        4. Экранируем для Telegram HTML mode
        """
        if not html_content:
            return ""
        
        # Шаг 1: Unescape HTML entities (&lt; -> <, &amp; -> &)
        text = html.unescape(str(html_content))
        
        # Шаг 2: Заменяем теги переноса строк на \n
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<p>', '\n', text, flags=re.IGNORECASE)
        
        # Шаг 3: Удаляем все остальные HTML-теги
        text = re.sub(r'<[^>]+>', '', text)
        
        # Шаг 4: Убираем лишние пробелы и пустые строки
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)
        
        # Шаг 5: Экранируем для безопасной вставки в Telegram HTML
        return html.escape(text)

    async def get_active_tickets(self):
        """Получить активные тикеты где пользователь — Requester, Assignee или Observer."""
        if not self.session_token:
            await self.init_session()

        async def _fetch_by_role(field_id, role_name):
            """Запрос тикетов по роли пользователя"""
            return await _do_fetch(field_id, Config.GLPI_MY_ID, role_name)

        async def _fetch_by_role_group(field_id, group_id, role_name):
            """Запрос тикетов по роли группы"""
            return await _do_fetch(field_id, group_id, role_name)

        async def _do_fetch(field_id, value, role_name):
            """Общий запрос тикетов"""
            params = {
                "criteria[0][field]": field_id,
                "criteria[0][searchtype]": "equals",
                "criteria[0][value]": value,
                "is_deleted": 0,
                # Field IDs: 2=ID, 1=Title, 12=Status, 15=Date, 21=Content, 83=Location, 4=Requester, 5=Tech
                "forcedisplay[0]": 2,
                "forcedisplay[1]": 1,
                "forcedisplay[2]": 12,
                "forcedisplay[3]": 15,
                "forcedisplay[4]": 21,
                "forcedisplay[5]": 83,
                "forcedisplay[6]": 4,
                "forcedisplay[7]": 5,
                "range": "0-100",
                "sort": "2",
                "order": "DESC",
            }

            try:
                async with aiohttp.ClientSession() as session:
                    url = f"{Config.GLPI_URL}/apirest.php/search/Ticket"
                    async with session.get(url, headers=self.get_headers(), params=params) as resp:
                        if resp.status in [200, 206]:
                            data = await resp.json()
                            results = []
                            for item in data.get("data", []):
                                # GLPI returns string keys: '2', '1', '12', '15', '21'
                                try:
                                    status_val = int(item.get("12", 0))
                                except (ValueError, TypeError):
                                    status_val = 0

                                results.append({
                                    "id": item.get("2"),
                                    "title": item.get("1", "Без названия"),
                                    "status": status_val,
                                    "date": item.get("15", ""),
                                    "content": item.get("21", ""),
                                    "location_name": item.get("83", ""),
                                    "requester_name": item.get("4", ""),
                                    "technician_name": item.get("5", ""),
                                })
                            logger.info(f"  {role_name}: {len(results)} tickets")
                            return results
                        else:
                            logger.warning(f"  {role_name}: HTTP {resp.status}")
                            return []
            except Exception as e:
                logger.error(f"  {role_name} error: {e}")
                return []

        logger.info("Fetching tickets by role...")

        # 3 separate requests for user roles
        tickets_requester = await _fetch_by_role(4, "Requester")
        tickets_assignee = await _fetch_by_role(5, "Assignee")
        tickets_observer = await _fetch_by_role(66, "Observer")

        # Also fetch by group observer (field 65)
        all_tickets = tickets_requester + tickets_assignee + tickets_observer

        group_ids = await self.get_user_groups()
        for gid in group_ids:
            group_tickets = await _fetch_by_role_group(65, gid, f"ObserverGroup-{gid}")
            all_tickets.extend(group_tickets)

        # Merge and deduplicate by ID, filter out Closed (6)
        merged = {}
        for ticket in all_tickets:
            tid = ticket.get("id")
            if tid and tid not in merged:
                status = ticket.get("status", 0)
                if status != 6:  # 6 = Closed
                    merged[tid] = ticket

        # Sort by ID descending
        result = sorted(merged.values(), key=lambda x: int(x.get("id", 0)), reverse=True)

        await self._resolve_ticket_extra_fields(result)

        logger.info(f"Total unique active tickets: {len(result)}")
        return result

    async def get_all_active_tickets(self):
        """Получить ВСЕ активные тикеты в системе (статусы 1-5, не закрытые).

        В отличие от get_active_tickets(), не фильтрует по роли пользователя (Requester/
        Assignee/Observer) — используется фоновым монитором (check_tickets), которому нужно
        видеть новые/изменённые тикеты, даже если директор формально не участник (GLPI не
        добавляет группу Administrators как observer автоматически). НЕ использовать для
        /my_tickets — там нужен именно отфильтрованный по роли список (get_active_tickets).

        Ловушка (реальный инцидент 2026-07-03/04): `criteria[searchtype]=notequals` на поле
        12 (Status) в этой версии GLPI Search API работает НЕ как исключение — вместо "статус
        != 6" он фактически вернул 398 тикетов СО СТАТУСОМ 6 (Closed) при totalcount=407, где
        реально активных было только 9. `is_deleted` и range-пагинация тут ни при чём, баг
        воспроизводится и одним запросом на весь диапазон. Единственный надёжный способ —
        явный OR по `equals` для каждого активного статуса (1..5), как это уже делает
        sysadmin-bot (`services/glpi.py: get_active_tickets`, "Fetched N active tickets
        (statuses 1-5)"). НЕ возвращать `criteria[searchtype]=notequals` для этого поля.
        """
        if not self.session_token:
            await self.init_session()

        results = []
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/search/Ticket"
                params = {
                    "criteria[0][field]": 12,
                    "criteria[0][searchtype]": "equals",
                    "criteria[0][value]": 1,
                    "forcedisplay[0]": 2,
                    "forcedisplay[1]": 1,
                    "forcedisplay[2]": 12,
                    "forcedisplay[3]": 15,
                    "forcedisplay[4]": 21,
                    "forcedisplay[5]": 83,
                    "forcedisplay[6]": 4,
                    "forcedisplay[7]": 5,
                    "range": "0-999",
                    "sort": "2",
                    "order": "DESC",
                }
                for i, st in enumerate([2, 3, 4, 5], start=1):
                    params[f"criteria[{i}][link]"] = "OR"
                    params[f"criteria[{i}][field]"] = 12
                    params[f"criteria[{i}][searchtype]"] = "equals"
                    params[f"criteria[{i}][value]"] = st
                async with session.get(url, headers=self.get_headers(), params=params) as resp:
                    if resp.status in [200, 206]:
                        data = await resp.json()
                        total = data.get("totalcount", 0)
                        for item in data.get("data", []):
                            try:
                                status_val = int(item.get("12", 0))
                            except (ValueError, TypeError):
                                status_val = 0
                            results.append({
                                "id": item.get("2"),
                                "title": item.get("1", "Без названия"),
                                "status": status_val,
                                "date": item.get("15", ""),
                                "content": item.get("21", ""),
                                "location_name": item.get("83", ""),
                                "requester_name": item.get("4", ""),
                                "technician_name": item.get("5", ""),
                            })
                        logger.info(f"  All active tickets: {len(results)} (statuses 1-5, total={total})")
                    else:
                        logger.warning(f"  All active tickets: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"  All active tickets error: {e}")

        await self._resolve_ticket_extra_fields(results)

        logger.info(f"Total unique active tickets: {len(results)}")
        return results

    async def _resolve_ticket_extra_fields(self, tickets):
        """Дозаполняет location/priority/date_creation/updater + имена requester/technician.

        Search API не возвращает location/priority надёжно (Field 83 = None), поэтому
        для каждого тикета делаем прямой GET /Ticket/{id}. Используется и
        get_active_tickets(), и get_all_active_tickets() — общая логика, было продублировано.
        """
        for ticket in tickets:
            tid = ticket.get("id")

            # Fetch extra fields from direct Ticket API (Search API returns None for location/priority)
            try:
                async with aiohttp.ClientSession() as api_session:
                    ticket_url = f"{Config.GLPI_URL}/apirest.php/Ticket/{tid}"
                    async with api_session.get(ticket_url, headers=self.get_headers()) as resp:
                        if resp.status == 200:
                            ticket_data = await resp.json()
                            loc_id = ticket_data.get("locations_id")
                            if loc_id and loc_id != 0:
                                ticket["location_name"] = await self._get_location_name(loc_id)
                            else:
                                ticket["location_name"] = "Не указано"
                            ticket["priority"] = ticket_data.get("priority", 3)
                            ticket["date_creation"] = ticket_data.get("date_creation", "")
                            ticket["users_id_lastupdater"] = ticket_data.get("users_id_lastupdater", 0)
                        else:
                            ticket["location_name"] = "Не указано"
                            ticket["priority"] = 3
                            ticket["date_creation"] = ""
                            ticket["users_id_lastupdater"] = 0
            except Exception as e:
                logger.warning(f"Failed to fetch ticket details for {tid}: {e}")
                ticket["location_name"] = "Не указано"
                ticket["priority"] = 3
                ticket["date_creation"] = ""
                ticket["users_id_lastupdater"] = 0

            # Requester ID -> Name (from Search API Field 4)
            req_id = ticket.get("requester_name")
            if req_id:
                try:
                    ticket["requester_name"] = await self._get_user_name(int(req_id))
                except (ValueError, TypeError):
                    ticket["requester_name"] = "Неизвестно"
            else:
                ticket["requester_name"] = "Неизвестно"

            # Technician ID -> Name (from Search API Field 5)
            tech_id = ticket.get("technician_name")
            if tech_id:
                try:
                    ticket["technician_name"] = await self._get_user_name(int(tech_id))
                except (ValueError, TypeError):
                    ticket["technician_name"] = ""
            else:
                ticket["technician_name"] = ""

    async def _get_entity_name(self, entity_id):
        """Получить название филиала по ID"""
        if not entity_id:
            return "Неизвестно"
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/Entity/{entity_id}"
                async with session.get(url, headers=self.get_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get('name', 'Неизвестно')
                    return f"Entity #{entity_id}"
        except Exception as e:
            logger.error(f"Error fetching entity name: {e}")
            return f"Entity #{entity_id}"
    
    async def _get_user_name(self, user_id):
        """Получить имя пользователя по ID"""
        if not user_id:
            return "Неизвестно"
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/User/{user_id}"
                async with session.get(url, headers=self.get_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Формируем полное имя
                        firstname = data.get('firstname', '')
                        realname = data.get('realname', '')
                        if firstname and realname:
                            return f"{firstname} {realname}"
                        elif data.get('name'):
                            return data.get('name')
                        return "Неизвестно"
                    return f"User #{user_id}"
        except Exception as e:
            logger.error(f"Error fetching user name: {e}")
            return f"User #{user_id}"
    
    async def _get_user_profile(self, user_id):
        """Получить профиль пользователя (включая locations_id)"""
        if not user_id:
            return None
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/User/{user_id}"
                async with session.get(url, headers=self.get_headers()) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        except Exception as e:
            logger.error(f"Error fetching user profile: {e}")
            return None
    
    async def get_user_groups(self):
        """Получить список групп пользователя"""
        if not self.session_token:
            await self.init_session()
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/User/{Config.GLPI_MY_ID}/Group_User"
                async with session.get(url, headers=self.get_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        group_ids = [item.get("groups_id") for item in data if item.get("groups_id")]
                        logger.info(f"User groups: {group_ids}")
                        return group_ids
                    return []
        except Exception as e:
            logger.error(f"Error fetching user groups: {e}")
            return []
    
    async def _get_location_name(self, location_id):
        """Получить название локации по ID"""
        if not location_id:
            return "Неизвестно"
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/Location/{location_id}"
                async with session.get(url, headers=self.get_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("completename") or data.get("name") or f"Location #{location_id}"
                    return f"Location #{location_id}"
        except Exception as e:
            logger.error(f"Error fetching location: {e}")
            return f"Location #{location_id}"

    async def _get_ticket_solution(self, ticket_id):
        """Получить последнее решение тикета (ITILSolution)"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/Ticket/{ticket_id}/ITILSolution"
                params = {"range": "0-1", "order": "DESC", "sort": "id"}
                async with session.get(url, headers=self.get_headers(), params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and isinstance(data, list) and len(data) > 0:
                            sol = data[0]
                            user_id = sol.get("users_id", 0)
                            content = sol.get("content", "")
                            user_name = await self._get_user_name(user_id) if user_id else "Неизвестно"
                            return {
                                "user_name": user_name,
                                "content": self.clean_html_to_text(content)
                            }
            return None
        except Exception as e:
            logger.error(f"Error fetching solution for ticket {ticket_id}: {e}")
            return None

    async def _get_ticket_followup(self, ticket_id):
        """Получить последний комментарий (ITILFollowup) тикета"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/Ticket/{ticket_id}/ITILFollowup"
                params = {"range": "0-1", "order": "DESC", "sort": "id"}
                async with session.get(url, headers=self.get_headers(), params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and isinstance(data, list) and len(data) > 0:
                            fu = data[0]
                            user_id = fu.get("users_id", 0)
                            content = fu.get("content", "")
                            user_name = await self._get_user_name(user_id) if user_id else "GLPI"
                            return {
                                "user_name": user_name,
                                "content": self.clean_html_to_text(content)
                            }
            return None
        except Exception as e:
            logger.error(f"Error fetching followup for ticket {ticket_id}: {e}")
            return None

    async def get_ticket_followups(self, ticket_id):
        """Получить ВСЕ комментарии (ITILFollowup) тикета"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/Ticket/{ticket_id}/ITILFollowup"
                params = {"range": "0-99"}
                async with session.get(url, headers=self.get_headers(), params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and isinstance(data, list):
                            result = []
                            for fu in data:
                                user_id = fu.get("users_id", 0)
                                content = fu.get("content", "")
                                date_creation = fu.get("date_creation", "")
                                user_name = await self._get_user_name(user_id) if user_id else "GLPI"
                                result.append({
                                    "user_name": user_name,
                                    "content": self.clean_html_to_text(content),
                                    "date_creation": date_creation
                                })
                            return result
            return []
        except Exception as e:
            logger.error(f"Error fetching followups for ticket {ticket_id}: {e}")
            return []

    async def get_ticket_solutions(self, ticket_id):
        """Получить ВСЕ решения (ITILSolution) тикета"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/Ticket/{ticket_id}/ITILSolution"
                params = {"range": "0-99"}
                async with session.get(url, headers=self.get_headers(), params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and isinstance(data, list):
                            result = []
                            for sol in data:
                                user_id = sol.get("users_id", 0)
                                content = sol.get("content", "")
                                date_creation = sol.get("date_creation", "")
                                user_name = await self._get_user_name(user_id) if user_id else "Неизвестно"
                                result.append({
                                    "user_name": user_name,
                                    "content": self.clean_html_to_text(content),
                                    "date_creation": date_creation
                                })
                            return result
            return []
        except Exception as e:
            logger.error(f"Error fetching solutions for ticket {ticket_id}: {e}")
            return []

    async def get_ticket_tasks(self, ticket_id):
        """Получить все задачи (TicketTask) тикета.

        Sub-resource is `TicketTask`, NOT `ITILTask` -- ITILTask is an abstract parent
        class in GLPI's data model, not a valid REST endpoint (400
        ERROR_RESOURCE_NOT_FOUND_NOR_COMMONDBTM). Status field on records is `state`,
        not `status`.
        """
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/Ticket/{ticket_id}/TicketTask"
                params = {"range": "0-49"}
                async with session.get(url, headers=self.get_headers(), params=params) as resp:
                    if resp.status == 200:
                        tasks = await resp.json()
                        return tasks if isinstance(tasks, list) else []
                    logger.warning(f"Error fetching tasks for ticket {ticket_id}: HTTP {resp.status}")
            return []
        except Exception as e:
            logger.error(f"Error fetching tasks for ticket {ticket_id}: {e}")
            return []

    async def get_ticket_technician(self, ticket_id):
        """Получить имя назначенного техника (Ticket_User type=2)"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/Ticket/{ticket_id}/Ticket_User"
                async with session.get(url, headers=self.get_headers()) as resp:
                    if resp.status == 200:
                        users = await resp.json()
                        for user in users:
                            if user.get("type") == 2:
                                uid = user.get("users_id")
                                return await self._get_user_name(int(uid)) if uid else None
            return None
        except Exception as e:
            logger.error(f"Error fetching technician for ticket {ticket_id}: {e}")
            return None

    async def get_ticket_validations(self, ticket_id):
        """Получить все согласования тикета"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/Ticket/{ticket_id}/TicketValidation"
                async with session.get(url, headers=self.get_headers()) as resp:
                    if resp.status == 200:
                        vals = await resp.json()
                        return vals if isinstance(vals, list) else []
            return []
        except Exception as e:
            logger.error(f"Error fetching validations for ticket {ticket_id}: {e}")
            return []

    async def _ensure_session(self):
        """Проверить сессию, при 401/403 — перелогиниться"""
        if not self.session_token:
            return await self.init_session()
        return True

    async def _api_request(self, method, endpoint, **kwargs):
        """Unified API request with auto-reauth on 401/403"""
        if not await self._ensure_session():
            return None
        for attempt in range(2):
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"{Config.GLPI_URL}/apirest.php{endpoint}"
                    func = getattr(session, method)
                    async with func(url, headers=self.get_headers(), **kwargs) as resp:
                        if resp.status in [401, 403] and attempt == 0:
                            logger.warning(f"GLPI session expired ({resp.status}), re-authenticating...")
                            await self.init_session()
                            continue
                        if resp.status in [200, 201, 206]:
                            return await resp.json()
                        return None
            except Exception as e:
                logger.error(f"API request {method} {endpoint}: {e}")
                if attempt == 0:
                    await self.init_session()
                    continue
                return None
        return None

    async def diagnose_search_options(self):
        """Диагностика SearchOptions для TicketValidation (проверка полей 3, 4, 7)"""
        if not self.session_token:
            await self.init_session()
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/listSearchOptions/TicketValidation"
                async with session.get(url, headers=self.get_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        # Проверяем ключевые поля
                        field_3 = data.get('3', {})  # Status
                        field_4 = data.get('4', {})  # Date
                        field_7 = data.get('7', {})  # Validator
                        
                        logger.info("🔍 GLPI SearchOptions Diagnostic:")
                        logger.info(f"  Field 3 (Status): {field_3.get('name', 'N/A')} - {field_3.get('field', 'N/A')}")
                        logger.info(f"  Field 4 (Date): {field_4.get('name', 'N/A')} - {field_4.get('field', 'N/A')}")
                        logger.info(f"  Field 7 (Validator): {field_7.get('name', 'N/A')} - UID: {field_7.get('uid', 'N/A')}")
                        logger.info(f"✅ Using Field 7 for users_id_validate search")
                        
                        return True
                    else:
                        logger.warning(f"⚠️ Failed to fetch SearchOptions: HTTP {resp.status}")
                        return False
        except Exception as e:
            logger.error(f"❌ Error in diagnose_search_options: {e}")
            return False

    async def update_validation(self, validation_id, status, comment=""):
        """Обновить статус валидации (3-Approve, 4-Refuse)"""
        if not self.session_token: await self.init_session()
        
        payload = {
            "input": {
                "id": validation_id,
                "status": status,
                "comment_validation": comment
            }
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/TicketValidation/{validation_id}"
                async with session.put(url, headers=self.get_headers(), json=payload) as resp:
                    return resp.status in [200, 201]
        except Exception as e:
            logger.error(f"Update validation error: {e}")
            return False

    async def add_ticket_followup(self, ticket_id, content):
        """Добавить комментарий (followup) к тикету"""
        if not self.session_token:
            await self.init_session()
        payload = {
            "input": {
                "items_id": ticket_id,
                "itemtype": "Ticket",
                "content": content,
                "is_private": 0
            }
        }
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/Ticket/{ticket_id}/ITILFollowup"
                async with session.post(url, headers=self.get_headers(), json=payload) as resp:
                    if resp.status in [200, 201]:
                        logger.info(f"Followup added to ticket #{ticket_id}")
                        return True
                    logger.error(f"Failed to add followup to #{ticket_id}: {resp.status}")
                    return False
        except Exception as e:
            logger.error(f"Error adding followup to ticket {ticket_id}: {e}")
            return False

    async def create_validation(self, ticket_id, validator_id, comment=""):
        """Создать согласование (TicketValidation) для другого пользователя"""
        if not self.session_token:
            await self.init_session()
        payload = {
            "input": {
                "tickets_id": ticket_id,
                "users_id_validate": validator_id,
                "comment_submission": comment,
                "status": 2  # WAITING
            }
        }
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/TicketValidation"
                async with session.post(url, headers=self.get_headers(), json=payload) as resp:
                    if resp.status in [200, 201]:
                        data = await resp.json()
                        val_id = data.get("id")
                        logger.info(f"Validation #{val_id} created for user {validator_id} on ticket #{ticket_id}")
                        return val_id
                    logger.error(f"Failed to create validation: {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"Error creating validation for ticket {ticket_id}: {e}")
            return None

    async def create_ticket(self, title, content, ticket_type=2):
        """Создание тикета с корректным указанием автора, локации и наблюдателя
        
        Args:
            title: Заголовок тикета
            content: Описание тикета
            ticket_type: Тип заявки (1=Инцидент, 2=Запрос). По умолчанию Запрос.
        """
        if not self.session_token:
            await self.init_session()
        
        # Получаем профиль пользователя для locations_id
        user_profile = await self._get_user_profile(Config.GLPI_MY_ID)
        locations_id = user_profile.get("locations_id", 0) if user_profile else 0
        
        payload = {
            "input": {
                "name": title,
                "content": content,
                "status": 1,  # New
                "priority": 3,
                "type": ticket_type,  # 1=Incident, 2=Request
                "entities_id": 0,  # Root entity для видимости везде
                "locations_id": locations_id,  # Локация из профиля пользователя
                "_users_id_requester": [Config.GLPI_MY_ID],  # Связать как Requester
                "_groups_id_observer": [1]  # Группа Administrators как наблюдатель
            }
        }
        
        logger.info(f"Creating ticket with locations_id={locations_id}, requester={Config.GLPI_MY_ID}")
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{Config.GLPI_URL}/apirest.php/Ticket"
                async with session.post(url, headers=self.get_headers(), json=payload) as resp:
                    if resp.status == 201:
                        data = await resp.json()
                        ticket_id = data.get("id")
                        logger.info(f"✅ Ticket #{ticket_id} created by Director (ID: {Config.GLPI_MY_ID})")
                        return ticket_id
                    else:
                        error_text = await resp.text()
                        logger.error(f"Failed to create ticket: {resp.status} - {error_text}")
                    return None
        except Exception as e:
            logger.error(f"Error creating ticket: {e}")
            return None

# === DATABASE ===
def init_db():
    if not os.path.exists(DATABASE_PATH.parent):
        os.makedirs(DATABASE_PATH.parent)
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS processed_validations (id INTEGER PRIMARY KEY, glpi_id INTEGER UNIQUE)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY,
                glpi_id INTEGER UNIQUE,
                status INTEGER,
                title TEXT,
                last_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

# === STATES ===
class Form(StatesGroup):
    waiting_for_refusal = State()
    waiting_for_ticket_type = State()
    waiting_for_ticket_title = State()
    waiting_for_ticket_desc = State()
    waiting_for_review_comment = State()

# === BOT SETUP ===
bot = Bot(token=Config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
glpi = GLPIClient()

# === HANDLERS ===

def get_main_menu_kb():
    """Клавиатура главного меню"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Проверить согласования", callback_data="check_validations")],
        [InlineKeyboardButton(text="📂 Мои заявки", callback_data="my_tickets")],
        [InlineKeyboardButton(text="➕ Создать заявку", callback_data="create_ticket")]
    ])

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if message.from_user.id != Config.ADMIN_ID: return
    await state.clear()
    await message.answer(f"👋 Добрый день! Я готов к работе.\n\nGLPI ID: {Config.GLPI_MY_ID}", reply_markup=get_main_menu_kb())

@router.callback_query(F.data == "main_menu")
async def callback_main_menu(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await call.message.answer("🏠 Главное меню", reply_markup=get_main_menu_kb())

@router.message(Command("approvals"))
async def cmd_approvals(message: Message):
    """Команда /approvals - показать все согласования"""
    if message.from_user.id != Config.ADMIN_ID:
        return

    # Создаём фейковый CallbackQuery для переиспользования логики
    validations = await glpi.get_all_pending_validations()

    if not validations:
        await message.answer("✅ Нет ожидающих согласований.")
        return

    status_info = {
        1: ("🟢", "Новый"),
        2: ("🟡", "В работе"),
        3: ("🔵", "Запланирован"),
        4: ("🟣", "Ожидание"),
        5: ("✅", "Решён"),
    }

    lines = ["📋 <b>СТАТУС СОГЛАСОВАНИЙ</b>", ""]
    shown_count = 0

    for val in validations:
        if shown_count >= 10:
            break

        ticket_id = val.get("ticket_id", "?")
        validator_name = val.get("validator_name", "Неизвестно")
        is_mine = val.get("is_mine", False)

        ticket = await glpi.get_ticket_details(ticket_id)
        if not ticket or ticket.get("is_deleted") == 1:
            continue

        ticket_status = ticket.get("status", 0)
        try:
            ticket_status = int(ticket_status)
        except:
            ticket_status = 0

        if ticket_status == 6:
            continue

        title = html.escape(str(ticket.get("name", "Без названия"))[:45])
        date_str = str(ticket.get("date_creation", "") or "")[:10]
        raw_content = ticket.get("content", "")
        clean_content = glpi.clean_html_to_text(raw_content)[:100]
        if len(raw_content) > 100:
            clean_content += "..."

        requester_name = ticket.get("_users_id_requester", "Не указан")
        emoji, status_name = status_info.get(ticket_status, ("⚪", f"Статус {ticket_status}"))

        lines.append(f"🎫 <b>#{ticket_id}</b> — {title}")
        lines.append(f"   📅 {date_str} | {emoji} {status_name}")
        if clean_content:
            lines.append(f"   📄 <i>{clean_content}</i>")
        lines.append(f"   👷 <b>Инициатор:</b> {html.escape(str(requester_name))}")
        if is_mine:
            lines.append(f"   🔴 <b>Ожидает согласования:</b> ВАС!")
        else:
            lines.append(f"   ⏳ <b>Ожидает согласования:</b> {html.escape(validator_name)}")
        lines.append("")
        shown_count += 1

    if shown_count == 0:
        await message.answer("✅ Нет активных согласований.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="check_validations")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]
    ])

    msg = chr(10).join(lines)
    await message.answer(msg, parse_mode="HTML", reply_markup=kb)

@router.message(Command("my_tickets"))
async def cmd_my_tickets(message: Message):
    """Команда /my_tickets - мои активные заявки"""
    if message.from_user.id != Config.ADMIN_ID:
        return

    tickets = await glpi.get_active_tickets()

    if not tickets:
        await message.answer("✅ Активных заявок нет.")
        return

    status_info = {
        1: ("🟢", "Новый"),
        2: ("🟡", "В работе"),
        3: ("🔵", "Запланирован"),
        4: ("🟣", "Ожидание"),
        5: ("✅", "Решён"),
    }

    lines = ["📂 <b>МОИ ЗАЯВКИ</b>", ""]

    for ticket in tickets[:10]:
        tid = ticket.get("id", "?")
        title = html.escape(str(ticket.get("title", "Без названия"))[:50])
        status = ticket.get("status", 0)
        date_str = str(ticket.get("date", ""))[:10]
        raw_content = ticket.get("content", "")

        clean_content = glpi.clean_html_to_text(raw_content)[:100]
        if len(raw_content) > 100:
            clean_content += "..."

        emoji, status_name = status_info.get(status, ("⚪", f"Статус {status}"))
        
        # Location name (already resolved in get_active_tickets)
        location_name = ticket.get("location_name", "Не указано")
        safe_location = html.escape(str(location_name))

        lines.append(f"🎫 <b>#{tid}</b> — {title}")
        lines.append(f"   🏢 {safe_location}")
        lines.append(f"   📅 {date_str} | {emoji} {status_name}")
        if clean_content:
            lines.append(f"   📝 <i>{clean_content}</i>")
        lines.append("")

    if len(tickets) > 10:
        lines.append(f"<i>...и ещё {len(tickets) - 10} заявок</i>")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="my_tickets")],
        [InlineKeyboardButton(text="🔗 Открыть GLPI", url=f"{Config.GLPI_URL}/front/ticket.php")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]
    ])

    msg = chr(10).join(lines)
    await message.answer(msg, parse_mode="HTML", reply_markup=kb)

@router.message(Command("help"))
async def cmd_help(message: Message):
    """Команда /help - помощь"""
    if message.from_user.id != Config.ADMIN_ID:
        return

    help_text = (
        "ℹ️ <b>ПОМОЩЬ</b>\n\n"
        "<b>Команды:</b>\n"
        "/start — Главное меню\n"
        "/approvals — Статус всех согласований\n"
        "/my_tickets — Мои активные заявки\n"
        "/help — Эта справка\n\n"
        "<b>Функции:</b>\n"
        "• Уведомления о новых заявках на согласование\n"
        "• Согласование/отклонение заявок из Telegram\n"
        "• Мониторинг статуса всех согласований\n"
        "• Просмотр активных заявок"
    )
    await message.answer(help_text, parse_mode="HTML")

@router.callback_query(F.data == "check_validations")
async def manual_check(call: CallbackQuery):
    """Режим супервизора: показать ВСЕ ожидающие согласования"""
    await call.answer("Проверяю согласования...")
    
    validations = await glpi.get_all_pending_validations()
    
    if not validations:
        await call.message.answer("✅ Нет ожидающих согласований.")
        return
    
    # Статусы для отображения
    status_info = {
        1: ("🟢", "Новый"),
        2: ("🟡", "В работе"),
        3: ("🔵", "Запланирован"),
        4: ("🟣", "Ожидание"),
        5: ("✅", "Решён"),
    }
    
    lines = ["📋 <b>СТАТУС СОГЛАСОВАНИЙ</b>", ""]
    shown_count = 0
    
    for val in validations:
        if shown_count >= 10:
            break
        
        ticket_id = val.get("ticket_id", "?")
        validator_name = val.get("validator_name", "Неизвестно")
        is_mine = val.get("is_mine", False)
        
        # Получаем полные данные тикета
        ticket = await glpi.get_ticket_details(ticket_id)
        
        # Пропускаем если тикет не найден (404)
        if not ticket:
            continue
        
        # Пропускаем удалённые тикеты
        if ticket.get("is_deleted") == 1:
            continue
        
        # Пропускаем закрытые тикеты
        ticket_status = ticket.get("status", 0)
        try:
            ticket_status = int(ticket_status)
        except (ValueError, TypeError):
            ticket_status = 0
        
        if ticket_status == 6:  # Closed
            continue
        
        # Извлекаем данные тикета
        title = html.escape(str(ticket.get("name", "Без названия"))[:45])
        date_str = str(ticket.get("date_creation", "") or ticket.get("date", ""))[:10]
        raw_content = ticket.get("content", "")
        clean_content = glpi.clean_html_to_text(raw_content)[:100]
        if len(raw_content) > 100:
            clean_content += "..."
        
        # Имя инициатора (заявителя)
        requester_name = ticket.get("_users_id_requester", "Неизвестно")
        if not requester_name or requester_name == "Неизвестно":
            requester_name = "Не указан"
        
        emoji, status_name = status_info.get(ticket_status, ("⚪", f"Статус {ticket_status}"))
        
        # Формируем блок по шаблону
        lines.append(f"🎫 <b>#{ticket_id}</b> — {title}")
        lines.append(f"   📅 {date_str} | {emoji} {status_name}")
        if clean_content:
            lines.append(f"   📄 <i>{clean_content}</i>")
        lines.append(f"   👷 <b>Инициатор:</b> {html.escape(str(requester_name))}")
        if is_mine:
            lines.append(f"   🔴 <b>Ожидает согласования:</b> ВАС!")
        else:
            lines.append(f"   ⏳ <b>Ожидает согласования:</b> {html.escape(validator_name)}")
        lines.append("")
        shown_count += 1
    
    if shown_count == 0:
        await call.message.answer("✅ Нет активных согласований.")
        return
    
    remaining = len(validations) - shown_count
    if remaining > 0:
        lines.append(f"<i>...и ещё {remaining} согласований</i>")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="check_validations")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]
    ])
    
    msg = chr(10).join(lines)
    await call.message.answer(msg, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "my_tickets")
async def my_tickets_handler(call: CallbackQuery):
    """Показать список активных заявок пользователя"""
    await call.answer("Загружаю заявки...")
    
    tickets = await glpi.get_active_tickets()
    
    if not tickets:
        await call.message.answer("✅ Активных заявок нет.")
        return
    
    # Статусы для отображения (все кроме 6=Closed)
    status_info = {
        1: ("🟢", "Новый"),
        2: ("🟡", "В работе"),
        3: ("🔵", "Запланирован"),
        4: ("🟣", "Ожидание"),
        5: ("✅", "Решён"),
    }
    
    # Формируем список заявок
    lines = ["📂 <b>МОИ ЗАЯВКИ</b>", ""]
    
    for ticket in tickets[:10]:  # Лимит 10 заявок (с контентом занимает больше места)
        tid = ticket.get("id", "?")
        title = html.escape(str(ticket.get("title", "Без названия"))[:50])
        status = ticket.get("status", 0)
        date_str = str(ticket.get("date", ""))[:10]  # Только дата
        raw_content = ticket.get("content", "")
        
        # Очищаем контент от HTML
        clean_content = glpi.clean_html_to_text(raw_content)[:100]
        if len(raw_content) > 100:
            clean_content += "..."
        
        emoji, status_name = status_info.get(status, ("⚪", f"Статус {status}"))
        
        # Location name (already resolved in get_active_tickets)
        location_name = ticket.get("location_name", "Не указано")
        safe_location = html.escape(str(location_name))
        
        lines.append(f"🎫 <b>#{tid}</b> — {title}")
        lines.append(f"   🏢 {safe_location}")
        lines.append(f"   📅 {date_str} | {emoji} {status_name}")
        if clean_content:
            lines.append(f"   📝 <i>{clean_content}</i>")
        lines.append("")
    
    if len(tickets) > 10:
        lines.append(f"<i>...и ещё {len(tickets) - 10} заявок</i>")
    
    # Кнопки
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="my_tickets")],
        [InlineKeyboardButton(text="🔗 Открыть GLPI", url=f"{Config.GLPI_URL}/front/ticket.php")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]
    ])
    
    msg = chr(10).join(lines)
    await call.message.answer(msg, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "create_ticket")
async def start_create_ticket(call: CallbackQuery, state: FSMContext):
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Задача", callback_data="ticket_type_2"),
            InlineKeyboardButton(text="🔥 Инцидент", callback_data="ticket_type_1")
        ]
    ])
    await call.message.answer("📝 Выберите тип заявки:", reply_markup=kb)
    await state.set_state(Form.waiting_for_ticket_type)

@router.callback_query(Form.waiting_for_ticket_type, F.data.startswith("ticket_type_"))
async def process_ticket_type(call: CallbackQuery, state: FSMContext):
    await call.answer()
    ticket_type = int(call.data.split("_")[-1])  # 1=Инцидент, 2=Задача
    type_name = "Инцидент" if ticket_type == 1 else "Задача"
    await state.update_data(ticket_type=ticket_type)
    await call.message.edit_text(f"✅ Тип: {type_name}\n\n📝 Напишите краткую суть заявки (заголовок):")
    await state.set_state(Form.waiting_for_ticket_title)

@router.message(Form.waiting_for_ticket_title)
async def process_ticket_title(message: Message, state: FSMContext):
    await state.update_data(ticket_title=message.text)
    await message.answer("📄 Теперь опишите заявку подробнее (содержание):")
    await state.set_state(Form.waiting_for_ticket_desc)

@router.message(Form.waiting_for_ticket_desc)
async def process_ticket_desc(message: Message, state: FSMContext):
    data = await state.get_data()
    title = data.get("ticket_title", "")
    content = message.text
    ticket_type = data.get("ticket_type", 2)
    type_name = "Инцидент" if ticket_type == 1 else "Запрос"
    
    ticket_id = await glpi.create_ticket(title, content, ticket_type=ticket_type)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]
    ])
    
    if ticket_id:
        await message.answer(
            f"✅ Заявка #{ticket_id} создана!\n"
            f"📋 Тип: {type_name}\n"
            f"👀 Наблюдатель: Administrators\n\n"
            f"🔗 {Config.GLPI_URL}/front/ticket.form.php?id={ticket_id}",
            reply_markup=kb
        )
    else:
        await message.answer("❌ Ошибка при создании заявки.", reply_markup=kb)
    await state.clear()

# --- VALIDATION LOGIC ---

@router.callback_query(F.data.startswith("approve_"))
async def approve_handler(call: CallbackQuery):
    # Проверяем, не содержит ли callback_data "None"
    if "None" in call.data:
        await call.answer("❌ Ошибка: неверный ID валидации", show_alert=True)
        return
    
    try:
        parts = call.data.split("_")
        val_id = int(parts[1])
        ticket_id = int(parts[2]) if len(parts) > 2 else None
    except (ValueError, IndexError):
        await call.answer("❌ Ошибка: неверный формат данных", show_alert=True)
        return
    
    if await glpi.update_validation(val_id, 3, "Согласовано через Telegram"):
        await call.message.edit_reply_markup(reply_markup=None)
        # Привязываем ответ к карточке тикета
        ticket_ref = f"#{ticket_id} " if ticket_id else ""
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]
        ])
        await call.message.answer(
            f"✅ Заявка {ticket_ref}— <b>СОГЛАСОВАНО</b>",
            parse_mode="HTML",
            reply_to_message_id=call.message.message_id,
            reply_markup=kb
        )
        await call.answer("✅ Согласовано")
    else:
        await call.answer("Ошибка API", show_alert=True)

@router.callback_query(F.data.startswith("refuse_"))
async def refuse_handler(call: CallbackQuery, state: FSMContext):
    # Проверяем, не содержит ли callback_data "None"
    if "None" in call.data:
        await call.answer("❌ Ошибка: неверный ID валидации", show_alert=True)
        return
    
    try:
        parts = call.data.split("_")
        val_id = int(parts[1])
        ticket_id = int(parts[2]) if len(parts) > 2 else None
    except (ValueError, IndexError):
        await call.answer("❌ Ошибка: неверный формат данных", show_alert=True)
        return
    
    # Сохраняем val_id, ticket_id и ID исходного сообщения для reply
    await state.update_data(
        val_id=val_id,
        ticket_id=ticket_id,
        origin_message_id=call.message.message_id
    )
    await call.message.answer("📝 Укажите причину отказа:")
    await state.set_state(Form.waiting_for_refusal)
    await call.answer()

@router.message(Form.waiting_for_refusal)
async def process_refusal(message: Message, state: FSMContext):
    data = await state.get_data()
    val_id = data.get("val_id")
    ticket_id = data.get("ticket_id")
    origin_message_id = data.get("origin_message_id")
    reason = message.text
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]
    ])
    
    if await glpi.update_validation(val_id, 4, reason):
        ticket_ref = f"#{ticket_id} " if ticket_id else ""
        await message.answer(
            f"❌ Заявка {ticket_ref}— <b>ОТКЛОНЕНО</b>\n💬 Причина: {reason}",
            parse_mode="HTML",
            reply_to_message_id=origin_message_id,
            reply_markup=kb
        )
    else:
        await message.answer("⚠️ Ошибка при отклонении.", reply_markup=kb)
    await state.clear()

# --- REVIEW REQUEST LOGIC ---

REVIEW_TARGET_ID = 7  # Maimescul Andrei

@router.callback_query(F.data.startswith("review_"))
async def review_handler(call: CallbackQuery, state: FSMContext):
    """Директор хочет запросить проверку — спрашивает комментарий"""
    if "None" in call.data:
        await call.answer("❌ Ошибка: неверный ID", show_alert=True)
        return
    try:
        parts = call.data.split("_")
        val_id = int(parts[1])
        ticket_id = int(parts[2]) if len(parts) > 2 else None
    except (ValueError, IndexError):
        await call.answer("❌ Ошибка формата данных", show_alert=True)
        return

    await state.update_data(val_id=val_id, ticket_id=ticket_id)
    await call.message.answer(
        "📝 <b>Запросить проверку</b>\n\n"
        "Напишите текст для Maimescul Andrei\n"
        "(вопросы, уточнения, дополнения):",
        parse_mode="HTML"
    )
    await state.set_state(Form.waiting_for_review_comment)
    await call.answer()

@router.message(Form.waiting_for_review_comment)
async def process_review_comment(message: Message, state: FSMContext):
    """Получен комментарий — создаём followup + новое согласование для id=7"""
    data = await state.get_data()
    val_id = data.get("val_id")
    ticket_id = data.get("ticket_id")
    comment = message.text

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]
    ])

    # 1. Добавляем followup к тикету
    followup_text = f"📩 <b>Запрос проверки от директора:</b>\n\n{html.escape(comment)}"
    await glpi.add_ticket_followup(ticket_id, followup_text)

    # 2. Создаём новое согласование для Maimescul Andrei
    review_comment = f"Директор запросил проверку:\n\n{comment}"
    new_val_id = await glpi.create_validation(ticket_id, REVIEW_TARGET_ID, review_comment)

    if new_val_id:
        await message.answer(
            f"✅ <b>Запрос проверки отправлен</b>\n\n"
            f"🎫 Заявка #{ticket_id}\n"
            f"👤 Получатель: Maimescul Andrei\n"
            f"💬 Комментарий: {comment[:200]}\n\n"
            f"🔗 <a href='{Config.GLPI_URL}/front/ticket.form.php?id={ticket_id}'>Открыть в GLPI</a>",
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True
        )
        logger.info(f"Review request sent for #{ticket_id}, new validation #{new_val_id}")
    else:
        await message.answer("⚠️ Ошибка при создании запроса проверки.", reply_markup=kb)

    await state.clear()

# === BACKGROUND MONITOR ===

async def check_validations(silent=True):
    validations = await glpi.get_pending_validations()
    count = 0
    
    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.cursor()
        for val in validations:
            val_id = val.get('id')
            ticket_id = val.get('ticket_id')
            raw_comment = val.get('comment_submission', '')
            
            if val_id is None or ticket_id is None:
                logger.warning(f"⚠️ Skipping validation with missing data: {val}")
                continue
            
            # Проверка на дубликаты в памяти (глобальный set)
            if val_id in glpi.notified_validations:
                continue
            
            # Проверка на дубликаты в БД (дополнительная защита)
            cursor.execute("SELECT 1 FROM processed_validations WHERE glpi_id=?", (val_id,))
            if cursor.fetchone():
                # Добавляем в память, чтобы не проверять БД каждый раз
                glpi.notified_validations.add(val_id)
                continue
            
            # Получаем детали тикета с расширенной информацией
            ticket = await glpi.get_ticket_details(ticket_id)
            if ticket:
                title = ticket.get('name', 'Без названия')
                
                # Очищаем описание с помощью улучшенной функции
                raw_content = ticket.get('content', '')
                clean_content = glpi.clean_html_to_text(raw_content)
                
                # Получаем имя заявителя (теперь это строка из get_ticket_details)
                requester_name = ticket.get('_users_id_requester', 'Неизвестно')
                if not requester_name or requester_name == 'Неизвестно':
                    # Fallback: пробуем users_id_recipient (получатель)
                    requester_name = ticket.get('users_id_recipient', 'Неизвестно')
            else:
                title = 'Загрузка...'
                clean_content = ''
                requester_name = 'Неизвестно'
            
            # Обрезаем длинное описание (уже экранировано в clean_html_to_text)
            short_content = clean_content[:300] + "..." if len(clean_content) > 300 else clean_content
            
            # Экранируем только title и requester (content уже экранирован)
            safe_title = html.escape(title)
            safe_requester = html.escape(requester_name)
            safe_content = short_content  # Уже экранирован в clean_html_to_text
            
            # Комментарий запроса (comment_submission из TicketValidation)
            comment_line = ""
            if raw_comment:
                clean_comment = glpi.clean_html_to_text(raw_comment)
                if len(clean_comment) > 200:
                    clean_comment = clean_comment[:200] + "..."
                comment_line = f"\n💬 <b>Комментарий:</b>\n<i>{clean_comment}</i>\n"
            
            msg = (
                f"📑 <b>ТРЕБУЕТСЯ СОГЛАСОВАНИЕ</b>\n\n"
                f"🎫 <b>Заявка #{ticket_id}</b>\n"
                f"👤 <b>Кто:</b> {safe_requester}\n"
                f"📝 <b>Тема:</b> {safe_title}\n"
                f"📄 <b>Описание:</b>\n<i>{safe_content}</i>"
                f"{comment_line}\n"
                f"🔗 <a href='{Config.GLPI_URL}/front/ticket.form.php?id={ticket_id}'>Открыть в GLPI</a>"
            )
            
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Согласовать", callback_data=f"approve_{val_id}_{ticket_id}"),
                    InlineKeyboardButton(text="❌ Отказать", callback_data=f"refuse_{val_id}_{ticket_id}")
                ],
                [
                    InlineKeyboardButton(text="📩 Запросить проверку", callback_data=f"review_{val_id}_{ticket_id}")
                ]
            ])

            # Отправка уведомления директору
            try:
                await bot.send_message(Config.ADMIN_ID, msg, parse_mode="HTML", reply_markup=kb)
                logger.info(f"✅ Уведомление о согласовании отправлено директору (ID: {Config.ADMIN_ID})")
                await asyncio.sleep(0.5)  # Telegram flood control
            except Exception as e:
                logger.error(f"❌ Не удалось отправить уведомление директору: {e}")

            # Запоминаем в памяти и БД
            glpi.notified_validations.add(val_id)
            glpi.notified_ticket_ids.add(ticket_id)  # Для дедупликации с monitor
            cursor.execute("INSERT INTO processed_validations (glpi_id) VALUES (?)", (val_id,))
            conn.commit()
            count += 1
            
    return count

async def check_tickets():
    """Проверка изменений в активных тикетах"""
    try:
        tickets = await glpi.get_all_active_tickets()
        if not tickets:
            return 0
        
        new_count = 0
        with sqlite3.connect(DATABASE_PATH) as conn:
            cursor = conn.cursor()
            
            for ticket in tickets:
                glpi_id = ticket.get('id')
                api_status = ticket.get('status')
                title = ticket.get('title', 'Без названия')
                location_name = ticket.get('location_name') or 'Не указано'
                requester_name = ticket.get('requester_name') or 'Неизвестно'
                technician_name = ticket.get('technician_name') or ''
                raw_content = ticket.get('content', '')
                priority = ticket.get('priority', 3)
                date_creation = ticket.get('date_creation') or ticket.get('date', '')
                users_id_lastupdater = ticket.get('users_id_lastupdater', 0)

                # Очищаем контент (500 символов для полного отображения описания)
                _full_content = glpi.clean_html_to_text(raw_content)
                clean_content = _full_content[:500]
                if len(_full_content) > 500:
                    clean_content += '...'
                
                if not glpi_id or not api_status:
                    continue
                
                # Проверяем, есть ли тикет в БД
                cursor.execute(
                    "SELECT status FROM tickets WHERE glpi_id = ?",
                    (glpi_id,)
                )
                row = cursor.fetchone()
                
                # Emoji для статусов
                status_emoji = {
                    1: "🟢",  # New
                    2: "🟡",  # Processing
                    3: "🔵",  # Planned
                    4: "🟣",  # Pending
                    5: "✅",  # Solved
                }
                emoji = status_emoji.get(api_status, "⚪")
                
                # Экранируем данные
                safe_title = html.escape(str(title))
                safe_location = html.escape(str(location_name))
                safe_requester = html.escape(str(requester_name))
                safe_technician = html.escape(str(technician_name)) if technician_name else ""

                priority_names = {
                    1: "Очень низкий", 2: "Низкий", 3: "Средний",
                    4: "Высокий", 5: "Очень высокий", 6: "Критический"
                }

                if row is None:
                    # Проверяем: не был ли этот тикет уже уведомлён через согласование
                    if glpi_id in glpi.notified_ticket_ids:
                        # Тикет уже получил уведомление "ТРЕБУЕТСЯ СОГЛАСОВАНИЕ"
                        # Записываем в БД тихо, без повторного уведомления
                        cursor.execute(
                            "INSERT INTO tickets (glpi_id, status, title) VALUES (?, ?, ?)",
                            (glpi_id, api_status, title)
                        )
                        conn.commit()
                        continue
                    
                    # Новый тикет (без согласования) — отправляем уведомление
                    priority_name = priority_names.get(priority, f"Уровень {priority}")
                    date_str = str(date_creation)[:16]

                    assignee_line = f"\n👷 <b>Кому:</b> {safe_technician}" if safe_technician else ""
                    desc_block = f"\n📝 <b>Описание:</b>\n<i>{clean_content}</i>" if clean_content else ""

                    msg = (
                        f"🆕 <b>НОВАЯ ЗАЯВКА #{glpi_id}</b>\n\n"
                        f"📋 {safe_title}\n\n"
                        f"👤 <b>От кого:</b> {safe_requester}{assignee_line}"
                        f"{desc_block}\n\n"
                        f"📍 <b>Местоположение:</b> {safe_location}\n\n"
                        f"📅 <b>Создано:</b> {date_str}\n"
                        f"⚡ <b>Приоритет:</b> {priority_name}\n"
                        f"📊 <b>Статус:</b> {get_status_name(api_status)}"
                    )

                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text="🔗 Открыть в GLPI",
                            url=f"{Config.GLPI_URL}/front/ticket.form.php?id={glpi_id}"
                        )]
                    ])

                    # Отправка уведомления о новом тикете директору
                    try:
                        await bot.send_message(Config.ADMIN_ID, msg, parse_mode="HTML", reply_markup=kb)
                        logger.info(f"✅ Уведомление о новом тикете #{glpi_id} отправлено директору")
                        await asyncio.sleep(0.5)  # Telegram flood control
                    except Exception as e:
                        logger.error(f"❌ Не удалось отправить уведомление о тикете #{glpi_id}: {e}")

                    # Сохраняем в БД
                    cursor.execute(
                        "INSERT INTO tickets (glpi_id, status, title) VALUES (?, ?, ?)",
                        (glpi_id, api_status, title)
                    )
                    conn.commit()
                    new_count += 1
                    
                else:
                    db_status = row[0]
                    if db_status != api_status:
                        # Изменение статуса — полный контекст
                        old_name = get_status_name(db_status)
                        new_name = get_status_name(api_status)

                        # Получаем полные данные тикета
                        full_ticket = await glpi.get_ticket_details(glpi_id)
                        if not full_ticket:
                            full_ticket = ticket

                        # Кто изменил
                        updater_id = full_ticket.get('users_id_lastupdater', 0)
                        last_updater_name = await glpi._get_user_name(updater_id) if updater_id else "Неизвестно"
                        safe_updater = html.escape(str(last_updater_name))

                        # Emoji для нового статуса
                        status_emoji_map = {1: "🆕", 2: "🔧", 3: "📅", 4: "⏸️", 5: "✅", 6: "🔒"}
                        status_hdr_emoji = status_emoji_map.get(api_status, "🔄")

                        # Назначение (всегда)
                        assignee = await glpi.get_ticket_technician(glpi_id)
                        assignee_line = f"\n🔧 <b>Назначена:</b> {html.escape(assignee)}" if assignee else ""

                        # Pending согласования
                        validation_line = ""
                        try:
                            validations = await glpi.get_ticket_validations(glpi_id)
                            if validations:
                                pending = [v for v in validations if int(v.get('status', 0)) in (1, 2)]
                                if pending:
                                    uid_set = list({int(v['users_id_validate']) for v in pending if v.get('users_id_validate')})
                                    names = []
                                    for uid in uid_set[:2]:
                                        n = await glpi._get_user_name(uid)
                                        if n:
                                            names.append(n)
                                    if names:
                                        escaped = [html.escape(n) for n in names]
                                        validation_line = f"\n⏳ <b>На согласовании у:</b> {', '.join(escaped)}"
                        except Exception:
                            pass

                        # Решение / комментарий к смене статуса
                        solution_block = ""

                        # Ищем followup/решение, добавленные вместе со сменой статуса (±2 мин от date_mod)
                        # Окно нужно, т.к. между опросами могло произойти НЕСКОЛЬКО смен статуса подряд
                        # (например 2→5 с решением, затем сразу 5→3 при отклонении решения) — бот видит
                        # только итоговый переход, поэтому решение может лежать в ITILSolution, а не в
                        # ITILFollowup, даже если итоговый статус не 5/6.
                        date_mod = full_ticket.get('date_mod', '')
                        mod_time = None
                        window = timedelta(minutes=2)
                        if date_mod:
                            try:
                                mod_time = datetime.strptime(date_mod, "%Y-%m-%d %H:%M:%S")
                            except ValueError:
                                mod_time = None

                        all_followups = await glpi.get_ticket_followups(glpi_id)
                        recent_followups = []
                        if mod_time:
                            for fu in all_followups:
                                fu_date = fu.get('date_creation', '')
                                if fu_date:
                                    try:
                                        fu_time = datetime.strptime(fu_date, "%Y-%m-%d %H:%M:%S")
                                        if abs((fu_time - mod_time).total_seconds()) <= window.total_seconds():
                                            recent_followups.append(fu)
                                    except ValueError:
                                        pass

                        all_solutions = await glpi.get_ticket_solutions(glpi_id)
                        recent_solutions = []
                        if mod_time:
                            for sol in all_solutions:
                                sol_date = sol.get('date_creation', '')
                                if sol_date:
                                    try:
                                        sol_time = datetime.strptime(sol_date, "%Y-%m-%d %H:%M:%S")
                                        if abs((sol_time - mod_time).total_seconds()) <= window.total_seconds():
                                            recent_solutions.append(sol)
                                    except ValueError:
                                        pass

                        if api_status in [5, 6]:
                            sol_data = await glpi._get_ticket_solution(glpi_id)
                            if sol_data and sol_data["content"]:
                                sol_user = html.escape(sol_data["user_name"])
                                solution_block = f"\n\n💡 <b>Решение ({sol_user}):</b>\n<i>{sol_data['content'][:500]}</i>"
                            elif recent_followups:
                                # Показываем ВСЕ комментарии, добавленные вместе со сменой статуса
                                fu_lines = []
                                for fu in recent_followups:
                                    if fu.get('content'):
                                        fu_user = html.escape(fu['user_name'])
                                        fu_lines.append(f"• <i>{fu['content'][:500]}</i> ({fu_user})")
                                if fu_lines:
                                    label = "Комментарий" if len(fu_lines) == 1 else "Комментарии"
                                    solution_block = f"\n\n💬 <b>{label}:</b>\n" + "\n".join(fu_lines)
                            else:
                                fu_data = await glpi._get_ticket_followup(glpi_id)
                                if fu_data and fu_data["content"]:
                                    fu_user = html.escape(fu_data["user_name"])
                                    solution_block = f"\n\n💬 <b>Комментарий ({fu_user}):</b>\n<i>{fu_data['content'][:500]}</i>"
                        else:
                            if recent_solutions:
                                last_sol = max(recent_solutions, key=lambda x: x.get('date_creation', ''))
                                if last_sol.get('content'):
                                    sol_user = html.escape(last_sol['user_name'])
                                    solution_block = f"\n\n💡 <b>Решение ({sol_user}):</b>\n<i>{last_sol['content'][:500]}</i>"
                            elif recent_followups:
                                # Показываем ВСЕ комментарии, добавленные вместе со сменой статуса
                                fu_lines = []
                                for fu in recent_followups:
                                    if fu.get('content'):
                                        fu_user = html.escape(fu['user_name'])
                                        fu_lines.append(f"• <i>{fu['content'][:500]}</i> ({fu_user})")
                                if fu_lines:
                                    label = "Комментарий" if len(fu_lines) == 1 else "Комментарии"
                                    solution_block = f"\n\n💬 <b>{label}:</b>\n" + "\n".join(fu_lines)
                            else:
                                fu_data = await glpi._get_ticket_followup(glpi_id)
                                if fu_data and fu_data["content"]:
                                    fu_user = html.escape(fu_data["user_name"])
                                    solution_block = f"\n\n💬 <b>Комментарий ({fu_user}):</b>\n<i>{fu_data['content'][:500]}</i>"

                        # Задачи (ITILTask)
                        # GLPI Planning class constants (inc/planning.class.php): только 3 значения, не 5!
                        TASK_STATUS = {0: 'ℹ️', 1: '⬜', 2: '✅'}
                        tasks_block = ""
                        try:
                            tasks = await glpi.get_ticket_tasks(glpi_id)
                            if tasks:
                                task_lines = []
                                for t in tasks:
                                    t_status = int(t.get('state', 0))
                                    t_emoji = TASK_STATUS.get(t_status, '❓')
                                    t_text = glpi.clean_html_to_text(t.get('content', ''))
                                    if len(t_text) > 100:
                                        t_text = t_text[:100] + '...'
                                    t_tech = ""
                                    t_tech_id = t.get('users_id_tech')
                                    if t_tech_id:
                                        tech_name = await glpi._get_user_name(int(t_tech_id))
                                        if tech_name:
                                            t_tech = f" → {html.escape(tech_name)}"
                                    t_time = ""
                                    actiontime = t.get('actiontime', 0)
                                    if actiontime and int(actiontime) > 0:
                                        total_sec = int(actiontime)
                                        hours, remainder = divmod(total_sec, 3600)
                                        minutes, _ = divmod(remainder, 60)
                                        t_time = f" ⏱ {hours}ч {minutes}м"
                                    t_dates = ""
                                    ds = t.get('date_start', '')
                                    de = t.get('date_end', '')
                                    if ds and de:
                                        t_dates = f" ({ds[-8:-3]} → {de[-8:-3]})"
                                    elif ds:
                                        t_dates = f" (с {ds[-8:-3]})"
                                    task_lines.append(f"  {t_emoji} {html.escape(t_text)}{t_tech}{t_time}{t_dates}")
                                if task_lines:
                                    tasks_block = "\n\n📂 <b>Задачи:</b>\n" + "\n".join(task_lines)
                        except Exception:
                            pass

                        # "Других изменений нет"
                        changes_line = ""
                        if not solution_block and not assignee_line and not validation_line and not tasks_block:
                            changes_line = "\n\n🔖 Других изменений в заявке не производилось"

                        msg = (
                            f"{status_hdr_emoji} <b>Статус заявки #{glpi_id} изменён</b>\n\n"
                            f"📋 {safe_title}\n\n"
                            f"👤 <b>От кого:</b> {safe_requester}\n"
                            f"📝 <b>Описание:</b>\n<i>{clean_content}</i>\n\n"
                            f"📍 <b>Местоположение:</b> {safe_location}\n\n"
                            f"📅 <b>Создано:</b> {date_creation[:16] if date_creation else 'N/A'}\n"
                            f"⚡ <b>Приоритет:</b> {priority_names.get(priority, 'Неизвестно')}\n"
                            f"📊 <b>Статус:</b> {old_name} → {new_name}"
                            f"\n👤 <b>Кто изменил:</b> {safe_updater}"
                            f"{assignee_line}"
                            f"{validation_line}"
                            f"{solution_block}"
                            f"{tasks_block}"
                            f"{changes_line}\n\n"
                            f"🔗 <a href='{Config.GLPI_URL}/front/ticket.form.php?id={glpi_id}'>Открыть в GLPI</a>"
                        )

                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔗 Открыть в GLPI",
                                url=f"{Config.GLPI_URL}/front/ticket.form.php?id={glpi_id}"
                            )]
                        ])

                        # Отправка уведомления об изменении статуса директору
                        try:
                            await bot.send_message(Config.ADMIN_ID, msg, parse_mode="HTML", reply_markup=kb)
                            logger.info(f"✅ Уведомление об изменении статуса тикета #{glpi_id} отправлено директору")
                            await asyncio.sleep(0.5)  # Telegram flood control
                        except Exception as e:
                            logger.error(f"❌ Не удалось отправить уведомление об изменении тикета #{glpi_id}: {e}")

                        # Обновляем статус в БД
                        cursor.execute(
                            "UPDATE tickets SET status = ?, title = ?, last_update = CURRENT_TIMESTAMP WHERE glpi_id = ?",
                            (api_status, title, glpi_id)
                        )
                        conn.commit()

        return new_count

    except Exception as e:
        logger.error(f"Error in check_tickets: {e}")
        return 0

def get_status_name(status_code):
    """Получить человекочитаемое название статуса"""
    status_names = {
        1: "Новый",
        2: "В работе (назначена)",
        3: "В работе (запланирована)",
        4: "Ожидание",
        5: "Решена",
        6: "Закрыта"
    }
    return status_names.get(status_code, f"Статус {status_code}")

async def monitor_loop():
    """Фоновый мониторинг с supervisor pattern и exponential backoff"""
    attempt = 0
    while True:
        try:
            await check_validations()
            new_tickets = await check_tickets()
            interval = 60 if new_tickets > 0 else Config.CHECK_INTERVAL
            attempt = 0  # Сброс при успешном цикле
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("[supervisor] monitor_loop cancelled")
            break
        except Exception as e:
            attempt += 1
            backoff = min(30 * attempt, 300)  # 30s, 60s, 90s... max 300s
            logger.error(f"[supervisor] monitor_loop error (attempt {attempt}): {e}", exc_info=True)
            await asyncio.sleep(backoff)

_supervised_tasks = []

async def main():
    init_db()
    await glpi.init_session()
    
    logger.info("🔧 Running SearchOptions diagnostic...")
    await glpi.diagnose_search_options()
    
    dp.include_router(router)
    
    # Запуск фонового мониторинга с отслеживанием
    monitor_task = asyncio.create_task(monitor_loop())
    _supervised_tasks.append(monitor_task)
    
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="approvals", description="⏳ Статус всех согласований"),
        BotCommand(command="my_tickets", description="📂 Мои активные заявки"),
        BotCommand(command="help", description="ℹ️ Помощь"),
    ])
    logger.info("✅ Bot commands set")

    await bot.delete_webhook(drop_pending_updates=True)
    
    try:
        await dp.start_polling(bot)
    finally:
        # Graceful shutdown
        logger.info("🛑 Shutting down...")
        for task in _supervised_tasks:
            task.cancel()
        await asyncio.gather(*_supervised_tasks, return_exceptions=True)
        logger.info("✅ Shutdown complete")

if __name__ == "__main__":
    asyncio.run(main())
