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
from datetime import datetime
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
                                    'ticket_id': item['tickets_id']
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
        
        # Resolve IDs to names for each ticket
        # Search API does not return location (Field 83 is None), so we fetch from direct API
        for ticket in result:
            tid = ticket.get("id")
            
            # Fetch locations_id from direct Ticket API (Search API returns None for location)
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
                        else:
                            ticket["location_name"] = "Не указано"
            except Exception as e:
                logger.warning(f"Failed to fetch location for ticket {tid}: {e}")
                ticket["location_name"] = "Не указано"
            
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
        
        logger.info(f"Total unique active tickets: {len(result)}")
        return result

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
            InlineKeyboardButton(text="📋 Запрос", callback_data="ticket_type_2"),
            InlineKeyboardButton(text="🔥 Инцидент", callback_data="ticket_type_1")
        ]
    ])
    await call.message.answer("📝 Выберите тип заявки:", reply_markup=kb)
    await state.set_state(Form.waiting_for_ticket_type)

@router.callback_query(Form.waiting_for_ticket_type, F.data.startswith("ticket_type_"))
async def process_ticket_type(call: CallbackQuery, state: FSMContext):
    await call.answer()
    ticket_type = int(call.data.split("_")[-1])  # 1=Инцидент, 2=Запрос
    type_name = "Инцидент" if ticket_type == 1 else "Запрос"
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

# === BACKGROUND MONITOR ===

async def check_validations(silent=True):
    validations = await glpi.get_pending_validations()
    count = 0
    
    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.cursor()
        for val in validations:
            # ИСПРАВЛЕНО v2.2: используем новую структуру {'id': int, 'ticket_id': int}
            val_id = val.get('id')  # Реальный ID валидации из ключа словаря
            ticket_id = val.get('ticket_id')  # ID тикета из Field 4
            
            # Проверка на None значения
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
            
            msg = (
                f"📑 <b>ТРЕБУЕТСЯ СОГЛАСОВАНИЕ</b>\n\n"
                f"🎫 <b>Заявка #{ticket_id}</b>\n"
                f"👤 <b>Кто:</b> {safe_requester}\n"
                f"📝 <b>Тема:</b> {safe_title}\n"
                f"📄 <b>Описание:</b>\n<i>{safe_content}</i>\n\n"
                f"🔗 <a href='{Config.GLPI_URL}/front/ticket.form.php?id={ticket_id}'>Открыть в GLPI</a>"
            )
            
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Согласовать", callback_data=f"approve_{val_id}_{ticket_id}"),
                    InlineKeyboardButton(text="❌ Отказать", callback_data=f"refuse_{val_id}_{ticket_id}")
                ]
            ])
            
            await bot.send_message(Config.ADMIN_ID, msg, parse_mode="HTML", reply_markup=kb)
            
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
        tickets = await glpi.get_active_tickets()
        if not tickets:
            return 0
        
        count = 0
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
                
                # Очищаем контент
                clean_content = glpi.clean_html_to_text(raw_content)[:150]
                if len(raw_content) > 150:
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
                    assignee_line = f"\n👷 <b>Кому:</b> {safe_technician}" if safe_technician else ""
                    content_line = f"\n📄 <i>{clean_content}</i>" if clean_content else ""

                    msg = (
                        f"{emoji} 🆕 <b>Новый тикет #{glpi_id}</b>\n"
                        f"🏢 <b>Филиал:</b> {safe_location}\n"
                        f"👤 <b>От кого:</b> {safe_requester}{assignee_line}\n"
                        f"📝 <b>Тема:</b> {safe_title}{content_line}"
                    )
                    
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text="🔗 Открыть в GLPI",
                            url=f"{Config.GLPI_URL}/front/ticket.form.php?id={glpi_id}"
                        )]
                    ])
                    
                    await bot.send_message(Config.ADMIN_ID, msg, parse_mode="HTML", reply_markup=kb)
                    
                    # Сохраняем в БД
                    cursor.execute(
                        "INSERT INTO tickets (glpi_id, status, title) VALUES (?, ?, ?)",
                        (glpi_id, api_status, title)
                    )
                    conn.commit()
                    count += 1
                    
                else:
                    db_status = row[0]
                    if db_status != api_status:
                        # Изменение статуса
                        old_name = get_status_name(db_status)
                        new_name = get_status_name(api_status)
                        
                        msg = (
                            f"{emoji} 🔄 <b>Статус изменен: #{glpi_id}</b>\n"
                            f"🏢 <b>Филиал:</b> {safe_location}\n"
                            f"👤 <b>От кого:</b> {safe_requester}\n"
                            f"📝 Тема: {title}\n"
                            f"📊 {old_name} → {new_name}\n"
                            f"⏰ {datetime.now().strftime('%H:%M')}"
                        )
                        
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔗 Открыть в GLPI",
                                url=f"{Config.GLPI_URL}/front/ticket.form.php?id={glpi_id}"
                            )]
                        ])
                        
                        await bot.send_message(Config.ADMIN_ID, msg, parse_mode="HTML", reply_markup=kb)
                        
                        # Обновляем статус в БД
                        cursor.execute(
                            "UPDATE tickets SET status = ?, title = ?, last_update = CURRENT_TIMESTAMP WHERE glpi_id = ?",
                            (api_status, title, glpi_id)
                        )
                        conn.commit()
                        count += 1
        
        return count
        
    except Exception as e:
        logger.error(f"Error in check_tickets: {e}")
        return 0

def get_status_name(status_code):
    """Получить человекочитаемое название статуса"""
    status_names = {
        1: "Новый",
        2: "В обработке",
        3: "Ожидает",
        4: "Ожидание",
        5: "Решен",
        6: "Закрыт"
    }
    return status_names.get(status_code, f"Статус {status_code}")

async def monitor_loop():
    while True:
        await check_validations()
        await check_tickets()
        await asyncio.sleep(Config.CHECK_INTERVAL)

async def main():
    init_db()
    await glpi.init_session()
    
    # Диагностика SearchOptions (проверка корректности полей)
    logger.info("🔧 Running SearchOptions diagnostic...")
    await glpi.diagnose_search_options()
    
    dp.include_router(router)
    
    # Запуск фонового мониторинга
    asyncio.create_task(monitor_loop())
    
    # Установка команд меню
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="approvals", description="⏳ Статус всех согласований"),
        BotCommand(command="my_tickets", description="📂 Мои активные заявки"),
        BotCommand(command="help", description="ℹ️ Помощь"),
    ])
    logger.info("✅ Bot commands set")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
