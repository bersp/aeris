# :coin: Aeris

A small Telegram bot that tracks what you spend and earn, and works out who
owes what when you share costs. I wrote it for personal use, so it does what I
need and not much more, but feel free to grab it and adapt it to whatever suits
you.

You mostly just type things at it: `coffee 1500` logs a coffee, `pizza 8000
Alice,Bob` splits a pizza. It fixes typos in product names and replies with
monospace tables. Everything is stored in a local SQLite file.

## Features

- Expenses with optional date, note, payer and beneficiaries
- Monthly balances, debts between users and payments to settle them
- Incomes, and your cash position after debts
- Future-dated expenses and installment plans, confirmed the day they are due
- Aliases and product groups to organize your spending
- Removing and rewriting entries, with a confirmation before anything changes
- A chart to compare your spending across months
- A web page to browse and filter everything you have logged

The full command reference is in [documentation.md](documentation.md), which is
also what `/help` serves as a Telegra.ph page.

## Running it

You need Python 3 and a bot token from [@BotFather](https://t.me/BotFather).

```bash
cp .env.example .env   # put your BOT_TOKEN in it
pip install -r requirements.txt
python bot.py
```

Data is stored in `bills.db` next to the bot. By default anyone who finds the
bot can register; to keep it private, set `ALLOWED_USER_IDS` in `.env` to a
comma-separated list of Telegram user ids (you can get the ids from
[@userinfobot](https://t.me/userinfobot)).

### The web pages (optional)

Two Telegram Mini Apps, and Telegram only opens those over HTTPS, so both need a
web server. The "Open chart" button on `/balance` opens `plot.html`, which draws
your cumulative spending month over month; `/ui` opens `ui.html`, which lists
every operation of a period, expands each one on tap, and filters by month, by
date range, by type and by text. Host the two files over HTTPS and set them as
`WEBAPP_URL` and `UI_URL` in `.env`. Either can be left unset: the chart button
just stops appearing, and `/ui` says it is not configured.

Both pages read their figures from a small read-only API the bot serves on
`127.0.0.1:API_PORT` (8081 by default), which starts along with the bot as soon
as either page is configured. Loopback only: put the same web server in front of
it, proxying `/api/` on the host that serves the pages. With Apache that is two
lines in the TLS vhost:

```apache
ProxyPass        /api/ http://127.0.0.1:8081/
ProxyPassReverse /api/ http://127.0.0.1:8081/
```

Requests are authenticated with the signed `initData` Telegram gives the Mini
App, so the API only ever answers for the user Telegram vouched for, and it
never takes a user id from the request.

### Publishing the docs

`/help` replies with a Telegra.ph Instant View page generated from
`documentation.md`. To publish your own copy, run `python publish_telegraph.py`
and set the printed URL as `HELP_URL` in `.env`.
