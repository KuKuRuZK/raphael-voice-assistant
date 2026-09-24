"""
Запусти цей скрипт ОДИН РАЗ щоб авторизувати Spotify.
Після успіху токен збережеться в .spotify_token і Lin буде
використовувати його автоматично без повторної авторизації.
"""
import json
import os
import sys

import spotipy
from spotipy.oauth2 import SpotifyOAuth

import spotify_common

# Ключі живуть у secrets.json, як і в lin.py. Раніше вони були зашиті прямо
# сюди, і при публікації репозиторію client_secret став би відкритим назавжди.
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
SECRETS_PATH = os.path.join(SCRIPT_DIR, "secrets.json")


def _secret(name: str) -> str:
    try:
        with open(SECRETS_PATH, encoding="utf-8") as f:
            return json.load(f).get(name, "") or os.environ.get(name, "")
    except Exception:
        return os.environ.get(name, "")


CLIENT_ID     = _secret("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = _secret("SPOTIFY_CLIENT_SECRET")

if not CLIENT_ID or not CLIENT_SECRET:
    print("Немає SPOTIFY_CLIENT_ID або SPOTIFY_CLIENT_SECRET.")
    print(f"Додай їх у {SECRETS_PATH} (зразок у secrets.example.json).")
    input("\nНатисни Enter щоб закрити...")
    sys.exit(1)

print("=" * 50)
print("  Авторизація Spotify для Лін")
print("=" * 50)
print()
print("Зараз відкриється браузер.")
print("Увійди у Spotify і натисни Agree.")
print("Після цього браузер закриється сам.")
print()

try:
    # Ті самі дозволи й той самий файл токена, що й у lin.py (spotify_common).
    # Шлях абсолютний: раніше токен падав у поточну теку, звідки запустили скрипт.
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=spotify_common.REDIRECT_URI,
        scope=spotify_common.SCOPES,
        cache_path=spotify_common.TOKEN_PATH,
        open_browser=True,
    ))

    user = sp.current_user()
    print("✅ Авторизація успішна!")
    # email приходить лише з дозволом user-read-email, якого ми не просимо
    print(f"   Акаунт: {user.get('display_name') or user.get('id', '?')}")
    print()

    devices = sp.devices().get("devices", [])
    if devices:
        print("📱 Знайдені пристрої:")
        for d in devices:
            active = " ← активний" if d["is_active"] else ""
            print(f"   - {d['name']} ({d['type']}){active}")
    else:
        print("📱 Пристроїв не знайдено (відкрий Spotify на ПК або телефоні)")

    print()
    print("✅ Токен збережено. Тепер можна запускати Lin!")

except Exception as e:
    print(f"❌ Помилка: {e}")

input("\nНатисни Enter щоб закрити...")
