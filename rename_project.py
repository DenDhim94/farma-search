#!/usr/bin/env python3
"""Переименование проекта: ФармПоиск → ФармА-Поиск во всех файлах.

Заменяет все варианты написания:
    ФармПоиск, Фармпоиск, фармПоиск, фармпоиск, ФАРМПОИСК
    farmsearch, FarmSearch, FARMSEARCH
    farmapoisk, FarmApoisk (если есть)

Использование:
    python rename_project.py --dry-run    # сначала посмотреть что изменится
    python rename_project.py              # применить замены
"""
import os
import re
import argparse

# Папка проекта (текущая директория)
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Расширения файлов для обработки
EXTENSIONS = {'.py', '.html', '.css', '.json', '.txt', '.md', '.js', '.cfg', '.ini', '.toml'}

# Замены (порядок важен — сначала длинные/специфичные)
REPLACEMENTS = [
    # Кибер-кириллица
    ("ФармПоиск", "ФармА-Поиск"),
    ("Фармпоиск", "ФармА-Поиск"),
    ("фармПоиск", "фармА-Поиск"),
    ("фармпоиск", "фармА-Поиск".lower()),
    ("ФАРМПОИСК", "ФАРМА-ПОИСК"),
    ("ФармПоиска", "ФармА-Поиска"),
    ("Фармпоиска", "ФармА-Поиска"),
    ("фармПоиска", "фармА-Поиска"),
    ("фармпоиска", "фармА-Поиска".lower()),
    # Латиница (если встречается в коде)
    ("farmsearch", "farma-search"),
    ("FarmSearch", "FarmA-Search"),
    ("FARMSEARCH", "FARMA-SEARCH"),
    ("farmapoisk", "farma-poisk"),
    ("FarmApoisk", "FarmA-Poisk"),
    # logger name
    ('"farmsearch"', '"farma-poisk"'),
    # Бекапы тоже
    ("ФармПоиск", "ФармА-Поиск"),  # повтор для надёжности
]

# Файлы, которые не трогаем
SKIP_FILES = {"rename_project.py", "app.log", "apteka.db", "token_cache.json",
              "niron_config.json", "tokens.json", "tokens.json.txt"}
SKIP_DIRS = {"__pycache__", ".git", "node_modules", "venv", ".venv"}

def get_replacement_pairs():
    """Возвращает уникальные пары замены."""
    seen = set()
    result = []
    for old, new in REPLACEMENTS:
        key = (old, new)
        if key not in seen:
            seen.add(key)
            result.append((old, new))
    return result

def process_file(filepath, dry_run=False):
    """Обрабатывает один файл. Возвращает (изменён, кол-во замен)."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (UnicodeDecodeError, PermissionError, FileNotFoundError):
        return False, 0

    original = content
    total_replacements = 0
    pairs = get_replacement_pairs()

    for old, new in pairs:
        count = content.count(old)
        if count > 0:
            content = content.replace(old, new)
            total_replacements += count

    if content == original:
        return False, 0

    if not dry_run:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

    return True, total_replacements

def main():
    parser = argparse.ArgumentParser(description="Переименование ФармПоиск → ФармА-Поиск")
    parser.add_argument("--dry-run", action="store_true", help="Показать изменения без записи")
    parser.add_argument("--dir", default=PROJECT_DIR, help="Директория проекта")
    args = parser.parse_args()

    print(f"{'=' * 60}")
    print(f"  Переименование: ФармПоиск → ФармА-Поиск")
    print(f"  Директория: {args.dir}")
    print(f"  Режим: {'ПРОСМОТР (dry-run)' if args.dry_run else 'ПРИМЕНЕНИЕ'}")
    print(f"{'=' * 60}\n")

    files_changed = 0
    total_replacements = 0

    for root, dirs, files in os.walk(args.dir):
        # Пропускаем служебные директории
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for filename in files:
            if filename in SKIP_FILES:
                continue

            filepath = os.path.join(root, filename)
            ext = os.path.splitext(filename)[1].lower()

            if ext not in EXTENSIONS:
                continue

            changed, count = process_file(filepath, args.dry_run)
            if changed:
                rel_path = os.path.relpath(filepath, args.dir)
                print(f"  {'📝' if args.dry_run else '✅'} {rel_path} ({count} замен)")
                files_changed += 1
                total_replacements += count

    print(f"\n{'=' * 60}")
    print(f"  Файлов изменено: {files_changed}")
    print(f"  Всего замен: {total_replacements}")
    if args.dry_run:
        print(f"\n  Это пробный запуск. Файлы не изменены.")
        print(f"  Для применения запустите без --dry-run")
    print(f"{'=' * 60}")

if __name__ == "__main__":
    main()
