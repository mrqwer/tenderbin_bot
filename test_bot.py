import json
from datetime import datetime, timedelta, timezone

import bot

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
END = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


def lot(i, fit="direct"):
    return (f"Лот {i} <b>", 1500000, END, f"https://x/{i}?a=1&b=2", "ГУ «Аппарат акима»", fit, "Окна ПВХ")


def test_link_token_round_trip_and_rejections():
    token = bot.make_token("s3cret", 42, now=1000)
    assert len(token) <= 64 and all(c.isalnum() or c in "-_" for c in token)  # Telegram's start limit
    assert bot.read_token("s3cret", token, now=1000) == 42
    assert bot.read_token("s3cret", token, now=1000 + bot.LINK_TTL + 1) is None   # expired
    assert bot.read_token("other", token, now=1000) is None                       # wrong secret
    forged = "2b" + token[2:]                                                     # another user id
    assert bot.read_token("s3cret", forged, now=1000) is None
    assert bot.read_token("s3cret", "garbage", now=1000) is None


def test_lot_block_escapes_and_shows_the_facts():
    text = bot.lot_block(1, lot(0), NOW)
    assert "🎯 <b>1. Лот 0 &lt;b&gt;</b>" in text                      # a lot name cannot break the markup
    assert "💰 1 500 000 ₸ · ⏳ до 20.11 15:00 (осталось 2 дн.)" in text   # Almaty time
    assert "🧩 Ваш товар: <i>Окна ПВХ</i>" in text
    assert '<a href="https://x/0?a=1&amp;b=2">' in text
    assert bot.time_left(NOW + timedelta(hours=5), NOW) == "5 ч"
    assert bot.time_left(NOW + timedelta(minutes=20), NOW) == "меньше часа"


def test_list_pages_and_pager():
    rows = [lot(i) for i in range(12)]
    text, pages = bot.list_page("Лента", rows, 2, "пусто", NOW)
    assert pages == 3 and "всего 12" in text and "11. Лот 10" in text and "Лот 9 " not in text
    assert bot.list_page("Лента", rows, 99, "пусто", NOW)[0] == text          # a page past the end is the last one
    assert bot.list_page("Лента", [], 0, "пусто", NOW) == ("<b>Лента</b>\n\nпусто", 1)
    first = json.loads(bot.pager_kb("feed", 0, 3))["inline_keyboard"][0]
    assert [b["callback_data"] for b in first] == ["noop", "noop", "feed:1"]
    assert len(json.loads(bot.pager_kb("feed", 0, 1))["inline_keyboard"]) == 1  # one page: no arrows
    for row in json.loads(bot.menu_kb())["inline_keyboard"]:
        for button in row:
            assert len(button.get("callback_data", "")) <= 64                   # Telegram's limit


def test_digest_text():
    assert bot.digest_text("ТОО Ромашка", [], []) is None
    text = bot.digest_text("ТОО Ромашка", [lot(i) for i in range(7)], [lot(9, None)], NOW)
    assert "🆕 Новые подходящие лоты: 7" in text and "…и ещё 2" in text and "Лот 5" not in text
    assert "⏰ Скоро срок у лотов в работе: 1" in text


def test_banner_falls_back_to_text_and_edit_under_a_picture_sends_anew(monkeypatch):
    tg, calls = bot.Telegram("x"), []
    monkeypatch.setattr(bot.Telegram, "banner_id", None)
    monkeypatch.setattr(tg, "upload", lambda *a, **k: {"ok": False, "description": "boom"})
    monkeypatch.setattr(tg, "call", lambda method, **k: calls.append((method, k.get("message_id"))) or (
        {"ok": False, "description": "Bad Request: there is no text in the message to edit"}
        if method == "editMessageText" else {"ok": True}))
    assert tg.send_banner(1, "привет") and calls == [("sendMessage", None)]
    calls.clear()
    assert tg.send(1, "лента", message_id=9) and calls == [("editMessageText", 9), ("sendMessage", None)]
