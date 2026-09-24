"""
Таблиця реальних випадків для mail_triage.classify.

Кожен рядок це лист, на якому правило колись помилялось або заради якого
його додали. Нове правило = новий рядок тут, тоді перестановка перевірок
у classify не зламає старе непомітно.
"""
import pytest

from raphael import mail_triage as mt

MAIN, OFFICIAL_BOX = "основна", "офіційна"

CASES = [
    # (скринька, From, тема, очікувана категорія)
    # ── Безпека й гроші в основній скриньці ──
    (MAIN, "Google <no-reply@accounts.google.com>", "Security alert", mt.SECURITY),
    (MAIN, "Microsoft <account-security-noreply@accountprotection.microsoft.com>",
     "Your single-use code", mt.SECURITY),
    (MAIN, "Netflix <info@account.netflix.com>", "Payment unsuccessful", mt.MONEY),
    (MAIN, "Shop <orders@shop.example>", "Your card was declined", mt.MONEY),
    (MAIN, "Revolut <no-reply@revolut.com>", "Weekly summary", mt.MONEY),
    (MAIN, "Orange <e-faktura@pl.orange.com>", "Faktura", mt.MONEY),
    # ── Робота й доставка ──
    (MAIN, "Jira <jira@team.atlassian.net>", "[JIRA] ticket assigned", mt.WORK),
    (MAIN, "Omniva <info@omniva.lt>", "Siunta pakeliui", mt.DELIVERY),
    # Доставка перевіряється раніше за магазини: посилка реальна, реклама ні
    (MAIN, "Temu <temu@temuemail.com>", "Your order has shipped", mt.DELIVERY),
    (MAIN, "Temu <temu@temuemail.com>", "Flash sale -90%", mt.NOISE),
    (MAIN, "Rozetka <info@rozetka.example>", "Ваше замовлення відправлено", mt.DELIVERY),
    # ── Установи й люди ──
    # migracija має домен .gov.lt: через це 70 листів мовчали в «авто»
    (OFFICIAL_BOX, "Migris <noreply@migracija.gov.lt>", "Leidimo galiojimas", mt.OFFICIAL),
    (OFFICIAL_BOX, "Friend <friend@gmail.com>", "Привіт", mt.HUMAN),
    (OFFICIAL_BOX, "LinkedIn <messaging-digest-noreply@linkedin.com>",
     "Anna just messaged you", mt.HUMAN),
    # Лист про скринінг прийшов з myworkday і мало не загубився
    (OFFICIAL_BOX, "Rocket Software <rocket@myworkday.com>", "Next steps", mt.HUMAN),
    (OFFICIAL_BOX, "Talent <no-reply@myworkday.com>", "Thank you for applying", mt.APPLIED),
    # ── Шум ──
    (OFFICIAL_BOX, "LinkedIn <jobalerts-noreply@linkedin.com>", "New jobs matching you", mt.NOISE),
    # У notion.so відправники з людськими іменами пролазили в «живий»
    (OFFICIAL_BOX, "Sophie <sophie@mail.notion.so>", "Quick question", mt.NOISE),
    # DM у Discord не будить голосом, хоч формально «людина написала»: тихе «авто»
    (OFFICIAL_BOX, "Discord <noreply@discord.com>", "Anna just messaged you", mt.AUTO),
    (OFFICIAL_BOX, "Unknown <hello@startup.io>", "Hi", mt.HUMAN),     # офіційна: незнайоме = людина
    (MAIN, "Unknown <hello@startup.io>", "Hi", mt.AUTO),              # основна: незнайоме = магазин

    # ── Хибні спрацювання, знайдені 2026-09-24 ──
    # Відмова на вакансію і відхилене запрошення не «гроші»
    (MAIN, "Careers <careers@company.example>", "Your application was unsuccessful", mt.AUTO),
    (MAIN, "Calendar <calendar-notification@google.com>", "Invitation declined: Sync", mt.AUTO),
    # Відгук на вакансію не посилка
    (MAIN, "Work.ua <support@work.ua>", "Ваше резюме відправлено роботодавцю", mt.NOISE),
    # Відбійник пошти не посилка і не «живий» лист з gmail
    (MAIN, "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
     "Delivery Status Notification (Failure): message not delivered", mt.AUTO),
    # Підроблене імʼя: адреса чужа, тож це не «безпека» і не зачитується
    (MAIN, "accounts.google.com <promo@spam-domain.xyz>", "Hi", mt.AUTO),
    (MAIN, "Revolut Support <help@phish.example>", "Hello", mt.AUTO),
]


@pytest.mark.parametrize("box,sender,subject,expected", CASES)
def test_classify(box, sender, subject, expected):
    assert mt.classify(sender, subject, box) == expected


def test_every_category_has_label():
    for cat in mt.ANNOUNCE | mt.ARCHIVE | set(mt.FALLBACK.values()):
        assert cat in mt.LABELS


def test_noise_is_never_announced():
    assert not (mt.ARCHIVE & mt.ANNOUNCE)
