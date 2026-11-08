"""
Ключі Google для серверної Лін: ЛИШЕ ЧИТАННЯ пошти й календаря. Запускати вдома:

    python -m cloud.google_auth 1     основна скринька → gmail_token_server.json
    python -m cloud.google_auth 2     друга скринька   → gmail_token_server2.json

Відкриється браузер, Влад сам вибирає акаунт і тисне «Дозволити». Домашні ключі Лін
(gmail_token*.json) уміють надсилати листи, змінювати пошту й фільтри; на сервері, де
поруч живе тестовий WordPress, таким не місце. З цими, навіть якщо сервер зламають, від
імені Влада нічого не надіслати й нічого не змінити.
"""
import os
import sys

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]
ROOT = os.environ.get("LIN_HOME_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def main() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # консоль Windows у cp1251 падала на «→»
    which = sys.argv[1].strip() if len(sys.argv) > 1 else "1"
    name = "gmail_token_server2.json" if which == "2" else "gmail_token_server.json"
    credentials = os.path.join(ROOT, "gmail_credentials.json")
    out = os.path.join(ROOT, name)
    print(f"Ключ лише на читання для {'другої' if which == '2' else 'основної'} скриньки → {out}")
    flow = InstalledAppFlow.from_client_secrets_file(credentials, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent",
                                  authorization_prompt_message="Відкриваю браузер: вибери акаунт і натисни «Дозволити».",
                                  success_message="Готово, вкладку можна закрити.")
    with open(out, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print(f"[OK] {out}")


if __name__ == "__main__":
    main()
