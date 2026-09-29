#!/usr/bin/env python3
"""Patch for main.py v9.0: fixes VACUUM and adds upload-history"""
import os, re

MAIN_FILE = "main.py"

if not os.path.exists(MAIN_FILE):
    print("ERROR: main.py not found!")
    exit(1)

with open(MAIN_FILE, "r", encoding="utf-8") as f:
    code = f.read()

original = code
changes = []

# 1. Fix VACUUM
old_vacuum = '    conn.execute("VACUUM")\n    b_del = conn.total_changes\n    conn.close()'
new_vacuum = '    b_del = conn.total_changes\n    conn.commit()\n    conn.close()\n    _v_conn = sqlite3.connect(DB_PATH)\n    _v_conn.isolation_level = None\n    _v_conn.execute("VACUUM")\n    _v_conn.close()'

if old_vacuum in code:
    code = code.replace(old_vacuum, new_vacuum)
    changes.append("VACUUM fixed")
elif 'conn.execute("VACUUM")' in code:
    code = code.replace('conn.execute("VACUUM")', 'conn.commit()\n    conn.close()\n    _v_conn = sqlite3.connect(DB_PATH)\n    _v_conn.isolation_level = None\n    _v_conn.execute("VACUUM")\n    _v_conn.close()')
    changes.append("VACUUM fixed (alt)")
else:
    print("WARN: VACUUM not found")

# 2. Add upload-history
if "/api/upload-history" not in code:
    insert_point = code.find('if __name__')
    if insert_point == -1:
        insert_point = code.find('uvicorn.run')
    if insert_point > 0:
        endpoint = '\n@app.get("/api/upload-history")\nasync def upload_history():\n    conn = get_db()\n    rows = conn.execute("SELECT id, filename, rows_total, rows_imported, uploaded_at FROM upload_history ORDER BY id DESC LIMIT 10").fetchall()\n    conn.close()\n    return JSONResponse([{"id": r[0], "filename": r[1], "rows_total": r[2], "rows_imported": r[3], "uploaded_at": r[4]} for r in rows])\n\n'
        code = code[:insert_point] + endpoint + code[insert_point:]
        changes.append("Added /api/upload-history")
    else:
        print("WARN: no insertion point")
else:
    print("OK: upload-history exists")

# Write
if code != original:
    with open(MAIN_FILE, "w", encoding="utf-8") as f:
        f.write(code)
    print(f"\nPatched! {len(changes)} changes:")
    for c in changes:
        print(f"  - {c}")
else:
    print("No changes needed.")
