#!/usr/bin/env python3
"""Патч для main.py v9.0 — исправляет две ошибки:
1. VACUUM внутри транзакции (sqlite3.OperationalError)
2. Отсутствующий эндпоинт /api/upload-history (404)
"""
import re

FILE = "main.py"

with open(FILE, "r", encoding="utf-8") as f:
    code = f.read()

original = code
changes = []

# ── Фикс 1: VACUUM вне транзакции ──
fixed_vacuum = False

# Вариант A: conn.execute("VACUUM") затем conn.commit()
old_a = 'conn.execute("VACUUM")\n    conn.commit()'
new_a = 'conn.commit()\n    conn.close()\n    # VACUUM нельзя выполнять внутри транзакции\n    conn2 = sqlite3.connect(DB_PATH)\n    conn2.isolation_level = None\n    conn2.execute("VACUUM")\n    conn2.close()'

if old_a in code:
    code = code.replace(old_a, new_a, 1)
    fixed_vacuum = True
    changes.append("Фикс 1: VACUUM вынесен из транзакции (вариант A)")

if not fixed_vacuum:
    # Вариант B: только conn.execute("VACUUM") без commit рядом
    old_b = 'conn.execute("VACUUM")'
    if old_b in code and 'conn2.execute("VACUUM")' not in code:
        new_b = 'conn.commit()\n    conn.close()\n    conn2 = sqlite3.connect(DB_PATH)\n    conn2.isolation_level = None\n    conn2.execute("VACUUM")\n    conn2.close()'
        code = code.replace(old_b, new_b, 1)
        fixed_vacuum = True
        changes.append("Фикс 1: VACUUM вынесен из транзакции (вариант B)")

if not fixed_vacuum:
    if 'conn2.execute("VACUUM")' in code:
        changes.append("Фикс 1: уже применён ранее, пропускаем")
    else:
        changes.append("Фикс 1: ВНИМАНИЕ — блок VACUUM не найден, нужно ручное исправление")

# ── Фикс 2: /api/upload-history ──
if '/api/upload-history' in code:
    changes.append("Фикс 2: /api/upload-history уже присутствует, пропускаем")
else:
    endpoint_code = '\n# ── API история загрузок ──\n@app.get("/api/upload-history")\nasync def upload_history():\n    conn = get_db()\n    rows = conn.execute("SELECT * FROM upload_history ORDER BY created_at DESC LIMIT 10").fetchall()\n    results = [{"file_name": r["file_name"], "source": r["source"],\n                "records_loaded": r["records_loaded"],\n                "records_skipped": r["records_skipped"],\n                "created_at": r["created_at"]} for r in rows]\n    conn.close()\n    return JSONResponse(results)\n\n'
    
    if '/api/cleanup' in code:
        idx = code.index('@app.post("/api/cleanup")')
        code = code[:idx] + endpoint_code + '\n' + code[idx:]
        changes.append("Фикс 2: /api/upload-history добавлен перед /api/cleanup")
    elif 'uvicorn.run' in code:
        idx = code.index('uvicorn.run')
        code = code[:idx] + endpoint_code + '\n' + code[idx:]
        changes.append("Фикс 2: /api/upload-history добавлен перед uvicorn.run")
    else:
        changes.append("Фикс 2: ВНИМАНИЕ — не найдено место для вставки")

# ── Сохраняем ──
if code != original:
    with open(FILE, "w", encoding="utf-8") as f:
        f.write(code)
    print("main.py обновлён!")
    print(f"   Размер: {len(code)} символов")
    for c in changes:
        print(f"   {c}")
else:
    print("Изменений не требуется")
    for c in changes:
        print(f"   {c}")
