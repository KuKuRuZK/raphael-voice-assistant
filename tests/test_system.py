"""Стан системи: журнал Windows через wevtutil і винуватець навантаження CPU."""
import types

SAMPLE = (
    "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
    "<Provider Name='Microsoft-Windows-Kernel-Power' Guid='{331c3b3a}'/>"
    "<EventID>41</EventID><Level>1</Level></System></Event>"
    "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
    "<Provider Name='disk'/><EventID Qualifiers='49156'>7</EventID><Level>2</Level>"
    "</System></Event>"
    "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
    "<Provider Name='Service Control Manager' Guid='{555908d1}' EventSourceName='x'/>"
    "<EventID Qualifiers='49152'>7000</EventID><Level>2</Level></System></Event>"
)


def test_wevtutil_xml_is_parsed(env):
    assert env.get("_parse_events")(SAMPLE) == [
        ("Microsoft-Windows-Kernel-Power", 1, "41"),
        ("disk", 2, "7"),
        ("Service Control Manager", 2, "7000"),
    ]
    assert env.get("_parse_events")("") == []


def test_utf16_output_is_decoded(env, monkeypatch):
    run = lambda *a, **k: types.SimpleNamespace(stdout=SAMPLE.encode("utf-16-le"))
    monkeypatch.setattr(env.get("subprocess"), "run", run)
    assert len(env.get("_parse_events")(env.get("_query_system_log")(60_000, 25))) == 3


def test_only_critical_or_important_errors_are_announced(env):
    env.patch("_query_system_log", lambda ms, count: SAMPLE)
    events = env.get("_get_event_log_errors")()
    # Service Control Manager: звичайна помилка з неважливого джерела
    assert [(p, lvl) for p, lvl, _ in events] == [
        ("Microsoft-Windows-Kernel-Power", "critical"), ("disk", "error")]


def test_missing_wevtutil_is_quiet(env):
    def missing(ms, count):
        raise FileNotFoundError("wevtutil")
    env.patch("_query_system_log", missing)
    assert env.get("_get_event_log_errors")() == []


def test_health_report_counts_honestly(env, monkeypatch):
    monkeypatch.setattr(env.get("psutil"), "cpu_percent", lambda interval=None: 12.0)
    env.patch("_query_system_log", lambda ms, count: SAMPLE)
    assert "в журналі 3 помилки" in env.get("_system_health_report")()
    env.patch("_query_system_log", lambda ms, count: SAMPLE * 7)      # 21 подія, ліміт 20
    assert "щонайменше" in env.get("_system_health_report")()
    env.patch("_query_system_log", lambda ms, count: "")
    assert "Помилок у журналі немає" in env.get("_system_health_report")()


def test_cpu_culprit_skips_idle(env, monkeypatch):
    def proc(pid, name, cpu):
        return types.SimpleNamespace(pid=pid, info={"name": name, "cpu_percent": cpu})
    procs = [proc(0, "System Idle Process", 750.0), proc(4, "Idle", 90.0),
             proc(1200, "chrome.exe", 35.0), proc(900, "python.exe", 60.0)]
    monkeypatch.setattr(env.get("psutil"), "process_iter", lambda attrs=None: iter(procs))
    assert env.get("_top_cpu_process")() == "python.exe"
