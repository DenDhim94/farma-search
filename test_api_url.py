import requests

BASES = [
    "https://hsvc.oasis38.ru/farmtech/v1",
    "https://hsvc.oasis38.ru/farmtech/stapi/v1",
    "https://hsvc.oasis38.ru/farmtech/api/v1",
    "https://hsvc.oasis38.ru/farmtech",
    "https://hsvc.oasis38.ru/v1",
    "https://hsvc.oasis38.ru/api/v1",
    "https://hsvc.oasis38.ru/stapi/v1",
]

print("\n=== Поиск правильного URL API ===\n")

for base in BASES:
    url = f"{base}/auth/token"
    try:
        resp = requests.post(url, data={"grant_type": "client_credentials", "client_id": "test", "client_secret": "test"}, timeout=5)
        code = resp.status_code
        if code in (401, 400):
            print(f"\u2705 ВЕРНЫЙ URL  | {code} | {url}")
            print(f"   Используйте: API_BASE_URL = \"{base}\"")
        elif code == 404:
            print(f"\u274c 404         | {code} | {url}")
        else:
            print(f"\u26a0  Неожиданный | {code} | {url}")
    except requests.exceptions.ConnectionError:
        print(f"\u274c Нет ответа  | --- | {url}")
    except Exception as e:
        print(f"\u274c Ошибка      | {type(e).__name__} | {url}")

print("\nНужна строка с ВЕРНЫЙ URL. Вставьте в main.py вместо API_BASE_URL.\n")