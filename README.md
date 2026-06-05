# Bills

Telegram bot to manage personal expenses.

## Commands

- `/register <name>` — register with a name, or rename yourself if already
  registered (reports the rename from the old name to the new one)
- `/add <product> <price>` — record an own expense (paid and consumed by you)
- `/add <product> <price> <payer> <beneficiaries>` — record a shared expense.
  Beneficiaries are separated by `/` (e.g. `bernardo/mercedes`) or `all` for
  every registered user. The cost is split equally; each beneficiary other than
  the payer ends up owing the payer their share.
- An optional purchase date can be given as the first argument, as `dd/mm/yy`,
  `dd/mm` or `dd` (missing parts are taken from today); e.g. `/add 01/06 carne
  800`. Without it, today is used.
- Products cannot be numbers.
- A product may carry a description in parentheses, e.g. `/add others(some note)
  500`. The description can contain spaces and is shown only in `/detailedlist`;
  in `/list` and `/balance` the product is marked with a trailing `+` instead
  (e.g. `others+`).
- A product matching one you already used is standardized to your original
  spelling. This covers both case (after `Pizza`, both `pizza` and `PIZZA` become
  `Pizza`) and a single-letter typo —insertion, deletion or substitution— (e.g.
  `Pizzza`, `Piza` and `Pozza` all become `Pizza`). A product you have never used
  before is flagged with a trailing `*` in the confirmation (e.g.
  `Added Pizza* ($20)`).
- `/addf <product> <price> [payer] [beneficiaries]` — same as `/add` but forces
  the product exactly as typed, skipping the case/typo standardization above.
  Useful for short products legitimately similar to existing ones (e.g. adding
  `Sofa` when you already have `Soda`). A product whose exact name is new is
  still flagged with a trailing `*`. Same optional date argument as `/add`.
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
- In `/list` and `/balance`, product names longer than 7 characters are shown as
  their first 5 characters plus `..` (e.g. `Supermarket` → `Super..`); products
  with a description keep the `+` within the same 7-character width (e.g.
  `Supermarket+` → `Supe..+`).
- `/products` — list your products (those you paid for or consumed) with how many
  times each was bought and your aliases for them, most-bought first
- `/alias <alias> <product>` — define a personal shortcut so typing the alias in
  `/add` records the product instead (e.g. `/alias super Supermarket`, then `/add
  super 1500` records `Supermarket`). Aliases are matched ignoring case and take
  precedence over the case/typo standardization. Use `/alias -d <alias>` to
  delete one.
- `/balance` — show your net debts with the other users
- `/pay <payee> <amount>` — pay money to another user, settling part or all of a
  debt (a money-only transaction, no product involved). You can also write
  `/pay <payer> <payee> <amount>` so anyone can record a payment between two
  users. Everyone involved is notified.
- `/help` (or `/start`) — show the list of commands

Each expense and payment gets an automatic id in the form `yymmddss` (date plus
two random letters).

You must register with `/register` before using any other command (except
`/start` and `/help`). Any name referenced in a command (`/add` payer and beneficiaries,
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
