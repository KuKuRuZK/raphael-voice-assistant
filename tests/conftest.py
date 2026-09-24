import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Модулі лежать у корені репозиторію
sys.path.insert(0, REPO)


def _project_modules():
    """Усі вже імпортовані модулі Рафаеля: lin.py і пакет raphael/."""
    dirs = {REPO, os.path.join(REPO, "raphael")}
    out = []
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None) or ""
        if f and os.path.dirname(os.path.abspath(f)) in dirs:
            out.append(m)
    return out


class Env:
    """
    Доступ до імен Рафаеля незалежно від того, в якому модулі вони живуть.
    Тести не знають, де саме лежить speak чи _seen_gmail_ids: це дозволяє
    перевіряти поведінку до і після перенесення коду між модулями.
    """
    def __init__(self):
        self._saved = []
        self.spoken = []

    def get(self, name):
        found = {id(getattr(m, name)): getattr(m, name)
                 for m in _project_modules() if hasattr(m, name)}
        if not found:
            raise AttributeError(f"{name} не знайдено в жодному модулі")
        if len(found) > 1:
            raise AssertionError(f"{name} має різні значення в різних модулях")
        return next(iter(found.values()))

    def patch(self, name, value):
        hits = [m for m in _project_modules() if hasattr(m, name)]
        if not hits:
            raise AttributeError(f"{name} не знайдено в жодному модулі")
        for m in hits:
            self._saved.append((m, name, getattr(m, name)))
            setattr(m, name, value)

    def answers(self, *replies):
        """listen() повертає ці відповіді по черзі, потім ""."""
        queue = list(replies)
        self.patch("listen", lambda *a, **k: queue.pop(0) if queue else "")

    def restore(self):
        for m, name, value in reversed(self._saved):
            setattr(m, name, value)
        self._saved.clear()


@pytest.fixture(scope="session")
def lin():
    import stubs
    stubs.install()
    import lin as module
    return module


@pytest.fixture
def env(lin, tmp_path):
    e = Env()
    e.patch("speak", lambda text: e.spoken.append(text))
    e.patch("NOTES_PATH", str(tmp_path / "notes.json"))
    e.patch("MEMORY_PATH", str(tmp_path / "memory.json"))
    e.patch("_notes_cache", [])
    e.patch("_notes_cache_time", 0.0)
    yield e
    e.restore()


def fake_reply(text):
    """Відповідь у форматі OpenAI chat.completions."""
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        message=types.SimpleNamespace(content=text))])
