"""Лін працює мишкою: крок за кроком, з підтвердженням незворотного і стопом."""
import pytest


@pytest.fixture
def agent(env, monkeypatch):
    from raphael import screen_agent as sa
    env.patch("BRAIN_LOCAL_MODEL", "lmstudio:google/gemma-4-26b-a4b-qat")
    env.patch("GAME", None)
    did = []
    for name in ("click", "doubleClick", "rightClick", "moveTo", "hotkey", "scroll"):
        monkeypatch.setattr(sa.pyautogui, name, lambda *a, _n=name, **k: did.append((_n, *a)), raising=False)
    monkeypatch.setattr(sa, "_monitor_box", lambda: (0, 0, 2000, 1000))
    monkeypatch.setattr(sa, "_shot", lambda box, crop=None: ("IMG", (crop or box)[:2] + (
        (crop or box)[2] - (crop or box)[0], (crop or box)[3] - (crop or box)[1])))
    monkeypatch.setattr(sa, "_ghost", lambda on: did.append(("ghost", on)))
    monkeypatch.setattr(sa, "_element_at", lambda x, y: ("", False))
    monkeypatch.setattr(sa, "_focused_is_password", lambda: False)
    monkeypatch.setattr(sa, "_refine", lambda target, x, y, box: (x, y))
    monkeypatch.setattr(sa.time, "sleep", lambda s: None)
    sa.did = did

    def script(*steps):
        queue = list(steps)
        monkeypatch.setattr(sa, "_ask", lambda text, image, max_tokens=300: queue.pop(0) if queue else {})
    sa.script = script
    return sa


def test_click_then_done(agent):
    agent.script({"action": "click", "target": "Оновити", "x": 500, "y": 250}, {"action": "done", "say": "Оновила."})
    assert agent.run("натисни Оновити") == "Оновила."
    assert ("moveTo", 1000, 250) in agent.did and ("click",) in agent.did
    assert agent.did[0] == ("ghost", True) and agent.did[-1] == ("ghost", False)    # тіло не на знімках


def test_risky_click_needs_yes(agent, monkeypatch):
    asked = []
    monkeypatch.setattr(agent, "_confirm", lambda q: asked.append(q) or False)
    agent.script({"action": "click", "target": "кнопка Надіслати", "x": 100, "y": 100})
    assert agent.run("відповідай на лист") == "Не натискаю, як скажеш."
    assert asked and ("click",) not in agent.did


def test_risky_name_under_cursor_counts_too(agent, monkeypatch):
    """Модель назвала ціль невинно, але під курсором «Delete»."""
    monkeypatch.setattr(agent, "_element_at", lambda x, y: ("Delete", False))
    monkeypatch.setattr(agent, "_confirm", lambda q: False)
    agent.script({"action": "click", "target": "іконка", "x": 100, "y": 100})
    assert agent.run("прибери це") == "Не натискаю, як скажеш."


def test_no_typing_into_password(agent, monkeypatch):
    monkeypatch.setattr(agent, "_focused_is_password", lambda: True)
    agent.script({"action": "type", "text": "secret"})
    assert "пароля" in agent.run("увійди")


def test_risky_keys_need_yes(agent, monkeypatch):
    monkeypatch.setattr(agent, "_confirm", lambda q: False)
    agent.script({"action": "key", "keys": "alt+f4"})
    assert agent.run("закрий") == "Не натискаю, як скажеш."
    assert not any(d[0] == "hotkey" for d in agent.did)


def test_stop_by_tap(agent, monkeypatch):
    steps = iter([{"action": "scroll", "direction": "down"}, {"action": "scroll", "direction": "down"}])

    def ask(text, image, max_tokens=300):
        agent.stop()                                  # тап посеред роботи
        return next(steps)
    monkeypatch.setattr(agent, "_ask", ask)
    assert agent.run("гортай") == "Зупинилась."
    assert not agent.running()


def test_not_in_game_and_gives_up_after_max_steps(agent, env):
    env.patch("GAME", "ARK")
    assert "граєш" in agent.run("натисни щось")
    env.patch("GAME", None)
    agent.script(*[{"action": "wait"}] * (agent.MAX_STEPS + 2))
    assert "не закінчила" in agent.run("чекай вічно")


def test_reply_with_text_around_json(agent):
    assert agent._parse('Ось:\n```json\n{"action": "done", "say": "Ок"}\n```') == {"action": "done", "say": "Ок"}
    assert agent._parse("не знаю") == {}


def test_screen_do_needs_explicit_words(lin, env):
    from conftest import fake_reply
    from raphael import assistant
    env.patch("_llm_call", lambda model, messages, **kw: fake_reply("[ACTION:screen_do:видалити файли]"))
    assert "не почула явного прохання" in assistant.ask_lin("як справи")


def test_same_click_three_times_is_treated_as_done(agent):
    """Кнопка, що нічого не міняє на екрані: у перевірці Лін тиснула її 15 разів."""
    agent.script(*[{"action": "click", "target": "синя кнопка", "x": 300, "y": 300}] * 5)
    said = agent.run("натисни синю кнопку")
    assert said.startswith("Зробила: click") and "готово" in said
    assert sum(1 for d in agent.did if d == ("click",)) == 2


def test_typing_goes_as_characters(agent, monkeypatch):
    import sys
    import types
    typed = []
    monkeypatch.setitem(sys.modules, "keyboard", types.SimpleNamespace(write=lambda text, delay=0: typed.append(text)))
    agent.script({"action": "type", "text": "Влад"}, {"action": "done", "say": "Вписала."})
    assert agent.run("впиши Влад") == "Вписала." and typed == ["Влад"]
