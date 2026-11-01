"""Домічування: листи, прочитані до перевірки монітора, теж отримують мітку."""
from test_mail_digest import _mail, box  # noqa: F401  (box це фікстура)


def test_sweep_labels_read_mail_and_skips_labelled(env, box):  # noqa: F811
    mt = env.get("mail_triage")
    svc = box({
        # Влад відкрив його в Gmail раніше за монітор: UNREAD уже знято
        "read": {"h": {"From": "Friend <friend@gmail.com>", "Subject": "Привіт"},
                 "labels": ["INBOX"]},
        "noise": _mail("LinkedIn <jobalerts-noreply@linkedin.com>", "New jobs"),
        "done": _mail("Friend <friend@gmail.com>", "Старе", "L0"),
    })
    labels = svc.labels_by_name

    assert env.get("_gmail_triage_sweep")() == 2

    assert labels[mt.LABELS[mt.HUMAN]] in svc.msgs["read"]["labels"]
    # Шум мітиться й прибирається з вхідних, як і в моніторі
    assert labels[mt.LABELS[mt.NOISE]] in svc.msgs["noise"]["labels"]
    assert "INBOX" not in svc.msgs["noise"]["labels"]
    # Уже помічений лист не чіпаємо
    assert svc.msgs["done"]["labels"] == ["UNREAD", "INBOX", "L0"]
    assert env.spoken == []


def test_sweep_does_nothing_without_triage(env, box):  # noqa: F811
    box({"read": {"h": {"From": "a@gmail.com", "Subject": "x"}, "labels": ["INBOX"]}})
    env.patch("MAIL_TRIAGE_ENABLED", False)
    assert env.get("_gmail_triage_sweep")() == 0
