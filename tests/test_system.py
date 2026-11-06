"""Стан системи: журнал Windows через wevtutil і винуватець навантаження CPU."""
import types

import pytest

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
    run = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=SAMPLE.encode("utf-16-le"))
    monkeypatch.setattr(env.get("subprocess"), "run", run)
    assert len(env.get("_parse_events")(env.get("_query_system_log")(60_000, 25))) == 3


def test_broken_query_is_not_an_empty_log(env, monkeypatch):
    run = lambda *a, **k: types.SimpleNamespace(returncode=87, stdout=b"",
                                                stderr=b"The parameter is incorrect.")
    monkeypatch.setattr(env.get("subprocess"), "run", run)
    with pytest.raises(OSError, match="parameter is incorrect"):
        env.get("_query_system_log")(60_000, 25)
    assert env.get("_get_event_log_errors")() == []          # монітор при цьому мовчить


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
    env.patch("_query_system_log", lambda ms, count: SAMPLE * 14)     # 42 події, ліміт 40
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



def _ev(provider, event_id, *data, level=2):
    fields = "".join(f"<Data Name='param{i + 1}'>{d}</Data>" for i, d in enumerate(data))
    return (f"<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
            f"<Provider Name='{provider}'/><EventID>{event_id}</EventID><Level>{level}</Level></System>"
            f"<EventData>{fields}</EventData></Event>")


# Ранок 2026-11-06: три перезапуски поспіль, Лін сказала «7 помилок», справжня одна
MORNING = (_ev("Microsoft-Windows-BitLocker-Driver", 24641, "0x0", "C:") * 5
           + _ev("Microsoft-Windows-Windows Firewall With Advanced Security", 2042, "18", "-2147024891") * 3
           + _ev("Microsoft-Windows-DistributedCOM", 10010, "{A463FCB9-6B1C-4E0D-A80B-A2CA7999E25D}")
           + _ev("Service Control Manager", 7023, "Microsoft Usage and Quality Insights", "%%2147943515")
           + _ev("Microsoft-Windows-DeviceAssociationService", 3502, "EsclUsb#{509f}", "spoolsv.exe")
           + _ev("Service Control Manager", 7000, "GVCIDrv", "%%2") * 3)


def test_health_report_skips_windows_noise_and_names_the_rest(env, monkeypatch):
    monkeypatch.setattr(env.get("psutil"), "cpu_percent", lambda interval=None: 5)
    env.patch("_query_system_log", lambda ms, count: MORNING)
    report = env.get("_system_health_report")()
    assert "в журналі 3 помилки: служба «драйвер підсвітки Gigabyte RGB Fusion» не запустилась (3 рази)" in report
    assert "BitLocker" not in report and "щонайменше" not in report


def test_only_noise_is_said_as_noise(env, monkeypatch):
    monkeypatch.setattr(env.get("psutil"), "cpu_percent", lambda interval=None: 5)
    env.patch("_query_system_log", lambda ms, count: _ev("Microsoft-Windows-BitLocker-Driver", 24641) * 4)
    assert "Помилок у журналі немає, лише звичний шум Windows" in env.get("_system_health_report")()


def test_service_stopped_for_other_reason_is_counted(env):
    is_noise = env.get("_is_event_noise")
    assert not is_noise("Service Control Manager", "7023", ["Spooler", "%%5"])
    assert is_noise("Service Control Manager", "7023", ["Usage", "%%1115"])
