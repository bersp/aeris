# Bills

Telegram bot to manage personal expenses.

## Commands

- `/register <name>` — register with a name
- `/rename <name>` — change your name
- `/add <product> <price>` — record an own expense (paid and consumed by you)
- `/add <product> <price> <payer> <beneficiaries>` — record a shared expense.
  Beneficiaries are separated by `/` (e.g. `bernardo/mercedes`) or `all` for
  every registered user. The cost is split equally; each beneficiary other than
  the payer ends up owing the payer their share.
- An optional purchase date can be given as the first argument, as `dd/mm/yy`,
  `dd/mm` or `dd` (missing parts are taken from today); e.g. `/add 01/06 carne
  800`. Without it, today is used.
- Products cannot be numbers.
- A confirmation with the product, price, payer, consumers and id is sent to
  everyone involved in the expense.
- `/remove <last|id suffix>` — remove an expense (the latest one whose id ends
  with the given digits; `last` removes the most recent one)
- `/list` — show your operations (expenses and payments) as a table with
  columns: day, product, price, `Pay` (payer, first two letters) and `For`
  (consumer: first two letters, `All` if every registered user, `Mix` if several
  but not all). By default the last 6; optionally `n N` (last N), `m [K]` (last K
  months, default 1) or `d [K]` (last K days, default today).
- `/detailedlist` — same operations and same optional arguments as `/list`, but
  one field per line (full date, product, price, payer name, consumers and full
  id), separated by `----`
- `/balance` — show your net debts with the other users
- `/pay <payee> <amount>` — pay money to another user, settling part or all of a
  debt (a money-only transaction, no product involved). You can also write
  `/pay <payer> <payee> <amount>` so anyone can record a payment between two
  users. Everyone involved is notified.

Each expense and payment gets an automatic id in the form `yymmddss` (date plus
two random letters).

You must register with `/register` before using any other command (except
`/start`). Any name referenced in a command (`/add` payer and beneficiaries,
`/pay` recipient) must also belong to a registered user; otherwise the command
is rejected.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Copy `.env.example` to `.env` and fill in `BOT_TOKEN`.
3. Install the dependencies and run the bot:

```bash
pip install -r requirements.txt
python bot.py
```
