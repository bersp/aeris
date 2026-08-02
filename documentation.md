# Aeris

Aeris tracks what you spend and earn, and works out who owes what when you share costs. Run `/help` to see this again.

The leading slash is optional: `income salary 6000` means `/income salary 6000`. An unknown first word means `/expense`, so `supermarket 1500` becomes `/expense supermarket 1500`.

## Register

```
/register <name>
```

Set your name first; others use it to share expenses. Run again to change it.

## Recording an expense

```
/expense [date] <product[(note)]> <price> [payer] [beneficiaries]
```

The everyday command, also `/exp`. Simplest is a product and price: `/exp coffee 1500`, dated today, paid by you. A leading date sets the day: `05`, `05/04` or `05/04/25` (e.g. `/exp 05/04 coffee 1500`). Parentheses after the product become a note: `/exp coffee(with Bob) 1500`. A multi-word product is typed with underscores: `ice_cream` is shown as `ice cream`. Aeris remembers products and fixes typos (`cofee` → `coffee`); a new one is marked with a *.

```
/forceexpense [date] <product> <price> [payer] [beneficiaries]
```

Like `/expense` but keeps the product exactly as typed, no typo matching. Short: `/fexp`.

## Sharing an expense

List who shares the cost after the price, comma-separated: `/exp pizza 8000 Alice,Bob` means you paid, Alice and Bob split it. `all` means everyone registered: `/exp supermarket 20000 all`. Another payer goes first: `/exp pizza 8000 Bob Alice,Carol`.

## Scheduling

A date after today defers an expense: nothing is recorded now, and on that day at 9am Aeris sends you its details with Add and Reject buttons. `/exp 20/12 gift 5000` asks you on the 20th.

```
/scheduleexpense <count><m|d> <start date> <product> <price> [payer] [beneficiaries]
```

Pay in installments, also `/sexp`. The first field sets how many charges and the gap between them: `6m` is six monthly, `6d` six daily. `/sexp 6m 15/08 rent 5000` asks you to confirm rent on the 15th of each month for six months.

```
/scheduled
```

Every upcoming expense, one-off deferrals and installments alike, each flagged as simple or installment. Cancel one with `/rm` and its id.

## Removing an entry

```
/remove <id suffix>
```

Delete something logged by mistake, also `/rm`. Works on expenses, incomes, payments and scheduled expenses alike. Pass the last few characters of its id, shown in replies and the detailed lists: `/rm fk`.

## Reviewing your spending

```
/list [n N | m [K] | d [K]]
```

Your operations as a compact table, this month by default. Other windows: `/list n 10` (last ten), `/list m 3` (three months), `/list d 7` (seven days).

```
/detailedlist [n N | m [K] | d [K]]
```

Same entries, fully detailed. Short: `/dlist`.

```
/balance [m [K]]
```

Expenses by month, who owes whom, and your cash. A month count goes back further: `/balance m 3`. The Open chart button plots your spending day by day, overlaying the months shown (between two and six), with your monthly income as a reference line.

```
/ui
```

Opens a web page listing everything you have logged, newest first, with the date on each entry. Tap one to see its full detail: price, your share, who paid, who it was for, and its id; tapping the detail closes it again. Filter by month, by a date range you pick, by type, or by typing part of a product or note. The Scheduled tab lists what is still coming, installments included, ignoring the period the rest of the page is showing. There is also a breakdown by product for whatever period you are looking at.

## Settling up

```
/pay [payer] <payee> <amount>
```

Record a cash payment against a debt. `/pay Bob 5000` means you paid Bob. A payment between others names the payer first: `/pay Alice Bob 5000`.

## Income

```
/income [date] <name> <amount>
```

Money coming in, also `/inc`: `/inc salary 300000`. Dates work as in expenses.

```
/incomes [m [K]]
```

Your income per month with a total; takes a month count like `/balance`.

```
/detailedincomes [n N | m [K] | d [K]]
```

Your incomes, fully detailed. Short: `/dincomes`.

## Aliases and products

```
/alias <alias> <product>
```

A shortcut for a long name: `/alias g groceries` lets `g` stand for `groceries`.

```
/rmalias <alias>
```

Removes an alias.

```
/products
```

Every product you bought, how often, and its aliases.

## Groups

```
/group <name> <product,product,...>
```

Fold products into one balance line: `/group transport taxi,subway,train` shows their total as `transport`. Run again with the same name to add more.

```
/ungroup <name>
```

Undo a group.

```
/groups
```

Your groups and their products.

## Display

```
/tablewidth [number]
```

Resize the tables if they wrap: `/tablewidth 40`. No number restores the default.
