"""
Переавторизація Gmail / Calendar для Лін.

Запуск (у звичайному терміналі, НЕ pythonw):
    python gmail_auth.py        -> основна скринька  -> gmail_token.json
    python gmail_auth.py 2      -> друга скринька    -> gmail_token2.json

Що робить:
  1. Генерує OAuth-лінк під твій client_id зі gmail_credentials.json.
  2. Відкриває браузер (і друкує лінк у консоль — можна скопіювати вручну).
  3. Ловить редірект на localhost, обмінює код на токен, зберігає токен поруч.

Перезапускати Рафаеля не треба: новий токен він підхопить сам на наступній
перевірці пошти (до двох хвилин) або щойно спитаєш про пошту чи календар.

Примітка: якщо OAuth-застосунок у Google у статусі "Testing", refresh-токен
живе лише ~7 днів (тому пошта і відвалилась). Щоб не робити це щотижня —
опублікуй застосунок: Google Auth Platform -> Audience -> Publish app.
"""
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS = os.path.join(SCRIPT_DIR, "gmail_credentials.json")

# Ті самі scopes, що очікує lin.py (_get_google_creds): пошта + чернетки + календар.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/calendar.events",
]


def main():
    which = sys.argv[1].strip() if len(sys.argv) > 1 else "1"
    token_file = "gmail_token2.json" if which == "2" else "gmail_token.json"
    token_path = os.path.join(SCRIPT_DIR, token_file)

    if not os.path.exists(CREDENTIALS):
        print(f"[X] Не знайдено {CREDENTIALS}")
        sys.exit(1)

    print(f"Авторизую скриньку -> {token_file}")
    if which == "2":
        print("УВАГА: у браузері увійди саме в ДРУГИЙ Google-акаунт (не основний).")
    print("Зараз відкриється браузер. Якщо ні - скопіюй URL з консолі вручну.\n")

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
    # access_type=offline + prompt=consent -> гарантовано видасть refresh_token
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        open_browser=True,
        authorization_prompt_message="Відкрий цей лінк для входу:\n{url}",
        success_message="Готово. Можна закрити вкладку і повернутись до Лін.",
    )

    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    print(f"\n[OK] Токен збережено: {token_path}")
    print("Перезапускати не треба: Рафаель підхопить новий токен сам за хвилину-дві.")


if __name__ == "__main__":
    main()
