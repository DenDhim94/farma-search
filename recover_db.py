#!/usr/bin/env python3
"""
Восстановление повреждённой базы данных apteka.db
Запуск: python recover_db.py
"""

import sqlite3
import os
import shutil
from datetime import datetime

DB_PATH = "apteka.db"
DB_BACKUP = f"apteka.db.broken_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
DB_RECOVERED = "apteka_recovered.db"

def main():
    if not os.path.exists(DB_PATH):
        print("База данных apteka.db не найдена — создаю новую с нуля.")
        create_fresh_db()
        print("Новая база создана. Запустите main.py для инициализации.")
        return

    # 1. Бэкап повреждённого файла
    print(f"Создаём резервную копию: {DB_BACKUP}")
    shutil.copy2(DB_PATH, DB_BACKUP)

    # 2. Пытаемся восстановить через .recover
    print("Пытаемся восстановить данные...")
    recovered = False

    try:
        # Метод 1: sqlite3 .recover (доступен в Python 3.10+ через CLI)
        import subprocess
        result = subprocess.run(
            ["sqlite3", DB_PATH, ".recover"],
            capture=True, text=True, timeout=60
        )
        if result.returncode == 0 and result.stdout.strip():
            with open("recover_dump.sql", "w", encoding="utf-8") as f:
                f.write(result.stdout)
            print("Дамп восстановления сохранён в recover_dump.sql")
            recovered = True
    except Exception as e:
        print(f"Метод .recover не сработал: {e}")

    if not recovered:
        # Метод 2: читаем что можем напрямую
        print("Метод .recover недоступен, пробуем прямое чтение...")
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=DELETE")  # Сброс WAL
            
            # Проверяем, какие таблицы доступны
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            print(f"Найдено таблиц: {len(tables)}")
            
            for (table_name,) in tables:
                try:
                    count = conn.execute(f"SELECT COUNT(*) FROM [{table_name}]").fetchone()[0]
                    print(f"  {table_name}: {count} записей")
                except Exception as e:
                    print(f"  {table_name}: повреждён ({e})")
            
            conn.close()
            
            # Экспортируем что можем
            dump_sql = export_what_we_can(DB_PATH, tables)
            if dump_sql:
                with open("recover_dump.sql", "w", encoding="utf-8") as f:
                    f.write(dump_sql)
                print("Частичный дамп сохранён в recover_dump.sql")
                recovered = True
        except Exception as e:
            print(f"Прямое чтение тоже не удалось: {e}")

    # 3. Создаём новую чистую БД
    print("\nСоздаём новую базу данных...")
    if os.path.exists(DB_RECOVERED):
        os.remove(DB_RECOVERED)
    
    create_fresh_db(DB_RECOVERED)
    
    # 4. Импортируем восстановленные данные
    if recovered and os.path.exists("recover_dump.sql"):
        print("Импортируем восстановленные данные...")
        try:
            conn = sqlite3.connect(DB_RECOVERED)
            with open("recover_dump.sql", "r", encoding="utf-8") as f:
                dump = f.read()
            # Выполняем только INSERT, пропускаем CREATE (уже созданы)
            statements = dump.split(";\n")
            imported = 0
            for stmt in statements:
                stmt = stmt.strip()
                if stmt.upper().startswith("INSERT"):
                    try:
                        conn.execute(stmt)
                        imported += 1
                    except:
                        pass
            conn.commit()
            conn.close()
            print(f"Импортировано {imported} записей")
        except Exception as e:
            print(f"Импорт частично не удался: {e}")

    # 5. Заменяем повреждённую БД на восстановленную
    os.remove(DB_PATH)
    shutil.move(DB_RECOVERED, DB_PATH)
    
    # Удаляем WAL и SHM файлы (могут быть повреждены)
    for ext in ["-wal", "-shm"]:
        wal = DB_PATH + ext
        if os.path.exists(wal):
            os.remove(wal)
            print(f"Удалён файл: {wal}")
    
    print("\n✅ Восстановление завершено!")
    print(f"   Резервная копия: {DB_BACKUP}")
    print(f"   Новая БД: {DB_PATH}")
    print("   Теперь запустите: python main.py")


def export_what_we_can(db_path, tables):
    """Экспортирует доступные данные в SQL-формат."""
    lines = []
    conn = sqlite3.connect(db_path)
    
    for (table_name,) in tables:
        if table_name.startswith("sqlite_") or table_name.endswith("_fts") or "_fts_" in table_name:
            continue
        try:
            rows = conn.execute(f"SELECT * FROM [{table_name}]").fetchall()
            cols = conn.execute(f"PRAGMA table_info([{table_name}])").fetchall()
            col_names = [c[1] for c in cols]
            
            for row in rows:
                values = []
                for v in row:
                    if v is None:
                        values.append("NULL")
                    elif isinstance(v, (int, float)):
                        values.append(str(v))
                    else:
                        escaped = str(v).replace("'", "''")
                        values.append(f"'{escaped}'")
                col_list = ", ".join(col_names)
                val_list = ", ".join(values)
                lines.append(f"INSERT INTO [{table_name}] ({col_list}) VALUES ({val_list});")
        except Exception as e:
            print(f"  Пропуск таблицы {table_name}: {e}")
    
    conn.close()
    return ";\n".join(lines)


def create_fresh_db(path="apteka.db"):
    """Создаёт новую БД с правильной структурой."""
    conn = sqlite3.connect(path)
    
    # PRAGMA
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")
    
    # Таблицы
    conn.execute("""
        CREATE TABLE IF NOT EXISTS excel_stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dep_code TEXT,
            product_name TEXT NOT NULL,
            product_name_lower TEXT NOT NULL,
            manufacturer TEXT,
            price REAL,
            stock_qty REAL,
            expiry_date TEXT,
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_code TEXT UNIQUE NOT NULL,
            items_json TEXT NOT NULL,
            total_price REAL,
            customer_name TEXT,
            customer_phone TEXT,
            pharmacy_code TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            expires_at TEXT,
            updated_at TEXT
        )
    """)
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pharmacies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code INTEGER UNIQUE,
            name TEXT,
            firm TEXT,
            address TEXT,
            phone TEXT,
            hours TEXT
        )
    """)
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS upload_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            rows_total INTEGER,
            rows_imported INTEGER,
            uploaded_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT DEFAULT (datetime('now','localtime')),
            status TEXT,
            records INTEGER,
            error_code TEXT,
            error_desc TEXT
        )
    """)
    
    # Индексы
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_stock_name_lower ON excel_stock(product_name_lower)",
        "CREATE INDEX IF NOT EXISTS idx_stock_dep ON excel_stock(dep_code)",
        "CREATE INDEX IF NOT EXISTS idx_stock_updated ON excel_stock(updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_created ON bookings(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_pharmacy ON bookings(pharmacy_code)",
    ]:
        conn.execute(idx)
    
    conn.commit()
    conn.close()
    
    # Демо-данные аптек
    insert_demo_pharmacies(path)


def insert_demo_pharmacies(path):
    """Заполняет справочник аптек значениями по умолчанию."""
    pharmacies = [
        (3, "Академическая 31 (ТЦ «Версаль»)", "ФармТехнологии ООО", "Иркутск, Академическая, д.31", "8-924-834-00-48", "с 900 до 2000"),
        (5, "Розы Люксембург 206", "ФармТехнологии ООО", "Иркутск, Розы Люксембург, д.206", "8-924-834-00-49", "круглосуточно"),
        (7, "Ленина 23", "ФармТехнологии ООО", "Иркутск, Ленина, д.23", "8-924-834-00-50", "с 800 до 2200"),
    ]
    
    conn = sqlite3.connect(path)
    for p in pharmacies:
        conn.execute(
            "INSERT OR IGNORE INTO pharmacies (code, name, firm, address, phone, hours) VALUES (?,?,?,?,?,?)",
            p
        )
    conn.commit()
    conn.close()
    print(f"Добавлено {len(pharmacies)} аптек по умолчанию")


if __name__ == "__main__":
    main()
