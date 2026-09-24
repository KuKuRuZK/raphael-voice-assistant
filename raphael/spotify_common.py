"""
Спільні налаштування Spotify для lin.py і spotify_auth.py.

Раніше дозволи (scopes) були прописані в обох файлах окремо і розійшлись:
скрипт авторизації просив 4, а Рафаель вимагав 6. Токен зі скрипта не
проходив перевірку spotipy, бібліотека лізла в інтерактивний вхід і під
pythonw падала з «lost sys.stdin». Тепер список один.
"""
import os

# Токен лежить у корені проєкту, поруч із lin.py і spotify_auth.py
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REDIRECT_URI = "http://127.0.0.1:8888/callback"
TOKEN_PATH = os.path.join(SCRIPT_DIR, ".spotify_token")

SCOPES = " ".join([
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-recently-played",
    "user-library-read",
    "playlist-read-private",
    "playlist-read-collaborative",
])

# Необовʼязкові дозволи: spotify_auth.py їх просить, але без них решта працює.
# Якби «лайки» стояли в SCOPES, старий токен перестав би проходити перевірку,
# і до повторного входу зламалась би вся музика, а не лише лайки.
OPTIONAL_SCOPES = ["user-library-modify"]   # лайкнути трек / прибрати лайк
AUTH_SCOPES = " ".join(SCOPES.split() + OPTIONAL_SCOPES)


def token_has_scopes(token: dict | None) -> bool:
    """Чи дає збережений токен усі потрібні дозволи."""
    granted = set(((token or {}).get("scope") or "").split())
    return set(SCOPES.split()) <= granted
