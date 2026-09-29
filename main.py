# ──────────────────────────────────────────────────────────────────────
# ФармА-Поиск v9.0 — бэкенд
# Изменения v9.0:
#   • WAL-режим SQLite (параллельные чтение/запись)
#   • executemany() для merge_stock (батчи по 1000)
#   • auto_expire — один SQL UPDATE вместо цикла
#   • Пароли из переменных окружения
#   • Маршрут /admin (отдельная админка)
#   • Эндпоинт /api/cleanup (очистка БД)
#   • Переименование: ФармПоиск → ФармА-Поиск
#   • PRAGMA: cache_size 64МБ, temp_store=MEMORY, mmap_size 256МБ
#   • enrich_pharmacy_info — только при отсутствии JOIN-данных
# ──────────────────────────────────────────────────────────────────────

import os
import sqlite3
import json
import re
import uuid
import time
import logging
import asyncio
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from openpyxl import load_workbook

# ── Логирование ──
logger = logging.getLogger("farmapoisk")
logger.setLevel(logging.DEBUG)
_file_handler = RotatingFileHandler("app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S"))
logger.addHandler(_file_handler)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S"))
logger.addHandler(_console_handler)

# ── Конфигурация ──
DB_PATH = "apteka.db"

# Пароли: из env или значения по умолчанию
STAFF_PASSWORD = os.environ.get("STAFF_PASSWORD", "159753!")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin2026!")

API_BASE_URL = "https://hsvc.oasis38.ru/farmtech/v1"
TOKEN_CACHE_FILE = "token_cache.json"
NIRON_CONFIG_FILE = "niron_config.json"

# ── Redis ──
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_TOKEN_KEY = "niron:access_token"
REDIS_TOKEN_EXPIRES_KEY = "niron:token_expires_at"
REDIS_SEARCH_PREFIX = "search:"

_redis_client = None

def get_redis():
    global _redis_client
    if _redis_client is not False and _redis_client is not None:
        return _redis_client
    try:
        import redis
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
        _redis_client.ping()
        logger.info("Redis подключен: %s", REDIS_URL)
        return _redis_client
    except ImportError:
        logger.warning("Модуль redis не установлен — кэш в файловом режиме")
        _redis_client = False
        return None
    except Exception as e:
        logger.warning("Redis недоступен (%s) — кэш в файловом режиме", e)
        _redis_client = False
        return None

# ── Конфигурация NiRON (через файл) ──
def load_niron_config():
    try:
        if os.path.exists(NIRON_CONFIG_FILE):
            with open(NIRON_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                return cfg.get("client_id", ""), cfg.get("client_secret", "")
    except Exception as e:
        logger.warning("Не удалось прочитать niron_config.json: %s", e)
    return "", ""

def save_niron_config(client_id, client_secret):
    with open(NIRON_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"client_id": client_id, "client_secret": client_secret}, f, indent=2, ensure_ascii=False)
    _delete_token_file(TOKEN_CACHE_FILE)
    r = get_redis()
    if r:
        try:
            r.delete(REDIS_TOKEN_KEY, REDIS_TOKEN_EXPIRES_KEY)
        except:
            pass
    logger.info("Конфигурация NiRON обновлена, кэш токена сброшен")

# ── Данные аптек ──
PHARMACIES_DATA = [
    {"code": 3,  "name": "Академическая 31 (ТЦ «Версаль»)",     "firm": "ФармТехнологии ООО", "address": "Иркутск, Академическая, д.31",                     "phone": "8-924-834-00-48",     "hours": "с 900 до 2000"},
    {"code": 10, "name": "Лыткина 29",                          "firm": "ФармТехнологии ООО", "address": "Иркутск, Лыткина, д.29",                           "phone": "8-924-834-00-49",     "hours": "с 900 до 2000"},
    {"code": 14, "name": "Радужный 115",                        "firm": "ФармТехнологии ООО", "address": "Иркутск, Радужный, д.115",                         "phone": "8-924-834-00-50",     "hours": "с 900 до 2000"},
    {"code": 20, "name": "Депутатская 80",                       "firm": "ФармТехнологии ООО", "address": "Иркутск, Депутатская, д.80",                       "phone": "8-924-834-00-51",     "hours": "круглосуточно"},
    {"code": 25, "name": "Академическая 16/1",                   "firm": "ФармТехнологии ООО", "address": "Иркутск, Академическая, д.16/1",                   "phone": "8-924-834-00-52",     "hours": "с 900 до 2000"},
    {"code": 30, "name": "Сухэ-Батора 14",                       "firm": "ФармТехнологии ООО", "address": "Иркутск, Сухэ-Батора, д.14",                     "phone": "8-924-834-00-53",     "hours": "с 900 до 2000"},
    {"code": 35, "name": "Рабочая 1",                             "firm": "ФармТехнологии ООО", "address": "Иркутск, Рабочая, д.1",                           "phone": "8-924-834-00-54",     "hours": "с 900 до 2000"},
    {"code": 40, "name": "Улица Рождественского 1",               "firm": "Эллада ООО",         "address": "Иркутск, ул. Рождественского, д.1",             "phone": "8-950-135-64-29",     "hours": "с 900 до 2000"},
]

# ── БД с WAL и PRAGMA ──
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # PRAGMA для производительности
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")      # 64 МБ кэш в RAM
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")     # 256 МБ memory-mapped I/O
    return conn

# ── Валидация входных данных ──
def validate_search_query(q):
    if not q or not q.strip():
        return False, "Пустой поисковый запрос"
    raw = q.strip()
    if len(raw) > 200:
        return False, "Слишком длинный запрос (максимум 200 символов)"
    if '\x00' in raw:
        return False, "Запрос содержит недопустимые символы"
    dangerous = set("'\";\\")
    if any(c in raw for c in dangerous):
        return False, "Запрос содержит недопустимые символы"
    return True, ""

def normalize_phone(phone):
    if not phone or not isinstance(phone, str):
        return None, False
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 11 and digits[0] in ('7', '8'):
        digits = '7' + digits[1:]
    elif len(digits) == 10:
        digits = '7' + digits
    else:
        return None, False
    if len(digits) != 11 or digits[0] != '7':
        return None, False
    return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}", True

def validate_booking_id(booking_id):
    if not booking_id or not isinstance(booking_id, str):
        return False
    return bool(re.match(r'^BR-[A-Za-z0-9]{1,8}$', booking_id))

def validate_status(status):
    return status in ("active", "ready", "done", "cancelled", "expired")

def validate_role(role):
    return role in ("staff", "admin")

# ── Утилиты дат ──
def parse_expiry(raw):
    if not raw:
        return None
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def format_expiry(raw):
    dt = parse_expiry(raw)
    if dt:
        return dt.strftime("%d.%m.%Y")
    return str(raw).strip() if raw else ""

def is_short_expiry(raw):
    dt = parse_expiry(raw)
    if not dt:
        return False
    now = datetime.now()
    delta = dt - now
    return timedelta(0) <= delta <= timedelta(days=60)

# ── Подстановка адресов (только fallback при отсутствии JOIN) ──
def enrich_pharmacy_info(dep_code, dep_name):
    code_str = str(dep_code).strip() if dep_code else ""
    name_str = (dep_name or "").strip()
    if code_str:
        for p in PHARMACIES_DATA:
            if str(p["code"]) == code_str:
                return p["name"], p["address"], p["phone"]
    if not name_str:
        return dep_name or "", "", ""
    name_lower = name_str.lower()
    for p in PHARMACIES_DATA:
        if p["name"].lower() == name_lower:
            return p["name"], p["address"], p["phone"]
    for p in PHARMACIES_DATA:
        pn_lower = p["name"].lower()
        if name_lower in pn_lower or pn_lower in name_lower:
            return p["name"], p["address"], p["phone"]
    return name_str, "", ""

# ── Merge stock (v9: executemany батчами) ──
MERGE_BATCH_SIZE = 1000

def merge_stock(conn, items, source_name):
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Загружаем существующие записи в словарь
    existing = {}
    for row in conn.execute("SELECT id, dep_code, dep_name, product_name, qty FROM excel_stock").fetchall():
        key = (str(row["dep_code"] or "").strip(), (row["dep_name"] or "").strip(), (row["product_name"] or "").strip())
        existing[key] = dict(row)

    incoming_keys = set()
    to_insert = []
    to_update = []
    inserted = 0
    updated = 0

    for item in items:
        key = (str(item["dep_code"]).strip(), item["dep_name"].strip(), item["product_name"].strip())
        incoming_keys.add(key)
        if key in existing:
            ex = existing[key]
            if abs(float(ex["qty"]) - float(item["qty"])) < 0.001:
                continue
            to_update.append((
                item["qty"], item["price"], item["expiry"], source_name, now_ts, ex["id"]
            ))
            updated += 1
        else:
            to_insert.append((
                item["dep_code"], item["dep_name"], item["product_name"], item["product_name"].lower(),
                item["qty"], item["price"], item["expiry"], source_name, now_ts
            ))
            inserted += 1

    # Батчевая вставка
    for i in range(0, len(to_insert), MERGE_BATCH_SIZE):
        conn.executemany(
            "INSERT INTO excel_stock (dep_code, dep_name, product_name, product_name_lower, qty, price, expiry, source, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            to_insert[i:i + MERGE_BATCH_SIZE]
        )

    # Батчевое обновление
    for i in range(0, len(to_update), MERGE_BATCH_SIZE):
        conn.executemany(
            "UPDATE excel_stock SET qty=?, price=?, expiry=?, source=?, updated_at=? WHERE id=?",
            to_update[i:i + MERGE_BATCH_SIZE]
        )

    # Батчевое удаление устаревших
    to_delete = [(ex["id"],) for key, ex in existing.items() if key not in incoming_keys]
    for i in range(0, len(to_delete), MERGE_BATCH_SIZE):
        conn.executemany("DELETE FROM excel_stock WHERE id=?", to_delete[i:i + MERGE_BATCH_SIZE])
    deleted = len(to_delete)

    return inserted, updated, deleted

# ── Умный поиск: синонимы ──
SYNONYMS = {
    "панадол": "парацетамол", "эффералган": "парацетамол", "цефекон": "парацетамол",
    "нурофен": "ибупрофен", "ибупром": "ибупрофен", "миг": "ибупрофен", "адвил": "ибупрофен",
    "аспирин": "ацетилсалициловая", "кардиомагнил": "ацетилсалициловая", "тромбоасс": "ацетилсалициловая",
    "ношпа": "дротаверин", "но-шпа": "дротаверин", "спазмалгон": "дротаверин",
    "супрастин": "хлоропирамин", "зиртек": "цетиризин", "цетрин": "цетиризин",
    "лоратадин": "кларитин", "кларитин": "лоратадин",
    "омез": "омепразол", "омепразол": "омепразол", "нольпаза": "пантопразол",
    "мезим": "панкреатин", "креон": "панкреатин", "фестал": "панкреатин",
    "эспумизан": "симетикон", "боботик": "симетикон",
    "линекс": "лактобактерии", "бак-сет": "лактобактерии",
    "аквамарис": "морская вода", "аквалор": "морская вода",
    "витрум": "витамин", "супрадин": "витамин", "компливит": "витамин",
    "магне": "магний", "магний": "магний",
    "энтерол": "сахаромицеты", "энтерофурил": "нифуроксазид",
    "регидрон": "электролиты", "хумана": "электролиты",
    "фрутоняня": "фруто", "фруто": "фруто",
}

def normalize_query(q):
    return re.sub(r'\s+', ' ', q.strip().lower())

def apply_synonyms(q):
    words = q.split()
    expanded = []
    changed = False
    original_brand = ""
    for w in words:
        if w in SYNONYMS:
            expanded.append(SYNONYMS[w])
            changed = True
            if not original_brand:
                original_brand = w
        else:
            expanded.append(w)
    if changed:
        return " ".join(expanded), True, original_brand
    return q, False, ""

def simple_stem(word):
    suffixes = ["ами", "ями", "ах", "ях", "ов", "ев", "ие", "ые", "ая", "яя", "ой", "ей",
                "ую", "юю", "ого", "его", "ом", "ем", "ах", "ях", "ам", "ям", "ы", "и",
                "а", "я", "о", "е", "у", "ю", "ь", "й"]
    for suf in sorted(suffixes, key=len, reverse=True):
        if len(word) > len(suf) + 3 and word.endswith(suf):
            return word[:-len(suf)]
    return word

def search_rank(product_name_lower, query_terms, original_query):
    if original_query in product_name_lower:
        return 100
    if product_name_lower.startswith(original_query):
        return 90
    score = 0
    for term in query_terms:
        if term in product_name_lower:
            score += 20
        else:
            stemmed = simple_stem(term)
            if len(stemmed) >= 4 and stemmed in product_name_lower:
                score += 15
            else:
                ratio = SequenceMatcher(None, term, product_name_lower).ratio()
                if ratio > 0.6:
                    score += int(ratio * 20)
    return min(score, 80)

# ── Кэш поиска ──
SEARCH_CACHE = {}
CACHE_TTL = 60
CACHE_MAX = 200

def get_search_cache(key):
    r = get_redis()
    if r:
        try:
            val = r.get(REDIS_SEARCH_PREFIX + key)
            if val:
                return json.loads(val)
        except:
            pass
    entry = SEARCH_CACHE.get(key)
    if entry:
        if time.time() - entry["ts"] < CACHE_TTL:
            return entry["data"]
        del SEARCH_CACHE[key]
    return None

def set_search_cache(key, data):
    r = get_redis()
    if r:
        try:
            r.setex(REDIS_SEARCH_PREFIX + key, CACHE_TTL, json.dumps(data, ensure_ascii=False))
            return
        except:
            pass
    if len(SEARCH_CACHE) >= CACHE_MAX:
        oldest = min(SEARCH_CACHE, key=lambda k: SEARCH_CACHE[k]["ts"])
        del SEARCH_CACHE[oldest]
    SEARCH_CACHE[key] = {"data": data, "ts": time.time()}

# ── API NiRON: токен ──
TOKEN_TTL = 86400
TOKEN_BUFFER = 30

def _save_token_file(path, token, expires_at):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "access_token": token,
                "expires_at": expires_at,
                "expires_at_iso": datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M:%S"),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning("Не удалось сохранить токен в файл: %s", e)

def _load_token_file(path):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("access_token", ""), int(data.get("expires_at", 0))
    except Exception:
        pass
    return "", 0

def _delete_token_file(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

def get_api_token():
    r = get_redis()
    client_id, client_secret = load_niron_config()
    if not client_id or not client_secret:
        return None, "NiRON не настроен: укажите Client ID и Client Secret в настройках"

    # 1. Проверяем Redis
    if r:
        try:
            token = r.get(REDIS_TOKEN_KEY)
            expires = r.get(REDIS_TOKEN_EXPIRES_KEY)
            if token and expires:
                expires_int = int(expires)
                if expires_int - TOKEN_BUFFER > int(time.time()):
                    return token, None
        except:
            pass

    # 2. Проверяем файл
    token, expires = _load_token_file(TOKEN_CACHE_FILE)
    if token and expires - TOKEN_BUFFER > int(time.time()):
        if r:
            try:
                r.set(REDIS_TOKEN_KEY, token)
                r.set(REDIS_TOKEN_EXPIRES_KEY, expires)
            except:
                pass
        return token, None

    # 3. Запрашиваем новый токен
    import requests
    try:
        resp = requests.post(
            API_BASE_URL + "/auth/token",
            json={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
            timeout=15
        )
        if resp.status_code != 200:
            return None, f"Ошибка аутентификации NiRON: {resp.status_code}"
        data = resp.json()
        token = data.get("access_token", "")
        expires_in = int(data.get("expires_in", TOKEN_TTL))
        expires_at = int(time.time()) + expires_in

        # Сохраняем
        _save_token_file(TOKEN_CACHE_FILE, token, expires_at)
        if r:
            try:
                r.set(REDIS_TOKEN_KEY, token)
                r.set(REDIS_TOKEN_EXPIRES_KEY, expires_at)
            except:
                pass

        logger.info("Получен новый токен NiRON, истекает: %s", datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M:%S"))
        return token, None
    except Exception as e:
        _delete_token_file(TOKEN_CACHE_FILE)
        if r:
            try:
                r.delete(REDIS_TOKEN_KEY, REDIS_TOKEN_EXPIRES_KEY)
            except:
                pass
        return None, str(e)

# ── API NiRON: загрузка остатков ──
def fetch_stock_from_api():
    import requests
    token, err = get_api_token()
    if err:
        return 0, err, None

    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(API_BASE_URL + "/dict/departments", headers=headers, timeout=15)
        if resp.status_code != 200:
            return 0, f"Departments error {resp.status_code}", resp.status_code

        deps = resp.json()
        all_items = []
        for dep in deps:
            dep_id = dep.get("id") or dep.get("dep_id")
            dep_name = dep.get("name") or dep.get("dep_name", "")
            cursor = None
            while True:
                params = {"dep_id": dep_id, "limit": 500}
                if cursor:
                    params["cursor"] = cursor
                r = requests.get(API_BASE_URL + "/stock", headers=headers, params=params, timeout=30)
                if r.status_code != 200:
                    break
                data = r.json()
                for item in data.get("items", []):
                    if item.get("deleted"):
                        continue
                    qty = float(item.get("qty", 0))
                    if qty < 0:
                        qty = 0
                    price = float(item.get("price", 0))
                    expiry = format_expiry(item.get("expires", ""))
                    name = item.get("name", "")
                    if not name:
                        continue
                    all_items.append({
                        "dep_code": str(dep_id),
                        "dep_name": dep_name,
                        "product_name": name,
                        "qty": qty,
                        "price": price,
                        "expiry": expiry
                    })
                cursor = data.get("next_cursor")
                if not cursor:
                    break

        conn = get_db()
        inserted, updated, deleted = merge_stock(conn, all_items, "api")
        conn.commit()
        conn.close()
        return len(all_items), None, None

    except Exception as e:
        return 0, str(e), None

# ── Инициализация БД (идемпотентная) ──
def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS excel_stock (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dep_code TEXT, dep_name TEXT, product_name TEXT,
        product_name_lower TEXT, qty REAL, price REAL, expiry TEXT,
        source TEXT DEFAULT 'demo',
        updated_at TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS bookings (
        id TEXT PRIMARY KEY,
        items_json TEXT, total_price REAL,
        client_name TEXT, client_phone TEXT,
        dep_name TEXT, source TEXT, status TEXT, created_at TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS pharmacies (
        code INTEGER PRIMARY KEY, name TEXT, firm TEXT,
        address TEXT, phone TEXT, hours TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS upload_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name TEXT, source TEXT, records_loaded INTEGER,
        records_skipped INTEGER, created_at TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS api_fetch_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        success INTEGER, error_code TEXT, error_msg TEXT,
        records_count INTEGER, created_at TEXT
    )""")

    # Индексы
    c.execute("CREATE INDEX IF NOT EXISTS idx_stock_name_lower ON excel_stock(product_name_lower)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_stock_dep_code ON excel_stock(dep_code)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_stock_price ON excel_stock(price)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_stock_updated ON excel_stock(updated_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bookings_created ON bookings(created_at)")

    # Демо-данные только если таблицы пустые
    ph_count = c.execute("SELECT COUNT(*) FROM pharmacies").fetchone()[0]
    if ph_count == 0:
        for p in PHARMACIES_DATA:
            c.execute("INSERT INTO pharmacies VALUES (?,?,?,?,?,?)",
                      (p["code"], p["name"], p["firm"], p["address"], p["phone"], p["hours"]))
        logger.info("Загружено %d аптек из справочника", len(PHARMACIES_DATA))

    bk_count = c.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    if bk_count == 0:
        demo_bookings = [
            {"id": "BR-001", "items_json": json.dumps([{"name":"Витамин C 500мг","qty":2,"price":120}]), "total_price": 240, "client_name":"Иванова Анна","client_phone":"+7 (902) 111-22-33","dep_name":"Академическая 31 (ТЦ «Версаль»)","source":"excel","status":"active","created_at":"2026-09-27 10:30"},
            {"id": "BR-002", "items_json": json.dumps([{"name":"Вода Боржоми 0.5л","qty":3,"price":80}]), "total_price": 240, "client_name":"Петров Иван","client_phone":"+7 (902) 444-55-66","dep_name":"Лыткина 29","source":"excel","status":"active","created_at":"2026-09-27 12:15"},
            {"id": "BR-003", "items_json": json.dumps([{"name":"Крем для рук","qty":1,"price":150}]), "total_price": 150, "client_name":"Сидорова Мария","client_phone":"+7 (902) 777-88-99","dep_name":"Радужный 115","source":"demo","status":"ready","created_at":"2026-09-27 14:00"},
        ]
        for b in demo_bookings:
            c.execute(
                "INSERT INTO bookings (id, items_json, total_price, client_name, client_phone, dep_name, source, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (b["id"], b["items_json"], b["total_price"], b["client_name"], b["client_phone"],
                 b["dep_name"], b["source"], b["status"], b["created_at"])
            )
        logger.info("Загружено %d демо-броней", len(demo_bookings))

    st_count = c.execute("SELECT COUNT(*) FROM excel_stock").fetchone()[0]
    if st_count == 0:
        demo_stock = [
            ("3", "Академическая 31", "Витамин C 500мг №10", 5, 120, "01.06.2027", "demo"),
            ("3", "Академическая 31", "Парацетамол 500мг №20", 12, 45, "15.08.2027", "demo"),
            ("10", "Лыткина 29", "Вода Боржоми 0.5л", 30, 80, "01.12.2027", "demo"),
            ("10", "Лыткина 29", "Ибупрофен 200мг №50", 8, 95, "20.05.2027", "demo"),
        ]
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for ds in demo_stock:
            c.execute(
                "INSERT INTO excel_stock (dep_code, dep_name, product_name, product_name_lower, qty, price, expiry, source, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ds[0], ds[1], ds[2], ds[2].lower(), ds[3], ds[4], ds[5], ds[6], now_ts)
            )
        logger.info("Загружено %d демо-остатков", len(demo_stock))

    conn.commit()
    conn.close()

# ── Фоновое автоистечение броней (v9: один SQL UPDATE) ──
async def auto_expire_bookings():
    while True:
        await asyncio.sleep(3600)
        try:
            conn = get_db()
            threshold = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.execute(
                "UPDATE bookings SET status = 'expired' WHERE status = 'active' AND created_at < ?",
                (threshold,)
            )
            expired_count = cur.rowcount
            if expired_count:
                conn.commit()
                logger.info("Автоистечение: %d броней переведены в 'expired'", expired_count)
            conn.close()
        except Exception as e:
            logger.error("Ошибка автоистечения броней: %s", e)

# ── Lifespan ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("=" * 50)
    logger.info("ФармА-Поиск v9.0 запущен")
    logger.info("  Главная:  http://127.0.0.1:8000")
    logger.info("  Персонал: http://127.0.0.1:8000/staff")
    logger.info("  Админ:    http://127.0.0.1:8000/admin")
    logger.info("  Аптеки:   http://127.0.0.1:8000/pharmacies")
    logger.info("  Redis:    %s", REDIS_URL)
    get_redis()
    asyncio.create_task(auto_expire_bookings())
    logger.info("=" * 50)
    yield

app = FastAPI(lifespan=lifespan)

# ── Статика ──
@app.get("/style.css")
async def get_css():
    return FileResponse("style.css", media_type="text/css")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/staff", response_class=HTMLResponse)
async def read_staff():
    with open("staff.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/admin", response_class=HTMLResponse)
async def read_admin():
    with open("admin.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/pharmacies", response_class=HTMLResponse)
async def read_pharmacies():
    with open("pharmacies.html", "r", encoding="utf-8") as f:
        return f.read()

# ── 404 ──
@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    if os.path.exists("404.html"):
        with open("404.html", "r", encoding="utf-8") as f:
            return HTMLResponse(f.read(), status_code=404)
    return HTMLResponse("<h1>404 — Страница не найдена</h1>", status_code=404)

# ── API вход ──
@app.post("/api/login")
async def login(request: Request):
    data = await request.json()
    pwd = data.get("password", "")
    role = data.get("role", "staff")
    if not validate_role(role):
        return JSONResponse({"ok": False, "error": "Недопустимая роль"}, status_code=400)
    if role == "admin":
        if pwd == ADMIN_PASSWORD:
            logger.info("Вход администратора")
            return JSONResponse({"ok": True, "role": "admin"})
        return JSONResponse({"ok": False, "error": "Неверный пароль"}, status_code=401)
    else:
        if pwd == STAFF_PASSWORD:
            logger.info("Вход персонала")
            return JSONResponse({"ok": True, "role": "staff"})
        return JSONResponse({"ok": False, "error": "Неверный пароль"}, status_code=401)

# ── API поиск ──
MAX_LIMIT = 100
DEFAULT_LIMIT = 25
MAX_TOTAL_COUNT = 500

@app.get("/api/search")
async def search(q: str = "", page: int = 1, limit: int = DEFAULT_LIMIT):
    raw_query = q.strip()

    ok, msg = validate_search_query(raw_query)
    if not ok:
        return JSONResponse({"error": True, "message": msg}, status_code=400)

    page = max(1, page)
    limit = min(max(limit, 1), MAX_LIMIT)

    normalized = normalize_query(raw_query)
    expanded, synonym_used, original_brand = apply_synonyms(normalized)
    query_terms = expanded.split()
    stemmed_terms = [simple_stem(t) for t in query_terms if len(t) > 4]

    cache_key = f"{normalized}|{page}|{limit}"
    cached = get_search_cache(cache_key)
    if cached:
        return JSONResponse(cached)

    try:
        conn = get_db()

        where_parts = []
        params = []
        for term in query_terms:
            where_parts.append("es.product_name_lower LIKE ?")
            params.append(f"%{term}%")
        for st in stemmed_terms:
            if st not in query_terms:
                where_parts.append("es.product_name_lower LIKE ?")
                params.append(f"%{st}%")

        where_sql = " AND ".join(where_parts) if where_parts else "1=1"

        count_sql = f"SELECT COUNT(*) FROM (SELECT 1 FROM excel_stock es WHERE {where_sql} LIMIT {MAX_TOTAL_COUNT})"
        total = conn.execute(count_sql, params).fetchone()[0]
        total_capped = total >= MAX_TOTAL_COUNT

        offset = (page - 1) * limit
        data_sql = f"""
            SELECT es.*, p.address, p.phone
            FROM excel_stock es
            LEFT JOIN pharmacies p ON CAST(es.dep_code AS INTEGER) = p.code
            WHERE {where_sql}
            ORDER BY es.price ASC
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(data_sql, params + [limit, offset]).fetchall()

        results = []
        for r in rows:
            exp = r["expiry"] or ""
            join_address = r["address"] if "address" in r.keys() else None
            join_phone = r["phone"] if "phone" in r.keys() else None
            # Используем enrich только если JOIN не дал результата
            if join_address:
                full_name = r["dep_name"]
                address = join_address
                phone = join_phone or ""
            else:
                full_name, address, phone = enrich_pharmacy_info(r["dep_code"], r["dep_name"])
            results.append({
                "name": r["product_name"],
                "dep_name": full_name if address else r["dep_name"],
                "dep_code": r["dep_code"],
                "address": address or "",
                "phone": phone or "",
                "price": r["price"],
                "qty": r["qty"],
                "expiry": format_expiry(exp),
                "short_expiry": is_short_expiry(exp),
                "source": r["source"] or "demo",
                "updated_at": r["updated_at"] or "",
                "rank": search_rank(r["product_name_lower"], query_terms, normalized)
            })

        conn.close()

        response = {
            "results": results,
            "query_info": {
                "original": raw_query,
                "normalized": normalized,
                "expanded": expanded,
                "synonym_used": synonym_used,
                "original_brand": original_brand,
                "total_results": total,
                "total_capped": total_capped,
                "page": page,
                "limit": limit,
                "total_pages": (total + limit - 1) // limit
            }
        }

        set_search_cache(cache_key, response)
        return JSONResponse(response)

    except sqlite3.OperationalError as e:
        logger.error("Ошибка БД при поиске '%s': %s", raw_query, e)
        return JSONResponse({"error": True, "message": "Ошибка базы данных при поиске", "detail": str(e)}, status_code=500)
    except sqlite3.DatabaseError as e:
        logger.error("БД недоступна при поиске '%s': %s", raw_query, e)
        return JSONResponse({"error": True, "message": "База данных недоступна", "detail": str(e)}, status_code=500)
    except Exception as e:
        logger.error("Внутренняя ошибка при поиске '%s': %s", raw_query, e)
        return JSONResponse({"error": True, "message": "Внутренняя ошибка сервера при поиске", "detail": str(e)}, status_code=500)

# ── API бронирование ──
@app.post("/api/book")
async def book(request: Request):
    data = await request.json()
    items = data.get("items", [])
    client_name = (data.get("client_name") or "").strip()
    client_phone_raw = data.get("client_phone", "")

    if not items:
        return JSONResponse({"ok": False, "error": "Корзина пуста"}, status_code=400)

    normalized_phone, phone_ok = normalize_phone(client_phone_raw)
    if not phone_ok:
        return JSONResponse({"ok": False, "error": "Неверный формат телефона. Используйте формат: +7 (XXX) XXX-XX-XX или 8XXXXXXXXXX"}, status_code=400)

    if not client_name or len(client_name) > 100:
        return JSONResponse({"ok": False, "error": "Укажите имя (1-100 символов)"}, status_code=400)

    warnings = []
    conn = get_db()
    valid_items = []
    for item in items:
        name = item.get("name", "")
        dep_name = item.get("dep_name", "")
        dep_code = item.get("dep_code", "")
        price = float(item.get("price", 0))
        qty = float(item.get("qty", 1))
        stock_row = conn.execute(
            "SELECT qty FROM excel_stock WHERE product_name = ? AND dep_name = ?",
            (name, dep_name)
        ).fetchone()
        stock_qty = float(stock_row["qty"]) if stock_row else None
        if qty < 0:
            warnings.append(f"Отрицательное количество для «{name}» — товар пропущен")
            continue
        if stock_qty is not None:
            if stock_qty <= 0:
                warnings.append(f"«{name}» в аптеке «{dep_name}» нет в наличии — пропущено")
                continue
            if 0 < stock_qty < 1:
                qty = stock_qty
            elif qty > stock_qty:
                warnings.append(f"«{name}» запрошено {qty}, в наличии {stock_qty} — урезано")
                qty = stock_qty
        valid_items.append({"name": name, "dep_name": dep_name, "dep_code": dep_code, "price": price, "qty": qty})

    groups = {}
    for item in valid_items:
        dep = item["dep_name"]
        if dep not in groups:
            groups[dep] = []
        groups[dep].append(item)

    bookings = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for dep_name, group_items in groups.items():
        booking_id = "BR-" + str(uuid.uuid4())[:6].upper()
        total = sum(it["price"] * it["qty"] for it in group_items)
        conn.execute(
            "INSERT INTO bookings (id, items_json, total_price, client_name, client_phone, dep_name, source, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (booking_id, json.dumps(group_items, ensure_ascii=False), total,
             client_name, normalized_phone,
             dep_name, "excel", "active", now)
        )
        bookings.append({"booking_id": booking_id, "dep_name": dep_name, "total": round(total, 2), "items_count": len(group_items)})

    conn.commit()
    conn.close()
    total_all = sum(b["total"] for b in bookings)
    logger.info("Создано %d броней для клиента '%s', всего %.1f руб.", len(bookings), client_name, total_all)
    return JSONResponse({"ok": True, "bookings": bookings, "total": round(total_all, 2), "warnings": warnings})

# ── API список броней ──
@app.get("/api/bookings")
async def get_bookings(q: str = "", dep: str = "", status: str = ""):
    conn = get_db()
    sql = "SELECT * FROM bookings WHERE 1=1"
    params = []
    if q:
        sql += " AND (id LIKE ? OR client_name LIKE ? OR CAST(total_price AS TEXT) LIKE ?)"
        qp = f"%{q}%"
        params.extend([qp, qp, qp])
    if dep and dep != "all":
        sql += " AND dep_name LIKE ?"
        params.append(f"%{dep}%")
    if status and status != "all":
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    results = []
    for r in rows:
        items = json.loads(r["items_json"]) if r["items_json"] else []
        results.append({
            "id": r["id"], "items": items, "total_price": r["total_price"],
            "client_name": r["client_name"], "client_phone": r["client_phone"],
            "dep_name": r["dep_name"], "source": r["source"], "status": r["status"],
            "created_at": r["created_at"]
        })
    conn.close()
    return JSONResponse(results)

# ── API обновление статуса ──
@app.post("/api/bookings/{booking_id}/status")
async def update_status(booking_id: str, request: Request):
    if not validate_booking_id(booking_id):
        return JSONResponse({"ok": False, "error": "Недопустимый ID брони"}, status_code=400)
    data = await request.json()
    new_status = data.get("status", "")
    if not validate_status(new_status):
        return JSONResponse({"ok": False, "error": "Недопустимый статус"}, status_code=400)
    conn = get_db()
    existing = conn.execute("SELECT id FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if not existing:
        conn.close()
        return JSONResponse({"ok": False, "error": "Бронь не найдена"}, status_code=404)
    conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (new_status, booking_id))
    conn.commit()
    conn.close()
    logger.info("Статус брони %s изменён на '%s'", booking_id, new_status)
    return JSONResponse({"ok": True})

# ── API статистика ──
@app.get("/api/stats")
async def stats():
    conn = get_db()
    total_items = conn.execute("SELECT COUNT(*) FROM excel_stock").fetchone()[0]
    total_deps = conn.execute("SELECT COUNT(DISTINCT dep_code) FROM excel_stock").fetchone()[0]
    active_bookings = conn.execute("SELECT COUNT(*) FROM bookings WHERE status = 'active'").fetchone()[0]
    total_bookings = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    total_pharmacies = conn.execute("SELECT COUNT(*) FROM pharmacies").fetchone()[0]
    conn.close()
    return JSONResponse({
        "total_items": total_items, "total_deps": total_deps,
        "active_bookings": active_bookings, "total_bookings": total_bookings,
        "total_pharmacies": total_pharmacies
    })

# ── API список аптек ──
@app.get("/api/pharmacies")
async def get_pharmacies(q: str = ""):
    conn = get_db()
    sql = "SELECT * FROM pharmacies"
    params = []
    if q:
        sql += " WHERE name LIKE ? OR address LIKE ? OR firm LIKE ?"
        qp = f"%{q}%"
        params.extend([qp, qp, qp])
    sql += " ORDER BY name"
    rows = conn.execute(sql, params).fetchall()
    results = [{"code": r["code"], "name": r["name"], "firm": r["firm"],
                "address": r["address"], "phone": r["phone"], "hours": r["hours"]} for r in rows]
    conn.close()
    return JSONResponse(results)

# ── API CRUD аптек ──
@app.post("/api/pharmacies")
async def create_pharmacy(request: Request):
    data = await request.json()
    code = data.get("code")
    name = (data.get("name") or "").strip()
    firm = (data.get("firm") or "").strip()
    address = (data.get("address") or "").strip()
    phone = (data.get("phone") or "").strip()
    hours = (data.get("hours") or "").strip()
    if not code or not name:
        return JSONResponse({"ok": False, "error": "Код и название обязательны"}, status_code=400)
    try:
        code = int(code)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Код должен быть числом"}, status_code=400)
    conn = get_db()
    existing = conn.execute("SELECT code FROM pharmacies WHERE code = ?", (code,)).fetchone()
    if existing:
        conn.close()
        return JSONResponse({"ok": False, "error": "Аптека с таким кодом уже существует"}, status_code=400)
    conn.execute("INSERT INTO pharmacies (code, name, firm, address, phone, hours) VALUES (?,?,?,?,?,?)",
                 (code, name, firm, address, phone, hours))
    conn.commit()
    conn.close()
    logger.info("Создана аптека: код=%d, имя='%s'", code, name)
    return JSONResponse({"ok": True})

@app.put("/api/pharmacies/{code}")
async def update_pharmacy(code: int, request: Request):
    data = await request.json()
    name = (data.get("name") or "").strip()
    firm = (data.get("firm") or "").strip()
    address = (data.get("address") or "").strip()
    phone = (data.get("phone") or "").strip()
    hours = (data.get("hours") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Название обязательно"}, status_code=400)
    conn = get_db()
    existing = conn.execute("SELECT code FROM pharmacies WHERE code = ?", (code,)).fetchone()
    if not existing:
        conn.close()
        return JSONResponse({"ok": False, "error": "Аптека не найдена"}, status_code=404)
    conn.execute("UPDATE pharmacies SET name=?, firm=?, address=?, phone=?, hours=? WHERE code=?",
                 (name, firm, address, phone, hours, code))
    conn.commit()
    conn.close()
    logger.info("Обновлена аптека: код=%d", code)
    return JSONResponse({"ok": True})

@app.delete("/api/pharmacies/{code}")
async def delete_pharmacy(code: int):
    conn = get_db()
    existing = conn.execute("SELECT code FROM pharmacies WHERE code = ?", (code,)).fetchone()
    if not existing:
        conn.close()
        return JSONResponse({"ok": False, "error": "Аптека не найдена"}, status_code=404)
    conn.execute("DELETE FROM pharmacies WHERE code = ?", (code,))
    conn.commit()
    conn.close()
    logger.info("Удалена аптека: код=%d", code)
    return JSONResponse({"ok": True})

# ── API конфиг NiRON ──
@app.get("/api/niron-config")
async def get_niron_config():
    client_id, client_secret = load_niron_config()
    return JSONResponse({
        "client_id": client_id,
        "configured": bool(client_id and client_secret)
    })

@app.post("/api/niron-config")
async def save_niron_config_api(request: Request):
    data = await request.json()
    client_id = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return JSONResponse({"ok": False, "error": "Client ID и Client Secret обязательны"}, status_code=400)
    save_niron_config(client_id, client_secret)
    return JSONResponse({"ok": True})

# ── API загрузка Excel ──
@app.post("/api/upload")
async def upload_excel(file: UploadFile = File(...)):
    tmp_path = "temp_upload.xlsx"
    try:
        contents = await file.read()
        with open(tmp_path, "wb") as f:
            f.write(contents)
        wb = load_workbook(tmp_path)
        ws = wb.active
        conn = get_db()
        all_items = []
        loaded = 0
        skipped = 0
        for row in ws.iter_rows(min_row=4, values_only=True):
            if not row or len(row) < 7:
                continue
            dep_code = str(row[1] or "").strip()
            dep_name = str(row[2] or "").strip()
            product_name = str(row[3] or "").strip()
            if not product_name or not dep_name:
                skipped += 1
                continue
            if "дооценка" in product_name.lower() or "уценка" in product_name.lower():
                skipped += 1
                continue
            try:
                qty = float(row[4]) if row[4] is not None else 0
            except (ValueError, TypeError):
                qty = 0
            if qty < 0:
                qty = 0
            try:
                price = float(row[5]) if row[5] is not None else 0
            except (ValueError, TypeError):
                price = 0
            expiry_raw = row[6]
            if isinstance(expiry_raw, datetime):
                expiry = expiry_raw.strftime("%d.%m.%Y")
            else:
                expiry = format_expiry(str(expiry_raw or "").strip())
            all_items.append({
                "dep_code": dep_code, "dep_name": dep_name,
                "product_name": product_name, "qty": qty, "price": price, "expiry": expiry
            })
            loaded += 1

        inserted, updated, deleted = merge_stock(conn, all_items, "excel")
        conn.execute(
            "INSERT INTO upload_history (file_name, source, records_loaded, records_skipped, created_at) VALUES (?,?,?,?,?)",
            (file.filename, "excel", loaded, skipped, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        logger.info("Excel: загружено %d, вставлено %d, обновлено %d, удалено %d, пропущено %d",
                    loaded, inserted, updated, deleted, skipped)
        return JSONResponse({
            "ok": True,
            "loaded": loaded,
            "inserted": inserted,
            "updated": updated,
            "deleted": deleted,
            "skipped": skipped
        })
    except Exception as e:
        logger.error("Ошибка загрузки Excel: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

# ── API история загрузок ──
@app.get("/api/upload/history")
async def upload_history():
    conn = get_db()
    rows = conn.execute("SELECT * FROM upload_history ORDER BY created_at DESC LIMIT 20").fetchall()
    conn.close()
    results = [{"id": r["id"], "file_name": r["file_name"], "source": r["source"],
                "records_loaded": r["records_loaded"], "records_skipped": r["records_skipped"],
                "created_at": r["created_at"]} for r in rows]
    return JSONResponse(results)

# ── API ручная выгрузка NiRON ──
@app.post("/api/fetch-niron")
async def fetch_niron():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        count, err, code = fetch_stock_from_api()
        success = err is None
        conn = get_db()
        conn.execute(
            "INSERT INTO api_fetch_log (success, error_code, error_msg, records_count, created_at) VALUES (?,?,?,?,?)",
            (1 if success else 0, str(code) if code else "", err or "", count if success else 0, now)
        )
        conn.commit()
        conn.close()
        if success:
            logger.info("NiRON: получено %d записей", count)
            return JSONResponse({"ok": True, "count": count})
        return JSONResponse({"ok": False, "error": err, "code": code or 0})
    except Exception as e:
        conn = get_db()
        conn.execute(
            "INSERT INTO api_fetch_log (success, error_code, error_msg, records_count, created_at) VALUES (?,?,?,?,?)",
            (0, "500", str(e), 0, now)
        )
        conn.commit()
        conn.close()
        logger.error("NiRON: ошибка выгрузки: %s", e)
        return JSONResponse({"ok": False, "error": str(e), "code": "500"})

# ── API статус NiRON ──
@app.get("/api/fetch-niron/status")
async def niron_status():
    conn = get_db()
    rows = conn.execute("SELECT * FROM api_fetch_log ORDER BY created_at DESC LIMIT 1").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM api_fetch_log").fetchone()[0]
    success = conn.execute("SELECT COUNT(*) FROM api_fetch_log WHERE success = 1").fetchone()[0]
    conn.close()
    if not rows:
        return JSONResponse({"last_time": None, "total_requests": 0, "success_requests": 0})
    r = rows[0]
    return JSONResponse({
        "last_time": r["created_at"],
        "last_success": bool(r["success"]),
        "last_count": r["records_count"],
        "last_error_code": r["error_code"] if not r["success"] else None,
        "last_error_msg": r["error_msg"] if not r["success"] else None,
        "total_requests": total,
        "success_requests": success
    })

# ── API очистка БД (v9: новый эндпоинт) ──

# ── API история загрузок ──
@app.get("/api/upload-history")
async def upload_history():
    conn = get_db()
    rows = conn.execute("SELECT * FROM upload_history ORDER BY created_at DESC LIMIT 10").fetchall()
    results = [{"file_name": r["file_name"], "source": r["source"],
                "records_loaded": r["records_loaded"],
                "records_skipped": r["records_skipped"],
                "created_at": r["created_at"]} for r in rows]
    conn.close()
    return JSONResponse(results)


@app.post("/api/cleanup")
async def cleanup_db(request: Request):
    data = await request.json()
    bookings_days = int(data.get("bookings_days", 30))
    stock_days = int(data.get("stock_days", 14))
    preview = data.get("preview", False)
    conn = get_db()
    now = datetime.now()
    bookings_cutoff = (now - timedelta(days=bookings_days)).strftime("%Y-%m-%d %H:%M:%S")
    stock_cutoff = (now - timedelta(days=stock_days)).strftime("%Y-%m-%d %H:%M:%S")

    if preview:
        b_count = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE status IN ('done','cancelled','expired') AND created_at < ?",
            (bookings_cutoff,)
        ).fetchone()[0]
        s_count = conn.execute(
            "SELECT COUNT(*) FROM excel_stock WHERE updated_at < ?", (stock_cutoff,)
        ).fetchone()[0]
        conn.close()
        return JSONResponse({"bookings_to_delete": b_count, "stock_to_delete": s_count})

    conn.execute(
        "DELETE FROM bookings WHERE status IN ('done','cancelled','expired') AND created_at < ?",
        (bookings_cutoff,)
    )
    b_deleted = conn.total_changes
    conn.execute("DELETE FROM excel_stock WHERE updated_at < ?", (stock_cutoff,))
    s_deleted = conn.total_changes - b_deleted
    conn.commit()
    conn.close()
    # VACUUM нельзя выполнять внутри транзакции
    conn2 = sqlite3.connect(DB_PATH)
    conn2.isolation_level = None
    conn2.execute("VACUUM")
    conn2.close()
    conn.close()
    logger.info("Очистка БД: удалено броней=%d, остатков=%d (cutoff: %s / %s)",
                b_deleted, s_deleted, bookings_cutoff, stock_cutoff)
    return JSONResponse({"bookings_deleted": b_deleted, "stock_deleted": s_deleted})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
