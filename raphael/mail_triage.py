"""
Сортувальник пошти для Рафаеля.

Задача одна: у скриньці на один живий лист припадає приблизно сім розсилок,
тому «є непрочитаний лист» як сигнал не працює. Тут листи розкладаються на
десять категорій, і голосом Рафаель оголошує лише шість (ANNOUNCE).

Категорії:
    шум       розсилки вакансій і реклама. Мітимо і прибираємо з вхідних.
    подача    автопідтвердження «ми отримали вашу заявку». Мітимо, лишаємо.
    офіційне  установи: біржа праці, банки, міграційна. Мітимо, лишаємо, оголошуємо.
    живий     людина написала руками. Мітимо, лишаємо, оголошуємо.
    авто      машинний відправник, якого ще не знаємо. Мітимо, лишаємо, мовчимо.
    події     квитки, мітапи. Мітимо, лишаємо, мовчимо.
    гроші, безпека, робота, доставка   лише для основної скриньки, оголошуємо.

Правила по відправнику дивляться ЛИШЕ на адресу, не на імʼя: імʼя в полі From
кожен пише яке хоче, і «accounts.google.com <promo@spam.xyz>» ставав «безпекою»
й зачитувався вголос.

Тести: tests/test_mail_triage.py. Нове правило = новий рядок у тесті.

«авто» це навмисний відстійник: туди падає все нерозпізнане машинне, і час від
часу варто туди зазирнути та дописати правило сюди.

Модуль не знає про Gmail нічого зайвого: сервіс передається ззовні.
"""
import re
from email.utils import parseaddr

# ── категорії ─────────────────────────────────────────────────────────────────
NOISE, APPLIED, OFFICIAL, HUMAN, AUTO = "шум", "подача", "офіційне", "живий", "авто"
# Додано для основної скриньки: там життя, а не тільки пошук роботи
MONEY, SECURITY, WORK, DELIVERY, EVENT = "гроші", "безпека", "робота", "доставка", "події"

LABELS = {
    NOISE:    "Трекер/Шум",
    APPLIED:  "Трекер/Подача",
    OFFICIAL: "Трекер/Офіційне",
    HUMAN:    "Трекер/Живий лист",
    AUTO:     "Трекер/Авто",
    MONEY:    "Трекер/Гроші",
    SECURITY: "Трекер/Безпека",
    WORK:     "Трекер/Робота",
    DELIVERY: "Трекер/Доставка",
    EVENT:    "Трекер/Події",
}

# Про що Рафаель говорить уголос
ANNOUNCE = {HUMAN, OFFICIAL, MONEY, SECURITY, WORK, DELIVERY}

# Що прибираємо з вхідних (архів, не кошик: лист лишається знайденим через пошук)
ARCHIVE = {NOISE}

# Правило за замовчуванням різне для двох скриньок, і це навмисно.
# Офіційна: незнайомий відправник це радше людина, бо адресу знають лише ті,
#           кому Влад її давав, і дошки вакансій.
# Основна:  незнайомий відправник це радше магазин, бо там 20 років реєстрацій.
FALLBACK = {"офіційна": HUMAN, "основна": AUTO}


# ── правила основної скриньки ─────────────────────────────────────────────────
# Порядок нижче за спаданням ціни помилки: пропустити лист про безпеку гірше,
# ніж про гроші, а про гроші гірше, ніж про роботу.

_SECURITY_FROM = re.compile(
    r"accounts\.google\.com|noreply-accounts@google|@account-security"
    r"|accountprotection\.microsoft\.com",       # разові коди Microsoft
    re.I)
_SECURITY_SUBJ = re.compile(
    r"security alert|сповіщення системи безпеки|critical security"
    r"|new sign-?in|new device|підозріл|suspicious|unusual activity"
    r"|verification code|код підтвердження|password (was )?changed|змінено пароль",
    re.I)

# Фінансові відправники: від них навіть маркетинг краще побачити
_MONEY_FROM = re.compile(
    r"@revolut\.com|@swedbank|@seb\.lt|@luminor|@paysera|@wise\.com|@paypal"
    r"|googleplay-noreply@google|@n26\.com|@monobank|@privatbank"
    r"|e-faktura@|@pl\.orange\.com",            # рахунки Orange Польща
    re.I)
# Без голих «declined» і «unsuccessful»: так пишуть і відмови на вакансії
# («your application was unsuccessful»), і календар («Invitation declined»).
# «payment unsuccessful» від Netflix ловиться словом «payment».
_MONEY_SUBJ = re.compile(
    r"payment|invoice|receipt|billing|charged|refund"
    r"|(card|transaction) (was |has been )?declined"
    r"|квитанц|рахунок|оплат|списан|платіж|повернення кошт"
    r"|mok[eė]jim|s[ąa]skait"                            # литовською
    r"|order .{0,20}(cancel|confirm)|замовлення .{0,20}(скасован|підтвердж)",
    re.I)

# Робота. Будь-який піддомен winwin.travel, плюс трекери й репозиторії.
_WORK = re.compile(
    r"@(\w+\.)?winwin\.travel|@atlassian\.net|jira@|gitlab@|@sentry\.io"
    r"|@bitbucket\.org",          # доступи до wwt-bitbucket
    re.I)

_DELIVERY = re.compile(
    r"@omniva\.lt|@dpd\.|@venipak|@lpexpress|@itella|@post\.lt|@ups\.com|@dhl",
    re.I)
# Перевіряється РАНІШЕ за магазини, тому «your order has shipped» від Temu
# піде в доставку, а не в шум. Це навмисно: посилка реальна, реклама ні.
# «відправлен» лише поруч із замовленням: інакше «Ваше резюме відправлено»
# ставало доставкою
_DELIVERY_SUBJ = re.compile(
    r"siunt|посилк|замовлення\W+(\w+\W+){0,3}?відправлен|shipment|has shipped|order shipped"
    r"|tracking number|out for delivery|delivered|į paštomat",
    re.I)
# Відбійники пошти: «message not delivered» не посилка
_BOUNCE_SUBJ = re.compile(
    r"delivery status notification|undeliver|not (been )?delivered|failure notice"
    r"|mail delivery (failed|subsystem)|returned mail|nepristatyt",
    re.I)

_EVENT = re.compile(r"eventbrite|@lzka\.lt|@meetup\.com|@email\.meetup\.com", re.I)

# Реклама, що приходить саме на основну скриньку
_SHOP = re.compile(
    # ЛОВИМО ЗА ДОМЕНОМ, А НЕ ЗА АДРЕСОЮ. AliExpress шле щонайменше з чотирьох
    # піддоменів (deals, mail, selections, ae-best-message-notice), і кожен раз,
    # коли я перелічував їх поіменно, наступний лист приходив з пʼятого.
    r"aliexpress\.|temuemail|temu\.com|pigu\.lt|\.bolt\.eu|bolt-food"
    r"|neonet\.pl|@message\.reserved\.com"
    r"|@e\.iqos\.com|@your\.zyn\.com|notino\.lt|farfetch|@travel\.kiwi\.com|kiwi\.com"
    r"|citybee\.lt|genius\.space|skins\.cash|dreamstime|manodaktaras\.lt"
    r"|kraujolaboratorija\.lt|ltglink\.lt|derekis\.lt|@updates\.eurovision"
    r"|no-reply@twitch\.tv|noreply@discord\.com|@mail\.instagram|priority\.instagram"
    r"|hoyoverse|@steampowered\.com|account\.netflix\.com|@email\.claude\.com"
    r"|@email\.openai\.com|support@codepen\.io|cvmarket\.lt|@crm\.pigu\.lt|@plus\.pigu\.lt"
    # Основний обсяг основної скриньки. Порядок тут не важливий, важливо покриття:
    # ці 40 відправників дають дві третини всього, що інакше висить у «авто».
    r"|info@twitter\.com|verify@twitter\.com|@redditmail\.com|@facebookmail\.com"
    r"|@pinterest\.com|@explore\.pinterest|@discover\.pinterest|@inspire\.pinterest"
    r"|@ideas\.pinterest|noreply@olx\.ua|@aruodas\.lt|@vseosvita\.ua|@obuchenie\.com\.ua"
    r"|@email\.forbes\.com|forbes@|@bolt\.eu|@bolt-food\.net|receipts@bolt"
    r"|@rides-marketing\.bolt|@delivery-partner-marketing\.bolt"
    r"|seaofthieves@microsoft|@info\.spark\.lt|@mail\.adobe\.com|@ryanairemail\.com"
    r"|hq@pleso\.me|@dmarket\.com|hello@lemongym\.lt|@email\.jysk\.lt"
    r"|@notify\.gta5rp\.com|hello@chess\.com|@multiplex\.ua|@ua\.goit\.global"
    r"|@n\.dribbble\.com|@mail\.ultimate-guitar\.com|no-reply@vinted\.lt"
    r"|@wolt\.com|@zalando|@shopifyemail\.com|@service-mail\.zalando",
    re.I)


# ── правила ───────────────────────────────────────────────────────────────────
# Установи. Тут помилка дорога, тому перевіряємо першими.
_OFFICIAL = re.compile(
    r"@uzt\.lt|@info\.uzt\.lt|portal\.uzt\.lt"          # біржа праці
    r"|@lb\.lt|lietuvosbankas"                          # Банк Литви
    # migracija має домен .gov.lt, а не .lt. Через цю літеру 70 листів про
    # закінчення терміну дії документа лежали в «авто» і мовчали.
    r"|@vmi\.lt|@sodra\.lt|@migracija\.gov\.lt|@migris|@vrm\.lt"
    r"|@registrucentras\.lt|@vgtu\.lt|@vilniustech\.lt" # реєстр, універ
    r"|@mruni\.eu|classroom\.google\.com",              # навчання: MRU, завдання
    re.I)

# Людина написала руками, особисте повідомлення в LinkedIn
_HUMAN_HINT = re.compile(
    r"just messaged you|надіслав.{0,15}повідомлення",
    re.I)

# Чати, де Влад і так читає повідомлення в самому застосунку. Формально там теж
# «людина надіслала повідомлення», але будити голосом через кожен Discord-DM
# безглуздо. Виняток із _HUMAN_HINT, а не окрема категорія.
_CHAT_APP = re.compile(r"@discord\.com|@telegram|@viber|@slack\.com|@mail\.instagram", re.I)

# Автопідтвердження поданої заявки
_APPLIED = re.compile(
    r"application received|thank you for applying|thanks for applying"
    r"|thank you for your application|your application for"
    r"|\[auto-reply\]"
    r"|дякуємо за відгук|дякуємо за ваш відгук"
    r"|d[žz]iaugiam[eė]s, kad pateikei"                  # Ignitis
    r"|patvirtinimas apie gaut[ąa] kandidat[ūu]r[ąa]"    # BTA
    r"|d[eė]kojame, kad kandidatuoji"                    # Банк Литви
    r"|parai[šs]ka jau",                                 # IKI
    re.I)

# Явний шум: масові розсилки й реклама
_NOISE_FROM = re.compile(
    # LinkedIn розсилає з семи різних адрес, і кожну довелось ловити окремо.
    # Живими лишаються рівно три: messaging-digest-noreply@, hit-reply@,
    # inmail-hit-reply@. Решта це алерти, дайджести й «19 переглядів профілю».
    r"jobalerts-noreply@linkedin|updates-noreply@linkedin|newsletters-noreply@linkedin"
    r"|jobs-noreply@linkedin|editors-noreply@linkedin|jobs-listings@linkedin"
    r"|linkedin@em\.linkedin|linkedin@e\.linkedin"
    r"|notifications-noreply@linkedin|security-noreply@linkedin"
    # Дошки вакансій і маркетингові серії. У notion.so відправники мають людські
    # імена (sophie@, ivan@), тому вони пролазили в «живий».
    r"|updates@meetfrank\.com|@mail\.notion\.so|@meetup\.com|@email\.meetup\.com"
    # Запрошення в контакти. Їх видно в самому LinkedIn, у пошті вони лише сміття.
    # УВАГА: inmail-hit-reply@ і hit-reply@ сюди НЕ входять, це справжні InMail
    # від рекрутерів, вони мають лишатись живими.
    # invitations@ це запрошення в контакти, messages-noreply@ це «19 переглядів
    # профілю», «додайте Х», стріки. Ані те, ані те не варте голосу.
    r"|invitations@linkedin\.com|messages-noreply@linkedin"
    r"|talent@ibm\.com|@your\.zyn\.com|support@work\.ua"
    r"|info@cvbankas\.lt|website@work\.ua|magic@djinni\.co"
    r"|@lensa\.com|jobs2web\.com|@order\.eventbrite\.com"
    r"|@e\.iqos\.com|@mail\.aliexpress|@mail\.instagram|no-reply@discord"
    r"|no-reply@twitch\.tv|delivery-marketing|@animevost\.org",
    re.I)

_NOISE_SUBJ = re.compile(
    r"Подписка на объявления|рекомендованих вакансій|Статистика Джина"
    r"|new jobs? (posted|matching|from)|one new job matching"
    r"|just in: new jobs|stay updated on new opportunities|stay connected with"
    r"|be the first to apply|apply now to|posted on \d"
    # Влад 2026-08-15: «і то і то бред, бо воно ніколи нічим не кінчається».
    # Формально це сигнал (роботодавець відкрив CV), практично, ніколи не веде
    # до розмови. Тому шум, попри спокусу вважати інакше.
    r"|Повідомлення на ваше резюме|резюме просмотрел|резюме переглянув",
    re.I)

# Платформи рекрутингу (ATS). Відправник машинний, але пише через них жива людина
# з відділу кадрів, і саме тут лежать листи типу «наступний крок» чи «інтервʼю».
# Перевіряється ПІСЛЯ шуму, бо ті ж платформи розсилають і маркетинг
# («Stay Connected with…»), і ПІСЛЯ автопідтверджень, бо «Thank you for applying»
# це подача, а не розмова.
# Причина існування правила: лист Rocket Software про скринінг прийшов з
# myworkday.com і мало не загубився серед машинних відправників.
_ATS = re.compile(
    r"myworkday\.com|@hire\.lever\.co|greenhouse-mail|teamtailor"
    r"|workablemail|recruitee|ashbyhq|smartrecruiters|icims"
    r"|@365ats\.com|@ignitis\.lt|talent\.sebgroup",
    re.I)

# Безкоштовна пошта. Компанії з таких адрес не розсилають, тому це майже завжди
# жива людина: друг, орендодавець, репетитор. Перевіряється ПЕРЕД правилом за
# замовчуванням, інакше в основній скриньці такий лист падав би в «авто».
_FREEMAIL = re.compile(
    r"@(gmail|googlemail|outlook|hotmail|live|yahoo|proton(mail)?|icloud|gmx)\."
    r"|@(ukr\.net|i\.ua|meta\.ua|mail\.ru|yandex\.)",
    re.I)

# Машинні відправники, яких ще не класифікували
_MACHINE = re.compile(r"no-?reply|donotreply|neatsakyti|notification|noreply"
                      r"|mailer-daemon|postmaster", re.I)


def classify(sender: str, subject: str, box: str = "офіційна") -> str:
    """
    Категорія листа за відправником і темою.

    box: «офіційна» або «основна». Впливає лише на те, куди падає невідомий
    відправник, і на те, чи перевіряються побутові категорії взагалі.

    ПОРЯДОК ПЕРЕВІРОК НЕ ДОВІЛЬНИЙ. Він за спаданням ціни помилки, і кожен
    наступний блок може перехопити лист у попереднього. Переставляти лише
    свідомо, з тестом.
    """
    # Правила, від яких лист стає ГУЧНІШИМ, дивляться лише на адресу: імʼя у From
    # підробляється як завгодно. Ознаки машини (noreply, notification) можна
    # брати й з імені: від них лист лише тихшає.
    raw = sender or ""
    sender = parseaddr(raw)[1] or raw
    subject = subject or ""
    if box == "основна":
        # Безпека найперша: пропустити лист про вхід у акаунт найдорожче
        if _SECURITY_FROM.search(sender) or _SECURITY_SUBJ.search(subject):
            return SECURITY
        # Гроші раніше за шум, бо «payment unsuccessful» приходить і від Netflix,
        # який в іншому житті чистий магазин
        if _MONEY_FROM.search(sender) or _MONEY_SUBJ.search(subject):
            return MONEY
        if _WORK.search(sender):
            return WORK
        if _DELIVERY.search(sender) or (_DELIVERY_SUBJ.search(subject)
                                        and not _BOUNCE_SUBJ.search(subject)):
            return DELIVERY

    if _OFFICIAL.search(sender):
        return OFFICIAL
    if _HUMAN_HINT.search(subject) and not _CHAT_APP.search(sender):
        return HUMAN
    if _APPLIED.search(subject):
        return APPLIED

    if box == "основна":
        if _SHOP.search(sender):
            return NOISE
        if _EVENT.search(sender):
            return EVENT

    if _NOISE_FROM.search(sender) or _NOISE_SUBJ.search(subject):
        return NOISE
    if _ATS.search(sender):
        return HUMAN
    if _FREEMAIL.search(sender) and not _MACHINE.search(raw):
        return HUMAN
    if _MACHINE.search(raw):
        return AUTO
    return FALLBACK.get(box, HUMAN)


# ── робота з мітками Gmail ────────────────────────────────────────────────────
def ensure_labels(svc) -> dict:
    """Створює мітки Трекер/* якщо їх ще немає. → {категорія: labelId}"""
    existing = {l["name"]: l["id"] for l in
                svc.users().labels().list(userId="me").execute().get("labels", [])}
    out = {}
    for cat, name in LABELS.items():
        if name not in existing:
            created = svc.users().labels().create(
                userId="me",
                body={"name": name,
                      "labelListVisibility": "labelShow",
                      "messageListVisibility": "show"},
            ).execute()
            existing[name] = created["id"]
        out[cat] = existing[name]
    return out


def headers(msg) -> tuple:
    h = {x["name"]: x["value"] for x in msg["payload"].get("headers", [])}
    return h.get("From", ""), h.get("Subject", "")
