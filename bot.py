"""TenderTap customer bot for site users who linked their Telegram chat: a
morning digest of new direct lots and of kanban lots that close soon, and a
menu with buttons to browse the feed, the deadlines and the settings.

Linking: the site gives the user https://t.me/<bot>?start=<token>; the token
is signed with LINK_SECRET, which the backend shares. No table of tokens."""
import base64
import html
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("bot")

ALMATY = timezone(timedelta(hours=5))
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "9"))        # Almaty time
SITE_URL = os.getenv("SITE_URL", "https://tendertap.kz")
LINK_TTL = 15 * 60       # seconds a link from the site stays valid
QUIET_HOUR = 21          # Almaty time: no alerts from this hour until the next digest
ALERT_MINUTES = 30       # how often new direct matches are looked for
SOON_HOURS = 48          # a kanban lot closing within this is "soon"
MAX_LOTS = 5             # lots listed per section; the rest are counted

PAGE = 5                 # lots on one page of a list
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


# --- link tokens -----------------------------------------------------------

def _sign(secret: str, payload: str) -> str:
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac)[:22].decode()


def make_token(secret: str, user_id: int, now: float | None = None) -> str:
    """The same token the backend makes; here for tests and manual linking."""
    exp = int(time.time() if now is None else now) + LINK_TTL
    payload = f"{user_id:x}-{exp:x}"
    return f"{payload}-{_sign(secret, payload)}"


def read_token(secret: str, token: str, now: float | None = None) -> int | None:
    """The user id from a valid, unexpired token; otherwise None."""
    try:
        uid, exp, sig = token.split("-", 2)
        user_id, expires = int(uid, 16), int(exp, 16)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(secret, f"{uid}-{exp}")):
        return None
    if expires < (time.time() if now is None else now):
        return None
    return user_id


# --- data ------------------------------------------------------------------

# The feed as the site shows it. ml.recommendations is rebuilt on every match,
# so "new since" comes from the first appearance in ml.recommendation_log.
FEED = """
SELECT l.name, l.price, l.end_date, l.url, l.organizer_name,
       rec.evidence->>'fit', rec.evidence->'best'->>'interest_text'
  FROM ml.recommendations rec
  JOIN core.lots l ON l.id = rec.lot_id
 WHERE rec.company_id = %(company)s
   AND (NOT %(direct)s OR rec.evidence->>'fit' = 'direct')
   AND (l.end_date IS NULL OR l.end_date > now())
   AND (%(since)s::timestamptz IS NULL OR NOT EXISTS (
            SELECT 1 FROM ml.recommendation_log g
             WHERE g.company_id = rec.company_id AND g.lot_id = rec.lot_id AND g.run_at <= %(since)s))
   AND (SELECT f.relevant FROM ml.feedback f
         WHERE f.company_id = rec.company_id AND f.lot_id = rec.lot_id
         ORDER BY f.created_at DESC, f.id DESC LIMIT 1) IS DISTINCT FROM false
   AND NOT EXISTS (SELECT 1 FROM core.kanban_cards kc
                    WHERE kc.company_id = rec.company_id AND kc.lot_id = l.id)
 ORDER BY rec.evidence->>'fit' IS NOT DISTINCT FROM 'related',
          NOT COALESCE((rec.evidence->>'has_spec')::boolean, false), rec.score DESC
"""

# Lots on the kanban without an outcome; `hours` NULL lists all of them.
KANBAN = """
SELECT l.name, l.price, l.end_date, l.url, l.organizer_name, NULL, NULL
  FROM core.kanban_cards kc
  JOIN core.lots l ON l.id = kc.lot_id
 WHERE kc.company_id = %(company)s
   AND l.end_date > now()
   AND (%(hours)s::int IS NULL OR l.end_date < now() + make_interval(hours => %(hours)s))
   AND NOT EXISTS (SELECT 1 FROM core.kanban_outcomes ko
                    WHERE ko.company_id = kc.company_id AND ko.lot_id = kc.lot_id)
 ORDER BY l.end_date
"""


def _rows(conn, sql: str, **params) -> list:
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


def new_lots(conn, company_id: int, since: datetime) -> list:
    return _rows(conn, FEED, company=company_id, direct=True, since=since)


def feed_lots(conn, company_id: int) -> list:
    return _rows(conn, FEED, company=company_id, direct=False, since=None)


def kanban_lots(conn, company_id: int, hours: int | None) -> list:
    return _rows(conn, KANBAN, company=company_id, hours=hours)


def company_name(conn, company_id: int) -> str:
    row = _rows(conn, "SELECT name FROM core.companies WHERE id = %(id)s", id=company_id)
    return row[0][0] if row else ""


# --- texts (Telegram HTML) -------------------------------------------------

def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def time_left(end_date: datetime, now: datetime | None = None) -> str:
    left = end_date - (now or datetime.now(timezone.utc))
    hours = int(left.total_seconds() // 3600)
    if hours < 1:
        return "меньше часа"
    return f"{hours} ч" if hours < 48 else f"{hours // 24} дн."


def lot_block(number: int, row, now: datetime | None = None) -> str:
    name, price, end_date, url, organizer, fit, matched = row
    title = esc((name or "Без названия").strip()[:120])
    mark = {"direct": "🎯 ", "related": "🔗 "}.get(fit, "")
    lines = [f"{mark}<b>{number}. {title}</b>"]
    facts = []
    if price:
        facts.append("💰 " + f"{price:,.0f} ₸".replace(",", " "))
    if end_date:
        facts.append("⏳ до " + end_date.astimezone(ALMATY).strftime("%d.%m %H:%M")
                     + f" (осталось {time_left(end_date, now)})")
    if facts:
        lines.append(" · ".join(facts))
    if organizer:
        lines.append("🏛 " + esc(organizer.strip()[:80]))
    if matched:
        lines.append("🧩 Ваш товар: <i>" + esc(matched.strip()[:80]) + "</i>")
    if url:
        lines.append(f'<a href="{html.escape(url)}">Открыть на площадке →</a>')
    return "\n".join(lines)


def list_page(title: str, rows: list, page: int, empty: str, now: datetime | None = None) -> tuple[str, int]:
    """One page of a lot list and the number of pages."""
    if not rows:
        return f"<b>{title}</b>\n\n{empty}", 1
    pages = (len(rows) + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    blocks = [lot_block(page * PAGE + i + 1, r, now) for i, r in enumerate(rows[page * PAGE:(page + 1) * PAGE])]
    return f"<b>{title}</b> · всего {len(rows)}\n\n" + "\n\n".join(blocks), pages


def digest_text(company: str, new: list, soon: list, now: datetime | None = None) -> str | None:
    """The morning message for one company; None when there is nothing to say."""
    if not new and not soon:
        return None
    parts = [f"☀️ <b>Доброе утро!</b>\n{esc(company)}"]
    for title, rows in (("🆕 Новые подходящие лоты", new), ("⏰ Скоро срок у лотов в работе", soon)):
        if rows:
            text = f"<b>{title}: {len(rows)}</b>\n\n" + "\n\n".join(
                lot_block(i + 1, r, now) for i, r in enumerate(rows[:MAX_LOTS]))
            if len(rows) > MAX_LOTS:
                text += f"\n\n…и ещё {len(rows) - MAX_LOTS}, смотрите по кнопке ниже"
            parts.append(text)
    return "\n\n".join(parts)


def alert_text(new: list, now: datetime | None = None) -> str:
    """The daytime message about direct matches that have just appeared."""
    title = "Новый подходящий лот" if len(new) == 1 else f"Новые подходящие лоты: {len(new)}"
    text = f"🆕 <b>{title}</b>\n\n" + "\n\n".join(lot_block(i + 1, r, now) for i, r in enumerate(new[:MAX_LOTS]))
    if len(new) > MAX_LOTS:
        text += f"\n\n…и ещё {len(new) - MAX_LOTS}, смотрите по кнопке ниже"
    return text


HELP = """<b>Что умеет бот</b>

☀️ Каждое утро в {hour}:00 присылает сводку: новые подходящие лоты и лоты из «В работе», у которых скоро срок. Если новостей нет — не беспокоит.

🆕 Днём, до {quiet}:00, сразу пишет о новом лоте с прямым совпадением — не нужно ждать утра.

<b>Команды</b>
/menu — главное меню
/today — сводка прямо сейчас
/new — новые лоты за сутки
/feed — вся лента подходящих лотов
/deadlines — сроки по лотам в работе
/stats — цифры по вашей компании
/settings — включить или выключить уведомления, отвязать чат
/help — эта справка

🎯 — прямое совпадение с вашим каталогом, 🔗 — смежный лот."""

NOT_LINKED = """👋 <b>Это бот TenderTap.</b>

Чат пока не привязан к вашему аккаунту. Откройте сайт → «Личный кабинет» → «Подключить Telegram» и перейдите по ссылке оттуда. Это занимает несколько секунд."""

COMMANDS = [("menu", "Главное меню"), ("today", "Сводка сейчас"), ("new", "Новые лоты за сутки"),
            ("feed", "Вся лента"), ("deadlines", "Сроки по лотам в работе"), ("stats", "Цифры по компании"),
            ("settings", "Настройки"), ("help", "Справка")]


# --- keyboards -------------------------------------------------------------

def _kb(*rows) -> str:
    return json.dumps({"inline_keyboard": [
        [{"text": t, ("url" if d.startswith("http") else "callback_data"): d} for t, d in row] for row in rows]})


def menu_kb() -> str:
    return _kb([("🆕 Новые лоты", "new:0"), ("📋 Вся лента", "feed:0")],
               [("⏰ Сроки", "soon:0"), ("📊 Цифры", "stats")],
               [("⚙️ Настройки", "settings"), ("❓ Справка", "help")],
               [("🌐 Открыть TenderTap", SITE_URL)])


def pager_kb(view: str, page: int, pages: int) -> str:
    rows = []
    if pages > 1:
        rows.append([("◀️", f"{view}:{page - 1}" if page > 0 else "noop"),
                     (f"{page + 1} / {pages}", "noop"),
                     ("▶️", f"{view}:{page + 1}" if page < pages - 1 else "noop")])
    rows.append([("🏠 Меню", "menu"), ("🌐 На сайт", SITE_URL)])
    return _kb(*rows)


def settings_kb(muted: bool) -> str:
    return _kb([("🔔 Включить уведомления" if muted else "🔕 Выключить уведомления", "mute:0" if muted else "mute:1")],
               [("🔌 Отвязать этот чат", "unlink")], [("🏠 Меню", "menu")])


# --- Telegram --------------------------------------------------------------

class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, http_timeout: int = 30, **params) -> dict:
        data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        try:
            with urllib.request.urlopen(self.base + method, data, timeout=http_timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            # 403: the user blocked the bot. The body says why; the URL holds the token.
            return json.load(exc)

    def upload(self, method: str, field: str, path: str, **params) -> dict:
        """A call with one file attached (multipart/form-data)."""
        boundary = "----tenderbin" + os.urandom(8).hex()
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                 for k, v in params.items() if v is not None]
        with open(path, "rb") as f:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                         f'filename="{os.path.basename(path)}"\r\n'
                         f'Content-Type: application/octet-stream\r\n\r\n'.encode() + f.read() + b"\r\n")
        request = urllib.request.Request(self.base + method, b"".join(parts) + f"--{boundary}--\r\n".encode(),
                                         {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            return json.load(exc)

    banner_id = None  # Telegram's id of the uploaded banner: the file is sent once per process

    def send_banner(self, chat_id: int, caption: str, keyboard: str | None = None) -> bool:
        """The banner picture with a caption (1024 characters at most); plain text if the picture fails."""
        common = dict(chat_id=chat_id, caption=caption[:1024], parse_mode="HTML", reply_markup=keyboard)
        try:
            if self.banner_id:
                answer = self.call("sendPhoto", photo=self.banner_id, **common)
            else:
                answer = self.upload("sendPhoto", "photo", os.path.join(ASSETS, "banner.jpg"), **common)
        except OSError as exc:
            answer = {"description": str(exc)}
        if answer.get("ok"):
            Telegram.banner_id = answer["result"]["photo"][-1]["file_id"]
            return True
        logger.warning("chat %s: banner not sent: %s", chat_id, answer.get("description"))
        return self.send(chat_id, caption, keyboard)

    def send(self, chat_id: int, text: str, keyboard: str | None = None, message_id: int | None = None) -> bool:
        """Sends a message, or rewrites `message_id` in place (a button press)."""
        answer = self.call("editMessageText" if message_id else "sendMessage", chat_id=chat_id,
                           message_id=message_id, text=text[:4000], parse_mode="HTML",
                           disable_web_page_preview="true", reply_markup=keyboard)
        if message_id and "no text in the message" in str(answer.get("description")):
            # The button sits under a picture, which cannot become a text: answer below it.
            return self.send(chat_id, text, keyboard)
        ok = bool(answer.get("ok")) or "not modified" in str(answer.get("description"))
        if not ok:
            logger.warning("chat %s: not sent: %s", chat_id, answer.get("description"))
        return ok


# --- the bot ---------------------------------------------------------------

def linked_user(conn, chat_id: int):
    """(user_id, company_id, muted) of an active site user linked to the chat."""
    row = _rows(conn, "SELECT u.id, u.company_id, c.muted FROM bot.chats c JOIN core.users u ON u.id = c.user_id "
                      "WHERE c.chat_id = %(chat)s AND u.is_active AND u.company_id IS NOT NULL", chat=chat_id)
    return row[0] if row else None


def link(conn, tg: Telegram, secret: str, chat_id: int, token: str) -> None:
    user_id = read_token(secret, token)
    site = _kb([("🌐 Открыть TenderTap", SITE_URL)])
    if user_id is None:
        tg.send(chat_id, "⌛️ Ссылка устарела или неверна. Получите новую в личном кабинете на сайте.", site)
        return
    row = _rows(conn, "SELECT c.name FROM core.users u JOIN core.companies c ON c.id = u.company_id "
                      "WHERE u.id = %(id)s AND u.is_active", id=user_id)
    if not row:
        tg.send(chat_id, "Аккаунт не найден или у него нет компании.", site)
        return
    conn.cursor().execute(
        "INSERT INTO bot.chats (chat_id, user_id) VALUES (%s, %s) "
        "ON CONFLICT (chat_id) DO UPDATE SET user_id = EXCLUDED.user_id, muted = false, "
        "linked_at = now(), last_digest_at = NULL", (chat_id, user_id))
    conn.commit()
    tg.send_banner(chat_id, f"✅ <b>Готово!</b> Чат привязан к компании «{esc(row[0])}».\n\n"
                            f"Каждое утро в {DIGEST_HOUR}:00 пришлю новые подходящие лоты и напомню о сроках. "
                            "А пока — загляните в меню.", menu_kb())


def show(conn, tg: Telegram, chat_id: int, view: str, message_id: int | None = None) -> None:
    """Renders one screen; `view` is a command name or a button's callback data."""
    user = linked_user(conn, chat_id)
    if not user:
        site = _kb([("🌐 Открыть TenderTap", SITE_URL)])
        if message_id:
            tg.send(chat_id, NOT_LINKED, site, message_id)
        else:
            tg.send_banner(chat_id, NOT_LINKED, site)
        return
    _, company, muted = user
    name, _, arg = view.partition(":")
    page = int(arg) if arg.lstrip("-").isdigit() else 0

    if name in ("new", "feed", "soon"):
        if name == "new":
            rows = new_lots(conn, company, datetime.now(timezone.utc) - timedelta(hours=24))
            title, empty = "🆕 Новые лоты за сутки", "За сутки новых подходящих лотов не появилось."
        elif name == "feed":
            rows = feed_lots(conn, company)
            title, empty = "📋 Лента", "Лента пока пуста. Проверьте каталог компании на сайте."
        else:
            rows = kanban_lots(conn, company, None)
            title, empty = "⏰ Сроки по лотам в работе", "В работе нет лотов с открытым сроком."
        text, pages = list_page(title, rows, page, empty)
        tg.send(chat_id, text, pager_kb(name, max(0, min(page, pages - 1)), pages), message_id)
    elif name == "today":
        text = digest_text(company_name(conn, company),
                           new_lots(conn, company, datetime.now(timezone.utc) - timedelta(hours=24)),
                           kanban_lots(conn, company, SOON_HOURS))
        tg.send(chat_id, text or "😌 За сутки новых подходящих лотов нет, сроки не горят.", menu_kb(), message_id)
    elif name == "stats":
        feed = feed_lots(conn, company)
        direct = sum(1 for r in feed if r[5] == "direct")
        work = kanban_lots(conn, company, None)
        soon = kanban_lots(conn, company, SOON_HOURS)
        total = f"{sum(r[1] or 0 for r in feed if r[5] == 'direct'):,.0f}".replace(",", " ")
        tg.send(chat_id, f"📊 <b>{esc(company_name(conn, company))}</b>\n\n"
                         f"🎯 Прямых совпадений в ленте: <b>{direct}</b>\n"
                         f"🔗 Смежных лотов: <b>{len(feed) - direct}</b>\n"
                         f"💰 Сумма прямых лотов: <b>{total} ₸</b>\n"
                         f"🗂 В работе с открытым сроком: <b>{len(work)}</b>\n"
                         f"🔥 Из них закрываются за {SOON_HOURS} ч: <b>{len(soon)}</b>", menu_kb(), message_id)
    elif name in ("mute", "stop"):
        muted = name == "stop" or arg == "1"
        conn.cursor().execute("UPDATE bot.chats SET muted = %s WHERE chat_id = %s", (muted, chat_id))
        conn.commit()
        show(conn, tg, chat_id, "settings", message_id)
    elif name == "settings":
        state = "🔕 выключены" if muted else f"🔔 сводка в {DIGEST_HOUR}:00, новые лоты сразу до {QUIET_HOUR}:00"
        tg.send(chat_id, f"⚙️ <b>Настройки</b>\n\nКомпания: {esc(company_name(conn, company))}\n"
                         f"Уведомления: {state}", settings_kb(muted), message_id)
    elif name == "unlink":
        conn.cursor().execute("DELETE FROM bot.chats WHERE chat_id = %s", (chat_id,))
        conn.commit()
        tg.send(chat_id, "🔌 Чат отвязан. Чтобы подключить снова, получите ссылку в личном кабинете.",
                _kb([("🌐 Открыть TenderTap", SITE_URL)]), message_id)
    elif name == "help":
        tg.send(chat_id, HELP.format(hour=DIGEST_HOUR, quiet=QUIET_HOUR), menu_kb(), message_id)
    else:  # menu, start, and anything we do not know
        tg.send(chat_id, f"🏠 <b>Главное меню</b>\n{esc(company_name(conn, company))}\n\nЧто показать?",
                menu_kb(), message_id)


def handle(conn, tg: Telegram, secret: str, update: dict) -> None:
    """One Telegram update: a text message or a button press, private chats only."""
    if "callback_query" in update:
        query = update["callback_query"]
        tg.call("answerCallbackQuery", callback_query_id=query["id"])
        message = query.get("message") or {}
        if query.get("data") != "noop" and message.get("chat", {}).get("type") == "private":
            show(conn, tg, message["chat"]["id"], query["data"], message.get("message_id"))
        return
    message = update.get("message") or {}
    text = message.get("text")
    if not text or message.get("chat", {}).get("type") != "private":
        return
    chat_id = message["chat"]["id"]
    command, _, arg = text.strip().partition(" ")
    command = command.split("@")[0].lower().lstrip("/")
    if command == "start" and arg.strip():
        link(conn, tg, secret, chat_id, arg.strip())
    else:
        show(conn, tg, chat_id, {"deadlines": "soon"}.get(command, command))


def send_digests(conn, tg: Telegram, now: datetime | None = None) -> int:
    """Once a day after DIGEST_HOUR: every unmuted chat that has not had today's."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ALMATY)
    if local.hour < DIGEST_HOUR:
        return 0
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    chats = _rows(conn, "SELECT c.chat_id, u.company_id, c.last_digest_at FROM bot.chats c "
                        "JOIN core.users u ON u.id = c.user_id "
                        "WHERE NOT c.muted AND u.is_active AND u.company_id IS NOT NULL "
                        "AND (c.last_digest_at IS NULL OR c.last_digest_at < %(day)s)", day=day_start)
    sent = 0
    for chat_id, company, last in chats:
        text = digest_text(company_name(conn, company),
                           new_lots(conn, company, last or now - timedelta(hours=24)),
                           kanban_lots(conn, company, SOON_HOURS))
        # Mark the day even when there is nothing to send or the user blocked
        # the bot: one attempt a day, never a loop.
        if text and tg.send(chat_id, text, menu_kb()):
            sent += 1
        conn.cursor().execute("UPDATE bot.chats SET last_digest_at = %s WHERE chat_id = %s", (now, chat_id))
        conn.commit()
    return sent


def send_alerts(conn, tg: Telegram, since: datetime, now: datetime | None = None) -> int:
    """Direct matches first seen after `since`, to every unmuted chat. Only
    between the digest and QUIET_HOUR, and only to chats that already had
    today's digest: the night's lots belong to the digest."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ALMATY)
    if not DIGEST_HOUR <= local.hour < QUIET_HOUR:
        return 0
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    chats = _rows(conn, "SELECT c.chat_id, u.company_id, c.last_digest_at FROM bot.chats c "
                        "JOIN core.users u ON u.id = c.user_id "
                        "WHERE NOT c.muted AND u.is_active AND u.company_id IS NOT NULL "
                        "AND c.last_digest_at >= %(day)s", day=day_start)
    sent, by_company = 0, {}
    for chat_id, company, last in chats:
        start = max(since, last)
        key = (company, start)
        if key not in by_company:
            by_company[key] = new_lots(conn, company, start)
        if by_company[key] and tg.send(chat_id, alert_text(by_company[key], now), menu_kb()):
            sent += 1
    return sent


def main() -> None:
    import psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tg = Telegram(os.environ["BOT_TOKEN"])
    secret, dsn = os.environ["LINK_SECRET"], os.environ["DATABASE_URL"]
    tg.call("setMyCommands", commands=json.dumps([{"command": c, "description": d} for c, d in COMMANDS]))
    tg.call("setMyShortDescription", short_description="Подходящие тендеры для вашей компании: новые лоты и сроки каждое утро.")
    tg.call("setMyDescription", description=(
        "TenderTap подбирает государственные и корпоративные закупки под каталог вашей компании.\n\n"
        "Бот каждое утро присылает новые подходящие лоты и напоминает о сроках по лотам в работе, "
        "а по кнопкам показывает ленту, сроки и цифры по компании.\n\n"
        "Чтобы начать, подключите бота в личном кабинете на " + SITE_URL))
    logger.info("started as @%s", tg.call("getMe").get("result", {}).get("username"))
    offset = 0
    # ponytail: the alert cursor lives in memory, so lots that appear while the
    # bot is down are not alerted (the next digest still lists them); a column
    # in bot.chats if that matters.
    alerted = datetime.now(timezone.utc)
    while True:
        try:
            with psycopg.connect(dsn) as conn:
                while True:
                    sent = send_digests(conn, tg)
                    if sent:
                        logger.info("digest sent to %s chats", sent)
                    now = datetime.now(timezone.utc)
                    if now - alerted >= timedelta(minutes=ALERT_MINUTES):
                        sent = send_alerts(conn, tg, alerted, now)
                        alerted = now
                        if sent:
                            logger.info("alert sent to %s chats", sent)
                    updates = tg.call("getUpdates", http_timeout=60, offset=offset, timeout=50,
                                      allowed_updates='["message","callback_query"]')
                    for update in updates.get("result", []):
                        offset = update["update_id"] + 1
                        try:
                            handle(conn, tg, secret, update)
                        except Exception:  # one bad update must not stop the rest
                            logger.exception("update %s failed", update.get("update_id"))
                            conn.rollback()
                    conn.rollback()  # no transaction stays open across the long poll
        except Exception:  # the database or Telegram is away: wait and reconnect
            logger.exception("loop failed, retrying in 15 s")
            time.sleep(15)


if __name__ == "__main__":
    main()
