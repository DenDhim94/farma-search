#!/usr/bin/env python3
"""Деплой и обновление ФармА-Поиск.

Команды:
    python deploy.py --check           # проверить целостность файлов
    python deploy.py --migrate         # применить миграции БД (WAL, FTS5, индексы)
    python deploy.py --restart         # миграции + перезапуск сервера
    python deploy.py --full            # полная проверка + миграции + перезапуск

Для systemd-сервиса:
    python deploy.py --restart --service farma-poisk
"""
import argparse
import os
import sqlite3
import subprocess
import sys

DB_PATH = "apteka.db"

# Ожидаемые файлы проекта
EXPECTED_FILES = [
    "main.py", "index.html", "staff.html", "admin.html", "pharmacies.html",
    "404.html", "style.css", "requirements.txt"
]

# Опциональные файлы
OPTIONAL_FILES = [
    "style_v8_additions.css", "cleanup_db.py", "rename_project.py",
    "deploy.py", "append_css.py", "tokens.json", "niron_config.json",
    "token_cache.json", "app.log"
]

def check_files(project_dir):
    """Проверяет наличие всех файлов проекта."""
    print("  Проверка файлов:")
    all_ok = True
    for f in EXPECTED_FILES:
        path = os.path.join(project_dir, f)
        exists = os.path.exists(path)
        status = "  ✅" if exists else "  ❌"
        print(f"    {status} {f}")
        if not exists:
            all_ok = False
    print(f"\n  Опциональные файлы:")
    for f in OPTIONAL_FILES:
        path = os.path.join(project_dir, f)
        exists = os.path.exists(path)
        if exists:
            print(f"    ✅ {f}")
    return all_ok

def migrate_db(db_path):
    """Применяет миграции к БД: WAL, FTS5, индексы, триггеры."""
    print("\n  Миграции БД:")
    if not os.path.exists(db_path):
        print("    ⚠ База данных не найдена — пропускаем")
        return

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # WAL-режим
    mode = c.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    print(f"    ✅ WAL-режим: {mode}")

    # Оптимизации
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA cache_size=-64000")
    c.execute("PRAGMA temp_store=MEMORY")
    print("    ✅ PRAGMA оптимизации применены")

    # FTS5
    try:
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS excel_stock_fts USING fts5(
            product_name, product_name_lower,
            content='excel_stock', content_rowid='id'
        )""")
        fts_count = c.execute("SELECT COUNT(*) FROM excel_stock_fts").fetchone()[0]
        stock_count = c.execute("SELECT COUNT(*) FROM excel_stock").fetchone()[0]
        if fts_count == 0 and stock_count > 0:
            c.execute("INSERT INTO excel_stock_fts(rowid, product_name, product_name_lower) SELECT id, product_name, product_name_lower FROM excel_stock")
            print(f"    ✅ FTS5 индекс построен: {stock_count} строк")
        else:
            print(f"    ✅ FTS5 индекс: {fts_count} строк")
    except Exception as e:
        print(f"    ⚠ FTS5 недоступен: {e}")

    # Триггеры FTS
    try:
        c.execute("""CREATE TRIGGER IF NOT EXISTS excel_stock_ai AFTER INSERT ON excel_stock BEGIN
            INSERT INTO excel_stock_fts(rowid, product_name, product_name_lower)
            VALUES (new.id, new.product_name, new.product_name_lower);
        END""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS excel_stock_ad AFTER DELETE ON excel_stock BEGIN
            INSERT INTO excel_stock_fts(excel_stock_fts, rowid, product_name, product_name_lower)
            VALUES('delete', old.id, old.product_name, old.product_name_lower);
        END""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS excel_stock_au AFTER UPDATE ON excel_stock BEGIN
            INSERT INTO excel_stock_fts(excel_stock_fts, rowid, product_name, product_name_lower)
            VALUES('delete', old.id, old.product_name, old.product_name_lower);
            INSERT INTO excel_stock_fts(rowid, product_name, product_name_lower)
            VALUES (new.id, new.product_name, new.product_name_lower);
        END""")
        print("    ✅ FTS5 триггеры созданы")
    except Exception as e:
        print(f"    ⚠ FTS5 триггеры: {e}")

    # Индексы
    c.execute("CREATE INDEX IF NOT EXISTS idx_stock_name_lower ON excel_stock(product_name_lower)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_stock_dep_code ON excel_stock(dep_code)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_stock_price ON excel_stock(price)")
    print("    ✅ Индексы проверены/созданы")

    conn.commit()
    conn.close()
    print("    ✅ Миграции завершены")

def restart_server(service_name=None, use_pid=False):
    """Перезапускает сервер."""
    print("\n  Перезапуск сервера:")
    if service_name:
        try:
            result = subprocess.run(
                ["systemctl", "restart", service_name],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                print(f"    ✅ Сервис '{service_name}' перезапущен")
            else:
                print(f"    ❌ Ошибка: {result.stderr}")
        except FileNotFoundError:
            print(f"    ⚠ systemctl не найден — возможно, не systemd")
        except Exception as e:
            print(f"    ❌ Ошибка: {e}")
    else:
        # Попытка через pid-файл
        pid_file = "farma-poisk.pid"
        if os.path.exists(pid_file):
            with open(pid_file) as f:
                old_pid = f.read().strip()
            try:
                os.kill(int(old_pid), 15)
                print(f"    ✅ Старый процесс (PID {old_pid}) остановлен")
            except Exception:
                pass
        # Запуск нового процесса
        try:
            proc = subprocess.Popen(
                [sys.executable, "main.py"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            with open(pid_file, "w") as f:
                f.write(str(proc.pid))
            print(f"    ✅ Сервер запущен (PID {proc.pid})")
            print(f"    📍 http://127.0.0.1:8000")
        except Exception as e:
            print(f"    ❌ Ошибка запуска: {e}")

def main():
    parser = argparse.ArgumentParser(description="Деплой ФармА-Поиск")
    parser.add_argument("--check", action="store_true", help="Проверить файлы")
    parser.add_argument("--migrate", action="store_true", help="Миграции БД")
    parser.add_argument("--restart", action="store_true", help="Миграции + перезапуск")
    parser.add_argument("--full", action="store_true", help="Полный цикл")
    parser.add_argument("--service", default=None, help="Имя systemd-сервиса")
    parser.add_argument("--db", default=DB_PATH, help="Путь к БД")
    args = parser.parse_args()

    project_dir = os.path.dirname(os.path.abspath(__file__))

    print(f"\n{'=' * 50}")
    print(f"  Деплой ФармА-Поиск")
    print(f"  Директория: {project_dir}")
    print(f"{'=' * 50}\n")

    do_check = args.check or args.full
    do_migrate = args.migrate or args.restart or args.full
    do_restart = args.restart or args.full

    if do_check:
        check_files(project_dir)

    if do_migrate:
        migrate_db(args.db)

    if do_restart:
        restart_server(args.service)

    if not any([do_check, do_migrate, do_restart]):
        print("  Использование:")
        print("    python deploy.py --check     # проверить файлы")
        print("    python deploy.py --migrate   # миграции БД")
        print("    python deploy.py --restart    # миграции + перезапуск")
        print("    python deploy.py --full       # полный цикл")

    print(f"\n{'=' * 50}\n")

if __name__ == "__main__":
    main()
