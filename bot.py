"""TenderTap customer bot: a morning digest of new direct lots and of kanban
lots that close soon, for site users who linked their Telegram chat.

Linking: the site gives the user https://t.me/<bot>?start=<token>; the token
is signed with LINK_SECRET, which the backend shares. No table of tokens."""
import base64
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
SOON_HOURS = 48          # a kanban lot closing within this is "soon"
MAX_LOTS = 5             # lots listed per section; the rest are counted

HELP = ("Это бот TenderTap. Каждое утро в {hour}:00 пришлю новые подходящие лоты "
        "и лоты из «В работе», у которых скоро срок.\n\n"
        "/today — сводка сейчас\n/stop — не присылать\n/start — снова присылать")
NOT_LINKED = ("Чат не привязан к аккаунту. Откройте {site} → Личный кабинет → "
              "«Подключить Telegram» и перейдите по ссылке оттуда.")


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


# --- digest ----------------------------------------------------------------

# The feed as the site shows it (direct matches only), limited to lots the
# company had not been recommended before `since`. ml.recommendations is
# rebuilt on every match, so first appearance comes from ml.recommendation_log.
NEW_LOTS = """
SELECT l.name, l.price, l.end_date, l.url
  FROM ml.recommendations rec
  JOIN core.lots l ON l.id = rec.lot_id
 WHERE rec.company_id = %(company)s
   AND rec.evidence->>'fit' = 'direct'
   AND (l.end_date IS NULL OR l.end_date > now())
   AND NOT EXISTS (SELECT 1 FROM ml.recommendation_log g
                    WHERE g.company_id = rec.company_id AND g.lot_id = rec.lot_id AND g.run_at <= %(since)s)
   AND (SELECT f.relevant FROM ml.feedback f
         WHERE f.company_id = rec.company_id AND f.lot_id = rec.lot_id
         ORDER BY f.created_at DESC, f.id DESC LIMIT 1) IS DISTINCT FROM false
   AND NOT EXISTS (SELECT 1 FROM core.kanban_cards kc
                    WHERE kc.company_id = rec.company_id AND kc.lot_id = l.id)
 ORDER BY NOT COALESCE((rec.evidence->>'has_spec')::boolean, false), rec.score DESC
"""

CLOSING_SOON = """
SELECT l.name, l.price, l.end_date, l.url
  FROM core.kanban_cards kc
  JOIN core.lots l ON l.id = kc.lot_id
 WHERE kc.company_id = %(company)s
   AND l.end_date > now() AND l.end_date < now() + make_interval(hours => %(hours)s)
   AND NOT EXISTS (SELECT 1 FROM core.kanban_outcomes ko
                    WHERE ko.company_id = kc.company_id AND ko.lot_id = kc.lot_id)
 ORDER BY l.end_date
"""


def _lot_line(name, price, end_date, url) -> str:
    parts = [(name or "Без названия").strip()[:90]]
    if price:
        parts.append(f"{price:,.0f} ₸".replace(",", " "))
    if end_date:
        parts.append("до " + end_date.astimezone(ALMATY).strftime("%d.%m %H:%M"))
    line = "• " + " — ".join(parts)
    return f"{line}\n  {url}" if url else line


def _section(title: str, rows: list) -> str:
    lines = [f"{title}: {len(rows)}"] + [_lot_line(*r) for r in rows[:MAX_LOTS]]
    if len(rows) > MAX_LOTS:
        lines.append(f"…и ещё {len(rows) - MAX_LOTS}")
    return "\n".join(lines)


def digest_text(company_name: str, new: list, soon: list) -> str | None:
    """The message for one company; None when there is nothing to say."""
    if not new and not soon:
        return None
    parts = [f"TenderTap · {company_name}"]
    if new:
        parts.append(_section("Новые подходящие лоты", new))
    if soon:
        parts.append(_section("Скоро срок у лотов в работе", soon))
    parts.append(f"Лента: {SITE_URL}")
    return "\n\n".join(parts)


def company_digest(conn, company_id: int, since: datetime) -> str | None:
    cur = conn.cursor()
    cur.execute("SELECT name FROM core.companies WHERE id = %s", (company_id,))
    row = cur.fetchone()
    if not row:
        return None
    cur.execute(NEW_LOTS, {"company": company_id, "since": since})
    new = cur.fetchall()
    cur.execute(CLOSING_SOON, {"company": company_id, "hours": SOON_HOURS})
    return digest_text(row[0], new, cur.fetchall())


# --- Telegram --------------------------------------------------------------

class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, http_timeout: int = 30, **params) -> dict:
        data = urllib.parse.urlencode(params).encode()
        try:
            with urllib.request.urlopen(self.base + method, data, timeout=http_timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            # 403: the user blocked the bot. The body says why; the URL holds the token.
            return json.load(exc)

    def send(self, chat_id: int, text: str) -> bool:
        answer = self.call("sendMessage", chat_id=chat_id, text=text[:4000],
                           disable_web_page_preview="true")
        if not answer.get("ok"):
            logger.warning("chat %s: not sent: %s", chat_id, answer.get("description"))
        return bool(answer.get("ok"))


# --- the bot ---------------------------------------------------------------

def linked_user(conn, chat_id: int):
    """(user_id, company_id) of an active site user linked to the chat."""
    cur = conn.cursor()
    cur.execute("SELECT u.id, u.company_id FROM bot.chats c JOIN core.users u ON u.id = c.user_id "
                "WHERE c.chat_id = %s AND u.is_active AND u.company_id IS NOT NULL", (chat_id,))
    return cur.fetchone()


def handle(conn, tg: Telegram, secret: str, chat_id: int, text: str) -> None:
    command, _, arg = text.strip().partition(" ")
    command = command.split("@")[0].lower()
    cur = conn.cursor()

    if command == "/start" and arg:
        user_id = read_token(secret, arg.strip())
        if user_id is None:
            tg.send(chat_id, "Ссылка устарела или неверна. Получите новую в личном кабинете: " + SITE_URL)
            return
        cur.execute("SELECT c.name FROM core.users u JOIN core.companies c ON c.id = u.company_id "
                    "WHERE u.id = %s AND u.is_active", (user_id,))
        row = cur.fetchone()
        if not row:
            tg.send(chat_id, "Аккаунт не найден или у него нет компании.")
            return
        cur.execute("INSERT INTO bot.chats (chat_id, user_id) VALUES (%s, %s) "
                    "ON CONFLICT (chat_id) DO UPDATE SET user_id = EXCLUDED.user_id, muted = false, "
                    "linked_at = now(), last_digest_at = NULL", (chat_id, user_id))
        conn.commit()
        tg.send(chat_id, f"Готово: чат привязан к компании «{row[0]}».\n\n" + HELP.format(hour=DIGEST_HOUR))
        return

    user = linked_user(conn, chat_id)
    if not user:
        tg.send(chat_id, NOT_LINKED.format(site=SITE_URL))
    elif command in ("/start", "/stop"):
        cur.execute("UPDATE bot.chats SET muted = %s WHERE chat_id = %s", (command == "/stop", chat_id))
        conn.commit()
        tg.send(chat_id, "Больше не присылаю. /start — вернуть." if command == "/stop" else "Снова присылаю сводку по утрам.")
    elif command == "/today":
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        tg.send(chat_id, company_digest(conn, user[1], since) or "За сутки новых подходящих лотов нет, сроки не горят.")
    else:
        tg.send(chat_id, HELP.format(hour=DIGEST_HOUR))


def send_digests(conn, tg: Telegram, now: datetime | None = None) -> int:
    """Once a day after DIGEST_HOUR: every unmuted chat that has not had today's."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ALMATY)
    if local.hour < DIGEST_HOUR:
        return 0
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    cur = conn.cursor()
    cur.execute("SELECT c.chat_id, u.company_id, c.last_digest_at FROM bot.chats c "
                "JOIN core.users u ON u.id = c.user_id "
                "WHERE NOT c.muted AND u.is_active AND u.company_id IS NOT NULL "
                "AND (c.last_digest_at IS NULL OR c.last_digest_at < %s)", (day_start,))
    sent = 0
    for chat_id, company_id, last in cur.fetchall():
        text = company_digest(conn, company_id, last or now - timedelta(hours=24))
        # Mark the day even when there is nothing to send or the user blocked
        # the bot: one attempt a day, never a loop.
        if text and tg.send(chat_id, text):
            sent += 1
        cur.execute("UPDATE bot.chats SET last_digest_at = %s WHERE chat_id = %s", (now, chat_id))
        conn.commit()
    return sent


def main() -> None:
    import psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tg = Telegram(os.environ["BOT_TOKEN"])
    secret, dsn = os.environ["LINK_SECRET"], os.environ["DATABASE_URL"]
    logger.info("started as @%s", tg.call("getMe").get("result", {}).get("username"))
    offset = 0
    while True:
        try:
            with psycopg.connect(dsn) as conn:
                while True:
                    sent = send_digests(conn, tg)
                    if sent:
                        logger.info("digest sent to %s chats", sent)
                    updates = tg.call("getUpdates", http_timeout=60, offset=offset, timeout=50)
                    for update in updates.get("result", []):
                        offset = update["update_id"] + 1
                        message = update.get("message") or {}
                        if message.get("text") and message.get("chat", {}).get("type") == "private":
                            handle(conn, tg, secret, message["chat"]["id"], message["text"])
                    conn.rollback()  # no transaction stays open across the long poll
        except Exception:  # the database or Telegram is away: wait and reconnect
            logger.exception("loop failed, retrying in 15 s")
            time.sleep(15)


if __name__ == "__main__":
    main()
