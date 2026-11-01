"""Tool calling: do_action(type, param) замість тегу, ті самі перевірки."""
import json
import types

import pytest

P = "openai/gpt-oss-120b"


def _call(action, param=None, name="do_action", raw=None):
    args = raw if raw is not None else json.dumps(
        {"type": action, **({"param": param} if param is not None else {})})
    return types.SimpleNamespace(function=types.SimpleNamespace(name=name, arguments=args))


def _message(content, *calls):
    return types.SimpleNamespace(content=content, tool_calls=list(calls) or None)


@pytest.fixture
def model(env):
    """Модель відповідає заданим повідомленням; seen: з якими kwargs її кликали."""
    seen = []

    def install(message, tool_calling=True):
        def chat(model, messages, **kw):
            seen.append(kw)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])
        env.patch("llm_chat", chat)
        env.patch("TOOL_CALLING", tool_calling)
        env.patch("GROQ_PRIMARY_MODEL", P)
        env.patch("FAST_MODEL", "")
        env.get("history")[1:] = []
        return seen
    return install


@pytest.fixture
def executed(env):
    done = []
    env.patch("execute_action", done.append)
    return done


def test_tool_action_parsing(env):
    tool_action = env.get("_tool_action")
    assert tool_action(_message("", _call("spotify_play", "Океан Ельзи"))) == "[ACTION:spotify_play:Океан Ельзи]"
    assert tool_action(_message("", _call("Spotify_Pause"))) == "[ACTION:spotify_pause:]"
    assert tool_action(_message("", _call("note_add", "купити [молоко]"))) == "[ACTION:note_add:купити [молоко)]"
    assert tool_action(_message("", _call("x", raw="{не json"), _call("volume_up"))) == "[ACTION:volume_up:]"
    assert tool_action(_message("", _call("rm -rf", "/"))) == ""                 # не назва дії
    assert tool_action(_message("", _call("volume_up", name="other_fn"))) == ""
    assert tool_action(_message("Привіт.")) == ""


def test_tool_call_runs_the_action(env, model, executed):
    seen = model(_message("Ставлю на паузу.", _call("spotify_pause")))
    assert env.get("ask_lin")("постав музику на паузу") == "Ставлю на паузу."
    assert executed == ["[ACTION:spotify_pause:]"]
    assert seen[0]["tools"][0]["function"]["name"] == "do_action"
    assert env.get("history")[-1]["content"] == "Ставлю на паузу. [ACTION:spotify_pause:]"


def test_tool_call_without_text(env, model, executed):
    model(_message(None, _call("volume_up")))
    assert env.get("ask_lin")("зроби гучніше в системі") == ""
    assert executed == ["[ACTION:volume_up:]"]


def test_dangerous_tool_call_is_still_blocked(env, model, executed):
    model(_message("Закриваю.", _call("kill_process", "chrome")))
    assert env.get("ask_lin")("як справи") == "Цього не роблю: не почула явного прохання."
    assert executed == []


def test_flag_off_sends_no_tools(env, model, executed):
    seen = model(_message("Ок [ACTION:volume_up:]"), tool_calling=False)
    env.get("ask_lin")("гучніше в системі")
    assert "tools" not in seen[0]
    assert executed == ["[ACTION:volume_up:]"]


def test_local_model_gets_no_tools(env):
    sent = {}

    def create(model, messages, **kw):
        sent.update(kw)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=_message("Ок."))])
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    env.patch("_llm", lambda m: (client, m.split(":", 1)[-1]))
    env.get("_llm_call")("local:gemma3:4b", [], tools=[{"type": "function"}])
    assert "tools" not in sent


def test_prompt_mentions_the_function_only_when_on(env, model):
    model(_message(""), tool_calling=True)
    assert "do_action" in env.get("build_system_prompt")()
    env.patch("TOOL_CALLING", False)
    assert "do_action" not in env.get("build_system_prompt")()
