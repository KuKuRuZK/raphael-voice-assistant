"""Пошта за добу в ранковому дайджесті: по категоріях сортувальника."""
import types

import pytest


def _ok(value):
    return types.SimpleNamespace(execute=lambda: value)


class Box:
    """Gmail з мітками: list фільтрує за всіма labelIds, modify вішає й знімає мітки."""
    def __init__(self, labels_by_name, msgs):
        self.labels_by_name, self.msgs = labels_by_name, msgs

    def users(self):
        return self

    messages = labels = users

    def list(self, userId, labelIds=None, maxResults=100, q=None):
        if labelIds is None and q is None:
            return _ok({"labels": [{"name": n, "id": i} for n, i in self.labels_by_name.items()]})
        hits = [i for i, m in self.msgs.items()
                if set(labelIds or []) <= set(m["labels"])
                and not (q and "newer_than" in q and m.get("old"))]
        return _ok({"messages": [{"id": i} for i in hits[:maxResults]]})

    def get(self, userId, id, format=None, metadataHeaders=None):
        m = self.msgs[id]
        return _ok({"labelIds": list(m["labels"]),
                    "payload": {"headers": [{"name": k, "value": v} for k, v in m["h"].items()]}})

    def modify(self, userId, id, body):
        labels = self.msgs[id]["labels"]
        labels.extend(body.get("addLabelIds", []))
        for label in body.get("removeLabelIds", []):
            labels.remove(label)
        return _ok({})


def _mail(sender, subject, *extra, old=False):
    return {"h": {"From": sender, "Subject": subject}, "labels": ["UNREAD", "INBOX", *extra], "old": old}


@pytest.fixture
def box(env):
    mt = env.get("mail_triage")
    labels = {name: f"L{i}" for i, name in enumerate(mt.LABELS.values())}

    def install(msgs):
        svc = Box(labels, msgs)
        env.patch("_gmail_accounts", lambda: [("основна", svc)])
        env.patch("_seen_gmail_ids", {})
        env.patch("_triage_label_cache", {})
        env.patch("_auth_dead", {})
        env.patch("MAIL_TRIAGE_ENABLED", True)
        return svc
    return install


def test_digest_counts_by_category(env, box):
    box({
        "s1": _mail("Google <no-reply@accounts.google.com>", "Security alert"),
        "m1": _mail("Revolut <no-reply@revolut.com>", "Your statement"),
        "m2": _mail("Revolut <no-reply@revolut.com>", "Payment received"),
        "h1": _mail("Іван Петренко <ivan.petrenko@gmail.com>", "Привіт, як справи?"),
        "n1": _mail("LinkedIn <jobalerts-noreply@linkedin.com>", "New jobs matching you"),
        "n2": _mail("LinkedIn <jobalerts-noreply@linkedin.com>", "More jobs"),
        "a1": _mail("Shop <news@shop.example>", "Sale -50%"),              # авто: мовчимо
        "o1": _mail("Revolut <no-reply@revolut.com>", "Old statement", old=True),
    })
    assert env.get("_gmail_digest_text")() == (
        "Непрочитане за добу: 1 лист про безпеку, 2 листи про гроші, "
        "1 живий лист від Іван Петренко. Ще прибрала з вхідних 2 розсилки.")


def test_digest_triages_first_so_monitor_stays_quiet(env, box):
    box({"h1": _mail("Іван <ivan.petrenko@gmail.com>", "Привіт")})
    assert "1 живий лист від Іван" in env.get("_gmail_digest_text")()
    assert env.get("_gmail_check_new")() == []            # уже розкладений і прозвучав


def test_digest_read_mail_is_not_repeated(env, box):
    svc = box({"m1": _mail("Revolut <no-reply@revolut.com>", "Your statement")})
    env.get("_gmail_digest_text")()
    svc.msgs["m1"]["labels"].remove("UNREAD")             # прочитав на телефоні
    assert env.get("_gmail_digest_text")() == "Важливих непрочитаних листів за добу немає."


def test_digest_without_triage_is_plain_unread(env, box):
    box({"h1": _mail("Іван <ivan.petrenko@gmail.com>", "Привіт")})
    env.patch("MAIL_TRIAGE_ENABLED", False)
    assert env.get("_gmail_digest_text")().startswith("У тебе 1 непрочитаний лист, від Іван")
