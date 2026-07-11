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
- A chart to compare your spending across months

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
comma-separated list of Telegram user ids.

### The chart (optional)

The "Open chart" button on `/balance` opens a Telegram Mini App, and Telegram
only opens those over HTTPS. So this part needs a web server: host `plot.html`
at some HTTPS URL and set it as `WEBAPP_URL` in `.env`. Any static hosting
works; the page has no backend, the data travels in the URL fragment. Without
`WEBAPP_URL` everything else runs fine, the button just doesn't appear.

### Publishing the docs

`/help` replies with a Telegra.ph Instant View page generated from
`documentation.md`. To publish your own copy, run `python publish_telegraph.py`
and set the printed URL as `HELP_URL` in `.env`.
