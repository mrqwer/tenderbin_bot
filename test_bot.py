from datetime import datetime, timezone

import bot


def test_link_token_round_trip_and_rejections():
    token = bot.make_token("s3cret", 42, now=1000)
    assert len(token) <= 64 and all(c.isalnum() or c in "-_" for c in token)  # Telegram's start limit
    assert bot.read_token("s3cret", token, now=1000) == 42
    assert bot.read_token("s3cret", token, now=1000 + bot.LINK_TTL + 1) is None   # expired
    assert bot.read_token("other", token, now=1000) is None                       # wrong secret
    forged = "2b" + token[2:]                                                     # another user id
    assert bot.read_token("s3cret", forged, now=1000) is None
    assert bot.read_token("s3cret", "garbage", now=1000) is None


def test_digest_text():
    assert bot.digest_text("ТОО Ромашка", [], []) is None
    end = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    new = [(f"Лот {i}", 1500000, end, f"https://x/{i}") for i in range(7)]
    text = bot.digest_text("ТОО Ромашка", new, [("Окна ПВХ", None, end, None)])
    assert "Новые подходящие лоты: 7" in text
    assert "• Лот 0 — 1 500 000 ₸ — до 20.11 15:00\n  https://x/0" in text   # Almaty time
    assert "…и ещё 2" in text and "Лот 5" not in text
    assert "Скоро срок у лотов в работе: 1\n• Окна ПВХ — до 20.11 15:00" in text
