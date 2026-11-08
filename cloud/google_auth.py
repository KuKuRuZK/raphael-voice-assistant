"""
Ключі Google для серверної Лін. Запускати вдома:

    python -m cloud.google_auth 1     основна скринька → gmail_token_server.json
    python -m cloud.google_auth 2     друга скринька   → gmail_token_server2.json

Відкриється браузер, Влад сам вибирає акаунт і тисне «Дозволити».

Права: gmail.modify (читати й позначати прочитаним, мітки, архів, кошик) і читання
календаря. Домашні ключі Лін ще й надсилають листи (gmail.compose) і міняють фільтри
(gmail.settings.basic); на сервері, де поруч живе тестовий WordPress, таким не місце.
З цими, навіть якщо сервер зламають, від імені Влада нічого не надіслати і назавжди
нічого не видалити (кошик відновлюється 30 днів). Спершу (2026-11-08 вранці) ключі були
лише на читання, а позначати прочитаним Влад попросив того ж дня.
"""
import os
import sys

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
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
    print(f"Ключ серверної Лін (читати й позначати прочитаним, без надсилання) для "
          f"{'другої' if which == '2' else 'основної'} скриньки → {out}")
    flow = InstalledAppFlow.from_client_secrets_file(credentials, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent",
                                  authorization_prompt_message="Відкриваю браузер: вибери акаунт і натисни «Дозволити».",
                                  success_message="Готово, вкладку можна закрити.")
    with open(out, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print(f"[OK] {out}")


if __name__ == "__main__":
    main()
