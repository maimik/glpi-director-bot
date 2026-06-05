# БАЗА ЗНАНИЙ: ИНФРАСТРУКТУРА PoVladar SRL

*Последнее обновление: 15.02.2026 — Phase 2-8: Complete Network Infrastructure Audit*

---

## 1. ПЕРСОНАЛ И ДОСТУП

**Главный системный администратор:** Андрей Андреевич (обращение: "Вы" / "Шеф")

**Зона ответственности:**
- Полный доступ ко всем подсетям и серверам инфраструктуры.
- Контактное лицо для критических инцидентов и эскалации.
- Уровень доступа: `root`/`administrator` на всех узлах.

**Инструкция для AI:**
Ты — системный помощник администратора. Твоя задача — управление серверами, мониторинг, выполнение команд через AI Console и предоставление технической информации.

---

## 2. ТОПОЛОГИЯ СЕТИ (Mikrotik)

| Подсеть | VLAN/Интерфейс | Назначение |
|---------|----------------|------------|
| **192.168.0.0/24** | 01_LAN OFFICE | **Серверный сегмент (Core Infrastructure)** |
| 192.168.10.0/24 | 02_VLAN-10 | Филиал "Траян-11" |
| 192.168.3.0/24 | 05_FRA | Филиал "Франко-38" |
| 192.168.20.0/24 | 03_VLAN-20 | Видеонаблюдение (CCTV) |
| 192.168.2.0/24 | 04_DUDEP | Производство |
| 10.10.10.0/24 | 00_WireGuard | VPN Туннель (удаленный доступ) |

### Удалённые филиалы (External Sites)

| Филиал | Внешний IP | Внутренний IP | Оборудование | Сервисы |
|--------|------------|---------------|--------------|---------|
| **OTVSK** (Chisinau, str. Otovaska, 16) | 89.149.115.99 | TBD | MikroTik | CCTV/DVR (8016, 1026, 8161), POS (Traian-11 fix) |

---

## 2.1. SFA-31: SMART TRAFFIC MANAGEMENT

*Обновлено: 14.02.2026 — Внедрена система "Умного управления трафиком"*

**Роутер:** MikroTik CCR2004 (SFA-31, Центральный офис)
**WAN IP:** 77.89.251.234
**LAN Subnet:** 192.168.0.0/24 (Admin Network)
**Статус:** ✅ **FULLY OPTIMIZED** (Smart QoS + Trusted Zones)

---

### 🛡️ Firewall Policy: Trusted Zones (Address Lists)

**Концепция:** Hybrid Firewall — Whitelist для админов, строгие правила для офисных сетей.

#### Address Lists (Списки доверенных зон)

| Список | Адреса | Назначение |
|--------|--------|------------|
| **ADMIN_NET** | `192.168.0.0/24` | Админская сеть (Директор, IT-отдел) — **Full Access** |
| **BACKUP_SERVERS** | `192.168.10.10` (NAS Траян), `192.168.3.7` (Franco-SW) | Серверы для ночных бэкапов |
| **LOCAL** | Динамический (DHCP) | Внутрисетевые устройства |
| **VIDEO** | `192.168.20.30`, `192.168.20.99`, `192.168.20.29` | Видеонаблюдение |

#### Правила Firewall Filter

```mikrotik
# 1. Полный доступ для админской сети (включая UDP/QUIC для YouTube/StarTV)
/ip firewall filter
add action=accept chain=forward comment="ALLOW: Admin Net Full Access (QUIC/StarTV/Kinogo)" \
    src-address-list=ADMIN_NET out-interface-list=WAN

# 2. Двунаправленный трафик для бэкапов (00:00-05:00)
add action=accept chain=forward comment="ALLOW: Backup Traffic (Admin ↔ Backup Servers)" \
    src-address-list=ADMIN_NET dst-address-list=BACKUP_SERVERS

add action=accept chain=forward comment="ALLOW: Backup Traffic (Backup Servers → Admin)" \
    src-address-list=BACKUP_SERVERS dst-address-list=ADMIN_NET
```

**Что разрешено для ADMIN_NET (192.168.0.0/24):**
- ✅ **Full Access** в Интернет (любые протоколы: TCP, UDP, ICMP)
- ✅ **QUIC (UDP 443)** — ускорение YouTube, Kinogo, онлайн-кинотеатров
- ✅ **StarTV IPTV** — потоковое видео через UDP
- ✅ **Бэкапы** на NAS и Franco-SW без ограничений

---

### ⏱️ Dynamic QoS: Day/Night Schedule

**Система:** Автоматическое переключение ширины канала по расписанию.

#### Режимы работы

| Режим | Время | Max Limit | Приоритеты | Назначение |
|-------|-------|-----------|------------|------------|
| 🌞 **DAY MODE** | 08:00 - 18:00 | **900 Mbps** | Строгие (1C > VPN > Video) | Бизнес-приложения |
| 🌙 **NIGHT MODE** | 18:00 - 08:00 | **UNLIMITED** (0) | Сохраняются | Бэкапы + домашнее ТВ |

#### Приоритеты Queue Tree (всегда активны)

```
TOTAL_PRIORITY (корневая очередь, max-limit изменяется по расписанию)
├── 1_WireGuard (priority=1, limit-at=100M) — VPN туннели
├── 2_1C_Traffic (priority=2, limit-at=50M)  — 1С Бухгалтерия
├── 3_VIDEO_System (priority=3, limit-at=200M) — Видеонаблюдение
├── 4_ADMIN_LOCAL (priority=4, limit-at=100M) — Админская сеть
└── 5_OTHER_TRAFFIC (priority=8, no-mark)     — Остальной трафик
```

**Логика:**
- **DAY MODE (08:00):** `max-limit=900M` — строгое соблюдение приоритетов, гарантия работы 1C и VPN.
- **NIGHT MODE (18:00):** `max-limit=0` — снятие искусственных ограничений, трафик летит на скорости порта. Приоритеты остаются активными (если 1C запустится ночью, она всё равно получит приоритет).

#### Скрипты управления

```mikrotik
# Автоматические скрипты (выполняются по расписанию)
/system script
add name="QoS_DAY_MODE" source={ /queue tree set [find name="TOTAL_PRIORITY"] max-limit=900M }
add name="QoS_NIGHT_MODE" source={ /queue tree set [find name="TOTAL_PRIORITY"] max-limit=0 }

# Расписание
/system scheduler
add name="Start_Day_QoS" start-time=08:00:00 interval=1d on-event="QoS_DAY_MODE"
add name="Start_Night_QoS" start-time=18:00:00 interval=1d on-event="QoS_NIGHT_MODE"

# Ручное управление (для экстренных случаев)
add name="MANUAL_DAY" source={ /queue tree set [find name="TOTAL_PRIORITY"] max-limit=900M }
add name="MANUAL_NIGHT" source={ /queue tree set [find name="TOTAL_PRIORITY"] max-limit=0 }
```

#### Мониторинг QoS

```mikrotik
# Проверить текущий режим
/queue tree print where name="TOTAL_PRIORITY"

# Посмотреть лог переключений
/log print where message~"QoS"

# Ручное переключение (экстренное)
/system script run MANUAL_DAY    # Включить ограничения
/system script run MANUAL_NIGHT  # Снять ограничения
```

---

### 📦 Окна бэкапов (Backup Windows)

**Расписание автоматических бэкапов:**
- **00:00 - 05:00** — Синхронизация серверов на NAS (192.168.10.10) и Franco-SW (192.168.3.7)
- **Режим:** NIGHT MODE (unlimited скорость)
- **Приоритет:** Низкий (priority=8), но при отсутствии другого трафика — максимальная скорость

**Серверы, участвующие в бэкапах:**
- `sfa-mng` (192.168.0.35) → NAS
- `zbxglpi-pvl` (192.168.0.33) → NAS
- `pvl-cloud` (192.168.0.25) → NAS
- Все серверы → Franco-SW (через VPN)

---

### 🎯 Решаемые проблемы

| Проблема | Решение | Статус |
|----------|---------|--------|
| Директор не может смотреть StarTV дома | ADMIN_NET получила Full Access (UDP/QUIC) | ✅ Решено |
| Онлайн-кинотеатры (Kinogo) не работают | Разрешен QUIC (UDP 443) для 192.168.0.0/24 | ✅ Решено |
| Бэкапы идут медленно ночью | NIGHT MODE снимает лимиты с 18:00 до 08:00 | ✅ Решено |
| 1C тормозит днем из-за YouTube | DAY MODE с приоритетами (1C > Other) | ✅ Решено |

---

### 🔧 Команды для управления

```bash
# SSH на роутер (НОВЫЙ ПОРТ: 22222, обновлено 14.02.2026)
ssh -p 22222 admin@192.168.0.1

# Проверить Address Lists
/ip firewall address-list print where list~"ADMIN_NET|BACKUP"

# Проверить порядок правил firewall
/ip firewall filter print where chain=forward

# Проверить текущий QoS режим
/queue tree print where name="TOTAL_PRIORITY"

# Посмотреть логи
/log print where message~"QoS|ALLOW"

# Добавить новый VIP-адрес в админскую сеть
/ip firewall address-list add list=ADMIN_NET address=192.168.0.55 comment="Родители директора"
```

---

### ⚡ Оптимизация производительности (14.02.2026)

**Применённые изменения для максимальной производительности:**

1. **Bridge FastPath: ВКЛЮЧЕН (Экспериментально)**
   ```mikrotik
   /interface bridge settings
   set allow-fast-path=yes use-ip-firewall-for-vlan=yes
   ```
   - ✅ **Статус:** Тестируется в production (RouterOS 7.21.2 поддерживает FastPath + VLAN Filtering)
   - ⚠️ **Мониторинг:** Проверять QoS packet marks и Mangle counters (24-48 часов)
   - 🎯 **Ожидаемый эффект:** CPU Load -5-15%, Inter-VLAN throughput +10-20%

2. **Hardware Flow Control: ОТКЛЮЧЕН**
   ```mikrotik
   /interface ethernet switch
   set 0 cpu-flow-control=no
   ```
   - Снижение задержек при высоких нагрузках

3. **WireGuard UDP Timeout: УВЕЛИЧЕН**
   ```mikrotik
   /ip firewall connection tracking
   set udp-timeout=60s
   ```
   - **Было:** 10s (слишком агрессивно, вызывало переподключения)
   - **Стало:** 60s (стабильные WireGuard туннели)

4. **WireGuard Keepalive Оптимизация:**
   - Филиалы со статическими IP (DU-DEP, FRANKO-38, OTTO-16): `persistent-keepalive=0`
   - Мобильные клиенты за NAT: `persistent-keepalive=25s`
   - **Экономия CPU:** ~10-15% от WireGuard нагрузки

**Критерии для отката FastPath:**
- ❌ QoS packet marks перестали расти
- ❌ Mangle counters = 0
- ❌ Packet reordering (VoIP/Video "дёргается")
- ❌ CPU Load вырос вместо снижения

**Команды мониторинга:**
```mikrotik
# Проверить FastTrack counters
/ip firewall filter print stats where action=fasttrack-connection

# Проверить Mangle packet marks
/ip firewall mangle print stats where chain=forward

# Проверить Queue Tree stats
/queue tree print stats

# Проверить WireGuard RX/TX
/interface wireguard peers print stats

# Откат FastPath (если проблемы)
/interface bridge settings set allow-fast-path=no
```

---

## 2.2. SW1 (SFA-SW1): L2 DISTRIBUTION SWITCH

*Обновлено: 15.02.2026 — Phase 2: L2 Distribution Audit Complete*

**Модель:** MikroTik CRS326-24G-2S+ (24x Gigabit + 2x 10G SFP+)
**IP Management:** 192.168.0.11/24
**RouterOS Version:** 7.21.2 (stable)
**Статус:** ✅ **AUDITED & SYNCED WITH SFA-RBG-1**

---

### 🔌 Роль в сети

SW1 — центральный коммутатор L2 Distribution Layer в офисе SFA-31. Выполняет функции:
- **VLAN Routing** (Inter-VLAN через SFA-RBG-1)
- **Distribution Hub** для подключения других свитчей (SW2, SW3, etc.)
- **Access Layer** для конечных устройств в офисе

---

### 🌐 Топология подключения

```
                       SFA-RBG-1 (CCR2004)
                       192.168.0.1
                          |
                    sfp3-SW1 (10G)
                          |
                    sfp-1_TR (uplink)
                          |
                    SW1 (CRS326)
                   192.168.0.11
                /       |        \
          SW2 (0.12) SW3 (0.13) Access Ports
       (Distribution) (Distribution) (Workstations)
```

**Uplink к SFA-RBG-1:**
- Порт SW1: `sfp-1_TR` (10G SFP+)
- Порт SFA-RBG-1: `sfp3-SW1` (10G SFP+)
- VLAN: 10, 20, 30 (tagged)

**Downlink к другим свитчам:**
- Порт: `eth24_TR`, `eth23_TR`, `sfp-2_TR`
- VLAN: 10, 20, 30 (tagged)

---

### 🏷️ VLAN Configuration

| VLAN ID | Subnet | Назначение | Trunk Ports | Access Ports (Untagged) |
|---------|--------|------------|-------------|------------------------|
| **1** | — | Native VLAN | BridgeLocal | eth1-eth8 (management) |
| **10** | 192.168.10.0/24 | Філіал Траян-11 | sfp-1_TR, eth24_TR, eth23_TR, sfp-2_TR | eth16_v10, eth14_v10, eth15_v10 |
| **20** | 192.168.20.0/24 | Видеонаблюдение (CCTV) | sfp-1_TR, eth24_TR, eth23_TR, sfp-2_TR | eth17_v20, eth18_v20 |
| **30** | 192.168.30.0/24 | Резерв | sfp-1_TR, eth24_TR, eth23_TR, sfp-2_TR | — |

**IP адреса на SW1 (SVI):**
- 192.168.0.11/24 — BridgeLocal (management)
- 192.168.10.190/24 — VLAN 10
- 192.168.20.201/24 — VLAN 20
- 192.168.30.253/24 — VLAN 30

**Default Gateway:** 192.168.0.1 (SFA-RBG-1) — ECMP маршрутизация через все VLAN

---

### 🌳 RSTP Configuration

**Spanning Tree Protocol:** RSTP (Rapid Spanning Tree Protocol)

| Параметр | Значение |
|----------|----------|
| **Bridge Priority** | 0x4000 (16384) |
| **Root Bridge** | ❌ NO (SFA-RBG-1 is Root, priority=0x1000) |
| **Uplink Port State** | Forwarding (sfp-1_TR) |
| **Protocol Mode** | RSTP |
| **VLAN Filtering** | Yes |

**Топология STP:**
```
SFA-RBG-1 (Root Bridge, priority=4096)
  └─ SW1 (Non-root, priority=16384)
      ├─ SW2 (Downstream)
      └─ SW3 (Downstream)
```

**Вердикт:** ✅ Корректная конфигурация. SFA-RBG-1 является Root Bridge, SW1 — designated switch.

---

### ⚡ Performance Optimization

**Flow Control:** Отключен на всех SFP портах
```mikrotik
/interface ethernet print where name~"sfp"
# sfp-1_TR: tx-flow-control=off, rx-flow-control=off
# sfp-2_TR: tx-flow-control=off, rx-flow-control=off
```

**Зачем отключен:**
- Синхронизация с SFA-RBG-1 (Phase 1 — Flow Control OFF)
- Снижение задержек при высоких нагрузках
- Улучшение производительности Inter-VLAN routing

**Hardware Offload:**
- Switch Chip: Marvell-98DX3236 (CRS326)
- Hardware VLAN filtering: Yes
- L2 switching: offloaded to switch chip

---

### 🔐 Security & Management

**SSH Access (Port 22222):**
```bash
# С сервера SFA-MNG (через SSH ключ):
ssh -p 22222 -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@192.168.0.11

# Или короткий alias:
ssh sfa-sw1
```

**Группа automation (AI Bot Access):**
```mikrotik
Policy: ssh,read,write,test,api,rest-api
Запрещено: password,reboot,policy,sensitive,romon (принцип least privilege)
```

**SSH Keys:**
- User: `sysadmin-bot`
- Key Type: Ed25519 (256 bit)
- Owner: `sysadmin-bot@sfa-mng`

**Disabled Services (Security Hardening):**
| Service | Port | Status |
|---------|------|--------|
| FTP | 21 | ❌ Disabled |
| Telnet | 23 | ❌ Disabled |
| HTTP | 80 | ❌ Disabled |
| HTTPS | 443 | ❌ Disabled |
| API | 8728 | ❌ Disabled |
| API-SSL | 8729 | ❌ Disabled |
| SSH | 22222 | ✅ Enabled (IP whitelist: 192.168.0.0/16, 10.10.10.0/24) |
| WinBox | 8291 | ✅ Enabled (IP whitelist: admin IPs only) |

**SNMP Security:** ✅ **НАСТРОЕНО**
```mikrotik
Community: pvl
Addresses: 192.168.0.0/16, 10.10.10.0/24 (включает Zabbix, SFA-MNG, VPN)
Read Access: Yes
Write Access: No
```
**Дата применения:** 15.02.2026
**Доступ разрешён только для:**
- 192.168.0.0/16 (Zabbix 192.168.0.33, SFA-MNG 192.168.0.35, и др.)
- 10.10.10.0/24 (VPN сеть)

---

### 📊 Phase 2 Audit Results (15.02.2026)

**Контрольные точки:**

| # | Проверка | Статус | Комментарий |
|---|----------|--------|-------------|
| 1 | **VLAN Tagging Consistency** | ✅ PASS | VLAN 10, 20, 30 синхронизированы с SFA-RBG-1 |
| 2 | **RSTP Root Bridge** | ✅ PASS | SFA-RBG-1 является Root (priority=4096 < 16384) |
| 3 | **Flow Control** | ✅ PASS | Отключен на всех SFP портах (sync с SFA-RBG-1) |
| 4 | **Security Hardening** | ✅ PASS | SSH ключи, disabled services, automation group |
| 5 | **SSH Access** | ✅ PASS | Порт 22222, IP whitelist, Ed25519 key |
| 6 | **System Identity** | ✅ PASS | `SFA-SW1` (унифицировано) |
| 7 | **SNMP Security** | ✅ PASS | IP whitelist применён (192.168.0.0/16, 10.10.10.0/24) |

**Статус:** ✅ **100% COMPLETE** — Все рекомендации исправлены (15.02.2026)

**Детальный отчёт:** `/home/maimik/PHASE2_AUDIT_REPORT.md`

---

### 🛠️ Команды управления

```bash
# SSH подключение (с сервера SFA-MNG)
ssh -p 22222 -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@192.168.0.11

# Проверить VLAN конфигурацию
/interface bridge vlan print

# Проверить RSTP статус
/interface bridge print detail

# Проверить Flow Control
/interface ethernet print where name~"sfp"

# Проверить security services
/ip service print

# Проверить SNMP
/snmp community print

# Проверить SSH ключи
/user ssh-keys print
```

---

## 2.3. SW2 (TR11-SW2): DISTRIBUTION SWITCH TRAIAN-11

*Обновлено: 15.02.2026 — Phase 3: L2 Distribution Audit Complete*

**Модель:** MikroTik CRS326-24G-2S+ (24x Gigabit + 2x 10G SFP+)
**IP Management:** 192.168.0.12/24
**RouterOS Version:** 7.21.2 (stable)
**Локация:** MGM Store (Магазин Траян-11, Кишинёв)
**Статус:** ✅ **AUDITED & SYNCED WITH SW1**

---

### 🔌 Роль в сети

SW2 — distribution свитч для филиала Traian-11 (MGM Store). Выполняет функции:
- **Access Layer** для рабочих станций и оборудования магазина
- **VLAN Segmentation** (торговый зал, видеонаблюдение)
- **Upstream к SW1** (центральный офис SFA-31)

---

### 🌐 Топология подключения

```
                    SW1 (SFA-SW1)
                    192.168.0.11
                         |
                  eth24_TR/eth23_TR (distribution port)
                         |
                    sfp-1_TR (uplink)
                         |
                    SW2 (CRS326)
                   192.168.0.12
                /       |        \
          eth3-6    eth8-12    eth17-20
        (VLAN 10)  (VLAN 10)   (VLAN 20)
```

**Uplink к SW1:**
- Порт SW2: `sfp-1_TR` (10G SFP+)
- Порт SW1: `eth24_TR` или `eth23_TR` (Gigabit distribution port)
- VLAN: 10, 20, 30 (tagged)

**Access порты:**
- VLAN 10 (Траян-11): eth3-6, eth8-12, eth14 (10 портов)
- VLAN 20 (Видеонаблюдение): eth17-20 (4 порта)

---

### 🏷️ VLAN Configuration

| VLAN ID | Subnet | Назначение | Trunk Ports | Access Ports (Untagged) | IP на SW2 |
|---------|--------|------------|-------------|------------------------|-----------|
| **10** | 192.168.10.0/24 | Траян-11 (MGM Store) | sfp-1_TR | eth3-6, eth8-12, eth14 (10 портов) | 192.168.10.181/24 |
| **20** | 192.168.20.0/24 | Видеонаблюдение (CCTV) | sfp-1_TR | eth17-20 (4 порта, камеры) | 192.168.20.216/24 |
| **30** | 192.168.30.0/24 | Резерв | sfp-1_TR | — | — |

**IP адреса на SW2 (SVI):**
- 192.168.0.12/24 — BridgeLocal (management)
- 192.168.10.181/24 — VLAN 10 (Traian-11)
- 192.168.20.216/24 — VLAN 20 (Видеонаблюдение)

**Default Gateway:** 192.168.0.1 (SFA-RBG-1) через SW1

---

### 🌳 RSTP Configuration

**Spanning Tree Protocol:** RSTP (Rapid Spanning Tree Protocol)

| Параметр | Значение |
|----------|----------|
| **Bridge Priority** | 0x4000 (16384) |
| **Root Bridge** | ❌ NO (SFA-RBG-1 is Root, priority=0x1000) |
| **Uplink Port State** | Forwarding (sfp-1_TR → SW1) |
| **Protocol Mode** | RSTP |
| **VLAN Filtering** | Yes |

**Топология STP:**
```
SFA-RBG-1 (Root Bridge, priority=4096)
  └─ SW1 (Non-root, priority=16384)
      └─ SW2 (Non-root, priority=16384) ◄── THIS DEVICE
```

**Вердикт:** ✅ Корректная конфигурация. SW2 является downstream свитчем, SFA-RBG-1 — Root Bridge.

---

### ⚡ Performance Optimization

**Flow Control:** Отключен на всех SFP портах
```mikrotik
/interface ethernet print where name~"sfp"
# sfp-1_TR: tx-flow-control=off, rx-flow-control=off ✅
# sfp-2_TR: tx-flow-control=off, rx-flow-control=off ✅
```

**Зачем отключен:**
- Синхронизация с SW1 и SFA-RBG-1
- Снижение задержек при высоких нагрузках
- Улучшение производительности Inter-VLAN routing

**Hardware Offload:**
- Switch Chip: Marvell-98DX3236 (CRS326)
- Hardware VLAN filtering: Yes
- L2 switching: offloaded to switch chip

---

### 🔐 Security & Management

**SSH Access (Port 22222):**
```bash
# С сервера SFA-MNG (через SSH ключ):
ssh -p 22222 -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@192.168.0.12
```

**Группа automation (AI Bot Access):**
```mikrotik
Policy: ssh,read,write,test,api,rest-api
Запрещено: password,reboot,policy,sensitive,romon
```

**SSH Keys:**
- User: `sysadmin-bot`
- Key Type: Ed25519 (256 bit)
- Owner: `sysadmin-bot@sfa-mng`

**Disabled Services (Security Hardening):**
| Service | Port | Status |
|---------|------|--------|
| FTP | 21 | ❌ Disabled |
| Telnet | 23 | ❌ Disabled |
| HTTP | 80 | ❌ Disabled |
| HTTPS | 443 | ❌ Disabled |
| API | 8728 | ❌ Disabled |
| API-SSL | 8729 | ❌ Disabled |
| SSH | 22222 | ✅ Enabled (IP whitelist: 192.168.0.0/16, 10.10.10.0/24) |
| WinBox | 8291 | ✅ Enabled (IP whitelist: admin IPs only) |

**SNMP Security:** ✅ **НАСТРОЕНО (15.02.2026)**
```mikrotik
Community: pvl
Addresses: 192.168.0.0/16, 10.10.10.0/24
Read Access: Yes
Write Access: No
```
**Доступ разрешён:**
- 192.168.0.0/16 (Zabbix, SFA-MNG, локальная сеть)
- 10.10.10.0/24 (VPN)

---

### 📊 Phase 3 Audit Results (15.02.2026)

**Контрольные точки:**

| # | Проверка | Статус | Комментарий |
|---|----------|--------|-------------|
| 1 | **Uplink Port** | ✅ PASS | sfp-1_TR → SW1 (корректно) |
| 2 | **Flow Control** | ✅ PASS | Отключен на SFP портах (sync с SW1) |
| 3 | **RSTP Topology** | ✅ PASS | SFA-RBG-1 Root, SW2 downstream от SW1 |
| 4 | **VLAN 10, 20** | ✅ PASS | Tagged на uplink |
| 5 | **VLAN 30 Sync** | ✅ PASS | Добавлен для единообразия (15.02.2026) |
| 6 | **SSH Access** | ✅ PASS | Порт 22222, IP whitelist, Ed25519 key |
| 7 | **Disabled Services** | ✅ PASS | Telnet, FTP, HTTP, API отключены |
| 8 | **SNMP Security** | ✅ PASS | IP whitelist применён (15.02.2026) |

**Применённые исправления (15.02.2026):**
1. ✅ SNMP Security — IP whitelist (192.168.0.0/16, 10.10.10.0/24)
2. ✅ VLAN 30 — добавлен на uplink для синхронизации trunk конфигурации

**Статус:** ✅ **100% COMPLETE**

**Детальный отчёт:** `/home/maimik/PHASE3_SW2_AUDIT_REPORT.md`

---

### 🛠️ Команды управления

```bash
# SSH подключение (с сервера SFA-MNG)
ssh -p 22222 -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@192.168.0.12

# Проверить VLAN конфигурацию
/interface bridge vlan print

# Проверить RSTP статус
/interface bridge print detail

# Проверить Flow Control
/interface ethernet print where name~"sfp"

# Проверить security services
/ip service print

# Проверить SNMP
/snmp community print

# Проверить SSH ключи
/user ssh-keys print

# Проверить IP адреса
/ip address print
```

---

### 2.4. SW3 (TR11-SW3): L2 Access Switch (Depozit)

**Дата аудита:** 15.02.2026 (Phase 4)
**Статус:** ✅ **AUDITED & SYNCED WITH SW1/SW2**

#### Основная информация

| Параметр | Значение |
|----------|----------|
| **Identity** | TR11-SW3 |
| **IP Address** | 192.168.0.13/24 |
| **Локация** | Depozit (Склад, Traian-11) |
| **Модель** | MikroTik CRS326-24G-2S+ |
| **RouterOS** | 7.21.2 (stable) |
| **Роль** | L2 Access Switch (downstream от SW1/SW2) |
| **CPU** | ARM, 2 cores @ 800MHz |
| **RAM** | 512 MiB |
| **Uptime** | Стабильный (без перезагрузок) |

#### Топология подключения

```
      SFA-RBG-1 (CCR2004)
            |
       SW1 (Central)
        |       |
      SW2     SW3 (192.168.0.13) ◄── THIS DEVICE
  (Traian)  (Depozit)
```

**Uplink к SW1 или SW2:**
- Порт SW3: `sfp-1_TR` (10G SFP+, Running)
- Upstream: SW1 или SW2 (distribution layer)
- VLAN Tagging: 10, 20, 30 (trunk)

#### VLAN Configuration

| VLAN ID | Subnet | Назначение | Tagged Ports | Untagged (Access) |
|---------|--------|------------|--------------|-------------------|
| **10** | 192.168.10.0/24 | Traian-11 Office | BridgeLocal, sfp-1_TR, sfp-2_TR, eth23_TR, eth24_TR, eth1_TR, eth2_TR | eth6_v10, eth12_v10, eth11_v10, eth14_v10, eth4_v10 (5 портов) |
| **20** | 192.168.20.0/24 | Видеонаблюдение | BridgeLocal, sfp-1_TR, sfp-2_TR, eth23_TR, eth24_TR, eth1_TR, eth2_TR | eth19_v20 (1 порт) |
| **30** | 192.168.30.0/24 | Reserved (trunk only) | BridgeLocal, sfp-1_TR | — (только trunk) |

**Management IP:** 192.168.0.13/24 (на BridgeLocal, не привязан к VLAN)

**Default Gateway:** 192.168.0.1 (SFA-RBG-1) через SW1/SW2

#### RSTP (Rapid Spanning Tree Protocol)

| Параметр | Значение |
|----------|----------|
| **Protocol Mode** | RSTP |
| **Bridge Priority** | 0x4000 (16384 — downstream switch) |
| **Root Bridge** | ❌ NO (SFA-RBG-1 is Root, priority=0x1000) |
| **Топология** | Leaf switch (3rd tier) |

**RSTP Hierarchy:**
```
SFA-RBG-1 (Root Bridge, priority=4096)
  └─ SW1 (priority=16384)
      ├─ SW2 (priority=16384)
      └─ SW3 (priority=16384) ◄── THIS DEVICE
```

**Вердикт:** ✅ Корректная конфигурация. SW3 является leaf switch, SFA-RBG-1 — Root Bridge.

#### Flow Control Settings

**SFP Ports (10G):**
| Порт | RX Flow Control | TX Flow Control |
|------|----------------|-----------------|
| **sfp-1_TR** (uplink) | OFF | OFF |
| **sfp-2_TR** (unused) | OFF | OFF |

**Причина отключения:**
- Синхронизация с SW1, SW2, SFA-RBG-1
- Снижение задержек при высоких нагрузках
- Предотвращение паузы в работе network stack

#### Security Hardening

**Disabled Services:**
| Service | Port | Status |
|---------|------|--------|
| Telnet | 23 | ❌ Disabled |
| FTP | 21 | ❌ Disabled |
| HTTP (www) | 80 | ❌ Disabled |
| HTTPS (www-ssl) | 443 | ❌ Disabled |
| API | 8728 | ❌ Disabled |
| API-SSL | 8729 | ❌ Disabled |

**Active Management Services:**
| Service | Port | Address Whitelist | Auth Method |
|---------|------|-------------------|-------------|
| **SSH** | 22222 | 192.168.0.0/16, 10.10.10.0/24 | Ed25519 Key (sysadmin-bot) |
| **WinBox** | 8291 | 192.168.0.1/32, 192.168.0.21/32, 192.168.10.21/32, 10.10.10.0/24, 188.138.208.200/32 | Password |
| **SNMP** | 161 | 192.168.0.0/16, 10.10.10.0/24 | Community "pvl" (read-only) |

**SSH Access:**
```bash
ssh -p 22222 -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@192.168.0.13
```

**User Group:** automation (ssh, read, write, api, rest-api, test — NO password, reboot, policy, sensitive)

#### Phase 4 Audit Results (15.02.2026)

**Контрольные точки:**

| # | Проверка | Статус | Комментарий |
|---|----------|--------|-------------|
| 1 | **Uplink Port** | ✅ PASS | sfp-1_TR активен (10G SFP+) |
| 2 | **Flow Control** | ✅ PASS | Уже было OFF (синхронизировано) |
| 3 | **RSTP Topology** | ✅ PASS | SFA-RBG-1 Root, SW3 downstream |
| 4 | **VLAN 10, 20** | ✅ PASS | Tagged на uplink |
| 5 | **VLAN 30 Sync** | ✅ FIXED | Добавлен на uplink (15.02.2026) |
| 6 | **SSH Access** | ✅ PASS | Порт 22222, IP whitelist, Ed25519 key |
| 7 | **Disabled Services** | ✅ PASS | Telnet, FTP, HTTP, API отключены |
| 8 | **SNMP Security** | ✅ FIXED | IP whitelist применён (15.02.2026) |

**Применённые исправления (15.02.2026):**
1. ✅ SNMP Security — IP whitelist (192.168.0.0/16, 10.10.10.0/24)
2. ✅ VLAN 30 — добавлен на uplink для синхронизации trunk конфигурации

**Статус:** ✅ **100% COMPLETE**

**Детальный отчёт:** `/home/maimik/PHASE4_SW3_FINAL_REPORT.md`

---

### 🛠️ Команды управления SW3

```bash
# SSH подключение (с сервера SFA-MNG)
ssh -p 22222 -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@192.168.0.13

# Проверить VLAN конфигурацию
/interface bridge vlan print

# Проверить RSTP статус
/interface bridge print detail

# Проверить Flow Control
/interface ethernet print where name~"sfp"

# Проверить security services
/ip service print

# Проверить SNMP
/snmp community print

# Проверить SSH ключи
/user ssh-keys print

# Проверить IP адреса
/ip address print
```

---

### 2.5. FRA-38 (CCR2004-FRA): Router (Филиал Franko-38)

**Дата аудита:** 15.02.2026 (Phase 5), 16.02.2026 (Phase 9-10 Wi-Fi)
**Статус:** ✅ **FULLY OPTIMIZED** (Router + QoS + Wi-Fi + Guest Isolation)

#### Основная информация

| Параметр | Значение |
|----------|----------|
| **Identity** | CCR2004-FRA |
| **IP Address (LAN)** | 192.168.3.1/24 |
| **IP Address (VPN)** | 10.10.10.3 |
| **WAN IP** | 77.89.229.154 |
| **Локация** | Франко-38 (Склад/Магазин) |
| **Модель** | MikroTik CCR2004-1G-12S+2XS |
| **RouterOS** | 7.21.2 (stable) |
| **Роль** | Branch Router + VPN Gateway |
| **CPU** | ARM64, 4 cores @ 2000MHz |
| **RAM** | 4096 MiB |

#### Топология подключения

```
      INTERNET (WAN)
           |
    77.89.229.154
           |
      CCR2004-FRA (192.168.3.1) ◄── THIS DEVICE
           |
      ┌────┴────┐
   LAN        Guest
192.168.3.0  192.168.30.0
   /24         /24
```

**WireGuard VPN к главному офису (SFA-31):**
```
FRA-38 (10.10.10.3)  ◄──────VPN──────►  SFA-31 (10.10.10.1)
   CCR2004-FRA                          SFA-RBG-1
```
- **Peer:** SFA (главный офис)
- **Endpoint:** 77.89.251.234:20119
- **Allowed Address:** 10.10.10.0/24, 192.168.0.0/16
- **Persistent Keepalive:** 25s
- **Status:** ✅ Active (Last handshake: <1 min)

#### Network Configuration

**Bridges:**
| Bridge | IP Address | Назначение | VLAN Filtering |
|--------|-----------|------------|----------------|
| **LocalBbridge** | 192.168.3.1/24 | Основная LAN | Disabled |
| **bridgeGuest** | 192.168.30.1/24 | Гостевая сеть | Disabled |
| **ATS** | — | (назначение неизвестно) | Disabled |

**Примечание:** VLAN filtering отключен (в отличие от SFA-RBG-1).

#### QoS Optimization (РЕШЁННАЯ ПРОБЛЕМА!)

**Проблема (до оптимизации):**
❌ **"Бутылочное горлышко"** — постоянный лимит 95 Mbps блокировал ночные бэкапы и обновления системы.

**Решение (применено администратором, 15.02.2026):**
✅ **Dynamic QoS** — автоматическое переключение между режимами через Scheduler + Scripts.

**Скрипты:** ⚠️ **UPDATED 15.02.2026**
| Скрипт | Команда | Назначение |
|--------|---------|------------|
| **QoS_DAY_MODE** | `/queue tree set [find name="TOTAL_DOWNLOAD"] max-limit=95M` | Ограничить канал до 95 Mbps (08:00-18:00) |
| **QoS_NIGHT_MODE** | `/queue tree set [find name="TOTAL_DOWNLOAD"] max-limit=98M` | Ограничить канал до 98 Mbps ночью (предотвращает ISP bottleneck) |

**Scheduler:**
| Задача | Время | Интервал | Действие | Статус |
|--------|-------|----------|----------|--------|
| **Start_Day_QoS** | 08:00 | 1d | QoS_DAY_MODE | ✅ Active |
| **Start_Night_QoS** | 18:00 | 1d | QoS_NIGHT_MODE | ✅ Active |

**Queue TOTAL_DOWNLOAD:** ⚠️ **UPDATED 15.02.2026**
- **Текущий max-limit:** Зависит от времени суток
  - 08:00-18:00 (DAY): 95M (защита от перегрузки)
  - 18:00-08:00 (NIGHT): 98M (оптимизировано для бэкапов без ISP bottleneck)
- **Priority:** 8
- **Примечание:** max-limit=0 (unlimited) вызывал packet loss из-за перегрузки на стороне провайдера
- **Parent:** global

**Результат:** ✅ **Проблема "бутылочного горлышка" РЕШЕНА!**
- Ночью (18:00-08:00): канал не ограничен — бэкапы и обновления работают на полной скорости
- Днём (08:00-18:00): канал ограничен 95 Mbps — защита от перегрузки

#### Security Hardening (Phase 5: 15.02.2026)

**Disabled Services:**
| Service | Port | Status |
|---------|------|--------|
| Telnet | 23 | ❌ Disabled |
| FTP | 21 | ❌ Disabled |
| HTTP (www) | 80 | ❌ Disabled |
| HTTPS (www-ssl) | 443 | ❌ Disabled |
| API | 8728 | ❌ Disabled |
| API-SSL | 8729 | ❌ Disabled |

**Active Management Services:**
| Service | Port | Address Whitelist | Auth Method | Status |
|---------|------|-------------------|-------------|--------|
| **SSH** | 22222 | 192.168.0.0/16, 10.10.10.0/24 | Ed25519 Key (sysadmin-bot) | ✅ Secure |
| **WinBox** | 8291 | 188.138.208.200/32, 77.89.251.234/32, 192.168.0.0/16, 10.10.10.0/24 | Password | ✅ Secure |

**SNMP Security:**
```mikrotik
/snmp community
set [find name="pvl"] addresses=192.168.0.0/16,10.10.10.0/24
```
✅ **Уже защищён** (IP whitelist для Zabbix и VPN)

**Neighbor Discovery:**
- **Interface List:** LAN only
- **Protocol:** cdp, lldp, mndp
- **Status:** ✅ Ограничен локальной сетью

**DNS Resolver Security (КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ):**

**ДО исправления (Phase 5):**
```
allow-remote-requests: yes
DNS port 53: ОТКРЫТ для внешних запросов (Open Resolver!)
```

**ПОСЛЕ исправления (15.02.2026):**
```mikrotik
# Firewall правило #19
/ip firewall filter add chain=input action=drop protocol=udp \
    in-interface-list=WAN dst-port=53 comment="BLOCK Public DNS (UDP)"

# Firewall правило #20
/ip firewall filter add chain=input action=drop protocol=tcp \
    in-interface-list=WAN dst-port=53 comment="BLOCK Public DNS (TCP)"
```

**Результат:**
- ✅ DNS доступен только из LAN (192.168.3.0/24) и VPN (10.10.10.0/24)
- ❌ DNS **заблокирован** для внешних IP (WAN)
- ✅ Защита от DNS amplification DDoS атак

#### Phase 5 Audit Results (15.02.2026)

**Контрольные точки:**

| # | Проверка | Статус | Комментарий |
|---|----------|--------|-------------|
| 1 | **QoS Scripts** (DAY/NIGHT) | ✅ PASS | Скрипты существуют и готовы к запуску |
| 2 | **Scheduler** (Start_Day/Night_QoS) | ✅ PASS | Настроен корректно, Next Run: 2026-02-16 |
| 3 | **Queue TOTAL_DOWNLOAD** | ✅ PASS | max-limit=95M (DAY MODE активен) |
| 4 | **Services Hardening** | ✅ PASS | Telnet, FTP, WWW, API отключены |
| 5 | **SNMP Security** | ✅ PASS | IP whitelist (уже был настроен) |
| 6 | **Neighbor Discovery** | ✅ PASS | Ограничен LAN |
| 7 | **DNS Resolver** | ✅ FIXED | Firewall блокирует DNS с WAN (15.02.2026) |
| 8 | **WireGuard VPN** | ✅ PASS | Active, подключён к SFA-31 |

**Применённые исправления (15.02.2026):**
1. ✅ **DNS Security** — Firewall rules #19, #20: Block DNS (UDP/TCP) from WAN

**Статус:** ✅ **100% COMPLETE & SECURED**

**Детальный отчёт:** `/home/maimik/PHASE5_FRANKO38_AUDIT_REPORT.md`

---

### 🛠️ Команды управления Franko-38

```bash
# SSH подключение (с сервера SFA-MNG через VPN)
ssh -p 22222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@10.10.10.3

# Проверить текущий QoS режим
/queue tree print where name="TOTAL_DOWNLOAD"

# Проверить scheduler (следующий запуск)
/system scheduler print where name~"QoS"

# Проверить WireGuard VPN
/interface wireguard print
/interface wireguard peers print

# Проверить DNS firewall
/ip firewall filter print where chain="input" and dst-port=53

# Проверить SNMP
/snmp community print

# Проверить security services
/ip service print

# Проверить логи QoS переключений
/log print where message~"QoS"
```

**Мониторинг QoS (рекомендуется первые 24-48 часов):**
```bash
# Утром (после 08:00) — проверить DAY MODE
ssh -p 22222 -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@10.10.10.3 \
    '/queue tree print where name="TOTAL_DOWNLOAD"'
# Ожидается: max-limit=95M

# Вечером (после 18:00) — проверить NIGHT MODE
ssh -p 22222 -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@10.10.10.3 \
    '/queue tree print where name="TOTAL_DOWNLOAD"'
# Ожидается: max-limit=0 (unlimited)
```

#### Wi-Fi Configuration (Phase 9 & 10: 16.02.2026)

**Статус:** ✅ **FULLY DEPLOYED** — Standalone Wi-Fi with Guest Isolation

**Access Point:** FRA38-wAP (192.168.3.253)
- **Model:** MikroTik wAP (IPQ4019, dual-band)
- **RouterOS:** 7.21.3 (stable)
- **Mode:** **Standalone** (CAPsMAN disabled)

**Wireless Networks:**

| Interface | SSID | Band | Security | VLAN Mode | Network | Status |
|-----------|------|------|----------|-----------|---------|--------|
| **wlan5G** | PVL-5G | 5GHz (ac) | WPA2-PSK (FRA_Local) | Native (untagged) | 192.168.3.0/24 | ✅ Active |
| **wlan2G** | PVL_FREE | 2.4GHz (g/n) | Open (Free_Local) | Tagged VLAN 30 | 192.168.30.0/24 | ✅ Active |

**Guest Network Isolation (VLAN 30):**
```mikrotik
# VLAN 30 interface on router
/interface vlan add name=vlan30_incoming interface=LocalBbridge vlan-id=30

# Add to guest bridge
/interface bridge port add bridge=bridgeGuest interface=vlan30_incoming

# Guest DHCP
/ip dhcp-server add name=dhcpGuest interface=bridgeGuest address-pool=dhcp_30
/ip pool add name=dhcp_30 ranges=192.168.30.100-192.168.30.254

# Firewall isolation (internet-only for guests)
/ip firewall filter
add chain=forward action=accept src-address=192.168.30.0/24 \
    dst-address=!192.168.30.0/24 comment="Guest### - Internet only"
```

**Network Flow:**
- **Office (wlan5G):** Client → Native VLAN → LocalBbridge → 192.168.3.0/24 (Full LAN access)
- **Guests (wlan2G):** Client → VLAN 30 tagged → vlan30_incoming → bridgeGuest → 192.168.30.0/24 (Internet only)

**Security:**
- ✅ Guest Wi-Fi isolated from LAN (192.168.3.0/24)
- ✅ DHCP pool: 192.168.30.100-254 (155 addresses)
- ✅ Firewall: Guests → Internet ONLY, LAN blocked

**Management:**
```bash
# SSH to access point
ssh -p 22222 -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@192.168.3.253

# Check wireless status
/interface wireless monitor wlan5G once
/interface wireless monitor wlan2G once

# Check registered clients
/interface wireless registration-table print
```

**Detailed Report:** `/home/maimik/PHASE9-10_FRANKO38_WIFI_REPORT.md`

#### Firewall Lockdown (Phase 11: 17.02.2026) ⚡ UPGRADED TO RFC1918

**Статус:** ✅ **FULLY SECURED** — L3/L4 Guest Isolation Active (RFC1918 Standard)

**Цель:** Полная изоляция Guest Wi-Fi (VLAN 30) от ВСЕХ частных сетей (RFC1918) и управления роутером на уровне firewall filter.

**Address List RFC1918 (Все частные сети):**
```mikrotik
/ip firewall address-list
add list=RFC1918 address=10.0.0.0/8 comment="Private Class A"
add list=RFC1918 address=172.16.0.0/12 comment="Private Class B"
add list=RFC1918 address=192.168.0.0/16 comment="Private Class C"
```

**Применённые правила:**

##### Rule #0: Block Guest → ANY Private Network (RFC1918)
```mikrotik
/ip firewall filter add chain=forward \
  src-address=192.168.30.0/24 \
  dst-address-list=RFC1918 \
  action=drop \
  comment="BLOCK: Guest to ANY Private Net (RFC1918)" \
  place-before=0
```

**Effect:** Guests не могут подключиться к ЛЮБЫМ частным сетям:
- ❌ 10.0.0.0/8 (VPN сети, включая 10.10.10.0/24)
- ❌ 172.16.0.0/12 (возможные будущие подсети)
- ❌ 192.168.0.0/16 (все локальные сети, включая Office LAN)

##### Rule #1: Block Guest → Router Management
```mikrotik
/ip firewall filter add chain=input \
  src-address=192.168.30.0/24 \
  protocol=tcp \
  dst-port=21,22,23,80,443,8291 \
  action=drop \
  comment="BLOCK: Guest Mgmt Access" \
  place-before=1
```

**Effect:** Guests не могут подключиться к управлению роутером

**Заблокированные порты:**
- ❌ FTP (21)
- ❌ SSH (22)
- ❌ Telnet (23)
- ❌ HTTP (80)
- ❌ HTTPS (443)
- ❌ WinBox (8291)

**Разрешённые сервисы:**
- ✅ DNS (53) — для резолвинга доменов
- ✅ DHCP (67) — для получения IP-адресов
- ✅ Internet — через existing forward rules

**Изоляция гостей:**
- ✅ **L2 Isolation:** VLAN 30 → bridgeGuest (separate bridge)
- ✅ **L3/L4 Isolation:** Firewall Filter (chain=forward + chain=input)
- ✅ **Defense in Depth:** Двухуровневая защита (VLAN + Firewall)

**Мониторинг попыток нарушения:**
```bash
# Проверить счетчики firewall
ssh -p 22222 -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@10.10.10.3 \
  '/ip firewall filter print stats where comment~"BLOCK: Guest"'

# Если BYTES/PACKETS > 0 → кто-то пытается пробиться
# Если BYTES/PACKETS = 0 → нет попыток нарушения
```

**Результат:**
- ✅ Guest Wi-Fi полностью изолирован от ВСЕХ частных сетей (RFC1918)
- ✅ Защита от доступа к VPN (10.10.10.0/24) и другим филиалам
- ✅ Защита от будущих подсетей (автоматическая блокировка через Address List)
- ✅ Guest Wi-Fi изолирован от управления роутером
- ✅ DNS/DHCP доступны для корректной работы гостей
- ✅ Internet-only access для гостей
- ✅ Audit trail через firewall counters

**Detailed Report:** `/home/maimik/PHASE11_FRA38_FIREWALL_AUDIT.md`

---

### 2.6. SW4 (FRA38-SW4): L2 Access Switch (Franko-38)

**Дата аудита:** 15.02.2026 (Phase 6)
**Статус:** ✅ **GOLDEN IMAGE COMPLIANT & SECURED**

#### Основная информация

| Параметр | Значение |
|----------|----------|
| **Identity** | FRA38-SW4 (было: SW4) |
| **IP Address** | 192.168.3.14/24 |
| **Локация** | Франко-38 (Склад/Магазин) |
| **Модель** | MikroTik CRS326-24G-2S+ |
| **RouterOS** | 7.21.3 (stable) |
| **Роль** | L2 Access Switch (flat network) |
| **CPU** | ARM, 2 cores @ 800MHz |
| **RAM** | 512 MiB |

#### Топология подключения

```
   CCR2004-FRA (Router, 192.168.3.1)
           |
           | (sfp-sfpplus1 uplink)
           |
      FRA38-SW4 (192.168.3.14) ◄── THIS DEVICE
           |
    ether1-24 (access ports)
   (VLAN 1, flat network)
```

**Uplink к роутеру:**
- Порт SW4: `sfp-sfpplus1` (10G SFP+, Running)
- Порт Router: LocalBbridge
- Network: Flat (VLAN 1, no trunking)

#### Network Configuration

**Network Type:** **Flat Network** (single VLAN 1)

| Параметр | Значение |
|----------|----------|
| **Bridge** | BridgeLocal |
| **VLAN Filtering** | Disabled (flat network) |
| **All Ports** | PVID=1 (untagged VLAN 1) |
| **Management IP** | 192.168.3.14/24 |
| **Uplink** | sfp-sfpplus1 → Router (192.168.3.1) |

**Примечание:** В отличие от SFA-31 (VLAN 10,20,30), филиал Franko-38 использует простую flat network без VLAN сегментации.

#### RSTP (Rapid Spanning Tree Protocol)

| Параметр | Значение |
|----------|----------|
| **Protocol Mode** | RSTP |
| **Bridge Priority** | 0x8000 (32768) |
| **Root Bridge** | ❌ NO (роутер bridgeGuest is Root) |
| **Root Port** | sfp-sfpplus1 (uplink) |
| **Root Path Cost** | 22010 |

**RSTP Hierarchy (Franko-38):**
```
CCR2004-FRA (Router)
  ├─ bridgeGuest (Root Bridge, priority=32768, MAC 18:FD:74:F6:2E:1B)
  ├─ LocalBbridge (priority=32768, MAC F4:1E:57:D0:5D:06)
  └─ SW4 (FRA38-SW4, priority=32768, MAC D4:01:C3:D0:4F:C0) ◄── THIS
```

**Вердикт:** ✅ Корректная конфигурация. SW4 является access switch, не Root Bridge.

**Примечание:** Root выбран по MAC адресу (все bridge имеют одинаковый priority 32768). Рекомендуется установить explicit priority на роутере для LocalBbridge.

#### Flow Control Settings

**SFP Ports (10G):**
| Порт | TX Flow Control | RX Flow Control | Статус |
|------|----------------|-----------------|--------|
| **sfp-sfpplus1** (uplink) | OFF | OFF | ✅ Корректно |
| **sfp-sfpplus2** (unused) | OFF | OFF | ✅ Корректно |

**Значение для QoS:**
- Flow Control отключен для корректной работы Dynamic QoS на роутере CCR2004-FRA
- Предотвращает задержки при переключении режимов DAY/NIGHT (см. раздел 2.5)

#### Security Hardening (Phase 6: 15.02.2026)

**Disabled Services:**
| Service | Port | Status |
|---------|------|--------|
| Telnet | 23 | ❌ Disabled |
| FTP | 21 | ❌ Disabled |
| HTTP (www) | 80 | ❌ Disabled |
| HTTPS (www-ssl) | 443 | ❌ Disabled |
| API | 8728 | ❌ Disabled |
| API-SSL | 8729 | ❌ Disabled |

**Active Management Services:**
| Service | Port | Address Whitelist | Auth Method | Status |
|---------|------|-------------------|-------------|--------|
| **SSH** | 22222 | 192.168.0.0/16, 10.10.10.0/24 | Ed25519 Key (sysadmin-bot) | ✅ Secure |
| **WinBox** | 8291 | 192.168.0.0/16, 10.10.10.0/24 | Password | ✅ Secure |

**SNMP Security (КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ):**

**ДО исправления (Phase 6):**
```
Community "pvl": addresses=::/0  ← ОТКРЫТО ДЛЯ ВСЕХ!
```

**ПОСЛЕ исправления (15.02.2026):**
```mikrotik
/snmp community set [find name="pvl"] addresses=192.168.0.0/16,10.10.10.0/24
```

**Результат:**
- ✅ SNMP доступен только из корпоративной сети (192.168.0.0/16) и VPN (10.10.10.0/24)
- ❌ SNMP **заблокирован** для внешних IP
- ✅ Включает Zabbix (192.168.0.33), SFA-MNG (192.168.0.35)

#### Golden Image Compliance (Phase 6)

**Контрольные точки:**

| # | Проверка | Статус | Комментарий |
|---|----------|--------|-------------|
| 1 | **Identity** | ✅ FIXED | FRA38-SW4 (было: SW4) |
| 2 | **Flow Control** | ✅ PASS | OFF на uplink (уже было) |
| 3 | **RSTP** | ✅ PASS | Not Root (Root - роутер) |
| 4 | **Services** | ✅ PASS | Telnet, FTP, WWW, API отключены |
| 5 | **SNMP Security** | ✅ FIXED | IP whitelist применён (15.02.2026) |
| 6 | **SSH** | ✅ PASS | Port 22222, IP whitelist, ключи |
| 7 | **VLAN** | ✅ PASS | Flat (VLAN 1) — задокументировано |

**Применённые исправления (15.02.2026):**
1. ✅ **Identity Standardization** — FRA38-SW4 (Golden Image)
2. ✅ **SNMP Security** — IP whitelist (192.168.0.0/16, 10.10.10.0/24)

**Статус:** ✅ **100% GOLDEN IMAGE COMPLIANT**

**Детальный отчёт:** `/home/maimik/PHASE6_SW4_AUDIT_REPORT.md`

---

### 🛠️ Команды управления SW4

```bash
# SSH подключение (из локальной сети Franko-38)
ssh -p 22222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@192.168.3.14

# Проверить Identity
/system identity print

# Проверить Flow Control
/interface ethernet print where name~"sfp"

# Проверить RSTP топологию
/interface bridge monitor BridgeLocal once

# Проверить SNMP
/snmp community print

# Проверить security services
/ip service print

# Проверить bridge ports
/interface bridge port print
```

---

### 2.7. DU-DEP (DUDEP): Branch Router (Производство)

**Дата аудита:** 15.02.2026 (Phase 7)
**Статус:** ✅ **OPTIMIZED & SECURED**

#### Основная информация

| Параметр | Значение |
|----------|----------|
| **Identity** | DUDEP |
| **WAN IP** | 77.89.241.86 |
| **VPN IP (WireGuard)** | 10.10.10.2 |
| **LAN Subnet** | 192.168.2.0/24 |
| **Модель** | MikroTik RB750Gr3 (hEX) |
| **RouterOS** | 7.21.3 (stable) |
| **Локация** | Производство |
| **CPU** | MIPS 1004Kc V2.15, 4 cores @ 880MHz |
| **RAM** | 256 MiB |

#### Топология подключения

```
SFA-31 Hub (10.10.10.1)
        |
   [WireGuard VPN]
        |
   DU-DEP (10.10.10.2) ◄── THIS DEVICE
        |
  192.168.2.0/24 (Production LAN)
        |
    DVR-DUDEP (192.168.2.5) — видеонаблюдение
```

**VPN Peer:** SFA-31 (77.89.251.234:20119)
**WAN Interface:** INET
**VPN Interface:** WG2

#### Security Hardening (Phase 7)

**DNS Resolver Protection:**
- ✅ **Закрыт Open Resolver** (firewall drop UDP/TCP 53 с WAN)
- Правила добавлены в `chain=input` для защиты от DDoS amplification

**SNMP Security:**
- ✅ **IP Whitelist:** 192.168.0.0/16, 10.10.10.0/24
- Community "public" — disabled
- Community "pvl" — активен только для внутренних подсетей

**Services:**
| Service | Port | Status |
|---------|------|--------|
| Telnet, FTP, WWW, API | 21, 23, 80, 8728 | ❌ DISABLED |
| **SSH** | 22222 | ✅ Enabled (IP whitelist) |
| **WinBox** | 8291 | ✅ Enabled (IP whitelist) |

#### Smart QoS: Day/Night Schedule

**Проблема (до оптимизации):** Статичный лимит 90M не учитывал ночные бэкапы.

**Решение:** Dynamic QoS с автоматическим переключением.

| Режим | Время | Max Limit | Назначение |
|-------|-------|-----------|------------|
| 🌞 **DAY MODE** | 08:00 - 18:00 | **95M** | Бизнес-приложения (1C, VPN) |
| 🌙 **NIGHT MODE** | 18:00 - 08:00 | **98M** | Бэкапы + стабильность мониторинга |

**ВАЖНО:** Ночной лимит = **98M** (НЕ 0!) для предотвращения Tail Drop у провайдера (урок из Franko-38).

**Priority Queues (всегда активны):**
1. **1C_PRIO** (priority=1, limit-at=10M) — Бухгалтерия
2. **VIDEO_PRIO** (priority=5, limit-at=20M) — Видеонаблюдение (192.168.2.5)
3. **OTHER_TRAFFIC** (priority=8) — Остальной трафик

**Scripts:**
```mikrotik
/system script print where name~"QoS"
# QoS_DAY_MODE: max-limit=95M
# QoS_NIGHT_MODE: max-limit=98M
```

**Scheduler:**
```mikrotik
/system scheduler print where name~"QoS"
# Start_Day_QoS: 08:00 (daily)
# Start_Night_QoS: 18:00 (daily)
```

#### VPN Connectivity

- ✅ **WireGuard активен** (Endpoint: SFA-31, 77.89.251.234:20119)
- ✅ **Ping к Hub:** 10.10.10.1 (packet-loss=0%, RTT ~1ms)
- ✅ **Ping к Zabbix:** 192.168.0.33 (packet-loss=0%, RTT ~1ms)

#### Управление (команды)

```bash
# SSH подключение
ssh -p 22222 -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@10.10.10.2

# Проверка QoS режима
/queue tree print where name="TOTAL_DOWNLOAD"

# Просмотр логов переключения
/log print where message~"QoS"

# Ручное переключение (экстренное)
/system script run QoS_DAY_MODE    # Включить ограничения
/system script run QoS_NIGHT_MODE  # Снять ограничения

# Проверка VPN
/interface wireguard peers print
ping 10.10.10.1 count=3
```

#### Мониторинг

- **Zabbix Host:** DU-DEP (SNMP + ICMP)
- **Firewall:** ✅ Zabbix (192.168.0.33) имеет доступ
- **Важные метрики:** CPU Load, WireGuard Peers, Queue Stats

---

### 2.8. OTTO-16: Branch Router (Магазин Кишинев)

**Дата аудита:** 15.02.2026 (Phase 8)
**Статус:** ✅ **OPTIMIZED & SECURED**

#### Основная информация

| Параметр | Значение |
|----------|----------|
| **Identity** | OTTO-16 |
| **WAN IP** | 89.149.115.99 |
| **VPN IP (WireGuard)** | 10.10.10.5 |
| **LAN Subnet** | 192.168.4.0/24 |
| **Модель** | MikroTik hAP ac² (RBD52G-5HacD2HnD) |
| **RouterOS** | 7.21.3 (stable) |
| **Локация** | Новый филиал (Кишинев, str. Otovaska, 16) |
| **CPU** | ARM, 4 cores @ 672MHz |
| **RAM** | 128 MiB |
| **WiFi** | 2.4 GHz + 5 GHz (802.11ac) |

#### Топология подключения

```
SFA-31 Hub (10.10.10.1)
        |
   [WireGuard VPN]
        |
   OTTO-16 (10.10.10.5) ◄── THIS DEVICE
        |
  192.168.4.0/24 (Store LAN)
        |
    Касса, 1C, POS-терминалы
```

**VPN Peer:** SFA-31 (77.89.251.234:20119)
**WAN Interface:** eth1_INET
**VPN Interface:** WG16

#### Security Hardening (Phase 8)

**DNS Resolver Protection:**
- ✅ **Закрыт Open Resolver** (firewall drop UDP/TCP 53 с WAN)
- Защита от использования в DDoS атаках

**SNMP Security:**
- ✅ **IP Whitelist:** 192.168.0.0/16, 10.10.10.0/24
- Community "public" — disabled

**Services:**
| Service | Port | Status |
|---------|------|--------|
| Telnet, FTP, WWW, API | 21, 23, 80, 8728 | ❌ DISABLED |
| **SSH** | 22222 | ✅ Enabled (IP whitelist) |
| **WinBox** | 8291 | ✅ Enabled (IP whitelist) |

#### Smart QoS: Day/Night Schedule

**Проблема (до оптимизации):** Статичный лимит 95M не учитывал ночные обновления ПО.

**Решение:** Dynamic QoS аналогично DU-DEP.

| Режим | Время | Max Limit | Назначение |
|-------|-------|-----------|------------|
| 🌞 **DAY MODE** | 08:00 - 18:00 | **95M** | Касса, 1C (стабильная работа магазина) |
| 🌙 **NIGHT MODE** | 18:00 - 08:00 | **98M** | Обновления ПО + стабильность |

**ВАЖНО:** Ночной лимит = **98M** (НЕ 0!) для предотвращения Tail Drop.

**Priority Queues:**
1. **1C_PRIO** (priority=1, limit-at=5M) — Бухгалтерия, касса
2. **OTHER_TRAFFIC** (priority=8) — Остальной трафик

**Scripts & Scheduler:** Аналогично DU-DEP (QoS_DAY_MODE, QoS_NIGHT_MODE, Start_Day_QoS, Start_Night_QoS)

#### VPN Connectivity

- ✅ **WireGuard активен** (Endpoint: SFA-31, 77.89.251.234:20119)
- ✅ **Ping к Hub:** 10.10.10.1 (packet-loss=0%, RTT ~4ms)
- ✅ **Ping к Zabbix:** 192.168.0.33 (packet-loss=0%, RTT ~4ms)

**Примечание:** RTT выше чем у DU-DEP (~4ms vs ~1ms) из-за большей географической удаленности.

#### Управление (команды)

```bash
# SSH подключение
ssh -p 22222 -i ~/.ssh/id_ed25519_mikrotik sysadmin-bot@10.10.10.5

# Проверка QoS режима
/queue tree print where name="TOTAL_DOWNLOAD"

# Просмотр логов переключения
/log print where message~"QoS"

# Ручное переключение
/system script run QoS_DAY_MODE
/system script run QoS_NIGHT_MODE

# Проверка VPN
/interface wireguard peers print
ping 10.10.10.1 count=3
```

#### Мониторинг

- **Zabbix Host:** OTTO-16 (SNMP + ICMP)
- **Firewall:** ✅ Zabbix (192.168.0.33) имеет доступ
- **Важные метрики:** CPU Load, WiFi Clients, Queue Stats, VPN Uptime

---

## 3. АКТУАЛЬНЫЙ СПИСОК УЗЛОВ (AI NODES)

*Статус на 18.01.2026: Linux-узлы активны. Windows-узлы ожидают настройки.*

### 🟢 Linux Servers (Debian/MX-Linux + SysVinit)

| Hostname | IP | Роль | Статус Director |
|----------|----|------|-----------------|
| **sfa-mng** | 192.168.0.35 | **AI Director Core**, Docker, Portainer, Gateway (8317), Chat (5555) | ✅ Master Node |
| **zbxglpi-pvl** | 192.168.0.33 | GLPI Helpdesk, Zabbix Monitoring | ✅ Managed |
| **pvl-cloud** | 192.168.0.25 | File Server, Nextcloud | ✅ Managed |
| **nas** | 192.168.10.10 | Сетевое хранилище (Траян-11) | ✅ Managed |
| **fr-sw** | 192.168.3.7 | Сервер филиала (Франко-38) | ✅ Managed |

### 🔴 Windows Servers (Windows 10 Enterprise LTSC 2021)

| Hostname | IP | Роль | Статус Director |
|----------|----|------|-----------------|
| **sfa-aam** | 192.168.0.21 | Рабочая станция / Офис | ⏳ Agent Pending |
| **sfa-data** | 192.168.10.21 | Сервер данных / Траян-11 | ⏳ Agent Pending |

---

## 4. ПРОЕКТ "DIRECTOR AI CONSOLE"

**Описание:**
Веб-интерфейс для управления инфраструктурой через естественный язык (Shell-GPT).

**Архитектура:**
`[Браузер] <-> [Flask:5555] <-> [AI Orchestrator] <-> [SSH Manager] <-> [Linux Nodes]`

### 🔌 AI Gateway Configuration (Порт 8317)
Основной шлюз с полным ("сырым") списком моделей. Используется серверами.
- **Endpoint:** `http://192.168.0.35:8317/v1`
- **Auth Header:** `Authorization: Bearer sk-vibecoding-secret`

### 🧹 LiteLLM Proxy (Порт 8400)
"Чистый" шлюз для IDE (VS Code, Roo Code). Фильтрует и переименовывает модели.
- **Endpoint:** `http://192.168.0.35:8400/v1`
- **Container:** `ai-cleaner` (Docker)
- **Config:** `/home/maimik/Projects/director/litellm/clean_models.yaml`

**Алиасы моделей (для VS Code):**
- `AG_Claude_Sonnet_4_5` (Antigravity)
- `DS_DeepSeek_V3` (DeepSeek Chat)
- `DS_DeepSeek_Coder` (DeepSeek Coder)
- `ANT_Claude_3_5_Sonnet` (Anthropic)
- `OAI_GPT_5_1` (OpenAI)

---

## 5. ТЕХНИЧЕСКИЕ РЕШЕНИЯ (TROUBLESHOOTING LOG)

### Проблема 1: Зависание команд и "Мусор" в выводе
**Симптомы:** Команда висит до таймаута или выдает ANSI-коды.
**Решение:**
1. Отключить PTY: `get_pty=False`.
2. Принудительно закрыть ввод: `stdin.close()` (критично для sgpt).

### Проблема 2: Копирование текста (Copy Button)
**Симптомы:** API `navigator.clipboard` не работает по HTTP.
**Решение:** Использование fallback-метода через `document.execCommand('copy')`.

---

## 6. БЫСТРЫЕ ССЫЛКИ

| Сервис | URL | Описание |
|--------|-----|----------|
| **Director Console** | http://192.168.0.35:5555/ai-console | AI-управление |
| **GLPI Helpdesk** | http://192.168.0.33/glpi | Заявки |
| **Zabbix** | http://192.168.0.33/zabbix | Мониторинг |
| **Portainer** | http://192.168.0.35:9000 | Docker UI |
