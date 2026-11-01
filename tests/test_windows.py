"""
Те, що перевіряється лише на справжній Windows (у CI: windows-latest).
Решта тестів іде на заглушках і однаково проходить і тут, і на Linux.
"""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="лише на Windows")


def test_wevtutil_query_really_runs(env):
    # Запит за тиждень: якщо XPath чи лапки в командному рядку зламані,
    # wevtutil поверне код помилки, і _query_system_log кине OSError
    xml = env.get("_query_system_log")(7 * 24 * 3600 * 1000, 5)
    events = env.get("_parse_events")(xml)
    assert len(events) <= 5
    assert all(level in (1, 2) for _, level, _ in events)
    assert not xml.strip() or "<Event" in xml


def test_health_report_on_real_windows(env):
    report = env.get("_system_health_report")()
    assert report.startswith("Зараз: процесор")
    assert "журнал" in report                  # рядок про журнал є завжди: з помилками чи без
