# tenderbin_bot

Telegram bot for TenderTap customers.

- **Morning digest** (09:00 Almaty), one message per linked user: new
  direct-match lots in the company's feed since the last digest, and lots on
  the kanban that close within 48 hours. Nothing to say — no message.
- **Menu with buttons**: new lots of the last day, the whole feed and the
  kanban deadlines as paged lists, the company's numbers, settings (digest
  on/off, unlink the chat), help.
- **Commands**: `/menu`, `/today`, `/new`, `/feed`, `/deadlines`, `/stats`,
  `/settings`, `/help`; Telegram shows them in the command list.

The bot only reads the site's data; marking a lot or taking it to work stays
on the site.

## Linking a chat to a site user

The site shows the user `https://t.me/<bot>?start=<token>`. The token is
`<user id hex>-<expiry hex>-<HMAC-SHA256(LINK_SECRET, "<id>-<expiry>"), base64url, first 22 chars>`
and lives 15 minutes. The backend makes it with the same `LINK_SECRET`; the bot
only verifies it, so there is no table of tokens. `bot.make_token` is the
reference implementation.

Until the site has the button, link by hand on the server:

    docker compose exec bot python -c "import os, bot; print('https://t.me/<bot>?start=' + bot.make_token(os.environ['LINK_SECRET'], <user id>))"

## Run

1. Database, once (as postgres): `migrations/001_bot.sql`, then set the
   password of role `bot_rw` (commands are at the top of the file). The role
   reads lots, recommendations and the kanban, three columns of `core.users`
   (no e-mails, no password hashes), and writes only `bot.chats`.
2. `cp .env.example .env` and fill it in.
3. `docker compose up -d --build`, then `docker compose logs -f bot`.

One process, long polling: run a single copy.

## Tests

    python -m pytest -q
