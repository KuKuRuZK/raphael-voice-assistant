"""«Вимкнути Лін повністю»: голос і модель не лишаються жити без неї."""


class _Proc:
    def __init__(self, pid, cmdline):
        self.pid, self.info, self.killed = pid, {"cmdline": cmdline}, False

    def kill(self):
        self.killed = True


def test_voice_server_stops_with_lin(env, lin, monkeypatch):
    import psutil
    from raphael import tts
    server = r"C:\AI\tts\lin_voice_server.py"
    env.patch("TTS_HTTP_SERVER", [r"C:\AI\tts\holos\Scripts\pythonw.exe", server])
    launcher = _Proc(1, [r"C:\AI\tts\holos\Scripts\pythonw.exe", server])
    python = _Proc(2, [r"C:\Python312\pythonw.exe", server])
    other = _Proc(3, ["pythonw", r"C:\src\Lin\lin.py"])
    editor = _Proc(4, ["notepad", r"C:\AI\tts\notes.txt"])
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [launcher, python, other, editor])

    assert tts.stop_http_server() == 2
    assert launcher.killed and python.killed
    assert not other.killed and not editor.killed


def test_quit_unloads_only_lins_model(env, lin, monkeypatch):
    from raphael import llm
    calls = []
    env.patch("BRAIN_LOCAL_MODEL", "lmstudio:google/gemma-4-26b-a4b-qat")
    monkeypatch.setattr(llm, "_lms", lambda *a, **k: calls.append(a) or True)
    assert llm.local_unload_mine() is True
    assert calls == [("unload", "google/gemma-4-26b-a4b-qat")]       # не --all: чужі моделі лишаються
