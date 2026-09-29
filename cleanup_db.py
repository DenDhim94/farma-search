#!/usr/bin/env python3
"""Очистка БД ФармА-Поиск: удаление старых броней и устаревших остатков.

Использование:
    python cleanup_db.py --dry-run              # посмотреть, что удалится
    python cleanup_db.py                         # очистка по умолчанию (30/14 дней)
    python cleanup_db.py --bookings-days 7 --stock-days 3  # агрессивная очистка

Можно добавить в cron:
    0 3 * * * cd /path/to/project && python cleanup_db.py >> cleanup.log 2>&1
"""
import argparse
import sqlite3
from datetime import datetime, timedelta

DB_PATH = "apteka.db"

def main():
    parser = argparse.ArgumentParser(description="Очистка БД ФармА-Поиск")
    parser.add_argument("--dry-run", action="store_true", help="Показать, что будет удалено, без удаления")
    parser.add_argument("--bookings-days", type=int, default=30, help="Удалить брони старше N дней (default: 30)")
    parser.add_argument("--stock-days", type=int, default=14, help="Удалить остатки, не обновлявшиеся N дней (default: 14)")
    parser.add_argument("--db", default=DB_PATH, help="Путь к БД (default: apteka.db)")
    args = parser.parse_args()

    now = datetime.now()
    bookings_threshold = (now - timedelta(days=args.bookings_days)).strftime("%Y-%m-%d %H:%M:%S")
    stock_threshold = (now - timedelta(days=args.stock_days)).strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Подсчёт
    bookings_count = conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE status IN ('done', 'cancelled', 'expired') AND created_at < ?",
        (bookings_threshold,)
    ).fetchone()[0]
    stock_count = conn.execute(
        "SELECT COUNT(*) FROM excel_stock WHERE updated_at < ?",
        (stock_threshold,)
    ).fetchone()[0]

    # Размер БД до
    db_size_before = conn.execute("PRAGMA page_count").fetchone()[0] * 4096

    print(f"{'=' * 50}")
    print(f"  Очистка БД ФармА-Поиск")
    print(f"{'=' * 50}")
    print(f"  База данных:     {args.db}")
    print(f"  Размер БД:       {db_size_before / 1024 / 1024:.1f} МБ")
    print(f"  Порог броней:    {args.bookings_days} дней (до {bookings_threshold})")
    print(f"  Порог остатков:  {args.stock_days} дней (до {stock_threshold})")
    print(f"  Режим:           {'ПРОСМОТР (dry-run)' if args.dry_run else 'УДАЛЕНИЕ'}")
    print(f"{'-' * 50}")
    print(f"  Броней к удалению: {bookings_count}")
    print(f"  Остатков к удалению: {stock_count}")
    print(f"{'=' * 50}")

    if args.dry_run:
        print("\n  Это пробный запуск. Данные не удалены.")
        print("  Для удаления запустите без --dry-run")
    else:
        conn.execute(
            "DELETE FROM bookings WHERE status IN ('done', 'cancelled', 'expired') AND created_at < ?",
            (bookings_threshold,)
        )
        conn.execute("DELETE FROM excel_stock WHERE updated_at < ?", (stock_threshold,))
        conn.execute("VACUUM")
        conn.commit()
        db_size_after = conn.execute("PRAGMA page_count").fetchone()[0] * 4096
        print(f"\n  ✅ Удалено: {bookings_count} броней, {stock_count} остатков")
        print(f"  Размер БД после: {db_size_after / 1024 / 1024:.1f} МБ")

    conn.close()

if __name__ == "__main__":
    main()
